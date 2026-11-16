"""doc_grep with untrusted patterns: exponential patterns are refused, slow ones time out, the service keeps answering."""

from __future__ import annotations

import asyncio
import re
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb_service import text_structure as ts
from kb_service.api import da_tools

EXPONENTIAL = [
    r"(a+)+$",
    r"(a*)*$",
    r"(a|a)+$",
    r"(a|aa)+$",
    r"(\w+\s?)+$",
    r"(a?)+$",
    r"(?:a+)+b",
    r"(\d+|\w+)+$",
    r"(x+x+)+y",
    r"^(\w+\.?)+@",
    r"(.*a){12}",
]
ACCEPTED = [
    r"\d+(\.\d+)+",
    r"(?:[,;]\s*)+",
    r"(\d{1,3}\.){3}\d{1,3}",
    r"(?:\s*,\s*\w+)+",
    r"v3\.2\.1-rc4",
    r"[A-Z]{3}-\d{4}",
    r"出口压力|联锁",
    r"colou?r",
]


@pytest.mark.parametrize("pattern", EXPONENTIAL)
def test_exponential_patterns_are_refused_at_once(pattern):
    started = time.perf_counter()
    with pytest.raises(ts.PatternError) as info:
        ts.compile_pattern(pattern)
    assert time.perf_counter() - started < 0.5
    assert "正则过于复杂" in str(info.value) and info.value.code == "bad_pattern"


def test_backreferences_are_refused():
    with pytest.raises(ts.PatternError, match="反向引用"):
        ts.compile_pattern(r"(ab)\1")


@pytest.mark.parametrize("pattern", ACCEPTED)
def test_ordinary_patterns_still_compile(pattern):
    regex, mode = ts.compile_pattern(pattern)
    assert mode == "regex" and regex.flags & re.IGNORECASE


def test_words_and_fixed_counts_match_in_process(monkeypatch):
    def no_worker():
        raise AssertionError("a pattern without backtracking must not need a worker process")

    monkeypatch.setattr(ts, "_pool", no_worker)
    text = "LFP-2048X 额定 3.2V\nE-0x1F3A 告警\n无关"
    for pattern in (r"LFP-2048X", r"[A-Z]-0x[0-9A-F]{4}", r"告警|额定", "a("):
        regex, _ = ts.compile_pattern(pattern)
        assert ts.grep_lines(text, regex, 5)[0] >= 0


def test_quantified_patterns_match_in_a_worker_like_in_process():
    text = "版本 v3.2.1-rc4\n电压 3.2 V\n地址 192.0.2.10\n没有数字\n" + "x" * 700
    for pattern in (r"\d+(\.\d+)+", r"v\d+(?:\.\d+)*-rc\d+", r"x+$"):
        regex, _ = ts.compile_pattern(pattern)
        assert ts._analyze(regex.pattern, regex.flags)[1], pattern
        assert ts.grep_lines(text, regex, 2) == ts._scan(text, regex, 2)


def test_a_slow_pattern_times_out_and_is_paused(monkeypatch):
    monkeypatch.setattr(ts, "GREP_TIMEOUT_S", 0.5)
    regex, _ = ts.compile_pattern(r"\d+\d+\d+\d+$")  # polynomial: passes the static check
    started = time.perf_counter()
    with pytest.raises(ts.PatternTimeout) as info:
        ts.grep_lines("1" * 400 + "x", regex, 5)
    assert time.perf_counter() - started < 3
    assert info.value.code == "pattern_timeout" and "超时" in str(info.value)

    started = time.perf_counter()
    with pytest.raises(ts.PatternTimeout):
        ts.grep_lines("12", regex, 5)  # paused: fails fast instead of burning another worker slot
    assert time.perf_counter() - started < 0.2

    other, _ = ts.compile_pattern(r"o+k")  # the killed worker is replaced
    assert ts.grep_lines("ok\nno\nooook", other, 5)[0] == 2


