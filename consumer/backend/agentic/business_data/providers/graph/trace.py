"""Vendor-neutral trace facade for provider-internal graph tools."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping

from ....observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from ....observability.provider import get_observer
from ....utils.logging import get_logger
from .models import GraphBackendResult, GraphSearchRuntimeContext

logger = get_logger("ontology_search.graph.trace")


class GraphProviderTrace:
    """Emit observability spans for GraphQueryAgent internal tool executions."""

    def __init__(self, context: GraphSearchRuntimeContext, observer: Any | None = None) -> None:
        provider_config = dict(context.provider_config or {})
        self.context = context
        self.run_id = str(provider_config.get("run_id") or "").strip()
        self.session_id = str(provider_config.get("session_id") or "").strip() or None
        self.turn_id = _optional_int(provider_config.get("turn_id"))
        # Parent span (outer tool_batch that invoked business_data_search) and the
        # agent iteration, so internal graph tool spans attach to the span tree
        # and get grouped under the right iteration in local perf analysis.
        self.parent_span_key = str(provider_config.get("parent_span_key") or "").strip() or None
        self.iteration = _optional_int(provider_config.get("agent_iteration"))
        self.observer = observer if observer is not None else get_observer()
        self.enabled = bool(self.run_id and getattr(self.observer, "enabled", False))
        self._counter = 0

    def tool_started(self, *, tool_name: str, tool_args: Mapping[str, Any]) -> tuple[str, float]:
        self._counter += 1
        span_key = "tool:graph:{}:{}".format(_safe_span_token(tool_name), self._counter)
        started = time.monotonic()
        self._emit(
            RuntimeEventType.TOOL_STARTED,
            span_key=span_key,
            payload={
                **self._base_payload(tool_name=tool_name),
                "status": RuntimeStatus.SUCCESS,
                "input": {
                    "tool_args_digest": _digest(tool_args),
                    "tool_arg_keys": sorted(str(key) for key in dict(tool_args or {}).keys()),
                },
            },
        )
        return span_key, started

    def tool_finished(
        self,
        *,
        span_key: str,
        started: float,
        tool_name: str,
        backend_result: GraphBackendResult | None,
        payload: Mapping[str, Any] | None,
    ) -> None:
        result_count = None
        truncated = False
        warnings_count = 0
        if backend_result is not None:
            result_count = (
                backend_result.total
                if backend_result.total is not None
                else len(backend_result.records or [])
            )
            truncated = bool(backend_result.truncated)
            warnings_count = len(backend_result.warnings or [])
        self._emit(
            RuntimeEventType.TOOL_FINISHED,
            span_key=span_key,
            payload={
                **self._base_payload(tool_name=tool_name),
                "status": RuntimeStatus.SUCCESS,
                "duration_ms": _elapsed_ms(started),
                "result_count": result_count,
                "truncated": truncated,
                "warnings_count": warnings_count,
                "output_preview": _payload_preview(payload),
            },
        )

    def tool_failed(
        self,
        *,
        span_key: str,
        started: float,
        tool_name: str,
        error: Exception,
    ) -> None:
        self._emit(
            RuntimeEventType.TOOL_FINISHED,
            span_key=span_key,
            payload={
                **self._base_payload(tool_name=tool_name),
                "status": RuntimeStatus.ERROR,
                "duration_ms": _elapsed_ms(started),
                "error_type": type(error).__name__,
                "error": str(error)[:300],
            },
        )

    def _base_payload(self, *, tool_name: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "display_name": "graph:{}".format(tool_name),
            "provider": self.context.provider,
            "ontology_id": self.context.ontology_id,
            "workspace_id": self.context.workspace_id,
            "entity": self.context.entity,
            "engine": "graph_search_runtime",
            "request_label": "graph_internal_tool",
        }
        if self.iteration is not None:
            payload["iteration"] = self.iteration
        return payload

    def _emit(
        self,
        event_type: str,
        *,
        span_key: str,
        payload: Mapping[str, Any],
    ) -> None:
        if not self.enabled:
            return
        try:
            self.observer.emit(
                RuntimeEvent(
                    type=event_type,
                    run_id=self.run_id,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                    span_key=span_key,
                    parent_key=self.parent_span_key,
                    payload=dict(payload or {}),
                )
            )
        except Exception:
            logger.warning("Failed to emit graph provider trace event", exc_info=True)


def _optional_int(value: Any) -> int | None:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved or None


def _safe_span_token(value: Any) -> str:
    token = str(value or "unknown").strip().replace(" ", "_")
    return token or "unknown"


def _elapsed_ms(started: float) -> int:
    return int(max(0.0, time.monotonic() - started) * 1000)


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return "sha256:{}".format(hashlib.sha256(raw).hexdigest())


def _payload_preview(payload: Mapping[str, Any] | None) -> str:
    if not payload:
        return ""
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        raw = str(payload)
    return raw[:1000]

