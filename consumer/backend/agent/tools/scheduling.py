"""Shared planning helpers for ordered tool-call execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Iterable, List, TypeVar


_ToolCallT = TypeVar("_ToolCallT")


@dataclass(frozen=True)
class ToolExecutionBatch(Generic[_ToolCallT]):
    """One ordered execution batch.

    Parallel batches contain only concurrency-safe items. Unsafe items are
    always emitted as singleton barrier batches.
    """

    items: tuple[_ToolCallT, ...]
    parallel: bool


def plan_barriered_tool_batches(
    items: Iterable[_ToolCallT],
    *,
    is_concurrency_safe: Callable[[_ToolCallT], bool],
    parallel_enabled: bool = True,
) -> List[ToolExecutionBatch[_ToolCallT]]:
    """Partition ordered calls into safe parallel batches and unsafe barriers."""

    ordered_items = list(items)
    if not parallel_enabled:
        return [
            ToolExecutionBatch(items=(item,), parallel=False)
            for item in ordered_items
        ]

    batches: List[ToolExecutionBatch[_ToolCallT]] = []
    pending_safe_items: List[_ToolCallT] = []

    def flush_safe_items() -> None:
        nonlocal pending_safe_items
        if not pending_safe_items:
            return
        batches.append(
            ToolExecutionBatch(
                items=tuple(pending_safe_items),
                parallel=True,
            )
        )
        pending_safe_items = []

    for item in ordered_items:
        if is_concurrency_safe(item):
            pending_safe_items.append(item)
            continue
        flush_safe_items()
        batches.append(ToolExecutionBatch(items=(item,), parallel=False))

    flush_safe_items()
    return batches


def normalize_parallelism_limit(value: object, *, fallback: object = 1) -> int:
    """Return a positive concurrency limit using the shared fallback rule."""

    try:
        fallback_limit = max(1, int(fallback or 1))
    except (TypeError, ValueError):
        fallback_limit = 1
    if not value:
        return fallback_limit
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return fallback_limit
