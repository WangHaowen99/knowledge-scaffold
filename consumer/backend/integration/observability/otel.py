# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Process-level OpenTelemetry bootstrap for the consumer backend."""

from __future__ import annotations

import importlib
import os
import threading
import time
from typing import Any, Callable, Mapping, Optional

from integration.observability.otel_data import metric_record, snapshot_readable_span
from utils.logging import get_logger

try:
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover - guarded by initialize_open_telemetry

    class _SpanProcessorBase:
        pass


logger = get_logger("observability.otel")

_lock = threading.RLock()
_provider_initialized: bool = False
_meter_provider_initialized: bool = False
_instrumented_apps: set[int] = set()
_client_instrumentation_initialized: bool = False
_metric_instruments: Optional[dict[str, Any]] = None
_router_metric_recorders: dict[str, "_RouterMetricRecorder"] = {}
_batch_metric_recorders: dict[str, "_RouterBatchMetricRecorder"] = {}

_BATCH_SPAN_PROCESSOR_DEFAULTS = {
    "max_queue_size": 2048,
    "max_export_batch_size": 256,
    "schedule_delay_millis": 300,
    "export_timeout_millis": 5000,
}
_BATCH_SPAN_PROCESSOR_MAXIMUMS = {
    "max_queue_size": 65536,
    "max_export_batch_size": 4096,
    "schedule_delay_millis": 5000,
    "export_timeout_millis": 5000,
}
_BATCH_METRIC_NAMES = {
    "accepted",
    "completed",
    "exported",
    "failed",
    "dropped",
    "current_depth",
    "peak_depth",
    "shutdown_drain_timeout",
}
_BATCH_METRIC_EXPORTERS = {
    "local",
    "langfuse",
    "speedscope",
    "workspace_fanout",
    "otel_pending_spans",
    "custom",
}
_BATCH_METRIC_RESULTS = {
    "success",
    "error",
    "failure",
    "partial_failure",
    "queue_full",
    "closed",
    "timeout",
    "snapshot",
    "unknown",
}
_PERFORMANCE_CACHE_NAMES = {
    "model_profile_references",
    "ontology_view",
    "system_config",
    "workspace_catalog",
}
_PERFORMANCE_CACHE_RESULTS = {"bypass", "error", "hit", "miss", "stale"}
_PERFORMANCE_PHASE_NAMES = {
    "auth",
    "handler",
    "model_profile_references",
    "ontology_view",
    "request",
    "system_config",
    "workspace_catalog",
}
_MAX_BATCH_METRIC_WORKSPACES_PER_ROUTER = 4096


def initialize_open_telemetry(app: Any = None) -> None:
    """Install the SDK exporters and supported instrumentation once per process."""
    global _provider_initialized, _meter_provider_initialized
    global _client_instrumentation_initialized

    with _lock:
        try:
            from opentelemetry import metrics, trace
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        except ImportError:
            logger.warning("OpenTelemetry SDK is not installed; runtime event observability remains unavailable")
            return

        resource = Resource.create(
            {
                "service.name": os.getenv("OTEL_SERVICE_NAME", "consumer-backend"),
                "service.namespace": "onto",
            }
        )
        if not _provider_initialized:
            provider = trace.get_tracer_provider()
            if not callable(getattr(provider, "add_span_processor", None)):
                candidate = TracerProvider(resource=resource, sampler=ALWAYS_ON)
                trace.set_tracer_provider(candidate)
                provider = trace.get_tracer_provider()
            add_processor = getattr(provider, "add_span_processor", None)
            if callable(add_processor):
                add_processor(_WorkspaceContextSpanProcessor())
                add_processor(_create_batch_span_processor(_WorkspaceSpanExporter()))
                _provider_initialized = True
            else:
                logger.warning("OpenTelemetry tracer provider does not accept span processors")

        if not _meter_provider_initialized:
            metric_reader = PeriodicExportingMetricReader(
                _WorkspaceMetricExporter(),
                export_interval_millis=_metric_export_interval_ms(),
            )
            candidate = MeterProvider(resource=resource, metric_readers=[metric_reader])
            current = metrics.get_meter_provider()
            if not callable(getattr(current, "force_flush", None)):
                metrics.set_meter_provider(candidate)
                current = metrics.get_meter_provider()
            if current is candidate or callable(getattr(current, "force_flush", None)):
                _meter_provider_initialized = True

        if app is not None:
            _instrument_fastapi(app)
        if not _client_instrumentation_initialized:
            _instrument_clients()
            _client_instrumentation_initialized = True


