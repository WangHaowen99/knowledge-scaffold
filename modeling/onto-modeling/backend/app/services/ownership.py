"""项目所有权：一个项目只归一个人（详细设计 1.6、4.10.1）。

项目内容（读写、会话、Studio、文件、资料、版本、导出发布、MCP token、改名/归档/删除）只归
所有者：元数据 owner 非空且等于当前用户。没有成员、建模者和只读评审。管理员不是所有者时只看
项目元数据、指定所有者；无主项目（owner 为空）普通用户看不到也打不开，管理员指定所有者后才
可用。鉴权关闭（没配 ONTO_WEB_AUTH_URL，request.state.user 为 None）时全部放开。
"""

from __future__ import annotations

import re
from pathlib import Path

from . import auth
from .workspace import WorkspaceError

# 指定所有者时的用户名格式：认证服务的用户名字符集，长度沿用原成员校验的上限
OWNER_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# 管理员看非本人项目时的元数据白名单：名称、所有者、阶段、归档状态与创建时间。
# 背景说明、对象/操作计数、评测结果、资料分析验收、发布记录、引擎与 MCP 配置都算项目内容
META_VIEW_FIELDS = ("id", "name", "owner", "archived", "created_at", "stages_done", "stages_total")

CODE_OWNER_ONLY = "project_owner_only"
CODE_UNOWNED = "project_unowned"


def current_user(request) -> dict | None:
    return getattr(request.state, "user", None)


def is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("role") == "admin"


def owns(meta: dict, username: str) -> bool:
    owner = str(meta.get("owner") or "")
    return bool(owner) and owner == username


def visible(meta: dict, username: str, admin: bool) -> bool:
    """项目列表与同名校验的范围：自己的项目；管理员另外看得到全部项目的元数据。"""
    return admin or owns(meta, username)


def meta_view(meta: dict) -> dict:
    return {field: meta[field] for field in META_VIEW_FIELDS if field in meta}


def list_view(projects: list[dict], user: dict | None) -> list[dict]:
    """项目列表：本人项目给完整信息，管理员看到的非本人项目只给白名单元数据。"""
    if user is None:
        return projects
    username, admin = user.get("sub", ""), is_admin(user)
    return [
        project if owns(project, username) else meta_view(project)
        for project in projects
        if visible(project, username, admin)
    ]


def denied(meta: dict) -> WorkspaceError:
    if not str(meta.get("owner") or ""):
        return WorkspaceError("该项目尚未指定所有者，请联系管理员", 403, CODE_UNOWNED)
    return WorkspaceError("项目内容只归所有者，无权访问", 403, CODE_OWNER_ONLY)


def require_owner(manager, project_id: str, request) -> Path:
    """项目内容守卫（读写与管理操作同一道）：只有所有者能用，返回项目根目录。"""
    root = manager.project_root(project_id)
    user = current_user(request)
    if user is None:
        return root
    meta = manager.get_project(project_id)
    if not owns(meta, user.get("sub", "")):
        raise denied(meta)
    return root


def require_meta_view(manager, project_id: str, request) -> dict:
    """项目元数据：所有者拿完整元数据，管理员拿白名单字段，其余与 require_owner 同样拒绝。"""
    meta = manager.get_project(project_id)
    user = current_user(request)
    if user is None or owns(meta, user.get("sub", "")):
        return meta
    if is_admin(user):
        return meta_view(meta)
    raise denied(meta)


def acts_as_owner(meta: dict, request) -> bool:
    user = current_user(request)
    return user is None or owns(meta, user.get("sub", ""))


def validate_owner_name(owner: str) -> str:
    owner = (owner or "").strip()
    if not owner:
        raise WorkspaceError("所有者不能为空", 400, "invalid_owner")
    if not OWNER_NAME_RE.fullmatch(owner):
        raise WorkspaceError(f"无效的用户名：{owner}", 400, "invalid_owner")
    return owner


def token_subject_allowed(meta: dict, subject: str) -> bool:
    """MCP token 只在签发人仍是项目所有者时有效：换所有者后旧所有者的 token 自动失效。

    MCP 传输不经过登录中间件，鉴权是否开启看部署配置；关闭时与网页一样全部放开。
    """
    if not auth.ENABLED:
        return True
    return owns(meta, subject)
