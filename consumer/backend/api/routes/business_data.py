"""Business data provider schema and ontology data source endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from agent.tools.business_data.connection_tester import test_business_data_connection
from agent.tools.business_data.ontology_sources_store import (
    OntologyDataSourceStoreError,
    build_unsaved_ontology_data_source,
    delete_ontology_data_source,
    get_ontology_data_source,
    publicize_ontology_data_source,
    set_ontology_data_source,
)
from agent.tools.business_data.provider_registry import list_provider_schemas
from agent.tools.business_data.resolver import ResolvedBusinessDataSource, resolve_business_data_source
from api.auth.auth import require_auth, require_role
from api.routes.errors import not_found_response
from workspace.workspaces import resolve_workspace_id, workspace_scope


business_data_bp = Blueprint("business_data", __name__, url_prefix="/agentic")


def _resolve_workspace_or_response():
    try:
        return resolve_workspace_id(request), None
    except KeyError as exc:
        return None, not_found_response(exc)
    except ValueError as exc:
        return None, (jsonify({"detail": str(exc)}), 400)


def _deprecated_response(replacement: str):
    _workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    return jsonify({
        "detail": "数据连接与绑定接口已停用，请改用本体数据源接口",
        "deprecated": True,
        "replacement": replacement,
    }), 410


@business_data_bp.route("/business-data/provider-schemas", methods=["GET"])
@require_auth()
def list_business_data_provider_schemas_api():
    _workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    return jsonify({"providers": list_provider_schemas()})


# One-release retirement shim. These routes never create new shared connections
# or connection_ref records; callers receive the canonical ontology endpoint.
@business_data_bp.route("/data-connections", methods=["GET"])
@require_auth()
def deprecated_list_data_connections_api():
    return _deprecated_response("/agentic/ontologies/{ontology_id}/data-source")


@business_data_bp.route("/data-connections", methods=["POST"])
@require_role("admin")
def deprecated_create_data_connection_api():
    return _deprecated_response("/agentic/ontologies/{ontology_id}/data-source")


@business_data_bp.route("/data-connections/<connection_id>", methods=["PATCH", "DELETE"])
@require_role("admin")
def deprecated_mutate_data_connection_api(connection_id: str):
    del connection_id
    return _deprecated_response("/agentic/ontologies/{ontology_id}/data-source")


@business_data_bp.route("/data-connections/<connection_id>/default", methods=["POST"])
@business_data_bp.route("/data-connections/<connection_id>/test", methods=["POST"])
@require_role("admin")
def deprecated_data_connection_action_api(connection_id: str):
    del connection_id
    return _deprecated_response("/agentic/ontologies/{ontology_id}/data-source/test")


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["GET"])
@require_auth()
def deprecated_get_ontology_data_binding_api(ontology_id: str):
    return _deprecated_response("/agentic/ontologies/{}/data-source".format(ontology_id))


@business_data_bp.route("/ontologies/<ontology_id>/data-binding", methods=["PUT", "DELETE"])
@require_role("admin")
def deprecated_mutate_ontology_data_binding_api(ontology_id: str):
    return _deprecated_response("/agentic/ontologies/{}/data-source".format(ontology_id))


def _data_source_response(ontology_id: str, source):
    return {
        "ontology_id": ontology_id,
        "configured": source is not None,
        "source": publicize_ontology_data_source(source) if source is not None else None,
    }


@business_data_bp.route("/ontologies/<ontology_id>/data-source", methods=["GET"])
@require_auth()
def get_ontology_data_source_api(ontology_id: str):
    workspace_id, error_response = _resolve_workspace_or_response()
    if error_response:
        return error_response
    with workspace_scope(workspace_id):
        try:
            source = get_ontology_data_source(ontology_id, workspace_id=workspace_id)
        except OntologyDataSourceStoreError as exc:
            return jsonify({"detail": str(exc)}), 409
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
        try:
            delete_ontology_data_source(ontology_id, workspace_id=workspace_id)
        except OntologyDataSourceStoreError as exc:
            return jsonify({"detail": str(exc)}), 409
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
            connection = (
                source
                if isinstance(source, ResolvedBusinessDataSource)
                else ResolvedBusinessDataSource(
                    ontology_id=source.ontology_id,
                    mode=source.mode,
                    provider=source.provider,
                    config=dict(source.config),
                )
            )
            result = test_business_data_connection(connection)
        except (OntologyDataSourceStoreError, ValueError) as exc:
            return jsonify({"detail": str(exc)}), 400
    return jsonify({
        "success": bool(result.get("success")),
        "message": str(result.get("message") or ""),
        "tested_at": result.get("tested_at"),
    })
