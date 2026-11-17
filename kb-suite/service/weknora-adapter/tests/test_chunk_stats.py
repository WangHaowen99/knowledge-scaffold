"""weknora_adapter.chunk_stats: chunk totals refreshed in the background (synthetic documents)."""

from __future__ import annotations

import asyncio

import pytest

from kb_provider_kit import ProviderError
from weknora_adapter.chunk_stats import ChunkStats

TTL = 10.0


class Backend:
    """Documents and chunk-list totals of one knowledge base, with a call log."""

    def __init__(self) -> None:
        self.docs = [
            {"id": "d1", "parse_status": "completed", "updated_at": "t1"},
            {"id": "d2", "parse_status": "processing", "updated_at": "t1"},
            {"id": "d3", "parse_status": "pending", "updated_at": "t1"},
        ]
        self.totals = {"d1": 5, "d2": 2, "d3": 7}
        self.listed = 0
        self.asked: list[str] = []
        self.fail_listing = False
        self.release = asyncio.Event()
        self.release.set()

    async def list_docs(self, kb_id: str) -> list[dict]:
        self.listed += 1
        await self.release.wait()
        if self.fail_listing:
            raise ProviderError(502, "backend_unavailable", "WeKnora 不可用")
        return [dict(d) for d in self.docs]

    async def count(self, doc_id: str) -> int:
        self.asked.append(doc_id)
        if doc_id not in self.totals:
            raise ProviderError(404, "not_found", "文档不存在")
        return self.totals[doc_id]


@pytest.fixture
def backend():
    return Backend()


@pytest.fixture
def clock():
    return [0.0]


@pytest.fixture
def stats(backend, clock):
    return ChunkStats(backend.list_docs, backend.count, ttl_s=TTL, clock=lambda: clock[0])


KB = {"id": "kb1", "knowledge_count": 3, "processing_count": 2, "chunk_count": 0}


async def _settled(stats: ChunkStats, kb: dict | None = None) -> int | None:
    kb = kb or KB
    stats.peek(kb)
    await stats.settle([kb["id"]], 5)
    return stats.peek(kb)


async def test_the_first_view_does_not_wait_and_one_refresh_runs(stats, backend):
    backend.release.clear()
    assert stats.peek(KB) is None and stats.peek(KB) is None
    await asyncio.sleep(0)
    assert backend.listed == 1
    backend.release.set()
    await stats.settle(["kb1"], 5)
    # a pending document has no chunks to ask about
    assert stats.peek(KB) == 5 + 2 and sorted(backend.asked) == ["d1", "d2"]


async def test_settle_returns_after_its_timeout(stats, backend):
    backend.release.clear()
    stats.peek(KB)
    await stats.settle(["kb1"], 0.01)
    assert stats.peek(KB) is None
    backend.release.set()
    await stats.settle(["kb1"], 5)
    assert stats.peek(KB) == 7


async def test_totals_are_kept_for_the_ttl_and_only_unsettled_documents_are_asked_again(stats, backend, clock):
    assert await _settled(stats) == 7
    clock[0] = TTL - 1
    assert await _settled(stats) == 7 and backend.listed == 1
    clock[0] = TTL + 1
    backend.totals["d2"] = 3
    assert await _settled(stats) == 5 + 3 and backend.listed == 2
    # the completed document was counted once, the processing one on every refresh
    assert backend.asked.count("d1") == 1 and backend.asked.count("d2") == 2
    # a reparsed document (new updated_at) is counted again
    backend.docs[0]["updated_at"] = "t2"
    backend.totals["d1"] = 6
    clock[0] = 2 * TTL + 2
    assert await _settled(stats) == 6 + 3 and backend.asked.count("d1") == 2


async def test_a_change_in_document_counts_refreshes_before_the_ttl(stats, backend):
    assert await _settled(stats) == 7
    backend.docs[1]["parse_status"] = "completed"
    assert await _settled(stats, {**KB, "processing_count": 1}) == 7 and backend.listed == 2


async def test_a_failed_refresh_keeps_the_last_total(stats, backend, clock):
    assert await _settled(stats) == 7
    backend.fail_listing = True
    clock[0] = TTL + 1
    assert await _settled(stats) == 7
    # and is not retried before the next TTL
    assert await _settled(stats) == 7 and backend.listed == 2


async def test_a_document_gone_in_the_meantime_counts_nothing(stats, backend):
    backend.docs.append({"id": "gone", "parse_status": "completed", "updated_at": "t1"})
    assert await _settled(stats) == 7


async def test_faq_and_empty_knowledge_bases_need_no_refresh(stats, backend):
    assert stats.peek({**KB, "chunk_count": 12}) == 12
    assert stats.peek({**KB, "knowledge_count": 0}) == 0
    await asyncio.sleep(0)
    assert backend.listed == 0


async def test_closing_cancels_running_refreshes(stats, backend):
    backend.release.clear()
    stats.peek(KB)
    await asyncio.sleep(0)
    await stats.aclose()
    backend.release.set()
    await asyncio.sleep(0)
    assert backend.listed == 1 and backend.asked == []
