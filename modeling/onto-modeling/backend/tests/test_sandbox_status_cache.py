"""脚本页沙箱摘要的缓存：60 秒内复用、过期先给旧结果并后台重测、换了检测函数不用旧结果。"""

import threading

from app.services import sandbox_status


def _probe_counter(monkeypatch, results):
    calls = []

    def probe():
        calls.append(1)
        return results[min(len(calls), len(results)) - 1]

    monkeypatch.setattr(sandbox_status, "status", probe)
    monkeypatch.setattr(sandbox_status, "_cache", None)
    return calls


def test_cached_within_ttl(monkeypatch):
    calls = _probe_counter(monkeypatch, [{"connected": True}])
    assert sandbox_status.cached_status() == {"connected": True}
    assert sandbox_status.cached_status() == {"connected": True}
    assert len(calls) == 1


def test_stale_returns_old_and_refreshes_in_background(monkeypatch):
    calls = _probe_counter(monkeypatch, [{"connected": True}, {"connected": False}])
    clock = [1000.0]
    monkeypatch.setattr(sandbox_status.time, "monotonic", lambda: clock[0])
    assert sandbox_status.cached_status() == {"connected": True}
    clock[0] += sandbox_status.CACHE_SECONDS + 1
    done = threading.Event()
    original = sandbox_status._store

    def store(probe):
        try:
            return original(probe)
        finally:
            done.set()

    monkeypatch.setattr(sandbox_status, "_store", store)
    assert sandbox_status.cached_status() == {"connected": True}, "过期时先给旧结果，不让页面等检测"
    assert done.wait(5)
    assert sandbox_status.cached_status() == {"connected": False}
    assert len(calls) == 2


def test_replaced_probe_ignores_old_result(monkeypatch):
    _probe_counter(monkeypatch, [{"connected": True}])
    assert sandbox_status.cached_status() == {"connected": True}
    monkeypatch.setattr(sandbox_status, "status", lambda: {"connected": False})
    assert sandbox_status.cached_status() == {"connected": False}
