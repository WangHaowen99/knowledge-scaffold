"""Process-scoped assembly for the B-side asynchronous run platform."""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable, Optional

from agent.runtime.loop import get_async_service_loop
from agent.runtime.manager import AsyncRunManager
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.service import AsyncExecutionService, OrchestratorSessionAdapter
from utils.logging import get_logger


_lock = threading.Lock()
_manager: Optional[AsyncRunManager] = None
_orchestrator_provider: Optional[Callable[[], Any]] = None
_manager_orchestrator: Any = None
logger = get_logger("runtime.bootstrap")


def configure_async_run_manager(orchestrator_provider: Callable[[], Any]) -> None:
    global _manager, _manager_orchestrator, _orchestrator_provider
    current_orchestrator = orchestrator_provider()
    previous_manager: Optional[AsyncRunManager] = None
    with _lock:
        if (
            _manager is not None
            and _manager_orchestrator is not current_orchestrator
        ):
            previous_manager = _manager
            _manager = None
            _manager_orchestrator = None
        _orchestrator_provider = orchestrator_provider
    if previous_manager is not None:
        try:
            get_async_service_loop().call(previous_manager.shutdown(), timeout=30.0)
        except Exception:
            logger.exception("Failed to shut down the replaced async run manager")


def get_async_run_manager() -> AsyncRunManager:
    global _manager, _manager_orchestrator
    with _lock:
        if _manager is None:
            if _orchestrator_provider is None:
                raise RuntimeError("异步运行服务尚未配置")
            orchestrator = _orchestrator_provider()
            # Bind one manager generation to one orchestrator generation. A
            # later config refresh can replace and drain it without in-flight
            # finalization switching to the new stores midway through a run.
            bound_provider = lambda: orchestrator
            session_adapter = OrchestratorSessionAdapter(bound_provider)
            runtime = JiuwenAsyncAgentRuntime(
                agent_config=orchestrator.agent_config,
                memory_manager_provider=session_adapter.memory_manager,
                allowed_lifecycle_modes=("legacy",),
            )
            _manager = AsyncRunManager(
                AsyncExecutionService(runtime, session_adapter=session_adapter)
            )
            _manager_orchestrator = orchestrator
        return _manager


def async_manager_matches(orchestrator: Any) -> bool:
    """Avoid routing an independently supplied test/runtime object through a stale manager."""
    with _lock:
        provider = _orchestrator_provider
        manager_orchestrator = _manager_orchestrator
    if manager_orchestrator is not None:
        return manager_orchestrator is orchestrator
    if provider is None:
        return False
    try:
        return provider() is orchestrator
    except Exception:
        return False


def call_async_run(coroutine, *, timeout: float = 30.0):
    return get_async_service_loop().call(coroutine, timeout=timeout)


def submit_async_run(coroutine):
    return get_async_service_loop().submit(coroutine)


def shutdown_async_run_manager(*, timeout: float = 30.0) -> bool:
    """Drain consumer runs before the process-owned Jiuwen Runner stops."""
    global _manager, _manager_orchestrator
    with _lock:
        manager = _manager
        _manager = None
        _manager_orchestrator = None
    if manager is None:
        return False
    get_async_service_loop().call(
        manager.shutdown(),
        timeout=max(0.1, float(timeout)),
    )
    return True


def reset_async_run_manager_for_tests() -> None:
    global _manager, _manager_orchestrator, _orchestrator_provider
    with _lock:
        _manager = None
        _manager_orchestrator = None
        _orchestrator_provider = None


def _shutdown_async_manager_at_exit() -> None:
    try:
        shutdown_async_run_manager()
    except BaseException:
        pass


atexit.register(_shutdown_async_manager_at_exit)
