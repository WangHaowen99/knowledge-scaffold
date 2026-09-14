"""Workspace-scoped observability event routing."""

from __future__ import annotations

import inspect
import threading
import time
import uuid
from typing import Any, Iterable, Sequence

from utils.logging import get_logger
from workspace.workspaces import get_current_workspace_id, validate_workspace_id
from integration.observability.bus import BatchExportOutcome, ObservabilityBus
from integration.observability.events import RuntimeEvent, RuntimeEventType
from integration.observability.identity import clear_runtime_trace_identity, record_runtime_event
from integration.observability.otel import (
    force_flush_open_telemetry,
    force_flush_runtime_metrics,
    record_observability_batch_metric,
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
_CLOSE_PHASE_FORCE_FLUSH = 0
_CLOSE_PHASE_DRAIN_RUNS = 1
_CLOSE_PHASE_CLOSE_BUSES = 2
_CLOSE_PHASE_SHUTDOWN_METRICS = 3
_CLOSE_PHASE_COMPLETE = 4


class _WorkspaceExporterFanout:
    """Keep one queue per workspace while isolating exporter failures."""

    metric_name = "workspace_fanout"

    def __init__(self, exporters: Iterable[Any]) -> None:
        self._exporters = [exporter for exporter in exporters if exporter is not None]
        self._closed_exporter_indexes: set[int] = set()
        self._close_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return bool(self._exporters)

    def export(self, event: RuntimeEvent) -> BatchExportOutcome:
        outcome = BatchExportOutcome()
        for exporter in self._exporters:
            try:
                exporter.export(event)
            except Exception:
                outcome = outcome.combine(BatchExportOutcome(failed_deliveries=1))
                logger.warning("Workspace observability exporter failed", exc_info=True)
            else:
                outcome = outcome.combine(BatchExportOutcome(succeeded_deliveries=1))
        return outcome

    def export_batch(self, events: Iterable[RuntimeEvent]) -> BatchExportOutcome:
        batch = tuple(events)
        if not batch:
            return BatchExportOutcome()
        outcome = BatchExportOutcome()
        for exporter in self._exporters:
            try:
                export_batch = (
                    getattr(exporter, "export_batch", None)
                    if inspect.getattr_static(exporter, "export_batch", None) is not None
                    else None
                )
                if callable(export_batch):
                    result = export_batch(batch)
                    outcome = outcome.combine(
                        result if isinstance(result, BatchExportOutcome) else BatchExportOutcome(succeeded_deliveries=1)
                    )
                    continue
                for event in batch:
                    try:
                        exporter.export(event)
                    except Exception:
                        outcome = outcome.combine(BatchExportOutcome(failed_deliveries=1))
                        logger.warning(
                            "Workspace observability exporter failed in batch fallback",
                            exc_info=True,
                        )
                    else:
                        outcome = outcome.combine(BatchExportOutcome(succeeded_deliveries=1))
            except Exception:
                outcome = outcome.combine(BatchExportOutcome(failed_deliveries=1))
                logger.warning(
                    "Workspace observability exporter batch failed",
                    exc_info=True,
                )
        return outcome

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

    def close(self) -> bool:
        all_closed = True
        with self._close_lock:
            for index, exporter in enumerate(self._exporters):
                if index in self._closed_exporter_indexes:
                    continue
                close = getattr(exporter, "close", None)
                if not callable(close):
                    self._closed_exporter_indexes.add(index)
                    continue
                try:
                    result = close()
                except Exception:
                    all_closed = False
                    logger.warning(
                        "Workspace observability exporter close failed",
                        exc_info=True,
                    )
                    continue
                if result is False:
                    all_closed = False
                    logger.warning("Workspace observability exporter close reported failure")
                    continue
                self._closed_exporter_indexes.add(index)
        return all_closed


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
        self._close_lock = threading.Lock()
        self._run_flush_lock = threading.RLock()
        self._observers_by_workspace: dict[str, ObservabilityBus | None] = {}
        self._run_workspaces: dict[str, str] = {}
        self._pending_spans_by_run: dict[str, list[OTelSpanSnapshot]] = {}
        self._pending_span_count = 0
        self._pending_span_drop_count = 0
        self._pending_span_peak_count = 0
        self._max_pending_spans_per_run = max(1, int(max_pending_spans_per_run))
        self._max_pending_spans_total = max(1, int(max_pending_spans_total))
        self._retiring = False
        self._closed = False
        self._draining_otel = False
        self._close_complete = False
        self._close_worker: threading.Thread | None = None
        self._close_phase = _CLOSE_PHASE_FORCE_FLUSH
        self._close_active_run_ids: tuple[str, ...] = ()
        self._close_was_retiring = False
        _register_router(self)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return True

    @property
    def accepts_new_metric_recorders(self) -> bool:
        with self._lock:
            return not self._closed

    def stats_snapshot(self) -> dict[str, int]:
        """Return bounded pending-span buffer health without run identities."""

        with self._lock:
            return {
                "pending_spans": self._pending_span_count,
                "pending_runs": len(self._pending_spans_by_run),
                "pending_span_dropped": self._pending_span_drop_count,
                "pending_span_peak": self._pending_span_peak_count,
                "max_pending_spans_per_run": self._max_pending_spans_per_run,
                "max_pending_spans_total": self._max_pending_spans_total,
            }

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

    def retire(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            self._retiring = True
            should_close = not self._run_workspaces
        if not force_flush_open_telemetry(timeout_millis=_remaining_timeout_millis(deadline)):
            logger.warning(
                "OpenTelemetry flush failed while retiring workspace router: router_id=%s",
                self.router_id,
            )
        if not force_flush_runtime_metrics(
            self.router_id,
            timeout_millis=_remaining_timeout_millis(deadline),
        ):
            logger.warning(
                "OpenTelemetry metric flush failed while retiring workspace router: router_id=%s",
                self.router_id,
            )
        if should_close:
            return self.close(timeout=_remaining_timeout_seconds(deadline))
        return False

    def close(self, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        started_worker = False
        while True:
            remaining = _remaining_timeout_seconds(deadline)
            acquired = (
                self._close_lock.acquire(timeout=remaining)
                if remaining > 0
                else self._close_lock.acquire(blocking=False)
            )
            if not acquired:
                return False
            worker: threading.Thread | None = None
            try:
                with self._lock:
                    if self._close_complete:
                        return True
                    if not self._closed:
                        self._closed = True
                        self._draining_otel = True
                        self._close_active_run_ids = tuple(self._run_workspaces)
                        self._close_was_retiring = self._retiring

                    no_local_resources = (
                        self._close_phase == _CLOSE_PHASE_FORCE_FLUSH
                        and (self._close_worker is None or not self._close_worker.is_alive())
                        and not self._run_workspaces
                        and not self._pending_spans_by_run
                        and not self._observers_by_workspace
                    )
                if remaining <= 0 and no_local_resources:
                    if shutdown_runtime_metrics(self.router_id, timeout_millis=0):
                        self._finish_close()
                        return True
                    return False

                worker = self._close_worker
                if worker is None or not worker.is_alive():
                    if started_worker:
                        return False
                    worker = threading.Thread(
                        target=self._run_close_worker,
                        args=(deadline,),
                        name=f"observability-close-{self.router_id[:8]}",
                        daemon=True,
                    )
                    self._close_worker = worker
                    worker.start()
                    started_worker = True
            finally:
                self._close_lock.release()

            if worker is None or worker is threading.current_thread():
                return False
            remaining = _remaining_timeout_seconds(deadline)
            worker.join(timeout=remaining)
            if worker.is_alive():
                return False
            with self._lock:
                if self._close_complete:
                    return True
            if _remaining_timeout_seconds(deadline) <= 0:
                return False

    def _run_close_worker(self, deadline: float) -> None:
        try:
            self._continue_close(deadline)
        except Exception:
            logger.warning(
                "Workspace observability close worker failed: router_id=%s",
                self.router_id,
                exc_info=True,
            )

    def _continue_close(self, deadline: float) -> None:
        with self._lock:
            phase = self._close_phase
        if phase == _CLOSE_PHASE_FORCE_FLUSH:
            if not self._force_flush_for_close(deadline):
                return
            with self._lock:
                phase = self._close_phase
        if phase == _CLOSE_PHASE_DRAIN_RUNS:
            if not self._drain_runs_for_close(deadline):
                return
            with self._lock:
                phase = self._close_phase
        if phase == _CLOSE_PHASE_CLOSE_BUSES:
            if not self._close_buses_for_close(deadline):
                return
            with self._lock:
                phase = self._close_phase
        if phase == _CLOSE_PHASE_SHUTDOWN_METRICS:
            if not shutdown_runtime_metrics(
                self.router_id,
                timeout_millis=_remaining_timeout_millis(deadline),
            ):
                return
            self._finish_close()

    def _force_flush_for_close(self, deadline: float) -> bool:
        with self._lock:
            self._ensure_close_observers_locked()
            active_run_ids = self._close_active_run_ids
        for run_id in active_run_ids:
            clear_runtime_trace_identity(run_id)
        flush_succeeded = True
        try:
            trace_flush_succeeded = force_flush_open_telemetry(timeout_millis=_remaining_timeout_millis(deadline))
        except Exception:
            flush_succeeded = False
            logger.warning(
                "OpenTelemetry flush failed before closing workspace router: router_id=%s",
                self.router_id,
                exc_info=True,
            )
        else:
            if not trace_flush_succeeded:
                flush_succeeded = False
                logger.warning(
                    "OpenTelemetry flush failed before closing workspace router: router_id=%s",
                    self.router_id,
                )
        try:
            metric_flush_succeeded = force_flush_runtime_metrics(
                self.router_id,
                timeout_millis=_remaining_timeout_millis(deadline),
            )
        except Exception:
            flush_succeeded = False
            logger.warning(
                "OpenTelemetry metric flush failed before closing workspace router: router_id=%s",
                self.router_id,
                exc_info=True,
            )
        else:
            if not metric_flush_succeeded:
                flush_succeeded = False
                logger.warning(
                    "OpenTelemetry metric flush failed before closing workspace router: router_id=%s",
                    self.router_id,
                )
        if not flush_succeeded:
            return False
        with self._lock:
            self._ensure_close_observers_locked()
            self._draining_otel = False
            self._close_phase = _CLOSE_PHASE_DRAIN_RUNS
        return True

    def _ensure_close_observers_locked(self) -> None:
        workspace_ids = set()
        for workspace_id in self._run_workspaces.values():
            if workspace_id:
                workspace_ids.add(workspace_id)
        for spans in self._pending_spans_by_run.values():
            for span in spans:
                if span.workspace_id:
                    workspace_ids.add(span.workspace_id)
        for workspace_id in sorted(workspace_ids):
            if workspace_id not in self._observers_by_workspace:
                self._create_observer_locked(workspace_id)

    def _drain_runs_for_close(self, deadline: float) -> bool:
        while True:
            with self._run_flush_lock:
                with self._lock:
                    pending_run_id = next(iter(self._pending_spans_by_run), "")
                    if not pending_run_id:
                        self._close_phase = _CLOSE_PHASE_CLOSE_BUSES
                        return True
                if _remaining_timeout_seconds(deadline) <= 0:
                    return False
                self._flush_run(pending_run_id)

    def _close_buses_for_close(self, deadline: float) -> bool:
        with self._run_flush_lock:
            with self._lock:
                observers = [observer for observer in self._observers_by_workspace.values() if observer is not None]
            if observers and _remaining_timeout_seconds(deadline) <= 0:
                return False
            buses_quiescent = True
            for observer in observers:
                try:
                    result = observer.close(timeout=_remaining_timeout_seconds(deadline))
                    buses_quiescent = result is not False and buses_quiescent
                except Exception:
                    buses_quiescent = False
                    logger.warning(
                        "Workspace observability observer close failed",
                        exc_info=True,
                    )
            if not buses_quiescent:
                return False
            with self._lock:
                self._close_phase = _CLOSE_PHASE_SHUTDOWN_METRICS
            return True

    def _finish_close(self) -> None:
        _unregister_router(self)
        with self._lock:
            self._observers_by_workspace.clear()
            self._run_workspaces.clear()
            self._pending_spans_by_run.clear()
            self._pending_span_count = 0
            self._draining_otel = False
            self._close_phase = _CLOSE_PHASE_COMPLETE
            self._close_complete = True

    def export_otel_span(self, span: OTelSpanSnapshot) -> None:
        if span.run_id:
            self._buffer_pending_span(span)
            return
        workspace_id = _validate_workspace_id(span.workspace_id)
        if not workspace_id:
            return
        with self._lock:
            if self._closed and not self._draining_otel:
                return
            observer = self._get_observer(workspace_id, allow_closed=True)
            if observer is not None:
                observer.emit(_span_export_event(span, workspace_id=workspace_id))

    def export_otel_metric(
        self,
        record: dict[str, Any],
        *,
        attributes: dict[str, Any],
        workspace_id: str = "",
    ) -> None:
        with self._lock:
            if self._closed and not self._draining_otel:
                return
        resolved_workspace_id = str(
            workspace_id or attributes.get("agent.workspace_id") or record.get("workspace_id") or ""
        ).strip()
        if not resolved_workspace_id:
            return
        observer = self._get_observer(resolved_workspace_id, allow_closed=True)
        if observer is None:
            return
        event = RuntimeEvent.from_otel(
            type=_OTEL_METRIC_EXPORTED,
            run_id=str(record.get("run_id") or ""),
            session_id=str(record.get("session_id") or "") or None,
            trace_id=None,
            span_id=None,
            parent_span_id=None,
            payload={"workspace_id": resolved_workspace_id, "otel_record": record},
            timestamp_ms=int(record.get("ts_ms") or 0),
        )
        emit_metric = getattr(observer, "emit_metric", None)
        if callable(emit_metric):
            emit_metric(event)
        else:
            observer.emit(event)

    def _flush_run(self, run_id: str) -> None:
        with self._run_flush_lock:
            self._flush_run_serialized(run_id)

    def _flush_run_serialized(self, run_id: str) -> None:
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
            self._release_pending_span_capacity(
                len(spans),
                workspace_id=workspace_id,
            )
            return
        observer = self._get_observer(workspace_id, allow_closed=True)
        if observer is None:
            self._release_pending_span_capacity(
                len(spans),
                workspace_id=workspace_id,
            )
            return
        try:
            accepted = observer.emit_batch(
                lambda: _run_export_events(spans, workspace_id=workspace_id),
                on_complete=lambda: self._release_pending_span_capacity(
                    len(spans),
                    workspace_id=workspace_id,
                ),
            )
        except Exception:
            self._release_pending_span_capacity(
                len(spans),
                workspace_id=workspace_id,
            )
            raise
        if not accepted:
            self._release_pending_span_capacity(
                len(spans),
                workspace_id=workspace_id,
            )

    def _release_pending_span_capacity(
        self,
        count: int,
        *,
        workspace_id: str = "",
    ) -> None:
        with self._lock:
            self._pending_span_count = max(0, self._pending_span_count - max(0, int(count)))
            current_depth = self._pending_span_count
        self._record_pending_span_metrics(
            workspace_id=workspace_id,
            current_depth=current_depth,
        )

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
            if self._closed and not self._draining_otel:
                return None
            return self._create_observer_locked(workspace_id)

    def _create_observer_locked(self, workspace_id: str) -> ObservabilityBus | None:
        exporter = self._build_exporter(workspace_id)
        observer = (
            ObservabilityBus(
                exporter,
                batch_queue_size=self._max_pending_spans_total,
                router_id=self.router_id,
                workspace_id=workspace_id,
            )
            if exporter is not None
            else None
        )
        self._observers_by_workspace[workspace_id] = observer
        return observer

    def _buffer_pending_span(self, span: OTelSpanSnapshot) -> None:
        dropped = False
        dropped_delta = 0
        peak_changed = False
        with self._lock:
            if self._closed and not self._draining_otel:
                return
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
                dropped_delta += 1
                spans = self._pending_spans_by_run.get(span.run_id)
                current_count = len(spans) if spans is not None else 0
            if current_count >= per_run_limit or self._pending_span_count >= self._max_pending_spans_total:
                self._pending_span_drop_count += 1
                dropped_delta += 1
                drop_count = self._pending_span_drop_count
                dropped = True
            else:
                if spans is None:
                    spans = []
                    self._pending_spans_by_run[span.run_id] = spans
                spans.append(span)
                self._pending_span_count += 1
                if self._pending_span_count > self._pending_span_peak_count:
                    self._pending_span_peak_count = self._pending_span_count
                    peak_changed = True
                drop_count = self._pending_span_drop_count
            current_depth = self._pending_span_count
            peak_depth = self._pending_span_peak_count
            workspace_id = str(span.workspace_id or self._run_workspaces.get(span.run_id, "")).strip()
        self._record_pending_span_metrics(
            workspace_id=workspace_id,
            current_depth=current_depth,
            dropped=dropped_delta,
            peak_depth=peak_depth if peak_changed else None,
        )
        if dropped and (drop_count == 1 or drop_count & (drop_count - 1) == 0):
            logger.warning(
                "Dropping buffered OpenTelemetry span because the pending span limit was reached: "
                "router_id=%s run_id=%s dropped=%s",
                self.router_id,
                span.run_id,
                drop_count,
            )

    def _record_pending_span_metrics(
        self,
        *,
        workspace_id: str,
        current_depth: int,
        dropped: int = 0,
        peak_depth: int | None = None,
    ) -> None:
        resolved_workspace_id = _validate_workspace_id(workspace_id)
        if not resolved_workspace_id:
            return
        try:
            if dropped:
                record_observability_batch_metric(
                    "dropped",
                    dropped,
                    exporter="otel_pending_spans",
                    result="queue_full",
                    router_id=self.router_id,
                    workspace_id=resolved_workspace_id,
                )
            record_observability_batch_metric(
                "current_depth",
                current_depth,
                exporter="otel_pending_spans",
                result="snapshot",
                router_id=self.router_id,
                workspace_id=resolved_workspace_id,
            )
            if peak_depth is not None:
                record_observability_batch_metric(
                    "peak_depth",
                    peak_depth,
                    exporter="otel_pending_spans",
                    result="snapshot",
                    router_id=self.router_id,
                    workspace_id=resolved_workspace_id,
                )
        except Exception:
            logger.warning("Pending OpenTelemetry span metric recording failed", exc_info=True)

    def _discard_pending_detail_span(self, *, preferred_run_id: str) -> bool:
        ordered_run_ids = [preferred_run_id] + [
            run_id for run_id in self._pending_spans_by_run if run_id != preferred_run_id
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

    @staticmethod
    def _build_exporter(workspace_id: str) -> object | None:
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


def _remaining_timeout_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _remaining_timeout_millis(deadline: float) -> int:
    return max(0, int(_remaining_timeout_seconds(deadline) * 1000))


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
