"""Process-scoped observer provider."""

from __future__ import annotations

import atexit
import threading
from typing import Optional

from utils.logging import get_logger
from integration.observability.dispatcher import DispatchSink, RuntimeEventDispatcher
from integration.observability.observer import NOOP_OBSERVER
from integration.observability.workspace_router import create_workspace_observability_router

logger = get_logger("observability.provider")

_observer_lock = threading.RLock()
_observer: Optional[object] = None


def get_observer():
    """Return the process-scoped observer for runtime events."""
    global _observer
    with _observer_lock:
        if _observer is None:
            _observer = _build_observer()
        return _observer


def get_observer_for_run(run_id: str, workspace_id: str):
    """Return and reserve the process observer before config refresh can retire it."""
    global _observer
    with _observer_lock:
        if _observer is None:
            _observer = _build_observer()
        reserve_run = getattr(_observer, "reserve_run", None)
        if callable(reserve_run):
            try:
                reserve_run(run_id, workspace_id)
            except Exception:
                logger.warning("Failed to reserve observability observer for run %s", run_id, exc_info=True)
        return _observer


def reset_observer(timeout: float = 5.0) -> None:
    """Drop the current observer so the next run uses the latest config."""
    global _observer
    with _observer_lock:
        current = _observer
        _observer = None
    _retire_observer(current, timeout=timeout)


def shutdown_observer(timeout: float = 5.0) -> None:
    """Close the current process-scoped observer."""
    global _observer
    with _observer_lock:
        current = _observer
        _observer = None
    _close_observer(current, timeout=timeout)


def _build_observer():
    from integration.platform.manager import get_platform_observer

    workspace_observer = _build_workspace_observer()
    platform_observer = get_platform_observer()

    sinks: list[DispatchSink] = []
    if workspace_observer is not NOOP_OBSERVER:
        sinks.append(DispatchSink(workspace_observer, close_on_close=True))
    if platform_observer is not None:
        sinks.append(DispatchSink(platform_observer, close_on_close=False))

    if not sinks:
        return NOOP_OBSERVER
    if len(sinks) == 1 and sinks[0].close_on_close:
        return sinks[0].observer
    return RuntimeEventDispatcher(sinks)


def _build_workspace_observer():
    try:
        router = create_workspace_observability_router()
    except Exception:
        logger.warning("Failed to initialize workspace observability router; using noop observer", exc_info=True)
        return NOOP_OBSERVER

    if getattr(router, "available", True) is False:
        return NOOP_OBSERVER
    return router


def _close_observer(observer: Optional[object], *, timeout: float) -> None:
    if observer is None or observer is NOOP_OBSERVER:
        return
    close = getattr(observer, "close", None)
    if not callable(close):
        return
    close(timeout=timeout)


def _retire_observer(observer: Optional[object], *, timeout: float) -> None:
    if observer is None or observer is NOOP_OBSERVER:
        return
    retire = getattr(observer, "retire", None)
    if callable(retire):
        retire(timeout=timeout)
        return
    _close_observer(observer, timeout=timeout)


atexit.register(shutdown_observer)
