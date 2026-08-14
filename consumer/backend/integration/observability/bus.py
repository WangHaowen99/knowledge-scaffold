"""Non-blocking observability event bus."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import queue
import threading
from typing import Callable, Deque, Iterable, Optional

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
    ) -> None:
        self._exporter = exporter
        self._queue_size = max(1, int(queue_size))
        self._batch_queue_size = max(1, int(batch_queue_size))
        self._queue: "queue.Queue[object]" = queue.Queue(
            maxsize=self._queue_size + self._batch_queue_size
        )
        self._queued_event_count = 0
        self._queued_batch_count = 0
        self._dropped_events: Deque[RuntimeEvent] = deque()
        self._drop_buffer_size = max(1, int(drop_buffer_size))
        self._drop_lock = threading.Lock()
        self._drop_notice_overflow_count = 0
        self._stop = object()
        self._closed = False
        self._lock = threading.Lock()
        self._exporter_closed = False
        self._exporter_close_lock = threading.Lock()
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
        with self._lock:
            if self._closed:
                return False
            if self._queued_event_count < self._queue_size:
                self._queued_event_count += 1
                self._queue.put_nowait(event)
                return True
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
        with self._lock:
            if self._closed:
                return False
            if self._queued_batch_count < self._batch_queue_size:
                self._queued_batch_count += 1
                self._queue.put_nowait(batch)
                return True
        logger.warning("Dropping observability run batch because the batch queue is full")
        return False

    def close(self, timeout: Optional[float] = 5.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.put_nowait(self._stop)
            except queue.Full:
                pass
        self._worker.join(timeout=timeout)
        if not self._worker.is_alive():
            self._close_exporter()

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

                try:
                    self._export_item(item)
                except Exception:
                    logger.warning("Observability exporter failed to export event", exc_info=True)
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
            try:
                self._export_item(item)
            except Exception:
                logger.warning("Observability exporter failed during drain", exc_info=True)
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

    def _record_drop(self, event: RuntimeEvent) -> None:
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
            self._record_drop(event)

    def _close_exporter(self) -> None:
        with self._exporter_close_lock:
            if self._exporter_closed:
                return
            self._exporter_closed = True
        close = getattr(self._exporter, "close", None)
        if not callable(close):
            return
        try:
            with self._export_lock:
                close()
        except Exception:
            logger.warning("Observability exporter close failed", exc_info=True)

    def _mark_dequeued(self, item: object) -> None:
        with self._lock:
            if isinstance(item, RuntimeEvent):
                self._queued_event_count = max(0, self._queued_event_count - 1)
            elif isinstance(item, _QueuedBatch):
                self._queued_batch_count = max(0, self._queued_batch_count - 1)

    def _export_item(self, event: object) -> None:
        if isinstance(event, _QueuedBatch):
            self._export_batch(event)
            return
        with self._export_lock:
            self._exporter.export(event)

    def _export_batch(self, batch: _QueuedBatch) -> None:
        try:
            for event in batch.event_factory():
                try:
                    with self._export_lock:
                        self._exporter.export(event)
                except Exception:
                    logger.warning(
                        "Observability exporter failed to export batched event",
                        exc_info=True,
                    )
        except Exception:
            logger.warning("Observability event batch generation failed", exc_info=True)
        finally:
            if batch.on_complete is not None:
                try:
                    batch.on_complete()
                except Exception:
                    logger.warning("Observability event batch completion failed", exc_info=True)
