# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, TypeAlias


JsonDict: TypeAlias = Dict[str, Any]
AgentMode: TypeAlias = Literal["quick", "thinking", "multi_agent"]
NormalizedEventKind: TypeAlias = Literal[
    "run.started",
    "run.delta",
    "reasoning.delta",
    "reasoning.summary",
    "tool.started",
    "tool.progress",
    "tool.finished",
    "subagent.started",
    "subagent.delta",
    "subagent.progress",
    "subagent.finished",
    "subagent_group.started",
    "subagent_group.finished",
    "run.completed",
    "run.awaiting",
    "run.failed",
    "run.cancelled",
    "raw",
]


@dataclass(slots=True)
class SseEvent:
    event: str = "message"
    data: str = ""
    json_data: Any = None
    id: Optional[str] = None
    retry: Optional[int] = None


@dataclass(slots=True)
class NormalizedStreamEvent:
    kind: NormalizedEventKind
    protocol: Literal["agentic", "a2a"]
    event_name: str
    payload: JsonDict = field(default_factory=dict)
    raw: Any = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[int] = None
    sequence: Optional[int] = None
    event_id: Optional[str] = None
    timestamp: Optional[int] = None
    source: JsonDict = field(default_factory=dict)


_AGENTIC_DIRECT_EVENT_KINDS = {
    "tool.started",
    "tool.progress",
    "tool.finished",
    "subagent.started",
    "subagent.delta",
    "subagent.progress",
    "subagent.finished",
    "subagent_group.started",
    "subagent_group.finished",
}


def _as_dict(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return value
    return {}


def _normalize_turn_finished_kind(payload: JsonDict) -> NormalizedEventKind:
    status = payload.get("status")
    if status == "awaiting_input":
        return "run.awaiting"
    if status == "error":
        return "run.failed"
    if status == "cancelled":
        return "run.cancelled"
    return "run.completed"


def _normalize_event_kind(event_type: str, payload: JsonDict) -> NormalizedEventKind:
    if event_type == "turn.started":
        return "run.started"
    if event_type == "turn.finished":
        return _normalize_turn_finished_kind(payload)
    if event_type == "token.delta":
        if payload.get("channel") == "reasoning":
            return "reasoning.delta"
        return "run.delta"
    if event_type == "reasoning.summary":
        return "reasoning.summary"
    if event_type in _AGENTIC_DIRECT_EVENT_KINDS:
        return event_type  # type: ignore[return-value]
    if event_type == "task.error":
        return "run.failed"
    return "raw"


def normalize_agentic_envelope(envelope: JsonDict) -> NormalizedStreamEvent:
    payload = _as_dict(envelope.get("payload"))
    return NormalizedStreamEvent(
        kind=_normalize_event_kind(str(envelope.get("type") or "raw"), payload),
        protocol="agentic",
        event_name=str(envelope.get("type") or "raw"),
        payload=payload,
        raw=envelope,
        run_id=_optional_str(envelope.get("run_id")),
        session_id=_optional_str(envelope.get("session_id")),
        turn_id=_optional_int(envelope.get("turn_id")),
        sequence=_optional_int(envelope.get("seq")),
        event_id=_optional_str(envelope.get("event_id")),
        timestamp=_optional_int(envelope.get("ts")),
        source=_as_dict(envelope.get("source")),
    )


def normalize_a2a_update(update: JsonDict) -> NormalizedStreamEvent:
    payload = _as_dict(update.get("payload"))
    event_name = str(update.get("event_type") or "task.update")
    task_id = _optional_str(update.get("task_id"))
    return NormalizedStreamEvent(
        kind=_normalize_event_kind(event_name, payload),
        protocol="a2a",
        event_name=event_name,
        payload=payload,
        raw=update,
        run_id=task_id,
        task_id=task_id,
        session_id=_optional_str(update.get("session_id")),
        timestamp=_optional_int(update.get("timestamp")),
        source=_as_dict(update.get("source")),
    )


def normalize_agentic_sse_event(event: SseEvent) -> NormalizedStreamEvent:
    if event.event == "agent.v2" and isinstance(event.json_data, dict):
        normalized = normalize_agentic_envelope(event.json_data)
        if normalized.event_id is None:
            normalized.event_id = event.id
        return normalized
    return NormalizedStreamEvent(
        kind="raw",
        protocol="agentic",
        event_name=event.event,
        payload={},
        raw=event,
        event_id=event.id,
    )


def normalize_a2a_sse_event(event: SseEvent) -> NormalizedStreamEvent:
    if event.event == "task.update" and isinstance(event.json_data, dict):
        return normalize_a2a_update(event.json_data)
    return NormalizedStreamEvent(
        kind="raw",
        protocol="a2a",
        event_name=event.event,
        payload={},
        raw=event,
    )


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
