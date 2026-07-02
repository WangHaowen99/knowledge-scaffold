"""Non-blocking observability event bus."""

from __future__ import annotations

import queue
import threading
from typing import Optional

from ..utils.logging import get_logger
from .events import RuntimeEvent
from .exporters import RuntimeExporter

logger = get_logger("observability.bus")


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
        flush_interval_seconds: float = 5.0,
    ) -> None:
        self._exporter = exporter
        self._queue: "queue.Queue[object]" = queue.Queue(maxsize=max(1, int(queue_size)))
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
                logger.warning(
                    "Dropping observability event because queue is full: type=%s run_id=%s",
                    event.type,
                    event.run_id,
                )
                return False

    def close(self, timeout: Optional[float] = 1.0) -> None:
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
                if self._is_closed() and self._queue.empty():
                    break

            self._drain()
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

    def _flush_exporter(self) -> None:
        flush = getattr(self._exporter, "flush", None)
        if not callable(flush):
            return
        try:
            flush()
        except Exception:
            logger.warning("Observability exporter flush failed", exc_info=True)

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
