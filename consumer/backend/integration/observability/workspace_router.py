"""Workspace-scoped observability event routing."""

from __future__ import annotations

import threading
import uuid
from typing import Any, Iterable, Sequence

from utils.logging import get_logger
from workspace.workspaces import get_current_workspace_id, validate_workspace_id
from integration.observability.bus import ObservabilityBus
from integration.observability.events import RuntimeEvent, RuntimeEventType
from integration.observability.identity import clear_runtime_trace_identity, record_runtime_event
from integration.observability.otel import (
    force_flush_open_telemetry,
    force_flush_runtime_metrics,
    shutdown_runtime_metrics,
)
from integration.observability.otel_data import (
    OTelSpanSnapshot,
    runtime_events_from_spans,
    span_record,
)
from integration.observability.exporters.langfuse import create_langfuse_exporter
from integration.observability.exporters.local import LocalPerfExporter
from integration.observability.exporters.speedscope import create_speedscope_exporter
from integration.observability.workspace_config import (
    get_workspace_observability_config,
    get_workspace_observability_log_dir,
)

logger = get_logger("observability.workspace_router")

_OTEL_SPAN_EXPORTED = "otel.span.exported"
_OTEL_METRIC_EXPORTED = "otel.metric.exported"
_ROUTER_REGISTRY_LOCK = threading.RLock()
_ROUTERS: dict[str, "WorkspaceObservabilityRouter"] = {}
_DEFAULT_ROUTER_ID = ""
_RUN_CONSUMER_LOCK = threading.RLock()
_RUN_RUNTIME_EVENT_CONSUMERS: dict[str, list[Any]] = {}
_DEFAULT_MAX_PENDING_SPANS_PER_RUN = 1000
_DEFAULT_MAX_PENDING_SPANS_TOTAL = 10000


