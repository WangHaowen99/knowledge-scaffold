"""Workspace management endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..services.auth import require_auth, require_role
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
)


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
        return jsonify({"detail": str(exc)}), 404
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
