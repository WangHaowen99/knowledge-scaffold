"""provider-kit's ``reading.grep`` over many documents: one slow pattern ends the grep quickly (synthetic text only).

provider-kit has no test run of its own; its pieces are tested from the adapters.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from kb_service import text_structure

from kb_provider_kit import ProviderError, reading

SLOW_TEXT = "7" * 400  # with \d+\d+\d+\d+x$ every scan of it runs past the deadline


def _docs(count: int) -> list[dict]:
    return [{"id": f"doc_{i}", "kb_id": "kb_demo", "title": f"合成文档 {i}", "parse_status": "completed"}
            for i in range(count)]


@pytest.fixture
def fresh_pool(monkeypatch):
    """A worker pool of its own, so a pattern paused by another test does not leak in."""

    pool = text_structure._WorkerPool(text_structure.GREP_WORKERS)
    monkeypatch.setattr(text_structure, "_pool_obj", pool)
    yield pool
    pool.close()


def test_a_timeout_is_a_readable_400(monkeypatch):
    def timed_out(text, regex, max_lines):
        raise text_structure.PatternTimeout("正则执行超时（单篇文档超过 3 秒）")

    async def read(doc):
        return "aaa"

    monkeypatch.setattr(text_structure, "grep_lines", timed_out)
    with pytest.raises(ProviderError) as refused:
        asyncio.run(reading.grep(_docs(1), read, "a+b", max_results=5, max_lines_per_doc=5))
    assert refused.value.status == 400 and refused.value.code == "pattern_timeout"


def test_the_first_timeout_ends_the_grep_over_many_documents(monkeypatch, fresh_pool):
    monkeypatch.setattr(text_structure, "GREP_TIMEOUT_S", 0.5)
    scanned: list[str] = []
    real_grep_lines = text_structure.grep_lines

    def counted(text, regex, max_lines):
        try:
            return real_grep_lines(text, regex, max_lines)
        except text_structure.PatternTimeout as exc:
            scanned.append("deadline" if "单篇文档超过" in str(exc) else "paused")
            raise

    async def read(doc):
        return SLOW_TEXT

    monkeypatch.setattr(text_structure, "grep_lines", counted)
    docs = _docs(40)
    started = time.perf_counter()
    with pytest.raises(ProviderError) as refused:
        asyncio.run(reading.grep(docs, read, r"\d+\d+\d+\d+x$", max_results=50, max_lines_per_doc=5))
    elapsed = time.perf_counter() - started
    assert refused.value.code == "pattern_timeout"
    assert scanned.count("deadline") <= text_structure.GREP_WORKERS  # only scans already on a worker ran out
    assert len(scanned) <= reading.GREP_CONCURRENCY < len(docs)  # the rest never started
    assert elapsed < 3


def test_reads_and_scans_stay_within_the_gate_on_grep_threads(monkeypatch):
    active, peak = 0, 0
    threads: set[str] = set()
    lock = threading.Lock()

    async def read(doc):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return f"{doc['title']}\n容量 3.2 V\n"

    def scan(text, regex, max_lines):
        with lock:
            threads.add(threading.current_thread().name)
        return text_structure._scan(text, regex, max_lines)

    monkeypatch.setattr(text_structure, "grep_lines", scan)
    result = asyncio.run(reading.grep(_docs(30), read, "容量", max_results=50, max_lines_per_doc=5))
    assert result["total_documents"] == 30 and result["total_matches"] == 30
    assert peak <= reading.GREP_CONCURRENCY
    assert threads and all(name.startswith("kb-grep") for name in threads)
