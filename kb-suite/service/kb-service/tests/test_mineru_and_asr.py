"""MinerU OCR for images and scanned PDFs, and speech to text, against simulated HTTP services."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys

import httpx
import pytest

from kb_service import jobs
from kb_service.parsing import (
    AUDIO_TYPES,
    MINERU_IMAGE_TYPES,
    SUPPORTED_TYPES,
    ParseError,
    ParseResult,
    SourceBlock,
    asr_client,
    docreader_client,
    mineru_client,
    mineru_types,
    parse_file,
    service_slot,
    supported_types,
)

MINERU_URL = "https://mineru.example.com/"
ASR_URL = "https://asr.example.com/v1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


@pytest.fixture
def configure(monkeypatch):
    """``configure(name=value, ...)`` sets KBS_* variables and reloads the settings."""

    from kb_service.config import get_settings

    def apply(**values):
        for key, value in values.items():
            monkeypatch.setenv(f"KBS_{key.upper()}", str(value))
        get_settings.cache_clear()
        return get_settings()

    yield apply
    get_settings.cache_clear()


def form_field(body: bytes, name: str) -> str:
    match = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n', body, re.DOTALL)
    return match.group(1).decode() if match else ""


class FakeMinerU:
    """mineru-api 3.x task API: submit, poll the status, fetch the result."""

    def __init__(self, md: str = "", content_list=None, states=("processing", "completed"), error: str = "") -> None:
        self.md = md
        self.content_list = content_list or []
        self.states = list(states)
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.forms: list[bytes] = []
        self.polls: dict[str, int] = {}
        self.in_flight = 0
        self.max_in_flight = 0

    def state_of(self, task_id: str) -> str:
        count = self.polls.get(task_id, 0)
        self.polls[task_id] = count + 1
        return self.states[min(count, len(self.states) - 1)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, str(request.url)))
        if request.method == "POST" and path == "/tasks":
            self.forms.append(request.content)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            return httpx.Response(202, json={"task_id": f"task-{len(self.forms)}", "status": "pending"})
        task_id = path.split("/")[2]
        if path.endswith("/result"):
            self.in_flight -= 1
            return httpx.Response(
                200,
                json={"task_id": task_id, "results": {"scan": {"md_content": self.md, "content_list": json.dumps(self.content_list)}}},
            )
        return httpx.Response(200, json={"task_id": task_id, "status": self.state_of(task_id), "error": self.error or None})


@pytest.fixture
def mineru(monkeypatch, configure):
    """``mineru(handler, **settings)`` turns MinerU on and routes its calls to ``handler``."""

    def install(handler, **settings):
        configure(**{"mineru_enabled": "true", "mineru_api_url": MINERU_URL, **settings})
        monkeypatch.setattr(mineru_client, "_transport", httpx.MockTransport(handler))
        monkeypatch.setattr(mineru_client, "POLL_INTERVAL_S", 0.01)
        return handler

    return install


def parse_error(name: str, data: bytes) -> ParseError:
    with pytest.raises(ParseError) as info:
        asyncio.run(parse_file(name, data))
    return info.value


# --- switched off: unchanged behaviour ---------------------------------------


def test_off_by_default_keeps_images_and_audio_unsupported(configure):
    settings = configure()
    assert not settings.mineru_enabled and not settings.asr_enabled
    assert supported_types(settings) == SUPPORTED_TYPES
    assert not SUPPORTED_TYPES & (MINERU_IMAGE_TYPES | AUDIO_TYPES)
    assert parse_error("photo.png", PNG).code == "unsupported_without_ocr"
    assert parse_error("scan.tif", PNG).code == "unsupported_type"
    assert parse_error("talk.mp3", b"ID3" + b"\x00" * 32).code == "unsupported_type"


def test_scanned_pdf_is_left_to_docreader_when_off(configure, monkeypatch):
    configure()
    monkeypatch.setattr(docreader_client, "read_file", _scanned_docreader)
    result = asyncio.run(parse_file("scan.pdf", PDF))
    assert result.engine == "docreader-builtin" and "images/scan_page_1.jpg" in result.markdown


# --- declared file types -----------------------------------------------------


def test_file_types_follow_the_settings(configure):
    settings = configure(mineru_enabled="true", mineru_api_url=MINERU_URL)
    assert {"png", "jpg", "jpeg", "tif", "tiff", "webp", "pdf"} <= supported_types(settings)
    settings = configure(mineru_file_types="pdf")
    assert mineru_types(settings) == {"pdf"} and "png" not in supported_types(settings)
    settings = configure(mineru_file_types=".PNG, jpg, docx")
    assert mineru_types(settings) == {"png", "jpg"}
    settings = configure(mineru_enabled="false", asr_enabled="true")
    assert AUDIO_TYPES <= supported_types(settings) and "png" not in supported_types(settings)


def test_capabilities_declare_image_types_when_mineru_is_on():
    code = (
        "import asyncio, json\n"
        "from kb_service.api.management import capabilities\n"
        "print(json.dumps(asyncio.run(capabilities(caller=None))['file_types']))\n"
    )
    env = {**os.environ, "KBS_MINERU_ENABLED": "true", "KBS_MINERU_API_URL": MINERU_URL, "KBS_ASR_ENABLED": "false"}
    out = subprocess.run(  # NOSONAR(S4721): this interpreter with a fixed program, no shell
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, check=True
    )
    file_types = json.loads(out.stdout.strip().splitlines()[-1])
    assert {"png", "jpg", "tif", "pdf", "docx"} <= set(file_types)
    assert "mp3" not in file_types


# --- images ------------------------------------------------------------------


def test_image_text_comes_from_mineru(mineru):
    fake = mineru(FakeMinerU(md="# 合成票据\n\n合成编号 KB-0001\n\n![](images/abc.jpg)\n", states=("pending", "processing", "completed")))
    result = asyncio.run(parse_file("scan.png", PNG))
    assert "合成编号 KB-0001" in result.markdown and "images/" not in result.markdown
    assert result.engine == "mineru-pipeline" and result.source_blocks == []
    assert result.metadata == {"ocr_engine": "mineru", "mineru_backend": "pipeline", "mineru_task_id": "task-1"}
    assert fake.calls[0] == ("POST", "https://mineru.example.com/tasks")
    assert fake.calls[-1] == ("GET", "https://mineru.example.com/tasks/task-1/result")
    assert fake.polls == {"task-1": 3}
    form = fake.forms[0]
    assert form_field(form, "backend") == "pipeline" and form_field(form, "parse_method") == "ocr"
    assert form_field(form, "return_images") == "false" and form_field(form, "lang_list") == "ch"
    assert b'name="files"; filename="scan.png"' in form


def test_image_without_text_fails_with_a_reason(mineru):
    mineru(FakeMinerU(md="![](images/photo.jpg)\n"))
    error = parse_error("photo.jpg", PNG)
    assert error.code == "empty_content" and "没有在图片中识别出文字" in error.message


def test_failed_task_reports_mineru_reason(mineru):
    mineru(FakeMinerU(states=("processing", "failed"), error="synthetic model load failure"))
    error = parse_error("scan.png", PNG)
    assert error.code == "parse_failed" and not error.retriable
    assert "MinerU 识别失败" in error.message and "synthetic model load failure" in error.message


def test_hybrid_backend_falls_back_to_pipeline(mineru):
    fake = FakeMinerU(md="合成正文 Gamma")
    states = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and not request.url.path.endswith("/result"):
            task_id = request.url.path.split("/")[2]
            return httpx.Response(200, json={"status": states.get(task_id), "error": "synthetic engine error"})
        response = fake(request)
        if request.method == "POST":
            backend = form_field(fake.forms[-1], "backend")
            states[f"task-{len(fake.forms)}"] = "failed" if backend.startswith("hybrid") else "completed"
        return response

    mineru(handler, mineru_backend="hybrid-auto-engine")
    result = asyncio.run(parse_file("scan.png", PNG))
    assert result.engine == "mineru-pipeline" and "Gamma" in result.markdown
    assert [form_field(form, "backend") for form in fake.forms] == ["hybrid-auto-engine", "pipeline"]


def test_unreachable_mineru_is_retried_later(mineru):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    mineru(refuse)
    error = parse_error("scan.png", PNG)
    assert error.code == "mineru_error" and error.retriable and "连不上 MinerU" in error.message


def test_server_error_is_retried_with_its_detail(mineru):
    mineru(lambda request: httpx.Response(503, json={"detail": "Task manager is not initialized"}))
    error = parse_error("scan.png", PNG)
    assert error.code == "mineru_error" and error.retriable
    assert "HTTP 503" in error.message and "Task manager is not initialized" in error.message


def test_slow_task_times_out(mineru):
    mineru(FakeMinerU(states=("processing",)), mineru_timeout_s="0.2")
    error = parse_error("scan.png", PNG)
    assert error.code == "mineru_error" and not error.retriable and "KBS_MINERU_TIMEOUT_S" in error.message


def test_mineru_without_task_api_is_explained(mineru):
    mineru(lambda request: httpx.Response(404, json={"detail": "Not Found"}))
    error = parse_error("scan.png", PNG)
    assert error.code == "mineru_error" and "3.x" in error.message


def test_enabled_without_address_fails_with_a_reason(configure):
    configure(mineru_enabled="true", mineru_api_url="")
    error = parse_error("scan.png", PNG)
    assert error.code == "mineru_error" and "KBS_MINERU_API_URL" in error.message


def test_mineru_calls_are_capped_per_process(mineru):
    fake = mineru(FakeMinerU(md="合成正文", states=("processing", "completed")))

    async def both():
        return await asyncio.gather(parse_file("a.png", PNG), parse_file("b.png", PNG))

    results = asyncio.run(both())
    assert [r.metadata["mineru_task_id"] for r in results] == ["task-1", "task-2"]
    assert fake.max_in_flight == 1


def test_busy_slot_puts_the_job_back_without_an_attempt():
    async def scenario():
        async with service_slot("synthetic", 1, 1.0):
            with pytest.raises(jobs.Defer):
                async with service_slot("synthetic", 1, 0.05):
                    pass
        async with service_slot("synthetic", 1, 0.05):
            pass

    asyncio.run(scenario())


# --- scanned PDFs ------------------------------------------------------------


async def _scanned_docreader(file_name, file_type, data, *, engine="builtin") -> ParseResult:
    lines, blocks, offset = [], [], 0
    for page in (1, 2, 3):
        line = f"![scan_page_{page}.jpg](images/scan_page_{page}.jpg)"
        offset += 2 if lines else 0
        blocks.append(SourceBlock(offset, offset + len(line), {"type": "pdf", "page": page}))
        lines.append(line)
        offset += len(line)
    return ParseResult(
        markdown="\n\n".join(lines),
        engine="docreader-builtin",
        source_blocks=blocks,
        metadata={"image_source_type": "scanned_pdf", "page_count": "3"},
        title="合成扫描件",
    )


async def _text_docreader(file_name, file_type, data, *, engine="builtin") -> ParseResult:
    page_text = "合成正文段落。" * 20
    markdown = "\n\n".join([page_text] * 3)
    blocks = [SourceBlock(i * (len(page_text) + 2), i * (len(page_text) + 2) + len(page_text), {"type": "pdf", "page": i + 1}) for i in range(3)]
    return ParseResult(markdown=markdown, engine="docreader-builtin", source_blocks=blocks)


SCANNED_MD = "# 第一章\n\n第一页的合成正文 Alpha\n\n第二页的合成正文 Beta\n\n<table><tr><td>合成表格 Delta</td></tr></table>\n"
SCANNED_CONTENT = [
    {"type": "text", "text": "第一章", "text_level": 1, "page_idx": 0},
    {"type": "text", "text": "第一页的合成正文 Alpha", "page_idx": 0},
    # Not in md_content; matching its text further down would skip page 2.
    {"type": "footer", "text": "Delta", "page_idx": 0},
    {"type": "text", "text": "第二页的合成正文 Beta", "page_idx": 1},
    {"type": "table", "table_body": "<table><tr><td>合成表格 Delta</td></tr></table>", "page_idx": 2},
]


def test_scanned_pdf_is_read_again_by_mineru(mineru, monkeypatch):
    fake = mineru(FakeMinerU(md=SCANNED_MD, content_list=SCANNED_CONTENT))
    monkeypatch.setattr(docreader_client, "read_file", _scanned_docreader)
    result = asyncio.run(parse_file("scan.pdf", PDF))
    assert result.engine == "mineru-pipeline" and result.markdown == SCANNED_MD
    assert result.title == "合成扫描件"
    assert result.metadata["ocr_reason"] == "scanned_pdf" and result.metadata["page_count"] == "3"
    assert form_field(fake.forms[0], "parse_method") == "ocr"
    pages = {b.locator["page"]: result.markdown[b.start : b.end] for b in result.source_blocks}
    assert sorted(pages) == [1, 2, 3]
    assert pages[1].startswith("# 第一章") and "Alpha" in pages[1]
    assert "Beta" in pages[2] and "Delta" in pages[3]
    assert all(b.locator["type"] == "pdf" for b in result.source_blocks)


def test_text_pdf_stays_with_docreader(mineru, monkeypatch):
    fake = mineru(FakeMinerU(md="不应被调用"))
    monkeypatch.setattr(docreader_client, "read_file", _text_docreader)
    result = asyncio.run(parse_file("report.pdf", PDF))
    assert result.engine == "docreader-builtin" and fake.calls == []


def test_scan_where_mineru_finds_nothing_keeps_docreader_result(mineru, monkeypatch):
    mineru(FakeMinerU(md="![](images/p1.jpg)\n"))
    monkeypatch.setattr(docreader_client, "read_file", _scanned_docreader)
    result = asyncio.run(parse_file("scan.pdf", PDF))
    assert result.engine == "docreader-builtin" and "scan_page_1.jpg" in result.markdown


def test_pdf_left_out_of_mineru_file_types_is_not_rescued(mineru, monkeypatch):
    fake = mineru(FakeMinerU(md=SCANNED_MD), mineru_file_types="png,jpg")
    monkeypatch.setattr(docreader_client, "read_file", _scanned_docreader)
    result = asyncio.run(parse_file("scan.pdf", PDF))
    assert result.engine == "docreader-builtin" and fake.calls == []


# --- speech to text ----------------------------------------------------------


@pytest.fixture
def asr(monkeypatch, configure):
    def install(handler, **settings):
        configure(**{"asr_enabled": "true", "asr_api_url": ASR_URL, **settings})
        monkeypatch.setattr(asr_client, "_transport", httpx.MockTransport(handler))

    return install


def test_audio_segments_become_timed_lines(asr):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["form"] = request.content
        return httpx.Response(
            200,
            json={
                "text": "ignored when segments exist",
                "language": "zh",
                "segments": [
                    {"start": 0.0, "end": 2.5, "text": " 合成会议录音第一句。"},
                    {"start": 2.5, "end": 5.25, "text": "第二句 Epsilon。"},
                    {"start": 5.25, "end": 6.0, "text": "  "},
                ],
            },
        )

    key = secrets.token_hex(8)
    asr(handler, asr_api_key=key, asr_model="synthetic-whisper")
    result = asyncio.run(parse_file("meeting.mp3", b"ID3" + b"\x00" * 32))
    assert result.markdown == "合成会议录音第一句。\n第二句 Epsilon。"
    assert result.engine == "asr" and result.metadata == {"asr_model": "synthetic-whisper", "language": "zh"}
    assert [(b.start, b.end, b.locator) for b in result.source_blocks] == [
        (0, 10, {"type": "audio", "start_ms": 0, "end_ms": 2500}),
        (11, 23, {"type": "audio", "start_ms": 2500, "end_ms": 5250}),
    ]
    assert seen["url"] == "https://asr.example.com/v1/audio/transcriptions"
    assert seen["auth"] == f"Bearer {key}"
    assert form_field(seen["form"], "model") == "synthetic-whisper"
    assert form_field(seen["form"], "response_format") == "verbose_json"
    assert b'name="file"; filename="meeting.mp3"' in seen["form"]


def test_plain_transcript_without_segments(asr):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"text": "合成转写文本 Zeta", "language": "zh"})

    asr(handler, asr_api_url="https://asr.example.com/v1/audio/transcriptions/")
    result = asyncio.run(parse_file("note.wav", b"RIFF" + b"\x00" * 32))
    assert result.markdown == "合成转写文本 Zeta" and result.source_blocks == []
    assert seen["url"] == "https://asr.example.com/v1/audio/transcriptions" and seen["auth"] is None


def test_asr_failures_are_readable(asr):
    asr(lambda request: httpx.Response(500, text="synthetic overload"))
    error = parse_error("note.wav", b"RIFF")
    assert error.code == "asr_error" and error.retriable and "synthetic overload" in error.message

    asr(lambda request: httpx.Response(400, json={"error": "unsupported format"}))
    error = parse_error("note.wav", b"RIFF")
    assert error.code == "parse_failed" and not error.retriable

    asr(lambda request: httpx.Response(200, json={"text": "  "}))
    assert parse_error("note.wav", b"RIFF").code == "empty_content"

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    asr(refuse)
    error = parse_error("note.wav", b"RIFF")
    assert error.code == "asr_error" and error.retriable


def test_asr_enabled_without_address_fails_with_a_reason(configure):
    configure(asr_enabled="true", asr_api_url="")
    error = parse_error("note.wav", b"RIFF")
    assert error.code == "asr_error" and "KBS_ASR_API_URL" in error.message
