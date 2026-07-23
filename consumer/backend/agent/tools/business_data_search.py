"""Business data search tool backed by configurable ontology data providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.business_data.resolver import (
    BusinessDataConnectionError,
    ResolvedBusinessDataSource,
    resolve_business_data_source,
)
from agent.tools.business_data.service import (
    BusinessDataSearchRequest,
    query_business_data_stream,
)
from agent.tools.events import _tool_span_key
from utils.logging import get_logger
from ontology.ontology_catalog import (
    ObjectDataMappingStatus,
    ObjectReferenceStatus,
    find_exact_object_data_mapping_result,
    get_ontology_manifest,
    resolve_object_reference_result,
)

logger = get_logger("tools.business_data_search")
BusinessDataSearchEventEmitter = Callable[[Dict[str, str]], None]
_GRAPH_SCHEMA_FALLBACK_PROVIDERS = {"neo4j", "hugegraph"}

BUSINESS_DATA_SEARCH_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "business_data_search",
        "description": (
            "Query actual business records for a loaded ontology entity. "
            "Use this for concrete domain data. "
            "For reliable field and entity names, prefer loading the matching object definition with "
            "ontology_context(resource='object'). "
            "Use one entity per call unless querying a join table explicitly defined in object definitions. "
            "An empty result means no matching data by default; do not retry the same retrieval goal unless "
            "the previous attempt failed due to an explicit prerequisite, argument, or retryable runtime error. "
            "Never retry a result that explicitly contains retryable=false."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ontology_id": {
                    "type": "string",
                    "description": "The ontology id selected via ontology_context(root).",
                },
                "entity": {
                    "type": "string",
                    "description": (
                        "The target entity/object key, title, canonical key, or declared alias from the selected ontology. "
                        "The tool resolves it to the exact object key before querying. "
                        "Prefer reading the object via ontology_context(resource='object') before querying so "
                        "the query can use the correct entity and field names."
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "A precise search query using ontology entity names and "
                        "attribute names from the matched ontology's object definitions. Never guess fields. "
                        "Prefer a clear, near-natural-language sentence that explicitly states the target entity, "
                        "filter conditions, and requested fields. Generic template examples: "
                        "'查询[实体名]中[条件]的[字段]信息',"
                        "'统计[实体名]中满足[条件]的记录数量'. These are style references only, not a strict format. "
                        "If multiple values for the same attribute are needed, prefer one query that includes "
                        "all values so one search can return multiple matching records."
                    ),
                }
            },
            "required": ["ontology_id", "entity", "query"],
        },
    },
}


def handle_business_data_search(
    ontology_id: str,
    query: str,
    entity: Optional[str] = None,
    workspace_id: Optional[str] = None,
    event_emitter: Optional[BusinessDataSearchEventEmitter] = None,
    tool_call_id: Optional[str] = None,
    provider_runtime_context: Optional[Mapping[str, Any]] = None,
    _resolved_connection: Optional[ResolvedBusinessDataSource] = None,
) -> str:
    normalized_ontology_id = str(ontology_id or "").strip()
    if not normalized_ontology_id:
        return "Error: ontology_id cannot be empty"
    if str(workspace_id or "").strip():
        _ontology, ontology_error = get_ontology_manifest(
            normalized_ontology_id,
            workspace_id=workspace_id,
        )
    else:
        _ontology, ontology_error = get_ontology_manifest(normalized_ontology_id)
    if ontology_error:
        return ontology_error

    normalized_entity = str(entity or "").strip()
    if not query or not query.strip():
        return "Error: Search query cannot be empty"

    normalized_query = query.strip()
    connection = _resolved_connection
    if connection is None:
        try:
            connection = resolve_business_data_source(
                workspace_id=workspace_id,
                ontology_id=normalized_ontology_id,
            )
        except BusinessDataConnectionError as exc:
            return "Error: {}".format(str(exc))

    logger.info(
        "Sending business_data_search query: workspace_id=%s ontology_id=%s mode=%s provider=%s entity=%s query=%s (tool_call_id=%s)",
        workspace_id or "n/a",
        normalized_ontology_id,
        connection.mode,
        connection.provider,
        normalized_entity or "n/a",
        normalized_query[:100],
        tool_call_id or "n/a",
    )

    try:
        final_content = ""
        error_content = ""
        saw_final_event = False
        request = BusinessDataSearchRequest(
            workspace_id=str(workspace_id or ""),
            ontology_id=normalized_ontology_id,
            entity=normalized_entity,
            query=normalized_query,
            provider_runtime_context=dict(provider_runtime_context or {}),
        )
        for event in query_business_data_stream(
            request,
            connection=connection,
        ):
            _emit_business_data_search_event(
                event_emitter=event_emitter,
                event=event,
                tool_call_id=tool_call_id,
            )
            event_type = str(event.get("type") or "").strip().lower()
            event_content = str(event.get("content") or "").strip()
            if event_type == "final":
                saw_final_event = True
                final_content = event_content
            elif event_type == "error":
                error_content = event_content or "数据源查询失败"
    except Exception as e:
        logger.exception("Unexpected error during database search: %s", str(e))
        return "Error: Unexpected error during database search."

    if error_content:
        logger.warning(
            "Business data search failed: ontology_id=%s mode=%s provider=%s tool_call_id=%s error=%s",
            connection.ontology_id,
            connection.mode,
            connection.provider,
            tool_call_id or "n/a",
            error_content,
        )
        return "Error: 业务数据查询失败：{}".format(error_content)

    if not saw_final_event:
        logger.warning(
            "Business data search ended without a final result: ontology_id=%s mode=%s provider=%s tool_call_id=%s",
            connection.ontology_id,
            connection.mode,
            connection.provider,
            tool_call_id or "n/a",
        )
        return "Error: 业务数据查询失败：数据源未返回查询结果"

    logger.info(
        "Business data search completed: ontology_id=%s mode=%s provider=%s tool_call_id=%s result=%s",
        connection.ontology_id,
        connection.mode,
        connection.provider,
        tool_call_id or "n/a",
        final_content,
    )
    return final_content


def _emit_business_data_search_event(
    *,
    event_emitter: Optional[BusinessDataSearchEventEmitter],
    event: Dict[str, str],
    tool_call_id: Optional[str],
) -> None:
    if not event_emitter:
        return

    try:
        event_emitter(event)
    except Exception:
        logger.warning(
            "Failed to emit business_data_search event (tool_call_id=%s)",
            tool_call_id or "n/a",
            exc_info=True,
        )


def _terminal_graph_timeout_error(content: str) -> Optional[Dict[str, str]]:
    try:
        payload = json.loads(str(content or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping) or payload.get("retryable") is not False:
        return None
    error_code = str(payload.get("error_code") or "").strip()
    if error_code not in {
        "GRAPH_AGENT_TIMEOUT",
        "GRAPH_BACKEND_TIMEOUT",
    }:
        return None
    return {
        "error_code": error_code,
        "message": "Graph query timed out.",
        "tool_name": "business_data_search",
    }


@dataclass(frozen=True)
class BusinessDataSearchTool:
    name: str = "business_data_search"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = True
    max_parallelism: Optional[int] = 5
    category: str = "business_data_search"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", BUSINESS_DATA_SEARCH_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        normalized_args = dict(args)
        normalized_args.pop("_resolved_connection", None)
        normalized_entity = str(normalized_args.get("entity") or "").strip()
        normalized_query = str(normalized_args.get("query") or "").strip()
        if not normalized_entity:
            return ToolOutcome(content="Error: entity cannot be empty")
        if not normalized_query:
            return ToolOutcome(content="Error: Search query cannot be empty")
        normalized_args["entity"] = normalized_entity
        normalized_args["query"] = normalized_query

        ontology, ontology_error = get_ontology_manifest(
            str(normalized_args.get("ontology_id") or ""),
            workspace_id=context.workspace_id,
        )
        if ontology_error:
            return ToolOutcome(content=ontology_error)
        assert ontology is not None
        graph_fallback_connection: Optional[ResolvedBusinessDataSource] = None
        reference_result = resolve_object_reference_result(
            Path(ontology.ontology_dir).resolve(),
            normalized_entity,
        )
        resolution = reference_result.resolution
        resolution_error = reference_result.error
        if reference_result.status != ObjectReferenceStatus.RESOLVED:
            if reference_result.status not in {
                ObjectReferenceStatus.NOT_FOUND,
                ObjectReferenceStatus.MISSING_DIRECTORY,
            }:
                return ToolOutcome(content=resolution_error)
            ontology_dir = Path(ontology.ontology_dir).resolve()
            mapping_result = find_exact_object_data_mapping_result(
                ontology_dir,
                normalized_entity,
            )
            if mapping_result.status == ObjectDataMappingStatus.INVALID:
                return ToolOutcome(content=mapping_result.error)
            has_exact_mapping = mapping_result.status == ObjectDataMappingStatus.FOUND
            if not has_exact_mapping:
                graph_fallback_connection = (
                    _resolve_graph_schema_fallback_connection(
                        workspace_id=context.workspace_id,
                        ontology_id=str(normalized_args.get("ontology_id") or ""),
                    )
                )
                if graph_fallback_connection is None:
                    return ToolOutcome(content=resolution_error)
        resolved_entity = (
            resolution.object_key if resolution is not None else normalized_entity
        )
        normalized_args["entity"] = resolved_entity
        if graph_fallback_connection is not None:
            normalized_args["_resolved_connection"] = graph_fallback_connection

        content = handle_business_data_search(
            **normalized_args,
            workspace_id=context.workspace_id,
            event_emitter=event_emitter,
            tool_call_id=tool_call_id,
            provider_runtime_context=_build_provider_runtime_context(
                context=context,
                tool_call_id=tool_call_id,
            ),
        )
        state_updates: Dict[str, Any] = {}
        entity_name = resolved_entity if graph_fallback_connection is None else ""
        if entity_name:
            state_updates["object_context_usage"] = [
                {
                    "entity_name": entity_name,
                    "iteration": int(context.agent_iteration or 0),
                }
            ]
        terminal_error = _terminal_graph_timeout_error(content)
        if terminal_error is not None:
            state_updates["terminal_error"] = terminal_error
        return ToolOutcome(
            content=content,
            state_updates=state_updates,
            terminal=terminal_error is not None,
        )


def _resolve_graph_schema_fallback_connection(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
) -> Optional[ResolvedBusinessDataSource]:
    try:
        connection = resolve_business_data_source(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
    except BusinessDataConnectionError:
        return None
    if connection.provider not in _GRAPH_SCHEMA_FALLBACK_PROVIDERS:
        return None
    return connection


def _build_provider_runtime_context(
    *,
    context: ToolContext,
    tool_call_id: Optional[str],
) -> Dict[str, Any]:
    return {
        "run_id": context.run_id,
        "session_id": context.session_id,
        "turn_id": context.turn_id,
        "parent_span_key": _tool_span_key(
            context,
            {
                "tool_name": "business_data_search",
                "tool_call_id": tool_call_id,
            },
        ),
        "agent_iteration": context.agent_iteration,
        "model_profile_id": context.model_profile_id,
        "_observer": context.observer,
    }
