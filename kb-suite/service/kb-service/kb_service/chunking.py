"""Markdown chunker.

Ported in spirit from WeKnora's Go chunker (internal/infrastructure/chunker):
heading-aware packing, protected spans for fenced code and tables, a
recursive separator fallback for oversized text, and a context header made of
the heading path. Chunks are code-point ranges of the L1 Markdown, so
``content == markdown[start:end]`` always holds and source locators can be
mapped by range overlap.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
HTML_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)
HTML_TABLE_CLOSE = re.compile(r"</table\s*>", re.IGNORECASE)

TEXT_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " "]
ANCHOR_KIND = {
    "heading": "heading",
    "paragraph": "paragraph",
    "list": "list",
    "code": "code",
    "table": "table",
    "image": "image",
}


@dataclass
class Block:
    start: int
    end: int
    kind: str
    header_path: tuple[str, ...]
    level: int = 0
    anchor_id: str = ""


@dataclass
class Chunk:
    seq: int
    start: int
    end: int
    content: str
    chunk_type: str
    context_header: str
    line_start: int
    anchor_id: str
    kinds: set[str] = field(default_factory=set)


class _Lines:
    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0]
        for match in re.finditer("\n", text):
            self.starts.append(match.end())

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def __len__(self) -> int:
        return len(self.starts)

    def span(self, index: int) -> tuple[int, int]:
        start = self.starts[index]
        end = self.starts[index + 1] - 1 if index + 1 < len(self.starts) else len(self.text)
        return start, end


def parse_blocks(markdown: str, doc_id: str = "") -> list[Block]:
    lines = _Lines(markdown)
    blocks: list[Block] = []
    stack: list[tuple[int, str]] = []
    counters: dict[str, int] = {}

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in stack)

    def emit(start: int, end: int, kind: str, level: int = 0) -> None:
        if end <= start or not markdown[start:end].strip():
            return
        anchor_kind = ANCHOR_KIND[kind]
        index = counters.get(anchor_kind, 0)
        counters[anchor_kind] = index + 1
        anchor = f"{doc_id}:{anchor_kind}:{index}" if doc_id else ""
        blocks.append(Block(start, end, kind, path(), level, anchor))

    i = 0
    para_start: int | None = None
    para_kind = "paragraph"
    para_end = 0

    def flush_para() -> None:
        nonlocal para_start
        if para_start is not None:
            emit(para_start, para_end, para_kind)
            para_start = None

    total = len(lines)
    while i < total:
        start, end = lines.span(i)
        line = markdown[start:end]
        stripped = line.strip()

        fence = FENCE_RE.match(line)
        if fence:
            flush_para()
            marker = fence.group(1)[0] * 3
            j = i + 1
            block_end = end
            while j < total:
                s, e = lines.span(j)
                block_end = e
                if markdown[s:e].strip().startswith(marker):
                    break
                j += 1
            emit(start, block_end, "code")
            i = j + 1
            continue

        if HTML_TABLE_OPEN.search(line):
            flush_para()
            j = i
            block_end = end
            while j < total:
                s, e = lines.span(j)
                block_end = e
                if HTML_TABLE_CLOSE.search(markdown[s:e]):
                    break
                j += 1
            emit(start, block_end, "table")
            i = j + 1
            continue

        if stripped.startswith("|"):
            flush_para()
            j = i
            block_end = end
            while j < total:
                s, e = lines.span(j)
                if not markdown[s:e].strip().startswith("|"):
                    break
                block_end = e
                j += 1
            emit(start, block_end, "table")
            i = j
            continue

        heading = HEADING_RE.match(line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            emit(start, end, "heading", level)
            i += 1
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        if IMAGE_LINE_RE.match(line):
            flush_para()
            emit(start, end, "image")
            i += 1
            continue

        is_list = bool(LIST_RE.match(line))
        if para_start is None:
            para_start = start
            para_kind = "list" if is_list else "paragraph"
        elif is_list and para_kind != "list":
            flush_para()
            para_start = start
            para_kind = "list"
        para_end = end
        i += 1
    flush_para()
    return blocks


def _split_ranges(text: str, start: int, end: int, size: int, seps: list[str]) -> list[tuple[int, int]]:
    if end - start <= size:
        return [(start, end)]
    if not seps:
        return [(pos, min(pos + size, end)) for pos in range(start, end, size)]
    sep = seps[0]
    parts: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        idx = text.find(sep, pos, end)
        if idx == -1:
            parts.append((pos, end))
            break
        cut = idx + len(sep)
        parts.append((pos, cut))
        pos = cut
    out: list[tuple[int, int]] = []
    for s, e in parts:
        if e - s > size:
            out.extend(_split_ranges(text, s, e, size, seps[1:]))
        else:
            out.append((s, e))
    return out


def split_text(
    text: str, start: int, end: int, size: int, overlap: int, separators: list[str] | None = None
) -> list[tuple[int, int]]:
    """Greedy-merge separator segments into pieces <= size, with overlap."""

    segments = _split_ranges(text, start, end, size, separators or TEXT_SEPARATORS)
    pieces: list[tuple[int, int]] = []
    current: list[tuple[int, int]] = []
    for seg in segments:
        length = (current[-1][1] - current[0][0]) if current else 0
        if current and length + (seg[1] - seg[0]) > size:
            pieces.append((current[0][0], current[-1][1]))
            carry: list[tuple[int, int]] = []
            carried = 0
            for prev in reversed(current):
                span = prev[1] - prev[0]
                if carried + span > overlap:
                    break
                carry.insert(0, prev)
                carried += span
            if carried + (seg[1] - seg[0]) > size:
                carry = []
            current = carry
        current.append(seg)
    if current:
        pieces.append((current[0][0], current[-1][1]))
    return pieces


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _overlap_start(text: str, block: Block, prev_end: int, overlap: int) -> int | None:
    if overlap <= 0 or block.kind not in {"paragraph", "list"}:
        return None
    window_start = max(block.start, prev_end - overlap)
    window = text[window_start:prev_end]
    for i, ch in enumerate(window):
        if ch in "。！？；.!?;\n" and i + 1 < len(window):
            return window_start + i + 1
    return None


def chunk_markdown(
    markdown: str,
    *,
    doc_id: str = "",
    chunk_size: int = 512,
    overlap: int = 80,
    separators: list[str] | None = None,
    strategy: str = "heading",
) -> tuple[list[Chunk], list[Block]]:
    """Split L1 Markdown into chunks.

    ``heading`` (default) packs blocks within a section and never crosses a
    heading; ``separator`` ignores structure and splits the whole text by the
    separator priority list (WeKnora's heuristic tier), keeping the heading
    path of the block each chunk starts in as its context header.
    """

    blocks = parse_blocks(markdown, doc_id)
    lines = _Lines(markdown)
    chunks: list[Chunk] = []
    hard_limit = chunk_size * 3
    seps = separators or TEXT_SEPARATORS

    if strategy == "separator":
        starts = [b.start for b in blocks]
        for s, e in split_text(markdown, 0, len(markdown), chunk_size, overlap, seps):
            s, e = _trim(markdown, s, e)
            if e <= s:
                continue
            idx = max(0, bisect.bisect_right(starts, s) - 1)
            block = blocks[idx] if blocks else None
            chunks.append(
                Chunk(
                    seq=len(chunks),
                    start=s,
                    end=e,
                    content=markdown[s:e],
                    chunk_type="text",
                    context_header=" > ".join(block.header_path) if block else "",
                    line_start=lines.line_of(s),
                    anchor_id=block.anchor_id if block else "",
                    kinds={block.kind} if block else {"paragraph"},
                )
            )
        return chunks, blocks

    def add(start: int, end: int, members: list[Block]) -> None:
        start, end = _trim(markdown, start, end)
        if end <= start:
            return
        kinds = {b.kind for b in members}
        if kinds <= {"table"}:
            chunk_type = "table"
        elif kinds <= {"code"}:
            chunk_type = "code"
        elif kinds <= {"image"}:
            chunk_type = "image"
        else:
            chunk_type = "text"
        first = members[0]
        anchor = first.anchor_id
        for b in members:
            if b.start <= start < b.end:
                anchor = b.anchor_id
                break
        chunks.append(
            Chunk(
                seq=len(chunks),
                start=start,
                end=end,
                content=markdown[start:end],
                chunk_type=chunk_type,
                context_header=" > ".join(first.header_path),
                line_start=lines.line_of(start),
                anchor_id=anchor,
                kinds=kinds,
            )
        )

    current: list[Block] = []
    current_start = 0

    def flush() -> None:
        nonlocal current
        if current:
            add(current_start, current[-1].end, current)
        current = []

    for block in blocks:
        length = block.end - block.start
        if block.kind == "heading" and current:
            flush()
        if length > chunk_size and block.kind != "heading":
            if block.kind in {"table", "code"} and length <= hard_limit:
                flush()
                add(block.start, block.end, [block])
                continue
            flush()
            if block.kind in {"table", "code"}:
                pieces = _split_ranges(markdown, block.start, block.end, hard_limit, ["\n"])
            else:
                pieces = split_text(markdown, block.start, block.end, chunk_size, overlap, seps)
            for s, e in pieces:
                add(s, e, [block])
            continue
        if current and block.end - current_start > chunk_size:
            prev = current[-1]
            prev_end = prev.end
            flush()
            same_section = prev.header_path == block.header_path
            overlap_from = _overlap_start(markdown, prev, prev_end, overlap) if same_section else None
            if overlap_from is not None and block.end - overlap_from <= chunk_size:
                current = [prev, block]
                current_start = overlap_from
                continue
        if not current:
            current_start = block.start
        current.append(block)
    flush()
    return chunks, blocks
