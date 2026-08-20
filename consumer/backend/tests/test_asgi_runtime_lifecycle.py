from __future__ import annotations

import asyncio

import pytest

from agent.runtime.config import NativeAsyncRuntimeConfig
from agent.runtime.event_bus import AsyncEventBus, EventBusClosedError
from agent.runtime.runtime import RuntimeContainer, RuntimeState
from agent.runtime.store import InMemoryRunRepository


class _RunnerLifecycle:
    def __init__(self, calls):
        self.calls = calls
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self, *, mode):
        self.start_calls += 1
        self.calls.append(("runner.start", mode))
        return True

    async def stop(self):
        self.stop_calls += 1
        self.calls.append(("runner.stop", None))
        return True


class _Supervisor:
    def __init__(self, calls, deadlines):
        self.calls = calls
        self.deadlines = deadlines

    @property
    def active_run_ids(self):
        return ()

    @property
    def active_derived_count(self):
        return 0

    async def close(self, *, deadline):
        self.calls.append(("supervisor.close", None))
        self.deadlines.append(deadline)
        return True


class _Manager:
    def __init__(self, calls, deadlines):
        self.calls = calls
        self.deadlines = deadlines

    async def stop_accepting(self):
        self.calls.append(("manager.stop_accepting", None))

    async def shutdown(self, *, deadline):
        self.calls.append(("manager.shutdown", None))
        self.deadlines.append(deadline)
        return True


class _EventHub:
    def __init__(self, calls, deadlines):
        self.calls = calls
        self.deadlines = deadlines

    async def start(self):
        self.calls.append(("event_hub.start", None))

    async def shutdown(self, *, deadline):
        self.calls.append(("event_hub.shutdown", None))
        self.deadlines.append(deadline)
        return True

    def metrics(self):
        return {"subscribers": 0, "reservations": 0}


class _HttpClient:
    def __init__(self, calls):
        self.calls = calls

    async def aclose(self):
        self.calls.append(("http_client.aclose", None))


class _Pool:
    def __init__(self, *, calls, deadlines, name, **_kwargs):
        self.calls = calls
        self.deadlines = deadlines
        self.name = name
        self.closed = False

    async def close(self, *, deadline):
        self.calls.append((f"{self.name}.close", None))
        self.deadlines.append(deadline)
        self.closed = True
        return True

    def metrics(self):
        return {"closed": self.closed, "pending": 0, "unfinished": 0}


def _pool_factory(calls, deadlines):
    def create(**kwargs):
        return _Pool(calls=calls, deadlines=deadlines, **kwargs)

    return create


def test_runtime_container_starts_and_stops_once_with_one_deadline():
    async def scenario():
        calls = []
        deadlines = []
        runner = _RunnerLifecycle(calls)
        supervisor = _Supervisor(calls, deadlines)
        manager = _Manager(calls, deadlines)
        event_hub = _EventHub(calls, deadlines)
        container = RuntimeContainer(
            config=NativeAsyncRuntimeConfig(),
            runner_lifecycle=runner,
            manager=manager,
            event_hub=event_hub,
            supervisor=supervisor,
            http_client_factory=lambda: _HttpClient(calls),
            pool_factory=_pool_factory(calls, deadlines),
        )

        assert container.state is RuntimeState.CREATED
        assert container.readiness() == {"ready": False, "state": "CREATED"}
        assert await container.start() is True
        assert await container.start() is False
        assert container.state is RuntimeState.ACCEPTING
        assert container.is_ready

        deadline = asyncio.get_running_loop().time() + 1.0
        assert await container.close(deadline=deadline) is True
        assert await container.close(deadline=deadline + 10.0) is True

        assert runner.start_calls == 1
        assert runner.stop_calls == 1
        assert deadlines == [deadline] * 6
        assert [name for name, _detail in calls] == [
            "runner.start",
            "event_hub.start",
            "manager.stop_accepting",
            "manager.shutdown",
            "supervisor.close",
            "event_hub.shutdown",
            "http_client.aclose",
            "tool-io.close",
            "external-io.close",
            "short-io.close",
            "runner.stop",
        ]
        assert container.state is RuntimeState.STOPPED
        assert not container.is_ready
        assert container.metrics()["start_count"] == 1
        assert container.metrics()["close_count"] == 1
        assert container.metrics()["close_complete"] is True

    asyncio.run(scenario())


def test_runtime_container_partial_start_failure_cleans_up_in_reverse_order():
    async def scenario():
        calls = []
        deadlines = []
        runner = _RunnerLifecycle(calls)
        supervisor = _Supervisor(calls, deadlines)
        manager = _Manager(calls, deadlines)
        event_hub = _EventHub(calls, deadlines)

        def fail_client_start():
            calls.append(("http_client.create", None))
            raise RuntimeError("synthetic client startup failure")

        container = RuntimeContainer(
            config=NativeAsyncRuntimeConfig(
                shutdown_total_deadline_seconds=1.0
            ),
            runner_lifecycle=runner,
            manager=manager,
            event_hub=event_hub,
            supervisor=supervisor,
            http_client_factory=fail_client_start,
            pool_factory=_pool_factory(calls, deadlines),
        )

        with pytest.raises(RuntimeError, match="synthetic client startup failure"):
            await container.start()

        assert [name for name, _detail in calls] == [
            "runner.start",
            "event_hub.start",
            "http_client.create",
            "event_hub.shutdown",
            "manager.shutdown",
            "tool-io.close",
            "external-io.close",
            "short-io.close",
            "supervisor.close",
            "runner.stop",
        ]
        assert len(set(deadlines)) == 1
        assert container.state is RuntimeState.STOPPED
        assert not container.is_ready
        assert container.metrics()["start_count"] == 0
        assert container.metrics()["close_complete"] is True

    asyncio.run(scenario())


def test_runtime_container_closes_the_process_level_event_bus():
    async def scenario():
        calls = []
        deadlines = []
        event_bus = AsyncEventBus(InMemoryRunRepository())
        container = RuntimeContainer(
            runner_lifecycle=_RunnerLifecycle(calls),
            event_hub=event_bus,
            supervisor=_Supervisor(calls, deadlines),
            pool_factory=_pool_factory(calls, deadlines),
        )

        await container.start()
        reservation_id = await event_bus.reserve_subscriber("run_synthetic")
        assert reservation_id
        assert container.metrics()["event_hub"]["reservations"] == 1

        assert await container.close(
            deadline=asyncio.get_running_loop().time() + 1.0
        )
        assert container.metrics()["event_hub"]["reservations"] == 0
        with pytest.raises(EventBusClosedError):
            await event_bus.reserve_subscriber("run_after_shutdown")

    asyncio.run(scenario())
