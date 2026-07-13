"""Tool-level SSE event emission helpers."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from integration.observability.events import RuntimeEvent, RuntimeEventType
from utils.streaming import emit_agent_event
from agent.tools.base import ToolContext

_SUBAGENT_LINEAGE_FIELDS = (
    "subagent_id",
    "task_id",
    "group_id",
    "depth",
    "parent_subagent_id",
    "root_tool_call_id",
)


def _inject_subagent_lineage(context: ToolContext, payload: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(payload or {})
    if enriched.get("caller") in (None, ""):
        enriched["caller"] = str(context.caller or "main")
    if context.agent_iteration is not None and enriched.get("iteration") in (None, ""):
        enriched["iteration"] = int(context.agent_iteration)
    lineage = context.subagent_lineage if isinstance(context.subagent_lineage, dict) else {}
    if context.caller != "subagent" or not lineage:
        return enriched

    for key in _SUBAGENT_LINEAGE_FIELDS:
        current = enriched.get(key)
        if current not in (None, ""):
            continue
        value = lineage.get(key)
        if value not in (None, ""):
            enriched[key] = value
    return enriched


def emit_protocol_event(
    *,
    context: ToolContext,
    event_type: str,
    payload: Dict[str, Any],
    source: Dict[str, str],
    writer: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    if not context.run_id:
        return

    scoped_payload = _inject_subagent_lineage(context, payload)

    if context.caller == "subagent" and context.event_emitter and writer is None:
        context.event_emitter(event_type, scoped_payload)
        if context.protocol_event_observer:
            context.protocol_event_observer(event_type, scoped_payload)
        _emit_tool_observability_event(context=context, event_type=event_type, payload=scoped_payload)
        return

    emit_agent_event(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type=event_type,
        source=source,
        payload=scoped_payload,
        writer=writer,
    )
    if context.protocol_event_observer:
        context.protocol_event_observer(event_type, scoped_payload)
    _emit_tool_observability_event(context=context, event_type=event_type, payload=scoped_payload)


def emit_tool_started(
    *,
    context: ToolContext,
    tool_name: str,
    tool_call_id: Optional[str],
    tool_input: Optional[Dict[str, Any]],
    message: Optional[str],
) -> None:
    emit_protocol_event(
        context=context,
        event_type="tool.started",
        source={"node": "agent_loop", "component": "tool"},
        payload={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "input": tool_input or {},
            "message": message or "调用工具 {}".format(tool_name),
        },
    )


def emit_tool_finished(
    *,
    context: ToolContext,
    tool_name: str,
    tool_call_id: Optional[str],
    status: str,
    raw_output: str,
    error: Optional[str],
    elapsed_ms: int,
) -> None:
    preview, truncated = _truncate_text(raw_output, limit=4096)
    emit_protocol_event(
        context=context,
        event_type="tool.finished",
        source={"node": "agent_loop", "component": "tool"},
        payload={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "status": "error" if status == "error" else "completed",
            "output_preview": preview,
            "output_truncated": truncated,
            "error": error,
            "elapsed_ms": elapsed_ms,
        },
    )


def _emit_tool_observability_event(
    *,
    context: ToolContext,
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    runtime_type = {
        "tool.started": RuntimeEventType.TOOL_STARTED,
        "tool.progress": RuntimeEventType.TOOL_PROGRESS,
        "tool.finished": RuntimeEventType.TOOL_FINISHED,
    }.get(event_type)
    if not runtime_type:
        return
    observer = context.observer
    if not observer or not getattr(observer, "enabled", True) or not context.run_id:
        return

    observer.emit(
        RuntimeEvent(
            type=runtime_type,
            run_id=str(context.run_id or ""),
            session_id=context.session_id,
            turn_id=context.turn_id,
            span_key=_tool_span_key(context, payload),
            parent_key=_tool_batch_span_key(context),
            payload=dict(payload or {}),
        )
    )


def _tool_span_key(context: ToolContext, payload: Dict[str, Any]) -> str:
    tool_call_id = str(payload.get("tool_call_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "unknown_tool").strip() or "unknown_tool"
    suffix = "{}:{}".format(tool_name, tool_call_id or "no_id")
    return "tool:{}:{}:{}".format(_caller_suffix(context), context.agent_iteration, suffix)


def _tool_batch_span_key(context: ToolContext) -> str:
    return "tools:{}:{}".format(_caller_suffix(context), context.agent_iteration)


def _caller_suffix(context: ToolContext) -> str:
    if context.caller != "subagent":
        return str(context.caller or "main")
    lineage = context.subagent_lineage if isinstance(context.subagent_lineage, dict) else {}
    subagent_id = str(lineage.get("subagent_id") or "").strip()
    return "subagent:{}".format(subagent_id) if subagent_id else "subagent"


def _truncate_text(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
