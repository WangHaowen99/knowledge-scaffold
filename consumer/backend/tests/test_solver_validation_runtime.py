from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import app as app_module
from solver_coordination import OperationValidationRunService, SyntheticValidationService
from solver_coordination.runtime import build_operation_validation_service


def _runtime_bundle(validation_service):
    return {
        "auth_manager": object(),
        "user_store_manager": object(),
        "llm_router": object(),
        "config": object(),
        "agent_config": object(),
        "solver_validation_service": validation_service,
        "solver_coordination_service": None,
    }


def _stub_lifespan(monkeypatch, runtime):
    class FakeRuntimeContainer:
        def __init__(self, *, config, components_factory):
            self.config = config
            self.components_factory = components_factory

        async def start(self):
            return None

        async def close(self, *, deadline):
            return True

    async def stop_runtime_warmup(*, deadline):
        return True

    monkeypatch.setattr(app_module, "run_startup_migrations", lambda: None)
    monkeypatch.setattr(
        app_module,
        "recover_workspace_ontology_replacements",
        lambda _root: None,
    )
    monkeypatch.setattr(app_module, "build_runtime_services", lambda **kwargs: runtime)
    monkeypatch.setattr(
        app_module,
        "initialize_runtime_dependencies",
        lambda *args, **kwargs: SimpleNamespace(
            operation_patcher=None,
            reflection_worker=None,
        ),
    )
    monkeypatch.setattr(app_module, "RuntimeContainer", FakeRuntimeContainer)
    monkeypatch.setattr(
        app_module.NativeAsyncRuntimeConfig,
        "from_agent_config",
        lambda _config: SimpleNamespace(shutdown_total_deadline_seconds=30.0),
    )
    monkeypatch.setattr(app_module, "components_factory", lambda _provider: object())
    monkeypatch.setattr(app_module, "set_agent_execution_runtime", lambda _runtime: None)
    monkeypatch.setattr(app_module, "configure_solver_tool_service", lambda _service: None)
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "stop_runtime_warmup", stop_runtime_warmup)
    monkeypatch.setattr(app_module, "init_skill_evolution_worker", lambda: None)
    monkeypatch.setattr(app_module, "reset_skill_evolution_singletons", lambda: None)
    monkeypatch.setattr(app_module, "start_platform_observer_if_enabled", lambda: None)
    monkeypatch.setattr(app_module, "shutdown_platform_observer", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "shutdown_observer", lambda **kwargs: None)


def test_production_factory_builds_operation_backed_service():
    runtime = object()
    service = build_operation_validation_service(operation_runtime=runtime)

    assert isinstance(service, OperationValidationRunService)
    assert not isinstance(service, SyntheticValidationService)
    assert callable(service.run)
    assert service.validator_revision == "ontology_operation_batch.v1"
    assert service.batch_validator.operation_runtime is runtime


def test_build_runtime_services_includes_validation_service(monkeypatch, tmp_path):
    config = SimpleNamespace(
        auth_server_url="https://auth.example.net",
        auth_server_admin_token="synthetic-admin-token",
        auth_server_audience="synthetic-audience",
        auth_server_issuer="https://auth.example.net",
        chat_storage_dir=tmp_path / "chat",
        execution_storage_dir=tmp_path / "execution",
    )
    agent_config = SimpleNamespace(limits=object())
    expected_service = object()

    monkeypatch.setattr(app_module.AppConfig, "from_env", lambda: config)
    monkeypatch.setattr(app_module.AgentConfig, "from_env", lambda: agent_config)
    monkeypatch.setattr(app_module, "LLMRouter", lambda *args, **kwargs: object())
    monkeypatch.setattr(app_module, "configure_llm_router", lambda router: None)
    monkeypatch.setattr(app_module, "RemoteAuthBackend", lambda **kwargs: object())
    monkeypatch.setattr(app_module.ToolOutputLimits, "from_config", lambda value: object())
    monkeypatch.setattr(app_module, "UserStoreManager", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        app_module,
        "build_operation_validation_service",
        lambda **_kwargs: expected_service,
    )

    runtime = app_module.build_runtime_services()

    assert runtime["solver_validation_service"] is expected_service


