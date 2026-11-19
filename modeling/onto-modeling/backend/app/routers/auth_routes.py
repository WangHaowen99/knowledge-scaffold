"""客户端配置与认证（API 契约 3.1 C-01、3.2 A-01～A-05）：代理 auth-server（OAuth2 密码模式子集）。

/api/v1/auth/ 与 /api/v1/client-config 免鉴权（契约 1.2 第 6 条）；退出登录在免鉴权前缀下，
处理器自己认 access token 取用户名。
"""

from __future__ import annotations

import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import config
from ..api import deps
from ..api.errors import ApiError
from ..services import audit, auth, uploads
from ..services.auth import AuthError
from ..services.packs.manifest import PAGE_API  # 页面接口版本只有这一个来源（ADR-0105 升到 1.1）

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["auth"])

# 上传上限与资料类型白名单的唯一来源在暂存上传区（契约 1.8）
LIMITS = uploads.LIMITS
MATERIAL_TYPES = uploads.MATERIAL_TYPES
# 金额币种：模型单价表（方案 B14）落地前，费用是引擎 SDK 报告的美元数
BILLING_CURRENCY = "USD"

# 最近一次成功登录（内存态，重启清零），A-07 的 last_login 用
LAST_LOGIN: dict[str, dict] = {}


def drawio_dir() -> Path:
    """drawio 编辑器静态页目录（官方 webapp 静态部署）。"""
    return Path(
        os.environ.get("ONTO_WEB_DRAWIO_DIR", str(config.PACKAGE_ROOT / "vendor" / "drawio" / "webapp"))
    )


def drawio_available() -> bool:
    return (drawio_dir() / "index.html").is_file()


def _maintenance_state() -> dict | None:
    """C-01 的维护状态（MaintenanceState，时间转 RFC 3339），供前端横幅。"""
    from ..governance import ops_state

    state = ops_state.maintenance_view()
    if not state:
        return None
    return {**state, "started_at": deps.iso(state.get("started_at")), "until": deps.iso(state.get("until"))}


def user_profile(claims: dict | None) -> dict:
    """UserProfile（契约 7.2）。本地模式是 local，同时拥有三种角色，role 取 admin。"""
    if claims is None:
        return {
            "username": deps.LOCAL_USER,
            "display_name": "本地用户",
            "role": "admin",
            "role_label": deps.ROLE_LABELS["admin"],
            "roles": ["user", "admin", "ops"],
            "auth_source": "local",
        }
    username = str(claims.get("sub") or "")
    role = claims.get("role") if claims.get("role") in deps.ROLE_LABELS else "user"
    return {
        "username": username,
        "display_name": str(claims.get("name") or username),
        "role": role,
        "role_label": deps.ROLE_LABELS[role],
        "roles": sorted({"user", role}),
        "auth_source": "auth-server",
    }


def _claims_of(access_token: str, username: str) -> dict:
    """刚从 auth-server 换来的 access token 的 claims，只用来回显用户资料，不用于鉴权。"""
    claims = auth.verify_access_token(access_token)
    if claims:
        return claims
    try:
        claims = jwt.decode(access_token, options={"verify_signature": False})
    except jwt.PyJWTError:
        claims = {}
    return claims if claims.get("sub") else {"sub": username}


def _token_response(tokens: dict, username: str, status: int = 200) -> JSONResponse:
    claims = _claims_of(str(tokens.get("access_token") or ""), username)
    content = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type") or "Bearer",
        "expires_in": tokens.get("expires_in"),
        "user": user_profile(claims),
    }
    return JSONResponse(status_code=status, content=content)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _require_auth_enabled() -> None:
    if not auth.ENABLED:
        raise ApiError("not_found", "本地模式不需要登录", 404)


def _upstream(exc: AuthError) -> ApiError:
    return ApiError("upstream_unavailable", str(exc) or "认证服务不可达", 502, {"upstream": "auth_server"})


# ---- C-01 ----


@router.get("/client-config")
def client_config(request: Request):
    """C-01：前端启动时取一次。维护模式与独立运维监听属于以后阶段，先给 null 与空串。"""
    from .admin import registration_enabled

    return {
        "api_version": "1",
        "server_version": request.app.version,
        "page_api": PAGE_API,
        "auth": {
            "enabled": auth.ENABLED,
            "registration_enabled": registration_enabled() if auth.ENABLED else False,
            "local_user": None if auth.ENABLED else deps.LOCAL_USER,
        },
        # 运维管理面地址（契约 1.1）：空串表示同源，由反代把 /api/v1/ops/ 转到运维监听
        "ops_base_url": config.OPS_BASE_URL,
        "maintenance": _maintenance_state(),
        "limits": dict(LIMITS),
        "material_types": list(MATERIAL_TYPES),
        "features": {"drawio": drawio_available()},
        "billing": {"currency": BILLING_CURRENCY},
    }


# ---- A-01～A-05 ----


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


