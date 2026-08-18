"""Small async compatibility primitives shared by runtime adapters."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import inspect
import threading
from contextvars import copy_context
from typing import Any, Callable, Optional, TypeVar


T = TypeVar("T")


_SYNC_EXECUTOR_LOCK = threading.Lock()
_SYNC_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_PROFILE_CURRENT_THREAD: Any = None
_PROFILE_CURRENT_THREAD_RESOLVED = False
_SYNC_RESULT_POLL_SECONDS = 0.005


def _get_sync_runtime_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _SYNC_EXECUTOR
    with _SYNC_EXECUTOR_LOCK:
        if _SYNC_EXECUTOR is None:
            _SYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                thread_name_prefix="consumer-sync-runtime",
            )
        return _SYNC_EXECUTOR


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


def shutdown_sync_runtime_executor(*, wait: bool = True) -> bool:
    """Release the process-owned executor used by synchronous adapters."""
    global _SYNC_EXECUTOR
    with _SYNC_EXECUTOR_LOCK:
        executor = _SYNC_EXECUTOR
        _SYNC_EXECUTOR = None
    if executor is None:
        return False
    executor.shutdown(wait=wait, cancel_futures=False)
    return True


def is_async_callable(func: Callable[..., Any]) -> bool:
    """Return whether a function or callable object has a native async call."""
    if inspect.iscoroutinefunction(func):
        return True
    call = getattr(func, "__call__", None)
    return bool(call and inspect.iscoroutinefunction(call))


async def call_async_or_thread(
    func: Callable[..., T],
    *args: Any,
    run_id: Optional[str] = None,
    task_name: str = "sync-runtime-worker",
    **kwargs: Any,
) -> T:
    """Await native async callables and isolate synchronous ones in a thread."""
    if is_async_callable(func):
        result = func(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    result = await run_sync_compat(
        func,
        *args,
        run_id=run_id,
        task_name=task_name,
        **kwargs,
    )
    return await result if inspect.isawaitable(result) else result


async def run_sync_compat(
    func: Callable[..., T],
    *args: Any,
    run_id: Optional[str] = None,
    task_name: str = "sync-runtime-worker",
    **kwargs: Any,
) -> T:
    """Run sync work without blocking the loop or abandoning it on cancel.

    Python cannot forcibly stop a running thread. Cancellation therefore waits
    for the worker to settle before it is re-raised, preventing the runtime
    from declaring cleanup complete while a tool can still mutate state.
    """

    profile_current_thread = _resolve_thread_profiler() if run_id else None

    def invoke() -> T:
        if profile_current_thread is not None:
            with profile_current_thread(run_id):
                return func(*args, **kwargs)
        return func(*args, **kwargs)

    caller_context = copy_context()
    submitted = _get_sync_runtime_executor().submit(caller_context.run, invoke)

    async def await_submitted() -> T:
        # Some supported interpreter builds can lose the callback used by
        # asyncio.wrap_future/run_in_executor after thread-local imports. Poll
        # the process-owned Future so completion never depends on that bridge.
        while not submitted.done():
            await asyncio.sleep(_SYNC_RESULT_POLL_SECONDS)
        return submitted.result()

    worker = asyncio.create_task(await_submitted(), name=task_name)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if worker.done() and not worker.cancelled():
            worker.exception()
        raise


def find_group_exception(
    exc: BaseException,
    exception_type: type[BaseException],
) -> Optional[BaseException]:
    """Find the first matching exception nested in a TaskGroup error."""
    if isinstance(exc, exception_type):
        return exc
    nested = getattr(exc, "exceptions", None)
    if not isinstance(nested, tuple):
        return None
    for child in nested:
        found = find_group_exception(child, exception_type)
        if found is not None:
            return found
    return None


atexit.register(shutdown_sync_runtime_executor)
