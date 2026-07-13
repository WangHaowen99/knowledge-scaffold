"""Authentication and user management endpoints (extracted from app.py, behavior unchanged).

注册相关端点是对独立 auth server 的薄代理（BFF）：consumer 自身不实现注册，
仅在配置了 AUTH_SERVER_URL 时转发。注册一步直接建号（无邮箱验证码），
见 docs/decisions/2026-07-10-open-registration-no-email-verification.md。
"""

from __future__ import annotations

import requests
from flask import Blueprint, current_app, jsonify, request

from ..services.auth import (
    PasswordPolicyError,
    get_auth_manager,
    get_current_user,
    require_auth,
    require_role,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

# 连接 3s / 读取 10s
_REGISTER_PROXY_TIMEOUT = (3, 10)


def _auth_server_url() -> str:
    config = current_app.config.get("APP_CONFIG")
    return str(getattr(config, "auth_server_url", "") or "").strip().rstrip("/")


def _proxy_register(path: str):
    base = _auth_server_url()
    if not base:
        return jsonify({"error": "注册功能未开启"}), 503
    try:
        upstream = requests.post(
            base + path,
            json=request.get_json(silent=True) or {},
            # auth server 按客户端 IP 限流，转发时必须带上真实来源 IP
            headers={"X-Forwarded-For": request.remote_addr or "unknown"},
            timeout=_REGISTER_PROXY_TIMEOUT,
        )
    except requests.RequestException:
        return jsonify({"error": "注册服务暂不可用，请稍后再试"}), 502
    try:
        payload = upstream.json()
    except ValueError:
        payload = {"error": "注册服务响应异常"}
    return jsonify(payload), upstream.status_code


@auth_bp.route("/capabilities", methods=["GET"])
def capabilities():
    """登录页能力探测；上游不可用时安全地隐藏可选注册入口。"""
    base = _auth_server_url()
    if not base:
        return jsonify({"registration_enabled": False})
    try:
        upstream = requests.get(base + "/capabilities", timeout=_REGISTER_PROXY_TIMEOUT)
        payload = upstream.json() if upstream.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        payload = {}
    return jsonify({"registration_enabled": payload.get("registration_enabled") is True})


@auth_bp.route("/register", methods=["POST"])
def register():
    return _proxy_register("/register")


@auth_bp.route("/login", methods=["POST"])
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
    payload = {"token": token, "user": user.to_public_dict()}
    # remote 模式在冻结契约 {token, user} 之上加法追加 refresh_token/expires_in
    # （agent_eval/SDK 只读 token 不受影响）；local 后端无 pop_login_tokens，响应不变
    if hasattr(auth_manager, "pop_login_tokens"):
        login_tokens = auth_manager.pop_login_tokens(user.username)
        if login_tokens:
            payload["refresh_token"] = login_tokens.get("refresh_token")
            payload["expires_in"] = login_tokens.get("expires_in")
    return jsonify(payload)


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    """方案 a：前端 authFetch 401 时静默调此端点换新 access token（仅 remote 模式）。"""
    auth_manager = get_auth_manager()
    if not auth_manager or not hasattr(auth_manager, "refresh_tokens"):
        return jsonify({"error": "当前认证模式不支持刷新"}), 503
    data = request.get_json(silent=True) or {}
    refresh_token = str(data.get("refresh_token", "")).strip()
    try:
        tokens = auth_manager.refresh_tokens(refresh_token)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 503
    if not tokens:
        # 上游拒绝（过期/已吊销）→ 401，前端据此登出
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({
        "token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_in": tokens["expires_in"],
    })


@auth_bp.route("/me", methods=["GET"])
@require_auth()
def me():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"user": user.to_public_dict()})


@auth_bp.route("/users", methods=["GET"])
@require_role("admin")
def list_users():
    auth_manager = get_auth_manager()
    if not auth_manager:
        return jsonify({"error": "Auth not configured"}), 500
    return jsonify({"users": auth_manager.list_users()})


@auth_bp.route("/users", methods=["POST"])
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


@auth_bp.route("/users/<username>", methods=["PUT"])
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


@auth_bp.route("/users/<username>", methods=["DELETE"])
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


@auth_bp.route("/change-password", methods=["PUT"])
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
