"""Reading helpers over a document's L1 markdown: windows with anchors, sections, outline, line grep.

Only the standard library and ``chunking`` are used, so the knowledge-backend
adapters (WeKnora, DA pipeline) install kb-service without dependencies and
reuse these for the provider contract's ``content.structure`` and ``grep``.
"""

from __future__ import annotations

import re

from .chunking import parse_blocks

MAX_PATTERN = 500
OUTLINE_LIMIT = 40


def outline(markdown: str) -> list[str]:
    heads = []
    for block in parse_blocks(markdown):
        if block.kind == "heading":
            heads.append("  " * (block.level - 1) + markdown[block.start : block.end].lstrip("#").strip())
            if len(heads) >= OUTLINE_LIMIT:
                break
    return heads


def section(markdown: str, heading: str) -> str | None:
    blocks = parse_blocks(markdown)
    target = heading.strip().lstrip("#").strip()
    for i, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        title = markdown[block.start : block.end].lstrip("#").strip()
        if title == target or target in title:
            end = len(markdown)
            for later in blocks[i + 1 :]:
                if later.kind == "heading" and later.level <= block.level:
                    end = later.start
                    break
            return markdown[block.start : end]
    return None


def anchors(markdown: str, doc_id: str, start: int, end: int) -> list[dict]:
    """Anchors ``{docId}:{type}:{index}`` of the blocks overlapping [start, end)."""

    out = []
    for block in parse_blocks(markdown, doc_id):
        if block.end <= start or block.start >= end:
            continue
        out.append(
            {
                "id": block.anchor_id,
                "type": block.anchor_id.split(":")[-2] if block.anchor_id else block.kind,
                "line_start": markdown.count("\n", 0, block.start) + 1,
                "preview": markdown[block.start : block.end][:80],
                "section_title": block.header_path[-1] if block.header_path else None,
            }
        )
    return out


def window(markdown: str, doc_id: str, *, offset: int = 0, limit: int = 30000, heading: str | None = None) -> dict | None:
    """A readable piece of the document: one section (``heading``) or ``limit`` characters from ``offset``.

    A window ends on a line break when one is in its second half. ``limit=0``
    returns only the outline and the size. ``None`` when ``heading`` is not found.
    """

    total = len(markdown)
    base = {"total_chars": total, "outline": outline(markdown)}
    if heading:
        found = section(markdown, heading)
        if found is None:
            return None
        start = markdown.find(found)
        return {**base, "content": found, "offset": start, "has_more": False,
                "anchors": anchors(markdown, doc_id, start, start + len(found))}
    offset = max(0, offset)
    if limit <= 0:
        return {**base, "content": "", "offset": offset, "has_more": offset < total, "anchors": []}
    end = min(total, offset + limit)
    if end < total:
        cut = markdown.rfind("\n", offset, end)
        if cut > offset + limit // 2:
            end = cut + 1
    out = {**base, "content": markdown[offset:end], "offset": offset, "has_more": end < total,
           "anchors": anchors(markdown, doc_id, offset, end)}
    if end < total:
        out["next_offset"] = end
    return out


class PatternError(ValueError):
    pass


def compile_pattern(pattern: str) -> tuple[re.Pattern, str]:
    """Case-insensitive regex, or the literal text when the pattern is not a valid regex."""

    if not pattern:
        raise PatternError("pattern 不能为空")
    if len(pattern) > MAX_PATTERN:
        raise PatternError(f"pattern 超过 {MAX_PATTERN} 字")
    try:
        return re.compile(pattern, re.IGNORECASE), "regex"
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE), "literal"


def grep_lines(text: str, regex: re.Pattern, max_lines: int) -> tuple[int, list[dict]]:
    """(number of matching lines, the first ``max_lines`` of them as {line_number, text})."""

    total, lines = 0, []
    for number, line in enumerate(text.split("\n"), start=1):
        if regex.search(line):
            total += 1
            if len(lines) < max_lines:
                lines.append({"line_number": number, "text": line[:500]})
    return total, lines
