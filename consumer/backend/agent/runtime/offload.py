"""Lifespan-owned bounded adapters for unavoidable synchronous I/O."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading
from typing import Any, Callable


_PROFILE_CURRENT_THREAD: Any = None
_PROFILE_CURRENT_THREAD_RESOLVED = False


def _resolve_thread_profiler() -> Any:
    """Resolve the optional profiler before entering an executor thread."""
    global _PROFILE_CURRENT_THREAD, _PROFILE_CURRENT_THREAD_RESOLVED
    if _PROFILE_CURRENT_THREAD_RESOLVED:
        return _PROFILE_CURRENT_THREAD
    try:
        from integration.observability.stack_profiler import profile_current_thread
    except ImportError:
        profile_current_thread = None
    _PROFILE_CURRENT_THREAD = profile_current_thread
    _PROFILE_CURRENT_THREAD_RESOLVED = True
    return _PROFILE_CURRENT_THREAD


class OffloadPoolClosed(RuntimeError):
    """Raised after a pool stops accepting work."""


class OffloadPoolSaturated(RuntimeError):
    """Raised when pending capacity cannot be acquired in time."""


class BoundedOffloadPool:
    """Thread offload with explicit worker, pending, timeout, and shutdown bounds."""

    def __init__(
        self,
        *,
        name: str,
        max_workers: int,
        max_pending: int,
        capacity_wait_seconds: float,
        operation_timeout_seconds: float,
    ) -> None:
        self.name = str(name)
        self.max_workers = max(1, int(max_workers))
        self.max_pending = max(1, int(max_pending))
        self.capacity_wait_seconds = max(0.001, float(capacity_wait_seconds))
        self.operation_timeout_seconds = max(
            0.001,
            float(operation_timeout_seconds),
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"agentic-{self.name}",
        )
        self._pending = asyncio.Semaphore(self.max_pending)
        # Async wrappers can be cancelled while their worker keeps running.
        # Track executor-owned futures so shutdown observes the actual workers.
        self._worker_futures: set[ConcurrentFuture[Any]] = set()
        self._state_lock = threading.Lock()
        self._running = 0
        self._rejected = 0
        self._closed = False
        self._executor_shutdown = False

    def set_operation_timeout_seconds(self, value: float) -> None:
        timeout = float(value)
        if timeout <= 0:
            raise ValueError("operation timeout must be positive")
        self.operation_timeout_seconds = timeout

    async def run(
        self,
        function: Callable[..., Any],
        *args: Any,
        timeout: float | None = None,
        capacity_timeout: float | None = None,
        profile_run_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._state_lock:
            if self._closed:
                raise OffloadPoolClosed(f"{self.name} offload pool is closed")
        try:
            await asyncio.wait_for(
                self._pending.acquire(),
                timeout=(
                    self.capacity_wait_seconds
                    if capacity_timeout is None
                    else max(0.001, float(capacity_timeout))
                ),
            )
        except TimeoutError as exc:
            with self._state_lock:
                self._rejected += 1
            raise OffloadPoolSaturated(
                f"{self.name} offload pending capacity is exhausted"
            ) from exc

        loop = asyncio.get_running_loop()
        context = copy_context()
        profile_current_thread = (
            _resolve_thread_profiler() if profile_run_id else None
        )
        pending_released = threading.Event()
        started = threading.Event()

        def release_pending_once() -> None:
            if pending_released.is_set():
                return
            pending_released.set()
            loop.call_soon_threadsafe(self._pending.release)

        def call() -> Any:
            if profile_current_thread is None:
                return function(*args, **kwargs)
            with profile_current_thread(profile_run_id):
                return function(*args, **kwargs)

        def invoke() -> Any:
            started.set()
            release_pending_once()
            with self._state_lock:
                self._running += 1
            try:
                return context.run(call)
            finally:
                with self._state_lock:
                    self._running -= 1

        try:
            with self._state_lock:
                if self._closed:
                    raise OffloadPoolClosed(
                        f"{self.name} offload pool is closed"
                    )
                worker_future = self._executor.submit(invoke)
                self._worker_futures.add(worker_future)
        except Exception:
            release_pending_once()
            raise

        worker_future.add_done_callback(self._discard_worker_future)
        async_future = asyncio.wrap_future(worker_future)
        effective_timeout = min(
            self.operation_timeout_seconds,
            (
                self.operation_timeout_seconds
                if timeout is None
                else max(0.001, float(timeout))
            ),
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(async_future),
                timeout=effective_timeout,
            )
        except (asyncio.CancelledError, TimeoutError):
            if worker_future.cancel() and not started.is_set():
                release_pending_once()
            async_future.cancel()
            raise
        finally:
            if worker_future.done() and not started.is_set():
                release_pending_once()

    async def close(self, *, deadline: float) -> bool:
        with self._state_lock:
            self._closed = True
            worker_futures = tuple(self._worker_futures)
        for future in worker_futures:
            future.cancel()

        loop = asyncio.get_running_loop()
        remaining = max(0.0, deadline - loop.time())
        waiters = [
            asyncio.wrap_future(future)
            for future in worker_futures
            if not future.done()
        ]
        if waiters and remaining > 0:
            done, pending = await asyncio.wait(waiters, timeout=remaining)
            for waiter in done:
                if waiter.cancelled():
                    continue
                waiter.exception()
            for waiter in pending:
                waiter.cancel()

        with self._state_lock:
            shutdown_executor = not self._executor_shutdown
            self._executor_shutdown = True
        if shutdown_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

        with self._state_lock:
            return not any(
                not future.done()
                for future in self._worker_futures
            )

    def _discard_worker_future(
        self,
        future: ConcurrentFuture[Any],
    ) -> None:
        with self._state_lock:
            self._worker_futures.discard(future)

    def metrics(self) -> dict[str, int | bool]:
        with self._state_lock:
            return {
                "running": self._running,
                "unfinished": sum(
                    not future.done()
                    for future in self._worker_futures
                ),
                "pending": max(
                    0,
                    self.max_pending - int(getattr(self._pending, "_value", 0)),
                ),
                "rejected": self._rejected,
                "closed": self._closed,
            }
