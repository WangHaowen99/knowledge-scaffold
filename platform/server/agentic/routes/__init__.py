"""Flask routes for the agentic API."""

from typing import Optional

from flask import request

from ..config import AgentConfig
from ..orchestrator import AgenticOrchestrator
from ..stores import UserStoreManager
from ..services.auth import get_current_user
from ..utils.logging import get_logger
from ..workspaces import resolve_workspace_id

from .workflows import workflows_bp
from .sessions import sessions_bp
from .ontologies import ontologies_bp
from .operations import operations_bp
from .skills import skills_bp
from .system_config import system_config_bp
from .workspaces import workspaces_bp
from .business_data import business_data_bp
from .a2a import a2a_bp
from .performance import performance_bp
from ..acp.server import acp_bp, set_orchestrator as acp_set_orchestrator

logger = get_logger("routes")

# Global orchestrator instance (initialized by register_routes)
_orchestrator: Optional[AgenticOrchestrator] = None
_user_store_manager: Optional[UserStoreManager] = None


def register_routes(
    app,
    user_store_manager: Optional[UserStoreManager] = None,
    agent_config: Optional[AgentConfig] = None,
):
    """
    Register agentic routes with the Flask app.

    Args:
        app: Flask application
        user_store_manager: Optional user store manager
        agent_config: Optional agent configuration
    """
    global _orchestrator, _user_store_manager
    _user_store_manager = user_store_manager

    # Initialize orchestrator
    _orchestrator = AgenticOrchestrator(
        user_store_manager=user_store_manager,
        agent_config=agent_config,
    )

    # Register blueprints
    app.register_blueprint(workflows_bp)
    app.register_blueprint(sessions_bp)
    app.register_blueprint(ontologies_bp)
    app.register_blueprint(operations_bp)
    app.register_blueprint(skills_bp)
    app.register_blueprint(system_config_bp)
    app.register_blueprint(workspaces_bp)
    app.register_blueprint(business_data_bp)
    app.register_blueprint(a2a_bp)
    app.register_blueprint(performance_bp)
    app.register_blueprint(acp_bp)

    acp_set_orchestrator(_orchestrator)

    logger.info("Agentic routes registered")


def update_runtime_dependencies(
    user_store_manager: Optional[UserStoreManager] = None,
    agent_config: Optional[AgentConfig] = None,
):
    """Refresh shared runtime dependencies after config changes."""
    global _orchestrator, _user_store_manager
    previous_run_store = _orchestrator.run_store if _orchestrator is not None else None
    _user_store_manager = user_store_manager
    _orchestrator = AgenticOrchestrator(
        user_store_manager=user_store_manager,
        agent_config=agent_config,
        run_store=previous_run_store,
    )
    acp_set_orchestrator(_orchestrator)


def get_orchestrator() -> AgenticOrchestrator:
    """Get the orchestrator instance."""
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized. Call register_routes first.")
    return _orchestrator


def get_request_workspace_id() -> str:
    """Resolve the current request workspace."""
    return resolve_workspace_id(request)


def get_request_stores():
    """Get chat and execution stores for the current user and workspace."""
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
