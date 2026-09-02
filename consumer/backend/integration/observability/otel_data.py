"""Immutable projections of OpenTelemetry SDK export data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from integration.observability.events import RuntimeEvent


RUNTIME_EVENT_NAME = "agent.runtime_event"

_LOCAL_SPAN_ATTRIBUTE_NAMES = {
    "agent.run_id",
    "agent.session_id",
    "agent.turn_id",
    "agent.workspace_id",
    "agent.status",
    "agent.caller",
    "agent.request_label",
    "agent.model",
    "agent.model_provider",
    "agent.response_model",
    "agent.finish_reason",
    "agent.tool_name",
    "agent.display_name",
    "agent.tool_call_id",
    "agent.script_name",
    "agent.phase_name",
    "agent.component",
    "agent.route",
    "agent.ontology_id",
    "agent.operation_key",
    "agent.execution_path",
    "agent.error_type",
    "agent.agent_mode",
    "http.request.method",
    "http.response.status_code",
    "http.method",
    "http.status_code",
    "http.route",
    "server.address",
    "server.port",
    "network.protocol.version",
    "url.scheme",
}
_LOCAL_URL_ATTRIBUTE_NAMES = {"http.url", "url.full"}
_LOCAL_METRIC_ATTRIBUTE_NAMES = {
    "cache",
    "exporter",
    "phase",
    "result",
    "agent.workspace_id",
    "agent.status",
    "agent.tool.name",
    "agent.token.type",
    "agent.model",
    "agent.model_provider",
    "agent.agent_mode",
}
_LOCAL_BOUNDED_METRIC_ATTRIBUTE_VALUES = {
    "cache": {
        "model_profile_references",
        "ontology_view",
        "system_config",
        "workspace_catalog",
    },
    "exporter": {
        "custom",
        "langfuse",
        "local",
        "speedscope",
        "workspace_fanout",
    },
    "phase": {
        "auth",
        "handler",
        "model_profile_references",
        "ontology_view",
        "request",
        "system_config",
        "workspace_catalog",
    },
    "result": {
        "bypass",
        "closed",
        "error",
        "failure",
        "hit",
        "miss",
        "partial_failure",
        "queue_full",
        "snapshot",
        "stale",
        "success",
        "timeout",
        "unknown",
    },
}
_LOCAL_EVENT_LABEL_FIELDS = {
    "status",
    "caller",
    "request_label",
    "model",
    "model_provider",
    "finish_reason",
    "tool_name",
    "display_name",
    "tool_call_id",
    "script_name",
    "phase_name",
    "component",
    "route",
    "ontology_id",
    "operation_key",
    "execution_path",
    "error_type",
    "database_provider",
}
_LOCAL_EVENT_METRIC_FIELDS = {
    "iteration",
    "duration_ms",
    "elapsed_ms",
    "tool_calls_count",
}


@dataclass(frozen=True)
class OTelEventSnapshot:
    name: str
    timestamp_ms: int
    attributes: dict[str, Any]


@dataclass(frozen=True)
class OTelSpanSnapshot:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    start_ms: int
    end_ms: int
    status: str
    attributes: dict[str, Any]
    events: tuple[OTelEventSnapshot, ...]
    scope_name: str

    @property
    def run_id(self) -> str:
        return str(self.attributes.get("agent.run_id") or "").strip()

    @property
    def workspace_id(self) -> str:
        return str(self.attributes.get("agent.workspace_id") or "").strip()

    @property
    def router_id(self) -> str:
        return str(self.attributes.get("agent.observability.router_id") or "").strip()


def snapshot_readable_span(span: Any) -> OTelSpanSnapshot:
    context = span.get_span_context()
    parent = getattr(span, "parent", None)
    parent_span_id = None
    if parent is not None and getattr(parent, "is_valid", False):
        parent_span_id = format(parent.span_id, "016x")
    status = getattr(getattr(span, "status", None), "status_code", None)
    status_name = str(getattr(status, "name", status or "UNSET")).lower()
    instrumentation_scope = getattr(span, "instrumentation_scope", None)
    events = tuple(
        OTelEventSnapshot(
            name=str(getattr(item, "name", "") or ""),
            timestamp_ms=int(getattr(item, "timestamp", 0) or 0) // 1_000_000,
            attributes=dict(getattr(item, "attributes", {}) or {}),
        )
        for item in (getattr(span, "events", None) or ())
    )
    return OTelSpanSnapshot(
        name=str(getattr(span, "name", "") or ""),
        trace_id=format(context.trace_id, "032x"),
        span_id=format(context.span_id, "016x"),
        parent_span_id=parent_span_id,
        start_ms=int(getattr(span, "start_time", 0) or 0) // 1_000_000,
        end_ms=int(getattr(span, "end_time", 0) or 0) // 1_000_000,
        status=status_name,
        attributes=dict(getattr(span, "attributes", {}) or {}),
        events=events,
        scope_name=str(getattr(instrumentation_scope, "name", "") or ""),
    )


def runtime_events_from_spans(spans: Sequence[OTelSpanSnapshot]) -> list[RuntimeEvent]:
    rows: list[tuple[int, int, int, int, RuntimeEvent]] = []
    for span_order, span in enumerate(spans):
        for event_order, item in enumerate(span.events):
            if item.name != RUNTIME_EVENT_NAME:
                continue
            attributes = item.attributes
            event_type = str(attributes.get("agent.event.type") or "").strip()
            if not event_type:
                continue
            payload = _decode_payload(attributes.get("agent.event.payload_json"))
            if span.workspace_id:
                payload.setdefault("workspace_id", span.workspace_id)
            timestamp_ms = int(attributes.get("agent.event.timestamp_ms") or item.timestamp_ms or span.end_ms)
            lifecycle_order, lineage_order = _runtime_event_lifecycle_order(
                event_type,
                span_order=span_order,
            )
            rows.append(
                (
                    timestamp_ms,
                    lifecycle_order,
                    lineage_order,
                    event_order,
                    RuntimeEvent.from_otel(
                        type=event_type,
                        run_id=span.run_id,
                        session_id=_optional_text(span.attributes.get("agent.session_id")),
                        turn_id=_optional_int(span.attributes.get("agent.turn_id")),
                        span_key=_optional_text(attributes.get("agent.event.span_key")),
                        parent_key=_optional_text(attributes.get("agent.event.parent_key")),
                        trace_id=span.trace_id,
                        span_id=span.span_id,
                        parent_span_id=span.parent_span_id,
                        payload=payload,
                        timestamp_ms=timestamp_ms,
                    ),
                )
            )
    rows.sort(key=lambda row: row[:4])
    return [row[4] for row in rows]


def _runtime_event_lifecycle_order(event_type: str, *, span_order: int) -> tuple[int, int]:
    if event_type.endswith((".started", ".prepared")):
        return 0, span_order
    if event_type.endswith((".finished", ".failed", ".cancelled")):
        return 2, -span_order
    return 1, span_order


def span_record(span: OTelSpanSnapshot) -> dict[str, Any]:
    labels = {}
    for key, value in span.attributes.items():
        projected = _local_span_attribute(key, value)
        if projected is not None:
            labels[key] = projected
    record = {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "span",
        "event": "span.ended",
        "name": span.name,
        "ts_ms": span.end_ms,
        "start_ms": span.start_ms,
        "duration_ms": max(0, span.end_ms - span.start_ms),
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "status": span.status,
        "scope": span.scope_name,
        "metrics": {"duration_ms": max(0, span.end_ms - span.start_ms)},
        "labels": labels,
    }
    events = []
    for item in span.events:
        event_record = _local_span_event_record(span, item)
        if event_record is not None:
            events.append(event_record)
    if events:
        record["events"] = events
    if span.parent_span_id:
        record["parent_span_id"] = span.parent_span_id
    for source, target in (
        ("agent.run_id", "run_id"),
        ("agent.session_id", "session_id"),
        ("agent.turn_id", "turn_id"),
    ):
        value = span.attributes.get(source)
        if value not in (None, ""):
            record[target] = value
    return record


def metric_record(
    *,
    name: str,
    point: Any,
    scope_name: str,
) -> dict[str, Any]:
    attributes = dict(getattr(point, "attributes", {}) or {})
    timestamp_ms = int(getattr(point, "time_unix_nano", 0) or 0) // 1_000_000
    value: Any
    if hasattr(point, "value"):
        value = getattr(point, "value")
    else:
        value = {
            "count": int(getattr(point, "count", 0) or 0),
            "sum": getattr(point, "sum", None),
            "min": getattr(point, "min", None),
            "max": getattr(point, "max", None),
            "bucket_counts": [
                int(item or 0)
                for item in (getattr(point, "bucket_counts", None) or ())
            ],
            "explicit_bounds": [
                item
                for item in (getattr(point, "explicit_bounds", None) or ())
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            ],
        }
    record = {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "metric",
        "event": "metric.recorded",
        "name": str(name or ""),
        "ts_ms": timestamp_ms,
        "value": value,
        "scope": str(scope_name or ""),
        "metrics": {"value": value},
        "labels": {
            key: item
            for key, item in attributes.items()
            if _is_local_metric_attribute(key, item)
        },
    }
    workspace_id = attributes.get("agent.workspace_id")
    if workspace_id not in (None, ""):
        record["workspace_id"] = workspace_id
    return record


def _is_local_metric_attribute(key: object, value: Any) -> bool:
    if key not in _LOCAL_METRIC_ATTRIBUTE_NAMES or not _is_scalar(value):
        return False
    allowed_values = _LOCAL_BOUNDED_METRIC_ATTRIBUTE_VALUES.get(str(key))
    return allowed_values is None or str(value) in allowed_values


def _decode_payload(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _local_span_event_record(
    span: OTelSpanSnapshot,
    item: OTelEventSnapshot,
) -> Optional[dict[str, Any]]:
    attributes = dict(item.attributes or {})
    payload: dict[str, Any] = {}
    event_name = str(item.name or "").strip()
    if event_name == RUNTIME_EVENT_NAME:
        event_name = str(attributes.get("agent.event.type") or "").strip()
        payload = _decode_payload(attributes.get("agent.event.payload_json"))
    if not event_name:
        return None

    labels = {}
    metrics = {}
    for key, value in payload.items():
        if key in _LOCAL_EVENT_LABEL_FIELDS and _is_scalar(value) and value not in (None, ""):
            labels[key] = str(value)[:160]
        if (
            key in _LOCAL_EVENT_METRIC_FIELDS
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ):
            metrics[key] = value
    if item.name != RUNTIME_EVENT_NAME:
        for key, value in attributes.items():
            if key == "exception.type" and _is_scalar(value):
                labels[key] = str(value)[:160]

    record: dict[str, Any] = {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "span_event",
        "event": event_name,
        "ts_ms": int(item.timestamp_ms or span.end_ms),
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "labels": labels,
        "metrics": metrics,
    }
    if span.parent_span_id:
        record["parent_span_id"] = span.parent_span_id
    for source, target in (
        ("agent.run_id", "run_id"),
        ("agent.session_id", "session_id"),
        ("agent.turn_id", "turn_id"),
    ):
        value = span.attributes.get(source)
        if value not in (None, ""):
            record[target] = value
    status = labels.get("status")
    if status:
        record["status"] = status
    caller = labels.get("caller")
    if caller:
        record["caller"] = caller
    iteration = metrics.get("iteration")
    if iteration is not None:
        record["iteration"] = iteration
    return record


def _local_span_attribute(name: str, value: Any) -> Any:
    if not _is_scalar(value):
        return None
    resolved_name = str(name or "")
    if resolved_name in _LOCAL_URL_ATTRIBUTE_NAMES:
        return _url_without_query(str(value))
    if resolved_name in _LOCAL_SPAN_ATTRIBUTE_NAMES:
        return value[:1000] if isinstance(value, str) else value
    return None


def _url_without_query(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:
        return str(value or "").split("?", 1)[0].split("#", 1)[0]


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float))


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
