"""Process-scoped, non-blocking OpenJiuwen runtime warm-up."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from utils.logging import get_logger


logger = get_logger("warmup")

_WARMUP_DISABLED = "disabled"
_WARMUP_IDLE = "idle"
_WARMUP_RUNNING = "running"
_WARMUP_SUCCESS = "success"
_WARMUP_FAILED = "failed"
_TERMINAL_STATES = frozenset({_WARMUP_SUCCESS, _WARMUP_FAILED})


@dataclass
class RuntimeWarmupStatus:
    """Snapshot of the process-wide warm-up lifecycle."""

    enabled: bool
    state: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    mode: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


_status = RuntimeWarmupStatus(enabled=False, state=_WARMUP_IDLE)
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy_status_unlocked() -> RuntimeWarmupStatus:
    return RuntimeWarmupStatus(
        enabled=_status.enabled,
        state=_status.state,
        started_at=_status.started_at,
        finished_at=_status.finished_at,
        duration_ms=_status.duration_ms,
        mode=_status.mode,
        error_type=_status.error_type,
        error_message=_status.error_message,
    )


def get_runtime_warmup_status() -> RuntimeWarmupStatus:
    """Return a detached copy of the current warm-up status."""
    with _lock:
        return _copy_status_unlocked()


def start_runtime_warmup(*, agent_config: Any = None) -> RuntimeWarmupStatus:
    """Start warm-up once in a daemon thread without delaying app startup."""
    enabled = bool(getattr(agent_config, "runtime_warmup_enabled", True))

    with _lock:
        if not enabled:
            _status.enabled = False
            _status.state = _WARMUP_DISABLED
            _status.started_at = None
            _status.finished_at = None
            _status.duration_ms = None
            _status.mode = None
            _status.error_type = None
            _status.error_message = None
            logger.info("Runtime warm-up disabled by system config")
            return _copy_status_unlocked()

        if _status.state in (_WARMUP_RUNNING, *_TERMINAL_STATES):
            return _copy_status_unlocked()

        _status.enabled = True
        _status.state = _WARMUP_RUNNING
        _status.started_at = _now_iso()
        _status.finished_at = None
        _status.duration_ms = None
        _status.mode = None
        _status.error_type = None
        _status.error_message = None

    thread = threading.Thread(
        target=_run_warmup,
        args=(agent_config,),
        name="runtime-warmup",
        daemon=True,
    )
    thread.start()
    logger.info("Runtime warm-up started mode=jiuwen-runtime")
    return get_runtime_warmup_status()


def _run_warmup(agent_config: Any) -> None:
    try:
        from agent.core.jiuwen_engine import warm_up_jiuwen_runtime

        result = warm_up_jiuwen_runtime(agent_config=agent_config)

        with _lock:
            _status.state = _WARMUP_SUCCESS
            _status.finished_at = _now_iso()
            _status.duration_ms = result.get("duration_ms")
            _status.mode = result.get("mode")

        logger.info(
            "Runtime warm-up finished state=success duration_ms=%s",
            result.get("duration_ms"),
        )
    except Exception:
        with _lock:
            _status.state = _WARMUP_FAILED
            _status.finished_at = _now_iso()
            _status.error_type = None
            _status.error_message = None
        logger.warning("Runtime warm-up finished state=failed")
