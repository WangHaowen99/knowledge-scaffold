"""Business data provider schema and ontology data source endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from agent.tools.business_data.ontology_sources_store import (
    MODE_CONNECTION_REF,
    OntologyDataSourceStoreError,
    build_unsaved_ontology_data_source,
    delete_ontology_data_source,
    delete_ontology_data_binding,
    get_ontology_data_source,
    get_ontology_data_binding,
    publicize_ontology_data_source,
    set_ontology_data_binding,
    set_ontology_data_source,
)
from agent.tools.business_data.connections_store import (
    BusinessDataConnectionConflictError,
    BusinessDataConnectionStoreError,
    create_data_connection,
    delete_data_connection,
    get_data_connection,
    list_data_connections,
    publicize_connection,
    set_default_data_connection,
    update_data_connection,
)
from agent.tools.business_data.connection_tester import test_business_data_connection
from agent.tools.business_data.provider_registry import list_provider_schemas
from agent.tools.business_data.resolver import ResolvedBusinessDataSource, resolve_business_data_source
from api.auth.auth import require_auth, require_role
from workspace.workspaces import resolve_workspace_id, workspace_scope
from api.routes.errors import not_found_response


business_data_bp = Blueprint("business_data", __name__, url_prefix="/agentic")


def _resolve_workspace_or_response():
    try:
        return resolve_workspace_id(request), None
    except KeyError as exc:
        return None, not_found_response(exc)
    except ValueError as exc:
        return None, (jsonify({"detail": str(exc)}), 400)


@business_data_bp.route("/business-data/provider-schemas", methods=["GET"])
@require_auth()
def list_business_data_provider_schemas_api():
    _workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
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
        try:
            delete_data_connection(connection_id, workspace_id=workspace_id)
        except BusinessDataConnectionConflictError as exc:
            return jsonify({
                "detail": str(exc),
                "bound_ontologies": exc.bound_ontology_ids,
            }), 409
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
            connection = ResolvedBusinessDataSource(
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


def _data_source_response(ontology_id: str, source):
    return {
        "ontology_id": ontology_id,
        "configured": source is not None,
        "source": publicize_ontology_data_source(source) if source is not None else None,
    }


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["GET"])
@require_auth()
def get_ontology_data_binding_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        binding = get_ontology_data_binding(ontology_id, workspace_id=workspace_id)
    return jsonify({"ontology_id": ontology_id, "binding": binding})


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
        except OntologyDataSourceStoreError as exc:
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
        except OntologyDataSourceStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({"success": True, "ontology_id": ontology_id})


@business_data_bp.route("/ontologies/<ontology_id>/data-source", methods=["GET"])
@require_auth()
def get_ontology_data_source_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        source = get_ontology_data_source(ontology_id, workspace_id=workspace_id)
    return jsonify(_data_source_response(ontology_id, source))


@business_data_bp.route("/ontologies/<ontology_id>/data-source", methods=["PUT"])
@require_role("admin")
def set_ontology_data_source_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    with workspace_scope(workspace_id):
        try:
            source = set_ontology_data_source(
                ontology_id=ontology_id,
                data=payload,
                workspace_id=workspace_id,
            )
        except OntologyDataSourceStoreError as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify(_data_source_response(ontology_id, source))


@business_data_bp.route("/ontologies/<ontology_id>/data-source", methods=["DELETE"])
@require_role("admin")
def delete_ontology_data_source_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        delete_ontology_data_source(ontology_id, workspace_id=workspace_id)
    return jsonify({
        "success": True,
        "ontology_id": ontology_id,
        "configured": False,
    })


@business_data_bp.route("/ontologies/<ontology_id>/data-source/test", methods=["POST"])
@require_role("admin")
def test_ontology_data_source_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    with workspace_scope(workspace_id):
        try:
            if payload:
                source = build_unsaved_ontology_data_source(
                    ontology_id=ontology_id,
                    data=payload,
                    workspace_id=workspace_id,
                )
            else:
                source = resolve_business_data_source(
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                )
            if isinstance(source, ResolvedBusinessDataSource):
                connection = source
            elif source.mode == MODE_CONNECTION_REF:
                stored_connection = get_data_connection(
                    source.connection_id,
                    workspace_id=workspace_id,
                )
                connection = ResolvedBusinessDataSource(
                    ontology_id=source.ontology_id,
                    mode=source.mode,
                    provider=stored_connection.provider,
                    config=dict(stored_connection.config),
                    id=stored_connection.id,
                    name=stored_connection.name,
                )
            else:
                connection = ResolvedBusinessDataSource(
                    ontology_id=source.ontology_id,
                    mode=source.mode,
                    provider=source.provider,
                    config=dict(source.config),
                    id=source.ontology_id,
                    name=source.ontology_id,
                )
            result = test_business_data_connection(connection)
        except (OntologyDataSourceStoreError, ValueError) as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "success": bool(result.get("success")),
        "message": str(result.get("message") or ""),
        "tested_at": result.get("tested_at"),
    })
