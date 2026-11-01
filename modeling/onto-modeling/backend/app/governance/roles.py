"""角色（详设 4.10.1、方案 E3、契约 1.3）：管理员、运维、普通用户三种，项目只归所有者。

- JWT 的 `role` claim 取 user、admin、ops 之一（auth-server 已支持 ops）；认不出的值按 user；
- 每个人都是 user：能建并拥有自己的项目。admin 另加系统设置、用户管理、指定所有者、服务令牌；
  ops 另加运维管理面的写操作、审计、错误留档、回收站；
- 运维管理面的只读接口 admin 也能调，写操作只有 ops（契约 3.25；备份 O-37、回收站 O-53/O-54 另说明
  admin 也可）；
- admin、ops 都只能看别人项目的元数据（ProjectMetaView），不能读写内容（所有权在 services/ownership）；
- 本地模式（鉴权关闭）是 local 用户，三种角色都有。
"""

from __future__ import annotations

from fastapi import Request

ROLES = ("user", "admin", "ops")
ROLE_LABELS = {"user": "建模者", "admin": "管理员", "ops": "运维"}
# 能看全部项目元数据的角色（契约 1.4）
META_ROLES = frozenset({"admin", "ops"})


def role_of(claims: dict | None) -> str:
    """单值角色（UserProfile.role）：本地模式取界面上权限最大的 admin。"""
    if claims is None:
        return "admin"
    role = claims.get("role")
    return role if role in ROLES else "user"


def roles_of(claims: dict | None) -> set[str]:
    if claims is None:
        return set(ROLES)
    return {"user", role_of(claims)}


def has(request: Request, *allowed: str) -> bool:
    from ..services import ownership

    return bool(roles_of(ownership.current_user(request)) & set(allowed))


def require(request: Request, *allowed: str) -> None:
    from ..api.errors import ApiError

    if not has(request, *allowed):
        raise ApiError("forbidden", "当前账号没有该操作的权限", 403, {"required_role": allowed[0]})


def require_ops_read(request: Request) -> None:
    """运维面只读：ops、admin。"""
    require(request, "ops", "admin")


def require_ops_write(request: Request, *, admin_too: bool = False) -> None:
    """运维面写操作：只有 ops；契约另说明 admin 也可的接口传 admin_too。"""
    if admin_too:
        require(request, "ops", "admin")
    else:
        require(request, "ops")


def sees_all_meta(claims: dict | None) -> bool:
    return claims is None or bool(roles_of(claims) & META_ROLES)
