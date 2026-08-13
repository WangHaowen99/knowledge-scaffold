"""Process-wide openJiuwen Runner lifecycle and event-loop ownership."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import importlib
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Literal


JiuwenRuntimeMode = Literal["legacy", "async"]
JiuwenRuntimeState = Literal["stopped", "starting", "started", "stopping"]


class JiuwenRuntimeLifecycleError(RuntimeError):
    """Raised when one process tries to mix incompatible Jiuwen loops."""


@dataclass(frozen=True)
class JiuwenRunnerLifecycleSnapshot:
    state: JiuwenRuntimeState
    mode: JiuwenRuntimeMode | None
    owner_loop_id: int | None
    start_count: int
    stop_count: int


def _load_openjiuwen_runner() -> Any:
    module = importlib.import_module("openjiuwen.core.runner")
    return module.Runner


class JiuwenRunnerLifecycle:
    """Idempotently start one Runner on exactly one event loop."""

    def __init__(self, *, runner_loader: Callable[[], Any] | None = None) -> None:
        self._runner_loader = runner_loader or _load_openjiuwen_runner
        self._guard = threading.RLock()
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._mode: JiuwenRuntimeMode | None = None
        self._transition_lock: asyncio.Lock | None = None
        self._state: JiuwenRuntimeState = "stopped"
        self._start_count = 0
        self._stop_count = 0

    def snapshot(self) -> JiuwenRunnerLifecycleSnapshot:
        with self._guard:
            return JiuwenRunnerLifecycleSnapshot(
                state=self._state,
                mode=self._mode,
                owner_loop_id=id(self._owner_loop) if self._owner_loop is not None else None,
                start_count=self._start_count,
                stop_count=self._stop_count,
            )

    async def start(self, *, mode: JiuwenRuntimeMode) -> bool:
        """Start Runner once and bind it to the current event loop and mode."""
        if mode not in {"legacy", "async"}:
            raise ValueError("mode must be 'legacy' or 'async'")
        loop = asyncio.get_running_loop()
        transition_lock = self._bind(loop, mode)
        async with transition_lock:
            with self._guard:
                if self._state == "started":
                    return False
                self._state = "starting"
            try:
                result = self._runner_loader().start()
                if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                    await result
            except BaseException:
                with self._guard:
                    self._state = "stopped"
                raise
            with self._guard:
                self._state = "started"
                self._start_count += 1
            return True

    async def stop(self) -> bool:
        """Stop Runner once; it must be stopped by its owning event loop."""
        loop = asyncio.get_running_loop()
        with self._guard:
            if self._owner_loop is None:
                return False
            self._require_owner(loop)
            transition_lock = self._transition_lock
        assert transition_lock is not None

        async with transition_lock:
            with self._guard:
                if self._state == "stopped":
                    self._clear_binding_unlocked()
                    return False
                self._state = "stopping"
            try:
                result = self._runner_loader().stop()
                if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                    await result
            except BaseException:
                with self._guard:
                    self._state = "started"
                raise
            with self._guard:
                self._state = "stopped"
                self._stop_count += 1
                self._clear_binding_unlocked()
            return True

    async def release(self, jiuwen_session_id: str) -> None:
        """Release one Jiuwen session on the Runner-owning event loop."""
        session_id = str(jiuwen_session_id or "").strip()
        if not session_id:
            raise ValueError("jiuwen_session_id must not be empty")
        loop = asyncio.get_running_loop()
        with self._guard:
            self._require_owner(loop)
            if self._state != "started":
                raise JiuwenRuntimeLifecycleError("openJiuwen Runner is not started")
        result = self._runner_loader().release(session_id)
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            await result

    def _bind(
        self,
        loop: asyncio.AbstractEventLoop,
        mode: JiuwenRuntimeMode,
    ) -> asyncio.Lock:
        with self._guard:
            if self._state == "stopping":
                raise JiuwenRuntimeLifecycleError("openJiuwen Runner is stopping")
            if (
                self._owner_loop is not None
                and self._owner_loop is not loop
                and self._state == "stopped"
                and self._owner_loop.is_closed()
            ):
                # A failed startup can leave the binding attached to the loop
                # that attempted it. Once that loop is closed there can be no
                # queued transition using its lock, so a later app startup may
                # safely establish a new owner.
                self._clear_binding_unlocked()
            if self._owner_loop is None:
                self._owner_loop = loop
                self._mode = mode
                self._transition_lock = asyncio.Lock()
            else:
                self._require_owner(loop)
                if self._mode != mode:
                    message = (
                        "openJiuwen Runner already uses {!r} mode; "
                        "{!r} mode must run in a separate process"
                    )
                    raise JiuwenRuntimeLifecycleError(
                        message.format(self._mode, mode)
                    )
            assert self._transition_lock is not None
            return self._transition_lock

    def _require_owner(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._owner_loop is not loop:
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner is bound to a different event loop"
            )

    def _clear_binding_unlocked(self) -> None:
        self._owner_loop = None
        self._mode = None
        self._transition_lock = None


class LegacyJiuwenLoopRunner:
    """Run the legacy synchronous bridge on one owned background loop."""

    def __init__(
        self,
        *,
        lifecycle: JiuwenRunnerLifecycle,
        startup_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
    ) -> None:
        self._lifecycle = lifecycle
        self._startup_timeout = max(0.1, float(startup_timeout))
        self._shutdown_timeout = max(0.1, float(shutdown_timeout))
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stopping = False

    def get_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._stopping:
                raise JiuwenRuntimeLifecycleError("legacy Jiuwen loop is stopping")
            if self._loop is not None and self._loop.is_running():
                return self._loop
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._startup_error = None
                self._loop = None
                self._thread = threading.Thread(
                    target=self._run_forever,
                    name="jiuwen-loop",
                    daemon=True,
                )
            thread = self._thread
            if not thread.is_alive():
                thread.start()

        if not self._ready.wait(timeout=self._startup_timeout):
            self.shutdown()
            raise JiuwenRuntimeLifecycleError(
                "Jiuwen background event loop failed to start"
            )
        if self._startup_error is not None:
            error = self._startup_error
            if thread is not threading.current_thread():
                thread.join(timeout=self._shutdown_timeout)
            with self._lock:
                self._loop = None
                self._thread = None
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner failed to start on the legacy loop"
            ) from error
        loop = self._loop
        if loop is None or not loop.is_running():
            raise JiuwenRuntimeLifecycleError("Jiuwen background event loop is not running")
        return loop

    def submit(self, awaitable: Any) -> "concurrent.futures.Future[Any]":
        try:
            loop = self.get_loop()
        except BaseException:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise
        return asyncio.run_coroutine_threadsafe(awaitable, loop)

    def shutdown(self) -> bool:
        """Stop Runner before stopping and joining the legacy loop thread."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            if thread is None:
                return False
            if self._stopping:
                return False
            if thread is threading.current_thread():
                raise JiuwenRuntimeLifecycleError(
                    "legacy Jiuwen loop cannot synchronously shut itself down"
                )
            self._stopping = True

        shutdown_complete = False
        try:
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self._lifecycle.stop(), loop)
                try:
                    future.result(timeout=self._shutdown_timeout)
                finally:
                    loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=self._shutdown_timeout)
            if thread.is_alive():
                raise JiuwenRuntimeLifecycleError(
                    "legacy Jiuwen loop did not stop before the shutdown timeout"
                )
            shutdown_complete = True
            return True
        finally:
            with self._lock:
                if shutdown_complete:
                    self._loop = None
                    self._thread = None
                self._stopping = False

    def _run_forever(self) -> None:
        loop = asyncio.new_event_loop()
        with self._lock:
            self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._lifecycle.start(mode="legacy"))
        except BaseException as exc:
            self._startup_error = exc
            try:
                loop.run_until_complete(self._lifecycle.stop())
            except BaseException:
                pass
            self._ready.set()
            loop.close()
            return

        if self._stopping:
            loop.run_until_complete(self._lifecycle.stop())
            self._ready.set()
            loop.close()
            return

        def keep_cross_thread_wakeup_bounded() -> None:
            # Normal submissions wake the selector immediately. This low-rate
            # fallback bounds shutdown/submission latency on runtimes where a
            # cross-thread selector wakeup is delayed or coalesced.
            loop.call_later(0.1, keep_cross_thread_wakeup_bounded)

        try:
            loop.call_soon(keep_cross_thread_wakeup_bounded)
            loop.call_soon(self._ready.set)
            loop.run_forever()
        finally:
            try:
                if self._lifecycle.snapshot().state != "stopped":
                    loop.run_until_complete(self._lifecycle.stop())
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()