def record_runtime_metrics(
    *,
    event_type: str,
    payload: Mapping[str, Any],
    workspace_id: str,
    metric_labels: Mapping[str, str],
    router_id: str = "",
) -> None:
    """Record the stable runtime metric set through the OTel metrics API."""
    instruments = _get_metric_instruments(router_id)
    if not instruments:
        return
    attributes = {"agent.workspace_id": str(workspace_id or "")}
    for key in ("model", "model_provider", "agent_mode"):
        value = str(payload.get(key) or metric_labels.get(key) or "").strip()
        if value:
            attributes["agent." + key] = value[:160]
    status = _metric_status(event_type, payload)
    if status:
        attributes["agent.status"] = status

    if event_type in {"run.finished", "run.failed", "run.cancelled"}:
        instruments["run.count"].add(1, attributes)
        _record_duration(
            instruments["run.duration"],
            payload,
            attributes,
            keys=("duration_ms",),
        )
        if event_type != "run.finished" or status in {"error", "failed", "cancelled"}:
            instruments["run.failure"].add(1, attributes)
        return

    if event_type in {"llm.finished", "llm.failed"}:
        instruments["llm.count"].add(1, attributes)
        _record_duration(
            instruments["llm.duration"],
            payload,
            attributes,
            keys=("wall_duration_ms", "duration_ms", "stream_duration_ms"),
        )
        if event_type == "llm.failed" or status in {"error", "failed"}:
            instruments["llm.failure"].add(1, attributes)
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        for token_type in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = _non_negative_int(usage.get(token_type))
            if value:
                instruments["llm.tokens"].add(
                    value,
                    {**attributes, "agent.token.type": token_type},
                )
        return

    if event_type == "tool.finished":
        tool_attributes = dict(attributes)
        tool_name = str(payload.get("tool_name") or "").strip()
        if tool_name:
            tool_attributes["agent.tool.name"] = tool_name
        instruments["tool.count"].add(1, tool_attributes)
        _record_duration(
            instruments["tool.duration"],
            payload,
            tool_attributes,
            keys=("elapsed_ms", "duration_ms"),
        )
        if status in {"error", "failed", "cancelled"}:
            instruments["tool.failure"].add(1, tool_attributes)


def record_observability_batch_metric(
    metric_name: str,
    value: int | float,
    *,
    exporter: str,
    result: str,
    router_id: str = "",
    workspace_id: str = "",
) -> None:
    """Record bounded-export health without business-identity labels."""
    resolved_name = str(metric_name or "").strip()
    if resolved_name not in _BATCH_METRIC_NAMES:
        return
    try:
        resolved_value = max(0.0, float(value))
    except (TypeError, ValueError):
        return
    instruments = _get_batch_metric_instruments(
        router_id=str(router_id or "").strip(),
        workspace_id=str(workspace_id or "").strip(),
    )
    instrument = instruments.get("observability." + resolved_name)
    if instrument is None:
        return
    attributes = {
        "exporter": _bounded_metric_value(
            exporter,
            allowed=_BATCH_METRIC_EXPORTERS,
            fallback="custom",
        ),
        "result": _bounded_metric_value(
            result,
            allowed=_BATCH_METRIC_RESULTS,
            fallback="unknown",
        ),
    }
    if resolved_name in {"current_depth", "peak_depth"}:
        instrument.set(resolved_value, attributes)
    else:
        instrument.add(resolved_value, attributes)


def record_cache_performance_metric(
    cache_name: str,
    result: str,
    *,
    router_id: str,
    workspace_id: str,
) -> bool:
    """Route a bounded cache outcome without identity metric labels."""
    resolved_cache = str(cache_name or "").strip()
    resolved_result = str(result or "").strip()
    if resolved_cache not in _PERFORMANCE_CACHE_NAMES or resolved_result not in _PERFORMANCE_CACHE_RESULTS:
        return False
    instruments = _get_batch_metric_instruments(
        router_id=str(router_id or "").strip(),
        workspace_id=str(workspace_id or "").strip(),
    )
    instrument = instruments.get("cache.event")
    if instrument is None:
        return False
    instrument.add(
        1,
        {"cache": resolved_cache, "result": resolved_result},
    )
    return True