@router.post("/auth/login")
def login(body: LoginBody, request: Request):
    """A-01：成功和失败都记审计，不记密码。"""
    _require_auth_enabled()
    try:
        tokens = auth.token_request(
            {"grant_type": "password", "username": body.username, "password": body.password}
        )
    except AuthError as exc:
        audit.log(body.username, "POST", request.url.path, {"action": "auth.login", "result": "failed"})
        if exc.status == 401:
            raise ApiError("invalid_credentials", "用户名或密码不正确", 401) from exc
        raise _upstream(exc) from exc
    audit.log(body.username, "POST", request.url.path, {"action": "auth.login", "result": "ok"})
    LAST_LOGIN[body.username] = {"at": time.time(), "ip": _client_ip(request)}
    return _token_response(tokens, body.username)


@router.post("/auth/local")
def enter_local_mode():
    """A-02：本地模式进入，此后前端不带令牌请求。鉴权开启时不存在（404）。"""
    if auth.ENABLED:
        raise ApiError("not_found", "接口不存在", 404)
    return {"user": user_profile(None)}


class RegisterBody(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


def _register_error(status: int, data: dict) -> ApiError:
    from .admin import auth_admin_error

    message = str(data.get("error") or "")
    if status == 503:
        return ApiError("registration_disabled", "注册未开启，请联系管理员开通账号", 403)
    if status == 429:
        return ApiError("rate_limited", "请求过于频繁，请稍后再试", 429, {"retry_after_s": None})
    if status == 400 and "已存在" in message:
        return ApiError("username_taken", "用户名已存在", 409)
    return auth_admin_error(status, data)


@router.post("/auth/register")
def register(body: RegisterBody, request: Request):
    """A-03：注册即登录。邮箱由服务端合成，界面不收集（auth-server 要求唯一邮箱）。"""
    if not auth.ENABLED:
        raise ApiError("registration_disabled", "本地模式不需要注册", 403)
    payload = {
        "username": body.username,
        "email": f"{body.username}@users.example.com",
        "password": body.password,
    }
    try:
        status, data = auth.register_request(payload)
    except AuthError as exc:
        raise _upstream(exc) from exc
    if not 200 <= status < 300:
        raise _register_error(status, data if isinstance(data, dict) else {})
    audit.log(body.username, "POST", request.url.path, {"action": "auth.register"})
    try:
        tokens = auth.token_request(
            {"grant_type": "password", "username": body.username, "password": body.password}
        )
    except AuthError as exc:
        raise _upstream(exc) from exc
    LAST_LOGIN[body.username] = {"at": time.time(), "ip": _client_ip(request)}
    return _token_response(tokens, body.username, status=201)


class RefreshBody(BaseModel):
    refresh_token: str = Field(min_length=1)


@router.post("/auth/refresh")
def refresh(body: RefreshBody):
    """A-04：换新令牌。刷新令牌无效或过期返回 refresh_invalid，前端回登录页。"""
    _require_auth_enabled()
    try:
        tokens = auth.token_request({"grant_type": "refresh_token", "refresh_token": body.refresh_token})
    except AuthError as exc:
        if exc.status == 401:
            raise ApiError("refresh_invalid", "登录已失效，请重新登录", 401) from exc
        raise _upstream(exc) from exc
    return _token_response(tokens, "")


class LogoutBody(BaseModel):
    refresh_token: str | None = None


def revoke_refresh_token(refresh_token: str) -> bool:
    """auth-server /revoke（RFC 7009）。它按用户整体吊销（token_version+1），其他设备同时退出。"""
    data = urllib.parse.urlencode({"token": refresh_token}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{auth.AUTH_URL}/revoke", data=data), timeout=10):
            return True
    except OSError as exc:
        _log.warning("退出登录时吊销刷新令牌失败：%s", exc)
        return False


@router.post("/auth/logout", status_code=204)
def logout(request: Request, body: LogoutBody | None = None):
    """A-05：吊销刷新令牌（auth-server 支持），响应 204。用户事件流（A-12）还没有，不用关流。"""
    username = ""
    header = request.headers.get("authorization", "")
    if auth.ENABLED and header.lower().startswith("bearer "):
        username = str((auth.verify_access_token(header[7:].strip()) or {}).get("sub") or "")
    revoked = False
    if auth.ENABLED and body and body.refresh_token:
        revoked = revoke_refresh_token(body.refresh_token)
    if username or revoked:
        audit.log(username or "anonymous", "POST", request.url.path, {"action": "auth.logout", "revoked": revoked})
    return Response(status_code=204)


def change_password_upstream(username: str, old_password: str, new_password: str) -> None:
    """自助改密（A-06）：auth-server 管理接口先用旧密码认证，再按密码规则（含不能与旧密码相同）改密。"""
    from .admin import _auth_admin_request, auth_admin_error

    status, data = _auth_admin_request(
        "PUT", "/admin/change-password",
        {"username": username, "old_password": old_password, "new_password": new_password},
    )
    if not 200 <= status < 300:
        raise auth_admin_error(status, data if isinstance(data, dict) else {})


def last_login(username: str) -> dict | None:
    entry = LAST_LOGIN.get(username)
    return {"at": deps.iso(entry["at"]), "ip": entry.get("ip")} if entry else None
