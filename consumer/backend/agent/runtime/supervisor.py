# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Canonical asyncio Task ownership for runs and derived work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any


class RunTaskSupervisor:
    """Own the one-to-one mapping between live runs and asyncio tasks."""

    def __init__(self) -> None:
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._derived_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def start_run(self, run_id: str, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        if self._closed:
            raise RuntimeError("run task supervisor is closed")
        existing = self._run_tasks.get(run_id)
        if existing is not None and not existing.done():
            raise ValueError(f"run task already exists: {run_id}")
        task = asyncio.create_task(
            awaitable,
            name=f"agentic-run-{run_id}",
        )
        self._run_tasks[run_id] = task
        task.add_done_callback(
            lambda completed, current_run_id=run_id: self._discard_run(
                current_run_id,
                completed,
            )
        )
        return task

    def start_derived(
        self,
        awaitable: Awaitable[Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        if self._closed:
            raise RuntimeError("run task supervisor is closed")
        task = asyncio.create_task(awaitable, name=name)
        self._derived_tasks.add(task)
        task.add_done_callback(self._derived_tasks.discard)
        return task

    def get_run_task(self, run_id: str) -> asyncio.Task[Any] | None:
        task = self._run_tasks.get(run_id)
        if task is None or task.done():
            return None
        return task

    async def cancel_run(self, run_id: str) -> bool:
        task = self.get_run_task(run_id)
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def close(self, *, deadline: float) -> bool:
        self._closed = True
        current = asyncio.current_task()
        tasks = [
            task
            for task in (*self._run_tasks.values(), *self._derived_tasks)
            if not task.done() and task is not current
        ]
        for task in tasks:
            task.cancel()
        if not tasks:
            return True
        timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in done:
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
        return not pending

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(
            run_id
            for run_id, task in self._run_tasks.items()
            if not task.done()
        )

    @property
    def active_derived_count(self) -> int:
        return sum(not task.done() for task in self._derived_tasks)

    def _discard_run(
        self,
        run_id: str,
        completed: asyncio.Task[Any],
    ) -> None:
        if self._run_tasks.get(run_id) is completed:
            self._run_tasks.pop(run_id, None)
