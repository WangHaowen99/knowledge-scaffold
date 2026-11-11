"""Contract ``content.structure`` and ``grep`` for adapters, on top of each backend's own L1 text.

The L1 window, anchors, outline and line grep come from kb-service's
``kb_service.text_structure`` (standard library only; install kb-service with
``--no-deps``), so every backend answers them the same way.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from kb_service import text_structure

from . import ProviderError

GREP_CONCURRENCY = 8
GREP_MAX_DOCS = 300


async def structure(markdown: str, doc_id: str, *, offset: int, limit: int, heading: str | None) -> dict:
    # parsing a whole document into blocks is CPU work; keep it off the event loop
    win = await asyncio.to_thread(text_structure.window, markdown, doc_id, offset=offset, limit=limit, heading=heading)
    if win is None:
        raise ProviderError(404, "not_found", f"没有找到章节：{heading}")
    return win


async def grep(
    docs: list[dict],
    read_l1: Callable[[dict], Awaitable[str]],
    pattern: str,
    *,
    max_results: int,
    max_lines_per_doc: int,
) -> dict:
    """Grep the L1 text of completed ``docs`` (contract Document views, ids already prefixed)."""

    try:
        regex, mode = text_structure.compile_pattern(pattern)
    except text_structure.PatternError as exc:
        raise ProviderError(400, "bad_pattern", str(exc)) from exc
    docs = [d for d in docs if d.get("parse_status") == "completed"][:GREP_MAX_DOCS]
    gate = asyncio.Semaphore(GREP_CONCURRENCY)

    async def scan(doc: dict) -> tuple[int, dict]:
        async with gate:
            text = await read_l1(doc)
        total, lines = await asyncio.to_thread(text_structure.grep_lines, text, regex, max_lines_per_doc)
        return total, {"doc_id": doc["id"], "kb_id": doc["kb_id"], "title": doc.get("title"),
                       "file_name": doc.get("file_name"), "lines": lines}

    found = [(n, m) for n, m in await asyncio.gather(*(scan(d) for d in docs)) if n]
    return {
        "pattern": pattern, "search_mode": mode, "total_matches": sum(n for n, _ in found),
        "total_documents": len(found), "has_more": len(found) > max_results,
        "matches": [m for _, m in found][:max_results],
    }
