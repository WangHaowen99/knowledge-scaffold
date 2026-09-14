"""Phase-5 compatibility, load, cleanup, and fixed-flow integration tests."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from agent.runtime import (
    JiuwenAsyncAgentRuntime,
    PublicEvent,
    PublicEventAdapter,
    RuntimeEvent,
    RuntimeInvocationContext,
)
from agent.runtime.mock import FixedAsyncAgentRuntime


def _context(run_id: str, **arguments) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session-{}".format(run_id),
        turn_id=1,
        workspace_id="workspace-regression",
        username="regression-user",
        query="run {}".format(run_id),
        agent_mode="thinking",
        llm_request_mode="stream",
        arguments=arguments or None,
    )


class _PublicEventBusSink:
    """Small line-B contract double: adapt, allocate identity, then persist."""

    def __init__(self, *, include_trace: bool = False) -> None:
        self.adapter = PublicEventAdapter(include_trace=include_trace)
        self.events: list[PublicEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        draft = self.adapter.adapt(event)
        if draft is None:
            return
        seq = len(self.events) + 1
        payload = draft.model_dump(exclude={"event_id", "seq"})
        self.events.append(
            PublicEvent(
                **payload,
                event_id="evt-{:04d}".format(seq),
                seq=seq,
            )
        )
        await asyncio.sleep(0)


class _ConcurrentEngine:
    def __init__(self, **kwargs):
        del kwargs

    async def arun(self, state, **kwargs):
        observer = kwargs["observer"]
        token_callback = kwargs["token_callback"]
        protocol_event = kwargs["protocol_event_observer"]
        run_id = state["run_id"]
        suffix = int(run_id.rsplit("-", 1)[-1])

        observer.emit(
            SimpleNamespace(
                type="llm.started",
                span_key="{}:model".format(run_id),
                payload={"iteration": 1},
            )
        )
        protocol_event(
            "tool.started",
            {
                "tool_name": "lookup",
                "tool_call_id": "{}:tool".format(run_id),
            },
        )
        await asyncio.sleep((suffix % 5) / 1000)
        protocol_event(
            "tool.finished",
            {
                "tool_name": "lookup",
                "tool_call_id": "{}:tool".format(run_id),
                "status": "completed",
                "elapsed_ms": suffix % 5,
            },
        )
        token_callback("content", "answer:{}".format(run_id), {"iteration": 1})
        return {
            **state,
            "final_output": "answer:{}".format(run_id),
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class JiuwenRuntimeRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_runtime_integrates_with_public_event_bus_contract(self):
        runtime = FixedAsyncAgentRuntime()
        sink = _PublicEventBusSink()
        result = await runtime.arun(_context("fixed-flow"), sink)

        self.assertEqual(result.status, "completed")
        self.assertEqual(
            [event.event_type for event in sink.events],
            [
                "turn.started",
                "model.started",
                "tool.started",
                "tool.finished",
                "token.delta",
                "turn.finished",
            ],
        )
        self.assertEqual(
            [event.seq for event in sink.events],
            list(range(1, len(sink.events) + 1)),
        )
        self.assertEqual(
            [event.event_id for event in sink.events],
            ["evt-{:04d}".format(index) for index in range(1, 7)],
        )
        self.assertEqual(sink.events[-1].status, "completed")
        self.assertEqual(runtime.active_run_count(), 0)

    async def test_repeated_mock_runs_do_not_retain_run_state(self):
        runtime = FixedAsyncAgentRuntime()

        for index in range(100):
            sink = _PublicEventBusSink()
            result = await runtime.arun(
                _context("mock-repeat-{}".format(index)),
                sink,
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(runtime.active_run_count(), 0)

    async def test_many_concurrent_native_runs_are_isolated_and_leave_no_tasks(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_ConcurrentEngine,
            max_event_buffer_size=64,
        )

        async def execute(index: int):
            run_id = "native-load-{}".format(index)
            sink = _PublicEventBusSink()
            result = await runtime.arun(_context(run_id), sink)
            return run_id, result, sink.events

        outputs = await asyncio.gather(*(execute(index) for index in range(50)))
        await asyncio.sleep(0)

        self.assertEqual(runtime.active_run_count(), 0)
        pending_runtime_tasks = []
        for task in asyncio.all_tasks():
            is_pending_runtime_task = (
                task is not asyncio.current_task() and not task.done() and task.get_name().startswith("jiuwen-runtime-")
            )
            if is_pending_runtime_task:
                pending_runtime_tasks.append(task)
        self.assertFalse(pending_runtime_tasks)
        for run_id, result, events in outputs:
            with self.subTest(run_id=run_id):
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.content, "answer:{}".format(run_id))
                self.assertEqual(len(events), 6)
                self.assertTrue(all(event.run_id == run_id for event in events))
                self.assertEqual(events[-1].event_type, "turn.finished")
                self.assertEqual(events[-1].status, "completed")
                tool_ids = {
                    event.payload.get("tool_call_id") for event in events if event.event_type.startswith("tool.")
                }
                self.assertEqual(tool_ids, {"{}:tool".format(run_id)})

    async def test_duplicate_active_run_id_is_rejected_without_harming_owner(self):
        started = asyncio.Event()

        class BlockingEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del kwargs
                started.set()
                await asyncio.Event().wait()
                return {**state, "final_output": "unreachable"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=BlockingEngine,
        )
        owner_sink = _PublicEventBusSink()
        owner = asyncio.create_task(runtime.arun(_context("duplicate-run"), owner_sink))
        await asyncio.wait_for(started.wait(), timeout=2)

        with self.assertRaisesRegex(RuntimeError, "already has an active run"):
            await runtime.arun(
                _context("duplicate-run"),
                _PublicEventBusSink(),
            )

        self.assertTrue(await runtime.cancel("duplicate-run", "load test cleanup"))
        result = await asyncio.wait_for(owner, timeout=2)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(runtime.active_run_count(), 0)


if __name__ == "__main__":
    unittest.main()
