"""Non-blocking observability event bus."""

from __future__ import annotations

from collections import deque
import queue
import threading
from typing import Deque, Optional

from utils.logging import get_logger
from integration.observability.events import RuntimeEvent, RuntimeEventType
from integration.observability.exporters import RuntimeExporter

logger = get_logger("observability.bus")

_RUN_TERMINAL_EVENTS = {
    RuntimeEventType.RUN_FINISHED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_CANCELLED,
}


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
        drop_buffer_size: int = 10000,
        flush_interval_seconds: float = 5.0,
    ) -> None:
        self._exporter = exporter
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._dropped_events: Deque[RuntimeEvent] = deque()
        self._drop_buffer_size = max(1, int(drop_buffer_size))
        self._drop_lock = threading.Lock()
        self._drop_notice_overflow_count = 0
        self._stop = object()
        self._closed = False
        self._lock = threading.Lock()
        self._exporter_closed = False
        self._exporter_close_lock = threading.Lock()
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
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                pass
        logger.warning(
            "Dropping observability event because queue is full: type=%s run_id=%s",
            event.type,
            event.run_id,
        )
        self._enqueue_drop_notice(event)
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
        # Exporters own their final flush. Close is idempotent, so invoke it
        # even when a daemon worker did not finish draining before shutdown.
        self._close_exporter()

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

                if item is self._stop:
                    break

                try:
                    self._exporter.export(item)
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
            if item is self._stop:
                continue
            try:
                self._exporter.export(item)
            except Exception:
                logger.warning("Observability exporter failed during drain", exc_info=True)
            self._drain_drop_notices()

    def _flush_exporter(self) -> None:
        flush = getattr(self._exporter, "flush", None)
        if not callable(flush):
            return
        try:
            flush()
        except Exception:
            logger.warning("Observability exporter flush failed", exc_info=True)

    def _record_drop(self, event: RuntimeEvent) -> None:
        record_drop = getattr(self._exporter, "record_drop", None)
        if not callable(record_drop):
            return
        try:
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
            close()
        except Exception:
            logger.warning("Observability exporter close failed", exc_info=True)
