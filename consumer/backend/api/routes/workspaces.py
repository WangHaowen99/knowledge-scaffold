"""Workspace management endpoints."""

from __future__ import annotations

import re

from flask import Blueprint, jsonify, request

from config.system_config import SystemConfigValidationError
from config.workspace_llm_profiles import (
    get_public_workspace_llm_profiles,
    test_workspace_llm_profile_connection,
    update_workspace_llm_profiles,
)
from integration.observability import reset_observer
from integration.observability.workspace_config import (
    WorkspaceObservabilityMigrationRequiredError,
    get_public_workspace_observability_config,
    test_workspace_observability_connection,
    update_workspace_observability_config,
)
from api.auth.auth import get_auth_manager, get_current_user, require_auth, require_role
from api.auth.user_deletion import (
    UserDeletionPendingError,
    UserDeletionReconciliationError,
)
from api.auth.service_auth import modeling_service_authorized
from api.workspace_acl_service import (
    WorkspaceIdentityDirectoryUnavailableError,
    WorkspaceIdentityNotFoundError,
    create_workspace_with_validated_owner,
    replace_workspace_members,
    update_workspace_authorized_with_validated_owner,
    update_workspace_owner_with_validation,
    validate_workspace_member,
)
from workspace.workspaces import (
    WorkspaceAuthorizationError,
    delete_workspace,
    enforce_workspace_access,
    enforce_workspace_owner_or_admin,
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

_MODELING_PROJECT_ID_PATTERN = re.compile(r"^pj_[A-Za-z0-9_-]{1,48}$")
_WORKSPACE_IDENTITY_ERRORS = (
    WorkspaceIdentityNotFoundError,
    WorkspaceIdentityDirectoryUnavailableError,
    UserDeletionPendingError,
    UserDeletionReconciliationError,
)


def _workspace_forbidden():
    return jsonify({"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}), 403


def _request_json_object():
    payload = request.get_json(silent=True)
    if payload is None:
        return {}, None
    if not isinstance(payload, dict):
        return None, (jsonify({"detail": "请求体必须是 JSON object"}), 400)
    return payload, None


def _workspace_identity_error(exc):
    if isinstance(exc, WorkspaceIdentityNotFoundError):
        payload = {"detail": exc.detail, "code": exc.code}
        if exc.field == "owner":
            payload["username"] = exc.usernames[0] if exc.usernames else ""
        else:
            payload["usernames"] = list(exc.usernames)
            if len(exc.usernames) == 1:
                payload["username"] = exc.usernames[0]
        return jsonify(payload), 400
    if isinstance(exc, UserDeletionPendingError):
        return jsonify({
            "detail": "用户 {} 正在删除或等待清理，暂时不能写入工作空间权限".format(exc.username),
            "code": "USER_DELETION_RECONCILIATION_PENDING",
            "username": exc.username,
            "reconciliation_id": exc.reconciliation_id,
        }), 409
    if isinstance(exc, UserDeletionReconciliationError):
        return jsonify({
            "error": "用户删除状态暂不可用，请稍后再试",
            "code": "USER_DELETION_STATE_UNAVAILABLE",
        }), 503
    return jsonify({
        "detail": WorkspaceIdentityDirectoryUnavailableError.detail,
        "code": WorkspaceIdentityDirectoryUnavailableError.code,
    }), 503


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


def _shadow_workspace_id(project_id: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9-]", "-", project_id).lower()
    return "mw-shadow-{}".format(normalized)[:60]


@workspaces_bp.route("/internal/modeling/shadow-workspaces/ensure", methods=["POST"])
def ensure_modeling_shadow_workspace_api():
    if not modeling_service_authorized(request):
        return jsonify({"error": "Forbidden"}), 403

    payload, payload_error = _request_json_object()
    if payload_error is not None:
        return payload_error
    project_id = str(payload.get("project_id") or "").strip()
    owner = str(payload.get("owner") or "").strip()
    if not _MODELING_PROJECT_ID_PATTERN.fullmatch(project_id):
        return jsonify({"detail": "project_id 不合法"}), 400
    if not owner:
        return jsonify({"detail": "owner 不能为空"}), 400

    workspace_id = _shadow_workspace_id(project_id)
    try:
        current = get_workspace(workspace_id)
    except KeyError:
        try:
            created = create_workspace_with_validated_owner(
                get_auth_manager(),
                name="建模影子·{}".format(project_id),
                description="知识包建模工具评测专用，自动管理",
                workspace_id=workspace_id,
                owner=owner,
                enforce_workspace_limit=False,
            )
        except _WORKSPACE_IDENTITY_ERRORS as exc:
            return _workspace_identity_error(exc)
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 400
        return jsonify({"created": True, "workspace": created}), 201
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400

    current_owner = current.get("owner")
    if current_owner not in (None, owner):
        return jsonify({"detail": "影子工作区已属于其他用户"}), 409
    if current_owner is None:
        try:
            current = update_workspace_owner_with_validation(
                get_auth_manager(),
                workspace_id,
                owner,
            )
        except _WORKSPACE_IDENTITY_ERRORS as exc:
            return _workspace_identity_error(exc)
        except ValueError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"created": False, "workspace": current})


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
@require_auth()
def create_workspace_api():
    payload, payload_error = _request_json_object()
    if payload_error is not None:
        return payload_error
    user = get_current_user()
    is_admin = getattr(user, "role", None) == "admin"
    owner = payload.get("owner") if is_admin and "owner" in payload else user.username
    set_as_default = bool(payload.get("set_as_default")) if is_admin else False
    try:
        created = create_workspace_with_validated_owner(
            get_auth_manager(),
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            workspace_id=payload.get("id"),
            set_as_default=set_as_default,
            auto_set_default=is_admin,
            owner=owner,
        )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
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
    payload, payload_error = _request_json_object()
    if payload_error is not None:
        return payload_error
    user = get_current_user()
    fields = {"name": payload.get("name"), "description": payload.get("description")}
    if "owner" in payload:
        fields["owner"] = payload["owner"]
    try:
        if "owner" in payload:
            if getattr(user, "role", None) != "admin":
                return _workspace_forbidden()
            owner = fields.pop("owner")
            updated = update_workspace_authorized_with_validated_owner(
                get_auth_manager(),
                workspace_id,
                user,
                owner=owner,
                **fields,
            )
        else:
            updated = update_workspace_authorized(
                workspace_id,
                user,
                name=fields["name"],
                description=fields["description"],
            )
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
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
    payload, payload_error = _request_json_object()
    if payload_error is not None:
        return payload_error
    user = get_current_user()
    members = payload.get("members")
    if members is None:
        return jsonify({"detail": "members 必须是字符串数组"}), 400
    try:
        enforce_workspace_owner_or_admin(workspace_id)
        return jsonify(replace_workspace_members(
            get_auth_manager(),
            workspace_id,
            user,
            members,
        ))
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except WorkspaceAuthorizationError:
        return _workspace_forbidden()
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>/members/validate", methods=["POST"])
@require_auth()
def validate_workspace_member_api(workspace_id: str):
    payload, payload_error = _request_json_object()
    if payload_error is not None:
        return payload_error
    try:
        enforce_workspace_owner_or_admin(workspace_id)
        username = validate_workspace_member(get_auth_manager(), payload.get("username"))
        return jsonify({"valid": True, "username": username})
    except _WORKSPACE_IDENTITY_ERRORS as exc:
        return _workspace_identity_error(exc)
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["DELETE"])
@require_auth()
def delete_workspace_api(workspace_id: str):
    user = get_current_user()
    is_admin = getattr(user, "role", None) == "admin"
    try:
        enforce_workspace_owner_or_admin(workspace_id)
        delete_workspace(workspace_id, allow_default=not is_admin)
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
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return jsonify(exc.to_dict()), 409
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
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return jsonify(exc.to_dict()), 409
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
    except WorkspaceObservabilityMigrationRequiredError as exc:
        return jsonify(exc.to_dict()), 409
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
