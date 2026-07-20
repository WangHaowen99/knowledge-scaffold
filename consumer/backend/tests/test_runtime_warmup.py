import logging
from types import SimpleNamespace

import agent.core.warmup as warmup


class _InlineThread:
    starts = 0

    def __init__(self, *, target, args, name, daemon):
        assert name == "runtime-warmup"
        assert daemon is True
        self._target = target
        self._args = args

    def start(self):
        type(self).starts += 1
        self._target(*self._args)


def _reset_status(monkeypatch):
    _InlineThread.starts = 0
    monkeypatch.setattr(
        warmup,
        "_status",
        warmup.RuntimeWarmupStatus(enabled=False, state="idle"),
    )
    monkeypatch.setattr(warmup.threading, "Thread", _InlineThread)


def test_runtime_warmup_can_be_disabled(monkeypatch):
    _reset_status(monkeypatch)

    status = warmup.start_runtime_warmup(
        agent_config=SimpleNamespace(runtime_warmup_enabled=False)
    )

    assert status.state == "disabled"
    assert status.enabled is False
    assert _InlineThread.starts == 0


def test_runtime_warmup_runs_once_and_records_success(monkeypatch):
    _reset_status(monkeypatch)
    calls = []

    monkeypatch.setattr(
        "agent.core.jiuwen_engine.warm_up_jiuwen_runtime",
        lambda *, agent_config: calls.append(agent_config)
        or {"duration_ms": 17, "mode": "jiuwen-runtime"},
    )
    config = SimpleNamespace(runtime_warmup_enabled=True)

    first = warmup.start_runtime_warmup(agent_config=config)
    second = warmup.start_runtime_warmup(agent_config=config)

    assert first.state == "success"
    assert second.state == "success"
    assert first.duration_ms == 17
    assert first.mode == "jiuwen-runtime"
    assert calls == [config]
    assert _InlineThread.starts == 1


def test_runtime_warmup_failure_is_terminal_without_exception_details(monkeypatch, caplog):
    _reset_status(monkeypatch)

    def fail(*, agent_config):
        raise RuntimeError("warm-up failed api_key=secret-value")

    monkeypatch.setattr(
        "agent.core.jiuwen_engine.warm_up_jiuwen_runtime",
        fail,
    )

    with caplog.at_level(logging.WARNING, logger="warmup"):
        first = warmup.start_runtime_warmup(
            agent_config=SimpleNamespace(runtime_warmup_enabled=True)
        )
        second = warmup.start_runtime_warmup(
            agent_config=SimpleNamespace(runtime_warmup_enabled=True)
        )

    assert first.state == "failed"
    assert first.error_type is None
    assert first.error_message is None
    assert second.state == "failed"
    assert _InlineThread.starts == 1
    records = [record for record in caplog.records if record.name == "warmup"]
    assert any(record.getMessage() == "Runtime warm-up finished state=failed" for record in records)
    assert all(record.exc_info is None for record in records)
    assert "secret-value" not in caplog.text


def test_runtime_warmup_returns_before_daemon_worker_runs(monkeypatch):
    _reset_status(monkeypatch)
    captured = {}

    class DeferredThread(_InlineThread):
        def start(self):
            type(self).starts += 1
            captured["target"] = self._target
            captured["args"] = self._args

    monkeypatch.setattr(warmup.threading, "Thread", DeferredThread)

    status = warmup.start_runtime_warmup(
        agent_config=SimpleNamespace(runtime_warmup_enabled=True)
    )

    assert status.state == "running"
    assert captured["target"] is warmup._run_warmup


def test_runtime_warmup_is_enabled_by_default(monkeypatch):
    _reset_status(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.warm_up_jiuwen_runtime",
        lambda *, agent_config: calls.append(agent_config)
        or {"duration_ms": 1, "mode": "jiuwen-runtime"},
    )

    status = warmup.start_runtime_warmup()

    assert status.state == "success"
    assert status.enabled is True
    assert calls == [None]
    assert _InlineThread.starts == 1
