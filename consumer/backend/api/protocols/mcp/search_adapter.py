"""MCP search adapter backed by GraphToolExecutor."""

from __future__ import annotations

from typing import Any, Mapping

from agent.tools.business_data.resolver import BusinessDataConnectionError, resolve_business_data_source
from agent.tools.business_data.providers.graph.backends.hugegraph import HugeGraphBackend
from agent.tools.business_data.providers.graph.backends.neo4j import Neo4jGraphBackend
from agent.tools.business_data.providers.graph.mapping import GraphMappingResolver
from agent.tools.business_data.providers.graph.models import GraphEntityMapping, GraphSearchRuntimeContext
from agent.tools.business_data.providers.graph.scope import GraphScopeError, build_graph_scope
from agent.tools.business_data.providers.graph.tools.executor import GraphToolExecutor, GraphToolInputError
from ontology.ontology_catalog import get_ontology_manifest
from workspace.workspaces import get_workspace, get_workspace_paths, workspace_scope
from .schemas import error_result
from integration.mcp.scope import MCPRequestScope
from integration.mcp.ontology_binding import (
    MCPOntologyBindingError,
    resolve_effective_ontology_id,
    validate_runtime_ontology_availability,
)
from .tool_contracts import SEARCH_TOOL_NAMES, list_search_tools, normalize_search_tool_args, ToolInputIssue

UNSUPPORTED_TOOLS: set[str] = set()


def execute_search_tool(tool_name: str, args: Mapping[str, Any] | None, *, scope: MCPRequestScope) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    if name in UNSUPPORTED_TOOLS:
        return error_result(
            error_type="unsupported_tool",
            error_code="UNSUPPORTED_TOOL",
            detail="P0 暂不支持 MCP search 工具: {}".format(name),
            retriable=False,
        )
    if name not in SEARCH_TOOL_NAMES:
        return error_result(
            error_type="tool_input_error",
            error_code="UNKNOWN_TOOL",
            detail="未知 MCP search 工具: {}".format(name or "<empty>"),
            retriable=False,
        )

    raw_args = dict(args or {})
    requested_ontology_id = raw_args.pop("ontology_id", None)
    entity = str(raw_args.pop("entity", "") or "").strip()
    query = str(raw_args.pop("query", "") or "").strip()
    try:
        ontology_id = resolve_effective_ontology_id(
            token_ontology_id=scope.ontology_id,
            requested_ontology_id=requested_ontology_id,
        )
    except MCPOntologyBindingError as exc:
        return error_result(
            error_type=exc.error_type,
            error_code=exc.code,
            detail=exc.detail,
            retriable=False,
        )
    normalized_args, input_issue = normalize_search_tool_args(name, raw_args)
    if input_issue:
        return _tool_input_error(input_issue)

    try:
        with workspace_scope(scope.workspace_id):
            if scope.ontology_id:
                validate_runtime_ontology_availability(
                    workspace_id=scope.workspace_id,
                    ontology_id=ontology_id,
                )
            runtime = _build_runtime(scope=scope, ontology_id=ontology_id, entity=entity, query=query)
            try:
                result = _execute_with_runtime(name, normalized_args or {}, runtime)
            finally:
                runtime.close()
    except MCPOntologyBindingError as exc:
        return error_result(
            error_type=exc.error_type,
            error_code=exc.code,
            detail=exc.detail,
            retriable=False,
        )
    except (BusinessDataConnectionError, GraphScopeError) as exc:
        return error_result(
            error_type="configuration_error",
            error_code="MCP_SEARCH_CONFIGURATION_ERROR",
            detail=str(exc),
            retriable=False,
        )
    except GraphToolInputError as exc:
        return error_result(
            error_type="tool_input_error",
            error_code=getattr(exc, "error_code", "GRAPH_TOOL_INPUT_ERROR"),
            detail=str(exc),
            retriable=getattr(exc, "retryable", True),
        )
    except Exception as exc:
        return error_result(
            error_type="backend_error",
            error_code="MCP_SEARCH_UNEXPECTED_ERROR",
            detail=str(exc),
            retriable=True,
        )

    payload = _public_tool_payload(
        result,
        scope=scope,
        provider=runtime.provider,
        ontology_id=ontology_id,
    )
    return payload


def _tool_input_error(issue: ToolInputIssue) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "type": "tool_input_error",
            "error_code": issue.error_code,
            "field": issue.field,
            "detail": issue.detail,
            "hint": issue.hint,
            "retriable": True,
        },
    }


class _SearchRuntimeBundle:
    def __init__(
        self,
        *,
        provider: str,
        backend: Any,
        context: GraphSearchRuntimeContext,
        mapping: GraphEntityMapping,
    ) -> None:
        self.provider = provider
        self.backend = backend
        self.context = context
        self.mapping = mapping

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()


