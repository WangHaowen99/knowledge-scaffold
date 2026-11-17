"""Chunk totals of WeKnora document knowledge bases, refreshed in the background.

WeKnora fills a knowledge base's ``chunk_count`` for FAQ bases only
(knowledgebase.go, FillKnowledgeBaseCounts). For document bases the total is the
sum of every document's chunk list, one request per document, so knowledge-base
views never compute it: they read the last total and a background task brings it
up to date. Listing knowledge bases thus costs the same however many documents
they hold.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable

from kb_provider_kit import ProviderError

log = logging.getLogger(__name__)

# parse statuses without chunks yet, and those whose chunk count can still change
NO_CHUNKS_YET = frozenset({"pending", "draft"})
STILL_CHUNKING = NO_CHUNKS_YET | {"processing"}

# raw kb id -> the knowledge base's documents (WeKnora knowledge objects)
ListDocs = Callable[[str], Awaitable[list[dict]]]
# raw doc id -> the total of the document's chunk list
CountChunks = Callable[[str], Awaitable[int]]


@dataclass
class _Total:
    # (knowledge_count, processing_count) the total was computed for
    signature: tuple[int, int]
    at: float
    value: int | None


class ChunkStats:
    def __init__(
        self, list_docs: ListDocs, count_chunks: CountChunks, *, ttl_s: float, concurrency: int = 8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._list_docs = list_docs
        self._count_chunks = count_chunks
        self._ttl_s = ttl_s
        self._clock = clock
        self._gate = asyncio.Semaphore(concurrency)
        self._totals: dict[str, _Total] = {}
        # kb id -> {doc id -> ((updated_at, parse_status), chunks)} for documents whose count is settled
        self._settled: dict[str, dict[str, tuple[tuple[str, str], int]]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def peek(self, kb: dict) -> int | None:
        """The last chunk total of ``kb`` (a WeKnora knowledge base); None until one is computed.

        Never waits: a total that is missing, older than the TTL or computed for other
        document counts gets a background refresh, at most one per knowledge base at a time.
        """

        if kb.get("chunk_count"):
            return int(kb["chunk_count"])
        kb_id = str(kb["id"])
        if not kb.get("knowledge_count"):
            self._totals.pop(kb_id, None)
            self._settled.pop(kb_id, None)
            return 0
        signature = (int(kb.get("knowledge_count") or 0), int(kb.get("processing_count") or 0))
        known = self._totals.get(kb_id)
        stale = known is None or known.signature != signature or self._clock() - known.at >= self._ttl_s
        if stale and kb_id not in self._tasks:
            task = asyncio.create_task(self._refresh(kb_id, signature))
            self._tasks[kb_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(kb_id, None))
        return known.value if known else None

    def doc_count(self, doc: dict) -> int | None:
        """The chunk count of a WeKnora document as counted by the last refresh; None if not counted or changed since."""

        kb_id = str(doc.get("knowledge_base_id") or "")
        cached = self._settled.get(kb_id, {}).get(str(doc.get("id")))
        version = (str(doc.get("updated_at") or ""), str(doc.get("parse_status") or ""))
        return cached[1] if cached and cached[0] == version else None

    async def settle(self, kb_ids: Iterable[str], timeout_s: float) -> None:
        """Wait at most ``timeout_s`` for the running refreshes of ``kb_ids``; they go on afterwards."""

        running = {self._tasks[k] for k in kb_ids if k in self._tasks}
        if running and timeout_s > 0:
            await asyncio.wait(running, timeout=timeout_s)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh(self, kb_id: str, signature: tuple[int, int]) -> None:
        known = self._totals.get(kb_id)
        try:
            value: int | None = await self._compute(kb_id)
        except Exception:  # noqa: BLE001 - a failed refresh keeps the last total until the next TTL
            log.warning("chunk total of knowledge base %s not refreshed", kb_id, exc_info=True)
            value = known.value if known else None
        self._totals[kb_id] = _Total(signature, self._clock(), value)

    async def _compute(self, kb_id: str) -> int:
        known, settled = self._settled.get(kb_id, {}), {}
        docs = await self._list_docs(kb_id)
        counts = await asyncio.gather(*(self._count(doc, known, settled) for doc in docs))
        self._settled[kb_id] = settled
        return sum(counts)

    async def _count(self, doc: dict, known: dict, settled: dict) -> int:
        status = str(doc.get("parse_status") or "")
        if status in NO_CHUNKS_YET:
            return 0
        version = (str(doc.get("updated_at") or ""), status)
        cached = known.get(doc["id"])
        if cached and cached[0] == version:
            settled[doc["id"]] = cached
            return cached[1]
        try:
            async with self._gate:
                count = await self._count_chunks(doc["id"])
        except ProviderError:
            # deleted in the meantime, or WeKnora busy: counted on the next refresh
            return 0
        if status not in STILL_CHUNKING:
            settled[doc["id"]] = (version, count)
        return count
