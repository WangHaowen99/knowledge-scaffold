# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Low-cardinality request performance instrumentation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
from typing import Any, Iterator

from utils.logging import get_logger


logger = get_logger("observability.performance")


SERVER_TIMING_PHASES = frozenset(
    {
        "auth",
        "handler",
        "model_profile_references",
        "ontology_view",
        "request",
        "system_config",
        "workspace_catalog",
    }
)
CACHE_NAMES = frozenset(
    {
        "model_profile_references",
        "ontology_view",
        "system_config",
        "workspace_catalog",
    }
)
CACHE_RESULTS = frozenset({"bypass", "error", "hit", "miss", "stale"})

_timings_var: ContextVar[dict[str, float] | None] = ContextVar(
    "consumer_request_performance_timings",
    default=None,
)
_cache_counter: Any = None
_phase_histogram: Any = None
_cache_counter_lock = threading.Lock()


@contextmanager
def request_performance_context() -> Iterator[None]:
    token = _timings_var.set({})
    try:
        yield
    finally:
        _timings_var.reset(token)


def record_timing(
    name: str,
    duration_ms: float,
    *,
    workspace_id: str = "",
) -> bool:
    """Record a request phase.

    Server-Timing is snapshotted when ASGI emits ``http.response.start``.
    Timings recorded later by a streaming body remain available as OTel
    metrics, but cannot be added to an HTTP header that is already sent.
    """
    resolved_name = str(name or "").strip()
    if resolved_name not in SERVER_TIMING_PHASES:
        return False
    if isinstance(duration_ms, bool):
        return False
    try:
        resolved_duration = float(duration_ms)
    except (TypeError, ValueError):
        return False
    if resolved_duration < 0 or not math.isfinite(resolved_duration):
        return False
    timings = _timings_var.get()
    if timings is None:
        return False
    timings[resolved_name] = timings.get(resolved_name, 0.0) + resolved_duration
    _record_phase_metric(
        resolved_name,
        resolved_duration,
        workspace_id=workspace_id,
    )
    return True


def get_server_timing_header() -> str:
    timings = _timings_var.get()
    if not timings:
        return ""
    return ", ".join(
        "{};dur={}".format(name, _format_duration(duration))
        for name, duration in timings.items()
    )


def record_cache_event(
    cache_name: str,
    result: str,
    *,
    workspace_id: str = "",
) -> bool:
    resolved_cache = str(cache_name or "").strip()
    resolved_result = str(result or "").strip()
    if resolved_cache not in CACHE_NAMES or resolved_result not in CACHE_RESULTS:
        return False
    route = _resolve_metric_route(workspace_id)
    if route is not None:
        try:
            from integration.observability.otel import record_cache_performance_metric

            if record_cache_performance_metric(
                resolved_cache,
                resolved_result,
                router_id=route[0],
                workspace_id=route[1],
            ):
                return True
        except Exception:
            logger.warning(
                "Scoped cache performance metric recording failed",
                exc_info=True,
            )
    try:
        _get_cache_counter().add(
            1,
            {"cache": resolved_cache, "result": resolved_result},
        )
        return True
    except Exception:
        logger.warning("Cache performance metric recording failed", exc_info=True)
        return False


def record_cache_access(
    cache_name: str,
    result: str,
    duration_ms: float,
    *,
    workspace_id: str = "",
) -> None:
    """Record one allowlisted cache outcome and its request-scoped duration."""
    record_timing(cache_name, duration_ms, workspace_id=workspace_id)
    record_cache_event(cache_name, result, workspace_id=workspace_id)


def _get_cache_counter() -> Any:
    global _cache_counter
    with _cache_counter_lock:
        if _cache_counter is None:
            from opentelemetry import metrics

            meter = metrics.get_meter("onto.consumer.performance")
            _cache_counter = meter.create_counter(
                "agent.cache.event.count",
                description="Allowlisted process cache outcomes",
            )
        return _cache_counter


def _record_phase_metric(
    phase_name: str,
    duration_ms: float,
    *,
    workspace_id: str,
) -> None:
    route = _resolve_metric_route(workspace_id)
    if route is not None:
        try:
            from integration.observability.otel import record_phase_performance_metric

            if record_phase_performance_metric(
                phase_name,
                duration_ms,
                router_id=route[0],
                workspace_id=route[1],
            ):
                return
        except Exception:
            logger.warning(
                "Scoped request phase metric recording failed",
                exc_info=True,
            )
    try:
        _get_phase_histogram().record(
            duration_ms,
            {"phase": phase_name},
        )
    except Exception:
        logger.warning("Request phase metric recording failed", exc_info=True)


def _get_phase_histogram() -> Any:
    global _phase_histogram
    with _cache_counter_lock:
        if _phase_histogram is None:
            from opentelemetry import metrics

            meter = metrics.get_meter("onto.consumer.performance")
            _phase_histogram = meter.create_histogram(
                "agent.request.phase.duration",
                unit="ms",
                description="Allowlisted request phase durations",
            )
        return _phase_histogram


def _resolve_metric_route(workspace_id: str) -> tuple[str, str] | None:
    resolved_workspace = str(workspace_id or "").strip()
    try:
        if not resolved_workspace:
            from workspace.workspaces import get_bound_workspace_id

            resolved_workspace = str(get_bound_workspace_id() or "").strip()
        if not resolved_workspace:
            return None
        from integration.observability.workspace_router import (
            get_default_workspace_router,
        )

        router = get_default_workspace_router()
        router_id = str(getattr(router, "router_id", "") or "").strip()
        if not router_id:
            return None
        return router_id, resolved_workspace
    except Exception:
        return None


def _format_duration(duration: float) -> str:
    value = ("{:.3f}".format(max(0.0, duration))).rstrip("0").rstrip(".")
    return value or "0"
