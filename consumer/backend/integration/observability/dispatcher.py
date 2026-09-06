"""Runtime event fan-out across independently-owned observers."""

from __future__ import annotations

from dataclasses import dataclass
import time
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

    def release_run(self, run_id: str) -> None:
        for sink in self._sinks:
            release_run = getattr(sink.observer, "release_run", None)
            if not callable(release_run):
                continue
            try:
                release_run(run_id)
            except Exception:
                logger.warning("Runtime event sink failed to release run", exc_info=True)

    def reserve_run(self, run_id: str, workspace_id: str) -> bool:
        reserved = False
        for sink in self._sinks:
            reserve_run = getattr(sink.observer, "reserve_run", None)
            if not callable(reserve_run):
                continue
            try:
                reserved = bool(reserve_run(run_id, workspace_id)) or reserved
            except Exception:
                logger.warning("Runtime event sink failed to reserve run", exc_info=True)
        return reserved

    def close(self, timeout: float = 1.0) -> bool:
        complete = True
        deadline = time.monotonic() + max(0.0, float(timeout))
        for sink in self._sinks:
            if not sink.close_on_close:
                continue
            close = getattr(sink.observer, "close", None)
            if not callable(close):
                continue
            try:
                result = close(timeout=max(0.0, deadline - time.monotonic()))
            except TypeError:
                try:
                    result = close()
                except Exception:
                    complete = False
                    logger.warning("Runtime event sink failed to close", exc_info=True)
                    continue
                else:
                    complete = result is not False and complete
            except Exception:
                complete = False
                logger.warning("Runtime event sink failed to close", exc_info=True)
            else:
                complete = result is not False and complete
        return complete

    def retire(self, timeout: float = 1.0) -> bool:
        complete = True
        deadline = time.monotonic() + max(0.0, float(timeout))
        for sink in self._sinks:
            if not sink.close_on_close:
                continue
            retire = getattr(sink.observer, "retire", None)
            if callable(retire):
                try:
                    result = retire(
                        timeout=max(0.0, deadline - time.monotonic())
                    )
                    complete = result is not False and complete
                    continue
                except TypeError:
                    try:
                        result = retire()
                    except Exception:
                        complete = False
                        logger.warning(
                            "Runtime event sink failed to retire",
                            exc_info=True,
                        )
                    else:
                        complete = result is not False and complete
                    continue
                except Exception:
                    complete = False
                    logger.warning("Runtime event sink failed to retire", exc_info=True)
            close = getattr(sink.observer, "close", None)
            if not callable(close):
                continue
            try:
                result = close(timeout=max(0.0, deadline - time.monotonic()))
            except TypeError:
                try:
                    result = close()
                except Exception:
                    complete = False
                    logger.warning(
                        "Runtime event sink failed to close during retire",
                        exc_info=True,
                    )
                    continue
                else:
                    complete = result is not False and complete
            except Exception:
                complete = False
                logger.warning("Runtime event sink failed to close during retire", exc_info=True)
            else:
                complete = result is not False and complete
        return complete
