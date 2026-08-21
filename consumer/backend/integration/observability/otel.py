"""Process-level OpenTelemetry bootstrap for the consumer backend."""

from __future__ import annotations

import importlib
import os
import threading
from typing import Any, Mapping, Optional

from integration.observability.otel_data import metric_record, snapshot_readable_span
from utils.logging import get_logger

try:
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessorBase
except ImportError:  # pragma: no cover - guarded by initialize_open_telemetry
    class _SpanProcessorBase:
        pass


logger = get_logger("observability.otel")

_lock = threading.RLock()
_provider_initialized = False
_meter_provider_initialized = False
_instrumented_apps: set[int] = set()
_client_instrumentation_initialized = False
_metric_instruments: Optional[dict[str, Any]] = None
_router_metric_recorders: dict[str, "_RouterMetricRecorder"] = {}


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
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor
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
                add_processor(SimpleSpanProcessor(_WorkspaceSpanExporter()))
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
            _instrument_flask(app)
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

        trace_result = trace.get_tracer_provider().force_flush(timeout_millis=timeout_millis)
        metric_result = metrics.get_meter_provider().force_flush(timeout_millis=timeout_millis)
        return bool(trace_result is not False and metric_result is not False)
    except Exception:
        logger.warning("OpenTelemetry force flush failed", exc_info=True)
        return False


def force_flush_runtime_metrics(router_id: str, timeout_millis: int = 5000) -> bool:
    """Flush metrics owned by one observer without switching export routes."""
    with _lock:
        recorder = _router_metric_recorders.get(str(router_id or ""))
    if recorder is None:
        return True
    try:
        return recorder.force_flush(timeout_millis=timeout_millis)
    except Exception:
        logger.warning("OpenTelemetry runtime metric flush failed: router_id=%s", router_id, exc_info=True)
        return False


def shutdown_runtime_metrics(router_id: str, timeout_millis: int = 5000) -> bool:
    """Flush and stop the metric pipeline owned by a retired observer."""
    with _lock:
        recorder = _router_metric_recorders.pop(str(router_id or ""), None)
    if recorder is None:
        return True
    try:
        recorder.shutdown(timeout_millis=timeout_millis)
        return True
    except Exception:
        logger.warning("OpenTelemetry runtime metric shutdown failed: router_id=%s", router_id, exc_info=True)
        return False


def _get_metric_instruments(router_id: str = "") -> dict[str, Any]:
    global _metric_instruments
    with _lock:
        resolved_router_id = str(router_id or "").strip()
        if resolved_router_id:
            from integration.observability.workspace_router import resolve_workspace_router

            if resolve_workspace_router(resolved_router_id) is None:
                return {}
            recorder = _router_metric_recorders.get(resolved_router_id)
            if recorder is None:
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

            _metric_instruments = _create_metric_instruments(
                metrics.get_meter("onto.consumer.runtime")
            )
        except Exception:
            _metric_instruments = {}
        return _metric_instruments


def _create_metric_instruments(meter: Any) -> dict[str, Any]:
    return {
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
    }


class _RouterMetricRecorder:
    def __init__(self, router_id: str) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        reader = PeriodicExportingMetricReader(
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
            metric_readers=[reader],
            shutdown_on_exit=False,
        )
        self.instruments = _create_metric_instruments(
            self._provider.get_meter("onto.consumer.runtime")
        )

    def force_flush(self, *, timeout_millis: int) -> bool:
        return bool(self._provider.force_flush(timeout_millis=timeout_millis) is not False)

    def shutdown(self, *, timeout_millis: int) -> None:
        self._provider.shutdown(timeout_millis=timeout_millis)


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


def _instrument_flask(app: Any) -> None:
    app_id = id(app)
    if app_id in _instrumented_apps:
        return
    try:
        from opentelemetry.instrumentation.flask import FlaskInstrumentor

        FlaskInstrumentor().instrument_app(app, request_hook=_flask_request_hook)
        _instrumented_apps.add(app_id)
    except ImportError:
        logger.info("Flask OpenTelemetry instrumentation is not installed")
    except Exception:
        logger.warning("Failed to instrument Flask", exc_info=True)


def _flask_request_hook(span: Any, environ: Mapping[str, Any]) -> None:
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
        logger.warning("Failed to bind Flask request to the workspace observer", exc_info=True)
    workspace_id = str(environ.get("HTTP_X_WORKSPACE_ID") or "").strip()
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
        def __init__(self, router_id: str = "") -> None:
            from opentelemetry.sdk.metrics import Counter, Histogram
            from opentelemetry.sdk.metrics.export import AggregationTemporality

            super().__init__(
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                }
            )
            self._router_id = str(router_id or "")

        def export(self, metrics_data: Any, timeout_millis: float = 10000, **kwargs: Any) -> Any:
            del timeout_millis, kwargs
            for resource_metrics in metrics_data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    scope_name = str(getattr(scope_metrics.scope, "name", "") or "")
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