def test_refresh_preserves_active_session_store_instances_and_terminal(
    monkeypatch,
    tmp_path,
):
    from workspace.user_store_manager import UserStoreManager

    workspace_runtime = tmp_path / "workspace-users"
    workspace_runtime.mkdir()
    monkeypatch.setattr(
        "workspace.user_store_manager.get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(
            users_runtime_dir=workspace_runtime,
        ),
    )
    manager = UserStoreManager(
        str(tmp_path / "chats"),
        str(tmp_path / "executions"),
    )
    chat_store = manager.get_chat_store("user-refresh", "workspace-refresh")
    execution_store = manager.get_execution_store(
        "user-refresh",
        "workspace-refresh",
    )
    execution_store.start_turn(
        "session-refresh",
        1,
        "run-refresh",
        "Synthetic active request",
    )
    config = SimpleNamespace(
        auth_server_url="https://auth.example.net",
        auth_server_admin_token="synthetic-admin-token",
        auth_server_audience="synthetic-audience",
        auth_server_issuer="https://auth.example.net",
        chat_storage_dir=tmp_path / "new-chat",
        execution_storage_dir=tmp_path / "new-execution",
        project_root=tmp_path,
    )
    agent_config = SimpleNamespace(limits=object())
    application = SimpleNamespace(
        state=SimpleNamespace(
            services={
                "LLM_ROUTER": object(),
                "USER_STORE_MANAGER": manager,
            },
            runtime_container=None,
            solver_recovery_worker=None,
        )
    )
    dependency_managers = []
    monkeypatch.setattr(app_module.AppConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        app_module.AgentConfig,
        "from_env",
        lambda: agent_config,
    )
    monkeypatch.setattr(
        app_module,
        "LLMRouter",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(app_module, "configure_llm_router", lambda _router: None)
    monkeypatch.setattr(
        app_module,
        "RemoteAuthBackend",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        app_module.ToolOutputLimits,
        "from_config",
        lambda _limits: None,
    )
    monkeypatch.setattr(
        app_module,
        "build_solver_coordination_service",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        app_module,
        "build_operation_validation_service",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        app_module,
        "configure_solver_tool_service",
        lambda _service: None,
    )
    monkeypatch.setattr(
        app_module,
        "_install_solver_recovery_worker",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        app_module,
        "update_runtime_dependencies",
        lambda store_manager, **_kwargs: dependency_managers.append(
            store_manager
        ),
    )
    monkeypatch.setattr(app_module, "reset_observer", lambda **_kwargs: None)

    refreshed = app_module.refresh_runtime_services(application)
    current_manager = application.state.services["USER_STORE_MANAGER"]
    current_chat_store = current_manager.get_chat_store(
        "user-refresh",
        "workspace-refresh",
    )
    current_execution_store = current_manager.get_execution_store(
        "user-refresh",
        "workspace-refresh",
    )
    current_execution_store.complete_turn(
        "session-refresh",
        1,
        {"answer": "completed after refresh"},
        "completed",
        run_id="run-refresh",
    )
    completed_turn = current_execution_store.get_turn(
        "session-refresh",
        1,
    )

    assert refreshed["user_store_manager"] is manager
    assert current_manager is manager
    assert current_chat_store is chat_store
    assert current_execution_store is execution_store
    assert dependency_managers == [manager]
    assert completed_turn["status"] == "success"
    assert completed_turn["final_output"] == {
        "answer": "completed after refresh"
    }


def test_shutdown_stops_worker_enabled_after_startup(monkeypatch):
    runtime = _runtime_bundle(object())
    runtime["solver_coordination_service"] = None
    _stub_lifespan(monkeypatch, runtime)
    workers = []

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.running = False
            workers.append(self)

        def start(self):
            self.running = True
            return self

        def stop(self, *, timeout_seconds=5.0):
            del timeout_seconds
            self.running = False
            return True

    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        FakeRecoveryWorker,
    )
    monkeypatch.setattr(
        app_module,
        "recover_solver_coordination_jobs",
        lambda _service: (),
        raising=False,
    )
    application = app_module.create_app()

    with TestClient(application):
        worker = app_module._install_solver_recovery_worker(
            application,
            object(),
        )
        assert worker.running is True

    assert workers[-1].running is False
    assert application.state.solver_recovery_worker is None


def test_shutdown_accepts_worker_disabled_after_startup(monkeypatch):
    runtime = _runtime_bundle(object())
    runtime["solver_coordination_service"] = object()
    _stub_lifespan(monkeypatch, runtime)

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.running = False

        def start(self):
            self.running = True
            return self

        def stop(self, *, timeout_seconds=5.0):
            del timeout_seconds
            self.running = False
            return True

    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        FakeRecoveryWorker,
    )
    monkeypatch.setattr(
        app_module,
        "recover_solver_coordination_jobs",
        lambda _service: (),
        raising=False,
    )
    application = app_module.create_app()

    with TestClient(application):
        worker = application.state.solver_recovery_worker
        assert worker.running is True
        assert app_module._install_solver_recovery_worker(
            application,
            None,
        ) is None
        assert worker.running is False

    assert application.state.solver_recovery_worker is None


