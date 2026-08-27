import asyncio

import pytest

from agent.runtime import RunTaskSupervisor


def test_supervisor_tracks_one_live_task_per_run_and_cancels_it():
    async def scenario():
        supervisor = RunTaskSupervisor()
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def run():
            started.set()
            try:
                await asyncio.Future()
            finally:
                finalized.set()

        task = supervisor.start_run("run-alpha", run())
        await asyncio.wait_for(started.wait(), timeout=1)
        assert task.get_name() == "agentic-run-run-alpha"
        assert supervisor.get_run_task("run-alpha") is task
        assert supervisor.active_run_ids == ("run-alpha",)

        duplicate_awaitable = asyncio.get_running_loop().create_future()
        with pytest.raises(ValueError, match="run task already exists"):
            supervisor.start_run("run-alpha", duplicate_awaitable)
        duplicate_awaitable.cancel()

        assert await supervisor.cancel_run("run-alpha") is True
        assert finalized.is_set()
        assert supervisor.get_run_task("run-alpha") is None
        assert supervisor.active_run_ids == ()
        assert await supervisor.cancel_run("run-alpha") is False
        assert await supervisor.close(
            deadline=asyncio.get_running_loop().time() + 0.1
        )

    asyncio.run(scenario())


def test_supervisor_close_cancels_run_and_derived_tasks_and_rejects_new_work():
    async def scenario():
        supervisor = RunTaskSupervisor()
        run_started = asyncio.Event()
        derived_started = asyncio.Event()

        async def wait_forever(started):
            started.set()
            await asyncio.Future()

        supervisor.start_run("run-beta", wait_forever(run_started))
        supervisor.start_derived(
            wait_forever(derived_started),
            name="synthetic-derived-task",
        )
        await asyncio.wait_for(run_started.wait(), timeout=1)
        await asyncio.wait_for(derived_started.wait(), timeout=1)
        assert supervisor.active_run_ids == ("run-beta",)
        assert supervisor.active_derived_count == 1

        assert await supervisor.close(
            deadline=asyncio.get_running_loop().time() + 0.5
        )
        assert supervisor.active_run_ids == ()
        assert supervisor.active_derived_count == 0

        rejected_run = asyncio.get_running_loop().create_future()
        with pytest.raises(RuntimeError, match="supervisor is closed"):
            supervisor.start_run("run-gamma", rejected_run)
        rejected_run.cancel()

        rejected_derived = asyncio.get_running_loop().create_future()
        with pytest.raises(RuntimeError, match="supervisor is closed"):
            supervisor.start_derived(
                rejected_derived,
                name="rejected-derived-task",
            )
        rejected_derived.cancel()

    asyncio.run(scenario())


def test_supervisor_close_reports_an_unfinished_cancellation_before_deadline():
    async def scenario():
        supervisor = RunTaskSupervisor()
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancellation_resistant_run():
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = supervisor.start_run(
            "run-deadline",
            cancellation_resistant_run(),
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        loop = asyncio.get_running_loop()
        assert not await supervisor.close(deadline=loop.time() + 0.01)
        assert supervisor.active_run_ids == ("run-deadline",)

        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert await supervisor.close(deadline=loop.time() + 0.1)
        assert supervisor.active_run_ids == ()

    asyncio.run(scenario())


def test_supervisor_close_from_owned_task_does_not_cancel_or_wait_for_itself():
    async def scenario():
        supervisor = RunTaskSupervisor()
        close_result = asyncio.get_running_loop().create_future()

        async def close_from_derived_task():
            result = await supervisor.close(
                deadline=asyncio.get_running_loop().time() + 0.05
            )
            close_result.set_result(result)

        task = supervisor.start_derived(
            close_from_derived_task(),
            name="synthetic-self-closing-task",
        )

        assert await asyncio.wait_for(close_result, timeout=0.1) is True
        await asyncio.wait_for(task, timeout=0.1)
        assert supervisor.active_derived_count == 0

    asyncio.run(scenario())
