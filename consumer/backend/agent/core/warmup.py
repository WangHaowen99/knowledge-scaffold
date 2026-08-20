"""Lifespan-owned, non-model OpenJiuwen runtime warm-up."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Any, Optional

from utils.logging import get_logger


logger = get_logger("warmup")

_WARMUP_DISABLED = "disabled"
_WARMUP_IDLE = "idle"
_WARMUP_RUNNING = "running"
_WARMUP_SUCCESS = "success"
_WARMUP_FAILED = "failed"
_WARMUP_CANCELLED = "cancelled"
_TERMINAL_STATES = frozenset(
    {_WARMUP_SUCCESS, _WARMUP_FAILED, _WARMUP_CANCELLED}
)


@dataclass
class RuntimeWarmupStatus:
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
_task: Optional[asyncio.Task[Any]] = None
_cancel_event: threading.Event = threading.Event()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy_status_unlocked() -> RuntimeWarmupStatus:
    return RuntimeWarmupStatus(**_status.__dict__)


def get_runtime_warmup_status() -> RuntimeWarmupStatus:
    with _lock:
        return _copy_status_unlocked()


def start_runtime_warmup(
    *,
    agent_config: Any = None,
    runtime: Any = None,
) -> RuntimeWarmupStatus:
    """Register optional warm-up with the lifespan task supervisor."""
    global _cancel_event, _task
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
            return _copy_status_unlocked()
        if runtime is None:
            raise RuntimeError(
                "runtime warm-up requires a lifespan-owned RuntimeContainer"
            )
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
        _cancel_event = threading.Event()
        _task = runtime.supervisor.start_derived(
            _run_warmup(agent_config, _cancel_event, runtime=runtime),
            name="runtime-warmup",
        )
    logger.info("Runtime warm-up started mode=jiuwen-runtime")
    return get_runtime_warmup_status()


async def stop_runtime_warmup(*, deadline: float) -> bool:
    """Cancel warm-up without exceeding the app's absolute shutdown deadline."""
    global _task
    with _lock:
        task = _task
        if task is None:
            return True
        _cancel_event.set()
        task.cancel()

    remaining = max(0.0, float(deadline) - asyncio.get_running_loop().time())
    try:
        await asyncio.wait_for(task, timeout=remaining)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        logger.warning("Runtime warm-up did not finish before shutdown deadline")
        return False
    finally:
        with _lock:
            if _task is task and task.done():
                _task = None
    return task.done()


def _mark_warmup_cancelled() -> None:
    with _lock:
        _status.state = _WARMUP_CANCELLED
        _status.finished_at = _now_iso()
        _status.error_type = None
        _status.error_message = None
    logger.info("Runtime warm-up finished state=cancelled")


def _load_runtime_warmup():
    from agent.core.jiuwen_engine import warm_up_jiuwen_runtime

    return warm_up_jiuwen_runtime


async def _run_warmup(
    agent_config: Any,
    cancel_event: threading.Event,
    *,
    runtime: Any,
) -> None:
    try:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        result = await runtime.short_io.run(
            _load_runtime_warmup(),
            agent_config=agent_config,
            cancel_event=cancel_event,
        )
        if cancel_event.is_set():
            raise asyncio.CancelledError
        with _lock:
            _status.state = _WARMUP_SUCCESS
            _status.finished_at = _now_iso()
            _status.duration_ms = result.get("duration_ms")
            _status.mode = result.get("mode")
        logger.info(
            "Runtime warm-up finished state=success duration_ms=%s",
            result.get("duration_ms"),
        )
    except asyncio.CancelledError:
        _mark_warmup_cancelled()
        raise
    except Exception:
        if cancel_event.is_set():
            _mark_warmup_cancelled()
            return
        with _lock:
            _status.state = _WARMUP_FAILED
            _status.finished_at = _now_iso()
            _status.error_type = None
            _status.error_message = None
        logger.warning("Runtime warm-up finished state=failed")