def test_install_refuses_to_replace_unterminated_worker(monkeypatch):
    application = SimpleNamespace(
        state=SimpleNamespace(solver_recovery_worker=None)
    )
    constructed_services = []

    class BlockedRecoveryWorker:
        running = True

        def stop(self, *, timeout_seconds=5.0):
            del timeout_seconds
            return False

    class ReplacementRecoveryWorker:
        def __init__(self, service):
            constructed_services.append(service)

    previous = BlockedRecoveryWorker()
    application.state.solver_recovery_worker = previous
    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        ReplacementRecoveryWorker,
    )

    with pytest.raises(RuntimeError, match="did not stop before replacement"):
        app_module._install_solver_recovery_worker(
            application,
            object(),
        )

    assert application.state.solver_recovery_worker is previous
    assert constructed_services == []


def test_shutdown_retains_ownership_of_unterminated_worker():
    application = SimpleNamespace(
        state=SimpleNamespace(solver_recovery_worker=None)
    )

    class BlockedRecoveryWorker:
        running = True

        def stop(self, *, timeout_seconds):
            assert timeout_seconds == 0.01
            return False

    worker = BlockedRecoveryWorker()
    application.state.solver_recovery_worker = worker

    with pytest.raises(RuntimeError, match="did not stop during shutdown"):
        app_module._stop_solver_recovery_worker(
            application,
            timeout_seconds=0.01,
        )

    assert application.state.solver_recovery_worker is worker


def test_blocked_solver_recovery_does_not_delay_lifespan_readiness(
    monkeypatch,
):
    from integration import solver_coordination_runtime

    runtime = _runtime_bundle(object())
    runtime["solver_coordination_service"] = object()
    _stub_lifespan(monkeypatch, runtime)
    recovery_entered = threading.Event()
    release_recovery = threading.Event()

    def blocking_recovery(_service):
        recovery_entered.set()
        release_recovery.wait(timeout=0.5)
        return ()

    monkeypatch.setattr(
        app_module,
        "recover_solver_coordination_jobs",
        blocking_recovery,
        raising=False,
    )
    monkeypatch.setattr(
        solver_coordination_runtime,
        "recover_solver_coordination_jobs",
        blocking_recovery,
    )
    application = app_module.create_app()
    started_at = time.perf_counter()

    try:
        with TestClient(application):
            ready_elapsed = time.perf_counter() - started_at
            assert recovery_entered.wait(timeout=1)
            assert ready_elapsed < 0.3
            release_recovery.set()
    finally:
        release_recovery.set()


