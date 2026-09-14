# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from internal_access import internal
import asyncio
import threading
from types import SimpleNamespace

import app as app_module
import pytest
from agent.runtime.config import NativeAsyncRuntimeConfig
from agent.runtime.event_bus import AsyncEventBus, EventBusClosedError
from agent.runtime.runtime import RuntimeContainer, RuntimeState
from agent.runtime.store import InMemoryRunRepository
from app import _run_session_recovery_batches, _stop_session_recovery


class _LifecycleRecoveryManager:
    def __init__(self, *, failures=0):
        self.failures = failures
        self.batch_calls = []
        self.close_calls = []
        self.summary_close_calls = []

    def recover_pending_sessions_batch(self, **options):
        self.batch_calls.append(options)
        return {
            "processed": 1,
            "recovered": 0,
            "failures": self.failures,
            "remaining": 0,
            "complete": True,
        }

    def close_recovery_discovery(self, *, wait_seconds):
        self.close_calls.append(wait_seconds)
        return True

    def close_summary_rebuilds(self, *, wait_seconds):
        self.summary_close_calls.append(wait_seconds)
        return True


def _build_lifespan_test_app(monkeypatch, manager):
    class FakeRuntimeContainer:
        def __init__(self, *, config, components_factory):
            del components_factory
            self.config = config
            self.runtime = object()

        async def start(self):
            return True

        async def close(self, *, deadline):
            assert deadline > asyncio.get_running_loop().time()
            return True

    async def stop_warmup(*, deadline):
        assert deadline > asyncio.get_running_loop().time()
        return True

    services = {
        "config": object(),
        "agent_config": object(),
        "llm_router": object(),
        "auth_manager": object(),
        "user_store_manager": manager,
        "solver_coordination_service": None,
    }
    monkeypatch.setattr(app_module, "run_startup_migrations", lambda: None)
    monkeypatch.setattr(
        app_module,
        "recover_workspace_ontology_replacements",
        lambda _root: None,
    )
    monkeypatch.setattr(app_module, "get_workspaces_root", lambda: object())
    monkeypatch.setattr(app_module, "build_runtime_services", lambda: services)
    monkeypatch.setattr(app_module, "register_routers", lambda _app: None)
    monkeypatch.setattr(app_module, "initialize_open_telemetry", lambda _app: None)
    monkeypatch.setattr(app_module, "configure_llm_router", lambda _router: None)
    monkeypatch.setattr(app_module, "configure_solver_tool_service", lambda _service: None)
    monkeypatch.setattr(app_module, "_install_solver_recovery_worker", lambda *_args: None)
    monkeypatch.setattr(app_module, "_stop_solver_recovery_worker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module,
        "initialize_runtime_dependencies",
        lambda *_args, **_kwargs: SimpleNamespace(
            operation_patcher=None,
            reflection_worker=None,
        ),
    )
    monkeypatch.setattr(
        app_module,
        "NativeAsyncRuntimeConfig",
        SimpleNamespace(
            from_agent_config=lambda _config: SimpleNamespace(
                shutdown_total_deadline_seconds=1.0,
            )
        ),
    )
    monkeypatch.setattr(app_module, "RuntimeContainer", FakeRuntimeContainer)
    monkeypatch.setattr(app_module, "components_factory", lambda _factory: object())
    monkeypatch.setattr(app_module, "set_agent_execution_runtime", lambda _runtime: None)
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **_kwargs: None)
    monkeypatch.setattr(app_module, "stop_runtime_warmup", stop_warmup)
    monkeypatch.setattr(app_module, "init_skill_evolution_worker", lambda: None)
    monkeypatch.setattr(app_module, "reset_skill_evolution_singletons", lambda: None)
    monkeypatch.setattr(app_module, "refresh_evidence_index_worker", lambda: None)
    monkeypatch.setattr(app_module, "start_platform_observer_if_enabled", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_platform_observer", lambda **_kwargs: None)
    monkeypatch.setattr(app_module, "shutdown_observer", lambda **_kwargs: None)
    return app_module.create_app()


async def _wait_for_recovery_batch(manager):
    for _attempt in range(100):
        if manager.batch_calls:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("session recovery task did not execute")


def test_session_recovery_batches_yield_until_bounded_queue_is_complete():
    class Manager:
        def __init__(self):
            self.calls = 0

        def recover_pending_sessions_batch(self, **options):
            assert options["max_items"] == 25
            assert options["max_seconds"] == 0.25
            assert isinstance(options["stop_event"], threading.Event)
            self.calls += 1
            return {
                "processed": 25 if self.calls == 1 else 1,
                "recovered": 0,
                "failures": 0,
                "remaining": 1 if self.calls == 1 else 0,
                "complete": self.calls > 1,
            }

    manager = Manager()
    outcome = asyncio.run(_run_session_recovery_batches(manager, threading.Event()))

    assert manager.calls == 2
    assert outcome["complete"] is True


def test_session_recovery_reports_failure_only_after_scanning_remaining_users():
    class Manager:
        def __init__(self):
            self.calls = 0

        def recover_pending_sessions_batch(self, **_options):
            self.calls += 1
            return {
                "processed": 1,
                "recovered": 0,
                "failures": 1 if self.calls == 1 else 0,
                "remaining": 1 if self.calls == 1 else 0,
                "complete": self.calls > 1,
            }

        @staticmethod
        def close_recovery_discovery(*, wait_seconds):
            assert wait_seconds == 0.25
            return True

    manager = Manager()
    with pytest.raises(RuntimeError, match="1 failure"):
        asyncio.run(_run_session_recovery_batches(manager, threading.Event()))

    assert manager.calls == 2


def test_session_recovery_batch_propagates_worker_drain_failure():
    class Manager:
        @staticmethod
        def recover_pending_sessions_batch(**_options):
            raise AssertionError("a pre-stopped recovery must not schedule work")

        @staticmethod
        def close_recovery_discovery(*, wait_seconds):
            assert wait_seconds == 0.25
            return False

    stopped = threading.Event()
    stopped.set()
    with pytest.raises(RuntimeError, match="did not drain"):
        asyncio.run(_run_session_recovery_batches(Manager(), stopped))


def test_session_recovery_shutdown_waits_for_cooperative_exit_without_cancelling():
    async def scenario():
        stop = threading.Event()
        completed = asyncio.Event()

        async def worker():
            while not stop.is_set():
                await asyncio.sleep(0)
            await asyncio.sleep(0.02)
            completed.set()

        task = asyncio.create_task(worker())
        assert await _stop_session_recovery(task, stop, timeout=0.001) is False
        assert task.cancelled() is False
        await task
        assert completed.is_set()
        assert task.cancelled() is False

    asyncio.run(scenario())


def test_create_app_lifespan_starts_bounded_recovery_and_drains_it_on_shutdown(
    monkeypatch,
):
    manager = _LifecycleRecoveryManager()
    application = _build_lifespan_test_app(monkeypatch, manager)

    async def scenario():
        async with application.router.lifespan_context(application):
            task = application.state.session_recovery_task
            assert isinstance(task, asyncio.Task)
            assert task.get_name() == "session-summary-recovery"
            await _wait_for_recovery_batch(manager)

    asyncio.run(scenario())

    assert len(manager.batch_calls) == 1
    options = manager.batch_calls[0]
    assert options["max_items"] == 25
    assert options["max_seconds"] == 0.25
    assert isinstance(options["stop_event"], threading.Event)
    assert manager.close_calls == [0.25]
    assert len(manager.summary_close_calls) == 1
    assert 0 <= manager.summary_close_calls[0] <= 2.0
    assert application.state.session_recovery_task is None


def test_create_app_lifespan_reports_recovery_failures_as_incomplete_shutdown(
    monkeypatch,
):
    manager = _LifecycleRecoveryManager(failures=1)
    application = _build_lifespan_test_app(monkeypatch, manager)

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="Application shutdown incomplete: session-recovery",
        ):
            async with application.router.lifespan_context(application):
                await _wait_for_recovery_batch(manager)

    asyncio.run(scenario())

    assert manager.close_calls == [0.25]
    assert application.state.session_recovery_task is None


def test_create_app_lifespan_reports_incomplete_observer_shutdown(monkeypatch):
    manager = _LifecycleRecoveryManager()
    application = _build_lifespan_test_app(monkeypatch, manager)
    monkeypatch.setattr(app_module, "shutdown_observer", lambda **_kwargs: False)

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="Application shutdown incomplete: observer",
        ):
            async with application.router.lifespan_context(application):
                await _wait_for_recovery_batch(manager)

    asyncio.run(scenario())


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

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
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
            config=NativeAsyncRuntimeConfig(shutdown_total_deadline_seconds=1.0),
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

        assert await container.close(deadline=asyncio.get_running_loop().time() + 1.0)
        assert container.metrics()["event_hub"]["reservations"] == 0
        with pytest.raises(EventBusClosedError):
            await event_bus.reserve_subscriber("run_after_shutdown")

    asyncio.run(scenario())


def test_safe_sync_step_propagates_explicit_incomplete_result():
    assert internal(app_module).safe_sync_step("synthetic", lambda: False) is False
    assert internal(app_module).safe_sync_step("synthetic", lambda: None) is True
