"""MinerU client: OCR for images and scanned PDFs (KBS_MINERU_*).

Same protocol and defaults as the DA knowledge pipeline platform's client for
mineru-api 3.x: ``POST /tasks`` (multipart form), poll ``GET /tasks/{id}``,
then ``GET /tasks/{id}/result``. A hybrid backend whose task fails is retried
with ``pipeline``. MinerU keeps the page images, so Markdown image links are
reduced to their alt text. mineru-api 4.x (the ``/v1`` API) is not supported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from ..config import Settings, get_settings
from . import ParseError, ParseResult, SourceBlock, service_slot

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 3.0
_SUBMIT_TIMEOUT_S = 300.0
_STATUS_TIMEOUT_S = 10.0
_RESULT_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 15.0
# Tests put an httpx.MockTransport here.
_transport: httpx.AsyncBaseTransport | None = None

_DONE = {"completed", "success", "done"}
_FAILED = {"failed", "error"}
# content_list types that appear in md_content (headers, footers and page numbers do not).
_IN_MARKDOWN = {"text", "table", "equation"}
_IMAGE_LINK = re.compile(r"!\[([^\]]*)\]\((?:\./)?images/[^)\s]*\)")
_ANY_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


async def read_image(file_name: str, data: bytes) -> ParseResult:
    result = await _parse(file_name, data, with_pages=False)
    if not text_chars(result.markdown):
        raise ParseError("empty_content", "MinerU 没有在图片中识别出文字")
    return result


async def rescue_scanned(file_name: str, data: bytes, parsed: ParseResult) -> ParseResult:
    """Re-read a PDF with MinerU when docreader found (almost) no text in it, i.e. a scan."""

    pages = page_count(parsed)
    chars = text_chars(parsed.markdown)
    if chars >= get_settings().mineru_scanned_chars_per_page * pages:
        return parsed
    logger.info("PDF has %d characters on %d pages, reading it with MinerU", chars, pages)
    result = await _parse(file_name, data, with_pages=True)
    if not text_chars(result.markdown):
        logger.info("MinerU found no text either, keeping the docreader result")
        return parsed
    result.title = parsed.title
    result.metadata = {**parsed.metadata, **result.metadata, "ocr_reason": "scanned_pdf"}
    return result


def text_chars(markdown: str) -> int:
    """Non-space characters outside image links."""

    return len(_SPACE.sub("", _ANY_IMAGE.sub("", markdown)))


def page_count(parsed: ParseResult) -> int:
    pages = [b.locator.get("page") for b in parsed.source_blocks]
    pages = [p for p in pages if isinstance(p, int) and p > 0]
    if pages:
        return max(pages)
    try:
        return max(1, int(parsed.metadata.get("page_count", 1)))
    except (TypeError, ValueError):
        return 1


async def _parse(file_name: str, data: bytes, *, with_pages: bool) -> ParseResult:
    settings = get_settings()
    base = settings.mineru_api_url.strip().rstrip("/")
    if not base:
        raise ParseError("mineru_error", "已开启 MinerU（KBS_MINERU_ENABLED），但没有配置地址 KBS_MINERU_API_URL")
    backend = settings.mineru_backend.strip() or "pipeline"
    async with service_slot("mineru", settings.mineru_concurrency, settings.mineru_timeout_s):
        try:
            payload, task_id = await _run(settings, base, file_name, data, backend)
        except ParseError as exc:
            if backend == "pipeline" or exc.code != "parse_failed":
                raise
            logger.warning("MinerU backend %s failed (%s), retrying with pipeline", backend, exc.message)
            backend = "pipeline"
            payload, task_id = await _run(settings, base, file_name, data, backend)
    markdown = _IMAGE_LINK.sub(lambda m: m.group(1).strip(), str(payload.get("md_content") or ""))
    return ParseResult(
        markdown=markdown,
        engine=f"mineru-{backend}",
        source_blocks=_page_blocks(markdown, payload.get("content_list")) if with_pages else [],
        metadata={"ocr_engine": "mineru", "mineru_backend": backend, "mineru_task_id": task_id},
    )


class _Session:
    """HTTP calls of one MinerU task, all bounded by the task's deadline."""

    def __init__(self, client: httpx.AsyncClient, base: str, timeout_s: float) -> None:
        self.client = client
        self.base = base
        self.timeout_s = timeout_s
        self.deadline = time.monotonic() + timeout_s

    def timed_out(self) -> ParseError:
        return ParseError(
            "mineru_error",
            f"MinerU 在 {self.timeout_s:g} 秒内没有完成识别（KBS_MINERU_TIMEOUT_S），文档较大时可调大后重新解析",
        )

    async def call(self, method: str, path: str, cap_s: float, **kwargs) -> httpx.Response:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self.timed_out()
        timeout = httpx.Timeout(min(cap_s, remaining), connect=min(_CONNECT_TIMEOUT_S, remaining))
        try:
            return await self.client.request(method, self.base + path, timeout=timeout, **kwargs)
        except httpx.TimeoutException as exc:
            if time.monotonic() >= self.deadline:
                raise self.timed_out() from exc
            raise ParseError("mineru_error", f"MinerU 响应超时（{type(exc).__name__}），稍后会重试", retriable=True) from exc
        except httpx.HTTPError as exc:
            raise ParseError("mineru_error", f"连不上 MinerU（{type(exc).__name__}），稍后会重试", retriable=True) from exc

    async def pause(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise self.timed_out()
        await asyncio.sleep(min(POLL_INTERVAL_S, remaining))


async def _run(settings: Settings, base: str, file_name: str, data: bytes, backend: str) -> tuple[dict, str]:
    # parse_method=ocr: a scan with a thin text layer (page numbers, a watermark)
    # would otherwise be read as a text PDF and come back just as empty.
    form = {
        "backend": backend,
        "parse_method": "ocr",
        "lang_list": "ch",
        "formula_enable": "true",
        "table_enable": "true",
        "image_analysis": "false",
        "return_md": "true",
        "return_content_list": "true",
        "return_middle_json": "false",
        "return_images": "false",
    }
    async with httpx.AsyncClient(transport=_transport, trust_env=False) as client:
        session = _Session(client, base, settings.mineru_timeout_s)
        resp = await session.call("POST", "/tasks", _SUBMIT_TIMEOUT_S, data=form, files={"files": (file_name, data)})
        if resp.status_code in (404, 405):
            raise ParseError(
                "mineru_error",
                "MinerU 没有 /tasks 接口：kb-service 只支持 mineru-api 3.x 的任务接口，请检查 KBS_MINERU_API_URL 与 MinerU 版本",
            )
        _check(resp, "提交任务")
        body = _json(resp)
        task_id = next((v for v in (body.get("task_id"), body.get("id"), body.get("taskId")) if isinstance(v, str) and v), "")
        if not task_id:
            raise ParseError("mineru_error", "MinerU 提交任务的响应里没有任务 ID（task_id）")
        while True:
            resp = await session.call("GET", f"/tasks/{task_id}", _STATUS_TIMEOUT_S)
            if resp.status_code == 404:
                raise ParseError("mineru_error", "MinerU 里找不到这个任务（MinerU 可能重启过），稍后会重试", retriable=True)
            _check(resp, "查询任务状态")
            status = _json(resp)
            state = str(status.get("status") or "").lower()
            if state in _FAILED:
                reason = status.get("error") or status.get("message") or "没有给出原因"
                raise ParseError("parse_failed", f"MinerU 识别失败：{str(reason)[:300]}")
            if state in _DONE:
                resp = await session.call("GET", f"/tasks/{task_id}/result", _RESULT_TIMEOUT_S)
                if resp.status_code != 202:  # 202: result not written yet
                    _check(resp, "取识别结果")
                    return _file_result(_json(resp)), task_id
            await session.pause()


def _check(resp: httpx.Response, stage: str) -> None:
    if resp.is_success:
        return
    body = _json(resp)
    detail = next((v for v in (body.get("detail"), body.get("error"), body.get("message")) if isinstance(v, str) and v), "")
    detail = (detail or resp.text or "").strip()[:300]
    server_side = resp.status_code >= 500
    raise ParseError(
        "mineru_error" if server_side else "parse_failed",
        f"MinerU {stage}失败（HTTP {resp.status_code}{'：' + detail if detail else ''}）",
        retriable=server_side,
    )


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _file_result(body: dict) -> dict:
    """The entry of the single uploaded file (results are keyed by file name)."""

    results = body.get("results")
    if isinstance(results, dict) and results:
        first = next(iter(results.values()))
        return first if isinstance(first, dict) else {}
    if "md_content" in body or "content" in body:
        return {"md_content": body.get("md_content", body.get("content"))}
    raise ParseError("parse_failed", f"MinerU 结果格式无法识别：{json.dumps(body, ensure_ascii=False)[:300]}")


def _page_blocks(markdown: str, content_list) -> list[SourceBlock]:
    """Page ranges of the Markdown, found by locating each content_list item in it in order."""

    if isinstance(content_list, str):
        try:
            content_list = json.loads(content_list)
        except ValueError:
            return []
    if not isinstance(content_list, list):
        return []
    starts: list[tuple[int, int]] = []
    cursor = 0
    for item in content_list:
        if not isinstance(item, dict) or item.get("type") not in _IN_MARKDOWN or not isinstance(item.get("page_idx"), int):
            continue
        needle = str(item.get("text") or item.get("table_body") or "").strip()[:30]
        found = markdown.find(needle, cursor) if needle else -1
        if found < 0:
            continue
        cursor = found + len(needle)
        if starts and starts[-1][1] == item["page_idx"]:
            continue
        line_start = markdown.rfind("\n", 0, found) + 1
        starts.append((0 if not starts else max(line_start, starts[-1][0]), item["page_idx"]))
    blocks = []
    for index, (start, page) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(markdown)
        if end > start:
            blocks.append(SourceBlock(start, end, {"type": "pdf", "page": page + 1}))
    return blocks
