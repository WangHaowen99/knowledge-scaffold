"""Flask application factory for the agentic system."""

from flask import Flask
from flask_cors import CORS

from .config import AgentConfig, AppConfig
from .observability import reset_observer
from .routes import register_routes, update_runtime_dependencies
from .routes.auth import auth_bp
from .services.auth import AuthManager
from .services.llm import LLMRouter, configure_llm_router
from .stores import UserStoreManager
from .utils.logging import get_logger
from .warmup import start_runtime_warmup

logger = get_logger("app")


def build_runtime_services(previous_llm_router=None):
    """Build runtime services from the latest persisted config."""
    config = AppConfig.from_env()
    agent_config = AgentConfig.from_env()
    llm_router = LLMRouter(
        config,
        agent_config=agent_config,
        previous_router=previous_llm_router,
    )
    configure_llm_router(llm_router)
    if config.auth_mode == "local":
        auth_manager = AuthManager(
            config.auth_users_file,
            config.auth_secret,
            config.auth_token_ttl_seconds,
        )
    elif config.auth_mode == "remote":
        # 施工步骤 4 接入 RemoteAuthBackend（对接独立 auth server）后开放
        raise RuntimeError("AUTH_MODE=remote 尚未实现，请使用 local（默认）")
    else:
        raise RuntimeError(f"未知 AUTH_MODE：{config.auth_mode}（支持 local）")
    user_store_manager = UserStoreManager()
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


def create_app() -> Flask:
    """Create and configure the Flask app."""
    app = Flask(__name__)
    app.json.ensure_ascii = False
    CORS(app, supports_credentials=True, allow_headers=["Content-Type", "Authorization", "X-Workspace-Id"])
    app.config["REFRESH_RUNTIME_SERVICES"] = refresh_runtime_services

    # 认证是应用外壳设施而非 agentic 域路由：在此注册，保证 create_app()
    # 单独构造（如测试打桩 register_routes）时 /auth/* 仍可用
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

    @app.route("/health", methods=["GET"])
    def health():
        return {"status": "ok"}

    return app
