"""v1 路由共用的依赖：工作区管理器单例、当前用户、所有权守卫、分页与时间格式。

路由模块一律经 `deps.workspaces` 取管理器（调用时取属性，测试替换这一处即可）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

from .. import config
from ..services import auth, ownership
from ..services.workspace import WorkspaceManager
from .errors import ApiError

workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)

LOCAL_USER = "local"
ROLE_LABELS = {"user": "建模者", "admin": "管理员", "ops": "运维"}
PAGE_DEFAULT, PAGE_MAX = 50, 200


def user(request: Request) -> dict | None:
    """当前登录用户的 claims；鉴权关闭（本地模式）时为 None。"""
    return ownership.current_user(request)


def username(request: Request) -> str:
    return (user(request) or {}).get("sub", "")


def actor(request: Request) -> str:
    """审计与提交署名用的操作者：本地模式记 local（契约 1.3）。"""
    return username(request) or (LOCAL_USER if not auth.ENABLED else "")


def roles(request: Request) -> set[str]:
    """本地模式同时拥有 user、admin、ops 三种角色（契约 1.3）。"""
    claims = user(request)
    if claims is None:
        return {"user", "admin", "ops"}
    role = claims.get("role") or "user"
    return {"user", role}


def require_role(request: Request, *allowed: str) -> None:
    if not roles(request) & set(allowed):
        raise ApiError("forbidden", "当前账号没有该操作的权限", 403, {"required_role": allowed[0]})


def require_owner(project_id: str, request: Request) -> Path:
    """项目内容守卫：只有所有者能用（1.4），返回项目根目录。"""
    return ownership.require_owner(workspaces, project_id, request)


def require_writable(project_id: str, request: Request) -> Path:
    """写接口：所有者，且项目没有归档（1.4：已归档项目只读）。"""
    root = require_owner(project_id, request)
    if workspaces.get_project(project_id).get("archived"):
        raise ApiError("project_archived", "项目已归档，只能查看", 409)
    return root


# ---- 时间 ----


def iso(value) -> str | None:
    """epoch 秒（或已是字符串的时间）→ RFC 3339 UTC 毫秒（契约 1.9）；空值为 None。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    moment = datetime.fromtimestamp(float(value), timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_time(value: str, field: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        raise ApiError("invalid_argument", f"{field} 必须是 RFC 3339 时间", 400, {"fields": {field: "time"}}) from None


# ---- 游标分页（契约 1.6） ----


def page(items: list, cursor: str = "", limit: int = 0, binding: dict | None = None,
         default: int = PAGE_DEFAULT, maximum: int = PAGE_MAX) -> dict:
    """游标绑定筛选条件：条件变了再用旧游标返回 invalid_cursor。"""
    limit = max(1, min(int(limit or default), maximum))
    key = hashlib.sha256(json.dumps(binding or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    offset = 0
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            offset = int(data["o"])
            valid = data.get("b") == key and offset >= 0
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ApiError("invalid_cursor", "游标无法解析，或筛选条件变了", 400)
    end = offset + limit
    next_cursor = None
    if end < len(items):
        next_cursor = base64.urlsafe_b64encode(json.dumps({"o": end, "b": key}).encode()).decode().rstrip("=")
    return {"items": items[offset:end], "next_cursor": next_cursor, "total": len(items)}


async def json_body(request: Request, limit: int = 1024 * 1024) -> dict:
    """读 JSON 对象请求体（默认上限 1 MiB，契约 1.8）。"""
    raw = await request.body()
    if len(raw) > limit:
        raise ApiError("payload_too_large", "请求体超过上限", 413, {"limit_bytes": limit})
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError("invalid_argument", "请求体必须是 UTF-8 JSON", 400) from None
    if not isinstance(data, dict):
        raise ApiError("invalid_argument", "请求体必须是 JSON 对象", 400)
    return data
