"""Unified model-facing memory capture tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set

from agent.runtime.async_utils import run_sync_compat
from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.evolution import append_operation_proposal_with_relations, get_proposal_buffer
from agent.memory.models import MEMORY_TYPE_TO_DIR
from llm import LLMPurpose, get_llm_router
from utils.json_utils import _loads_json_object_loose
from utils.logging import get_logger
from utils.read_docs import normalize_read_documents_registry
from utils.security.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from agent.memory.protocol import MemoryManager

logger = get_logger("tools.memory")

_MEMORY_TYPES = set(MEMORY_TYPE_TO_DIR)
_EVOLUTION_KINDS = {"procedure", "pitfall", "schema"}

_EXPLICIT_MEMORY_PATTERNS = (
    re.compile(r"(?:请|帮我|麻烦)?(?:记住|记下|记一下|记下来|保存到记忆|存到记忆)"),
    re.compile(r"(?:以后|今后|后续|下次).{0,16}(?:一直|都|继续|记得|遵循|使用|采用|按照)"),
    re.compile(r"\bremember\s+(?:that|to|this|my)\b", re.IGNORECASE),
    re.compile(r"\b(?:save|store)\s+this\s+(?:for|in)\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+now\s+on\b", re.IGNORECASE),
)
_NEGATED_MEMORY_PATTERN = re.compile(
    r"(?:不要|别|无需|不用|不必).{0,8}(?:记住|记忆|保存)|"
    r"\b(?:do\s+not|don't|dont|never)\s+(?:remember|save|store)\b",
    re.IGNORECASE,
)

MEMORY_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Capture an explicit user-provided memory item for future use. "
            "Call when the user asks the system to remember, keep, follow later, "
            "reuse, or correct durable information. Pass the memory content as "
            "close to the user's wording as possible. The service stores the item "
            "in the appropriate durable subsystem."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The user-provided content to preserve for future use.",
                }
            },
            "required": ["content"],
        },
    },
}


_ROUTER_SYSTEM_PROMPT = """\
You route one user-provided memory item into exactly one durable subsystem.

Return a single JSON object only.

Destinations:
- ordinary_memory: durable user response preference or non-sensitive user profile detail.
- operation_evolution: reusable ontology operation procedure, workflow rule, pitfall, or entity schema correction.

Schema:
{
  "destination": "ordinary_memory" | "operation_evolution",
  "reason": "string"
}

"""


_ORDINARY_EXTRACTOR_SYSTEM_PROMPT = """\
Extract a user-personalization memory item.

Return a single JSON object only.

Schema:
{
  "content": "string",
  "memory_type": "preference" | "profile",
  "reason": "string"
}

Rules:
- Use preference for durable response, workflow, language, or format preferences.
- Use profile only when the content states a stable, non-sensitive fact about the user, their team, or their long-term work context.
- Preserve the user's wording closely, but make the content self-contained.
"""


_OPERATION_EXTRACTOR_SYSTEM_PROMPT = """\
Extract a reusable ontology operation memory item.

Return a single JSON object only.

Schema:
{
  "content": "string",
  "kind": "procedure" | "pitfall" | "schema",
  "ontology_id": "string or null",
  "operation_key": "string or null",
  "entity_key": "string or null",
  "reason": "string"
}

Use the provided execution context to fill ontology_id, operation_key, and entity_key when there is exactly one clear target.
For operation_evolution:
- procedure: reusable steps or future execution rules.
- pitfall: mistakes, warnings, edge cases, or checks to avoid repeat failures.
- schema: entity field names, formats, meanings, or mapping corrections.

Scope rules:
- procedure and pitfall are operation-scoped when a clear operation exists; leave operation_key null for ontology-level guidance.
- schema may be object-level; set operation_key to null when the schema correction is a generally valid fact about the entity across operations.
- schema should include entity_key whenever the target entity is clear.
- Only set operation_key for schema when the user explicitly limits the correction to a specific operation or the correction depends on that operation's context.
"""

_ORDINARY_RELATION_SYSTEM_PROMPT = """\
Compare one new ordinary user-personalization memory item with existing ordinary memories.

Return a single JSON object only.

Schema:
{
  "decisions": [
    {
      "source_uri": "string",
      "relation": "duplicate" | "supersedes" | "refines" | "conflicts" | "unrelated",
      "reason": "string"
    }
  ]
}