_JIUWEN_RUNNER_LIFECYCLE = JiuwenRunnerLifecycle()
_LEGACY_JIUWEN_LOOP_RUNNER = LegacyJiuwenLoopRunner(
    lifecycle=_JIUWEN_RUNNER_LIFECYCLE
)


def get_jiuwen_runner_lifecycle() -> JiuwenRunnerLifecycle:
    return _JIUWEN_RUNNER_LIFECYCLE


def get_legacy_jiuwen_loop_runner() -> LegacyJiuwenLoopRunner:
    return _LEGACY_JIUWEN_LOOP_RUNNER


@asynccontextmanager
async def jiuwen_runtime_lifespan() -> AsyncIterator[JiuwenRunnerLifecycle]:
    """Own the Runner on the current ASGI event loop for one app lifespan."""
    lifecycle = get_jiuwen_runner_lifecycle()
    started_here = await lifecycle.start(mode="async")
    try:
        yield lifecycle
    finally:
        if started_here:
            await lifecycle.stop()


def shutdown_legacy_jiuwen_runtime() -> bool:
    return get_legacy_jiuwen_loop_runner().shutdown()


def _shutdown_legacy_at_exit() -> None:
    try:
        shutdown_legacy_jiuwen_runtime()
    except BaseException:
        pass


atexit.register(_shutdown_legacy_at_exit)
