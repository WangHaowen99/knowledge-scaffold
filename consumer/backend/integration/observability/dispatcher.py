"""Runtime event fan-out across independently-owned observers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from utils.logging import get_logger
from integration.observability.events import RuntimeEvent

logger = get_logger("observability.dispatcher")


@dataclass(frozen=True)
class DispatchSink:
    observer: object
    close_on_close: bool = True


class RuntimeEventDispatcher:
    """Fan out runtime events while preserving sink lifecycle ownership."""

    def __init__(self, sinks: Iterable[DispatchSink]) -> None:
        self._sinks = [
            sink
            for sink in sinks
            if sink.observer is not None and getattr(sink.observer, "enabled", True)
        ]

    @property
    def enabled(self) -> bool:
        return bool(self._sinks)

    def emit(self, event: RuntimeEvent) -> bool:
        accepted = False
        for sink in self._sinks:
            emit = getattr(sink.observer, "emit", None)
            if not callable(emit):
                continue
            try:
                accepted = bool(emit(event)) or accepted
            except Exception:
                logger.warning("Runtime event sink failed to emit event", exc_info=True)
        return accepted

    def close(self, timeout: float = 1.0) -> None:
        for sink in self._sinks:
            if not sink.close_on_close:
                continue
            close = getattr(sink.observer, "close", None)
            if not callable(close):
                continue
            try:
                close(timeout=timeout)
            except TypeError:
                close()
            except Exception:
                logger.warning("Runtime event sink failed to close", exc_info=True)

    def retire(self, timeout: float = 1.0) -> None:
        for sink in self._sinks:
            if not sink.close_on_close:
                continue
            retire = getattr(sink.observer, "retire", None)
            if callable(retire):
                try:
                    retire(timeout=timeout)
                    continue
                except TypeError:
                    retire()
                    continue
                except Exception:
                    logger.warning("Runtime event sink failed to retire", exc_info=True)
            close = getattr(sink.observer, "close", None)
            if not callable(close):
                continue
            try:
                close(timeout=timeout)
            except TypeError:
                close()
            except Exception:
                logger.warning("Runtime event sink failed to close during retire", exc_info=True)
