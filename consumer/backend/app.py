"""FastAPI application factory for the Consumer backend."""

from __future__ import annotations

import asyncio
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
from integration.platform.manager import (
    reset_platform_observer,
    shutdown_platform_observer,
    start_platform_observer_if_enabled,
)
from llm import LLMRouter, configure_llm_router
from migrations.runner import run_startup_migrations
from persistence.chat_store import ToolOutputLimits
from utils.logging import get_logger
from workspace.user_store_manager import UserStoreManager


logger = get_logger("app")


def build_runtime_services(previous_llm_router=None) -> dict[str, Any]:
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
    configure_llm_router(llm_router)
    auth_manager = RemoteAuthBackend(
        base_url=config.auth_server_url,
        admin_token=config.auth_server_admin_token,
        audience=config.auth_server_audience,
        issuer=config.auth_server_issuer,
    )
    user_store_manager = UserStoreManager(
        config.chat_storage_dir,
        config.execution_storage_dir,
        tool_output_limits=ToolOutputLimits.from_config(agent_config.limits),
    )
    return {
        "config": config,
        "agent_config": agent_config,
        "llm_router": llm_router,
        "auth_manager": auth_manager,
        "user_store_manager": user_store_manager,
    }


def _service_mapping(runtime: dict[str, Any]) -> dict[str, Any]:
    return {
        "AUTH_MANAGER": runtime["auth_manager"],
        "USER_STORE_MANAGER": runtime["user_store_manager"],
        "LLM_ROUTER": runtime["llm_router"],
        "APP_CONFIG": runtime["config"],
        "AGENT_CONFIG": runtime["agent_config"],
        "REFRESH_RUNTIME_SERVICES": refresh_runtime_services,
        "REFRESH_PLATFORM_CONNECTOR": refresh_platform_connector,
    }


def refresh_runtime_services(app: FastAPI) -> dict[str, Any]:
    """Refresh mutable configuration without replacing the active Run owner."""

    services = getattr(app.state, "services", {})
    refreshed = build_runtime_services(
        previous_llm_router=services.get("LLM_ROUTER")
    )
    app.state.services = _service_mapping(refreshed)
    orchestrator = update_runtime_dependencies(
        refreshed["user_store_manager"],
        agent_config=refreshed["agent_config"],
    )
    container = getattr(app.state, "runtime_container", None)
    if container is not None and container.runtime is not None:
        container.runtime.agent_config = refreshed["agent_config"]
    reset_observer(timeout=1.0)
    return refreshed


def refresh_platform_connector() -> None:
    reset_platform_observer(timeout=1.0, restart=True)
    reset_observer(timeout=1.0)


def _safe_sync_step(name: str, callback: Callable[..., Any], *args, **kwargs) -> bool:
    try:
        callback(*args, **kwargs)
        return True
    except Exception:
        logger.exception("Lifecycle step failed: %s", name)
        return False


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
        services_installed = False
        started = False
        try:
            run_startup_migrations()
            services = build_runtime_services()
            app.state.services = _service_mapping(services)
            services_installed = True
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
    install_http_middleware(app)
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(auth_router)
    register_routers(app)

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
