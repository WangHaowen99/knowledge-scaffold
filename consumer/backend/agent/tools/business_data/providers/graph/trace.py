# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Observability bridge for provider-internal graph tools and model calls."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Mapping
from typing import Any

from agent.tools.business_data.providers.base import OntologySearchEvent, normalize_event
from integration.observability import (
    AgentRuntimeTrace,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeStatus,
    get_observability_context,
    get_observer,
)
from utils.logging import get_logger

from .models import GraphSearchRuntimeContext


logger = get_logger("ontology_search.graph.trace")


class GraphToolTraceSink:
    """Translate normalized graph-tool progress into child runtime spans.

    The outer ``business_data_search`` tool owns the parent span. Runtime
    metadata is injected by the business-data service and is intentionally
    absent from persisted connection configuration.
    """

    def __init__(
        self,
        context: GraphSearchRuntimeContext,
        *,
        observer: Any | None = None,
        downstream: Any | None = None,
    ) -> None:
        provider_config = dict(context.provider_config or {})
        inherited = get_observability_context()
        self.context = context
        self.run_id = str(provider_config.get("run_id") or inherited.run_id or "").strip()
        self.session_id = (
            str(provider_config.get("session_id") or inherited.session_id or "").strip()
            or None
        )
        self.turn_id = _optional_int(provider_config.get("turn_id"))
        if self.turn_id is None:
            self.turn_id = inherited.turn_id
        self.parent_span_key = (
            str(provider_config.get("parent_span_key") or inherited.parent_key or "").strip()
            or None
        )
        self.iteration = _optional_int(provider_config.get("agent_iteration"))
        configured_observer = provider_config.get("_observer")
        self.observer = observer or configured_observer or inherited.observer or get_observer()
        self.enabled = bool(self.run_id and getattr(self.observer, "enabled", False))
        self.downstream = downstream
        self._counter = 0
        self._active: dict[str, list[tuple[str, float]]] = {}
        self._lock = threading.Lock()

    def __call__(self, event: OntologySearchEvent) -> None:
        normalized = normalize_event(event)
        event_type = normalized["type"].lower()
        tool_name = _tool_name_from_content(normalized["content"])
        if event_type == "start":
            self._start(tool_name)
        elif event_type in {"intermediate", "error"}:
            self._finish(tool_name, failed=event_type == "error")
        if callable(self.downstream):
            self.downstream(normalized)

    def _start(self, tool_name: str) -> None:
        with self._lock:
            self._counter += 1
            span_key = "tool:graph:{}:{}:{}".format(
                _safe_span_token(tool_name),
                _trace_token(self.context, self.parent_span_key),
                self._counter,
            )
            self._active.setdefault(tool_name, []).append((span_key, time.monotonic()))
        self._emit(
            RuntimeEventType.TOOL_STARTED,
            span_key=span_key,
            payload={
                **self._base_payload(tool_name),
                "status": "running",
            },
        )

    def _finish(self, tool_name: str, *, failed: bool) -> None:
        with self._lock:
            stack = self._active.get(tool_name) or []
            if not stack:
                return
            span_key, started = stack.pop()
            if not stack:
                self._active.pop(tool_name, None)
        self._emit(
            RuntimeEventType.TOOL_FINISHED,
            span_key=span_key,
            payload={
                **self._base_payload(tool_name),
                "status": RuntimeStatus.ERROR if failed else RuntimeStatus.SUCCESS,
                "duration_ms": int(max(0.0, time.monotonic() - started) * 1000),
            },
        )

    def _base_payload(self, tool_name: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "display_name": "graph:{}".format(tool_name),
            "provider": self.context.provider,
            "workspace_id": self.context.workspace_id,
            "ontology_id": self.context.ontology_id,
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
                    payload=dict(payload),
                )
            )
        except Exception:
            logger.warning("Failed to emit graph runtime trace event", exc_info=True)


def build_graph_agent_runtime_trace(context: GraphSearchRuntimeContext) -> AgentRuntimeTrace:
    """Build the current Jiuwen model-trace facade under the outer tool span."""

    sink = GraphToolTraceSink(context)
    trace = AgentRuntimeTrace(
        observer=sink.observer,
        run_id=sink.run_id,
        session_id=sink.session_id,
        turn_id=sink.turn_id,
        caller="graph:{}".format(_trace_token(context, sink.parent_span_key)),
    )
    if sink.parent_span_key:
        trace.loop_span_key = sink.parent_span_key
    return trace


def _tool_name_from_content(content: str) -> str:
    _, separator, suffix = str(content or "").rpartition(":")
    name = suffix.strip() if separator else str(content or "").strip()
    return name or "unknown"


def _safe_span_token(value: Any) -> str:
    token = str(value or "unknown").strip().replace(" ", "_")
    return token or "unknown"


def _trace_token(context: GraphSearchRuntimeContext, parent_span_key: str | None) -> str:
    raw = "|".join([
        str(parent_span_key or ""),
        str(context.provider_config.get("run_id") or ""),
        context.workspace_id,
        context.ontology_id,
        context.entity,
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
