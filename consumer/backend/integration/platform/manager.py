"""Independent onto-platform exporter lifecycle management."""

from __future__ import annotations

import atexit
import threading
from typing import Optional

from integration.observability.bus import ObservabilityBus
from utils.logging import get_logger
from integration.platform.config import PlatformConfig
from integration.platform.exporter import PlatformExporter

logger = get_logger("platform.manager")

_platform_lock = threading.RLock()
_platform_observer: Optional[ObservabilityBus] = None


def get_platform_observer() -> ObservabilityBus | None:
    """Return the process-scoped platform observer, starting it if enabled."""
    global _platform_observer
    with _platform_lock:
        if _platform_observer is None:
            _platform_observer = _build_platform_observer()
        return _platform_observer


def start_platform_observer_if_enabled() -> ObservabilityBus | None:
    """Start platform export if the platform connector config enables it."""
    return get_platform_observer()


def reset_platform_observer(timeout: float = 1.0, *, restart: bool = True) -> ObservabilityBus | None:
    """Close the current platform observer and optionally rebuild it."""
    global _platform_observer
    with _platform_lock:
        current = _platform_observer
        _platform_observer = None
    _close_platform_observer(current, timeout=timeout)
    if restart:
        return get_platform_observer()
    return None


def shutdown_platform_observer(timeout: float = 1.0) -> None:
    reset_platform_observer(timeout=timeout, restart=False)


def _build_platform_observer() -> ObservabilityBus | None:
    try:
        config = PlatformConfig.from_env()
    except Exception:
        logger.warning("Failed to load onto-platform connector config", exc_info=True)
        return None

    if not config.enabled:
        return None

    exporter = PlatformExporter(config, start_heartbeat=True)
    if not exporter.available:
        logger.warning("onto-platform connector is enabled but incomplete; skipping exporter")
        exporter.close()
        return None

    exporter.export_ontology_snapshots()
    return ObservabilityBus(exporter)


def _close_platform_observer(observer: ObservabilityBus | None, *, timeout: float) -> None:
    if observer is None:
        return
    observer.close(timeout=timeout)


atexit.register(shutdown_platform_observer)
