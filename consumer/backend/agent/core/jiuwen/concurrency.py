# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Consumer-owned concurrency scheduling for the openJiuwen adapter."""

from __future__ import annotations

import asyncio
import importlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

from ...tools.scheduling import (
    normalize_parallelism_limit,
    plan_barriered_tool_batches,
)


@dataclass(frozen=True)
class JiuwenToolConcurrencyPolicy:
    concurrency_safe: bool
    max_parallelism: Optional[int] = None


@dataclass(frozen=True)
class JiuwenToolCallIdentity:
    """Keep model correlation separate from Consumer's internal call ID."""

    model_tool_call_id: str
    correlation_token: object


@dataclass(frozen=True)
class _ScheduledJiuwenToolCall:
    tool_call: Any
    policy: JiuwenToolConcurrencyPolicy


_UNBOUND_TOOL_CALL_IDENTITY = object()
_CURRENT_TOOL_CALL_IDENTITY: ContextVar[Any] = ContextVar(
    "jiuwen_current_tool_call_identity",
    default=_UNBOUND_TOOL_CALL_IDENTITY,
)


@contextmanager
def bind_jiuwen_tool_call_identity(tool_call: Any) -> Iterator[JiuwenToolCallIdentity]:
    """Expose one model call's identity to its adapter and callbacks."""

    identity = JiuwenToolCallIdentity(
        model_tool_call_id=_tool_call_id(tool_call),
        correlation_token=object(),
    )
    token = _CURRENT_TOOL_CALL_IDENTITY.set(identity)
    try:
        yield identity
    finally:
        _CURRENT_TOOL_CALL_IDENTITY.reset(token)


def current_jiuwen_tool_call_identity() -> Optional[JiuwenToolCallIdentity]:
    """Return the bound identity, preserving an explicitly empty model ID."""

    value = _CURRENT_TOOL_CALL_IDENTITY.get()
    if value is _UNBOUND_TOOL_CALL_IDENTITY:
        return None
    return value


def current_jiuwen_tool_call_id() -> Optional[str]:
    """Return the bound model ID, or ``None`` outside a scheduled call."""

    identity = current_jiuwen_tool_call_identity()
    return identity.model_tool_call_id if identity is not None else None


class JiuwenConcurrencyAbilityManager:
    """Wrap Jiuwen's manager with Consumer tool-concurrency semantics."""

    def __init__(
        self,
        delegate: Any,
        *,
        max_parallel_workers: int = 10,
        policy_resolver: Optional[Callable[[str], JiuwenToolConcurrencyPolicy]] = None,
    ) -> None:
        self._delegate = delegate
        self._max_parallel_workers = normalize_parallelism_limit(max_parallel_workers)
        self._policy_resolver = policy_resolver or resolve_jiuwen_tool_concurrency_policy

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def configure_parallelism(self, max_parallel_workers: int) -> None:
        self._max_parallel_workers = normalize_parallelism_limit(max_parallel_workers)

    async def execute(
        self,
        ctx: Any,
        tool_call: Any,
        session: Any,
        parallel_tool_calls: Any = True,
        tag: Any = None,
    ) -> List[Any]:
        """Execute one Jiuwen tool-call list with ordered barrier semantics.

        OpenJiuwen 0.1.14 uses the fourth positional argument as ``tag`` while
        later versions expose ``parallel_tool_calls`` before ``tag``. Accept
        both forms and keep delegating each concrete call to Jiuwen's original
        manager so its callbacks, rails, and ToolMessages remain authoritative.
        """

        if tag is None and not isinstance(parallel_tool_calls, bool):
            tag = parallel_tool_calls
            parallel_tool_calls = True

        scheduled_calls = [
            _ScheduledJiuwenToolCall(
                tool_call=item,
                policy=self._policy_resolver(_tool_call_name(item)),
            )
            for item in _normalize_tool_calls(tool_call)
        ]
        batches = plan_barriered_tool_batches(
            scheduled_calls,
            is_concurrency_safe=lambda item: item.policy.concurrency_safe,
            parallel_enabled=bool(parallel_tool_calls),
        )

        results: List[Any] = []
        for batch_index, batch in enumerate(batches):
            if _has_force_finish_request(ctx):
                results.extend(
                    _build_skipped_results(
                        batches[batch_index:],
                        reason="a previous tool terminated the workflow",
                    )
                )
                break
            if batch.parallel:
                batch_results = await self._execute_parallel_batch(
                    list(batch.items),
                    ctx=ctx,
                    session=session,
                    tag=tag,
                )
            else:
                batch_results = await self._execute_one(
                    batch.items[0],
                    ctx=ctx,
                    session=session,
                    tag=tag,
                )
            results.extend(batch_results)
            if _has_force_finish_request(ctx):
                results.extend(
                    _build_skipped_results(
                        batches[batch_index + 1 :],
                        reason="a previous tool terminated the workflow",
                    )
                )
                break
        return results

    async def _execute_parallel_batch(
        self,
        scheduled_calls: List[_ScheduledJiuwenToolCall],
        *,
        ctx: Any,
        session: Any,
        tag: Any,
    ) -> List[Any]:
        global_semaphore = asyncio.Semaphore(self._max_parallel_workers)
        tool_semaphores: Dict[str, asyncio.Semaphore] = {}
        for scheduled_call in scheduled_calls:
            tool_name = _tool_call_name(scheduled_call.tool_call)
            if tool_name in tool_semaphores:
                continue
            tool_semaphores[tool_name] = asyncio.Semaphore(
                normalize_parallelism_limit(
                    scheduled_call.policy.max_parallelism,
                    fallback=self._max_parallel_workers,
                )
            )

        async def execute_safely(
            scheduled_call: _ScheduledJiuwenToolCall,
        ) -> List[Any]:
            tool_semaphore = tool_semaphores[_tool_call_name(scheduled_call.tool_call)]
            # Do not let calls waiting on one tool occupy every global slot.
            async with tool_semaphore:
                async with global_semaphore:
                    return await self._execute_one(
                        scheduled_call,
                        ctx=ctx,
                        session=session,
                        tag=tag,
                    )

        nested_results = await asyncio.gather(*(execute_safely(item) for item in scheduled_calls))
        return [result for call_results in nested_results for result in call_results]

    async def _execute_one(
        self,
        scheduled_call: _ScheduledJiuwenToolCall,
        *,
        ctx: Any,
        session: Any,
        tag: Any,
    ) -> List[Any]:
        with bind_jiuwen_tool_call_identity(scheduled_call.tool_call):
            result = await self._delegate.execute(
                ctx=ctx,
                tool_call=scheduled_call.tool_call,
                session=session,
                tag=tag,
            )
        return list(result or [])


