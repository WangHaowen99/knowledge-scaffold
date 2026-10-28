"""Document parsing: route a source to docreader or a native reader.

All readers return Markdown (the L1 text) plus source blocks that tie
code-point ranges of that Markdown back to the original file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

SUPPORTED_TYPES = TEXT_TYPES | TABLE_TYPES | NATIVE_SHEET_TYPES | JSON_TYPES | DOCREADER_TYPES


def file_type_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def normalize_newlines(markdown: str, blocks: list[SourceBlock]) -> tuple[str, list[SourceBlock]]:
    """Drop ``\\r`` and shift block offsets so they still point at the same text."""

    if "\r" not in markdown:
        return markdown, blocks
    removed_before: list[int] = []
    count = 0
    out = []
    for ch in markdown:
        removed_before.append(count)
        if ch == "\r":
            count += 1
            continue
        out.append(ch)
    removed_before.append(count)

    def shift(pos: int) -> int:
        pos = max(0, min(pos, len(markdown)))
        return pos - removed_before[pos]

    shifted = [SourceBlock(shift(b.start), shift(b.end), b.locator) for b in blocks]
    return "".join(out), [b for b in shifted if b.end > b.start]


async def parse_file(file_name: str, data: bytes, engine_rules: dict[str, str] | None = None) -> ParseResult:
    """Route a file to a parser. ``engine_rules`` maps an extension to a
    docreader engine (builtin, markitdown, ...); ``auto`` or no rule keeps the
    default routing below."""

    from . import native
    from .docreader_client import read_file

    file_type = file_type_of(file_name)
    engine = (engine_rules or {}).get(file_type) or "auto"
    if engine != "auto" and file_type not in IMAGE_TYPES:
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
    markdown, blocks = normalize_newlines(result.markdown, result.source_blocks)
    result.markdown, result.source_blocks = markdown, blocks
    if not result.markdown.strip():
        raise ParseError("empty_content", "解析结果为空")
    return result


async def parse_url(url: str) -> ParseResult:
    from .docreader_client import read_url

    result = await read_url(url)
    result.markdown, result.source_blocks = normalize_newlines(result.markdown, result.source_blocks)
    if not result.markdown.strip():
        raise ParseError("empty_content", "网页解析结果为空")
    return result