def record_phase_performance_metric(
    phase_name: str,
    duration_ms: float,
    *,
    router_id: str,
    workspace_id: str,
) -> bool:
    """Route one allowlisted request phase through the shared recorder."""
    resolved_phase = str(phase_name or "").strip()
    if resolved_phase not in _PERFORMANCE_PHASE_NAMES:
        return False
    try:
        resolved_duration = max(0.0, float(duration_ms))
    except (TypeError, ValueError):
        return False
    instruments = _get_batch_metric_instruments(
        router_id=str(router_id or "").strip(),
        workspace_id=str(workspace_id or "").strip(),
    )
    instrument = instruments.get("request.phase.duration")
    if instrument is None:
        return False
    instrument.record(resolved_duration, {"phase": resolved_phase})
    return True


def _metric_status(event_type: str, payload: Mapping[str, Any]) -> str:
    raw = str(payload.get("status") or "").strip().lower()
    if raw in {"error", "failed", "failure"}:
        return "error"
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw in {"success", "completed", "complete", "ok"}:
        return "success"
    return {
        "run.finished": "success",
        "run.failed": "error",
        "run.cancelled": "cancelled",
        "llm.finished": "success",
        "llm.failed": "error",
        "tool.finished": "success",
    }.get(event_type, "")


def force_flush_open_telemetry(timeout_millis: int = 5000) -> bool:
    """Flush SDK data for shutdown and integration tests."""
    try:
        from opentelemetry import metrics, trace

        trace_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
        metric_flush = getattr(metrics.get_meter_provider(), "force_flush", None)
        trace_result = trace_flush(timeout_millis=timeout_millis) if callable(trace_flush) else True
        metric_result = metric_flush(timeout_millis=timeout_millis) if callable(metric_flush) else True
        return bool(trace_result is not False and metric_result is not False)
    except Exception:
        logger.warning("OpenTelemetry force flush failed", exc_info=True)
        return False


def force_flush_runtime_metrics(router_id: str, timeout_millis: int = 5000) -> bool:
    """Flush metrics owned by one observer without switching export routes."""
    resolved_router_id = str(router_id or "")
    with _lock:
        recorder = _router_metric_recorders.get(resolved_router_id)
        batch_recorder = _batch_metric_recorders.get(resolved_router_id)
        recorders = ([recorder] if recorder is not None else []) + (
            [batch_recorder] if batch_recorder is not None else []
        )
    try:
        return _operate_metric_recorders(
            recorders,
            operation="force_flush",
            timeout_millis=timeout_millis,
        )
    except Exception:
        logger.warning("OpenTelemetry runtime metric flush failed: router_id=%s", router_id, exc_info=True)
        return False


def shutdown_runtime_metrics(router_id: str, timeout_millis: int = 5000) -> bool:
    """Flush and stop the metric pipeline owned by a retired observer."""
    resolved_router_id = str(router_id or "")
    with _lock:
        recorder = _router_metric_recorders.get(resolved_router_id)
        batch_recorder = _batch_metric_recorders.get(resolved_router_id)
    pending = (
        (_router_metric_recorders, recorder),
        (_batch_metric_recorders, batch_recorder),
    )
    deadline = time.monotonic() + max(0, int(timeout_millis)) / 1000
    succeeded = True
    for recorder_map, current_recorder in pending:
        if current_recorder is None:
            continue
        remaining_millis = max(0, int((deadline - time.monotonic()) * 1000))
        recorder_succeeded = False
        if remaining_millis > 0:
            try:
                recorder_succeeded = current_recorder.shutdown(timeout_millis=remaining_millis) is not False
            except Exception:
                logger.warning(
                    "OpenTelemetry runtime metric shutdown failed: router_id=%s",
                    router_id,
                    exc_info=True,
                )
        if recorder_succeeded:
            with _lock:
                if recorder_map.get(resolved_router_id) is current_recorder:
                    recorder_map.pop(resolved_router_id, None)
        else:
            succeeded = False
    return succeeded


