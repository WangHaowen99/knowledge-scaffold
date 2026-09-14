# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Non-blocking observability event bus."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import inspect
import queue
import threading
import time
from typing import Any, Callable, Deque, Iterable, Optional

from utils.logging import get_logger
from integration.observability.events import RuntimeEvent, RuntimeEventType
from integration.observability.exporters import RuntimeExporter

logger = get_logger("observability.bus")

_RUN_TERMINAL_EVENTS = {
    RuntimeEventType.RUN_FINISHED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}


@dataclass(frozen=True)
class _QueuedBatch:
    event_factory: Callable[[], Iterable[RuntimeEvent]]
    on_complete: Optional[Callable[[], None]] = None


@dataclass(frozen=True)
class _GaugeUpdate:
    pass


@dataclass(frozen=True)
class BatchExportOutcome:
    """Delivery-level result for one queued batch work item."""

    succeeded_deliveries: int = 0
    failed_deliveries: int = 0

    @property
    def result(self) -> str:
        if self.failed_deliveries <= 0:
            return "success"
        if self.succeeded_deliveries > 0:
            return "partial_failure"
        return "failure"

    def combine(self, other: "BatchExportOutcome") -> "BatchExportOutcome":
        return BatchExportOutcome(
            succeeded_deliveries=self.succeeded_deliveries
            + max(0, int(other.succeeded_deliveries)),
            failed_deliveries=self.failed_deliveries
            + max(0, int(other.failed_deliveries)),
        )