def install_jiuwen_concurrency_ability_manager(
    agent: Any,
    *,
    max_parallel_workers: int = 10,
) -> bool:
    """Install the Consumer scheduler through Jiuwen's public manager field."""

    manager = getattr(agent, "ability_manager", None)
    if manager is None:
        return False
    if isinstance(manager, JiuwenConcurrencyAbilityManager):
        manager.configure_parallelism(max_parallel_workers)
        return True
    agent.ability_manager = JiuwenConcurrencyAbilityManager(
        manager,
        max_parallel_workers=max_parallel_workers,
    )
    return True


def resolve_jiuwen_tool_concurrency_policy(
    tool_name: str,
) -> JiuwenToolConcurrencyPolicy:
    """Resolve Consumer metadata while preserving native Jiuwen behavior."""

    try:
        from ...tools.registry import get_tool_spec

        spec = get_tool_spec(str(tool_name or ""))
    except Exception:
        spec = None
    if spec is None:
        return JiuwenToolConcurrencyPolicy(concurrency_safe=True)
    return JiuwenToolConcurrencyPolicy(
        concurrency_safe=bool(getattr(spec, "concurrency_safe", False)),
        max_parallelism=getattr(spec, "max_parallelism", None),
    )


def _normalize_tool_calls(tool_call: Any) -> List[Any]:
    if tool_call is None:
        return []
    if isinstance(tool_call, (list, tuple)):
        return list(tool_call)
    return [tool_call]


def _tool_call_name(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return str(getattr(tool_call, "name", "") or "")


def _tool_call_id(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _has_force_finish_request(ctx: Any) -> bool:
    value = getattr(ctx, "has_force_finish_request", False)
    return bool(value() if callable(value) else value)


def _build_skipped_results(
    batches: List[Any],
    *,
    reason: str,
) -> List[Any]:
    message_module = importlib.import_module("openjiuwen.core.foundation.llm.schema.message")
    content = json.dumps(
        {
            "status": "skipped",
            "reason": reason,
        },
        ensure_ascii=False,
    )
    results: List[Any] = []
    for batch in batches:
        for scheduled_call in batch.items:
            results.append(
                (
                    None,
                    message_module.ToolMessage(
                        content=content,
                        tool_call_id=_tool_call_id(scheduled_call.tool_call),
                    ),
                )
            )
    return results