def _get_metric_instruments(router_id: str = "") -> dict[str, Any]:
    global _metric_instruments
    with _lock:
        resolved_router_id = str(router_id or "").strip()
        if resolved_router_id:
            from integration.observability.workspace_router import resolve_workspace_router

            router = resolve_workspace_router(resolved_router_id)
            if router is None:
                return {}
            recorder = _router_metric_recorders.get(resolved_router_id)
            if recorder is None:
                if not router.accepts_new_metric_recorders:
                    return {}
                try:
                    recorder = _RouterMetricRecorder(resolved_router_id)
                except Exception:
                    logger.warning(
                        "Failed to initialize OpenTelemetry runtime metrics: router_id=%s",
                        resolved_router_id,
                        exc_info=True,
                    )
                    return {}
                _router_metric_recorders[resolved_router_id] = recorder
            return recorder.instruments
        if _metric_instruments is not None:
            return _metric_instruments
        try:
            from opentelemetry import metrics

            _metric_instruments = _create_metric_instruments(metrics.get_meter("onto.consumer.runtime"))
        except Exception:
            _metric_instruments = {}
        return _metric_instruments


def _get_batch_metric_instruments(
    *,
    router_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    if not router_id and not workspace_id:
        return _get_metric_instruments()
    if not router_id or not workspace_id:
        return {}
    with _lock:
        from integration.observability.workspace_router import resolve_workspace_router

        router = resolve_workspace_router(router_id)
        if router is None:
            return {}
        recorder = _batch_metric_recorders.get(router_id)
        if recorder is None:
            if not router.accepts_new_metric_recorders:
                return {}
            try:
                recorder = _RouterBatchMetricRecorder(router_id=router_id)
            except Exception:
                logger.warning(
                    "Failed to initialize OpenTelemetry batch metrics: router_id=%s",
                    router_id,
                    exc_info=True,
                )
                return {}
            _batch_metric_recorders[router_id] = recorder
        return recorder.instruments_for_workspace(workspace_id)


def _operate_metric_recorders(
    recorders: list[Any],
    *,
    operation: str,
    timeout_millis: int,
) -> bool:
    deadline = time.monotonic() + max(0, int(timeout_millis)) / 1000
    succeeded = True
    for recorder in recorders:
        remaining_millis = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_millis <= 0:
            return False
        result = getattr(recorder, operation)(timeout_millis=remaining_millis)
        succeeded = bool(result is not False and succeeded)
    return succeeded


def _create_metric_instruments(meter: Any) -> dict[str, Any]:
    return {
        "cache.event": meter.create_counter("agent.cache.event.count"),
        "request.phase.duration": meter.create_histogram(
            "agent.request.phase.duration",
            unit="ms",
        ),
        "run.count": meter.create_counter("agent.run.count"),
        "run.failure": meter.create_counter("agent.run.failure.count"),
        "run.duration": meter.create_histogram("agent.run.duration", unit="ms"),
        "llm.count": meter.create_counter("agent.llm.call.count"),
        "llm.failure": meter.create_counter("agent.llm.failure.count"),
        "llm.duration": meter.create_histogram("agent.llm.duration", unit="ms"),
        "llm.tokens": meter.create_counter("agent.llm.token.count"),
        "tool.count": meter.create_counter("agent.tool.call.count"),
        "tool.failure": meter.create_counter("agent.tool.failure.count"),
        "tool.duration": meter.create_histogram("agent.tool.duration", unit="ms"),
        "observability.accepted": meter.create_counter("agent.observability.export.accepted"),
        "observability.completed": meter.create_counter("agent.observability.export.completed"),
        "observability.exported": meter.create_counter("agent.observability.export.succeeded"),
        "observability.failed": meter.create_counter("agent.observability.export.failed"),
        "observability.dropped": meter.create_counter("agent.observability.export.dropped"),
        "observability.current_depth": meter.create_gauge("agent.observability.queue.current_depth"),
        "observability.peak_depth": meter.create_gauge("agent.observability.queue.peak_depth"),
        "observability.shutdown_drain_timeout": meter.create_counter("agent.observability.shutdown.drain.timeout"),
    }


class _RouterMetricRecorder:
    def __init__(self, router_id: str) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        self._reader = PeriodicExportingMetricReader(
            _WorkspaceMetricExporter(router_id=router_id),
            export_interval_millis=_metric_export_interval_ms(),
        )
        self._provider = MeterProvider(
            resource=Resource.create(
                {
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "consumer-backend"),
                    "service.namespace": "onto",
                }
            ),
            metric_readers=[self._reader],
            shutdown_on_exit=False,
        )
        self.instruments = _create_metric_instruments(self._provider.get_meter("onto.consumer.runtime"))

    def force_flush(self, *, timeout_millis: int) -> bool:
        return bool(self._provider.force_flush(timeout_millis=timeout_millis) is not False)

    def shutdown(self, *, timeout_millis: int) -> bool:
        result = self._provider.shutdown(timeout_millis=timeout_millis)
        return bool(result is not False and _metric_reader_stopped(getattr(self, "_reader", None)))


