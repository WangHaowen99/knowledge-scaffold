"""Runtime OpenTelemetry identity allocation.

Runtime facades still use their stable business span keys to describe
boundaries. This module gives those boundaries canonical W3C-compatible IDs
before the event enters any asynchronous observer or SSE writer.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from utils.logging import get_logger


logger = get_logger("observability.identity")


def _new_trace_id() -> str:
    return _non_zero_hex(secrets.randbits(128), 32)


def _non_zero_hex(value: int, width: int) -> str:
    return format(value or 1, "0{}x".format(width))[-width:]


def _stable_id(seed: str, width: int) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:width] if int(digest[:width], 16) else ("0" * (width - 1) + "1")


@dataclass
class _RunIdentity:
    trace_id: str
    external_parent_span_id: Optional[str] = None
    trace_id_locked: bool = False
    router_id: str = ""
    workspace_id: str = ""
    span_ids: dict[str, str] = field(default_factory=dict)
    spans: dict[str, object] = field(default_factory=dict)
    span_scopes: dict[str, object] = field(default_factory=dict)
    metric_labels: dict[str, str] = field(default_factory=dict)


_lock = threading.RLock()
_identities: dict[str, _RunIdentity] = {}


def bind_runtime_trace_identity(
    run_id: str,
    *,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
) -> str:
    """Bind the request trace before RuntimeEvent or SSE creation."""
    resolved_run_id = str(run_id or "")
    if not resolved_run_id:
        return ""
    current_trace_id, current_span_id = _current_otel_identity()
    explicit_trace_id = _normalize_hex(trace_id, 32)
    normalized_trace_id = current_trace_id or explicit_trace_id or _new_trace_id()
    normalized_parent = current_span_id or _normalize_hex(parent_span_id, 16)
    with _lock:
        current = _identities.get(resolved_run_id)
        if current is None:
            current = _RunIdentity(
                trace_id=normalized_trace_id,
                external_parent_span_id=normalized_parent,
                trace_id_locked=bool(explicit_trace_id and not current_trace_id),
            )
            _identities[resolved_run_id] = current
        else:
            current.trace_id = normalized_trace_id
            current.trace_id_locked = bool(explicit_trace_id and not current_trace_id)
            if normalized_parent:
                current.external_parent_span_id = normalized_parent
        root_key = "run:{}".format(resolved_run_id)
        if root_key not in current.span_ids:
            current.span_ids[root_key] = _stable_id(
                "{}:{}".format(resolved_run_id, root_key),
                16,
            )
        return current.trace_id


def resolve_runtime_identity(
    run_id: str,
    *,
    span_key: Optional[str] = None,
    parent_key: Optional[str] = None,
    trace_id: Optional[str] = None,
    parent_span_id: Optional[str] = None,
    event_type: Optional[str] = None,
    timestamp_ms: Optional[int] = None,
    start_time_ms: Optional[int] = None,
    materialize_span: bool = True,
) -> tuple[str, str, Optional[str]]:
    """Return canonical trace, span, and parent IDs for one runtime event."""
    resolved_run_id = str(run_id or "")
    key = str(span_key or "event:{}".format(event_type or "runtime_event"))
    parent = str(parent_key or "")
    with _lock:
        identity = _identities.get(resolved_run_id)
        explicit_trace_id = _normalize_hex(trace_id, 32)
        if identity is None:
            identity = _RunIdentity(
                trace_id=explicit_trace_id or _new_trace_id(),
                external_parent_span_id=_normalize_hex(parent_span_id, 16),
                trace_id_locked=bool(explicit_trace_id),
            )
            _identities[resolved_run_id] = identity
        elif explicit_trace_id:
            identity.trace_id = explicit_trace_id
            identity.trace_id_locked = True

        span_id = identity.span_ids.get(key)
        if span_id is None:
            span_id = _start_otel_span(
                identity,
                key=key,
                parent_key=parent,
                event_type=event_type,
                timestamp_ms=start_time_ms or timestamp_ms,
                materialize_span=materialize_span,
            ) or _stable_id("{}:{}".format(resolved_run_id, key), 16)
            identity.span_ids[key] = span_id

        if parent:
            parent_id = identity.span_ids.get(parent)
            if parent_id is None:
                parent_id = _stable_id("{}:{}".format(resolved_run_id, parent), 16)
                identity.span_ids[parent] = parent_id
        else:
            parent_id = identity.external_parent_span_id

        return identity.trace_id, span_id, parent_id


def record_runtime_event(
    event: Any,
    *,
    router_id: str,
    workspace_id: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Convert one RuntimeEvent into an actual OTel span event and metrics."""
    resolved_run_id = str(getattr(event, "run_id", "") or "")
    event_type = str(getattr(event, "type", "") or "runtime_event")
    span_key = getattr(event, "span_key", None)
    parent_key = getattr(event, "parent_key", None)
    timestamp_ms = int(getattr(event, "timestamp_ms", 0) or 0)
    session_id = getattr(event, "session_id", None)
    turn_id = getattr(event, "turn_id", None)
    payload = dict(getattr(event, "payload", {}) or {})
    key = str(span_key or "event:{}".format(event_type))
    parent = str(parent_key or "")
    with _lock:
        identity = _identities.get(resolved_run_id)
        if identity is None:
            identity = _RunIdentity(
                trace_id=_normalize_hex(getattr(event, "trace_id", None), 32) or _new_trace_id(),
                external_parent_span_id=_normalize_hex(
                    getattr(event, "parent_span_id", None),
                    16,
                ),
            )
            _identities[resolved_run_id] = identity
        identity.router_id = str(router_id or identity.router_id)
        resolved_workspace_id = str(workspace_id or payload.get("workspace_id") or identity.workspace_id).strip()
        if resolved_workspace_id:
            identity.workspace_id = resolved_workspace_id
        _update_metric_labels(identity, event_type=event_type, payload=payload)

        span = identity.spans.get(key)
        if span is None:
            start_time_ms = (
                timestamp_ms - int(payload.get("duration_ms") or payload.get("elapsed_ms") or 0)
                if event_type in _END_EVENTS
                else timestamp_ms
            )
            started_span_id = _start_otel_span(
                identity,
                key=key,
                parent_key=parent,
                event_type=event_type,
                timestamp_ms=start_time_ms,
                materialize_span=True,
            )
            if started_span_id:
                identity.span_ids[key] = started_span_id
            span = identity.spans.get(key)

        span_id = identity.span_ids.get(key) or _stable_id(
            "{}:{}".format(resolved_run_id, key),
            16,
        )
        identity.span_ids[key] = span_id
        if parent:
            parent_span_id = identity.span_ids.get(parent)
            if parent_span_id is None:
                parent_span_id = _stable_id("{}:{}".format(resolved_run_id, parent), 16)
                identity.span_ids[parent] = parent_span_id
        else:
            parent_span_id = identity.external_parent_span_id

        if span is None:
            return identity.trace_id, span_id, parent_span_id

        attributes = _otel_attributes(payload)
        attributes["agent.run_id"] = resolved_run_id
        attributes["agent.observability.router_id"] = identity.router_id
        if identity.workspace_id:
            attributes["agent.workspace_id"] = identity.workspace_id
        if session_id:
            attributes["agent.session_id"] = str(session_id)
        if turn_id is not None:
            attributes["agent.turn_id"] = int(turn_id)
        try:
            for name, value in attributes.items():
                span.set_attribute(name, value)
            span.add_event(
                "agent.runtime_event",
                attributes={
                    "agent.event.type": event_type,
                    "agent.event.payload_json": json.dumps(
                        payload,
                        ensure_ascii=True,
                        default=str,
                        separators=(",", ":"),
                    ),
                    "agent.event.span_key": str(span_key or ""),
                    "agent.event.parent_key": str(parent_key or ""),
                    "agent.event.timestamp_ms": timestamp_ms,
                },
                timestamp=timestamp_ms * 1_000_000 if timestamp_ms else None,
            )
            status = str(payload.get("status") or "").strip().lower()
            if status in {"error", "failed"}:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, str(payload.get("error_message") or "")[:1000]))
            if status == "cancelled" and event_type in _RUN_TERMINAL_EVENTS:
                span.add_event("agent.cancelled", attributes=attributes)
            _record_otel_metrics(
                event_type=event_type,
                payload=payload,
                workspace_id=identity.workspace_id,
                metric_labels=dict(identity.metric_labels),
                router_id=identity.router_id,
            )
            if event_type in _END_EVENTS:
                try:
                    span.end(end_time=(int(timestamp_ms) * 1_000_000) if timestamp_ms else None)
                finally:
                    identity.spans.pop(key, None)
                    scope = identity.span_scopes.pop(key, None)
                    if scope is not None:
                        scope.__exit__(None, None, None)
        except Exception:
            # Runtime observability must never affect the agent run.
            pass
        return identity.trace_id, span_id, parent_span_id


