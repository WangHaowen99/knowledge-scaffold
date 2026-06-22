"""Local JSONL performance exporter for runtime observability events."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping, Optional

from ...events import RuntimeEvent, RuntimeEventType
from ....utils.logging import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    LOG_DIR,
    DatePartitionedRotatingFileHandler,
)

_LOGGER_NAME = "agentic.observability.local_perf"


class _PerfRecordFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "perf_record", {})
        return json.dumps(payload, ensure_ascii=True, default=str)


class LocalPerfExporter:
    """Project runtime events into bounded local performance records."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        local = config.get("local") if isinstance(config.get("local"), dict) else {}
        base_path = os.path.abspath(os.path.join(LOG_DIR, "agent_perf.log"))
        self._handler = DatePartitionedRotatingFileHandler(
            base_path,
            encoding="utf-8",
            max_bytes=DEFAULT_MAX_BYTES,
            backup_count=DEFAULT_BACKUP_COUNT,
            retention_days=_coerce_positive_int(local.get("retention_days"), 30),
        )
        self._handler.setLevel(logging.INFO)
        self._handler.setFormatter(_PerfRecordFormatter())
        self._run_metrics: Dict[str, Dict[str, int]] = {}
        self._run_labels: Dict[str, Dict[str, str]] = {}

    @property
    def available(self) -> bool:
        return True

    def export(self, event: RuntimeEvent) -> None:
        self._track_run_labels(event)
        self._track_run_metrics(event)
        records = _project_event(event)
        if event.type in _RUN_TERMINAL_EVENTS:
            records.append(self._run_summary(event))
            self._run_metrics.pop(event.run_id, None)
            self._run_labels.pop(event.run_id, None)
        for record in records:
            self._write_record(record)

    def _write_record(self, record: Dict[str, Any]) -> None:
        log_record = logging.LogRecord(
            name=_LOGGER_NAME,
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=record.get("event") or "runtime_event",
            args=(),
            exc_info=None,
        )
        log_record.perf_record = record
        self._handler.handle(log_record)

    def _track_run_metrics(self, event: RuntimeEvent) -> None:
        if event.type not in {
            RuntimeEventType.LLM_FINISHED,
            RuntimeEventType.AGENT_ITERATION_FINISHED,
            RuntimeEventType.TOOL_STARTED,
            RuntimeEventType.TOOL_FINISHED,
        }:
            return
        metrics = self._run_metrics.setdefault(event.run_id, {})
        payload = dict(event.payload or {})
        if event.type == RuntimeEventType.LLM_FINISHED:
            _increment(metrics, "llm_calls")
            _increment(metrics, "llm_stream_total_ms", payload.get("stream_duration_ms"))
            _increment(
                metrics,
                "llm_wall_total_ms",
                _first_present_value(payload, "wall_duration_ms", "duration_ms", "stream_duration_ms"),
            )
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            if "cache_total_input_tokens" in usage:
                _increment(metrics, "llm_cache_hit_tokens", usage.get("cache_hit_tokens"))
                _increment(metrics, "llm_cache_total_input_tokens", usage.get("cache_total_input_tokens"))
        elif event.type == RuntimeEventType.AGENT_ITERATION_FINISHED:
            if payload.get("caller") == "main":
                _increment(metrics, "main_iterations")
        elif event.type == RuntimeEventType.TOOL_STARTED:
            _increment(metrics, "tool_calls_started")
        elif event.type == RuntimeEventType.TOOL_FINISHED:
            _increment(metrics, "tool_calls_finished")
            _increment(metrics, "tool_call_total_ms", payload.get("elapsed_ms"))

    def _track_run_labels(self, event: RuntimeEvent) -> None:
        if event.type != RuntimeEventType.RUN_STARTED:
            return
        payload = dict(event.payload or {})
        self._run_labels[event.run_id] = _copy_label_fields(
            payload,
            "workspace_id",
            "model",
            "model_provider",
            "agent_mode",
            "selected_ontology_id",
        )

    def _run_summary(self, event: RuntimeEvent) -> Dict[str, Any]:
        payload = dict(event.payload or {})
        metrics = dict(self._run_metrics.get(event.run_id) or {})
        _copy_present(metrics, "total_duration_ms", payload.get("duration_ms"))
        _set_cache_hit_rate(metrics)
        return _record(
            event,
            kind="run_summary",
            status=payload.get("status"),
            metrics=metrics,
            labels=dict(self._run_labels.get(event.run_id) or {}),
        )

    def flush(self) -> None:
        flush = getattr(self._handler, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        self._handler.close()


def create_local_perf_exporter(config: Mapping[str, Any]) -> LocalPerfExporter:
    return LocalPerfExporter(config)


_RUN_TERMINAL_EVENTS = {
    RuntimeEventType.RUN_FINISHED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}


def _project_event(event: RuntimeEvent) -> list[Dict[str, Any]]:
    payload = dict(event.payload or {})
    if event.type in {
        RuntimeEventType.RUN_STARTED,
        RuntimeEventType.RUN_FINISHED,
        RuntimeEventType.RUN_FAILED,
        RuntimeEventType.RUN_CANCELLED,
    }:
        return [
            _record(
                event,
                kind="run",
                status=payload.get("status"),
                metrics=_copy_metric_fields(
                    payload,
                    "duration_ms",
                    "user_query_chars",
                    "final_answer_chars",
                ),
                labels=_copy_label_fields(payload, "workspace_id", "model", "model_provider"),
            )
        ]
    if event.type in {
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    }:
        return [
            _record(
                event,
                kind="agent_loop",
                status=payload.get("status"),
                caller=payload.get("caller"),
                labels=_copy_label_fields(
                    payload,
                    "agent_mode",
                    "execution_path",
                    "ontology_id",
                    "operation_key",
                ),
            )
        ]
    if event.type in {
        RuntimeEventType.AGENT_ITERATION_PREPARED,
        RuntimeEventType.AGENT_ITERATION_FINISHED,
    }:
        return [
            _record(
                event,
                kind="agent_iteration",
                status=payload.get("status"),
                caller=payload.get("caller"),
                iteration=payload.get("iteration"),
                metrics=_iteration_metrics(payload),
                labels=_copy_label_fields(payload, "purpose", "agent_mode"),
            )
        ]
    if event.type == RuntimeEventType.LLM_FINISHED:
        return [
            _record(
                event,
                kind="llm_call",
                status=payload.get("status"),
                caller=payload.get("caller"),
                iteration=payload.get("iteration"),
                metrics=_llm_finished_metrics(payload),
                labels=_copy_label_fields(
                    payload,
                    "request_label",
                    "model",
                    "model_provider",
                    "finish_reason",
                ),
            )
        ]
    if event.type in {
        RuntimeEventType.TOOL_BATCH_STARTED,
        RuntimeEventType.TOOL_BATCH_FINISHED,
        RuntimeEventType.TOOL_BATCH_FAILED,
    }:
        return [
            _record(
                event,
                kind="tool_batch",
                status=payload.get("status"),
                caller=payload.get("caller"),
                iteration=payload.get("iteration"),
                metrics=_copy_metric_fields(payload, "tool_calls_count", "duration_ms", "tool_names"),
            )
        ]
    if event.type in {RuntimeEventType.TOOL_STARTED, RuntimeEventType.TOOL_FINISHED}:
        return [
            _record(
                event,
                kind="tool_call",
                status=payload.get("status"),
                caller=payload.get("caller"),
                iteration=payload.get("iteration"),
                metrics=_tool_metrics(payload),
                labels=_copy_label_fields(
                    payload,
                    "tool_name",
                    "display_name",
                    "tool_call_id",
                    "script_name",
                ),
            )
        ]
    if event.type in {
        RuntimeEventType.RUNTIME_PHASE_STARTED,
        RuntimeEventType.RUNTIME_PHASE_FINISHED,
        RuntimeEventType.RUNTIME_PHASE_FAILED,
    }:
        return [
            _record(
                event,
                kind="runtime_phase",
                status=payload.get("status"),
                metrics=_runtime_phase_metrics(payload),
                labels=_copy_label_fields(
                    payload,
                    "phase_name",
                    "component",
                    "route",
                    "reason",
                    "ontology_id",
                    "operation_key",
                    "execution_path",
                    "error_type",
                ),
            )
        ]
    return []


def _record(
    event: RuntimeEvent,
    *,
    kind: str,
    status: Any = None,
    caller: Any = None,
    iteration: Any = None,
    metrics: Optional[Dict[str, Any]] = None,
    labels: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "schema_version": 1,
        "kind": kind,
        "event": event.type,
        "ts_ms": event.timestamp_ms,
        "run_id": event.run_id,
    }
    _copy_present(record, "session_id", event.session_id)
    _copy_present(record, "turn_id", event.turn_id)
    _copy_present(record, "span_key", event.span_key)
    _copy_present(record, "parent_key", event.parent_key)
    _copy_present(record, "status", status)
    _copy_present(record, "caller", caller)
    _copy_present(record, "iteration", iteration)
    record["metrics"] = metrics or {}
    record["labels"] = labels or {}
    return record


def _llm_finished_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for key in (
        "response_headers_ms",
        "duration_ms",
        "stream_duration_ms",
        "wall_duration_ms",
        "first_reasoning_ms",
        "first_content_ms",
        "first_tool_call_ms",
        "tool_calls_count",
    ):
        _copy_present(metrics, key, payload.get(key))

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        _copy_present(metrics, key, usage.get(key))
    for key in ("cache_hit_tokens", "cache_miss_tokens", "cache_total_input_tokens", "cache_hit_rate"):
        _copy_present(metrics, key, usage.get(key))

    text_metrics = {
        "prompt_chars": (payload.get("prompt_chars"), payload.get("prompt_text")),
        "completion_chars": (payload.get("completion_chars"), payload.get("completion_text")),
        "reasoning_chars": (payload.get("reasoning_chars"), payload.get("reasoning_text")),
    }
    for metric_name, (explicit_value, raw_value) in text_metrics.items():
        if explicit_value is not None:
            metrics[metric_name] = explicit_value
        elif raw_value is not None:
            metrics[metric_name] = len(str(raw_value or ""))
    return metrics


def _iteration_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _copy_metric_fields(
        payload,
        "prompt_build_ms",
        "system_prompt_chars",
        "sliding_window_applied",
        "llm_message_count",
        "llm_input_chars",
        "tools_count",
        "llm_stream_ms",
        "llm_wall_ms",
        "tools_window_ms",
        "tools_overlap_llm_ms",
        "tools_after_llm_ms",
        "tool_calls_count",
        "tool_names",
        "total_ms",
        "total_elapsed_ms",
    )


def _tool_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics = _copy_metric_fields(
        payload,
        "elapsed_ms",
        "duration_ms",
        "terminal",
        "state_update_keys",
    )
    raw_input = payload.get("input")
    if raw_input is not None:
        metrics["input_chars"] = len(json.dumps(raw_input, ensure_ascii=True, default=str))
    raw_output = payload.get("output_preview")
    if raw_output is not None:
        metrics["output_chars"] = len(str(raw_output or ""))
    return metrics


def _runtime_phase_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    metrics = _copy_metric_fields(payload, "duration_ms")
    for key, value in payload.items():
        if key in {"phase_name", "component", "status", "duration_ms"}:
            continue
        if isinstance(value, (bool, int, float)):
            metrics[key] = value
        elif isinstance(value, list):
            metrics[key] = _bounded_list(value)
    return metrics


def _copy_metric_fields(payload: Dict[str, Any], *field_names: str) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for key in field_names:
        value = payload.get(key)
        if isinstance(value, list):
            value = _bounded_list(value)
        _copy_present(metrics, key, value)
    return metrics


def _copy_label_fields(payload: Dict[str, Any], *field_names: str) -> Dict[str, Any]:
    labels: Dict[str, Any] = {}
    for key in field_names:
        value = payload.get(key)
        if value is None or value == "":
            continue
        labels[key] = str(value)[:160]
    return labels


def _bounded_list(values: list[Any], *, limit: int = 20) -> list[Any]:
    return list(values[:limit])


def _copy_present(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is None or value == "":
        return
    target[key] = value


def _first_present_value(source: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= 1 else default


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _set_cache_hit_rate(metrics: Dict[str, Any]) -> None:
    hit_tokens = _optional_int(metrics.get("llm_cache_hit_tokens"))
    total_input_tokens = _optional_int(metrics.get("llm_cache_total_input_tokens"))
    if hit_tokens is None or total_input_tokens is None or total_input_tokens <= 0:
        return
    metrics["llm_cache_hit_rate"] = hit_tokens / total_input_tokens


def _increment(metrics: Dict[str, int], key: str, value: Any = 1) -> None:
    try:
        increment = int(value)
    except (TypeError, ValueError):
        return
    metrics[key] = int(metrics.get(key) or 0) + increment