class _RouterBatchMetricRecorder:
    def __init__(self, *, router_id: str) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        self._lock = threading.RLock()
        self._next_scope_number = 1
        self._workspace_by_scope: dict[str, str] = {}
        self._scope_by_workspace: dict[str, str] = {}
        self._instruments_by_scope: dict[str, dict[str, Any]] = {}
        self._reader = PeriodicExportingMetricReader(
            _WorkspaceMetricExporter(
                router_id=router_id,
                workspace_resolver=self.resolve_workspace,
            ),
            export_interval_millis=_metric_export_interval_ms(),
        )
        self._provider = MeterProvider(
            resource=Resource.create(
                {
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "consumer-backend"),
                    "service.namespace": "onto",
                }
            ),
            metric_readers=[self._reader],
            shutdown_on_exit=False,
        )

    def instruments_for_workspace(self, workspace_id: str) -> dict[str, Any]:
        with self._lock:
            scope_name = self._scope_by_workspace.get(workspace_id)
            if scope_name is None:
                if len(self._workspace_by_scope) >= _MAX_BATCH_METRIC_WORKSPACES_PER_ROUTER:
                    logger.warning("OpenTelemetry batch metric workspace limit reached")
                    return {}
                scope_name = "onto.consumer.observability.batch.scope-{:08x}".format(self._next_scope_number)
                self._next_scope_number += 1
                self._scope_by_workspace[workspace_id] = scope_name
                self._workspace_by_scope[scope_name] = workspace_id
            instruments = self._instruments_by_scope.get(scope_name)
            if instruments is None:
                instruments = _create_metric_instruments(self._provider.get_meter(scope_name))
                self._instruments_by_scope[scope_name] = instruments
            return instruments

    def resolve_workspace(self, scope_name: str) -> str:
        with self._lock:
            return self._workspace_by_scope.get(str(scope_name or ""), "")

    def force_flush(self, *, timeout_millis: int) -> bool:
        return bool(self._provider.force_flush(timeout_millis=timeout_millis) is not False)

    def shutdown(self, *, timeout_millis: int) -> bool:
        result = self._provider.shutdown(timeout_millis=timeout_millis)
        return bool(result is not False and _metric_reader_stopped(getattr(self, "_reader", None)))


def _metric_reader_stopped(reader: Any) -> bool:
    thread = getattr(reader, "_daemon_thread", None)
    return thread is None or not thread.is_alive()


def _record_duration(
    instrument: Any,
    payload: Mapping[str, Any],
    attributes: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
) -> None:
    duration = None
    for key in keys:
        parsed = _non_negative_int(payload.get(key))
        if parsed is not None:
            duration = parsed
            break
    if duration is not None:
        instrument.record(duration, attributes)