class _WorkspaceExporterFanout:
    """Keep one queue per workspace while isolating exporter failures."""

    def __init__(self, exporters: Iterable[Any]) -> None:
        self._exporters = [exporter for exporter in exporters if exporter is not None]

    @property
    def available(self) -> bool:
        return bool(self._exporters)

    def export(self, event: RuntimeEvent) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(event)
            except Exception:
                logger.warning("Workspace observability exporter failed", exc_info=True)

    def record_drop(self, event: RuntimeEvent) -> None:
        for exporter in self._exporters:
            record_drop = getattr(exporter, "record_drop", None)
            if not callable(record_drop):
                continue
            try:
                record_drop(event)
            except Exception:
                logger.warning("Workspace observability exporter drop recording failed", exc_info=True)

    def flush(self) -> None:
        for exporter in self._exporters:
            flush = getattr(exporter, "flush", None)
            if not callable(flush):
                continue
            try:
                flush()
            except Exception:
                logger.warning("Workspace observability exporter flush failed", exc_info=True)

    def close(self) -> None:
        for exporter in self._exporters:
            close = getattr(exporter, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                logger.warning("Workspace observability exporter close failed", exc_info=True)


class WorkspaceObservabilityRouter:
    """Route runtime events to the configured exporter for the event workspace."""

    def __init__(
        self,
        *,
        max_pending_spans_per_run: int = _DEFAULT_MAX_PENDING_SPANS_PER_RUN,
        max_pending_spans_total: int = _DEFAULT_MAX_PENDING_SPANS_TOTAL,
    ) -> None:
        self.router_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._observers_by_workspace: dict[str, ObservabilityBus | None] = {}
        self._run_workspaces: dict[str, str] = {}
        self._pending_spans_by_run: dict[str, list[OTelSpanSnapshot]] = {}
        self._pending_span_count = 0
        self._pending_span_drop_count = 0
        self._max_pending_spans_per_run = max(1, int(max_pending_spans_per_run))
        self._max_pending_spans_total = max(1, int(max_pending_spans_total))
        self._retiring = False
        self._closed = False
        _register_router(self)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return True

    def emit(self, event: RuntimeEvent) -> bool:
        if self._is_closed():
            return False

        workspace_id = self._resolve_workspace_for_event(event)
        if not workspace_id:
            return False

        try:
            trace_id, span_id, parent_span_id = record_runtime_event(
                event,
                router_id=self.router_id,
                workspace_id=workspace_id,
            )
            object.__setattr__(event, "trace_id", trace_id)
            object.__setattr__(event, "span_id", span_id)
            object.__setattr__(event, "parent_span_id", parent_span_id)
            return True
        except Exception:
            logger.warning(
                "Workspace observability observer failed: workspace_id=%s run_id=%s event=%s",
                workspace_id,
                event.run_id,
                event.type,
                exc_info=True,
            )
            return False

    def reserve_run(self, run_id: str, workspace_id: str) -> bool:
        resolved_run_id = str(run_id or "").strip()
        resolved_workspace_id = _validate_workspace_id(str(workspace_id or "").strip())
        if not resolved_run_id or not resolved_workspace_id:
            return False
        with self._lock:
            if self._closed or self._retiring:
                return False
            existing = self._run_workspaces.get(resolved_run_id)
            if existing and existing != resolved_workspace_id:
                return False
            self._run_workspaces[resolved_run_id] = resolved_workspace_id
            observer = self._get_observer(resolved_workspace_id)
            if observer is None:
                self._run_workspaces.pop(resolved_run_id, None)
                return False
            return True

    def release_run(self, run_id: str) -> None:
        resolved_run_id = str(run_id or "")
        clear_runtime_trace_identity(resolved_run_id)
        with self._lock:
            retiring = self._retiring
        if retiring and not force_flush_open_telemetry():
            logger.warning(
                "OpenTelemetry flush failed before retiring workspace router: router_id=%s run_id=%s",
                self.router_id,
                resolved_run_id,
            )
        if retiring and not force_flush_runtime_metrics(self.router_id):
            logger.warning(
                "OpenTelemetry metric flush failed before retiring workspace router: router_id=%s run_id=%s",
                self.router_id,
                resolved_run_id,
            )
        self._flush_run(resolved_run_id)
        should_close = False
        with self._lock:
            self._run_workspaces.pop(resolved_run_id, None)
            should_close = self._retiring and not self._run_workspaces
        if should_close:
            self.close()

    def export(self, event: RuntimeEvent) -> None:
        self.emit(event)

    def retire(self, timeout: float = 1.0) -> None:
        del timeout
        with self._lock:
            self._retiring = True
            should_close = not self._run_workspaces
        if not force_flush_open_telemetry():
            logger.warning(
                "OpenTelemetry flush failed while retiring workspace router: router_id=%s",
                self.router_id,
            )
        if not force_flush_runtime_metrics(self.router_id):
            logger.warning(
                "OpenTelemetry metric flush failed while retiring workspace router: router_id=%s",
                self.router_id,
            )
        if should_close:
            self.close()

    def close(self, timeout: float = 1.0) -> None:
        with self._lock:
            if self._closed:
                return
            active_run_ids = list(self._run_workspaces)
            retiring = self._retiring

        for run_id in active_run_ids:
            clear_runtime_trace_identity(run_id)
        if not retiring and not force_flush_open_telemetry():
            logger.warning(
                "OpenTelemetry flush failed before closing workspace router: router_id=%s",
                self.router_id,
            )
        if not retiring and not force_flush_runtime_metrics(self.router_id):
            logger.warning(
                "OpenTelemetry metric flush failed before closing workspace router: router_id=%s",
                self.router_id,
            )
        shutdown_runtime_metrics(self.router_id)

        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending_run_ids = list(self._pending_spans_by_run)

        for run_id in pending_run_ids:
            self._flush_run(run_id)

        with self._lock:
            observers = [
                observer
                for observer in self._observers_by_workspace.values()
                if observer is not None
            ]
            self._observers_by_workspace.clear()
            self._run_workspaces.clear()

        for observer in observers:
            try:
                observer.close(timeout=timeout)
            except Exception:
                logger.warning("Workspace observability observer close failed", exc_info=True)
        _unregister_router(self)

    def export_otel_span(self, span: OTelSpanSnapshot) -> None:
        if self._is_closed():
            return
        if span.run_id:
            self._buffer_pending_span(span)
            return
        workspace_id = span.workspace_id
        if not workspace_id:
            return
        observer = self._get_observer(workspace_id)
        if observer is not None:
            observer.emit(_span_export_event(span, workspace_id=workspace_id))

    def export_otel_metric(
        self,
        record: dict[str, Any],
        *,
        attributes: dict[str, Any],
    ) -> None:
        workspace_id = str(attributes.get("agent.workspace_id") or record.get("workspace_id") or "").strip()
        if not workspace_id:
            return
        observer = self._get_observer(workspace_id)
        if observer is None:
            return
        event = RuntimeEvent.from_otel(
            type=_OTEL_METRIC_EXPORTED,
            run_id=str(record.get("run_id") or ""),
            session_id=str(record.get("session_id") or "") or None,
            trace_id=None,
            span_id=None,
            parent_span_id=None,
            payload={"workspace_id": workspace_id, "otel_record": record},
            timestamp_ms=int(record.get("ts_ms") or 0),
        )
        emit_metric = getattr(observer, "emit_metric", None)
        if callable(emit_metric):
            emit_metric(event)
        else:
            observer.emit(event)

    def _flush_run(self, run_id: str) -> None:
        if not run_id:
            return
        with self._lock:
            spans = self._pending_spans_by_run.pop(run_id, [])
            workspace_id = self._run_workspaces.get(run_id, "")
        consumers = _take_runtime_event_consumers(run_id)
        if not spans:
            return
        spans = _order_spans_parent_first(spans)
        events = runtime_events_from_spans(spans) if consumers else []
        for consumer in consumers:
            for event in events:
                try:
                    consumer(event)
                except Exception:
                    logger.warning("Runtime event consumer failed", exc_info=True)
                    break
        if not workspace_id:
            workspace_id = next((span.workspace_id for span in spans if span.workspace_id), "")
        if not workspace_id:
            self._release_pending_span_capacity(len(spans))
            return
        observer = self._get_observer(workspace_id, allow_closed=True)
        if observer is None:
            self._release_pending_span_capacity(len(spans))
            return
        try:
            accepted = observer.emit_batch(
                lambda: _run_export_events(spans, workspace_id=workspace_id),
                on_complete=lambda: self._release_pending_span_capacity(len(spans)),
            )
        except Exception:
            self._release_pending_span_capacity(len(spans))
            raise
        if not accepted:
            self._release_pending_span_capacity(len(spans))

    def _release_pending_span_capacity(self, count: int) -> None:
        with self._lock:
            self._pending_span_count = max(0, self._pending_span_count - max(0, int(count)))

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _resolve_workspace_for_event(self, event: RuntimeEvent) -> str | None:
        payload = dict(event.payload or {})
        explicit_workspace_id = str(payload.get("workspace_id") or "").strip()
        workspace_id: str | None = None

        with self._lock:
            workspace_id = self._run_workspaces.get(event.run_id)
            if event.type == RuntimeEventType.RUN_STARTED and self._retiring and not workspace_id:
                return None

        if not workspace_id:
            if explicit_workspace_id:
                workspace_id = _validate_workspace_id(explicit_workspace_id)
            elif event.type == RuntimeEventType.RUN_STARTED:
                workspace_id = _current_workspace_id()

        if event.type == RuntimeEventType.RUN_STARTED and workspace_id:
            with self._lock:
                self._run_workspaces[event.run_id] = workspace_id

        return workspace_id

    def _get_observer(
        self,
        workspace_id: str,
        *,
        allow_closed: bool = False,
    ) -> ObservabilityBus | None:
        with self._lock:
            if self._closed and not allow_closed:
                return None
            if workspace_id in self._observers_by_workspace:
                return self._observers_by_workspace[workspace_id]
            exporter = self._build_exporter(workspace_id)
            observer = (
                ObservabilityBus(
                    exporter,
                    batch_queue_size=self._max_pending_spans_total,
                )
                if exporter is not None
                else None
            )
            self._observers_by_workspace[workspace_id] = observer
            return observer

    def _buffer_pending_span(self, span: OTelSpanSnapshot) -> None:
        dropped = False
        with self._lock:
            spans = self._pending_spans_by_run.get(span.run_id)
            current_count = len(spans) if spans is not None else 0
            per_run_limit = self._max_pending_spans_per_run
            if span.name != "agent.run":
                per_run_limit = max(0, per_run_limit - 1)
            if (
                span.name == "agent.run"
                and self._pending_span_count >= self._max_pending_spans_total
                and self._discard_pending_detail_span(preferred_run_id=span.run_id)
            ):
                self._pending_span_drop_count += 1
                spans = self._pending_spans_by_run.get(span.run_id)
                current_count = len(spans) if spans is not None else 0
            if current_count >= per_run_limit or self._pending_span_count >= self._max_pending_spans_total:
                self._pending_span_drop_count += 1
                drop_count = self._pending_span_drop_count
                dropped = True
            else:
                if spans is None:
                    spans = []
                    self._pending_spans_by_run[span.run_id] = spans
                spans.append(span)
                self._pending_span_count += 1
                drop_count = self._pending_span_drop_count
        if dropped and (drop_count == 1 or drop_count & (drop_count - 1) == 0):
            logger.warning(
                "Dropping buffered OpenTelemetry span because the pending span limit was reached: "
                "router_id=%s run_id=%s dropped=%s",
                self.router_id,
                span.run_id,
                drop_count,
            )

    def _discard_pending_detail_span(self, *, preferred_run_id: str) -> bool:
        ordered_run_ids = [preferred_run_id] + [
            run_id
            for run_id in self._pending_spans_by_run
            if run_id != preferred_run_id
        ]
        for run_id in ordered_run_ids:
            spans = self._pending_spans_by_run.get(run_id)
            if not spans:
                continue
            for index in range(len(spans) - 1, -1, -1):
                if spans[index].name == "agent.run":
                    continue
                spans.pop(index)
                self._pending_span_count = max(0, self._pending_span_count - 1)
                if not spans:
                    self._pending_spans_by_run.pop(run_id, None)
                return True
        return False

    def _build_exporter(self, workspace_id: str) -> object | None:
        try:
            config = get_workspace_observability_config(workspace_id)
        except Exception:
            logger.warning("Failed to load workspace observability config: %s", workspace_id, exc_info=True)
            return None

        if not bool(config.get("enabled")):
            return None

        mode = str(config.get("mode") or "local").strip().lower()
        if mode not in {"local", "langfuse"}:
            logger.warning("Unknown workspace observability mode=%s workspace_id=%s", mode, workspace_id)
            return None

        exporters: list[object] = []
        log_dir = get_workspace_observability_log_dir(workspace_id)
        if mode == "local":
            try:
                exporters.append(LocalPerfExporter(config, log_dir=log_dir))
            except Exception:
                logger.warning(
                    "Failed to initialize local observability exporter: workspace_id=%s",
                    workspace_id,
                    exc_info=True,
                )
        else:
            try:
                exporters.append(create_langfuse_exporter(config))
            except Exception:
                logger.warning(
                    "Failed to initialize Langfuse exporter: workspace_id=%s",
                    workspace_id,
                    exc_info=True,
                )

        try:
            exporters.append(
                create_speedscope_exporter(
                    config,
                    output_dir=log_dir / "speedscope",
                )
            )
        except Exception:
            logger.warning(
                "Failed to initialize Speedscope exporter: workspace_id=%s",
                workspace_id,
                exc_info=True,
            )

        available_exporters = []
        for exporter in exporters:
            if getattr(exporter, "available", True) is False:
                close = getattr(exporter, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        logger.warning("Unavailable workspace exporter close failed", exc_info=True)
                continue
            available_exporters.append(exporter)
        return _WorkspaceExporterFanout(available_exporters) if available_exporters else None


def create_workspace_observability_router() -> WorkspaceObservabilityRouter:
    return WorkspaceObservabilityRouter()


def _order_spans_parent_first(spans: Iterable[OTelSpanSnapshot]) -> list[OTelSpanSnapshot]:
    ordered = sorted(
        spans,
        key=lambda item: (item.start_ms, item.end_ms, item.span_id),
    )
    span_by_id = {span.span_id: span for span in ordered if span.span_id}
    result: list[OTelSpanSnapshot] = []
    emitted: set[str] = set()
    visiting: set[str] = set()

    def append_with_parent(span: OTelSpanSnapshot) -> None:
        if span.span_id in emitted:
            return
        if span.span_id in visiting:
            return
        visiting.add(span.span_id)
        parent = span_by_id.get(str(span.parent_span_id or ""))
        if parent is not None:
            append_with_parent(parent)
        visiting.discard(span.span_id)
        if span.span_id not in emitted:
            emitted.add(span.span_id)
            result.append(span)

    for span in ordered:
        append_with_parent(span)
    return result


def resolve_workspace_router(router_id: str) -> WorkspaceObservabilityRouter | None:
    with _ROUTER_REGISTRY_LOCK:
        if router_id:
            return _ROUTERS.get(router_id)
        return _ROUTERS.get(_DEFAULT_ROUTER_ID)


def get_default_workspace_router() -> WorkspaceObservabilityRouter | None:
    return resolve_workspace_router("")


def register_run_runtime_event_consumer(run_id: str, consumer: Any) -> None:
    _register_runtime_event_consumer(run_id, consumer)


def unregister_run_runtime_event_consumer(run_id: str, consumer: Any) -> None:
    _unregister_runtime_event_consumer(run_id, consumer)


def _register_runtime_event_consumer(run_id: str, consumer: Any) -> None:
    resolved_run_id = str(run_id or "").strip()
    if not resolved_run_id or not callable(consumer):
        return
    with _RUN_CONSUMER_LOCK:
        _RUN_RUNTIME_EVENT_CONSUMERS.setdefault(resolved_run_id, []).append(consumer)


def _unregister_runtime_event_consumer(run_id: str, consumer: Any) -> None:
    resolved_run_id = str(run_id or "").strip()
    with _RUN_CONSUMER_LOCK:
        consumers = _RUN_RUNTIME_EVENT_CONSUMERS.get(resolved_run_id)
        if not consumers:
            return
        remaining = [item for item in consumers if item is not consumer]
        if remaining:
            _RUN_RUNTIME_EVENT_CONSUMERS[resolved_run_id] = remaining
        else:
            _RUN_RUNTIME_EVENT_CONSUMERS.pop(resolved_run_id, None)


def _take_runtime_event_consumers(run_id: str) -> list[Any]:
    with _RUN_CONSUMER_LOCK:
        return _RUN_RUNTIME_EVENT_CONSUMERS.pop(str(run_id or ""), [])


def _register_router(router: WorkspaceObservabilityRouter) -> None:
    global _DEFAULT_ROUTER_ID
    with _ROUTER_REGISTRY_LOCK:
        _ROUTERS[router.router_id] = router
        _DEFAULT_ROUTER_ID = router.router_id


def _unregister_router(router: WorkspaceObservabilityRouter) -> None:
    global _DEFAULT_ROUTER_ID
    with _ROUTER_REGISTRY_LOCK:
        _ROUTERS.pop(router.router_id, None)
        if _DEFAULT_ROUTER_ID == router.router_id:
            _DEFAULT_ROUTER_ID = next(reversed(_ROUTERS), "")


def _span_export_event(
    span: OTelSpanSnapshot,
    *,
    workspace_id: str,
) -> RuntimeEvent:
    return RuntimeEvent.from_otel(
        type=_OTEL_SPAN_EXPORTED,
        run_id=span.run_id,
        session_id=str(span.attributes.get("agent.session_id") or "") or None,
        turn_id=_optional_int(span.attributes.get("agent.turn_id")),
        trace_id=span.trace_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        payload={
            "workspace_id": workspace_id,
            "otel_record": span_record(span),
        },
        timestamp_ms=span.end_ms,
    )


def _run_export_events(
    spans: Sequence[OTelSpanSnapshot],
    *,
    workspace_id: str,
) -> Iterable[RuntimeEvent]:
    for span in spans:
        yield _span_export_event(span, workspace_id=workspace_id)
    yield from runtime_events_from_spans(spans)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_workspace_id(workspace_id: str) -> str | None:
    try:
        return validate_workspace_id(workspace_id)
    except ValueError:
        logger.warning("Invalid workspace id in observability event: %s", workspace_id)
        return None


def _current_workspace_id() -> str | None:
    try:
        return get_current_workspace_id()
    except Exception:
        return None
