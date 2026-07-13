"""Flask application factory for the agentic system."""

from flask import Flask, jsonify, request
from flask_cors import CORS

from config import AgentConfig, AppConfig
from agent.tools.business_data.ontology_sources_store import migrate_legacy_ontology_data_sources_for_all_workspaces
from integration.observability import reset_observer
from integration.observability.migration import migrate_global_observability_to_workspaces
from integration.platform.manager import reset_platform_observer, start_platform_observer_if_enabled
from api.routes import register_routes, update_runtime_dependencies
from api.auth.auth import (
    AuthManager,
    PasswordPolicyError,
    require_auth,
    require_role,
    get_auth_manager,
    get_current_user,
)
from llm import LLMRouter, configure_llm_router
from workspace.user_store_manager import UserStoreManager
from utils.logging import get_logger

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
    auth_manager = AuthManager(
        config.auth_users_file,
        config.auth_secret,
        config.auth_token_ttl_seconds,
    )
    user_store_manager = UserStoreManager(
        config.chat_storage_dir,
        config.execution_storage_dir,
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


def _run_startup_observability_migration() -> None:
    try:
        result = migrate_global_observability_to_workspaces()
    except Exception:
        logger.warning("Workspace observability migration failed during startup", exc_info=True)
        return

    errors = result.get("errors") if isinstance(result, dict) else None
    if errors:
        logger.warning("Workspace observability migration completed with warnings: %s", errors)


def _run_startup_ontology_data_source_migration() -> None:
    try:
        result = migrate_legacy_ontology_data_sources_for_all_workspaces()
    except Exception:
        logger.warning("Ontology data source migration failed during startup", exc_info=True)
        return

    errors = result.get("errors") if isinstance(result, dict) else None
    if errors:
        logger.warning("Ontology data source migration completed with warnings: %s", errors)


def create_app() -> Flask:
    """Create and configure the Flask app."""
    _run_startup_observability_migration()
    _run_startup_ontology_data_source_migration()

    app = Flask(__name__)
    app.json.ensure_ascii = False
    CORS(app, supports_credentials=True, allow_headers=["Content-Type", "Authorization", "X-Workspace-Id"])
    app.config["REFRESH_RUNTIME_SERVICES"] = refresh_runtime_services
    app.config["REFRESH_PLATFORM_CONNECTOR"] = refresh_platform_connector

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
    start_platform_observer_if_enabled()

    @app.route("/health", methods=["GET"])
    def health():
        return {"status": "ok"}

    @app.route("/auth/login", methods=["POST"])
    def login():
        data = request.get_json() or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        user = auth_manager.authenticate(username, password)
        if not user:
            return jsonify({"error": "Invalid credentials"}), 401
        token = auth_manager.issue_token(user)
        return jsonify({"token": token, "user": user.to_public_dict()})

    @app.route("/auth/me", methods=["GET"])
    @require_auth()
    def me():
        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        return jsonify({"user": user.to_public_dict()})

    @app.route("/auth/users", methods=["GET"])
    @require_role("admin")
    def list_users():
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        return jsonify({"users": auth_manager.list_users()})

    @app.route("/auth/users", methods=["POST"])
    @require_role("admin")
    def create_user():
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        data = request.get_json() or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        try:
            user = auth_manager.create_user(username, password)
            return jsonify({"user": user.to_public_dict()}), 201
        except PasswordPolicyError as exc:
            return jsonify({
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            }), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/auth/users/<username>", methods=["PUT"])
    @require_role("admin")
    def update_user(username):
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        data = request.get_json() or {}
        password = data.get("password")
        if password is not None:
            password = str(password)
        current = get_current_user()
        if password is not None and current and current.username == username:
            return jsonify({"error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口"}), 400
        try:
            user = auth_manager.update_user(username, password=password)
            return jsonify({"user": user.to_public_dict()})
        except PasswordPolicyError as exc:
            return jsonify({
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            }), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/auth/users/<username>", methods=["DELETE"])
    @require_role("admin")
    def delete_user(username):
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        current = get_current_user()
        if current and current.username == username:
            return jsonify({"error": "不能删除自己"}), 400
        if username == "admin":
            return jsonify({"error": "不能删除 admin 管理员"}), 400
        try:
            auth_manager.delete_user(username)
            return jsonify({"success": True})
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/auth/change-password", methods=["PUT"])
    @require_auth()
    def change_password():
        auth_manager = get_auth_manager()
        if not auth_manager:
            return jsonify({"error": "Auth not configured"}), 500
        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json() or {}
        old_password = str(data.get("old_password", ""))
        new_password = str(data.get("new_password", ""))
        if not old_password or not new_password:
            return jsonify({"error": "原密码和新密码不能为空"}), 400
        try:
            auth_manager.change_password(user.username, old_password, new_password)
            return jsonify({
                "success": True,
                "requires_relogin": True,
            })
        except PasswordPolicyError as exc:
            return jsonify({
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            }), 400
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return app