Relation rules:
- duplicate: the new item says the same durable user preference or profile fact as the old item.
- supersedes: the new item explicitly replaces the old item for the same subject.
- refines: the new item is a more specific version that should replace the old item in recall.
- conflicts: both items cannot safely be true at the same time, and the new explicit memory should win.
- unrelated: both can coexist.
- A broad default and a narrower exception can coexist; do not replace the default unless the user says the default changed.

Examples:
- "Use English when answering handling-cost questions" conflicts with "Use Chinese when answering handling-cost questions".

Return one decision for every provided candidate source_uri.
"""


@dataclass(frozen=True)
class _MemoryTaskContext:
    memory_manager: Optional["MemoryManager"]
    session_id: Optional[str]
    run_id: Optional[str]
    workspace_id: Optional[str]
    selected_ontology_id: Optional[str]
    read_documents: Optional[Dict[str, Any]]


def _strip_json_fence(text: str) -> str:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    return candidate.strip()


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _latest_user_request(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(list(messages or [])):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") in {None, "text"}
            )
    return ""


def has_explicit_memory_request(messages: List[Dict[str, Any]]) -> bool:
    request = _latest_user_request(messages)
    if not request or _NEGATED_MEMORY_PATTERN.search(request):
        return False
    return any(pattern.search(request) for pattern in _EXPLICIT_MEMORY_PATTERNS)


_has_explicit_memory_request = has_explicit_memory_request


def _recent_read_context(read_documents: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    registry = normalize_read_documents_registry(read_documents)
    rows: List[Dict[str, Any]] = []
    for path in registry["recent_paths"][-8:]:
        record = dict(registry["by_path"].get(path) or {})
        if not record:
            continue
        doc_type = str(record.get("doc_type") or "").strip()
        if doc_type not in {"operation_doc", "object_doc"}:
            continue
        rows.append(
            {
                "ontology_id": record.get("ontology_id"),
                "operation_key": record.get("operation_name"),
                "entity_key": record.get("entity_name"),
                "description": record.get("description"),
            }
        )
    return rows


def _infer_unique_context_value(rows: List[Dict[str, Any]], key: str) -> Optional[str]:
    values = {
        _safe_str(row.get(key))
        for row in rows
        if _safe_str(row.get(key))
    }
    if len(values) == 1:
        return next(iter(values))
    return None


def _build_operation_extractor_input(
    *,
    content: str,
    context: _MemoryTaskContext,
) -> str:
    read_context = _recent_read_context(context.read_documents)
    selected_ontology_id = _safe_str(context.selected_ontology_id)
    inferred_ontology_id = _infer_unique_context_value(read_context, "ontology_id")
    ontology_line = "当前本体：未明确"
    if selected_ontology_id:
        ontology_line = "用户选取的本体：{}".format(selected_ontology_id)
    elif inferred_ontology_id:
        ontology_line = "模型选取的本体：{}".format(inferred_ontology_id)

    operation_lines = _format_context_items(
        read_context,
        key="operation_key",
        empty_text="无",
    )
    entity_lines = _format_context_items(
        read_context,
        key="entity_key",
        empty_text="无",
    )
    return "\n".join(
        [
            "原始记忆内容：{}".format(content),
            "",
            ontology_line,
            "",
            "已读取流程：",
            operation_lines,
            "",
            "已读取实体：",
            entity_lines,
        ]
    )


def _format_context_items(rows: List[Dict[str, Any]], *, key: str, empty_text: str) -> str:
    seen: Set[str] = set()
    lines: List[str] = []
    for row in rows:
        name = _safe_str(row.get(key))
        if not name or name in seen:
            continue
        seen.add(name)
        description = _safe_str(row.get("description"))
        if description:
            lines.append("- 名称：{}；描述：{}".format(name, description))
        else:
            lines.append("- 名称：{}".format(name))
    return "\n".join(lines) if lines else empty_text


def _parse_memory_payload(raw: str) -> Dict[str, Any]:
    text = _strip_json_fence(raw)
    parsed = _loads_json_object_loose(text)
    if parsed is not None:
        return parsed
    return json.loads(text)


def _call_memory_llm(
    *,
    system_prompt: str,
    payload: Any,
    max_tokens: int,
    request_label: str,
) -> Dict[str, Any]:
    user_content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    response = get_llm_router().call(
        purpose=LLMPurpose.MEMORY,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        tools=[],
        stream=False,
        temperature=0,
        max_tokens=max_tokens,
        enable_reasoning=False,
        run_id=None,
        request_label=request_label,
    )
    return _parse_memory_payload(response.content)


def _route_memory_destination(
    *,
    content: str,
) -> Dict[str, Any]:
    return _call_memory_llm(
        system_prompt=_ROUTER_SYSTEM_PROMPT,
        payload={"content": content},
        max_tokens=250,
        request_label="memory_route",
    )


def _extract_ordinary_memory(
    *,
    content: str,
) -> Dict[str, Any]:
    return _call_memory_llm(
        system_prompt=_ORDINARY_EXTRACTOR_SYSTEM_PROMPT,
        payload={"content": content},
        max_tokens=500,
        request_label="memory_extract_ordinary",
    )


def _extract_operation_memory(
    *,
    content: str,
    context: _MemoryTaskContext,
) -> Dict[str, Any]:
    operation_input = _build_operation_extractor_input(
        content=content,
        context=context,
    )
    return _call_memory_llm(
        system_prompt=_OPERATION_EXTRACTOR_SYSTEM_PROMPT,
        payload=operation_input,
        max_tokens=700,
        request_label="memory_extract_operation",
    )


def _ordinary_relation_candidates(
    *,
    memory_manager: "MemoryManager",
    content: str,
    memory_type: str,
) -> List[Any]:
    try:
        return memory_manager.recall_memory(
            query=content,
            memory_type=memory_type,
            scope="user",
            project_key=None,
            app_session_id=None,
            k=20,
            include_archived=False,
        )
    except Exception:
        logger.warning("ordinary memory candidate recall failed; appending as new", exc_info=True)
        return []


def _judge_ordinary_relations(
    *,
    new_item: Dict[str, Any],
    candidates: List[Any],
) -> Dict[str, Dict[str, str]]:
    if not candidates:
        return {}
    payload = {
        "new_item": {
            "content": _safe_str(new_item.get("content")),
            "memory_type": _safe_str(new_item.get("memory_type")).lower(),
        },
        "candidates": [
            {
                "source_uri": candidate.source_uri,
                "content": candidate.content,
                "memory_type": candidate.memory_type,
                "scope": candidate.scope,
                "created_at": candidate.created_at,
                "updated_at": candidate.updated_at,
            }
            for candidate in candidates
        ],
    }
    try:
        decision_payload = _call_memory_llm(
            system_prompt=_ORDINARY_RELATION_SYSTEM_PROMPT,
            payload=payload,
            max_tokens=900,
            request_label="memory_relation_ordinary",
        )
    except Exception:
        logger.warning("ordinary memory relation judgment failed; appending as new", exc_info=True)
        return {}

    decisions: Dict[str, Dict[str, str]] = {}
    raw_decisions = decision_payload.get("decisions")
    if not isinstance(raw_decisions, list):
        return decisions
    valid = {"duplicate", "supersedes", "refines", "conflicts", "unrelated"}
    for item in raw_decisions:
        if not isinstance(item, dict):
            continue
        source_uri = _safe_str(item.get("source_uri"))
        relation = _safe_str(item.get("relation")).lower()
        if not source_uri or relation not in valid:
            continue
        decisions[source_uri] = {
            "relation": relation,
            "reason": _safe_str(item.get("reason")),
        }
    return decisions


def _classify_memory_item(
    *,
    content: str,
    context: _MemoryTaskContext,
) -> Dict[str, Any]:
    route = _route_memory_destination(
        content=content,
    )
    destination = _safe_str(route.get("destination")).lower()
    route_reason = _safe_str(route.get("reason"))

    if destination == "operation_evolution":
        extracted = _extract_operation_memory(
            content=content,
            context=context,
        )
        extracted["destination"] = "operation_evolution"
    else:
        extracted = _extract_ordinary_memory(
            content=content,
        )
        extracted["destination"] = "ordinary_memory"

    if route_reason and not _safe_str(extracted.get("reason")):
        extracted["reason"] = route_reason
    return extracted


def _route_with_context_defaults(route: Dict[str, Any], context: _MemoryTaskContext, content: str) -> Dict[str, Any]:
    read_context = _recent_read_context(context.read_documents)
    resolved = dict(route)
    resolved["content"] = _safe_str(resolved.get("content")) or _safe_str(content)
    resolved["ontology_id"] = (
        _safe_str(resolved.get("ontology_id"))
        or _safe_str(context.selected_ontology_id)
        or _infer_unique_context_value(read_context, "ontology_id")
    )
    kind = _safe_str(resolved.get("kind")).lower()
    explicit_operation_key = _safe_str(resolved.get("operation_key"))
    if explicit_operation_key:
        resolved["operation_key"] = explicit_operation_key
    elif kind != "schema":
        resolved["operation_key"] = _infer_unique_context_value(read_context, "operation_key")
    else:
        resolved["operation_key"] = None
    resolved["entity_key"] = (
        _safe_str(resolved.get("entity_key"))
        or _infer_unique_context_value(read_context, "entity_key")
    )
    return resolved


def _store_ordinary_memory(
    *,
    route: Dict[str, Any],
    memory_manager: "MemoryManager",
    session_id: Optional[str],
) -> Dict[str, Any]:
    memory_type = _safe_str(route.get("memory_type")).lower()
    if memory_type not in _MEMORY_TYPES:
        memory_type = "preference"
    content = _safe_str(route.get("content"))
    candidates = _ordinary_relation_candidates(
        memory_manager=memory_manager,
        content=content,
        memory_type=memory_type,
    )
    relation_decisions = _judge_ordinary_relations(
        new_item={"content": content, "memory_type": memory_type},
        candidates=candidates,
    )

    duplicate_candidate = next(
        (
            candidate
            for candidate in candidates
            if relation_decisions.get(candidate.source_uri, {}).get("relation") == "duplicate"
        ),
        None,
    )
    archived_source_uris: List[str] = []
    relation_summary: Dict[str, str] = {}
    for candidate in candidates:
        decision = relation_decisions.get(candidate.source_uri) or {}
        relation = decision.get("relation")
        if relation in {"supersedes", "refines", "conflicts"}:
            try:
                archived = memory_manager.archive_memory(
                    source_uri=candidate.source_uri,
                    reason=decision.get("reason"),
                )
            except Exception:
                logger.warning(
                    "ordinary memory archive failed for %s",
                    candidate.source_uri,
                    exc_info=True,
                )
                archived = False
            if archived:
                archived_source_uris.append(candidate.source_uri)
                relation_summary[candidate.source_uri] = relation

    store_content = duplicate_candidate.content if duplicate_candidate is not None else content
    result = memory_manager.store_memory(
        content=store_content,
        memory_type=memory_type,
        scope="user",
        project_key=None,
        app_session_id=session_id,
        source="explicit",
    )
    payload = result.to_dict()
    payload["destination"] = "ordinary_memory"
    if duplicate_candidate is not None:
        payload["relation"] = "duplicate"
        payload["matched_source_uri"] = duplicate_candidate.source_uri
    if archived_source_uris:
        payload["archived_source_uris"] = archived_source_uris
        payload["relations"] = relation_summary
    return payload


def _store_operation_evolution(
    *,
    route: Dict[str, Any],
    context: _MemoryTaskContext,
) -> Dict[str, Any]:
    kind = _safe_str(route.get("kind")).lower()
    ontology_id = _safe_str(route.get("ontology_id"))
    operation_key = _safe_str(route.get("operation_key"))
    entity_key = _safe_str(route.get("entity_key")) or None

    if kind not in _EVOLUTION_KINDS:
        kind = "procedure"
    missing_scope = (
        not ontology_id
        or (kind == "schema" and not operation_key and not entity_key)
    )
    if missing_scope:
        if context.memory_manager:
            fallback_route = {
                **route,
                "memory_type": "preference",
                "reason": "operation scope missing; preserved as ordinary memory",
            }
            payload = _store_ordinary_memory(
                route=fallback_route,
                memory_manager=context.memory_manager,
                session_id=context.session_id,
            )
            payload["fallback_from"] = "operation_evolution"
            return payload
        return {
            "stored": False,
            "destination": "operation_evolution",
            "status": "error",
            "error": "Operation evolution scope missing and memory manager not available.",
        }

    buffer = get_proposal_buffer()
    write_result = append_operation_proposal_with_relations(
        buffer=buffer,
        workspace_id=context.workspace_id,
        ontology_id=ontology_id,
        operation_key=operation_key or None,
        kind=kind,
        content=_safe_str(route.get("content")),
        source="active_chat",
        entity_key=entity_key,
        rationale=_safe_str(route.get("reason")) or None,
        run_id=context.run_id,
        user_id=None,
    )
    aggregate = write_result.aggregate
    aggregate_payload = aggregate.to_dict()
    payload = {
        "stored": True,
        "destination": "operation_evolution",
        "ontology_id": ontology_id,
        "operation_key": operation_key or None,
        "entity_key": entity_key,
        "kind": kind,
        "fingerprint": aggregate.fingerprint,
        "status": aggregate.status,
        "occurrences": aggregate.occurrences,
        "active_endorsements": aggregate.active_endorsements,
        "created_at": aggregate_payload.get("created_at"),
        "updated_at": aggregate_payload.get("updated_at"),
    }
    if write_result.relation == "duplicate":
        payload["relation"] = "duplicate"
        payload["matched_fingerprint"] = write_result.matched_fingerprint
    if write_result.replaced_fingerprints:
        payload["replaced_fingerprints"] = write_result.replaced_fingerprints
        payload["relations"] = write_result.relations
    return payload


def _process_memory_task(content: str, context: _MemoryTaskContext) -> Dict[str, Any]:
    route = _classify_memory_item(
        content=content,
        context=context,
    )
    route = _route_with_context_defaults(
        route,
        context,
        content,
    )
    destination = _safe_str(route.get("destination")).lower()
    if destination == "ordinary_memory":
        if not context.memory_manager:
            return {
                "stored": False,
                "destination": "ordinary_memory",
                "status": "error",
                "error": "Memory manager not available.",
            }
        return _store_ordinary_memory(
            route=route,
            memory_manager=context.memory_manager,
            session_id=context.session_id,
        )
    if destination == "operation_evolution":
        return _store_operation_evolution(route=route, context=context)
    if not context.memory_manager:
        return {
            "stored": False,
            "destination": "ordinary_memory",
            "status": "error",
            "error": "Memory manager not available.",
        }
    return _store_ordinary_memory(
        route={
            **route,
            "destination": "ordinary_memory",
            "memory_type": _safe_str(route.get("memory_type")) or "preference",
            "reason": route.get("reason") or "unsupported destination; preserved as ordinary memory",
        },
        memory_manager=context.memory_manager,
        session_id=context.session_id,
    )


def store_user_memory(
    content: str,
    *,
    memory_manager: "MemoryManager",
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Store a user-managed ordinary memory item synchronously.

    Used by the user-memory management UI (config center) to persist a
    manually authored preference/profile item. Runs the ordinary extractor
    (to classify preference vs profile and tidy wording) and the ordinary
    relation/dedupe pass, so manually added items stay consistent with
    model-captured ones.
    """
    content = _safe_str(content)
    if not content:
        return {"stored": False, "status": "error", "error": "empty content"}
    route = _extract_ordinary_memory(content=content)
    route["content"] = _safe_str(route.get("content")) or content
    route["destination"] = "ordinary_memory"
    return _store_ordinary_memory(
        route=route,
        memory_manager=memory_manager,
        session_id=session_id,
    )


