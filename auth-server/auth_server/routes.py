"""全部 HTTP 端点：标准协议侧 + Admin API。

Admin API 的中文错误消息与 violations 结构与 consumer 现有 /auth/users* 响应保持一致，
consumer BFF 只做透传。"依赖当前登录用户"的守卫（不能删除自己 / 不能重置当前登录用户
密码 / 不能删除 admin 管理员）留在 consumer 侧，本服务不感知调用者身份。
"""

from __future__ import annotations

import hmac
from functools import wraps
from typing import Any, Dict, Optional

from flask import Blueprint, current_app, jsonify, request

from .config import AuthServerConfig
from .policy import PasswordPolicyError
from .registration import RegistrationRateLimited, RegistrationService
from .tokens import TokenService
from .users import UserStore, UserView

bp = Blueprint("auth_server", __name__)


def _config() -> AuthServerConfig:
    return current_app.config["AUTH_SERVER_CONFIG"]


def _store() -> UserStore:
    return current_app.config["USER_STORE"]


def _tokens() -> TokenService:
    return current_app.config["TOKEN_SERVICE"]


def _registration() -> Optional[RegistrationService]:
    return current_app.config.get("REGISTRATION_SERVICE")


def _request_data() -> Dict[str, Any]:
    """/token 与 /revoke 接受 form 或 json。"""
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def _client_ip() -> str:
    """限流用客户端 IP：经 consumer BFF 转发时取 X-Forwarded-For 首跳。

    本服务只在内网供 BFF 访问，该头可信；直连时回落 remote_addr。
    """
    forwarded = str(request.headers.get("X-Forwarded-For", "")).split(",")[0].strip()
    return forwarded or request.remote_addr or "unknown"


def _invalid_grant(description: str):
    return jsonify({"error": "invalid_grant", "error_description": description}), 400


def _validate_refresh_token(refresh_token: str) -> Optional[UserView]:
    claims = _tokens().decode(refresh_token, expected_typ="refresh")
    if not claims:
        return None
    user = _store().get_user(str(claims.get("sub") or ""))
    if not user:
        return None
    if claims.get("ver") != user.token_version:
        return None
    return user


# --------------------------------------------------------------- 标准协议侧


@bp.route("/capabilities", methods=["GET"])
def capabilities():
    """公开、非敏感的登录页能力开关。"""
    return jsonify({"registration_enabled": _config().enable_registration})


@bp.route("/token", methods=["POST"])
def token():
    data = _request_data()
    grant_type = str(data.get("grant_type", "")).strip()

    if grant_type == "password":
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        # must_change_password 的用户照常发 token（拦截是 consumer 的事），
        # access token 带 pwd_cr=true 供 consumer 判断。
        user = _store().authenticate(username, password)
        if not user:
            return _invalid_grant("invalid username or password")
        return jsonify(_tokens().issue_pair(user))

    if grant_type == "refresh_token":
        user = _validate_refresh_token(str(data.get("refresh_token", "")))
        if not user:
            return _invalid_grant("invalid refresh token")
        return jsonify(_tokens().issue_pair(user))

    return jsonify({"error": "unsupported_grant_type"}), 400


@bp.route("/revoke", methods=["POST"])
def revoke():
    # RFC 7009：无论成功与否一律 200，不向调用方泄露 token 状态。
    user = _validate_refresh_token(str(_request_data().get("token", "")))
    if user:
        _store().revoke_user_tokens(user.username)
    return jsonify({})


@bp.route("/.well-known/openid-configuration", methods=["GET"])
def openid_configuration():
    base = _config().issuer.rstrip("/")
    return jsonify(
        {
            "issuer": _config().issuer,
            "token_endpoint": base + "/token",
            "revocation_endpoint": base + "/revoke",
            "jwks_uri": base + "/jwks.json",
            "userinfo_endpoint": base + "/userinfo",
            "grant_types_supported": ["password", "refresh_token"],
        }
    )


@bp.route("/jwks.json", methods=["GET"])
def jwks():
    return jsonify(current_app.config["KEY_MANAGER"].jwks())


@bp.route("/userinfo", methods=["GET"])
def userinfo():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _unauthorized()
    claims = _tokens().decode(auth_header.split(" ", 1)[1].strip(), expected_typ="access")
    if not claims:
        return _unauthorized()
    user = _store().get_user(str(claims.get("sub") or ""))
    if not user or claims.get("ver") != user.token_version:
        return _unauthorized()
    # role/password_change_required 以存储为准：token 中的 claim 只作提示不作准（决策 D3）
    return jsonify(
        {
            "sub": user.username,
            "role": user.role,
            "password_change_required": user.password_change_required,
        }
    )


def _unauthorized():
    response = jsonify({"error": "invalid_token"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    return response


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------- 开放注册


def _rate_limited():
    return jsonify({"error": "请求过于频繁，请稍后再试"}), 429


def require_registration_enabled(fn):
    """注册未开启（AUTH_ENABLE_REGISTRATION=false）时一律 503。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _registration() is None:
            return jsonify({"error": "注册功能未开启"}), 503
        return fn(*args, **kwargs)

    return wrapper


@bp.route("/register", methods=["POST"])
@require_registration_enabled
def register():
    data = _request_data()
    try:
        user = _registration().register(
            username=str(data.get("username", "")),
            email=str(data.get("email", "")),
            password=str(data.get("password", "")),
            client_ip=_client_ip(),
        )
        return jsonify({"success": True, "user": user.to_public_dict()})
    except RegistrationRateLimited:
        return _rate_limited()
    except PasswordPolicyError as exc:
        return _password_policy_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


# ------------------------------------------------------------------ Admin API


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        admin_token = _config().admin_token
        if not admin_token:
            return jsonify({"error": "Admin API disabled"}), 503
        provided = request.headers.get("X-Admin-Token", "")
        if not hmac.compare_digest(provided.encode("utf-8"), admin_token.encode("utf-8")):
            return jsonify({"error": "Unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def _password_policy_response(exc: PasswordPolicyError):
    return (
        jsonify({"error": str(exc), "code": "PASSWORD_POLICY_FAILED", "violations": exc.violations}),
        400,
    )


@bp.route("/admin/users", methods=["GET"])
@require_admin
def admin_list_users():
    # admin list 形状 = 现有契约 + email（仅注册用户有），consumer BFF 可按需透传
    return jsonify({"users": _store().list_users_admin()})


@bp.route("/admin/users", methods=["POST"])
@require_admin
def admin_create_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role") or "user")
    try:
        user = _store().create_user(username, password, role)
        return jsonify({"user": user.to_public_dict()}), 201
    except PasswordPolicyError as exc:
        return _password_policy_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/admin/users/<username>", methods=["PUT"])
@require_admin
def admin_update_user(username: str):
    data = request.get_json(silent=True) or {}
    password = data.get("password")
    if password is not None:
        password = str(password)
    role = data.get("role")
    if role is not None:
        role = str(role)
    try:
        user = _store().update_user(username, password=password, role=role)
        return jsonify({"user": user.to_public_dict()})
    except PasswordPolicyError as exc:
        return _password_policy_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/admin/users/<username>", methods=["DELETE"])
@require_admin
def admin_delete_user(username: str):
    try:
        _store().delete_user(username)
        return jsonify({"success": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/admin/change-password", methods=["PUT"])
@require_admin
def admin_change_password():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    if not old_password or not new_password:
        return jsonify({"error": "原密码和新密码不能为空"}), 400
    try:
        _store().change_password(username, old_password, new_password)
        return jsonify({"success": True})
    except PasswordPolicyError as exc:
        return _password_policy_response(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
