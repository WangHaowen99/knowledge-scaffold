"""FastAPI application factory for the Consumer backend."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from agent.core.warmup import start_runtime_warmup, stop_runtime_warmup
from agent.runtime import NativeAsyncRuntimeConfig, RuntimeContainer
from agent.runtime.factory import components_factory
from agent.skill_evolution.registry import (
    init_skill_evolution_worker,
    reset_skill_evolution_singletons,
)
from agent.tools.solver import configure_solver_tool_service
from api.auth.remote_auth import RemoteAuthBackend
from api.exceptions import APIError, api_error_handler
from api.middleware import install_http_middleware
from api.routes import (
    get_orchestrator,
    initialize_runtime_dependencies,
    register_routers,
    set_agent_execution_runtime,
    update_runtime_dependencies,
)
from api.routes.auth import auth_router
from config import AgentConfig, AppConfig
from integration.observability import reset_observer, shutdown_observer
from integration.observability.otel import initialize_open_telemetry
from integration.platform.manager import (
    reset_platform_observer,
    shutdown_platform_observer,
    start_platform_observer_if_enabled,
)
from integration.solver_coordination_runtime import (
    SolverCoordinationRecoveryWorker,
    build_solver_coordination_service,
)
from integration.solver_operation_runtime import ControlledScriptOperationRuntime
from llm import LLMRouter, configure_llm_router
from migrations.runner import run_startup_migrations
from ontology.directory_replacement import recover_workspace_ontology_replacements
from ontology.evidence.worker import refresh_evidence_index_worker
from persistence.chat_store import ToolOutputLimits
from solver_coordination.runtime import build_operation_validation_service
from utils.logging import get_logger
from workspace.user_store_manager import UserStoreManager
from workspace.workspaces import get_workspaces_root


logger = get_logger("app")
_solver_recovery_worker_lock = threading.RLock()
_runtime_refresh_lock = threading.RLock()


async def _run_session_recovery_batches(
    manager: UserStoreManager,
    stop_event: threading.Event,
) -> dict[str, int | bool]:
    """Drain bounded recovery batches without delaying application readiness."""

    outcome: dict[str, int | bool] = {
        "processed": 0,
        "recovered": 0,
        "failures": 0,
        "remaining": 0,
        "complete": True,
    }
    total_failures = 0
    try:
        while not stop_event.is_set():
            outcome = await asyncio.to_thread(
                manager.recover_pending_sessions_batch,
                max_items=25,
                max_seconds=0.25,
                stop_event=stop_event,
            )
            failures = int(outcome.get("failures") or 0)
            total_failures += failures
            if outcome.get("complete"):
                break
            await asyncio.sleep(0)
        if total_failures:
            raise RuntimeError(
                "session recovery completed with {} failure(s)".format(
                    total_failures
                )
            )
    finally:
        close_discovery = getattr(manager, "close_recovery_discovery", None)
        if callable(close_discovery):
            drained = await asyncio.to_thread(close_discovery, wait_seconds=0.25)
            if drained is False:
                raise RuntimeError("session recovery workers did not drain")
    return outcome


async def _stop_session_recovery(
    task: asyncio.Task | None,
    stop_event: threading.Event,
    *,
    timeout: float,
) -> bool:
    stop_event.set()
    if task is None:
        return True
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.01, timeout),
        )
        return True
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_session_recovery_result)
        return False
    except asyncio.CancelledError:
        return True
    except Exception:
        logger.exception("Session recovery task failed")
        return False


def _consume_session_recovery_result(task: asyncio.Future[Any]) -> None:
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


def install_solver_recovery_worker(app: FastAPI, solver_service):
    """Replace the process-local solver recovery worker owned by the app."""

    with _solver_recovery_worker_lock:
        previous = getattr(app.state, "solver_recovery_worker", None)
        stop = getattr(previous, "stop", None)
        if callable(stop):
            if stop() is not True:
                raise RuntimeError(
                    "solver recovery worker did not stop before replacement"
                )
        elif previous is not None:
            raise RuntimeError(
                "solver recovery worker cannot be stopped before replacement"
            )
        app.state.solver_recovery_worker = None
        if solver_service is None:
            return None

        candidate = None
        try:
            candidate = SolverCoordinationRecoveryWorker(solver_service)
            started = candidate.start()
            worker = started if started is not None else candidate
        except Exception:
            candidate_stop = getattr(candidate, "stop", None)
            if callable(candidate_stop):
                try:
                    candidate_stop()
                except Exception:
                    logger.exception(
                        "Failed to stop rejected solver recovery worker"
                    )
            restart = getattr(previous, "start", None)
            if callable(restart):
                try:
                    restored = restart()
                    app.state.solver_recovery_worker = (
                        restored if restored is not None else previous
                    )
                except Exception:
                    logger.exception(
                        "Failed to restore previous solver recovery worker"
                    )
            raise
        app.state.solver_recovery_worker = worker
        return worker


def stop_solver_recovery_worker(
    app: FastAPI,
    *,
    timeout_seconds: float,
) -> None:
    with _solver_recovery_worker_lock:
        worker = getattr(app.state, "solver_recovery_worker", None)
        if worker is None:
            app.state.solver_recovery_worker = None
            return
        stop = getattr(worker, "stop", None)
        if not callable(stop):
            raise RuntimeError(
                "solver recovery worker cannot be stopped during shutdown"
            )
        if stop(timeout_seconds=timeout_seconds) is not True:
            raise RuntimeError(
                "solver recovery worker did not stop during shutdown"
            )
        app.state.solver_recovery_worker = None


def build_runtime_services(
    previous_llm_router=None,
    *,
    user_store_manager: UserStoreManager | None = None,
) -> dict[str, Any]:
    """Build synchronous configuration and storage services."""
    config = AppConfig.from_env()
    if not config.auth_server_url:
        raise RuntimeError("认证需要配置 AUTH_SERVER_URL（auth server 地址）")
    agent_config = AgentConfig.from_env()
    llm_router = LLMRouter(
        config,
        agent_config=agent_config,
        previous_router=previous_llm_router,
    )
    auth_manager = RemoteAuthBackend(
        base_url=config.auth_server_url,
        admin_token=config.auth_server_admin_token,
        audience=config.auth_server_audience,
        issuer=config.auth_server_issuer,
    )
    if user_store_manager is None:
        user_store_manager = UserStoreManager(
            config.chat_storage_dir,
            config.execution_storage_dir,
            tool_output_limits=ToolOutputLimits.from_config(
                agent_config.limits
            ),
        )
    solver_coordination_service = build_solver_coordination_service(
        runtime_root=getattr(config, "project_root", config.chat_storage_dir)
    )
    return {
        "config": config,
        "agent_config": agent_config,
        "llm_router": llm_router,
        "auth_manager": auth_manager,
        "user_store_manager": user_store_manager,
        "solver_validation_service": build_operation_validation_service(
            operation_runtime=ControlledScriptOperationRuntime()
        ),
        "solver_coordination_service": solver_coordination_service,
    }


def _service_mapping(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "AUTH_MANAGER": runtime["auth_manager"],
        "USER_STORE_MANAGER": runtime["user_store_manager"],
        "LLM_ROUTER": runtime["llm_router"],
        "APP_CONFIG": runtime["config"],
        "AGENT_CONFIG": runtime["agent_config"],
        "SOLVER_COORDINATION_VALIDATION_SERVICE": runtime.get(
            "solver_validation_service"
        ),
        "SOLVER_COORDINATION_SERVICE": runtime.get(
            "solver_coordination_service"
        ),
        "REFRESH_RUNTIME_SERVICES": refresh_runtime_services,
        "REFRESH_PLATFORM_CONNECTOR": refresh_platform_connector,
    }


def refresh_runtime_services(app: FastAPI) -> dict[str, Any]:
    """Refresh mutable configuration without replacing the active Run owner."""

    with _runtime_refresh_lock:
        previous_services = getattr(app.state, "services", {})
        previous_manager = previous_services.get("USER_STORE_MANAGER")
        previous_router = previous_services.get("LLM_ROUTER")
        previous_agent_config = previous_services.get("AGENT_CONFIG")
        previous_solver_service = previous_services.get(
            "SOLVER_COORDINATION_SERVICE"
        )
        refreshed = build_runtime_services(
            previous_llm_router=previous_router,
            user_store_manager=previous_manager,
        )
        solver_service = refreshed.get("solver_coordination_service")
        container = getattr(app.state, "runtime_container", None)
        container_runtime = getattr(container, "runtime", None)
        previous_container_agent_config = getattr(
            container_runtime,
            "agent_config",
            None,
        )
        worker_replaced = False
        try:
            configure_llm_router(refreshed["llm_router"])
            configure_solver_tool_service(solver_service)
            update_runtime_dependencies(
                refreshed["user_store_manager"],
                agent_config=refreshed["agent_config"],
            )
            if container_runtime is not None:
                container_runtime.agent_config = refreshed["agent_config"]
            install_solver_recovery_worker(app, solver_service)
            worker_replaced = True
            reset_observer(timeout=1.0)
            refresh_evidence_index_worker()
        except Exception:
            logger.exception("Runtime service refresh failed; restoring previous services")
            if previous_router is not None:
                _safe_sync_step(
                    "runtime-refresh-llm-rollback",
                    configure_llm_router,
                    previous_router,
                )
            _safe_sync_step(
                "runtime-refresh-solver-tool-rollback",
                configure_solver_tool_service,
                previous_solver_service,
            )
            if previous_manager is not None:
                _safe_sync_step(
                    "runtime-refresh-dependencies-rollback",
                    update_runtime_dependencies,
                    previous_manager,
                    agent_config=previous_agent_config,
                )
            if container_runtime is not None:
                container_runtime.agent_config = previous_container_agent_config
            if worker_replaced:
                _safe_sync_step(
                    "runtime-refresh-worker-rollback",
                    install_solver_recovery_worker,
                    app,
                    previous_solver_service,
                )
            app.state.services = previous_services
            raise
        app.state.services = _service_mapping(refreshed)
        return refreshed


def refresh_platform_connector() -> None:
    reset_platform_observer(timeout=1.0, restart=True)
    reset_observer(timeout=1.0)


def _safe_sync_step(name: str, callback: Callable[..., Any], *args, **kwargs) -> bool:
    try:
        result = callback(*args, **kwargs)
        return result is not False
    except Exception:
        logger.exception("Lifecycle step failed: %s", name)
        return False


def safe_sync_step_for_test(
    name: str,
    callback: Callable[..., Any],
    *args,
    **kwargs,
) -> bool:
    """Exercise lifecycle result normalization through a public test boundary."""
    return _safe_sync_step(name, callback, *args, **kwargs)


def create_app() -> FastAPI:
    """Create the single-worker ASGI application and its owned runtime."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_container: RuntimeContainer | None = None
        operation_patcher = None
        orchestrator = None
        platform_started = False
        skill_worker_started = False
        warmup_started = False
        session_recovery_task: asyncio.Task | None = None
        session_recovery_stop = threading.Event()
        services_installed = False
        started = False
        try:
            run_startup_migrations()
            recover_workspace_ontology_replacements(get_workspaces_root())
            services = build_runtime_services()
            configure_llm_router(services["llm_router"])
            app.state.services = _service_mapping(services)
            services_installed = True
            configure_solver_tool_service(
                services.get("solver_coordination_service")
            )
            await asyncio.to_thread(
                install_solver_recovery_worker,
                app,
                services.get("solver_coordination_service"),
            )
            orchestrator = initialize_runtime_dependencies(
                services["user_store_manager"],
                agent_config=services["agent_config"],
            )
            runtime_container = RuntimeContainer(
                config=NativeAsyncRuntimeConfig.from_agent_config(
                    services["agent_config"]
                ),
                components_factory=components_factory(lambda: orchestrator),
            )
            await runtime_container.start()
            app.state.runtime_container = runtime_container
            app.state.agent_execution_runtime = runtime_container
            set_agent_execution_runtime(runtime_container)
            recovery_method = getattr(
                services["user_store_manager"],
                "recover_pending_sessions_batch",
                None,
            )
            if callable(recovery_method):
                session_recovery_task = asyncio.create_task(
                    _run_session_recovery_batches(
                        services["user_store_manager"],
                        session_recovery_stop,
                    ),
                    name="session-summary-recovery",
                )
                app.state.session_recovery_task = session_recovery_task
            warmup_started = _safe_sync_step(
                "runtime-warmup-start",
                start_runtime_warmup,
                agent_config=services["agent_config"],
                runtime=runtime_container,
            )

            operation_patcher = getattr(orchestrator, "operation_patcher", None)
            if operation_patcher is not None:
                await operation_patcher.start()
            init_skill_evolution_worker()
            skill_worker_started = True
            # Evidence index worker follows the same master-switch lifecycle
            # (no-op when the ontology evidence feature is disabled).
            refresh_evidence_index_worker()
            start_platform_observer_if_enabled()
            platform_started = True
            started = True
            yield
        finally:
            failed: list[str] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + (
                runtime_container.config.shutdown_total_deadline_seconds
                if runtime_container is not None
                else 30.0
            )
            if not await _stop_session_recovery(
                session_recovery_task,
                session_recovery_stop,
                timeout=min(2.0, max(0.01, deadline - loop.time())),
            ):
                failed.append("session-recovery")
            app.state.session_recovery_task = None
            close_summary_rebuilds = getattr(
                services.get("user_store_manager") if services_installed else None,
                "close_summary_rebuilds",
                None,
            )
            if callable(close_summary_rebuilds):
                try:
                    rebuilds_closed = await asyncio.to_thread(
                        close_summary_rebuilds,
                        wait_seconds=min(
                            2.0,
                            max(0.0, deadline - loop.time()),
                        ),
                    )
                    if rebuilds_closed is False:
                        failed.append("session-summary-rebuild")
                except Exception:
                    logger.exception("Session summary rebuild shutdown failed")
                    failed.append("session-summary-rebuild")
            try:
                await asyncio.to_thread(
                    stop_solver_recovery_worker,
                    app,
                    timeout_seconds=min(
                        5.0,
                        max(0.0, deadline - loop.time()),
                    ),
                )
            except Exception:
                logger.exception("Solver recovery worker shutdown failed")
                failed.append("solver-recovery")
            configure_solver_tool_service(None)
            if operation_patcher is not None:
                try:
                    if not await operation_patcher.close(deadline=deadline):
                        failed.append("operation-patcher")
                except Exception:
                    logger.exception("Operation patcher shutdown failed")
                    failed.append("operation-patcher")
            reflection_worker = getattr(orchestrator, "reflection_worker", None)
            if reflection_worker is not None:
                try:
                    if not await reflection_worker.close(deadline=deadline):
                        failed.append("reflection-worker")
                except Exception:
                    logger.exception("Reflection worker shutdown failed")
                    failed.append("reflection-worker")
            if warmup_started:
                try:
                    if not await stop_runtime_warmup(deadline=deadline):
                        failed.append("runtime-warmup")
                except Exception:
                    logger.exception("Runtime warm-up shutdown failed")
                    failed.append("runtime-warmup")
            if runtime_container is not None:
                try:
                    if not await runtime_container.close(deadline=deadline):
                        failed.append("runtime-container")
                except Exception:
                    logger.exception("Runtime container shutdown failed")
                    failed.append("runtime-container")
            if skill_worker_started and not _safe_sync_step(
                "skill-evolution",
                reset_skill_evolution_singletons,
            ):
                failed.append("skill-evolution")
            remaining = max(0.0, deadline - loop.time())
            if not _safe_sync_step(
                "observer",
                shutdown_observer,
                timeout=min(5.0, remaining),
            ):
                failed.append("observer")
            if platform_started and not _safe_sync_step(
                "platform-observer",
                shutdown_platform_observer,
                timeout=min(5.0, max(0.0, deadline - loop.time())),
            ):
                failed.append("platform-observer")
            set_agent_execution_runtime(None)
            app.state.runtime_container = None
            app.state.agent_execution_runtime = None
            if services_installed:
                app.state.services = {}
            if failed and started:
                raise RuntimeError(
                    "Application shutdown incomplete: {}".format(
                        ", ".join(failed)
                    )
                )

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = {}
    app.state.runtime_container = None
    app.state.solver_recovery_worker = None
    app.state.session_recovery_task = None
    install_http_middleware(app)
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(auth_router)
    register_routers(app)
    initialize_open_telemetry(app)

    @app.get("/health")
    async def health():
        container = app.state.runtime_container
        readiness = (
            container.readiness()
            if container is not None
            else {"ready": False, "state": "CREATED"}
        )
        return {
            "status": "ok" if readiness["state"] != "STOPPED" else "stopped",
            "runtime": readiness,
        }

    @app.head("/health", include_in_schema=False)
    async def health_head():
        return Response(status_code=200)

    @app.get("/ready")
    async def ready():
        container = app.state.runtime_container
        readiness = (
            container.readiness()
            if container is not None
            else {"ready": False, "state": "CREATED"}
        )
        return JSONResponse(
            readiness,
            status_code=200 if readiness["ready"] else 503,
        )

    @app.get("/health/runtime")
    async def runtime_metrics():
        container = app.state.runtime_container
        if container is None:
            return JSONResponse(
                {"ready": False, "state": "CREATED"},
                status_code=503,
            )
        return container.metrics()

    return app
