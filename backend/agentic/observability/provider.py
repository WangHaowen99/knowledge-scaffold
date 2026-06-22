"""Process-scoped observer provider."""

from __future__ import annotations

import atexit
import threading
from typing import Optional

from ..config.system_config import load_system_config
from ..utils.logging import get_logger
from .bus import ObservabilityBus
from .exporters import EXPORTER_REGISTRY
from .observer import NOOP_OBSERVER

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


def reset_observer(timeout: float = 1.0) -> None:
    """Drop the current observer so the next run uses the latest config."""
    global _observer
    with _observer_lock:
        current = _observer
        _observer = None
    _close_observer(current, timeout=timeout)


def shutdown_observer(timeout: float = 1.0) -> None:
    """Close the current process-scoped observer."""
    reset_observer(timeout=timeout)


def _build_observer():
    try:
        config = load_system_config().get("observability") or {}
    except Exception:
        logger.warning("Failed to load observability config; using noop observer", exc_info=True)
        return NOOP_OBSERVER

    if not bool(config.get("enabled", False)):
        return NOOP_OBSERVER

    mode = str(config.get("mode") or "local").strip().lower()
    exporter_factory = EXPORTER_REGISTRY.get(mode)
    if exporter_factory is None:
        logger.warning("Unknown observability exporter mode=%s; using noop observer", mode)
        return NOOP_OBSERVER

    try:
        exporter = exporter_factory(config)
        if getattr(exporter, "available", True) is False:
            logger.warning("Observability exporter mode=%s is unavailable; using noop observer", mode)
            return NOOP_OBSERVER

        return ObservabilityBus(exporter)
    except Exception:
        logger.warning(
            "Failed to initialize observability exporter mode=%s; using noop observer",
            mode,
            exc_info=True,
        )
        return NOOP_OBSERVER


def _close_observer(observer: Optional[object], *, timeout: float) -> None:
    if observer is None or observer is NOOP_OBSERVER:
        return
    close = getattr(observer, "close", None)
    if not callable(close):
        return
    close(timeout=timeout)


atexit.register(shutdown_observer)
