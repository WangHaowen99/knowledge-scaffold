"""管理员接口（API 契约 3.24 AD-01～AD-07）：用户管理（代理 auth-server Admin API）、项目所有者。

用户管理的写操作只有 admin；ops 能看用户列表（契约 1.3）。auth-server 现在只有 admin、user
两种角色（ops 角色属于 E3/P1），改成 ops 时由 auth-server 拒绝。
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from .. import config
from ..api import deps
from ..api.deps import workspaces  # noqa: F401  旧测试仍 monkeypatch admin.workspaces，路由一律用 deps.workspaces
from ..api.errors import ApiError
from ..api.views import project_meta_view
from ..services import audit, auth, ownership

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

ONLINE_WINDOW_SECONDS = 600
MAX_SIZE_SCAN_FILES = 20000
ROLES = ("user", "admin", "ops")

# auth-server 密码策略的违规项 → 契约 weak_password 的 failed_rules（2.2）
PASSWORD_RULES = {
    "min_length": "len",
    "max_length": "max",
    "character_classes": "classes",
    "contains_username": "contains_username",
    "common_password": "common",  # NOSONAR(S2068) 策略违规项的名字映射，不是口令
    "surrounding_whitespace": "edge_space",
    "same_as_old": "same_as_old",
}


def workspace_size(root: Path) -> int:
    """工作区占用（不含 .git），文件数超过上限时返回已统计的部分。"""
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for filename in filenames:
            count += 1
            if count > MAX_SIZE_SCAN_FILES:
                return total
            try:
                total += (Path(dirpath) / filename).stat().st_size
            except OSError:
                continue
    return total


# ---- 代理 auth-server Admin API ----


def auth_admin_available() -> bool:
    return bool(config.AUTH_ADMIN_TOKEN and auth.ENABLED)


def _auth_admin_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    """调 auth-server 管理接口，返回 (状态码, 响应体)；没配管理令牌或不可达时抛契约错误。"""
    if not auth_admin_available():
        raise ApiError("user_admin_unavailable", "未配置认证服务管理令牌，用户管理不可用", 503)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{auth.AUTH_URL}{path}",
        data=data,
        method=method,
        headers={
            # auth-server 管理 API 以 X-Admin-Token 头鉴权
            "X-Admin-Token": config.AUTH_ADMIN_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        return exc.code, body if isinstance(body, dict) else {}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError("upstream_unavailable", "认证服务不可达", 502, {"upstream": "auth_server"}) from exc


def auth_admin_error(status: int, data: dict) -> ApiError:
    """auth-server 的错误响应 → 契约错误码（auth-server 只给中文消息，按消息归类）。"""
    message = str(data.get("error") or "认证服务返回异常")
    if data.get("code") == "PASSWORD_POLICY_FAILED":
        rules = [PASSWORD_RULES.get(item, item) for item in data.get("violations") or []]
        return ApiError("weak_password", "密码不满足规则", 400, {"failed_rules": rules})
    if status in (401, 503):
        # 401 是管理令牌不被认（两边配置不一致），对调用方同样是用户管理不可用
        return ApiError("user_admin_unavailable", "认证服务管理接口不可用", 503)
    if status == 429:
        return ApiError("rate_limited", "请求过于频繁，请稍后再试", 429, {"retry_after_s": None})
    if status >= 500:
        return ApiError("upstream_unavailable", "认证服务异常", 502, {"upstream": "auth_server"})
    if "不存在" in message:
        return ApiError("user_not_found", message, 404)
    if "已存在" in message:
        return ApiError("username_taken", message, 409)
    if message == "原密码错误":
        return ApiError("old_password_incorrect", "旧密码不正确", 400)
    return ApiError("invalid_argument", message, 400)


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    status, data = _auth_admin_request(method, path, payload)
    if not 200 <= status < 300:
        raise auth_admin_error(status, data)
    return data


def list_auth_users() -> list[dict]:
    users = _call("GET", "/admin/users").get("users")
    return [user for user in users or [] if isinstance(user, dict) and user.get("username")]


def _auth_user(username: str) -> dict:
    for user in list_auth_users():
        if user["username"] == username:
            return user
    raise ApiError("user_not_found", f"用户 {username} 不存在", 404)


def admin_user_view(user: dict, project_counts: dict[str, int], now: float | None = None) -> dict:
    """AdminUser（契约 7.2）：auth-server 没有显示名和锁定状态，显示名取用户名，锁定给 null。"""
    now = time.time() if now is None else now
    username = user["username"]
    last_seen = auth.LAST_SEEN.get(username)
    return {
        "username": username,
        "display_name": user.get("display_name") or username,
        "role": user.get("role") or "user",
        "online": bool(last_seen) and now - last_seen < ONLINE_WINDOW_SECONDS,
        "last_seen_at": deps.iso(last_seen),
        "project_count": project_counts.get(username, 0),
        "locked_until": None,
    }


def project_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for meta in deps.workspaces.list_projects():
        owner = str(meta.get("owner") or "")
        if owner:
            counts[owner] = counts.get(owner, 0) + 1
    return counts


_registration_cache: tuple[float, bool | None] | None = None
REGISTRATION_CACHE_SECONDS = 60


def registration_enabled() -> bool | None:
    """auth-server 是否开放注册（/capabilities，公开接口），缓存 60 秒；读不到为 None。

    C-01 每次打开页面都会调，auth-server 不可达时不能每次都等满超时。
    """
    global _registration_cache
    if not auth.ENABLED:
        return False
    if _registration_cache and time.time() - _registration_cache[0] < REGISTRATION_CACHE_SECONDS:
        return _registration_cache[1]
    try:
        with urllib.request.urlopen(f"{auth.AUTH_URL}/capabilities", timeout=3) as response:
            value = json.loads(response.read() or b"{}").get("registration_enabled")
    except (ValueError, OSError):
        value = None
    value = value if isinstance(value, bool) else None
    _registration_cache = (time.time(), value)
    return value


def _owned_projects(username: str) -> list[dict]:
    return [meta for meta in deps.workspaces.list_projects() if str(meta.get("owner") or "") == username]


def _meta_views(metas: list[dict]) -> list[dict]:
    return [project_meta_view(meta, deps.workspaces.project_root(meta["id"])) for meta in metas]


# ---- AD-01～AD-04 用户 ----


@router.get("/users")
def list_users(request: Request, q: str = "", cursor: str = "", limit: int = 0):
    """AD-01：用户列表。没配管理令牌时 available=false、items 为空，页面据此提示。"""
    deps.require_role(request, "admin", "ops")
    stats = {
        "accounts": 0,
        "by_role": {role: 0 for role in ROLES},
        "new_7d": None,  # auth-server 不记账号创建时间
        "locked": None,  # auth-server 没有登录锁定
        "registration_enabled": registration_enabled(),
        "auth": {"alg": "RS256", "issuer_checked": True},
    }
    if not auth_admin_available():
        return {"items": [], "next_cursor": None, "total": 0, "stats": stats, "available": False}
    users = list_auth_users()
    stats["accounts"] = len(users)
    for user in users:
        role = user.get("role") or "user"
        stats["by_role"][role] = stats["by_role"].get(role, 0) + 1
    counts, now = project_counts(), time.time()
    needle = q.strip().lower()
    items = [
        admin_user_view(user, counts, now)
        for user in sorted(users, key=lambda item: item["username"])
        if not needle or needle in user["username"].lower()
    ]
    return {**deps.page(items, cursor, limit, {"q": needle}), "stats": stats, "available": True}


class RoleBody(BaseModel):
    role: str


@router.patch("/users/{username}")
def update_user_role(username: str, body: RoleBody, request: Request):
    """AD-02：改角色。不能把最后一个管理员降级。"""
    deps.require_role(request, "admin")
    if body.role not in ROLES:
        raise ApiError("invalid_argument", "角色只能是 user、admin 或 ops", 400, {"fields": {"role": "enum"}})
    users = list_auth_users()
    target = next((user for user in users if user["username"] == username), None)
    if target is None:
        raise ApiError("user_not_found", f"用户 {username} 不存在", 404)
    admins = [user for user in users if user.get("role") == "admin"]
    if target.get("role") == "admin" and body.role != "admin" and len(admins) <= 1:
        raise ApiError("forbidden", "不能降级最后一个管理员", 403, {"reason": "last_admin"})
    previous = target.get("role") or "user"
    user = _call("PUT", f"/admin/users/{username}", {"role": body.role}).get("user") or {**target, "role": body.role}
    audit.log(
        deps.actor(request), "PATCH", request.url.path,
        detail={"action": "user.role", "username": username, "from": previous, "to": body.role},
    )
    return admin_user_view(user, project_counts())


class ResetPasswordBody(BaseModel):
    password: str | None = Field(default=None, max_length=128)
    generate: bool = False
    require_change: bool = False


def generate_password(length: int = 16) -> str:
    """服务端生成的临时密码：大小写字母、数字、符号各至少一个，满足 auth-server 密码规则。"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    pools = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*-_=+")
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(ch in pool for ch in value) for pool in pools):
            return value


