"""Business data connection and ontology binding endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..business_data.bindings_store import (
    OntologyDataBindingError,
    delete_ontology_data_binding,
    get_ontology_data_binding,
    list_bound_ontology_ids,
    set_ontology_data_binding,
)
from ..business_data.connections_store import (
    BusinessDataConnectionStoreError,
    create_data_connection,
    delete_data_connection,
    get_data_connection,
    list_data_connections,
    publicize_connection,
    set_default_data_connection,
    update_data_connection,
)
from ..business_data.connection_tester import test_business_data_connection
from ..business_data.provider_registry import list_provider_schemas
from ..business_data.resolver import ResolvedBusinessDataConnection
from ..services.auth import require_auth, require_role
from ..workspaces import resolve_workspace_id, workspace_scope


business_data_bp = Blueprint("business_data", __name__, url_prefix="/agentic")


def _resolve_workspace_or_response():
    try:
        return resolve_workspace_id(request), None
    except KeyError as exc:
        return None, (jsonify({"detail": str(exc)}), 404)
    except ValueError as exc:
        return None, (jsonify({"detail": str(exc)}), 400)


@business_data_bp.route("/business-data/provider-schemas", methods=["GET"])
@require_auth()
def list_business_data_provider_schemas_api():
    return jsonify({"providers": list_provider_schemas()})


@business_data_bp.route("/data-connections", methods=["GET"])
@require_auth()
def list_data_connections_api():
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        return jsonify(list_data_connections(workspace_id))


@business_data_bp.route("/data-connections", methods=["POST"])
@require_role("admin")
def create_data_connection_api():
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    with workspace_scope(workspace_id):
        try:
            connection = create_data_connection(payload, workspace_id=workspace_id)
            result = list_data_connections(workspace_id)
        except BusinessDataConnectionStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "connection": publicize_connection(connection),
        "default_connection_id": result["default_connection_id"],
    }), 201


@business_data_bp.route("/data-connections/<connection_id>", methods=["PATCH"])
@require_role("admin")
def update_data_connection_api(connection_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    with workspace_scope(workspace_id):
        try:
            connection = update_data_connection(connection_id, payload, workspace_id=workspace_id)
        except BusinessDataConnectionStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"connection": publicize_connection(connection)})


@business_data_bp.route("/data-connections/<connection_id>", methods=["DELETE"])
@require_role("admin")
def delete_data_connection_api(connection_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        bound_ontologies = list_bound_ontology_ids(connection_id, workspace_id=workspace_id)
        if bound_ontologies:
            return jsonify({
                "detail": "该连接仍被本体绑定，不能删除",
                "bound_ontologies": bound_ontologies,
            }), 409
        try:
            delete_data_connection(connection_id, workspace_id=workspace_id)
        except BusinessDataConnectionStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"success": True, "id": connection_id})


@business_data_bp.route("/data-connections/<connection_id>/default", methods=["POST"])
@require_role("admin")
def set_default_data_connection_api(connection_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        try:
            default_connection_id = set_default_data_connection(connection_id, workspace_id=workspace_id)
        except BusinessDataConnectionStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"default_connection_id": default_connection_id})


@business_data_bp.route("/data-connections/<connection_id>/test", methods=["POST"])
@require_role("admin")
def test_data_connection_api(connection_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        try:
            stored_connection = get_data_connection(connection_id, workspace_id=workspace_id)
            connection = ResolvedBusinessDataConnection(
                id=stored_connection.id,
                name=stored_connection.name,
                provider=stored_connection.provider,
                config=dict(stored_connection.config),
            )
            result = test_business_data_connection(connection)
        except BusinessDataConnectionStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "success": bool(result.get("success")),
        "message": str(result.get("message") or ""),
        "tested_at": result.get("tested_at"),
        "connection": publicize_connection(stored_connection),
    })


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["GET"])
@require_auth()
def get_ontology_data_binding_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        return jsonify({
            "ontology_id": ontology_id,
            "binding": get_ontology_data_binding(ontology_id, workspace_id=workspace_id),
        })


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["PUT"])
@require_role("admin")
def set_ontology_data_binding_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    with workspace_scope(workspace_id):
        try:
            binding = set_ontology_data_binding(
                ontology_id=ontology_id,
                connection_id=payload.get("connection_id"),
                workspace_id=workspace_id,
            )
        except OntologyDataBindingError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"binding": binding})


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["DELETE"])
@require_role("admin")
def delete_ontology_data_binding_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        try:
            delete_ontology_data_binding(ontology_id, workspace_id=workspace_id)
        except OntologyDataBindingError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"success": True, "ontology_id": ontology_id})
