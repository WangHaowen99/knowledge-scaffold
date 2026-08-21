"""Business data provider schema and ontology data source endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

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
from api.dependencies import (
    require_workspace_id,
    require_workspace_owner_or_admin_id,
    run_in_runtime_pool,
)
from api.request_parsing import read_optional_json_object


business_data_router = APIRouter(prefix="/agentic")


@business_data_router.get("/business-data/provider-schemas")
def list_business_data_provider_schemas_api(
    _workspace_id: str = Depends(require_workspace_id),
):
    return {"providers": list_provider_schemas()}


def _data_source_response(ontology_id: str, source):
    return {
        "ontology_id": ontology_id,
        "configured": source is not None,
        "source": publicize_ontology_data_source(source) if source is not None else None,
    }


@business_data_router.get("/ontologies/{ontology_id}/data-source")
async def get_ontology_data_source_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    try:
        source = await run_in_runtime_pool(
            request,
            "short_io",
            get_ontology_data_source,
            ontology_id,
            workspace_id=workspace_id,
        )
    except OntologyDataSourceStoreError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return _data_source_response(ontology_id, source)


@business_data_router.put("/ontologies/{ontology_id}/data-source")
async def set_ontology_data_source_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    payload = await read_optional_json_object(request)
    try:
        source = await run_in_runtime_pool(
            request,
            "short_io",
            set_ontology_data_source,
            ontology_id=ontology_id,
            data=payload,
            workspace_id=workspace_id,
        )
    except OntologyDataSourceStoreError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return _data_source_response(ontology_id, source)


@business_data_router.delete("/ontologies/{ontology_id}/data-source")
async def delete_ontology_data_source_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    try:
        await run_in_runtime_pool(
            request,
            "short_io",
            delete_ontology_data_source,
            ontology_id,
            workspace_id=workspace_id,
        )
    except OntologyDataSourceStoreError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return {
        "success": True,
        "ontology_id": ontology_id,
        "configured": False,
    }


def _resolve_test_data_source(
    *,
    ontology_id: str,
    workspace_id: str,
    payload: dict,
):
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
    return connection


@business_data_router.post("/ontologies/{ontology_id}/data-source/test")
async def test_ontology_data_source_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_owner_or_admin_id),
):
    payload = await read_optional_json_object(request)
    try:
        connection = await run_in_runtime_pool(
            request,
            "short_io",
            _resolve_test_data_source,
            ontology_id=ontology_id,
            workspace_id=workspace_id,
            payload=payload,
        )
        result = await run_in_runtime_pool(
            request,
            "external_io",
            test_business_data_connection,
            connection,
        )
    except (OntologyDataSourceStoreError, ValueError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {
        "success": bool(result.get("success")),
        "message": str(result.get("message") or ""),
        "tested_at": result.get("tested_at"),
    }
