# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""FastAPI router assembly and native async runtime dependency wiring."""

from __future__ import annotations

from typing import Any, Optional

from agent.core.orchestrator import AgenticOrchestrator
from api.auth.auth import get_current_user
from api.protocols.acp.server import acp_router
from api.routes.a2a import a2a_router
from api.routes.business_data import business_data_router
from api.routes.errors import workspace_not_found_response
from api.routes.evidence import evidence_router
from api.routes.mcp import mcp_router
from api.routes.memories import memories_router
from api.routes.ontologies import ontologies_router
from api.routes.operations import operations_router
from api.routes.performance import performance_router
from api.routes.platform_config import platform_config_router
from api.routes.sessions import sessions_router
from api.routes.skill_evolution import skill_evolution_router
from api.routes.skills import skills_router
from api.routes.solver_coordination import (
    solver_coordination_public_router,
    solver_coordination_router,
)
from api.routes.system_config import system_config_router
from api.routes.workflows import workflows_router
from api.routes.workspaces import workspaces_router
from config import AgentConfig
from utils.logging import get_logger
from workspace.user_store_manager import UserStoreManager
from workspace.workspaces import (
    WorkspaceNotFoundError,
    get_current_workspace_id,
)

logger = get_logger("routes")

ROUTERS = (
    workflows_router,
    sessions_router,
    ontologies_router,
    operations_router,
    skills_router,
    system_config_router,
    platform_config_router,
    workspaces_router,
    business_data_router,
    a2a_router,
    performance_router,
    memories_router,
    mcp_router,
    acp_router,
    skill_evolution_router,
    evidence_router,
    solver_coordination_router,
    solver_coordination_public_router,
)

_orchestrator: Optional[AgenticOrchestrator] = None
_user_store_manager: Optional[UserStoreManager] = None
_agent_execution_runtime: Any = None


def register_routers(app) -> None:
    """Attach every Consumer router to the single FastAPI application."""
    for router in ROUTERS:
        app.include_router(router)

    async def handle_workspace_not_found(_request, exc: WorkspaceNotFoundError):
        return workspace_not_found_response(exc)

    app.add_exception_handler(WorkspaceNotFoundError, handle_workspace_not_found)
    logger.info("Agentic routers registered")


def initialize_runtime_dependencies(
    user_store_manager: Optional[UserStoreManager] = None,
    agent_config: Optional[AgentConfig] = None,
) -> AgenticOrchestrator:
    """Initialize process services during FastAPI lifespan startup."""
    global _orchestrator, _user_store_manager
    _user_store_manager = user_store_manager
    _orchestrator = AgenticOrchestrator(
        user_store_manager=user_store_manager,
        agent_config=agent_config,
    )
    return _orchestrator


def register_routes(
    app,
    user_store_manager: Optional[UserStoreManager] = None,
    agent_config: Optional[AgentConfig] = None,
) -> None:
    """Test-oriented convenience that attaches routers and initializes services."""
    register_routers(app)
    initialize_runtime_dependencies(
        user_store_manager=user_store_manager,
        agent_config=agent_config,
    )


def update_runtime_dependencies(
    user_store_manager: Optional[UserStoreManager] = None,
    agent_config: Optional[AgentConfig] = None,
) -> AgenticOrchestrator:
    """Refresh shared runtime dependencies after configuration changes."""
    global _orchestrator, _user_store_manager
    _user_store_manager = user_store_manager
    if _orchestrator is None:
        _orchestrator = AgenticOrchestrator(
            user_store_manager=user_store_manager,
            agent_config=agent_config,
        )
    else:
        _orchestrator.user_store_manager = user_store_manager
        if agent_config is not None:
            _orchestrator.agent_config = agent_config
    return _orchestrator


def get_orchestrator() -> AgenticOrchestrator:
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    return _orchestrator


def set_agent_execution_runtime(runtime) -> None:
    global _agent_execution_runtime
    _agent_execution_runtime = runtime


def get_agent_execution_runtime():
    if _agent_execution_runtime is None:
        raise RuntimeError("Agent execution runtime not initialized")
    return _agent_execution_runtime


def get_request_workspace_id() -> str:
    return get_current_workspace_id()


def get_request_stores():
    user = get_current_user()
    if not user:
        raise RuntimeError("User not resolved for request")
    if not _user_store_manager:
        raise RuntimeError("User store manager not initialized")
    workspace_id = get_request_workspace_id()
    return (
        _user_store_manager.get_chat_store(user.username, workspace_id),
        _user_store_manager.get_execution_store(user.username, workspace_id),
    )


def prune_request_sessions(*, max_sessions: int, batch_size: int = 1) -> list[str]:
    """Run the manager-owned bounded prune for the current request scope."""

    user = get_current_user()
    if not user:
        raise RuntimeError("User not resolved for request")
    if not _user_store_manager:
        raise RuntimeError("User store manager not initialized")
    return _user_store_manager.prune_sessions(
        user.username,
        get_request_workspace_id(),
        max_sessions=max_sessions,
        batch_size=batch_size,
    )


def prune_request_sessions_batch(*, max_sessions: int, batch_size: int = 25):
    """Return explicit convergence state for one manager-owned prune batch."""

    user = get_current_user()
    if not user:
        raise RuntimeError("User not resolved for request")
    if not _user_store_manager:
        raise RuntimeError("User store manager not initialized")
    return _user_store_manager.prune_sessions_batch(
        user.username,
        get_request_workspace_id(),
        max_sessions=max_sessions,
        batch_size=batch_size,
    )


def get_user_stores(username: str, workspace_id: str):
    """Resolve durable stores for non-HTTP protocol adapters."""
    if not _user_store_manager:
        raise RuntimeError("User store manager not initialized")
    normalized_user = str(username or "anonymous").strip() or "anonymous"
    normalized_workspace = str(workspace_id or "default").strip() or "default"
    return (
        _user_store_manager.get_chat_store(normalized_user, normalized_workspace),
        _user_store_manager.get_execution_store(
            normalized_user,
            normalized_workspace,
        ),
    )