@router.post("/users/{username}/reset-password")
def reset_password(username: str, body: ResetPasswordBody, request: Request):
    """AD-03：重置密码。generate=true 时服务端生成，明文只在这次响应里出现一次。

    require_change（下次登录强制改密）auth-server 不支持，见 gaps。
    """
    deps.require_role(request, "admin")
    if body.generate:
        password = generate_password()
    elif body.password:
        password = body.password
    else:
        missing = {"password": "required"}  # NOSONAR(S2068) 表单校验结果：密码字段必填，不是口令
        raise ApiError("invalid_argument", "请填写新密码或选择自动生成", 400, {"fields": missing})
    _call("PUT", f"/admin/users/{username}", {"password": password})
    audit.log(
        deps.actor(request), "POST", request.url.path,
        detail={"action": "user.password_reset", "username": username, "generated": body.generate},
    )
    return {"generated_password": password} if body.generate else {}


class DeleteUserBody(BaseModel):
    projects: str | None = None  # transfer | trash；用户名下没有项目时可省略
    transfer_to: str | None = None


def revoke_mcp_tokens_of(username: str) -> int:
    """吊销该用户签发的全部 MCP token（用户删除时，契约 AD-04）。"""
    from ..services.onto_mcp import tokens

    with tokens.database(deps.workspaces) as conn:
        return conn.execute(
            "UPDATE tokens SET revoked=? WHERE subject=? AND revoked IS NULL", (time.time(), username)
        ).rowcount


