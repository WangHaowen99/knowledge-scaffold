"""Phase-3 tests for async tools and structured internal concurrency."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import agent.tools.executor as tool_executor_module
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter
from agent.exceptions import AgentLoopStalledError
from agent.runtime.async_utils import (
    clear_bounded_offload_pools,
    configure_bounded_offload_pools,
    run_sync_compat,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.sub_agents.base import SubAgent
from agent.sub_agents.manager import SubAgentManager, SubAgentTaskSpec
from agent.tools.base import (
    ToolBatchResult,
    ToolContext,
    ToolInvocation,
    ToolOutcome,
)
from agent.tools.executor import aexecute_tool_batch
from agent.tools.registry import TOOL_SPECS


def _context(
    *,
    run_id: str = "run-async-tools",
    tool_timeout_seconds: float = 600,
) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id=run_id,
        session_id="session-async-tools",
        turn_id=1,
        agent_config=SimpleNamespace(
            parallel_default_max_workers=4,
            sub_agent_max_concurrency=4,
            sub_agent_max_depth=2,
            tool_timeout_seconds=tool_timeout_seconds,
        ),
        caller="main",
    )


def _invocation(name: str, call_id: str) -> ToolInvocation:
    return ToolInvocation(id=call_id, name=name, arguments={})


class _ToolBase:
    schema = {
        "type": "function",
        "function": {
            "name": "phase3_tool",
            "description": "phase 3 test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    allowed_callers = {"main"}
    concurrency_safe = True
    max_parallelism = None
    category = "regular"


class AsyncToolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.offload_pools = [
            BoundedOffloadPool(
                name=name,
                max_workers=8,
                max_pending=32,
                capacity_wait_seconds=1,
                operation_timeout_seconds=3,
            )
            for name in ("test-short", "test-external", "test-tool")
        ]
        configure_bounded_offload_pools(
            short_io=self.offload_pools[0],
            external_io=self.offload_pools[1],
            tool_io=self.offload_pools[2],
        )

    async def asyncTearDown(self):
        clear_bounded_offload_pools()
        deadline = asyncio.get_running_loop().time() + 3
        for pool in self.offload_pools:
            await pool.close(deadline=deadline)

    async def test_native_async_tool_runs_directly_on_the_caller_loop(self):
        observed = {}

        class NativeTool(_ToolBase):
            name = "phase3_native"

            async def aexecute(self, **kwargs):
                del kwargs
                observed["loop"] = asyncio.get_running_loop()
                observed["thread"] = threading.current_thread()
                await asyncio.sleep(0)
                return ToolOutcome(content="native")

            def execute(self, **kwargs):
                del kwargs
                raise AssertionError("native async tools must not use execute()")

        caller_loop = asyncio.get_running_loop()
        with patch.dict(TOOL_SPECS, {NativeTool.name: NativeTool()}):
            result = await aexecute_tool_batch(
                [_invocation(NativeTool.name, "call-native")],
                _context(),
            )

        self.assertEqual(result.messages[0]["content"], "native")
        self.assertIs(observed["loop"], caller_loop)
        self.assertIs(observed["thread"], threading.current_thread())

    async def test_sync_tool_does_not_block_the_event_loop(self):
        started = threading.Event()
        release = threading.Event()
        observed = {}

        class SyncTool(_ToolBase):
            name = "phase3_sync"

            def execute(self, **kwargs):
                del kwargs
                observed["thread"] = threading.current_thread()
                started.set()
                release.wait(timeout=2)
                return ToolOutcome(content="sync")

        with patch.dict(TOOL_SPECS, {SyncTool.name: SyncTool()}):
            task = asyncio.create_task(
                aexecute_tool_batch(
                    [_invocation(SyncTool.name, "call-sync")],
                    _context(),
                )
            )
            await asyncio.wait_for(run_sync_compat(started.wait, 1), timeout=2)
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertIsNot(observed["thread"], threading.current_thread())
            release.set()
            result = await task

        self.assertEqual(result.messages[0]["content"], "sync")

    async def test_parallel_failure_does_not_drop_independent_result(self):
        both_started = asyncio.Event()
        started = 0
        started_lock = asyncio.Lock()

        class SuccessTool(_ToolBase):
            name = "phase3_success"

            async def aexecute(self, **kwargs):
                nonlocal started
                del kwargs
                async with started_lock:
                    started += 1
                    if started == 2:
                        both_started.set()
                await both_started.wait()
                return ToolOutcome(content="success")

        class FailureTool(_ToolBase):
            name = "phase3_failure"

            async def aexecute(self, **kwargs):
                nonlocal started
                del kwargs
                async with started_lock:
                    started += 1
                    if started == 2:
                        both_started.set()
                await both_started.wait()
                raise ValueError("expected failure")

        with patch.dict(
            TOOL_SPECS,
            {
                SuccessTool.name: SuccessTool(),
                FailureTool.name: FailureTool(),
            },
        ):
            result = await aexecute_tool_batch(
                [
                    _invocation(SuccessTool.name, "call-success"),
                    _invocation(FailureTool.name, "call-failure"),
                ],
                _context(),
            )

        self.assertEqual(
            [message["tool_call_id"] for message in result.messages],
            ["call-success", "call-failure"],
        )
        self.assertEqual(result.messages[0]["content"], "success")
        self.assertIn("expected failure", result.messages[1]["content"])

    async def test_cancelling_sync_tool_waits_for_worker_cleanup(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class BlockingTool(_ToolBase):
            name = "phase3_blocking"

            def execute(self, **kwargs):
                del kwargs
                started.set()
                release.wait(timeout=2)
                finished.set()
                return ToolOutcome(content="finished")

        with patch.dict(TOOL_SPECS, {BlockingTool.name: BlockingTool()}):
            task = asyncio.create_task(
                aexecute_tool_batch(
                    [_invocation(BlockingTool.name, "call-blocking")],
                    _context(),
                )
            )
            await asyncio.wait_for(run_sync_compat(started.wait, 1), timeout=2)
            task.cancel()
            async with asyncio.timeout(1):
                while not task.done():
                    await asyncio.sleep(0.005)
            self.assertTrue(task.done())
            self.assertEqual(
                self.offload_pools[2].metrics()["unfinished"],
                1,
            )
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            async with asyncio.timeout(1):
                while self.offload_pools[2].metrics()["unfinished"]:
                    await asyncio.sleep(0.005)

        self.assertTrue(finished.is_set())

    async def test_production_executor_has_no_sync_batch_wrapper(self):
        self.assertFalse(hasattr(tool_executor_module, "execute_tool_batch"))

    async def test_jiuwen_adapter_awaits_async_batch_and_allocates_unique_ids(self):
        started = 0
        both_started = asyncio.Event()
        observed_ids = []

        class AdapterTool(_ToolBase):
            name = "phase3_adapter"

        async def batch_executor(tool_calls, context):
            nonlocal started
            del context
            observed_ids.append(tool_calls[0].id)
            started += 1
            if started == 2:
                both_started.set()
            await both_started.wait()
            call = tool_calls[0]
            return ToolBatchResult(
                messages=[
                    {
                        "role": "tool",
                        "content": call.id,
                        "tool_call_id": call.id,
                        "name": call.name,
                    }
                ]
            )

        state = {
            "run_id": "run-adapter",
            "session_id": "session-adapter",
            "messages": [],
            "results": {},
        }
        with patch.dict(TOOL_SPECS, {AdapterTool.name: AdapterTool()}):
            adapter = CurrentRuntimeToolAdapter(
                tool_name=AdapterTool.name,
                state=state,
                context_factory=_context,
                tool_batch_executor=batch_executor,
            )
            contents = await asyncio.gather(adapter.invoke({}), adapter.invoke({}))

        self.assertEqual(len(set(observed_ids)), 2)
        self.assertCountEqual(contents, observed_ids)
        tool_message_ids = {
            message.get("tool_call_id") for message in state["messages"] if message.get("role") == "tool"
        }
        self.assertEqual(tool_message_ids, set(observed_ids))

    async def test_terminal_tool_keeps_the_existing_serial_barrier(self):
        later_calls = []

        class TerminalTool(_ToolBase):
            name = "phase3_terminal"
            concurrency_safe = False

            async def aexecute(self, **kwargs):
                del kwargs
                return ToolOutcome(content="pause", terminal=True)

        class LaterTool(_ToolBase):
            name = "phase3_later"
            concurrency_safe = False

            async def aexecute(self, **kwargs):
                del kwargs
                later_calls.append(True)
                return ToolOutcome(content="must not run")

        with patch.dict(
            TOOL_SPECS,
            {TerminalTool.name: TerminalTool(), LaterTool.name: LaterTool()},
        ):
            result = await aexecute_tool_batch(
                [
                    _invocation(TerminalTool.name, "call-terminal"),
                    _invocation(LaterTool.name, "call-later"),
                ],
                _context(),
            )

        self.assertTrue(result.terminal)
        self.assertEqual(result.terminal_tool_call_id, "call-terminal")
        self.assertEqual(later_calls, [])
        self.assertEqual(
            json.loads(result.messages[1]["content"])["status"],
            "skipped",
        )

    async def test_tool_timeout_produces_explicit_finished_error(self):
        events = []
        cancelled = asyncio.Event()

        class TimeoutTool(_ToolBase):
            name = "phase3_timeout"

            async def aexecute(self, **kwargs):
                del kwargs
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        context = _context(tool_timeout_seconds=0.01)

        def observe_protocol_event(event_type, payload):
            events.append((event_type, payload))

        context.protocol_event_observer = observe_protocol_event
        with patch.dict(TOOL_SPECS, {TimeoutTool.name: TimeoutTool()}):
            result = await aexecute_tool_batch(
                [_invocation(TimeoutTool.name, "call-timeout")],
                context,
            )

        self.assertTrue(cancelled.is_set())
        self.assertIn("工具执行超时", result.messages[0]["content"])
        finished = [payload for event, payload in events if event == "tool.finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["tool_call_id"], "call-timeout")
        self.assertEqual(finished[0]["status"], "error")
        self.assertEqual(finished[0]["error_code"], "TOOL_TIMEOUT")
        self.assertEqual(finished[0]["timeout_seconds"], 0.01)

    async def test_sync_tool_timeout_returns_before_worker_finishes(self):
        events = []
        started = threading.Event()
        finished = threading.Event()
        release = threading.Event()

        class SlowSyncTool(_ToolBase):
            name = "phase3_sync_timeout"

            def execute(self, **kwargs):
                del kwargs
                started.set()
                release.wait(timeout=2)
                finished.set()
                return ToolOutcome(content="late result")

        context = _context(tool_timeout_seconds=0.01)

        def observe_protocol_event(event_type, payload):
            events.append((event_type, payload))

        context.protocol_event_observer = observe_protocol_event
        with patch.dict(TOOL_SPECS, {SlowSyncTool.name: SlowSyncTool()}):
            try:
                result = await asyncio.wait_for(
                    aexecute_tool_batch(
                        [_invocation(SlowSyncTool.name, "call-sync-timeout")],
                        context,
                    ),
                    timeout=0.5,
                )
                self.assertTrue(started.is_set())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
                await asyncio.wait_for(run_sync_compat(finished.wait), timeout=2)

        self.assertIn("工具执行超时", result.messages[0]["content"])
        finished_events = [payload for event, payload in events if event == "tool.finished"]
        self.assertEqual(finished_events[0]["error_code"], "TOOL_TIMEOUT")
        self.assertEqual(finished_events[0]["timeout_seconds"], 0.01)

    async def test_sync_timeout_worker_registry_is_released_after_worker_finishes(self):
        from agent.tools import executor as executor_module

        started = threading.Event()
        finished = threading.Event()
        release = threading.Event()

        class RegistryTool(_ToolBase):
            name = "phase3_registry_timeout"

            def execute(self, **kwargs):
                del kwargs
                started.set()
                release.wait(timeout=2)
                finished.set()
                return ToolOutcome(content="late result")

        context = _context(tool_timeout_seconds=0.01)
        with patch.dict(TOOL_SPECS, {RegistryTool.name: RegistryTool()}):
            try:
                result = await aexecute_tool_batch(
                    [_invocation(RegistryTool.name, "call-registry-timeout")],
                    context,
                )
                self.assertIn("工具执行超时", result.messages[0]["content"])
                state = executor_module._background_sync_tool_state()
                self.assertEqual(len(state.tasks), 1)
            finally:
                release.set()
                await asyncio.wait_for(run_sync_compat(finished.wait), timeout=2)
                async with asyncio.timeout(1):
                    while executor_module._background_sync_tool_state().tasks:
                        await asyncio.sleep(0.005)

        self.assertEqual(len(executor_module._background_sync_tool_state().tasks), 0)

    async def test_repeated_failure_guard_still_stops_the_tool_loop(self):
        iteration = 0

        def context_factory():
            nonlocal iteration
            iteration += 1
            context = _context()
            context.agent_iteration = iteration
            return context

        async def failing_batch(tool_calls, context):
            del context
            call = tool_calls[0]
            return ToolBatchResult(
                messages=[
                    {
                        "role": "tool",
                        "content": json.dumps({"status": "error", "error": "same failure"}),
                        "tool_call_id": call.id,
                        "name": call.name,
                    }
                ]
            )

        adapter = CurrentRuntimeToolAdapter(
            tool_name="phase3_repeated_failure",
            state={
                "run_id": "run-repeated",
                "session_id": "session-repeated",
                "messages": [],
                "results": {},
            },
            context_factory=context_factory,
            tool_batch_executor=failing_batch,
        )
        await adapter.invoke({"value": 1})
        with self.assertRaises(AgentLoopStalledError):
            await adapter.invoke({"value": 1})


class StructuredSubAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_subagent_native_async_entry_uses_the_caller_loop(self):
        calls = []
        caller_loop = asyncio.get_running_loop()
        agent_config = SimpleNamespace(sub_agent_max_iterations=3)

        class FakeLoopEngine:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            # pylint: disable-next=add-staticmethod-or-classmethod-decorator
            def run(self, state, **kwargs):
                calls.append(("sync", threading.current_thread(), kwargs))
                return {**state, "final_output": "sync-result", "messages": []}

            async def arun(self, state, **kwargs):
                calls.append(("async", asyncio.get_running_loop(), kwargs))
                await asyncio.sleep(0)
                return {**state, "final_output": "async-result", "messages": []}

        common_patches = (
            patch("agent.core.engine.AgentLoopEngine", FakeLoopEngine),
            patch(
                "agent.sub_agents.base.get_llm_router",
                return_value=SimpleNamespace(agent_config=agent_config),
            ),
            patch("agent.sub_agents.base.build_ontologies_section", return_value=""),
            patch("agent.sub_agents.base.build_skills_section", return_value=""),
        )
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3]:
            async_result = await SubAgent().aexecute("async task")

        self.assertEqual(async_result["output"], "async-result")
        self.assertEqual([call[0] for call in calls], ["async"])
        self.assertIs(calls[0][1], caller_loop)
        self.assertEqual(calls[0][2]["caller"], "subagent")

    async def test_subagents_run_in_task_group_and_preserve_input_order(self):
        started = 0
        both_started = asyncio.Event()

        class FakeSubAgent:
            async def aexecute(self, task, context, **kwargs):
                nonlocal started
                del context, kwargs
                started += 1
                if started == 2:
                    both_started.set()
                await both_started.wait()
                return {
                    "output": task,
                    "_success": True,
                    "_key_steps": ["dispatch started", "final result produced"],
                }

        manager = SubAgentManager(max_concurrency=2, max_depth=2)
        tasks = [
            SubAgentTaskSpec(id="first", task="first-result", context={}),
            SubAgentTaskSpec(id="second", task="second-result", context={}),
        ]
        with patch("agent.sub_agents.manager.SubAgent", FakeSubAgent):
            result = await manager.aexecute_group(
                run_id="run-subagents",
                session_id="session-subagents",
                parent_tool_call_id="tool-parent",
                parent_agent_mode="multi_agent",
                parent_model_profile_id=None,
                parent_workspace_id=None,
                event_emitter=None,
                lineage=None,
                tasks=tasks,
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [item["id"] for item in result["tasks"]],
            ["first", "second"],
        )
        self.assertEqual(
            [item["result"]["output"] for item in result["tasks"]],
            ["first-result", "second-result"],
        )

    async def test_parent_cancellation_cascades_to_subagent_tasks(self):
        started = asyncio.Event()
        cancelled = []
        start_count = 0

        class FakeSubAgent:
            async def aexecute(self, task, context, **kwargs):
                nonlocal start_count
                del context, kwargs
                start_count += 1
                if start_count == 2:
                    started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(task)

        manager = SubAgentManager(max_concurrency=2, max_depth=2)
        tasks = [
            SubAgentTaskSpec(id="first", task="first", context={}),
            SubAgentTaskSpec(id="second", task="second", context={}),
        ]
        with patch("agent.sub_agents.manager.SubAgent", FakeSubAgent):
            parent = asyncio.create_task(
                manager.aexecute_group(
                    run_id="run-cancel-subagents",
                    session_id="session-subagents",
                    parent_tool_call_id="tool-parent",
                    parent_agent_mode="multi_agent",
                    parent_model_profile_id=None,
                    parent_workspace_id=None,
                    event_emitter=None,
                    lineage=None,
                    tasks=tasks,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            parent.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await parent

        self.assertCountEqual(cancelled, ["first", "second"])

    async def test_nested_async_batch_reuses_parent_slot_without_deadlock(self):
        current_running = 0
        max_running = 0

        class FakeSubAgent:
            async def aexecute(self, task, context, **kwargs):
                nonlocal current_running, max_running
                del context, kwargs
                current_running += 1
                max_running = max(max_running, current_running)
                await asyncio.sleep(0)
                current_running -= 1
                return {
                    "output": task,
                    "_success": True,
                    "_key_steps": ["dispatch started", "final result produced"],
                }

        manager = SubAgentManager(max_concurrency=1, max_depth=3)
        semaphore = manager._get_async_semaphore()
        await semaphore.acquire()
        tasks = [
            SubAgentTaskSpec(id="first", task="first-result", context={}),
            SubAgentTaskSpec(id="second", task="second-result", context={}),
        ]
        try:
            with patch("agent.sub_agents.manager.SubAgent", FakeSubAgent):
                result = await asyncio.wait_for(
                    manager.aexecute_group(
                        run_id="run-nested-subagents",
                        session_id="session-subagents",
                        parent_tool_call_id="tool-parent",
                        parent_agent_mode="multi_agent",
                        parent_model_profile_id=None,
                        parent_workspace_id=None,
                        event_emitter=None,
                        lineage={
                            "subagent_id": "sub-parent",
                            "depth": 1,
                        },
                        tasks=tasks,
                    ),
                    timeout=2,
                )
        finally:
            semaphore.release()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            [item["id"] for item in result["tasks"]],
            ["first", "second"],
        )
        self.assertEqual(max_running, 1)

    async def test_cancelling_nested_async_batch_restores_reserved_slots(self):
        started = asyncio.Event()
        cancelled = 0

        class FakeSubAgent:
            async def aexecute(self, task, context, **kwargs):
                nonlocal cancelled
                del task, context, kwargs
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled += 1

        manager = SubAgentManager(max_concurrency=3, max_depth=3)
        semaphore = manager._get_async_semaphore()
        await semaphore.acquire()
        tasks = [
            SubAgentTaskSpec(id="first", task="first", context={}),
            SubAgentTaskSpec(id="second", task="second", context={}),
            SubAgentTaskSpec(id="third", task="third", context={}),
        ]
        try:
            with patch("agent.sub_agents.manager.SubAgent", FakeSubAgent):
                group = asyncio.create_task(
                    manager.aexecute_group(
                        run_id="run-cancel-nested-subagents",
                        session_id="session-subagents",
                        parent_tool_call_id="tool-parent",
                        parent_agent_mode="multi_agent",
                        parent_model_profile_id=None,
                        parent_workspace_id=None,
                        event_emitter=None,
                        lineage={"subagent_id": "sub-parent", "depth": 1},
                        tasks=tasks,
                    )
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                group.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await group

            self.assertEqual(semaphore._value, manager.max_concurrency - 1)
            self.assertEqual(cancelled, len(tasks))
        finally:
            semaphore.release()

        self.assertEqual(semaphore._value, manager.max_concurrency)


if __name__ == "__main__":
    unittest.main()