def _non_negative_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _batch_span_processor_options(
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, int]:
    source = os.environ if env is None else env
    options = {
        "max_queue_size": _positive_operational_int(
            source,
            "OTEL_BSP_MAX_QUEUE_SIZE",
            _BATCH_SPAN_PROCESSOR_DEFAULTS["max_queue_size"],
            maximum=_BATCH_SPAN_PROCESSOR_MAXIMUMS["max_queue_size"],
        ),
        "max_export_batch_size": _positive_operational_int(
            source,
            "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
            _BATCH_SPAN_PROCESSOR_DEFAULTS["max_export_batch_size"],
            maximum=_BATCH_SPAN_PROCESSOR_MAXIMUMS["max_export_batch_size"],
        ),
        "schedule_delay_millis": _positive_operational_int(
            source,
            "OTEL_BSP_SCHEDULE_DELAY",
            _BATCH_SPAN_PROCESSOR_DEFAULTS["schedule_delay_millis"],
            maximum=_BATCH_SPAN_PROCESSOR_MAXIMUMS["schedule_delay_millis"],
        ),
        "export_timeout_millis": _positive_operational_int(
            source,
            "OTEL_BSP_EXPORT_TIMEOUT",
            _BATCH_SPAN_PROCESSOR_DEFAULTS["export_timeout_millis"],
            maximum=_BATCH_SPAN_PROCESSOR_MAXIMUMS["export_timeout_millis"],
        ),
    }
    if options["max_export_batch_size"] > options["max_queue_size"]:
        logger.warning(
            "OTEL_BSP_MAX_EXPORT_BATCH_SIZE exceeds OTEL_BSP_MAX_QUEUE_SIZE; "
            "clamping the export batch size to the queue size"
        )
        options["max_export_batch_size"] = options["max_queue_size"]
    return options


def _create_batch_span_processor(
    span_exporter: Any,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    options = _batch_span_processor_options(env)
    return _BoundedBatchSpanProcessor(
        span_exporter,
        shutdown_timeout_millis=options["export_timeout_millis"],
        **options,
    )


def _positive_operational_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid positive integer override for %s", name)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive override for %s", name)
        return default
    if value > maximum:
        logger.warning("Clamping override for %s to %s", name, maximum)
        return maximum
    return value


def _bounded_metric_value(
    value: Any,
    *,
    allowed: set[str],
    fallback: str,
) -> str:
    resolved = str(value or "").strip().lower()
    return resolved if resolved in allowed else fallback


try:
    from opentelemetry.sdk.trace.export import BatchSpanProcessor as _SdkBatchSpanProcessor

    class _BoundedBatchSpanProcessor(_SdkBatchSpanProcessor):
        def __init__(
            self,
            span_exporter: Any,
            *,
            shutdown_timeout_millis: int,
            **options: Any,
        ) -> None:
            self.shutdown_timeout_millis = max(1, int(shutdown_timeout_millis))
            super().__init__(span_exporter, **options)

        def shutdown(self) -> Any:
            return self._batch_processor.shutdown(timeout_millis=self.shutdown_timeout_millis)

except ImportError:  # pragma: no cover - guarded by initialize_open_telemetry

    class _BoundedBatchSpanProcessor:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("OpenTelemetry SDK is not installed")


class _WorkspaceContextSpanProcessor(_SpanProcessorBase):
    def on_start(self, span: Any, parent_context: Any = None) -> None:
        try:
            from opentelemetry import trace

            parent_span = trace.get_current_span(parent_context)
            parent_attributes = dict(getattr(parent_span, "attributes", {}) or {})
            for name in (
                "agent.observability.router_id",
                "agent.workspace_id",
                "agent.run_id",
                "agent.session_id",
            ):
                value = parent_attributes.get(name)
                if value not in (None, ""):
                    span.set_attribute(name, value)
        except Exception:
            logger.warning("Failed to propagate OpenTelemetry span attributes", exc_info=True)

    def on_end(self, span: Any) -> None:
        del span

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True


def _instrument_fastapi(app: Any) -> None:
    app_id = id(app)
    if app_id in _instrumented_apps:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            server_request_hook=_asgi_server_request_hook,
        )
        _instrumented_apps.add(app_id)
    except ImportError:
        logger.info("FastAPI OpenTelemetry instrumentation is not installed")
    except Exception:
        logger.warning("Failed to instrument FastAPI", exc_info=True)


