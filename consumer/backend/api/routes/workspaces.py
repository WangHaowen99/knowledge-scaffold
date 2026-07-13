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
from api.auth.auth import require_auth, require_role
from workspace.workspaces import (
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
)
from api.routes.errors import not_found_response


workspaces_bp = Blueprint("workspaces", __name__, url_prefix="/agentic")


@workspaces_bp.route("/workspaces", methods=["GET"])
@require_auth()
def list_workspaces_api():
    items = list_workspaces()
    return jsonify({
        "workspaces": items,
        "total": len(items),
        "default_workspace_id": get_default_workspace_id(),
    })


@workspaces_bp.route("/workspaces", methods=["POST"])
@require_role("admin")
def create_workspace_api():
    payload = request.get_json(silent=True) or {}
    try:
        created = create_workspace(
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            workspace_id=payload.get("id"),
            set_as_default=bool(payload.get("set_as_default")),
        )
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(created), 201


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["GET"])
@require_auth()
def get_workspace_api(workspace_id: str):
    try:
        return jsonify(get_workspace(workspace_id))
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400


@workspaces_bp.route("/workspaces/<workspace_id>", methods=["PATCH"])
@require_role("admin")
def update_workspace_api(workspace_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        updated = update_workspace(
            workspace_id,
            name=payload.get("name"),
            description=payload.get("description"),
        )
    except KeyError as exc:
        return not_found_response(exc)
    except ValueError as exc:
        return jsonify({"detail": str(exc)}), 400
    return jsonify(updated)


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
        return jsonify(get_current_workspace_runtime_config())
    except KeyError as exc:
        return not_found_response(exc)


@workspaces_bp.route("/workspaces/<workspace_id>/llm-profiles", methods=["GET"])
@require_auth()
def get_workspace_llm_profiles_api(workspace_id: str):
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
