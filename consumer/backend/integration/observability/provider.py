"""Process-scoped observer provider."""

from __future__ import annotations

import atexit
import threading
import time
from typing import Optional

from utils.logging import get_logger
from integration.observability.dispatcher import DispatchSink, RuntimeEventDispatcher
from integration.observability.observer import NOOP_OBSERVER
from integration.observability.workspace_router import create_workspace_observability_router

logger = get_logger("observability.provider")

_observer_lock = threading.RLock()
_observer: Optional[object] = None
_retiring_observers: list[object] = []


def set_observer_for_test(observer: Optional[object]) -> None:
    """Install the process observer for lifecycle tests without bypassing its lock."""
    global _observer
    with _observer_lock:
        _observer = observer


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


def reset_observer(timeout: float = 5.0) -> bool:
    """Drop the current observer so the next run uses the latest config."""
    global _observer, _retiring_observers
    with _observer_lock:
        candidates = _unique_observers([*_retiring_observers, _observer])
        _observer = None
        _retiring_observers = []
    deadline = time.monotonic() + max(0.0, float(timeout))
    incomplete = []
    for observer in candidates:
        if not _retire_observer(
            observer,
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            incomplete.append(observer)
    _retain_incomplete_observers(incomplete)
    return not incomplete


def shutdown_observer(timeout: float = 5.0) -> bool:
    """Close the current process-scoped observer."""
    global _observer, _retiring_observers
    with _observer_lock:
        candidates = _unique_observers([*_retiring_observers, _observer])
        _observer = None
        _retiring_observers = []
    deadline = time.monotonic() + max(0.0, float(timeout))
    incomplete = []
    for observer in candidates:
        if not _close_observer(
            observer,
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            incomplete.append(observer)
    _retain_incomplete_observers(incomplete)
    return not incomplete


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


def _unique_observers(observers: list[object | None]) -> list[object]:
    unique: list[object] = []
    identities: set[int] = set()
    for observer in observers:
        if observer is None or observer is NOOP_OBSERVER or id(observer) in identities:
            continue
        identities.add(id(observer))
        unique.append(observer)
    return unique


def _retain_incomplete_observers(observers: list[object]) -> None:
    global _retiring_observers
    if not observers:
        return
    with _observer_lock:
        _retiring_observers = _unique_observers(
            [*_retiring_observers, *observers]
        )


def _close_observer(observer: Optional[object], *, timeout: float) -> bool:
    if observer is None or observer is NOOP_OBSERVER:
        return True
    close = getattr(observer, "close", None)
    if not callable(close):
        return True
    try:
        result = close(timeout=timeout)
    except TypeError:
        try:
            result = close()
        except Exception:
            logger.warning("Failed to close observability observer", exc_info=True)
            return False
    except Exception:
        logger.warning("Failed to close observability observer", exc_info=True)
        return False
    return result is not False


def _retire_observer(observer: Optional[object], *, timeout: float) -> bool:
    if observer is None or observer is NOOP_OBSERVER:
        return True
    retire = getattr(observer, "retire", None)
    if callable(retire):
        try:
            result = retire(timeout=timeout)
        except TypeError:
            try:
                result = retire()
            except Exception:
                logger.warning("Failed to retire observability observer", exc_info=True)
                return False
        except Exception:
            logger.warning("Failed to retire observability observer", exc_info=True)
            return False
        return result is not False
    return _close_observer(observer, timeout=timeout)


atexit.register(shutdown_observer)