async def test_event_loop_keeps_running_while_a_worker_times_out(monkeypatch):
    monkeypatch.setattr(ts, "GREP_TIMEOUT_S", 1.0)
    regex, _ = ts.compile_pattern(r"\d+\d+\d+\d+\d+$")
    ticks = 0

    async def heartbeat():  # stands in for /healthz while doc_grep runs in a thread
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        with pytest.raises(ts.PatternTimeout):
            await asyncio.to_thread(ts.grep_lines, "9" * 400 + "x", regex, 5)
    finally:
        beat.cancel()
    assert ticks >= 10


def test_doc_grep_answers_a_catastrophic_pattern_with_a_readable_error(monkeypatch):
    async def fake_context(request, capability):
        return type("Caller", (), {"tenant_id": "tenant_demo"})(), ["kb_demo"]

    def no_database():
        raise AssertionError("a refused pattern must not reach the documents")

    monkeypatch.setattr(da_tools, "_context", fake_context)
    monkeypatch.setattr(da_tools.retrieval.db, "pool", no_database)
    app = FastAPI()
    app.include_router(da_tools.router)
    started = time.perf_counter()
    r = TestClient(app).post("/v1/tools/doc_grep", json={"pattern": "(a+)+$"})
    assert time.perf_counter() - started < 2
    body = r.json()
    assert r.status_code == 200 and "正则过于复杂" in body["error"] and body["code"] == "bad_pattern"


def test_grep_endpoint_reports_a_timeout_as_400(api_client, monkeypatch):
    async def slow(*args, **kwargs):
        raise ts.PatternTimeout("正则执行超时（单篇文档超过 3 秒）")

    monkeypatch.setattr("kb_service.retrieval.grep", slow)
    r = api_client.post("/v1/grep", json={"pattern": r"\d+\d+$"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "pattern_timeout"


@pytest.fixture
def fresh_pool():
    pools = []

    def make(size: int) -> ts._WorkerPool:
        pools.append(ts._WorkerPool(size))
        return pools[-1]

    yield make
    for pool in pools:
        pool.close()


def _run_together(count: int, call) -> list[str]:
    outcomes: list[str] = []
    lock = threading.Lock()

    def one() -> None:
        try:
            call()
            result = "ok"
        except ts.PatternTimeout as exc:
            result = str(exc)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=one) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


def test_callers_queued_behind_a_timeout_fail_fast(monkeypatch, fresh_pool):
    monkeypatch.setattr(ts, "GREP_TIMEOUT_S", 0.5)
    pool = fresh_pool(1)
    regex, _ = ts.compile_pattern(r"\d+\d+\d+\d+x$")
    started = time.perf_counter()
    outcomes = _run_together(8, lambda: pool.grep(regex, "7" * 400, 5))
    elapsed = time.perf_counter() - started
    timeouts = [o for o in outcomes if "单篇文档超过" in o]
    paused = [o for o in outcomes if "暂停使用" in o]
    assert len(timeouts) == 1 and len(paused) == 7  # the queued callers did not run their 0.5 s each
    assert elapsed < 3


def test_waiting_for_a_worker_is_bounded(monkeypatch, fresh_pool):
    monkeypatch.setattr(ts, "GREP_QUEUE_TIMEOUT_S", 0.2)
    pool = fresh_pool(1)
    busy = pool._idle.get()  # the only worker is taken
    try:
        regex, _ = ts.compile_pattern(r"o+k")
        started = time.perf_counter()
        with pytest.raises(ts.PatternTimeout, match="排队超时"):
            pool.grep(regex, "ok", 5)
        assert time.perf_counter() - started < 1
    finally:
        pool._idle.put(busy)


async def test_kb_service_grep_runs_on_its_own_threads(monkeypatch):
    from kb_service import retrieval

    seen: list[str] = []

    def rows_scan(*args):
        seen.append(threading.current_thread().name)
        return {"pattern": "", "searchMode": "regex", "totalMatches": 0, "totalDocuments": 0, "hasMore": False,
                "matches": []}

    class Pool:
        async def fetch(self, *args):
            return []

    monkeypatch.setattr(retrieval, "_grep_rows", rows_scan)
    monkeypatch.setattr(retrieval.db, "pool", lambda: Pool())
    await retrieval.grep("tenant_demo", ["kb_demo"], "o+k")
    assert seen and seen[0].startswith("kb-grep")
