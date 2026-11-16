"""Document parsing: route a source to docreader or a native reader.

All readers return Markdown (the L1 text) plus source blocks that tie
code-point ranges of that Markdown back to the original file. When
configured, MinerU reads images and scanned PDFs and an ASR service
transcribes audio.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..config import Settings, get_settings


class ParseError(Exception):
    def __init__(self, code: str, message: str, *, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retriable = retriable


@dataclass
class SourceBlock:
    start: int
    end: int
    locator: dict


@dataclass
class Asset:
    name: str
    data: bytes
    mime_type: str = ""


@dataclass
class ParseResult:
    markdown: str
    engine: str
    source_blocks: list[SourceBlock] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    title: str = ""


TEXT_TYPES = {"md", "markdown", "txt", "text", "log"}
TABLE_TYPES = {"csv", "tsv"}
NATIVE_SHEET_TYPES = {"xlsx"}
JSON_TYPES = {"json"}
DOCREADER_TYPES = {
    "pdf", "docx", "doc", "pptx", "ppt", "xls", "html", "htm", "epub", "mhtml", "xmind",
}
IMAGE_TYPES = {"jpg", "jpeg", "png", "gif", "bmp", "tiff", "webp"}
# Only read when the matching service is configured (KBS_MINERU_*, KBS_ASR_*).
MINERU_IMAGE_TYPES = IMAGE_TYPES | {"tif"}
MINERU_DOC_TYPES = {"pdf"}
AUDIO_TYPES = {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma"}


def mineru_types(settings: Settings) -> set[str]:
    """Extensions handed to MinerU: images directly, PDFs only when docreader finds no text."""

    if not settings.mineru_enabled:
        return set()
    wanted = {name.strip().lstrip(".").lower() for name in settings.mineru_file_types.split(",")}
    return wanted & (MINERU_IMAGE_TYPES | MINERU_DOC_TYPES)


def supported_types(settings: Settings) -> set[str]:
    types = TEXT_TYPES | TABLE_TYPES | NATIVE_SHEET_TYPES | JSON_TYPES | DOCREADER_TYPES
    types |= mineru_types(settings) & MINERU_IMAGE_TYPES
    if settings.asr_enabled:
        types |= AUDIO_TYPES
    return types


# Settings are read from the environment once per process.
SUPPORTED_TYPES = supported_types(get_settings())

_slots: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


@asynccontextmanager
async def service_slot(name: str, limit: int, wait_s: float):
    """Hold one of ``limit`` per-process slots for an external service.

    A job that gets no slot within ``wait_s`` goes back to the queue without
    using an attempt (jobs.Defer) instead of waiting past its lease.
    """

    loop = asyncio.get_running_loop()
    held = _slots.get(name)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Semaphore(max(1, limit)))
        _slots[name] = held
    gate = held[1]
    try:
        async with asyncio.timeout(wait_s):
            await gate.acquire()
    except TimeoutError:
        from .. import jobs

        raise jobs.Defer(30) from None
    try:
        yield
    finally:
        gate.release()


def file_type_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def normalize_newlines(markdown: str, blocks: list[SourceBlock]) -> tuple[str, list[SourceBlock]]:
    """Drop ``\\r`` and NUL (PostgreSQL text cannot hold it) and shift block offsets so they still point at the same text."""

    if "\r" not in markdown and "\x00" not in markdown:
        return markdown, blocks
    removed_before: list[int] = []
    count = 0
    out = []
    for ch in markdown:
        removed_before.append(count)
        if ch in "\r\x00":
            count += 1
            continue
        out.append(ch)
    removed_before.append(count)

    def shift(pos: int) -> int:
        pos = max(0, min(pos, len(markdown)))
        return pos - removed_before[pos]

    shifted = [SourceBlock(shift(b.start), shift(b.end), b.locator) for b in blocks]
    return "".join(out), [b for b in shifted if b.end > b.start]


# ZIP-based formats and the folder their main part lives in.
_ZIP_FORMATS = {
    "docx": ("word/", "Word 文档"),
    "pptx": ("ppt/", "PowerPoint 演示文稿"),
    "xlsx": ("xl/", "Excel 工作簿"),
    "epub": ("", "EPUB 电子书"),
    "xmind": ("", "XMind 文件"),
}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def check_container(file_type: str, data: bytes) -> None:
    """Fail early when a PDF or ZIP-based document is not one at all.

    docreader returns the raw bytes of a damaged .docx as text instead of an
    error, which showed garbage as a completed document. An old binary Office
    file saved under the new extension is left to docreader, which converts it.
    """

    if file_type == "pdf":
        if b"%PDF-" not in data[:1024]:
            raise ParseError("parse_failed", "文件不是有效的 PDF（没有 PDF 文件头），可能已损坏或只是改了扩展名")
        return
    spec = _ZIP_FORMATS.get(file_type)
    if spec is None or data.startswith(_OLE_MAGIC):
        return
    folder, label = spec
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError) as exc:
        raise ParseError("parse_failed", f"文件已损坏或不是有效的{label}（.{file_type}），无法解析") from exc
    if folder and not any(name.startswith(folder) for name in names):
        raise ParseError("parse_failed", f"文件不是有效的{label}（.{file_type} 里缺少正文部分），无法解析")


async def parse_file(file_name: str, data: bytes, engine_rules: dict[str, str] | None = None) -> ParseResult:
    """Route a file to a parser. ``engine_rules`` maps an extension to a
    docreader engine (builtin, markitdown, ...); ``auto`` or no rule keeps the
    default routing below."""

    from . import native
    from .docreader_client import read_file

    settings = get_settings()
    file_type = file_type_of(file_name)
    check_container(file_type, data)
    engine = (engine_rules or {}).get(file_type) or "auto"
    ocr_types = mineru_types(settings)
    if file_type in ocr_types & MINERU_IMAGE_TYPES:
        from .mineru_client import read_image

        result = await read_image(file_name, data)
    elif settings.asr_enabled and file_type in AUDIO_TYPES:
        from .asr_client import transcribe

        result = await transcribe(file_name, data)
    elif engine != "auto" and file_type not in IMAGE_TYPES:
        result = await read_file(file_name, file_type, data, engine=engine)
    elif file_type in TEXT_TYPES:
        result = native.read_text(data)
    elif file_type in TABLE_TYPES:
        result = native.read_delimited(data, delimiter="\t" if file_type == "tsv" else ",")
    elif file_type in NATIVE_SHEET_TYPES:
        result = native.read_xlsx(data)
    elif file_type in JSON_TYPES:
        result = native.read_json(data)
    elif file_type in DOCREADER_TYPES:
        result = await read_file(file_name, file_type, data)
    elif file_type in IMAGE_TYPES:
        raise ParseError(
            "unsupported_without_ocr",
            "图片需要 OCR 或 VLM 才能入库，当前版本未接入",
        )
    else:
        raise ParseError("unsupported_type", f"不支持的文件类型: .{file_type or '?'}")
    if file_type in ocr_types & MINERU_DOC_TYPES:
        from .mineru_client import rescue_scanned

        result = await rescue_scanned(file_name, data, result)
    markdown, blocks = normalize_newlines(result.markdown, result.source_blocks)
    result.markdown, result.source_blocks = markdown, blocks
    if not result.markdown.strip():
        raise ParseError("empty_content", "解析结果为空")
    return result


async def parse_url(url: str) -> ParseResult:
    from .. import url_guard
    from .docreader_client import read_url

    # Checked again at fetch time: the name may resolve differently than at import.
    try:
        await url_guard.check_public_url(url)
    except url_guard.UrlRejected as exc:
        raise ParseError(exc.code, exc.message) from exc
    try:
        result = await read_url(url)
    except ParseError as exc:
        raise ParseError(exc.code, url_guard.safe_fetch_error(exc.message, url), retriable=exc.retriable) from exc
    result.markdown, result.source_blocks = normalize_newlines(result.markdown, result.source_blocks)
    if not result.markdown.strip():
        raise ParseError("empty_content", "网页解析结果为空")
    return result
