"""Small async compatibility primitives shared by runtime adapters."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional, TypeVar


T = TypeVar("T")


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

    def invoke() -> T:
        if run_id:
            try:
                from integration.observability.stack_profiler import profile_current_thread
            except ImportError:
                return func(*args, **kwargs)
            with profile_current_thread(run_id):
                return func(*args, **kwargs)
        return func(*args, **kwargs)

    worker = asyncio.create_task(
        asyncio.to_thread(invoke),
        name=task_name,
    )
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
