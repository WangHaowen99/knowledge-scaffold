"""Flask application factory for the agentic system."""

from flask import Flask
from flask_cors import CORS

from config import AgentConfig, AppConfig
from agent.core.warmup import start_runtime_warmup
from integration.observability import reset_observer
from integration.observability.otel import initialize_open_telemetry
from integration.platform.manager import reset_platform_observer, start_platform_observer_if_enabled
from migrations.runner import run_startup_migrations
from api.routes import register_routes, update_runtime_dependencies
from api.routes.auth import auth_bp
from api.auth.remote_auth import RemoteAuthBackend
from llm import LLMRouter, configure_llm_router
from workspace.user_store_manager import UserStoreManager
from persistence.chat_store import ToolOutputLimits
from server_config import MAX_CONTENT_LENGTH
from ontology.directory_replacement import recover_workspace_ontology_replacements
from workspace.workspaces import get_workspaces_root


def build_runtime_services(previous_llm_router=None):
    """Build runtime services from the latest persisted config."""
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


def refresh_runtime_services(app: Flask):
    """Refresh app-scoped services after config changes."""
    previous_llm_router = app.config.get("LLM_ROUTER")
    runtime = build_runtime_services(previous_llm_router=previous_llm_router)
    app.config["AUTH_MANAGER"] = runtime["auth_manager"]
    app.config["USER_STORE_MANAGER"] = runtime["user_store_manager"]
    app.config["LLM_ROUTER"] = runtime["llm_router"]
    app.config["APP_CONFIG"] = runtime["config"]
    app.config["AGENT_CONFIG"] = runtime["agent_config"]
    update_runtime_dependencies(
        runtime["user_store_manager"],
        agent_config=runtime["agent_config"],
    )
    reset_observer(timeout=1.0)
    return runtime


def refresh_platform_connector() -> None:
    reset_platform_observer(timeout=1.0, restart=True)
    reset_observer(timeout=1.0)


def create_app() -> Flask:
    """Create and configure the Flask app."""
    run_startup_migrations()
    recover_workspace_ontology_replacements(get_workspaces_root())

    app = Flask(__name__)
    initialize_open_telemetry(app)
    app.json.ensure_ascii = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
    CORS(
        app,
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-Workspace-Id", "traceparent"],
    )
    app.config["REFRESH_RUNTIME_SERVICES"] = refresh_runtime_services
    app.config["REFRESH_PLATFORM_CONNECTOR"] = refresh_platform_connector

    # Auth belongs to the application shell and remains available when domain
    # routes are stubbed in focused tests.
    app.register_blueprint(auth_bp)

    runtime = build_runtime_services()
    app.config["AUTH_MANAGER"] = runtime["auth_manager"]
    app.config["USER_STORE_MANAGER"] = runtime["user_store_manager"]
    app.config["LLM_ROUTER"] = runtime["llm_router"]
    app.config["APP_CONFIG"] = runtime["config"]
    app.config["AGENT_CONFIG"] = runtime["agent_config"]

    register_routes(
        app,
        runtime["user_store_manager"],
        agent_config=runtime["agent_config"],
    )
    start_runtime_warmup(agent_config=runtime["agent_config"])
    start_platform_observer_if_enabled()

    @app.route("/health", methods=["GET"])
    def health():
        return {"status": "ok"}

    return app
