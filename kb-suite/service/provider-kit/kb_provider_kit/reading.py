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
    loop = asyncio.get_running_loop()
    stopped = False

    async def scan(doc: dict) -> tuple[int, dict]:
        nonlocal stopped
        # at most GREP_CONCURRENCY documents are read and scanned at once, on grep's own threads
        async with gate:
            if stopped:
                return 0, {}
            text = await read_l1(doc)
            if stopped:
                return 0, {}
            try:
                total, lines = await loop.run_in_executor(
                    text_structure.grep_executor(), text_structure.grep_lines, text, regex, max_lines_per_doc
                )
            except text_structure.PatternError:
                stopped = True  # before the gate lets the next document in
                raise
        return total, {"doc_id": doc["id"], "kb_id": doc["kb_id"], "title": doc.get("title"),
                       "file_name": doc.get("file_name"), "lines": lines}

    tasks = [asyncio.create_task(scan(d)) for d in docs]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException as exc:  # e.g. PatternTimeout: the first failure ends the whole grep
        stopped = True
        for task in tasks:
            task.cancel()
        if isinstance(exc, text_structure.PatternError):
            raise ProviderError(400, exc.code, str(exc)) from exc
        raise
    found = [(n, m) for n, m in results if n]
    return {
        "pattern": pattern, "search_mode": mode, "total_matches": sum(n for n, _ in found),
        "total_documents": len(found), "has_more": len(found) > max_results,
        "matches": [m for _, m in found][:max_results],
    }
