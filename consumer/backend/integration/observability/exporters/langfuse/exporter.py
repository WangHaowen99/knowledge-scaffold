"""Langfuse exporter for runtime observability events."""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, Mapping, Optional

from utils.logging import get_logger
from integration.observability.events import RuntimeEventType, RuntimeRequestLabel, RuntimeStatus
from integration.observability.exporters.langfuse.client import LangfuseTraceAdapter, create_langfuse_client

logger = get_logger("observability.exporters.langfuse.exporter")

_LINEAGE_FIELDS = {
    "subagent_id",
    "task_id",
    "group_id",
    "depth",
    "parent_subagent_id",
    "root_tool_call_id",
    "task_index",
}


class LangfuseExporter:
    """Translate vendor-neutral runtime events into Langfuse traces.

    This layer owns event-to-Langfuse mapping and span-key bookkeeping. Direct
    SDK calls, sanitization, and client lifecycle are delegated to the adapter.
    """

    def __init__(self, adapter: Any) -> None:
        self._trace = adapter
        self._trace_ids_by_run: Dict[str, str] = {}
        self._spans_by_run: Dict[str, Dict[str, str]] = {}
        self._tool_progress_by_run: Dict[str, Dict[str, list]] = {}
        self._run_stats_by_run: Dict[str, Dict[str, Any]] = {}
        self._terminal_runs: set[str] = set()
        self._handlers: Dict[str, Callable[[Any], None]] = {
            RuntimeEventType.RUN_STARTED: self._handle_run_started,
            RuntimeEventType.RUN_FINISHED: self._handle_run_finished,
            RuntimeEventType.RUN_FAILED: self._handle_run_finished,
            RuntimeEventType.RUN_CANCELLED: self._handle_run_finished,
            RuntimeEventType.AGENT_LOOP_STARTED: self._handle_agent_loop_started,
            RuntimeEventType.AGENT_LOOP_FINISHED: self._end_success_span,
            RuntimeEventType.LLM_STARTED: self._handle_llm_started,
            RuntimeEventType.LLM_FINISHED: self._handle_llm_finished,
            RuntimeEventType.LLM_FAILED: self._end_error_span,
            RuntimeEventType.TOOL_BATCH_STARTED: self._handle_tool_batch_started,
            RuntimeEventType.TOOL_BATCH_FINISHED: self._end_success_span,
            RuntimeEventType.TOOL_BATCH_FAILED: self._end_error_span,
            RuntimeEventType.TOOL_STARTED: self._handle_tool_started,
            RuntimeEventType.TOOL_PROGRESS: self._handle_tool_progress,
            RuntimeEventType.TOOL_FINISHED: self._handle_tool_finished,
        }

    @property
    def available(self) -> bool:
        return bool(getattr(self._trace, "available", True))

    def export(self, event) -> None:
        handler = self._handlers.get(event.type)
        if handler:
            handler(event)

    def flush(self) -> None:
        self._trace.flush()

    def close(self) -> None:
        close = getattr(self._trace, "close", None)
        if callable(close):
            close()
        else:
            self._trace.flush()

    def _handle_run_started(self, event) -> None:
        if event.run_id in self._trace_ids_by_run:
            return
        self._terminal_runs.discard(event.run_id)
        payload = dict(event.payload or {})
        model = payload.get("model")
        workspace_id = str(payload.get("workspace_id") or "default")
        self._run_stats_by_run[event.run_id] = {
            "started_at_ms": event.timestamp_ms,
            "model": model,
            "model_provider": payload.get("model_provider"),
            "llm_calls": 0,
            "tool_calls_started": 0,
            "tool_calls_finished": 0,
            "main_iterations": 0,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "subagents": {},
        }
        trace_id = self._trace.create_trace(
            name="agent_request:{}".format(event.run_id),
            user_id=payload.get("user_id"),
            session_id=event.session_id,
            metadata={
                "trace_id": event.trace_id,
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "workspace_id": workspace_id,
                "request_id": event.run_id,
                "run_id": event.run_id,
                "started_at_ms": event.timestamp_ms,
                "model": model,
                "model_provider": payload.get("model_provider"),
                "model_profile_id": payload.get("model_profile_id"),
                "protocol": "agent-stream.v2",
                "planner_thinking_enabled": False,
            },
            tags=[
                "agentic_chat",
                "workspace:{}".format(workspace_id),
                "run:{}".format(event.run_id),
                "session:{}".format(event.session_id),
                "stream",
            ],
            input={
                "user_query": str(payload.get("user_query") or "")[:500],
                "user_query_full": str(payload.get("user_query") or ""),
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "started_at_ms": event.timestamp_ms,
                "model": model,
            },
            trace_id=_stable_trace_id(event),
        )
        if trace_id:
            self._trace_ids_by_run[event.run_id] = trace_id
            self._spans_by_run.setdefault(event.run_id, {})

    def _handle_run_finished(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        if not trace_id:
            return
        payload = dict(event.payload or {})
        status = payload.get("status") or _status_from_event_type(event.type)
        stats = self._run_stats(event.run_id)
        duration_ms = payload.get("duration_ms")
        if duration_ms is None and stats.get("started_at_ms") is not None:
            duration_ms = max(0, int(event.timestamp_ms) - int(stats["started_at_ms"]))
        self._trace.update_trace_output(
            trace_id,
            {
                "final_answer": str(payload.get("final_answer") or "")[:2000],
                "final_answer_full": str(payload.get("final_answer") or ""),
                "status": status,
                "finished_at_ms": event.timestamp_ms,
                "duration_ms": duration_ms,
                "main_iterations": stats.get("main_iterations", 0),
                "llm_calls": stats.get("llm_calls", 0),
                "tool_calls_started": stats.get("tool_calls_started", 0),
                "tool_calls_finished": stats.get("tool_calls_finished", 0),
                "usage": dict(stats.get("usage") or {}),
                "subagents": list((stats.get("subagents") or {}).values()),
                **{
                    key: value
                    for key, value in payload.items()
                    if key in {"cancel_reason", "error_type", "error_message"}
                },
            },
        )
        self._terminal_runs.add(event.run_id)
        self._cleanup_run_if_terminal_idle(event.run_id)

    def _handle_agent_loop_started(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        if not trace_id or not event.span_key:
            return
        payload = dict(event.payload or {})
        caller = str(payload.get("caller") or "main")
        self._record_subagent_lineage(event.run_id, payload)
        span_id = self._trace.create_span(
            trace_id=trace_id,
            name="agent_loop:{}".format(caller),
            metadata={**_event_identity_metadata(event), **payload, "started_at_ms": event.timestamp_ms},
            tags=["agent_loop", caller],
        )
        self._store_span(event, span_id)

    def _handle_llm_started(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        if not trace_id or not event.span_key:
            return
        payload = dict(event.payload or {})
        label = str(payload.get("request_label") or "")
        caller = str(payload.get("caller") or "main")
        iteration = payload.get("iteration")
        call_index = payload.get("call_index")
        self._record_llm_started(event.run_id, payload)
        if label == RuntimeRequestLabel.REASONING_SUMMARY:
            name = "llm:reasoning_summary"
        elif call_index is not None and iteration is not None:
            name = "llm:{}_iteration_{}_call_{}".format(caller, iteration, call_index)
        else:
            name = "llm:{}_iteration".format(caller)
        tags = ["llm", caller] + (["iteration_{}".format(iteration)] if iteration is not None else [])
        if call_index is not None:
            tags.append("call_{}".format(call_index))
        span_id = self._trace.create_llm_span(
            trace_id=trace_id,
            name=name,
            metadata={
                **_event_identity_metadata(event),
                **_metadata_without_payload(payload, {"input"}),
            },
            tags=tags,
            parent_observation_id=self._span_id(event.run_id, event.parent_key),
            input=payload.get("input"),
            model=payload.get("model"),
        )
        self._store_span(event, span_id)

    def _handle_llm_finished(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        span_id = self._span_id(event.run_id, event.span_key)
        if not trace_id or not span_id:
            return
        payload = dict(event.payload or {})
        self._record_llm_finished(event.run_id, payload)
        self._trace.update_llm_span_tokens(
            trace_id,
            span_id,
            payload.get("usage") or {},
            str(payload.get("prompt_text") or ""),
            str(payload.get("completion_text") or ""),
            output={
                **dict(payload.get("output") or {}),
                "metrics": _metadata_without_payload(
                    payload,
                    {"prompt_text", "completion_text", "reasoning_text", "tool_calls", "output"},
                ),
            },
        )
        self._forget_span(event)

    def _handle_tool_batch_started(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        if not trace_id or not event.span_key:
            return
        payload = dict(event.payload or {})
        caller = str(payload.get("caller") or "main")
        iteration = payload.get("iteration")
        self._record_subagent_lineage(event.run_id, payload)
        span_id = self._trace.create_span(
            trace_id=trace_id,
            name="tools:{}_iteration_{}".format(caller, iteration),
            metadata={**_event_identity_metadata(event), **payload, "started_at_ms": event.timestamp_ms},
            tags=["tools", caller] + (["iteration_{}".format(iteration)] if iteration is not None else []),
            parent_observation_id=self._span_id(event.run_id, event.parent_key),
        )
        self._store_span(event, span_id)

    def _handle_tool_started(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        if not trace_id or not event.span_key:
            return
        payload = dict(event.payload or {})
        tool_name = payload.get("tool_name") or "unknown_tool"
        display_name = str(payload.get("display_name") or "").strip()
        span_display_name = "tool:{}".format(display_name or tool_name)

        self._record_tool_started(event.run_id, payload)
        span_id = self._trace.create_span(
            trace_id=trace_id,
            name=span_display_name,
            metadata={
                **_event_identity_metadata(event),
                **_metadata_without_payload(payload, {"input"}),
                "started_at_ms": event.timestamp_ms,
            },
            tags=["tool", str(tool_name)],
            parent_observation_id=self._span_id(event.run_id, event.parent_key),
            input=payload.get("input"),
        )

        self._store_span(event, span_id)

    def _handle_tool_progress(self, event) -> None:
        if not event.span_key:
            return
        payload = dict(event.payload or {})
        progress_by_span = self._tool_progress_by_run.setdefault(event.run_id, {})
        progress_events = progress_by_span.setdefault(event.span_key, [])
        progress_events.append(
            {
                "ts_ms": event.timestamp_ms,
                **payload,
            }
        )
        if len(progress_events) > 200:
            del progress_events[:-200]

    def _handle_tool_finished(self, event) -> None:
        trace_id = self._trace_id(event.run_id)
        span_id = self._span_id(event.run_id, event.span_key)

        if not trace_id or not span_id:
            return

        payload = dict(event.payload or {})
        tool_name = payload.get("tool_name") or "unknown_tool"
        status = payload.get("status") or RuntimeStatus.SUCCESS

        self._record_tool_finished(event.run_id, payload)
        progress_events = self._tool_progress_by_run.get(event.run_id, {}).pop(event.span_key, [])
        output = {
            **payload,
            "finished_at_ms": event.timestamp_ms,
        }
        if progress_events:
            output["progress_events"] = progress_events

        try:
            self._trace.end_observation(
                trace_id,
                span_id,
                status=str(status),
                output=output,
            )
        except Exception as exc:
            logger.error(
                "_handle_tool_finished: EXCEPTION run=%s tool=%s span_id=%s error=%s",
                event.run_id,
                tool_name,
                span_id,
                exc,
                exc_info=True,
            )

        self._forget_span(event)

    def _end_success_span(self, event) -> None:
        self._end_span(event, default_status=RuntimeStatus.SUCCESS)

    def _end_error_span(self, event) -> None:
        self._end_span(event, default_status=RuntimeStatus.ERROR)

    def _end_span(self, event, *, default_status: str) -> None:
        trace_id = self._trace_id(event.run_id)
        span_id = self._span_id(event.run_id, event.span_key)
        if not trace_id or not span_id:
            return
        payload = dict(event.payload or {})
        self._trace.end_observation(
            trace_id,
            span_id,
            status=str(payload.get("status") or default_status),
            output=payload.get("output") if payload.get("output") is not None else payload,
        )
        self._forget_span(event)

    def _trace_id(self, run_id: str) -> Optional[str]:
        return self._trace_ids_by_run.get(run_id)

    def _span_id(self, run_id: str, span_key: Optional[str]) -> Optional[str]:
        if not span_key:
            return None
        return self._spans_by_run.get(run_id, {}).get(span_key)

    def _store_span(self, event, span_id: Optional[str]) -> None:
        if not span_id or not event.span_key:
            return
        self._spans_by_run.setdefault(event.run_id, {})[event.span_key] = span_id

    def _forget_span(self, event) -> None:
        if not event.span_key:
            return
        self._spans_by_run.get(event.run_id, {}).pop(event.span_key, None)
        self._cleanup_run_if_terminal_idle(event.run_id)

    def _cleanup_run_if_terminal_idle(self, run_id: str) -> None:
        if run_id not in self._terminal_runs:
            return
        if self._spans_by_run.get(run_id):
            return
        self._terminal_runs.discard(run_id)
        self._spans_by_run.pop(run_id, None)
        self._tool_progress_by_run.pop(run_id, None)
        self._run_stats_by_run.pop(run_id, None)
        self._trace_ids_by_run.pop(run_id, None)

    def _run_stats(self, run_id: str) -> Dict[str, Any]:
        return self._run_stats_by_run.setdefault(
            run_id,
            {
                "llm_calls": 0,
                "tool_calls_started": 0,
                "tool_calls_finished": 0,
                "main_iterations": 0,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "subagents": {},
            },
        )

    def _record_llm_started(self, run_id: str, payload: Dict[str, Any]) -> None:
        stats = self._run_stats(run_id)
        stats["llm_calls"] = int(stats.get("llm_calls") or 0) + 1
        if payload.get("caller") == "main" and payload.get("iteration") is not None:
            stats["main_iterations"] = max(
                int(stats.get("main_iterations") or 0),
                int(payload.get("iteration") or 0),
            )
        self._record_subagent_lineage(run_id, payload)

    def _record_llm_finished(self, run_id: str, payload: Dict[str, Any]) -> None:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        totals = self._run_stats(run_id).setdefault(
            "usage",
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            totals[key] = int(totals.get(key) or 0) + int(usage.get(key) or 0)

    def _record_tool_started(self, run_id: str, payload: Dict[str, Any]) -> None:
        stats = self._run_stats(run_id)
        stats["tool_calls_started"] = int(stats.get("tool_calls_started") or 0) + 1
        self._record_subagent_lineage(run_id, payload)

    def _record_tool_finished(self, run_id: str, payload: Dict[str, Any]) -> None:
        stats = self._run_stats(run_id)
        stats["tool_calls_finished"] = int(stats.get("tool_calls_finished") or 0) + 1
        self._record_subagent_lineage(run_id, payload)

    def _record_subagent_lineage(self, run_id: str, payload: Dict[str, Any]) -> None:
        subagent_id = str(payload.get("subagent_id") or "").strip()
        if not subagent_id:
            return
        lineage = {key: payload.get(key) for key in _LINEAGE_FIELDS if payload.get(key) not in (None, "")}
        lineage["subagent_id"] = subagent_id
        self._run_stats(run_id).setdefault("subagents", {})[subagent_id] = lineage


def create_langfuse_exporter(config: Mapping[str, Any]) -> LangfuseExporter:
    """Build a Langfuse exporter from observability provider config."""
    langfuse_section = config.get("langfuse") or {}
    if not isinstance(langfuse_section, dict):
        raise ValueError("Langfuse exporter requires observability.langfuse config")
    langfuse_config = {
        **langfuse_section,
        "enabled": True,
    }
    client = create_langfuse_client(langfuse_config)
    adapter = LangfuseTraceAdapter(client)
    return LangfuseExporter(adapter)


def _status_from_event_type(event_type: str) -> str:
    if event_type == RuntimeEventType.RUN_FAILED:
        return RuntimeStatus.ERROR
    if event_type == RuntimeEventType.RUN_CANCELLED:
        return RuntimeStatus.CANCELLED
    return RuntimeStatus.SUCCESS


def _stable_trace_id(event) -> str:
    """Join a validated caller trace, otherwise derive a stable local id."""

    event_trace_id = str(getattr(event, "trace_id", "") or "").strip().lower()
    if (
        len(event_trace_id) == 32
        and event_trace_id != "0" * 32
        and all(char in "0123456789abcdef" for char in event_trace_id)
    ):
        return event_trace_id

    payload = getattr(event, "payload", None)
    if isinstance(payload, dict):
        external_trace_id = str(payload.get("external_trace_id") or "").strip().lower()
        if (
            len(external_trace_id) == 32
            and external_trace_id != "0" * 32
            and all(char in "0123456789abcdef" for char in external_trace_id)
        ):
            return external_trace_id
    return uuid.uuid5(
        uuid.NAMESPACE_DNS,
        "{}:{}".format(event.session_id or "", event.run_id),
    ).hex


def _metadata_without_payload(payload: Dict[str, Any], omitted_keys: set[str]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in omitted_keys}


def _event_identity_metadata(event) -> Dict[str, Any]:
    values = {
        "trace_id": getattr(event, "trace_id", None),
        "span_id": getattr(event, "span_id", None),
        "parent_span_id": getattr(event, "parent_span_id", None),
    }
    metadata = {}
    for key, value in values.items():
        if value:
            metadata[key] = value
    return metadata
