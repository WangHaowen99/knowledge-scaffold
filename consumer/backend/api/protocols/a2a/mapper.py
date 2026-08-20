"""Mapping utilities between internal runtime entities and A2A-facing shapes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.runtime.contracts import RunStatus
from api.protocols.a2a.models import A2ATaskState


def map_public_event_to_a2a_update(
    event: Any,
    *,
    external_task_id: str,
) -> Dict[str, Any]:
    """Project the B public event without exposing consumer-only fields."""
    return {
        "task_id": external_task_id,
        "canonical_run_id": event.run_id,
        "session_id": event.session_id,
        "event_type": event.event_type,
        "timestamp": event.created_at,
        "source": event.source or {},
        "payload": event.payload,
        "seq": event.seq,
    }


def map_run_status_to_a2a(
    run_status: Optional[RunStatus],
) -> A2ATaskState:
    """Map internal run/turn status to A2A task state."""
    if run_status is None:
        return A2ATaskState.NOT_FOUND

    if run_status == RunStatus.RUNNING:
        return A2ATaskState.IN_PROGRESS
    if run_status == RunStatus.AWAITING_INPUT:
        return A2ATaskState.FAILED
    if run_status == RunStatus.FAILED:
        return A2ATaskState.FAILED
    if run_status == RunStatus.CANCELLED:
        return A2ATaskState.CANCELED
    if run_status == RunStatus.QUEUED:
        return A2ATaskState.PENDING

    return A2ATaskState.COMPLETED


def map_agent_v2_event_to_a2a_update(event: Dict[str, Any]) -> Dict[str, Any]:
    """Project persisted `agent.v2` event to a concise A2A-style update payload."""
    data = event.get("data") or {}
    payload = data.get("payload") or {}
    return {
        "task_id": data.get("run_id"),
        "session_id": data.get("session_id"),
        "event_type": data.get("type"),
        "timestamp": data.get("ts"),
        "source": data.get("source") or {},
        "payload": payload,
    }