_END_EVENTS = {
    "agent_loop.finished",
    "agent_iteration.finished",
    "llm.finished",
    "llm.failed",
    "tool_batch.finished",
    "tool_batch.failed",
    "tool.finished",
    "runtime_phase.finished",
    "runtime_phase.failed",
    "run.finished",
    "run.failed",
    "run.cancelled",
}

_RUN_TERMINAL_EVENTS = {
    "run.finished",
    "run.failed",
    "run.cancelled",
}

_SPAN_NAMES = {
    "run.started": "agent.run",
    "run.finished": "agent.run",
    "run.failed": "agent.run",
    "run.cancelled": "agent.run",
    "agent_loop.started": "agent.loop",
    "agent_loop.finished": "agent.loop",
    "agent_iteration.prepared": "agent.iteration",
    "agent_iteration.finished": "agent.iteration",
    "llm.started": "llm.call",
    "llm.finished": "llm.call",
    "llm.failed": "llm.call",
    "tool_batch.started": "tool.batch",
    "tool_batch.finished": "tool.batch",
    "tool_batch.failed": "tool.batch",
    "tool.started": "tool.call",
    "tool.finished": "tool.call",
    "runtime_phase.started": "runtime.phase",
    "runtime_phase.finished": "runtime.phase",
    "runtime_phase.failed": "runtime.phase",
}

