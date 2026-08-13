"""Phase-1 tests for openJiuwen Runner and event-loop ownership."""

from __future__ import annotations

import asyncio
import threading

import pytest

import agent.runtime.lifecycle as lifecycle_module
from agent.runtime.lifecycle import (
    JiuwenRunnerLifecycle,
    JiuwenRuntimeLifecycleError,
    LegacyJiuwenLoopRunner,
)


class _FakeRunner:
    def __init__(self) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.released_session_ids: list[str] = []
        self.start_loop_ids: list[int] = []
        self.stop_loop_ids: list[int] = []

    async def start(self) -> bool:
        self.start_calls += 1
        self.start_loop_ids.append(id(asyncio.get_running_loop()))
        await asyncio.sleep(0)
        return True

    async def stop(self) -> bool:
        self.stop_calls += 1
        self.stop_loop_ids.append(id(asyncio.get_running_loop()))
        await asyncio.sleep(0)
        return True

    async def release(self, session_id: str) -> None:
        self.released_session_ids.append(session_id)
        await asyncio.sleep(0)


def test_runner_lifecycle_start_stop_and_release_are_idempotent():
    runner = _FakeRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)

    async def exercise():
        starts = await asyncio.gather(
            lifecycle.start(mode="async"),
            lifecycle.start(mode="async"),
            lifecycle.start(mode="async"),
        )
        await lifecycle.release("session-01")
        first_stop = await lifecycle.stop()
        second_stop = await lifecycle.stop()
        return starts, first_stop, second_stop

    starts, first_stop, second_stop = asyncio.run(exercise())

    assert starts.count(True) == 1
    assert starts.count(False) == 2
    assert first_stop is True
    assert second_stop is False
    assert runner.start_calls == 1
    assert runner.stop_calls == 1
    assert runner.released_session_ids == ["session-01"]
    assert runner.start_loop_ids == runner.stop_loop_ids
    assert lifecycle.snapshot().state == "stopped"
    assert lifecycle.snapshot().owner_loop_id is None


def test_runner_lifecycle_rejects_mode_mixing_on_one_loop():
    runner = _FakeRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)

    async def exercise():
        await lifecycle.start(mode="legacy")
        with pytest.raises(JiuwenRuntimeLifecycleError, match="separate process"):
            await lifecycle.start(mode="async")
        await lifecycle.stop()

    asyncio.run(exercise())


def test_runner_lifecycle_rejects_unknown_mode():
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: _FakeRunner())

    with pytest.raises(ValueError, match="legacy.*async"):
        asyncio.run(lifecycle.start(mode="worker"))  # type: ignore[arg-type]


def test_runner_lifecycle_rejects_a_different_event_loop():
    runner = _FakeRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)

    asyncio.run(lifecycle.start(mode="async"))

    with pytest.raises(JiuwenRuntimeLifecycleError, match="different event loop"):
        asyncio.run(lifecycle.start(mode="async"))


def test_runner_lifecycle_failed_start_can_be_cleared_and_retried():
    class FlakyRunner(_FakeRunner):
        async def start(self) -> bool:
            await super().start()
            if self.start_calls == 1:
                raise RuntimeError("start failed")
            return True

    runner = FlakyRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)

    async def exercise():
        with pytest.raises(RuntimeError, match="start failed"):
            await lifecycle.start(mode="async")
        assert await lifecycle.stop() is False
        assert await lifecycle.start(mode="async") is True
        assert await lifecycle.stop() is True

    asyncio.run(exercise())
    assert runner.start_calls == 2
    assert runner.stop_calls == 1


def test_runner_lifecycle_failed_start_can_retry_on_a_new_event_loop():
    class FlakyRunner(_FakeRunner):
        async def start(self) -> bool:
            await super().start()
            if self.start_calls == 1:
                raise RuntimeError("start failed")
            return True

    runner = FlakyRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)

    with pytest.raises(RuntimeError, match="start failed"):
        asyncio.run(lifecycle.start(mode="async"))

    async def retry_and_stop():
        assert await lifecycle.start(mode="async") is True
        assert await lifecycle.stop() is True

    asyncio.run(retry_and_stop())
    assert runner.start_calls == 2
    assert runner.stop_calls == 1
    assert lifecycle.snapshot().owner_loop_id is None


def test_async_lifespan_starts_and_stops_the_same_owner(monkeypatch):
    runner = _FakeRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)
    monkeypatch.setattr(lifecycle_module, "_JIUWEN_RUNNER_LIFECYCLE", lifecycle)

    async def exercise():
        async with lifecycle_module.jiuwen_runtime_lifespan() as active:
            assert active is lifecycle
            assert active.snapshot().state == "started"
            assert active.snapshot().mode == "async"
        assert lifecycle.snapshot().state == "stopped"

    asyncio.run(exercise())
    assert runner.start_calls == 1
    assert runner.stop_calls == 1


def test_legacy_loop_starts_runner_before_accepting_work_and_stops_in_order():
    runner = _FakeRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)
    legacy = LegacyJiuwenLoopRunner(lifecycle=lifecycle)

    loop = legacy.get_loop()
    assert lifecycle.snapshot().state == "started"
    assert lifecycle.snapshot().mode == "legacy"
    assert lifecycle.snapshot().owner_loop_id == id(loop)

    async def identify_loop_and_thread():
        return id(asyncio.get_running_loop()), threading.current_thread().name

    worker_loop_id, worker_name = legacy.submit(identify_loop_and_thread()).result(timeout=2)
    assert worker_loop_id == id(loop)
    assert worker_name == "jiuwen-loop"

    assert legacy.shutdown() is True
    assert legacy.shutdown() is False
    assert runner.start_calls == 1
    assert runner.stop_calls == 1
    assert runner.start_loop_ids == runner.stop_loop_ids == [id(loop)]
    assert lifecycle.snapshot().state == "stopped"


def test_legacy_loop_does_not_accept_work_when_runner_start_fails():
    class FailingRunner(_FakeRunner):
        async def start(self) -> bool:
            await super().start()
            raise RuntimeError("runner unavailable")

    runner = FailingRunner()
    lifecycle = JiuwenRunnerLifecycle(runner_loader=lambda: runner)
    legacy = LegacyJiuwenLoopRunner(lifecycle=lifecycle)

    with pytest.raises(JiuwenRuntimeLifecycleError, match="failed to start"):
        legacy.get_loop()

    assert lifecycle.snapshot().state == "stopped"
    assert lifecycle.snapshot().owner_loop_id is None
