"""Streaming helpers for the agent-stream.v2 protocol."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Callable, Dict, Iterator, Optional

from agent.events import (
    AgentEventPayload,
    AgentEventSource,
    AgentEventType,
    AgentStreamEvent,
)
from integration.observability.identity import resolve_runtime_identity

StreamWriter = Callable[[AgentStreamEvent], None]

PROTOCOL = "agent-stream.v2"
SSE_EVENT = "agent.v2"

_seq_lock = threading.Lock()
_run_sequences: Dict[str, int] = {}
_stream_writer_var: ContextVar[Optional[StreamWriter]] = ContextVar(
    "agent_stream_writer",
    default=None,
)


def get_stream_writer() -> StreamWriter:
    """
    Return the current run's stream writer.

    This mirrors the former workflow lookup contract so existing call sites and
    tests can keep using the same function name.
    """
    writer = _stream_writer_var.get()
    if writer is None:
        raise RuntimeError("Called get_stream_writer outside of a runnable context")
    return writer


@contextmanager
def stream_writer_context(writer: Optional[StreamWriter]) -> Iterator[None]:
    """Temporarily bind a stream writer for the current execution context."""
    token: Token[Optional[StreamWriter]] = _stream_writer_var.set(writer)
    try:
        yield
    finally:
        _stream_writer_var.reset(token)


def _get_timestamp() -> int:
    return int(time.time() * 1000)


def _next_seq(run_id: str) -> int:
    with _seq_lock:
        current = _run_sequences.get(run_id, 0) + 1
        _run_sequences[run_id] = current
        return current


def clear_run_sequence(run_id: Optional[str]) -> None:
    if not run_id:
        return
    with _seq_lock:
        _run_sequences.pop(run_id, None)


def build_agent_event(
    *,
    run_id: str,
    turn_id: int,
    event_type: AgentEventType,
    payload: Optional[AgentEventPayload] = None,
    session_id: Optional[str] = None,
    source: Optional[AgentEventSource] = None,
    span_key: Optional[str] = None,
    parent_key: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
) -> AgentStreamEvent:
    seq = _next_seq(run_id)
    identity_span_key = span_key
    if not identity_span_key and event_type in {"turn.started", "turn.finished"}:
        identity_span_key = "run:{}".format(run_id)
    resolved_trace_id, resolved_span_id, resolved_parent_span_id = resolve_runtime_identity(
        run_id,
        span_key=identity_span_key,
        parent_key=parent_key,
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        event_type=event_type,
        materialize_span=False,
    )
    envelope: Dict[str, Any] = {
        "protocol": PROTOCOL,
        "event_id": "{}:{}".format(run_id, seq),
        "seq": seq,
        "ts": _get_timestamp(),
        "session_id": session_id,
        "run_id": run_id,
        "turn_id": int(turn_id),
        "type": event_type,
        "source": source or {"node": "system", "component": "workflow"},
        "payload": dict(payload or {}),
    }
    if span_key:
        envelope["span_key"] = span_key
    if parent_key:
        envelope["parent_key"] = parent_key
    envelope["trace_id"] = resolved_trace_id
    envelope["span_id"] = span_id or resolved_span_id
    if parent_span_id or resolved_parent_span_id:
        envelope["parent_span_id"] = parent_span_id or resolved_parent_span_id
    return {
        "event": SSE_EVENT,
        "data": envelope,
    }


def try_get_stream_writer() -> Optional[StreamWriter]:
    """
    Best-effort stream writer lookup.

    Returns None when called outside of a bound workflow execution context.
    """
    try:
        return get_stream_writer()
    except RuntimeError as exc:
        if "outside of a runnable context" in str(exc):
            return None
        raise


def emit_agent_event(
    *,
    run_id: str,
    turn_id: int,
    event_type: AgentEventType,
    payload: Optional[AgentEventPayload] = None,
    session_id: Optional[str] = None,
    source: Optional[AgentEventSource] = None,
    writer: Optional[StreamWriter] = None,
    span_key: Optional[str] = None,
    parent_key: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
) -> None:
    writer_fn = writer or try_get_stream_writer()
    if not writer_fn:
        return
    writer_fn(
        build_agent_event(
            run_id=run_id,
            turn_id=turn_id,
            event_type=event_type,
            payload=payload,
            session_id=session_id,
            source=source,
            span_key=span_key,
            parent_key=parent_key,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
        )
    )
