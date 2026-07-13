from __future__ import annotations

from collections.abc import Iterable

from integration.observability.events import RuntimeEvent


class CompositeExporter:
    """Fan out one runtime event to multiple exporters."""

    def __init__(self, exporters: Iterable[object]) -> None:
        self._exporters = [exporter for exporter in exporters if exporter is not None]

    @property
    def available(self) -> bool:
        return any(getattr(exporter, "available", True) for exporter in self._exporters)

    def export(self, event: RuntimeEvent) -> None:
        for exporter in self._exporters:
            if getattr(exporter, "available", True):
                exporter.export(event)

    def flush(self) -> None:
        for exporter in self._exporters:
            flush = getattr(exporter, "flush", None)
            if callable(flush):
                flush()

    def close(self) -> None:
        for exporter in self._exporters:
            close = getattr(exporter, "close", None)
            if callable(close):
                close()
