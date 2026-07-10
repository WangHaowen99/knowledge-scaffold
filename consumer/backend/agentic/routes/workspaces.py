"""Workspace management endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.auth import get_current_user, require_auth, require_role
from ..workspaces import (
    create_workspace,
    delete_workspace,
    get_current_workspace_runtime_config,
    get_default_workspace_id,
    get_workspace,
    get_workspace_runtime_config,
    list_workspaces,
    set_default_workspace,
    update_workspace,
    update_workspace_runtime_config,
    user_can_access_workspace,
)


workspaces_bp = Blueprint("workspaces", __name__, url_prefix="/agentic")


def _workspace_forbidden():
    return jsonify({"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}), 403


def _is_global_admin(user) -> bool:
    return getattr(user, "role", None) == "admin"


def _relationship_role(user, workspace: dict):
    """当前用户与该空间的关系：owner→'admin'，member→'member'，其余 None。"""
    username = getattr(user, "username", None)
    if not username:
        return None
    if workspace.get("owner") == username:
        return "admin"
    if username in (workspace.get("members") or []):
        return "member"
    return None


@workspaces_bp.route("/workspaces", methods=["GET"])
@require_auth()
def list_workspaces_api():
    user = get_current_user()
    items = []
    for item in list_workspaces():
        if not user_can_access_workspace(user, item, bool(item.get("is_default"))):
            continue
        item["role"] = _relationship_role(user, item)
        items.append(item)
    return jsonify({
        "workspaces": items,
        "total": len(items),
        "default_workspace_id": get_default_workspace_id(),
    })


@workspaces_bp.route("/workspaces", methods=["POST"])
@require_role("admin")
def create_workspace_api():
    payload = request.get_json(silent=True) or {}
    owner = payload.get("owner") if "owner" in payload else get_current_user().username
    try:
        created = create_workspace(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            workspace_id=payload.get("id"),
            set_as_default=bool(payload.get("set_as_default")),
            owner=owner,
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(created), 201


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["GET"])
@require_auth()
def get_workspace_api(workspace_id: str):
    try:
        workspace = get_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user = get_current_user()
    # 与列表过滤同一可见性规则：不可见空间连元数据（名称/成员）也不暴露
    if not user_can_access_workspace(user, workspace, bool(workspace.get("is_default"))):
        return _workspace_forbidden()
    workspace["role"] = _relationship_role(user, workspace)
    return jsonify(workspace)


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["PATCH"])
@require_auth()
def update_workspace_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    try:
        current = get_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    is_admin = _is_global_admin(user)
    is_owner = bool(current.get("owner")) and current.get("owner") == user.username
    if not (is_admin or is_owner):
        return _workspace_forbidden()
    if "owner" in payload and not is_admin:
        return _workspace_forbidden()

    # 仅转发 body 里显式给出的字段：缺省 owner 表示不改，body 里的 owner:null 才清空为公共空间
    fields = {"name": payload.get("name"), "description": payload.get("description")}
    if "owner" in payload:
        fields["owner"] = payload["owner"]
    try:
        updated = update_workspace(workspace_id, **fields)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


@workspaces_bp.route("/workspaces/<workspace_id>/members", methods=["PUT"])
@require_auth()
def update_workspace_members_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    try:
        current = get_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    is_owner = bool(current.get("owner")) and current.get("owner") == user.username
    if not (_is_global_admin(user) or is_owner):
        return _workspace_forbidden()

    members = payload.get("members")
    if members is None:
        return jsonify({"detail": "members 必须是字符串数组"}), 400
    try:
        updated = update_workspace(workspace_id, members=members)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["DELETE"])
@require_role("admin")
def delete_workspace_api(workspace_id: str):
    try:
        delete_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify({"success": True, "id": workspace_id})


@workspaces_bp.route("/workspaces/<workspace_id>/default", methods=["POST"])
@require_role("admin")
def set_default_workspace_api(workspace_id: str):
    try:
        updated = set_default_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "workspace": updated,
        "default_workspace_id": updated["id"],
    })


@workspaces_bp.route("/workspaces/<workspace_id>/runtime-config", methods=["GET"])
@require_auth()
def get_runtime_config_api(workspace_id: str):
    # 该端点从 URL 路径取 id，不经 resolve_workspace_id 收口，须自行做同款可见性校验，
    # 否则非成员可读私有空间的 acp_suppress_script_patterns 等配置（水平越权读）
    try:
        workspace = get_workspace(workspace_id)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    if not user_can_access_workspace(
        get_current_user(), workspace, bool(workspace.get("is_default"))
    ):
        return _workspace_forbidden()
    try:
        return jsonify(get_workspace_runtime_config(workspace_id))
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404


@workspaces_bp.route("/workspaces/<workspace_id>/runtime-config", methods=["PATCH"])
@require_role("admin")
def update_runtime_config_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"detail": "请求体不能为空"}), 400
    try:
        updated = update_workspace_runtime_config(workspace_id, payload)
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


@workspaces_bp.route("/workspaces/current/runtime-config", methods=["GET"])
@require_auth()
def get_current_runtime_config_api():
    try:
        return jsonify(get_current_workspace_runtime_config())
    except KeyError as exc:
        return jsonify({"detail": str(exc)}), 404