class ObservabilityBus:
    """Queue events for a background exporter.

    Responsible for non-blocking delivery, exporter flushing, and failure
    isolation. It does not understand provider-specific trace semantics.

    ``emit`` never waits for exporter I/O. If the queue is full, the event is
    dropped and the agent run continues.
    """

    def __init__(
        self,
        exporter: RuntimeExporter,
        *,
        queue_size: int = 1000,
        batch_queue_size: int = 10000,
        drop_buffer_size: int = 10000,
        flush_interval_seconds: float = 5.0,
        metric_recorder: Optional[Callable[..., None]] = None,
        router_id: str = "",
        workspace_id: str = "",
    ) -> None:
        self._exporter = exporter
        self._metric_exporter = _exporter_metric_name(exporter)
        self._metric_recorder = metric_recorder or _default_metric_recorder
        self._metric_router_id = str(router_id or "").strip()
        self._metric_workspace_id = str(workspace_id or "").strip()
        self._queue_size = max(1, int(queue_size))
        self._batch_queue_size = max(1, int(batch_queue_size))
        self._queue: "queue.Queue[object]" = queue.Queue(
            maxsize=self._queue_size + self._batch_queue_size + 2
        )
        self._queued_event_count = 0
        self._queued_batch_count = 0
        self._accepted_count = 0
        self._completed_count = 0
        self._exported_count = 0
        self._failed_count = 0
        self._dropped_count = 0
        self._peak_depth = 0
        self._shutdown_drain_timeout_count = 0
        self._dropped_events: Deque[RuntimeEvent] = deque()
        self._drop_buffer_size = max(1, int(drop_buffer_size))
        self._drop_lock = threading.Lock()
        self._drop_notice_overflow_count = 0
        self._stop = object()
        self._gauge_update = _GaugeUpdate()
        self._closed = False
        self._lock = threading.Lock()
        self._gauge_update_queued = False
        self._current_depth_dirty = False
        self._peak_depth_dirty = False
        self._pending_metric_counts: dict[tuple[str, str], float] = {}
        self._exporter_closed = False
        self._exporter_close_in_progress = False
        self._exporter_close_attempts = 0
        self._exporter_close_lock = threading.Lock()
        self._exporter_close_worker: threading.Thread | None = None
        self._export_lock = threading.RLock()
        self._flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self._worker = threading.Thread(
            target=self._run,
            name="agentic-observability-exporter",
            daemon=True,
        )
        self._worker.start()

    @property
    def enabled(self) -> bool:
        return True

    def emit(self, event: RuntimeEvent) -> bool:
        accepted, result = self._enqueue(item=event, is_batch=False)
        if accepted:
            return True
        if result == "queue_full":
            logger.warning(
                "Dropping observability event because queue is full: type=%s run_id=%s",
                event.type,
                event.run_id,
            )
            self._enqueue_drop_notice(event)
        return False

    def emit_batch(
        self,
        event_factory: Callable[[], Iterable[RuntimeEvent]],
        *,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Queue one run unit bounded by the router's pending-span limits."""
        batch = _QueuedBatch(event_factory=event_factory, on_complete=on_complete)
        accepted, result = self._enqueue(item=batch, is_batch=True)
        if accepted:
            return True
        if result == "queue_full":
            logger.warning("Dropping observability run batch because the batch queue is full")
        return False

    def stats_snapshot(self) -> dict[str, int]:
        """Return queue-work-item counts; one lazy batch is one work item."""
        with self._lock:
            return {
                "accepted": self._accepted_count,
                "completed": self._completed_count,
                "exported": self._exported_count,
                "failed": self._failed_count,
                "dropped": self._dropped_count,
                "current_depth": self._current_depth_unlocked(),
                "peak_depth": self._peak_depth,
                "shutdown_drain_timeout": self._shutdown_drain_timeout_count,
            }

    def close(self, timeout: Optional[float] = 5.0) -> bool:
        deadline = (
            None
            if timeout is None
            else time.monotonic() + max(0.0, float(timeout))
        )

        def remaining_timeout() -> Optional[float]:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        with self._exporter_close_lock:
            close_attempts_before = self._exporter_close_attempts
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._queue.put_nowait(self._stop)
                except queue.Full:
                    pass
        self._worker.join(timeout=remaining_timeout())
        if self._worker.is_alive():
            self._record_shutdown_timeout()
            return False
        with self._exporter_close_lock:
            if self._exporter_closed:
                return True
            close_attempted_by_worker = (
                self._exporter_close_attempts > close_attempts_before
            )
        if close_attempted_by_worker:
            return False
        remaining = remaining_timeout()
        if remaining is not None and remaining <= 0:
            self._record_shutdown_timeout()
            return False
        close_worker = self._start_exporter_close_worker()
        if close_worker is None:
            with self._exporter_close_lock:
                return self._exporter_closed
        close_worker.join(timeout=remaining_timeout())
        if close_worker.is_alive():
            self._record_shutdown_timeout()
            return False
        with self._exporter_close_lock:
            return self._exporter_closed

    def emit_metric(self, event: RuntimeEvent) -> bool:
        """Persist an OTel metric outside the trace-detail queue."""
        with self._lock:
            if self._closed:
                return False
        try:
            self._export_item(event)
            return True
        except Exception:
            logger.warning("Observability exporter failed to export metric", exc_info=True)
            return False

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._flush_interval_seconds)
                except queue.Empty:
                    self._flush_exporter()
                    if self._is_closed():
                        break
                    continue

                self._mark_dequeued(item)
                if item is self._stop:
                    break
                if isinstance(item, _GaugeUpdate):
                    self._flush_metric_updates()
                    if self._is_closed() and self._queue.empty():
                        break
                    continue

                try:
                    outcome = self._export_item(item)
                except Exception:
                    outcome = BatchExportOutcome(failed_deliveries=1)
                    logger.warning("Observability exporter failed to export event", exc_info=True)
                self._mark_completed(outcome)
                self._drain_drop_notices()
                if self._is_closed() and self._queue.empty():
                    break

            self._drain()
            self._drain_drop_notices()
            self._flush_exporter()
        finally:
            self._close_exporter()

    def _is_closed(self) -> bool:
        with self._lock:
            return self._closed

    def _drain(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            self._mark_dequeued(item)
            if item is self._stop:
                continue
            if isinstance(item, _GaugeUpdate):
                self._flush_metric_updates()
                continue
            try:
                outcome = self._export_item(item)
            except Exception:
                outcome = BatchExportOutcome(failed_deliveries=1)
                logger.warning("Observability exporter failed during drain", exc_info=True)
            self._mark_completed(outcome)
            self._drain_drop_notices()

    def _flush_exporter(self) -> None:
        flush = getattr(self._exporter, "flush", None)
        if not callable(flush):
            return
        try:
            with self._export_lock:
                flush()
        except Exception:
            logger.warning("Observability exporter flush failed", exc_info=True)

    def _notify_exporter_drop(self, event: RuntimeEvent) -> None:
        record_drop = getattr(self._exporter, "record_drop", None)
        if not callable(record_drop):
            return
        try:
            with self._export_lock:
                record_drop(event)
        except Exception:
            logger.warning("Observability exporter failed to record dropped event", exc_info=True)

    def _enqueue_drop_notice(self, event: RuntimeEvent) -> None:
        with self._drop_lock:
            if len(self._dropped_events) < self._drop_buffer_size:
                self._dropped_events.append(event)
                return
            if event.type in _RUN_TERMINAL_EVENTS:
                for index, existing in enumerate(self._dropped_events):
                    if existing.type not in _RUN_TERMINAL_EVENTS:
                        self._dropped_events.rotate(-index)
                        self._dropped_events.popleft()
                        self._dropped_events.rotate(index)
                        self._dropped_events.append(event)
                        return
                self._dropped_events.popleft()
                self._dropped_events.append(event)
                return
            self._drop_notice_overflow_count += 1

    def _drain_drop_notices(self) -> None:
        notices = []
        overflow_count = 0
        with self._drop_lock:
            while self._dropped_events:
                notices.append(self._dropped_events.popleft())
            if self._drop_notice_overflow_count:
                overflow_count = self._drop_notice_overflow_count
                self._drop_notice_overflow_count = 0
        if overflow_count:
            logger.warning(
                "Dropped %s observability drop notices because the drop buffer was full",
                overflow_count,
            )
        for event in notices:
            self._notify_exporter_drop(event)

    def _close_exporter(self) -> bool:
        with self._exporter_close_lock:
            if self._exporter_closed:
                return True
            if self._exporter_close_in_progress:
                return False
            self._exporter_close_in_progress = True
            self._exporter_close_attempts += 1
        succeeded = False
        try:
            close = getattr(self._exporter, "close", None)
            if not callable(close):
                succeeded = True
            else:
                with self._export_lock:
                    result = close()
                succeeded = result is not False
        except Exception:
            logger.warning("Observability exporter close failed", exc_info=True)
        else:
            if not succeeded:
                logger.warning("Observability exporter close reported failure")
        finally:
            with self._exporter_close_lock:
                self._exporter_close_in_progress = False
                if succeeded:
                    self._exporter_closed = True
        return succeeded

    def _start_exporter_close_worker(self) -> threading.Thread | None:
        with self._exporter_close_lock:
            if self._exporter_closed:
                return None
            worker = self._exporter_close_worker
            if worker is not None and worker.is_alive():
                return worker
            if self._exporter_close_in_progress:
                return None
            worker = threading.Thread(
                target=self._close_exporter,
                name="agentic-observability-exporter-close",
                daemon=True,
            )
            self._exporter_close_worker = worker
            worker.start()
            return worker

    def _record_shutdown_timeout(self) -> None:
        with self._lock:
            self._shutdown_drain_timeout_count += 1
            self._increment_metric_unlocked(
                "shutdown_drain_timeout",
                result="timeout",
            )
            self._schedule_gauge_update_unlocked()

    def _mark_dequeued(self, item: object) -> None:
        changed = False
        with self._lock:
            if isinstance(item, RuntimeEvent):
                self._queued_event_count = max(0, self._queued_event_count - 1)
                changed = True
            elif isinstance(item, _QueuedBatch):
                self._queued_batch_count = max(0, self._queued_batch_count - 1)
                changed = True
            if changed:
                self._current_depth_dirty = True
                self._schedule_gauge_update_unlocked()

    def _export_item(self, event: object) -> BatchExportOutcome:
        if isinstance(event, _QueuedBatch):
            return self._export_batch(event)
        with self._export_lock:
            result = self._exporter.export(event)
        if isinstance(result, BatchExportOutcome):
            return result
        return BatchExportOutcome(succeeded_deliveries=1)

    def _export_batch(self, batch: _QueuedBatch) -> BatchExportOutcome:
        outcome = BatchExportOutcome()
        try:
            events = tuple(batch.event_factory())
            with self._export_lock:
                export_batch = _declared_batch_exporter(self._exporter)
                if callable(export_batch):
                    result = export_batch(events)
                    outcome = (
                        result
                        if isinstance(result, BatchExportOutcome)
                        else BatchExportOutcome(succeeded_deliveries=1)
                    )
                else:
                    for event in events:
                        try:
                            self._exporter.export(event)
                        except Exception:
                            outcome = outcome.combine(
                                BatchExportOutcome(failed_deliveries=1)
                            )
                            logger.warning(
                                "Observability exporter failed to export batched event",
                                exc_info=True,
                            )
                        else:
                            outcome = outcome.combine(
                                BatchExportOutcome(succeeded_deliveries=1)
                            )
        finally:
            if batch.on_complete is not None:
                try:
                    batch.on_complete()
                except Exception:
                    logger.warning("Observability event batch completion failed", exc_info=True)
        return outcome

    def _enqueue(self, *, item: object, is_batch: bool) -> tuple[bool, str]:
        accepted = False
        result = "queue_full"
        with self._lock:
            if self._closed:
                result = "closed"
            else:
                current_count = (
                    self._queued_batch_count if is_batch else self._queued_event_count
                )
                limit = self._batch_queue_size if is_batch else self._queue_size
                if current_count < limit:
                    if is_batch:
                        self._queued_batch_count += 1
                    else:
                        self._queued_event_count += 1
                    self._queue.put_nowait(item)
                    self._accepted_count += 1
                    self._increment_metric_unlocked("accepted", result="success")
                    self._current_depth_dirty = True
                    depth = self._current_depth_unlocked()
                    if depth > self._peak_depth:
                        self._peak_depth = depth
                        self._peak_depth_dirty = True
                    self._schedule_gauge_update_unlocked()
                    accepted = True
                    result = "success"
            if not accepted:
                self._dropped_count += 1
                self._increment_metric_unlocked("dropped", result=result)
                self._schedule_gauge_update_unlocked()
        return accepted, result

    def _mark_completed(self, outcome: BatchExportOutcome) -> None:
        with self._lock:
            self._completed_count += 1
            if outcome.failed_deliveries > 0:
                self._failed_count += 1
            else:
                self._exported_count += 1
            self._increment_metric_unlocked("completed", result=outcome.result)
            if outcome.failed_deliveries > 0:
                self._increment_metric_unlocked("failed", result=outcome.result)
            else:
                self._increment_metric_unlocked("exported", result="success")
            self._schedule_gauge_update_unlocked()

    def _current_depth_unlocked(self) -> int:
        return self._queued_event_count + self._queued_batch_count

    def _increment_metric_unlocked(
        self,
        name: str,
        *,
        result: str,
        value: int | float = 1,
    ) -> None:
        key = (name, result)
        self._pending_metric_counts[key] = (
            self._pending_metric_counts.get(key, 0.0) + float(value)
        )

    def _schedule_gauge_update_unlocked(self) -> None:
        if self._gauge_update_queued:
            return
        self._gauge_update_queued = True
        try:
            self._queue.put_nowait(self._gauge_update)
        except queue.Full:
            self._gauge_update_queued = False
            logger.warning("Observability metric control queue is unexpectedly full")

    def _flush_metric_updates(self) -> None:
        with self._lock:
            self._gauge_update_queued = False
            metric_counts = self._pending_metric_counts
            self._pending_metric_counts = {}
            current_depth_dirty = self._current_depth_dirty
            peak_depth_dirty = self._peak_depth_dirty
            current_depth = self._current_depth_unlocked()
            peak_depth = self._peak_depth
            self._current_depth_dirty = False
            self._peak_depth_dirty = False
        for (name, result), value in metric_counts.items():
            self._publish_metric(name, value, result=result)
        if current_depth_dirty:
            self._publish_metric("current_depth", current_depth, result="snapshot")
        if peak_depth_dirty:
            self._publish_metric("peak_depth", peak_depth, result="snapshot")

    def _publish_metric(self, name: str, value: int | float, *, result: str) -> None:
        try:
            self._metric_recorder(
                name,
                value,
                exporter=self._metric_exporter,
                result=result,
                router_id=self._metric_router_id,
                workspace_id=self._metric_workspace_id,
            )
        except Exception:
            logger.warning("Observability queue metric recording failed", exc_info=True)


def _default_metric_recorder(
    name: str,
    value: int | float,
    *,
    exporter: str,
    result: str,
    router_id: str,
    workspace_id: str,
) -> None:
    from integration.observability.otel import record_observability_batch_metric

    record_observability_batch_metric(
        name,
        value,
        exporter=exporter,
        result=result,
        router_id=router_id,
        workspace_id=workspace_id,
    )


def _exporter_metric_name(exporter: object) -> str:
    value = str(getattr(exporter, "metric_name", "") or "").strip().lower()
    if value in {"local", "langfuse", "speedscope", "workspace_fanout"}:
        return value
    return "custom"


def _declared_batch_exporter(exporter: object) -> Optional[Callable[..., Any]]:
    if inspect.getattr_static(exporter, "export_batch", None) is None:
        return None
    candidate = getattr(exporter, "export_batch", None)
    return candidate if callable(candidate) else None