@router.delete("/users/{username}")
def delete_user(username: str, request: Request, body: DeleteUserBody | None = None):
    """AD-04：删除用户。名下项目移交给另一个用户；移入回收站暂不支持（删除已改为永久删除）。"""
    deps.require_role(request, "admin")
    body = body or DeleteUserBody()
    if not auth_admin_available():
        raise ApiError("user_admin_unavailable", "未配置认证服务管理令牌，用户管理不可用", 503)
    owned = _owned_projects(username)
    target = ""
    if owned or body.projects:
        if body.projects == "trash":
            raise ApiError(
                "invalid_argument", "暂不支持把项目移入回收站，请移交给其他用户", 400,
                {"fields": {"projects": "trash_unsupported"}},
            )
        if body.projects != "transfer":
            raise ApiError(
                "invalid_argument", "请选择名下项目的去向", 400, {"fields": {"projects": "required"}},
            )
        target = (body.transfer_to or "").strip()
        if not target or target == username or not ownership.OWNER_NAME_RE.fullmatch(target):
            raise ApiError("transfer_target_invalid", "移交对象不存在或不合法", 400)
    users = {user["username"] for user in list_auth_users()}
    if username not in users:
        raise ApiError("user_not_found", f"用户 {username} 不存在", 404)
    if target and target not in users:
        raise ApiError("transfer_target_invalid", "移交对象不存在或不合法", 400)
    _call("DELETE", f"/admin/users/{username}")
    transferred = 0
    from ..services import project_events

    for meta in owned:
        deps.workspaces.set_owner(meta["id"], target)
        transferred += 1
        project_events.project_removed(username, meta["id"], "transferred")
        project_events.card_changed(meta["id"])
    revoked = revoke_mcp_tokens_of(username)
    from ..services import notifications, personal_model_settings

    personal_model_settings.delete_user(username)
    notifications.forget_user(username)
    audit.log(
        deps.actor(request), "DELETE", request.url.path,
        detail={
            "action": "user.delete", "username": username, "transfer_to": target or None,
            "transferred": transferred, "mcp_tokens_revoked": revoked,
        },
    )
    return {"deleted": username, "transferred": transferred, "trashed": 0}


