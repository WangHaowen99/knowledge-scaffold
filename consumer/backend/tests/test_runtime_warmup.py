import asyncio
import logging
from types import SimpleNamespace

import agent.core.warmup as warmup
import pytest


class _InlineShortIO:
    async def run(self, function, **kwargs):
        return function(**kwargs)


class _Supervisor:
    def __init__(self):
        self.starts = 0

    def start_derived(self, awaitable, *, name):
        assert name == "runtime-warmup"
        self.starts += 1
        return asyncio.create_task(awaitable, name=name)


def _runtime(*, short_io=None):
    return SimpleNamespace(
        short_io=short_io or _InlineShortIO(),
        supervisor=_Supervisor(),
    )


def _reset_status(monkeypatch):
    monkeypatch.setattr(
        warmup,
        "_status",
        warmup.RuntimeWarmupStatus(enabled=False, state="idle"),
    )
    monkeypatch.setattr(warmup, "_task", None)


def test_runtime_warmup_can_be_disabled_without_runtime(monkeypatch):
    _reset_status(monkeypatch)

    status = warmup.start_runtime_warmup(agent_config=SimpleNamespace(runtime_warmup_enabled=False))

    assert status.state == "disabled"
    assert status.enabled is False


def test_runtime_warmup_runs_once_on_supervised_short_io(monkeypatch):
    _reset_status(monkeypatch)
    calls = []

    def warm(*, agent_config, cancel_event):
        calls.append((agent_config, cancel_event.is_set()))
        return {"duration_ms": 17, "mode": "jiuwen-runtime"}

    monkeypatch.setattr(warmup, "_load_runtime_warmup", lambda: warm)
    config = SimpleNamespace(runtime_warmup_enabled=True)
    runtime = _runtime()

    async def exercise():
        first = warmup.start_runtime_warmup(
            agent_config=config,
            runtime=runtime,
        )
        assert first.state == "running"
        await warmup._task
        second = warmup.start_runtime_warmup(
            agent_config=config,
            runtime=runtime,
        )
        return second

    status = asyncio.run(exercise())

    assert status.state == "success"
    assert status.duration_ms == 17
    assert status.mode == "jiuwen-runtime"
    assert calls == [(config, False)]
    assert runtime.supervisor.starts == 1


def test_runtime_warmup_failure_is_terminal_without_exception_details(
    monkeypatch,
    caplog,
):
    _reset_status(monkeypatch)

    def fail(*, agent_config, cancel_event):
        del agent_config, cancel_event
        raise RuntimeError("warm-up failed detail=private-marker")

    monkeypatch.setattr(warmup, "_load_runtime_warmup", lambda: fail)

    async def exercise():
        runtime = _runtime()
        warmup.start_runtime_warmup(
            agent_config=SimpleNamespace(runtime_warmup_enabled=True),
            runtime=runtime,
        )
        await warmup._task

    with caplog.at_level(logging.WARNING, logger="warmup"):
        asyncio.run(exercise())

    status = warmup.get_runtime_warmup_status()
    assert status.state == "failed"
    assert status.error_type is None
    assert status.error_message is None
    records = [record for record in caplog.records if record.name == "warmup"]
    assert any(record.getMessage() == "Runtime warm-up finished state=failed" for record in records)
    assert all(record.exc_info is None for record in records)
    assert "private-marker" not in caplog.text


def test_runtime_warmup_cancel_uses_absolute_deadline(monkeypatch):
    _reset_status(monkeypatch)
    monkeypatch.setattr(
        warmup,
        "_load_runtime_warmup",
        lambda: (lambda **_kwargs: None),
    )
    started = asyncio.Event()

    class BlockingShortIO:
        async def run(self, function, **kwargs):
            del function, kwargs
            started.set()
            await asyncio.Event().wait()

    async def exercise():
        runtime = _runtime(short_io=BlockingShortIO())
        status = warmup.start_runtime_warmup(
            agent_config=SimpleNamespace(runtime_warmup_enabled=True),
            runtime=runtime,
        )
        assert status.state == "running"
        await started.wait()
        deadline = asyncio.get_running_loop().time() + 1.0
        assert await warmup.stop_runtime_warmup(deadline=deadline) is True

    asyncio.run(exercise())

    assert warmup.get_runtime_warmup_status().state == "cancelled"
    assert warmup._task is None


def test_runtime_warmup_requires_lifespan_runtime(monkeypatch):
    _reset_status(monkeypatch)

    with pytest.raises(RuntimeError, match="lifespan-owned RuntimeContainer"):
        warmup.start_runtime_warmup(agent_config=SimpleNamespace(runtime_warmup_enabled=True))
