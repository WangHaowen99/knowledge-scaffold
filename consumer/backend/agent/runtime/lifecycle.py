# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Process-wide openJiuwen Runner lifecycle and event-loop ownership."""

from __future__ import annotations

import asyncio
import importlib
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Literal


JiuwenRuntimeMode = Literal["async"]
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
        if mode != "async":
            raise ValueError("mode must be 'async'")
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
        if transition_lock is None:
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner transition lock is unavailable"
            )

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
            if self._can_rebind_closed_owner(loop):
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
                    raise JiuwenRuntimeLifecycleError(
                        "openJiuwen Runner mode does not match its owner"
                    )
            transition_lock = self._transition_lock
            if transition_lock is None:
                raise JiuwenRuntimeLifecycleError(
                    "openJiuwen Runner transition lock is unavailable"
                )
            return transition_lock

    def _can_rebind_closed_owner(self, loop: asyncio.AbstractEventLoop) -> bool:
        owner_loop = self._owner_loop
        if owner_loop is None:
            return False
        if owner_loop is loop:
            return False
        if self._state != "stopped":
            return False
        return owner_loop.is_closed()

    def _require_owner(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._owner_loop is not loop:
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner is bound to a different event loop"
            )

    def _clear_binding_unlocked(self) -> None:
        self._owner_loop = None
        self._mode = None
        self._transition_lock = None


_JIUWEN_RUNNER_LIFECYCLE = JiuwenRunnerLifecycle()


def get_jiuwen_runner_lifecycle() -> JiuwenRunnerLifecycle:
    return _JIUWEN_RUNNER_LIFECYCLE


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