def _build_runtime(*, scope: MCPRequestScope, ontology_id: str, entity: str, query: str) -> _SearchRuntimeBundle:
    connection = resolve_business_data_source(
        workspace_id=scope.workspace_id,
        ontology_id=ontology_id,
    )
    ontology_manifest, ontology_error = get_ontology_manifest(
        ontology_id,
        workspace_id=scope.workspace_id,
    )
    if ontology_manifest is None:
        raise BusinessDataConnectionError(
            str(ontology_error or "Ontology '{}' not found".format(ontology_id)).replace("Error: ", "")
        )
    ontology_version = str(
        getattr(ontology_manifest, "version", "")
        or getattr(ontology_manifest, "active_version", "")
        or ""
    ).strip()
    provider = str(connection.provider or "").strip().lower()
    if provider not in {"neo4j", "hugegraph"}:
        raise BusinessDataConnectionError("MCP Search 仅支持 neo4j 或 hugegraph provider")
    workspace_paths = get_workspace_paths(scope.workspace_id)
    workspace_name = ""
    try:
        workspace_name = str(get_workspace(scope.workspace_id).get("name") or "").strip()
    except Exception:
        workspace_name = ""
    provider_config = {
        **dict(connection.config),
        "_workspace_root": str(workspace_paths.ontologies_dir),
        "workspace_id": scope.workspace_id,
        "workspace_name": workspace_name,
        "ontology_id": ontology_id,
        "ontology_version": ontology_version,
    }
    backend = Neo4jGraphBackend(provider_config) if provider == "neo4j" else HugeGraphBackend(provider_config)
    graph_scope = build_graph_scope(provider_config)
    mapping = _resolve_mapping(
        workspace_root=str(workspace_paths.ontologies_dir),
        ontology_id=ontology_id,
        ontology_version=ontology_version,
        provider=provider,
        entity=entity,
    )
    context = GraphSearchRuntimeContext(
        provider=provider,
        workspace_id=scope.workspace_id,
        ontology_id=ontology_id,
        ontology_version=ontology_version,
        workspace_root=str(workspace_paths.ontologies_dir),
        entity=entity,
        query=query,
        timeout_seconds=int(provider_config.get("timeout_seconds") or 60),
        max_result_rows=int(provider_config.get("max_result_rows") or 100),
        scope=graph_scope,
        mapping=mapping,
        provider_config=provider_config,
        schema_context={},
    )
    return _SearchRuntimeBundle(provider=provider, backend=backend, context=context, mapping=mapping)


def _resolve_mapping(
    *,
    workspace_root: str,
    ontology_id: str,
    ontology_version: str = "",
    provider: str,
    entity: str,
) -> GraphEntityMapping:
    if entity:
        resolver = GraphMappingResolver(
            workspace_root=workspace_root,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            provider=provider,
        )
        mapping = resolver.resolve(entity)
        if mapping is not None:
            return mapping
    return GraphEntityMapping(
        entity=entity or "_mcp_explicit_tool_args",
        kind="node",
        source="mcp_explicit_tool_args",
        label=entity,
    )


def _execute_with_runtime(tool_name: str, args: dict[str, Any], runtime: _SearchRuntimeBundle):
    executor = GraphToolExecutor(
        backend=runtime.backend,
        context=runtime.context,
        mapping=runtime.mapping,
    )
    if tool_name == "get_object_types":
        execution = executor.execute("get_nodes_schema", {})
        result = dict(execution.payload.get("result") or {})
        labels = result.get("labels") if isinstance(result, dict) else []
        execution.payload["result"] = {"items": labels or [], "total": len(labels or [])}
        execution.tool_args["mcp_alias"] = "get_object_types"
        return execution
    if tool_name == "get_relation_types":
        execution = executor.execute("get_edges_schema", {})
        result = dict(execution.payload.get("result") or {})
        relation_types = result.get("relationship_types") if isinstance(result, dict) else []
        execution.payload["result"] = {"items": relation_types or [], "total": len(relation_types or [])}
        execution.tool_args["mcp_alias"] = "get_relation_types"
        return execution
    return executor.execute(tool_name, args)


def _public_tool_payload(execution, *, scope: MCPRequestScope, provider: str, ontology_id: str) -> dict[str, Any]:
    payload = dict(execution.payload or {})
    result = payload.get("result")
    # Some MCP-facing tools are compatibility aliases implemented by a
    # different internal graph tool (for example, get_relation_types uses
    # get_edges_schema to obtain the relationship schema).  Keep the public
    # response aligned with the tool requested by the client while retaining
    # the internal execution name for diagnostics.
    execution_tool_args = getattr(execution, "tool_args", {})
    tool_args = execution_tool_args if isinstance(execution_tool_args, Mapping) else {}
    internal_tool_name = str(execution.tool_name or "")
    alias = str(tool_args.get("mcp_alias") or "").strip()
    allowed_aliases = {
        "get_nodes_schema": {"get_object_types"},
        "get_edges_schema": {"get_relation_types"},
    }
    public_tool_name = (
        alias
        if alias in allowed_aliases.get(internal_tool_name, set())
        else internal_tool_name
    )
    response = {
        "ok": bool(payload.get("success", True)),
        "result": result,
        "message": payload.get("message") or {"description": "查询成功"},
        "scope": {
            "username": scope.username,
            "workspace_id": scope.workspace_id,
        },
        "upstream": {
            "provider": provider,
            "ontology_id": ontology_id,
        },
        "tool": public_tool_name,
    }
    if "warnings" in payload:
        response["warnings"] = payload["warnings"]
    backend_result = execution.backend_result
    if backend_result is not None:
        response["record_count"] = (
            backend_result.total
            if backend_result.total is not None
            else len(backend_result.records or [])
        )
        response["truncated"] = bool(backend_result.truncated)
    return response
