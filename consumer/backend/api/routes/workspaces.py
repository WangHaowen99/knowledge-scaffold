"""Workspace management endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from config.system_config import SystemConfigValidationError
from config.workspace_llm_profiles import (
    get_public_workspace_llm_profiles,
    test_workspace_llm_profile_connection,
    update_workspace_llm_profiles,
)
from integration.observability import reset_observer
from integration.observability.workspace_config import (
    get_public_workspace_observability_config,
    test_workspace_observability_connection,
    update_workspace_observability_config,
)
from api.auth.auth import get_current_user, require_auth, require_role
from workspace.workspaces import (
    WorkspaceAuthorizationError,
    create_workspace,
    delete_workspace,
    enforce_workspace_access,
    get_default_workspace_id,
    get_workspace,
    get_workspace_runtime_config,
    list_workspaces,
    resolve_workspace_id,
    set_default_workspace,
    update_workspace_authorized,
    update_workspace_runtime_config,
    user_can_access_workspace,
)
from api.routes.errors import not_found_response


workspaces_bp = Blueprint("workspaces", __name__, url_prefix="/agentic")


def _workspace_forbidden():
    return jsonify({"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}), 403


def _relationship_role(user, workspace: dict):
    username = getattr(user, "username", None)
    if not username:
        return None
    if workspace.get("owner") == username:
        return "admin"
    if username in (workspace.get("members") or []):
        return "member"
    return None


def _enforce_direct_workspace_access(workspace_id: str):
    """Enforce ACL for path-based endpoints that do not call resolve_workspace_id."""
    try:
        return enforce_workspace_access(workspace_id), None
    except KeyError as exc:
        return None, not_found_response(exc)
    except ValueError as exc:
        return None, (jsonify({"detail": str(exc)}), 400)


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
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    user = get_current_user()
    if not user_can_access_workspace(user, workspace, bool(workspace.get("is_default"))):
        return _workspace_forbidden()
    workspace["role"] = _relationship_role(user, workspace)
    return jsonify(workspace)


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["PATCH"])
@require_auth()
def update_workspace_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    fields = {"name": payload.get("name"), "description": payload.get("description")}
    if "owner" in payload:
        fields["owner"] = payload["owner"]
    try:
        updated = update_workspace_authorized(workspace_id, user, **fields)
    except WorkspaceAuthorizationError:
        return _workspace_forbidden()
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


@workspaces_bp.route("/workspaces/<workspace_id>/members", methods=["PUT"])
@require_auth()
def update_workspace_members_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    user = get_current_user()
    members = payload.get("members")
    if members is None:
        return jsonify({"detail": "members 必须是字符串数组"}), 400
    try:
        return jsonify(update_workspace_authorized(workspace_id, user, members=members))
    except WorkspaceAuthorizationError:
        return _workspace_forbidden()
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["DELETE"])
@require_role("admin")
def delete_workspace_api(workspace_id: str):
    try:
        delete_workspace(workspace_id)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify({"success": True, "id": workspace_id})


@workspaces_bp.route("/workspaces/<workspace_id>/default", methods=["POST"])
@require_role("admin")
def set_default_workspace_api(workspace_id: str):
    try:
        updated = set_default_workspace(workspace_id)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "workspace": updated,
        "default_workspace_id": updated["id"],
    })


@workspaces_bp.route("/workspaces/<workspace_id>/runtime-config", methods=["GET"])
@require_auth()
def get_runtime_config_api(workspace_id: str):
    _, error = _enforce_direct_workspace_access(workspace_id)
    if error is not None:
        return error
    try:
        return jsonify(get_workspace_runtime_config(workspace_id))
    except KeyError as exc:
        return not_found_response(exc)


@workspaces_bp.route("/workspaces/<workspace_id>/runtime-config", methods=["PATCH"])
@require_role("admin")
def update_runtime_config_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"detail": "请求体不能为空"}), 400
    try:
        updated = update_workspace_runtime_config(workspace_id, payload)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


@workspaces_bp.route("/workspaces/<workspace_id>/observability-config", methods=["GET"])
@require_auth()
def get_observability_config_api(workspace_id: str):
    _, error = _enforce_direct_workspace_access(workspace_id)
    if error is not None:
        return error
    try:
        return jsonify(get_public_workspace_observability_config(workspace_id))
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>/observability-config", methods=["PATCH"])
@require_role("admin")
def update_observability_config_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    if not payload:
        return jsonify({"detail": "请求体不能为空"}), 400
    try:
        update_workspace_observability_config(workspace_id, payload)
        reset_observer(timeout=1.0)
        return jsonify(get_public_workspace_observability_config(workspace_id))
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>/observability-config/test", methods=["POST"])
@require_role("admin")
def test_observability_config_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(test_workspace_observability_connection(workspace_id, payload))
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/current/runtime-config", methods=["GET"])
@require_auth()
def get_current_runtime_config_api():
    try:
        workspace_id = resolve_workspace_id(request)
        return jsonify(get_workspace_runtime_config(workspace_id))
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>/llm-profiles", methods=["GET"])
@require_auth()
def get_workspace_llm_profiles_api(workspace_id: str):
    _, error = _enforce_direct_workspace_access(workspace_id)
    if error is not None:
        return error
    try:
        return jsonify({"profiles": get_public_workspace_llm_profiles(workspace_id)})
    except KeyError as exc:
        return not_found_response(exc)


@workspaces_bp.route("/workspaces/<workspace_id>/llm-profiles", methods=["PATCH"])
@require_role("admin")
def update_workspace_llm_profiles_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(update_workspace_llm_profiles(workspace_id, payload))
    except KeyError as exc:
        return not_found_response(exc)
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>/llm-profiles/test", methods=["POST"])
@require_role("admin")
def test_workspace_llm_profiles_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(test_workspace_llm_profile_connection(workspace_id, payload))
    except KeyError as exc:
        return not_found_response(exc)
    except SystemConfigValidationError as exc:
        return jsonify({"detail": str(exc)}), 400
