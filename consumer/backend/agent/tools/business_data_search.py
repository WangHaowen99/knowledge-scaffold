"""Business data search tool backed by configurable ontology data providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Set, Tuple

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
from agent.tools.business_data.value_lookup import execute_value_lookups
from agent.tools.events import _tool_span_key
from utils.logging import get_logger
from ontology.ontology_catalog import (
    ObjectDataMappingStatus,
    ObjectReferenceStatus,
    find_exact_object_data_mapping_result,
    find_field_alias_matches,
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
            "Query concrete domain data or resolve value-domain mappings for a loaded ontology "
            "entity. For reliable field and entity names, prefer loading the matching object "
            "definition with ontology_context(resource='object'). When ontology_context marks a "
            "field with value_bound: true, that field has a value-domain mapping table: if the "
            "user asks for the semantic name, business meaning, or aliases of such a field's "
            "values, call action=\"lookup_values\" and answer strictly from the returned table — "
            "the table may hold values that contradict training knowledge, so do not infer from "
            "object field definitions or model memory. Use one entity per call unless querying a "
            "join table explicitly defined in object definitions. An empty result means no matching "
            "data by default; do not retry the same retrieval goal unless the previous attempt "
            "failed due to an explicit prerequisite, argument, or retryable runtime error. Never "
            "retry a result that explicitly contains retryable=false."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "ontology_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The ontology id selected via ontology_context(root).",
                },
                "entity": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "The target entity/object key, title, canonical key, or declared alias from the selected ontology. "
                        "The tool resolves it to the exact object key before querying. "
                        "Prefer reading the object via ontology_context(resource='object') before querying so "
                        "the query can use the correct entity and field names."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["query", "lookup_values"],
                    "default": "query",
                    "description": (
                        "Use \"query\" (default) to query records from the selected business "
                        "object. Use \"lookup_values\" to resolve the semantic name, business "
                        "meaning, or aliases of values (see the top-level description for when "
                        "to choose lookup_values). Omitting action means \"query\". Never "
                        "combine the two actions in one call."
                    ),
                },
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "A precise search query using ontology entity names and "
                        "attribute names from the matched ontology's object definitions. Never guess fields. "
                        "Prefer a clear, near-natural-language sentence that explicitly states the target entity, "
                        "filter conditions, and requested fields. Generic template examples: "
                        "'查询[实体名]中[条件]的[字段]信息',"
                        "'统计[实体名]中满足[条件]的记录数量'. These are style references only, not a strict format. "
                        "If multiple values for the same attribute are needed, prefer one query that includes "
                        "all values so one search can return multiple matching records. For action=\"lookup_values\", "
                        "express the concrete value-domain request for the submitted values. The Consumer preserves "
                        "this text and appends only the validated table, query fields, and values for each binding group."
                    ),
                },
                "value_inputs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "description": (
                        "Required only for action=\"lookup_values\". Submit unresolved raw user "
                        "or result values under the value-bound logical fields of entity (the "
                        "fields marked value_bound: true in the object definition) so the "
                        "Consumer can resolve and group Data Mapping bindings. Batch values that "
                        "share one field or binding. Reuse successful lookup results already "
                        "present in the current run and submit only unresolved values. Do not "
                        "include value_inputs for action=\"query\"."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["field_key", "values"],
                        "properties": {
                            "field_key": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 128,
                            },
                            "values": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 32,
                                "items": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 256,
                                },
                                "uniqueItems": True,
                            },
                        },
                    },
                },
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
    _field_alias_mappings: Optional[Tuple[Tuple[str, str], ...]] = None,
    *,
    ontology_version: Optional[str] = None,
) -> str:
    normalized_ontology_id = str(ontology_id or "").strip()
    if not normalized_ontology_id:
        return "Error: ontology_id cannot be empty"
    manifest_kwargs = {}
    if str(ontology_version or "").strip():
        manifest_kwargs["version"] = ontology_version
    if str(workspace_id or "").strip():
        _ontology, ontology_error = get_ontology_manifest(
            normalized_ontology_id,
            **manifest_kwargs,
            workspace_id=workspace_id,
        )
    else:
        _ontology, ontology_error = get_ontology_manifest(
            normalized_ontology_id,
            **manifest_kwargs,
        )
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
            field_alias_mappings=tuple(_field_alias_mappings or ()),
            ontology_version=str(ontology_version or "").strip(),
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
                ontology_id=connection.ontology_id,
                provider=connection.provider,
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
    ontology_id: str,
    provider: str,
) -> None:
    if not event_emitter:
        return

    try:
        event_emitter({
            **event,
            "ontology_id": str(ontology_id or "").strip(),
            "provider": str(provider or "").strip(),
        })
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
        if any(str(key).startswith("_") for key in normalized_args):
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: internal arguments are not allowed"
            )
        public_arguments = {
            "ontology_id",
            "entity",
            "action",
            "query",
            "value_inputs",
        }
        if any(key not in public_arguments for key in normalized_args):
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: unexpected arguments are not allowed"
            )
        # action and value_inputs are consumed here; they must not leak into
        # handle_business_data_search which only accepts query-path parameters.
        action_present = "action" in normalized_args
        raw_action = normalized_args.pop("action", None)
        value_inputs = normalized_args.pop("value_inputs", None)
        if action_present and not isinstance(raw_action, str):
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: action must be 'query' or 'lookup_values'"
            )
        action = str(raw_action if action_present else "query").strip().lower()
        # Only a genuine non-empty list counts as "value_inputs provided".  Some
        # providers inject null / [] defaults for optional parameters; those are
        # treated as omitted so they do not trip the combination check below.
        has_value_inputs = isinstance(value_inputs, list) and len(value_inputs) > 0

        normalized_entity = str(normalized_args.get("entity") or "").strip()
        normalized_query = str(normalized_args.get("query") or "").strip()
        if not normalized_entity:
            return ToolOutcome(content="Error: entity cannot be empty")
        if not normalized_query:
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: query is required"
            )
        normalized_args["entity"] = normalized_entity
        normalized_args["query"] = normalized_query

        # action / parameter-combination validation (before any Provider call)
        if action not in ("query", "lookup_values"):
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: action must be 'query' or 'lookup_values'"
            )
        if has_value_inputs and action != "lookup_values":
            return ToolOutcome(
                content=(
                    "Error: INVALID_ACTION_ARGUMENTS: value_inputs requires "
                    "action='lookup_values'. Set action='lookup_values' to "
                    "resolve canonical values, business meanings, or aliases "
                    "of unresolved values, or remove value_inputs to perform a "
                    "business data query."
                )
            )
        if action == "lookup_values" and not has_value_inputs:
            return ToolOutcome(
                content="Error: INVALID_ACTION_ARGUMENTS: value_inputs is required with action='lookup_values'"
            )

        context_selected_ontology_id = str(getattr(context, "selected_ontology_id", None) or "").strip()
        context_selected_ontology_version = str(getattr(context, "selected_ontology_version", None) or "").strip()
        manifest_kwargs = {"workspace_id": context.workspace_id}
        if (
            str(normalized_args.get("ontology_id") or "").strip() == context_selected_ontology_id
            and context_selected_ontology_version
        ):
            manifest_kwargs["version"] = context_selected_ontology_version
        ontology, ontology_error = get_ontology_manifest(
            str(normalized_args.get("ontology_id") or ""),
            **manifest_kwargs,
        )
        if ontology_error:
            return ToolOutcome(content=ontology_error)
        assert ontology is not None
        ontology_dir = Path(ontology.ontology_dir).resolve()
        graph_fallback_connection: Optional[ResolvedBusinessDataSource] = None
        reference_result = resolve_object_reference_result(
            ontology_dir,
            normalized_entity,
        )
        resolution = reference_result.resolution
        resolution_error = reference_result.error
        if reference_result.status != ObjectReferenceStatus.RESOLVED:
            # lookup_values requires a real Object definition as the field
            # boundary; a Data Mapping alone is not sufficient.  Reject before
            # the data-mapping fallback so value-domain lookups cannot proceed
            # against resources that have no Object definition.
            if action == "lookup_values":
                return ToolOutcome(content=resolution_error)
            if reference_result.status not in {
                ObjectReferenceStatus.NOT_FOUND,
                ObjectReferenceStatus.MISSING_DIRECTORY,
            }:
                return ToolOutcome(content=resolution_error)
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

        # -- lookup_values path ------------------------------------------------
        if action == "lookup_values":
            try:
                connection = resolve_business_data_source(
                    workspace_id=context.workspace_id,
                    ontology_id=str(normalized_args.get("ontology_id") or ""),
                )
            except BusinessDataConnectionError as exc:
                return ToolOutcome(content="Error: {}".format(str(exc)))
            response = execute_value_lookups(
                ontology_dir=ontology_dir,
                object_key=resolved_entity,
                model_query=normalized_query,
                value_inputs=value_inputs,
                connection=connection,
                workspace_id=str(context.workspace_id or ""),
                ontology_id=str(normalized_args.get("ontology_id") or ""),
                ontology_version=(
                    context_selected_ontology_version
                    if str(normalized_args.get("ontology_id") or "").strip() == context_selected_ontology_id
                    else ""
                ),
                event_emitter=event_emitter,
                tool_call_id=tool_call_id,
                provider_runtime_context=_build_provider_runtime_context(
                    context=context,
                    tool_call_id=tool_call_id,
                ),
            )
            return ToolOutcome(content=response.to_json())

        # -- query path (unchanged) -------------------------------------------
        field_alias_mappings: Tuple[Tuple[str, str], ...] = ()
        resolution_path = getattr(resolution, "path", None)
        if resolution_path is not None:
            field_alias_mappings = find_field_alias_matches(
                Path(resolution_path),
                normalized_query,
            )
        normalized_args["_field_alias_mappings"] = field_alias_mappings
        if graph_fallback_connection is not None:
            normalized_args["_resolved_connection"] = graph_fallback_connection

        content = handle_business_data_search(
            **normalized_args,
            ontology_version=(
                context_selected_ontology_version
                if str(normalized_args.get("ontology_id") or "").strip() == context_selected_ontology_id
                else None
            ),
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
        if entity_name and _business_query_succeeded(content):
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


_SUCCESS_FLAG_KEYS = ("ok", "_success", "success")
_ERROR_FLAG_KEYS = ("error", "_error")


def _has_explicit_failure_signal(parsed: Mapping) -> bool:
    """Return True when a parsed JSON payload carries an explicit failure marker.

    ``ok``/``_success``/``success`` are success flags, so they use identity
    comparison against ``False`` (a missing key or ``0`` is not a failure).
    ``error``/``_error`` are error flags, so any truthy value is a failure.
    """
    if any(parsed.get(key) for key in _ERROR_FLAG_KEYS):
        return True
    return any(parsed.get(key) is False for key in _SUCCESS_FLAG_KEYS)


def _business_query_succeeded(content: str) -> bool:
    text = str(content or "").strip()
    if not text or text.lower().startswith("error:"):
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return True
    if not isinstance(parsed, Mapping):
        return True
    if _has_explicit_failure_signal(parsed):
        return False
    return str(parsed.get("status") or "").strip().lower() not in {
        "error",
        "failed",
    }


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
        "run_id": getattr(context, "run_id", None),
        "session_id": getattr(context, "session_id", None),
        "turn_id": getattr(context, "turn_id", None),
        "parent_span_key": _tool_span_key(
            context,
            {
                "tool_name": "business_data_search",
                "tool_call_id": tool_call_id,
            },
        ),
        "agent_iteration": getattr(context, "agent_iteration", None),
        "model_profile_id": getattr(context, "model_profile_id", None),
        "selected_ontology_version": getattr(context, "selected_ontology_version", None),
        "_observer": getattr(context, "observer", None),
    }