@router.get("/users/{username}/projects")
def user_projects(username: str, request: Request):
    """AD-05：用户名下的项目（删除用户弹窗用），只给元数据视图。"""
    deps.require_role(request, "admin")
    return {"items": _meta_views(_owned_projects(username))}


# ---- AD-06、AD-07 项目所有者 ----


@router.get("/projects/unowned")
def unowned_projects(request: Request):
    """AD-06：无主项目，等管理员指定所有者。"""
    deps.require_role(request, "admin")
    metas = [meta for meta in deps.workspaces.list_projects() if not str(meta.get("owner") or "")]
    return {"items": _meta_views(metas)}


class OwnerBody(BaseModel):
    owner: str


@router.put("/projects/{project_id}/owner")
def set_project_owner(project_id: str, body: OwnerBody, request: Request):
    """AD-07：指定项目所有者。改成自己即可读项目内容，所以每次都记审计（原值与新值）。"""
    deps.require_role(request, "admin")
    owner = ownership.validate_owner_name(body.owner)
    previous, meta = deps.workspaces.set_owner(project_id, owner)
    audit.log(
        deps.actor(request), "PUT", request.url.path,
        detail={"action": "project.set_owner", "project": project_id, "from": previous, "to": owner},
    )
    return project_meta_view(meta, deps.workspaces.project_root(project_id))


# ---- AD-08～AD-10 审核申请 ----


@router.get("/requests")
def list_admin_requests(request: Request, kind: str = "", state: str = "", limit: int = 50, cursor: str = ""):
    """AD-08：待管理员处理的申请（数据源登记、知识提名）。"""
    from ..services import admin_requests

    deps.require_role(request, "admin")
    return admin_requests.list_requests(kind, state, limit, cursor)


@router.post("/requests/{request_id}/approve")
def approve_admin_request(request_id: str, request: Request, body: dict = Body(default={})):
    """AD-09：通过。数据源申请要带凭据（`secret`），按申请登记并授权给申请人的项目。同步路由：登记会调连接器网关。"""
    from ..services import admin_requests

    deps.require_role(request, "admin")
    view = admin_requests.approve(request_id, body, deps.actor(request))
    audit.log(deps.actor(request), "POST", request.url.path,
              detail={"action": "admin.request.approve", "request_id": request_id, "kind": view["kind"],
                      "source_id": view.get("source_id"), "requested_by": view["requested_by"]})
    return view


@router.post("/requests/{request_id}/reject")
def reject_admin_request(request_id: str, request: Request, body: dict = Body(default={})):
    """AD-10：退回，要写原因。"""
    from ..services import admin_requests

    deps.require_role(request, "admin")
    view = admin_requests.reject(request_id, body, deps.actor(request))
    audit.log(deps.actor(request), "POST", request.url.path,
              detail={"action": "admin.request.reject", "request_id": request_id, "kind": view["kind"],
                      "requested_by": view["requested_by"]})
    return view


@router.get("/config-registry")
def config_registry_view(request: Request, effect: str = "", q: str = ""):
    """SY-14：配置注册表（admin；ops 只读可看）。只读，配置项在各自的设置页修改；密钥只给引用。"""
    from ..governance import config_registry

    deps.require_role(request, "admin", "ops")
    if effect and effect not in config_registry.EFFECTS:
        raise ApiError("invalid_argument", "effect 取值不合法", 400, {"fields": {"effect": "enum"}})
    return {"precedence": list(config_registry.DEFAULT_PRECEDENCE),
            "items": config_registry.items(effect or None, q or None)}
