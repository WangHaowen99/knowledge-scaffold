# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import asyncio
import threading
from contextvars import ContextVar

import agent.runtime.offload as offload_module
import pytest
from agent.runtime import (
    BoundedOffloadPool,
    OffloadPoolClosed,
    OffloadPoolSaturated,
)


async def _wait_for_metric(
    pool: BoundedOffloadPool,
    key: str,
    value: int,
) -> None:
    async def wait_until_matching():
        while pool.metrics()[key] != value:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait_until_matching(), timeout=1)


def test_offload_pool_bounds_pending_work_and_preserves_event_loop_progress():
    async def scenario():
        pool = BoundedOffloadPool(
            name="synthetic-capacity",
            max_workers=1,
            max_pending=1,
            capacity_wait_seconds=0.02,
            operation_timeout_seconds=1,
        )
        release = threading.Event()
        queued_invoked = threading.Event()

        first = asyncio.create_task(pool.run(release.wait, 0.5))
        await _wait_for_metric(pool, "running", 1)

        def queued_operation():
            queued_invoked.set()
            return "queued-result"

        second = asyncio.create_task(pool.run(queued_operation))
        await _wait_for_metric(pool, "pending", 1)

        probe = asyncio.create_task(asyncio.sleep(0.005))
        with pytest.raises(OffloadPoolSaturated):
            await pool.run(lambda: "rejected", capacity_timeout=0.01)
        await probe
        assert pool.metrics()["rejected"] == 1
        assert not queued_invoked.is_set()

        release.set()
        assert await first is True
        assert await second == "queued-result"
        assert queued_invoked.is_set()
        assert await pool.close(deadline=asyncio.get_running_loop().time() + 0.5)

    asyncio.run(scenario())


def test_offload_timeout_keeps_worker_visible_until_real_completion():
    entered = threading.Event()
    release = threading.Event()

    def blocking_operation():
        entered.set()
        release.wait(timeout=1)
        return "finished"

    async def scenario():
        pool = BoundedOffloadPool(
            name="synthetic-timeout",
            max_workers=1,
            max_pending=1,
            capacity_wait_seconds=0.1,
            operation_timeout_seconds=0.01,
        )
        try:
            with pytest.raises(TimeoutError):
                await pool.run(blocking_operation)
            assert entered.is_set()
            assert pool.metrics()["running"] == 1
            assert pool.metrics()["unfinished"] == 1
            assert not await pool.close(deadline=asyncio.get_running_loop().time() + 0.01)
        finally:
            release.set()
            await _wait_for_metric(pool, "unfinished", 0)
            assert await pool.close(deadline=asyncio.get_running_loop().time() + 0.5)

    asyncio.run(scenario())


def test_offload_close_cancels_queued_worker_without_invoking_it():
    entered = threading.Event()
    release = threading.Event()
    queued_invoked = threading.Event()

    def blocking_operation():
        entered.set()
        release.wait(timeout=1)
        return "finished"

    def queued_operation():
        queued_invoked.set()
        return "queued"

    async def scenario():
        pool = BoundedOffloadPool(
            name="synthetic-shutdown",
            max_workers=1,
            max_pending=1,
            capacity_wait_seconds=0.1,
            operation_timeout_seconds=1,
        )
        running_task = asyncio.create_task(pool.run(blocking_operation))
        queued_task = None
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            queued_task = asyncio.create_task(pool.run(queued_operation))
            await _wait_for_metric(pool, "unfinished", 2)

            assert not await pool.close(deadline=asyncio.get_running_loop().time() + 0.01)
            with pytest.raises(asyncio.CancelledError):
                await queued_task
            assert not queued_invoked.is_set()
            assert pool.metrics()["unfinished"] == 1
        finally:
            release.set()
            assert await running_task == "finished"
            if queued_task is not None and not queued_task.done():
                queued_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued_task
            await _wait_for_metric(pool, "unfinished", 0)
            assert await pool.close(deadline=asyncio.get_running_loop().time() + 0.5)

    asyncio.run(scenario())


def test_offload_pool_propagates_context_and_run_observability(monkeypatch):
    async def scenario():
        marker = ContextVar("synthetic_marker", default="unset")
        profiled_runs = []

        class _ProfileScope:
            def __init__(self, run_id):
                self.run_id = run_id

            def __enter__(self):
                profiled_runs.append((self.run_id, threading.current_thread().name))

            # pylint: disable-next=add-staticmethod-or-classmethod-decorator
            def __exit__(self, exc_type, exc, traceback):
                return False

        monkeypatch.setattr(
            offload_module,
            "_resolve_thread_profiler",
            lambda: _ProfileScope,
        )
        pool = BoundedOffloadPool(
            name="synthetic-context",
            max_workers=1,
            max_pending=1,
            capacity_wait_seconds=0.1,
            operation_timeout_seconds=1,
        )
        context_reset_handle = marker.set("request-context")
        try:
            assert (
                await pool.run(
                    marker.get,
                    profile_run_id="run-observed",
                )
                == "request-context"
            )
        finally:
            marker.reset(context_reset_handle)
        assert profiled_runs == [("run-observed", "agentic-synthetic-context_0")]

        pool.set_operation_timeout_seconds(2)
        assert pool.operation_timeout_seconds == 2
        with pytest.raises(ValueError, match="timeout must be positive"):
            pool.set_operation_timeout_seconds(0)

        assert await pool.close(deadline=asyncio.get_running_loop().time() + 0.5)
        assert pool.metrics()["closed"] is True
        with pytest.raises(OffloadPoolClosed):
            await pool.run(lambda: "rejected")

    asyncio.run(scenario())


def test_run_forwards_business_run_id_keyword():
    async def scenario():
        pool = BoundedOffloadPool(
            name="business-run-id",
            max_workers=1,
            max_pending=1,
            capacity_wait_seconds=0.1,
            operation_timeout_seconds=1,
        )

        def operation(*, run_id: str) -> str:
            return run_id

        try:
            assert await pool.run(operation, run_id="business-run") == "business-run"
        finally:
            assert await pool.close(deadline=asyncio.get_running_loop().time() + 0.5)

    asyncio.run(scenario())