_SAFE_STRING_ATTRIBUTES = {
    "status",
    "caller",
    "request_label",
    "model",
    "model_provider",
    "response_model",
    "finish_reason",
    "tool_name",
    "display_name",
    "tool_call_id",
    "script_name",
    "phase_name",
    "component",
    "route",
    "reason",
    "cancel_reason",
    "ontology_id",
    "operation_key",
    "execution_path",
    "error_type",
    "workspace_id",
    "agent_mode",
}


def _start_otel_span(
    identity: _RunIdentity,
    *,
    key: str,
    parent_key: str,
    event_type: Optional[str],
    timestamp_ms: Optional[int],
    materialize_span: bool,
) -> Optional[str]:
    if not materialize_span:
        return None
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            TraceState,
            get_tracer,
            set_span_in_context,
            use_span,
        )
    except ImportError:
        return None

    parent_span = identity.spans.get(parent_key) if parent_key else None
    if parent_span is not None:
        parent_context = set_span_in_context(parent_span)
    elif parent_key and parent_key in identity.span_ids:
        parent_context = set_span_in_context(
            NonRecordingSpan(
                SpanContext(
                    trace_id=int(identity.trace_id, 16),
                    span_id=int(identity.span_ids[parent_key], 16),
                    is_remote=False,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                    trace_state=TraceState(),
                )
            )
        )
    else:
        current_span_context = trace.get_current_span().get_span_context()
        current_trace_matches = current_span_context.trace_id == int(identity.trace_id, 16)
        current_parent_matches = (
            not identity.external_parent_span_id
            or current_span_context.span_id == int(identity.external_parent_span_id, 16)
        )
        if current_span_context.is_valid and current_trace_matches and current_parent_matches:
            parent_context = otel_context.get_current()
        elif identity.external_parent_span_id:
            parent_context = set_span_in_context(
                NonRecordingSpan(
                    SpanContext(
                        trace_id=int(identity.trace_id, 16),
                        span_id=int(identity.external_parent_span_id, 16),
                        is_remote=True,
                        trace_flags=TraceFlags(TraceFlags.SAMPLED),
                        trace_state=TraceState(),
                    )
                )
            )
        elif identity.trace_id_locked:
            # OTel cannot create a root span with a caller-supplied trace ID
            # unless the caller also supplied a parent span. Preserve that ID
            # for the unified runtime record rather than silently replacing it.
            return None
        else:
            parent_context = otel_context.Context()

    span = get_tracer("onto.consumer.runtime").start_span(
        name=_SPAN_NAMES.get(str(event_type or ""), "runtime.event"),
        context=parent_context,
        start_time=(int(timestamp_ms) * 1_000_000) if timestamp_ms else None,
    )
    span_context = span.get_span_context()
    if not span_context.is_valid:
        return None
    identity.spans[key] = span
    try:
        scope = use_span(span, end_on_exit=False)
        scope.__enter__()
        identity.span_scopes[key] = scope
    except Exception:
        identity.spans.pop(key, None)
        return None
    identity.trace_id = format(span_context.trace_id, "032x")
    return format(span_context.span_id, "016x")


