"""Authentication and user management endpoints (extracted from app.py, behavior unchanged).

注册相关端点是对独立 auth server 的薄代理（BFF）：consumer 自身不实现注册，
仅在配置了 AUTH_SERVER_URL 时转发。注册一步直接建号（无邮箱验证码），
见 docs/decisions/2026-07-10-open-registration-no-email-verification.md。
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.auth.auth import (
    AuthUser,
    PasswordPolicyError,
    get_auth_manager,
    require_admin_user,
    require_authenticated_user,
)
from api.auth.user_deletion import (
    UserDeletionReconciliationError,
    UserDeletionPendingError,
    create_identity_if_reusable,
    delete_user_with_cleanup,
    normalize_username,
)
from api.dependencies import run_in_runtime_pool
from api.request_parsing import read_json_object, read_optional_json_object
from workspace.workspaces import WorkspaceOwnershipConflictError

auth_router = APIRouter(prefix="/auth")

# 连接 3s / 读取 10s
_REGISTER_PROXY_TIMEOUT = (3, 10)
REFRESH_COOKIE_NAME = "onto_refresh_token"


def _first_forwarded_value(request: Request, name: str) -> str:
    value = request.headers.get(name, "")
    return value.split(",", 1)[0].strip()


def _request_scheme(request: Request) -> str:
    # The frontend reverse proxy overwrites this header with the browser-facing
    # scheme. Backend deployments must keep this header within that trusted hop.
    forwarded = _first_forwarded_value(request, "x-forwarded-proto").lower()
    return forwarded if forwarded in {"http", "https"} else request.url.scheme.lower()


def _request_origin(request: Request) -> str:
    host = _first_forwarded_value(request, "x-forwarded-host") or request.headers.get("host", "")
    return f"{_request_scheme(request)}://{host}".rstrip("/").lower()


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return f"{parsed.scheme}://{parsed.netloc}".lower() == _request_origin(request)


def _origin_error(request: Request):
    if _same_origin(request):
        return None
    return JSONResponse({"error": "跨站认证请求被拒绝"}, status_code=403)


def _secure_cookie(request: Request) -> bool:
    return _request_scheme(request) == "https"


def _set_refresh_cookie(response: JSONResponse, request: Request, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path="/auth",
    )


def _auth_server_url(request: Request) -> str:
    config = getattr(request.app.state, "services", {}).get("APP_CONFIG")
    return str(getattr(config, "auth_server_url", "") or "").strip().rstrip("/")


def _auth_server_endpoint(base: str, path: str) -> str:
    return urljoin(f"{base.rstrip('/')}/", path.lstrip("/"))


def _proxy_register(request: Request, path: str, payload: dict):
    base = _auth_server_url(request)
    if not base:
        return JSONResponse({"error": "注册功能未开启"}, status_code=503)
    try:
        upstream = requests.post(
            _auth_server_endpoint(base, path),
            json=payload,
            # auth server 按客户端 IP 限流，转发时必须带上真实来源 IP
            headers={
                "X-Forwarded-For": (
                    request.client.host if request.client is not None else "unknown"
                )
            },
            timeout=_REGISTER_PROXY_TIMEOUT,
        )
    except requests.RequestException:
        return JSONResponse(
            {"error": "注册服务暂不可用，请稍后再试"},
            status_code=502,
        )
    try:
        payload = upstream.json()
    except ValueError:
        payload = {"error": "注册服务响应异常"}
    return JSONResponse(payload, status_code=upstream.status_code)


@auth_router.get("/capabilities")
async def capabilities(request: Request):
    """登录页能力探测；上游不可用时安全地隐藏可选注册入口。"""
    return await run_in_runtime_pool(
        request,
        "external_io",
        _get_capabilities,
        request,
    )


def _get_capabilities(request: Request):
    base = _auth_server_url(request)
    if not base:
        return {"registration_enabled": False}
    try:
        upstream = requests.get(
            _auth_server_endpoint(base, "/capabilities"),
            timeout=_REGISTER_PROXY_TIMEOUT,
        )
        payload = upstream.json() if upstream.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        payload = {}
    return {"registration_enabled": payload.get("registration_enabled") is True}


def _register_identity(request: Request, payload: dict):
    username = normalize_username(payload.get("username"))
    if not username:
        return _proxy_register(request, "/register", payload)
    try:
        return create_identity_if_reusable(
            username,
            lambda: _proxy_register(request, "/register", payload),
        )
    except UserDeletionPendingError as exc:
        return _user_deletion_pending_response(exc)
    except UserDeletionReconciliationError:
        return JSONResponse(
            {
                "error": "用户删除状态暂不可用，请稍后再试",
                "code": "USER_DELETION_STATE_UNAVAILABLE",
            },
            status_code=503,
        )


@auth_router.post("/register")
async def register(request: Request):
    payload = await read_optional_json_object(request)
    return await run_in_runtime_pool(
        request,
        "external_io",
        _register_identity,
        request,
        payload,
    )


@auth_router.post("/login")
async def login(request: Request):
    rejected = _origin_error(request)
    if rejected:
        return rejected
    data = await read_json_object(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    try:
        result = await run_in_runtime_pool(
            request,
            "external_io",
            auth_manager.login,
            username,
            password,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    if not result:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)
    payload = {"token": result.access_token, "user": result.user.to_public_dict()}
    if result.expires_in is not None:
        payload["expires_in"] = result.expires_in
    response = JSONResponse(payload)
    if result.refresh_token is not None:
        _set_refresh_cookie(response, request, result.refresh_token)
    return response


@auth_router.post("/refresh")
async def refresh(request: Request):
    """方案 a：前端 authFetch 401 时静默调此端点换新 access token（仅 remote 模式）。"""
    auth_manager = get_auth_manager(request)
    if not auth_manager or not hasattr(auth_manager, "refresh_tokens"):
        return JSONResponse(
            {"error": "当前认证模式不支持刷新"},
            status_code=503,
        )
    rejected = _origin_error(request)
    if rejected:
        return rejected
    refresh_token = str(request.cookies.get(REFRESH_COOKIE_NAME, "")).strip()
    if not refresh_token:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        tokens = await run_in_runtime_pool(
            request,
            "external_io",
            auth_manager.refresh_tokens,
            refresh_token,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    if not tokens:
        # 上游拒绝（过期/已吊销）→ 401，前端据此登出
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    response = JSONResponse({
        "token": tokens["access_token"],
        "expires_in": tokens["expires_in"],
    })
    _set_refresh_cookie(response, request, tokens["refresh_token"])
    return response


@auth_router.post("/logout")
async def logout(request: Request):
    """Revoke the refresh token before confirming logout."""
    auth_manager = get_auth_manager(request)
    if not auth_manager or not hasattr(auth_manager, "revoke_refresh_token"):
        return JSONResponse(
            {"error": "当前认证模式不支持登出吊销"},
            status_code=503,
        )
    rejected = _origin_error(request)
    if rejected:
        return rejected
    refresh_token = str(request.cookies.get(REFRESH_COOKIE_NAME, "")).strip()
    if not refresh_token:
        response = JSONResponse({"success": True})
        response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth")
        return response
    try:
        await run_in_runtime_pool(
            request,
            "external_io",
            auth_manager.revoke_refresh_token,
            refresh_token,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    response = JSONResponse({"success": True})
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/auth")
    return response


@auth_router.get("/me")
def me(user: AuthUser = Depends(require_authenticated_user)):
    return {"user": user.to_public_dict()}


@auth_router.get("/users")
async def list_users(
    request: Request,
    _user: AuthUser = Depends(require_admin_user),
):
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    users = await run_in_runtime_pool(
        request,
        "external_io",
        auth_manager.list_users,
    )
    return {"users": users}


def _create_user(auth_manager, username: str, password: str, role: str):
    return create_identity_if_reusable(
        username,
        lambda: auth_manager.create_user(username, password, role=role),
    )


@auth_router.post("/users")
async def create_user(
    request: Request,
    _user: AuthUser = Depends(require_admin_user),
):
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    data = await read_json_object(request)
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role") or "user")
    try:
        user = await run_in_runtime_pool(
            request,
            "external_io",
            _create_user,
            auth_manager,
            username,
            password,
            role,
        )
        return JSONResponse(
            {"user": user.to_public_dict()},
            status_code=201,
        )
    except UserDeletionPendingError as exc:
        return _user_deletion_pending_response(exc)
    except UserDeletionReconciliationError:
        return JSONResponse(
            {
                "error": "用户删除状态暂不可用，请稍后再试",
                "code": "USER_DELETION_STATE_UNAVAILABLE",
            },
            status_code=503,
        )
    except PasswordPolicyError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            },
            status_code=400,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@auth_router.put("/users/{username}")
async def update_user(
    username: str,
    request: Request,
    current: AuthUser = Depends(require_admin_user),
):
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    username = normalize_username(username)
    data = await read_json_object(request)
    password = data.get("password")
    if password is not None:
        password = str(password)
    role = data.get("role")
    if role is not None:
        role = str(role)
    if password is not None and current.username == username:
        return JSONResponse(
            {"error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口"},
            status_code=400,
        )
    try:
        user = await run_in_runtime_pool(
            request,
            "external_io",
            auth_manager.update_user,
            username,
            password=password,
            role=role,
        )
        return {"user": user.to_public_dict()}
    except PasswordPolicyError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            },
            status_code=400,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@auth_router.delete("/users/{username}")
async def delete_user(
    username: str,
    request: Request,
    current: AuthUser = Depends(require_admin_user),
):
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    username = normalize_username(username)
    if current.username == username:
        return JSONResponse({"error": "不能删除自己"}, status_code=400)
    if username == "admin":
        return JSONResponse({"error": "不能删除 admin 管理员"}, status_code=400)
    try:
        result = await run_in_runtime_pool(
            request,
            "external_io",
            delete_user_with_cleanup,
            auth_manager,
            username,
        )
        if result.pending_reconciliation:
            return JSONResponse(
                {
                    "success": True,
                    "reconciliation_required": True,
                    "reconciliation_id": result.reconciliation_id,
                },
                status_code=202,
            )
        return {"success": True}
    except UserDeletionPendingError as exc:
        return _user_deletion_pending_response(exc)
    except WorkspaceOwnershipConflictError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "code": "WORKSPACE_OWNER_TRANSFER_REQUIRED",
                "workspace_ids": exc.workspace_ids,
            },
            status_code=409,
        )
    except UserDeletionReconciliationError as exc:
        payload = {
            "error": str(exc),
            "code": "USER_DELETION_RECONCILIATION_FAILED",
        }
        if exc.reconciliation_id:
            payload["reconciliation_required"] = True
            payload["reconciliation_id"] = exc.reconciliation_id
        return JSONResponse(payload, status_code=500)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _user_deletion_pending_response(exc: UserDeletionPendingError):
    return JSONResponse(
        {
            "error": str(exc),
            "code": "USER_DELETION_RECONCILIATION_PENDING",
            "reconciliation_id": exc.reconciliation_id,
        },
        status_code=409,
    )


@auth_router.put("/change-password")
async def change_password(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
):
    auth_manager = get_auth_manager(request)
    if not auth_manager:
        return JSONResponse({"error": "Auth not configured"}, status_code=500)
    data = await read_json_object(request)
    old_password = str(data.get("old_password", ""))
    new_password = str(data.get("new_password", ""))
    if not old_password or not new_password:
        return JSONResponse(
            {"error": "原密码和新密码不能为空"},
            status_code=400,
        )
    try:
        await run_in_runtime_pool(
            request,
            "external_io",
            auth_manager.change_password,
            user.username,
            old_password,
            new_password,
        )
        return {
            "success": True,
            "requires_relogin": True,
        }
    except PasswordPolicyError as exc:
        return JSONResponse(
            {
                "error": str(exc),
                "code": "PASSWORD_POLICY_FAILED",
                "violations": exc.violations,
            },
            status_code=400,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
