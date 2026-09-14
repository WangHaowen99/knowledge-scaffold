# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Async-call helpers routed through lifespan-owned bounded offload pools."""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, TypeVar


T = TypeVar("T")
OffloadKind = Literal["short", "external", "tool"]


@dataclass(frozen=True)
class _OffloadPools:
    short: Any
    external: Any
    tool: Any


_POOLS_LOCK = threading.RLock()
_POOLS: _OffloadPools | None = None


def configure_bounded_offload_pools(
    *,
    short_io: Any,
    external_io: Any,
    tool_io: Any,
) -> None:
    """Bind the pools owned and closed by the active RuntimeContainer."""

    if any(pool is None for pool in (short_io, external_io, tool_io)):
        raise ValueError("all bounded offload pools are required")
    global _POOLS
    with _POOLS_LOCK:
        _POOLS = _OffloadPools(
            short=short_io,
            external=external_io,
            tool=tool_io,
        )


def clear_bounded_offload_pools() -> None:
    global _POOLS
    with _POOLS_LOCK:
        _POOLS = None


def is_async_callable(func: Callable[..., Any]) -> bool:
    """Return whether a function or callable object has a native async call."""

    if inspect.iscoroutinefunction(func):
        return True
    call = getattr(func, "__call__", None)
    return bool(call and inspect.iscoroutinefunction(call))


def _pool_for(kind: OffloadKind, task_name: str) -> Any:
    selected = kind
    normalized_name = str(task_name or "").lower()
    if selected == "short" and any(
        token in normalized_name
        for token in ("tool", "subagent", "engine", "direct-execution")
    ):
        selected = "tool"
    elif selected == "short" and any(
        token in normalized_name
        for token in ("http", "remote", "external", "network")
    ):
        selected = "external"
    with _POOLS_LOCK:
        pools = _POOLS
    return getattr(pools, selected) if pools is not None else None


async def call_async_or_thread(
    func: Callable[..., T],
    *args: Any,
    run_id: Optional[str] = None,
    task_name: str = "sync-runtime-worker",
    offload_kind: OffloadKind = "short",
    **kwargs: Any,
) -> T:
    """Await native async work or use the selected bounded sync pool."""

    if is_async_callable(func):
        result = func(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result
    return await run_sync_compat(
        func,
        *args,
        run_id=run_id,
        task_name=task_name,
        offload_kind=offload_kind,
        **kwargs,
    )


async def run_sync_compat(
    func: Callable[..., T],
    *args: Any,
    run_id: Optional[str] = None,
    task_name: str = "sync-runtime-worker",
    offload_kind: OffloadKind = "short",
    **kwargs: Any,
) -> T:
    """Run unavoidable sync work without creating a private executor.

    Production startup always binds RuntimeContainer pools.  Direct unit tests
    that intentionally construct a service without an application lifespan
    execute their small fakes inline instead of silently creating an unmanaged
    process resource.
    """

    pool = _pool_for(offload_kind, task_name)
    if pool is None:
        result = func(*args, **kwargs)
    else:
        result = await pool.run(
            func,
            *args,
            profile_run_id=run_id,
            **kwargs,
        )
    return await result if inspect.isawaitable(result) else result


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