def _asgi_server_request_hook(
    span: Any,
    scope: Mapping[str, Any],
) -> None:
    if span is None:
        return
    try:
        from integration.observability.provider import get_observer
        from integration.observability.workspace_router import get_default_workspace_router

        get_observer()
        router = get_default_workspace_router()
        if router is not None:
            span.set_attribute("agent.observability.router_id", router.router_id)
    except Exception:
        logger.warning(
            "Failed to bind ASGI request to the workspace observer",
            exc_info=True,
        )
    try:
        from starlette.datastructures import Headers

        workspace_id = str(Headers(scope=scope).get("x-workspace-id") or "").strip()
    except Exception:
        logger.warning("Failed to read ASGI request headers", exc_info=True)
        workspace_id = ""
    if workspace_id:
        span.set_attribute("agent.workspace_id", workspace_id)


def _instrument_clients() -> None:
    instrumentors = (
        ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
        ("aiohttp", "opentelemetry.instrumentation.aiohttp_client", "AioHttpClientInstrumentor"),
    )
    for name, module_name, class_name in instrumentors:
        try:
            module = importlib.import_module(module_name)
            getattr(module, class_name)().instrument()
        except ImportError:
            logger.info("%s OpenTelemetry instrumentation is not installed", name)
        except Exception:
            logger.warning("Failed to instrument %s", name, exc_info=True)


def _metric_export_interval_ms() -> int:
    raw = str(os.getenv("OTEL_METRIC_EXPORT_INTERVAL_MS") or "5000").strip()
    try:
        return max(1000, int(raw))
    except ValueError:
        return 5000


try:
    from opentelemetry.sdk.trace.export import SpanExportResult, SpanExporter

    class _WorkspaceSpanExporter(SpanExporter):
        def export(self, spans: Any) -> Any:
            for readable_span in spans:
                try:
                    snapshot = snapshot_readable_span(readable_span)
                    from integration.observability.workspace_router import resolve_workspace_router

                    router = resolve_workspace_router(snapshot.router_id)
                    if router is not None:
                        router.export_otel_span(snapshot)
                except Exception:
                    logger.warning("Failed to export OpenTelemetry span", exc_info=True)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

except ImportError:  # pragma: no cover - guarded by initialize_open_telemetry

    class _WorkspaceSpanExporter:  # type: ignore[no-redef]
        pass


try:
    from opentelemetry.sdk.metrics.export import MetricExportResult, MetricExporter

    class _WorkspaceMetricExporter(MetricExporter):
        def __init__(
            self,
            router_id: str = "",
            workspace_id: str = "",
            workspace_resolver: Optional[Callable[[str], str]] = None,
        ) -> None:
            from opentelemetry.sdk.metrics import Counter, Histogram
            from opentelemetry.sdk.metrics.export import AggregationTemporality

            super().__init__(
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                }
            )
            self._router_id = str(router_id or "")
            self._workspace_id = str(workspace_id or "")
            self._workspace_resolver = workspace_resolver

        def export(self, metrics_data: Any, timeout_millis: float = 10000, **kwargs: Any) -> Any:
            del timeout_millis, kwargs
            for resource_metrics in metrics_data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    scope_name = str(getattr(scope_metrics.scope, "name", "") or "")
                    workspace_id = self._workspace_id
                    if not workspace_id and self._workspace_resolver is not None:
                        workspace_id = str(self._workspace_resolver(scope_name) or "")
                    for metric in scope_metrics.metrics:
                        for point in getattr(metric.data, "data_points", ()):
                            try:
                                attributes = dict(getattr(point, "attributes", {}) or {})
                                from integration.observability.workspace_router import resolve_workspace_router

                                router = resolve_workspace_router(self._router_id)
                                if router is not None:
                                    router.export_otel_metric(
                                        metric_record(name=metric.name, point=point, scope_name=scope_name),
                                        attributes=attributes,
                                        workspace_id=workspace_id,
                                    )
                            except Exception:
                                logger.warning("Failed to export OpenTelemetry metric", exc_info=True)
            return MetricExportResult.SUCCESS

        def shutdown(self, timeout_millis: float = 30000, **kwargs: Any) -> None:
            del timeout_millis, kwargs

        def force_flush(self, timeout_millis: float = 10000) -> bool:
            del timeout_millis
            return True

except ImportError:  # pragma: no cover - guarded by initialize_open_telemetry

    class _WorkspaceMetricExporter:  # type: ignore[no-redef]
        pass
