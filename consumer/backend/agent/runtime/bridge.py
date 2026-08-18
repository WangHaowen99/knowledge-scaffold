"""Synchronous protocol helpers for the process-scoped async run manager.

Flask and the standalone MCP transport are synchronous protocol surfaces.  This
module keeps their blocking wait at the boundary while the B run manager and
agent runtime continue to execute on the dedicated asyncio loop.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from typing import Any, Optional

from agent.runtime.contracts import PublicEvent, RunScope, RunSnapshot, RunStatus
from agent.runtime.event_bus import STREAM_CLOSED, STREAM_RECONNECT


def public_event_envelope(event: PublicEvent) -> dict[str, Any]:
    """Convert a public event to the legacy agent-stream.v2 envelope."""
    return {
        "protocol": "agent-stream.v2",
        "type": event.event_type,
        "run_id": event.run_id,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "seq": event.seq,
        "source": event.source or {},
        "payload": event.payload,
    }


def legacy_public_event_envelope(event: PublicEvent) -> dict[str, Any]:
    """Project PublicEvent to the pre-async agent-stream.v2 vocabulary."""
    envelope = public_event_envelope(event)
    payload = dict(envelope["payload"])
    if event.event_type == "turn.finished":
        payload["status"] = status_to_legacy(
            payload.get("status") or event.status
        )
        if "final_output" not in payload:
            if payload.get("structured_content") is not None:
                payload["final_output"] = payload.get("structured_content")
            elif "content" in payload:
                payload["final_output"] = payload.get("content")
    envelope["payload"] = payload
    return envelope


def wait_for_run(
    manager: Any,
    call_async: Callable[[Any], Any],
    run_id: str,
    scope: RunScope,
    *,
    timeout_seconds: float,
    after_seq: int = 0,
    on_event: Optional[Callable[[PublicEvent], None]] = None,
) -> RunSnapshot:
    """Wait for one B run without executing protocol code on its event loop.

    ``BlockingSubscription`` intentionally exposes a bounded ``queue.Queue`` so
    Flask/MCP can consume events without introducing a second async transport.
    The subscription is always released, including timeout and client errors.
    """
    cursor = max(0, int(after_seq))
    subscription = call_async(manager.subscribe(run_id, scope, after_seq=cursor))
    deadline = time.monotonic() + max(0.01, float(timeout_seconds))
    try:
        while True:
            for event in subscription.replay:
                cursor = max(cursor, event.seq)
                if on_event is not None:
                    on_event(event)
                if event.event_type == "turn.finished":
                    snapshot = call_async(manager.get_run(run_id, scope))
                    if snapshot is not None:
                        return snapshot
            subscription.replay = []

            if subscription.closed:
                snapshot = call_async(manager.get_run(run_id, scope))
                if snapshot is not None:
                    return snapshot

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("异步运行等待超时")
            try:
                item = subscription.queue.get(timeout=min(1.0, remaining))
            except queue.Empty:
                continue
            if item is STREAM_CLOSED:
                snapshot = call_async(manager.get_run(run_id, scope))
                if snapshot is not None:
                    return snapshot
                continue
            if item is STREAM_RECONNECT:
                call_async(manager.unsubscribe(subscription))
                subscription = call_async(
                    manager.subscribe(run_id, scope, after_seq=cursor)
                )
                continue
            if not isinstance(item, PublicEvent):
                continue
            cursor = max(cursor, item.seq)
            if on_event is not None:
                on_event(item)
            if item.event_type != "turn.finished":
                continue
            snapshot = call_async(manager.get_run(run_id, scope))
            if snapshot is not None:
                return snapshot
    finally:
        call_async(manager.unsubscribe(subscription))


def status_to_legacy(value: RunStatus | str) -> str:
    """Map B's public status to the existing protocol vocabulary."""
    normalized = value.value if isinstance(value, RunStatus) else str(value)
    return {
        RunStatus.COMPLETED.value: "success",
        RunStatus.FAILED.value: "error",
        RunStatus.CANCELLED.value: "cancelled",
        RunStatus.AWAITING_INPUT.value: "awaiting_input",
    }.get(normalized, normalized)
