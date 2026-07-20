"""Business data search tool backed by configurable ontology data providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.business_data.resolver import (
    BusinessDataConnectionError,
    resolve_business_data_source,
)
from agent.tools.business_data.service import (
    BusinessDataSearchRequest,
    query_business_data_stream,
)
from agent.tools.events import _tool_span_key
from utils.logging import get_logger
from ontology.ontology_catalog import get_ontology_manifest

logger = get_logger("tools.business_data_search")
BusinessDataSearchEventEmitter = Callable[[Dict[str, str]], None]

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
            "the previous attempt failed due to an explicit prerequisite, argument, or runtime error."
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
                        "The exact target entity/object key from the selected ontology's object definition. "
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
            if str(event.get("type") or "").strip().lower() == "final":
                final_content = str(event.get("content") or "").strip()
    except Exception as e:
        logger.exception("Unexpected error during database search: %s", str(e))
        return "Error: Unexpected error during database search."

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
        content = handle_business_data_search(
            **args,
            workspace_id=context.workspace_id,
            event_emitter=event_emitter,
            tool_call_id=tool_call_id,
            provider_runtime_context=_build_provider_runtime_context(
                context=context,
                tool_call_id=tool_call_id,
            ),
        )
        state_updates: Dict[str, Any] = {}
        entity_name = str(args.get("entity") or "").strip()
        if entity_name:
            state_updates["object_context_usage"] = [
                {
                    "entity_name": entity_name,
                    "iteration": int(context.agent_iteration or 0),
                }
            ]
        return ToolOutcome(
            content=content,
            state_updates=state_updates,
        )


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