def _otel_attributes(payload: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in payload.items():
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        if isinstance(value, str) and key in _SAFE_STRING_ATTRIBUTES:
            result["agent." + str(key)] = value[:1000]
        elif isinstance(value, (bool, int, float)):
            result["agent." + str(key)] = value
    return result


def _record_otel_metrics(
    *,
    event_type: str,
    payload: dict[str, Any],
    workspace_id: str,
    metric_labels: dict[str, str],
    router_id: str,
) -> None:
    try:
        from integration.observability.otel import record_runtime_metrics

        record_runtime_metrics(
            event_type=event_type,
            payload=payload,
            workspace_id=workspace_id,
            metric_labels=metric_labels,
            router_id=router_id,
        )
    except Exception:
        logger.warning("Failed to record OpenTelemetry metrics", exc_info=True)


def _update_metric_labels(
    identity: _RunIdentity,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    for key in ("model", "model_provider", "agent_mode"):
        value = str(payload.get(key) or "").strip()
        if not value:
            continue
        if event_type == "run.started" or key not in identity.metric_labels:
            identity.metric_labels[key] = value[:160]


def clear_runtime_trace_identity(run_id: str) -> None:
    with _lock:
        identity = _identities.pop(str(run_id or ""), None)
        if identity is None:
            return
        for key, span in reversed(list(identity.spans.items())):
            try:
                span.end()
            except Exception:
                logger.warning("Failed to end OpenTelemetry span %s", key, exc_info=True)
            scope = identity.span_scopes.pop(key, None)
            if scope is not None:
                try:
                    scope.__exit__(None, None, None)
                except Exception:
                    logger.warning("Failed to close OpenTelemetry span scope %s", key, exc_info=True)


def _normalize_hex(value: Optional[str], width: int) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if len(raw) != width:
        return None
    try:
        if int(raw, 16) == 0:
            return None
    except ValueError:
        return None
    return raw


def _current_otel_identity() -> tuple[Optional[str], Optional[str]]:
    try:
        from opentelemetry import trace

        span_context = trace.get_current_span().get_span_context()
        if not span_context.is_valid:
            return None, None
        return (
            format(span_context.trace_id, "032x"),
            format(span_context.span_id, "016x"),
        )
    except (ImportError, AttributeError, TypeError):
        return None, None