async def handle_memory(
    content: str,
    *,
    context: ToolContext,
) -> str:
    content = _safe_str(content)
    if not content:
        return json.dumps(
            {"stored": False, "status": "error", "error": "empty content"},
            ensure_ascii=False,
        )

    context_messages = list(context.messages or [])
    if context_messages and not _has_explicit_memory_request(context_messages):
        return json.dumps(
            {
                "stored": False,
                "status": "rejected",
                "error": "explicit_current_user_memory_request_required",
            },
            ensure_ascii=False,
        )

    try:
        task_context = _MemoryTaskContext(
            memory_manager=context.memory_manager,
            session_id=context.session_id,
            run_id=context.run_id,
            workspace_id=context.workspace_id,
            selected_ontology_id=context.selected_ontology_id,
            read_documents=dict(context.read_documents or {}),
        )
        result = await run_sync_compat(
            _process_memory_task,
            content,
            task_context,
            run_id=context.run_id,
            task_name="memory-tool-capture",
            offload_kind="tool",
        )
        return json.dumps(
            result,
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.error("memory tool failed: %s", type(exc).__name__)
        return json.dumps(
            {
                "stored": False,
                "status": "error",
                "error": redact_sensitive_text(exc),
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class MemoryTool:
    name: str = "memory"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = None
    category: str = "memory"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", MEMORY_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main"})

    async def aexecute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del tool_call_id, event_emitter
        return ToolOutcome(content=await handle_memory(**args, context=context))