def test_refresh_rolls_back_services_when_worker_start_fails(monkeypatch):
    manager = object()
    old_solver_service = object()
    new_solver_service = object()
    old_services = {
        "LLM_ROUTER": object(),
        "USER_STORE_MANAGER": manager,
        "AGENT_CONFIG": object(),
        "SOLVER_COORDINATION_SERVICE": old_solver_service,
    }
    refreshed = _runtime_bundle(object())
    refreshed["user_store_manager"] = manager
    refreshed["solver_coordination_service"] = new_solver_service
    application = SimpleNamespace(
        state=SimpleNamespace(
            services=old_services,
            runtime_container=None,
            solver_recovery_worker=None,
        )
    )
    workers = []

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.running = False
            workers.append(self)

        def start(self):
            if self.service is new_solver_service:
                raise RuntimeError("synthetic worker start failure")
            self.running = True
            return self

        def stop(self, timeout_seconds=None):
            del timeout_seconds
            self.running = False
            return True

    old_worker = FakeRecoveryWorker(old_solver_service).start()
    application.state.solver_recovery_worker = old_worker
    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda **_kwargs: refreshed,
    )
    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        FakeRecoveryWorker,
    )
    monkeypatch.setattr(app_module, "configure_llm_router", lambda _router: None)
    monkeypatch.setattr(
        app_module,
        "configure_solver_tool_service",
        lambda _service: None,
    )
    monkeypatch.setattr(
        app_module,
        "recover_solver_coordination_jobs",
        lambda _service: (),
        raising=False,
    )
    monkeypatch.setattr(
        app_module,
        "update_runtime_dependencies",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(app_module, "reset_observer", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="synthetic worker start failure"):
        app_module.refresh_runtime_services(application)

    current_worker = application.state.solver_recovery_worker
    assert application.state.services is old_services
    assert current_worker.service is old_solver_service
    assert current_worker.running is True
    assert all(
        not worker.running
        for worker in workers
        if worker is not current_worker
    )


def test_refresh_rolls_back_worker_when_runtime_update_fails(monkeypatch):
    manager = object()
    old_solver_service = object()
    new_solver_service = object()
    old_agent_config = object()
    new_agent_config = object()
    old_services = {
        "LLM_ROUTER": object(),
        "USER_STORE_MANAGER": manager,
        "AGENT_CONFIG": old_agent_config,
        "SOLVER_COORDINATION_SERVICE": old_solver_service,
    }
    refreshed = _runtime_bundle(object())
    refreshed["user_store_manager"] = manager
    refreshed["agent_config"] = new_agent_config
    refreshed["solver_coordination_service"] = new_solver_service
    application = SimpleNamespace(
        state=SimpleNamespace(
            services=old_services,
            runtime_container=None,
            solver_recovery_worker=None,
        )
    )
    workers = []
    dependency_configs = []

    class FakeRecoveryWorker:
        def __init__(self, service):
            self.service = service
            self.running = False
            workers.append(self)

        def start(self):
            self.running = True
            return self

        def stop(self, timeout_seconds=None):
            del timeout_seconds
            self.running = False
            return True

    old_worker = FakeRecoveryWorker(old_solver_service).start()
    application.state.solver_recovery_worker = old_worker
    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda **_kwargs: refreshed,
    )
    monkeypatch.setattr(
        app_module,
        "SolverCoordinationRecoveryWorker",
        FakeRecoveryWorker,
    )
    monkeypatch.setattr(app_module, "configure_llm_router", lambda _router: None)
    monkeypatch.setattr(
        app_module,
        "configure_solver_tool_service",
        lambda _service: None,
    )
    monkeypatch.setattr(
        app_module,
        "recover_solver_coordination_jobs",
        lambda _service: (),
        raising=False,
    )

    def update_runtime(_manager, *, agent_config):
        dependency_configs.append(agent_config)
        if agent_config is new_agent_config:
            raise RuntimeError("synthetic runtime update failure")
        return SimpleNamespace()

    monkeypatch.setattr(
        app_module,
        "update_runtime_dependencies",
        update_runtime,
    )
    monkeypatch.setattr(app_module, "reset_observer", lambda **_kwargs: None)

    with pytest.raises(RuntimeError, match="synthetic runtime update failure"):
        app_module.refresh_runtime_services(application)

    current_worker = application.state.solver_recovery_worker
    assert application.state.services is old_services
    assert current_worker.service is old_solver_service
    assert current_worker.running is True
    assert dependency_configs == [new_agent_config, old_agent_config]
    assert all(
        not worker.running
        for worker in workers
        if worker is not current_worker
    )


def test_create_app_injects_validation_service(monkeypatch):
    expected_service = object()
    runtime = _runtime_bundle(expected_service)
    _stub_lifespan(monkeypatch, runtime)

    application = app_module.create_app()

    with TestClient(application):
        assert (
            application.state.services[
                "SOLVER_COORDINATION_VALIDATION_SERVICE"
            ]
            is expected_service
        )

    assert application.state.services == {}


def test_refresh_replaces_validation_service_only_after_success(monkeypatch):
    old_service = object()
    new_service = object()
    application = SimpleNamespace(
        state=SimpleNamespace(
            services={
                "LLM_ROUTER": object(),
                "SOLVER_COORDINATION_VALIDATION_SERVICE": old_service,
            },
            runtime_container=None,
            solver_recovery_worker=None,
        )
    )
    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda **kwargs: _runtime_bundle(new_service),
    )
    monkeypatch.setattr(app_module, "update_runtime_dependencies", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "configure_solver_tool_service", lambda _service: None)
    monkeypatch.setattr(app_module, "_install_solver_recovery_worker", lambda *args: None)
    monkeypatch.setattr(app_module, "reset_observer", lambda **kwargs: None)

    app_module.refresh_runtime_services(application)
    assert (
        application.state.services["SOLVER_COORDINATION_VALIDATION_SERVICE"]
        is new_service
    )

    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic build failure")),
    )
    with pytest.raises(RuntimeError, match="synthetic build failure"):
        app_module.refresh_runtime_services(application)

    assert (
        application.state.services["SOLVER_COORDINATION_VALIDATION_SERVICE"]
        is new_service
    )
