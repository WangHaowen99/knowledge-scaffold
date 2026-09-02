"""Phase-4 tests for real Jiuwen RuntimeEvent and public draft adaptation."""

from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.runtime import (
    JiuwenAsyncAgentRuntime,
    PublicEventAdapter,
    RuntimeEvent,
    RuntimeInvocationContext,
)
from agent.runtime.mock import ListRuntimeEventSink
from agent.runtime.async_utils import run_sync_compat
from agent.runtime.jiuwen import _build_resumable_llm_context
from agent.exceptions import ModelInvocationError, ModelOutputTruncatedError
from agent.runtime.lifecycle import JiuwenRuntimeLifecycleError


def _context(
    run_id: str,
    *,
    include_trace: bool = False,
) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session-runtime",
        turn_id=3,
        workspace_id="workspace-runtime",
        username="alice",
        query="runtime request",
        agent_mode="multi_agent",
        llm_request_mode="stream",
        model_profile_id="accurate",
        include_trace=include_trace,
    )


class _ScriptedEngine:
    def __init__(self, **kwargs):
        del kwargs

    async def arun(self, state, **kwargs):
        observer = kwargs["observer"]
        token_callback = kwargs["token_callback"]
        protocol_event = kwargs["protocol_event_observer"]
        event_writer = kwargs["event_writer"]

        observer.emit(
            SimpleNamespace(
                type="llm.started",
                span_key="llm:1",
                payload={
                    "iteration": 1,
                    "input": {
                        "messages": [{"role": "user", "password": "secret"}]
                    },
                },
            )
        )
        observer.emit(
            SimpleNamespace(
                type="llm.started",
                span_key="llm:1",
                payload={"iteration": 1},
            )
        )
        observer.emit(
            SimpleNamespace(
                type="llm.started",
                span_key="summary:1",
                payload={"iteration": 1, "request_label": "reasoning_summary"},
            )
        )
        protocol_event(
            "tool.started",
            {
                "tool_name": "lookup",
                "tool_call_id": "tool-01",
                "iteration": 1,
                "batch_index": 1,
                "input": {"password": "tool-secret"},
            },
        )
        protocol_event(
            "tool.finished",
            {
                "tool_name": "lookup",
                "tool_call_id": "tool-01",
                "status": "completed",
                "elapsed_ms": 8,
                "output_preview": "Authorization: Bearer tool-token",
                "output_size": 40,
                "output_truncated": False,
            },
        )
        event_writer({
            "event": "agent.v2",
            "data": {
                "type": "tool.finished",
                "source": {"node": "agent_loop", "component": "tool"},
                "payload": {
                    "tool_name": "lookup",
                    "tool_call_id": "tool-01",
                    "status": "completed",
                },
            },
        })
        token_callback("content", "answer ", {"iteration": 1})
        token_callback(
            "reasoning",
            "private reasoning",
            {"iteration": 1, "reasoning_block_id": "reasoning-01"},
        )
        event_writer({
            "event": "agent.v2",
            "data": {
                "type": "reasoning.summary",
                "source": {"node": "agent_loop", "component": "llm"},
                "payload": {
                    "summary": "reasoning summary",
                    "reasoning_block_id": "reasoning-01",
                    "iteration": 1,
                },
            },
        })
        event_writer({
            "event": "agent.v2",
            "data": {
                "type": "iteration.completed",
                "source": {"node": "agent_loop", "component": "iteration"},
                "payload": {
                    "iteration": 1,
                    "status": "final_output",
                    "total_elapsed_ms": 18,
                    "tool_calls_count": 1,
                    "tool_names": ["lookup"],
                },
            },
        })
        return {
            **state,
            "final_output": "answer",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "trace": {
                "prompt": "secret prompt",
                "step": "model",
                "headers": {"Authorization": "Bearer trace-token"},
            },
        }


class JiuwenRuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_error_classification_is_stable_and_provider_neutral(self):
        class HttpError(RuntimeError):
            def __init__(self, status_code):
                super().__init__("provider detail must not become the public contract")
                self.status_code = status_code

        cases = [
            (ModelInvocationError(TimeoutError("slow")), "MODEL_TIMEOUT", "model", True, None),
            (ModelInvocationError(HttpError(408)), "MODEL_TIMEOUT", "model", True, 408),
            (ModelInvocationError(HttpError(429)), "MODEL_RATE_LIMITED", "model", True, 429),
            (ModelInvocationError(HttpError(409)), "MODEL_PROVIDER_ERROR", "model", True, 409),
            (ModelInvocationError(HttpError(401)), "AUTHENTICATION_FAILED", "model", False, 401),
            (ModelInvocationError(HttpError(403)), "AUTHENTICATION_FAILED", "model", False, 403),
            (ModelInvocationError(HttpError(503)), "MODEL_PROVIDER_ERROR", "model", True, 503),
            (ModelInvocationError(HttpError(400)), "MODEL_PROVIDER_ERROR", "model", False, 400),
            (ModelInvocationError(ConnectionError("offline")), "MODEL_CONNECTION_ERROR", "model", True, None),
            (ValueError("internal detail"), "INTERNAL_ERROR", "runtime", False, None),
        ]

        for exc, code, source, retriable, status_code in cases:
            with self.subTest(code=code, status_code=status_code):
                error = JiuwenAsyncAgentRuntime._runtime_error(exc)
                self.assertEqual(error.error_code, code)
                self.assertEqual(error.source, source)
                self.assertEqual(error.retriable, retriable)
                self.assertEqual(error.details.get("http_status"), status_code)
                self.assertNotIn(type(exc).__name__, str(error.details))
                self.assertNotIn("provider detail", error.message)
                self.assertNotIn("internal detail", error.message)

        import httpx

        for exc in (httpx.ConnectError("offline"), httpx.NetworkError("offline")):
            with self.subTest(provider_exception=type(exc).__name__):
                error = JiuwenAsyncAgentRuntime._runtime_error(ModelInvocationError(exc))
                self.assertEqual(error.error_code, "MODEL_CONNECTION_ERROR")
                self.assertEqual(error.source, "model")
                self.assertTrue(error.retriable)

    def test_resumable_context_trims_incomplete_tool_call_suffix(self):
        messages = [
            {"role": "user", "content": "synthetic request"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-complete",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-complete",
                "name": "lookup",
                "content": "synthetic result",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-incomplete",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
        ]

        checkpoint = _build_resumable_llm_context(messages)

        self.assertEqual(
            [message["role"] for message in checkpoint],
            ["user", "assistant", "tool"],
        )
        self.assertEqual(checkpoint[-1]["tool_call_id"], "call-complete")
        self.assertNotIn("call-incomplete", str(checkpoint))

    async def test_retriable_model_failure_returns_recovery_and_valid_history(self):
        class ProviderError(RuntimeError):
            status_code = 503

        class FailingEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del kwargs
                state["messages"] = [
                    {"role": "user", "content": "runtime request"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-complete",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-complete",
                        "name": "lookup",
                        "content": "synthetic result",
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-incomplete",
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                ]
                raise ModelInvocationError(ProviderError("private detail"))

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=FailingEngine,
        )
        sink = ListRuntimeEventSink()

        result = await runtime.arun(_context("run-recoverable"), sink)
        terminal = sink.events[-1]
        draft = PublicEventAdapter().adapt(terminal)

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.error.retriable)
        self.assertEqual(result.recovery.action, "continue")
        self.assertEqual(result.recovery.label, "继续执行")
        self.assertEqual(
            result.recovery.message,
            "Continue the previous task from where it was interrupted, using the "
            "preserved context. Do not repeat steps that have already been completed.",
        )
        self.assertEqual(
            [message.role for message in result.history_messages],
            ["user", "assistant", "tool"],
        )
        self.assertNotIn("call-incomplete", str(result.history_messages))
        self.assertEqual(terminal.payload["recovery"]["action"], "continue")
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.payload["recovery"]["action"], "continue")
        self.assertNotIn("history_messages", draft.payload)

    def test_runtime_error_classification_walks_wrapped_causes(self):
        try:
            try:
                raise TimeoutError("provider timeout")
            except TimeoutError as cause:
                raise ModelInvocationError(cause) from cause
        except ModelInvocationError as wrapped:
            error = JiuwenAsyncAgentRuntime._runtime_error(wrapped)

        self.assertEqual(error.error_code, "MODEL_TIMEOUT")
        self.assertTrue(error.retriable)

        try:
            try:
                raise TimeoutError("provider timeout")
            except TimeoutError as cause:
                raise ModelInvocationError(cause) from cause
        except ModelInvocationError as wrapped_with_code:
            wrapped_error = JiuwenAsyncAgentRuntime._runtime_error(wrapped_with_code)

        self.assertEqual(wrapped_error.error_code, "MODEL_TIMEOUT")
        self.assertEqual(wrapped_error.source, "model")

        self.assertEqual(
            JiuwenAsyncAgentRuntime._runtime_error(TimeoutError("tool timeout")).error_code,
            "INTERNAL_ERROR",
        )

    def test_runtime_error_preserves_explicit_stable_error_code(self):
        class ExplicitToolError(RuntimeError):
            error_code = "TOOL_EXECUTION_FAILED"
            public_message = "Tool execution failed."
            retriable = False

        error = JiuwenAsyncAgentRuntime._runtime_error(ExplicitToolError("private"))

        self.assertEqual(error.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(error.message, "Tool execution failed.")
        self.assertEqual(error.source, "tool")
        self.assertFalse(error.retriable)

        class ExplicitToolTimeoutError(TimeoutError):
            error_code = "TOOL_TIMEOUT"
            public_message = "Tool execution timed out."
            retriable = True

        timeout_error = JiuwenAsyncAgentRuntime._runtime_error(
            ExplicitToolTimeoutError("private tool timeout")
        )
        self.assertEqual(timeout_error.error_code, "TOOL_TIMEOUT")
        self.assertEqual(timeout_error.source, "tool")
        self.assertTrue(timeout_error.retriable)

        class PrivateSdkError(RuntimeError):
            error_code = "SDK_RUNTIME_ERROR"

        private_error = JiuwenAsyncAgentRuntime._runtime_error(
            PrivateSdkError("token=private-provider-detail")
        )
        self.assertEqual(private_error.error_code, "SDK_RUNTIME_ERROR")
        self.assertEqual(private_error.message, "Agent Runtime 执行失败")
        self.assertNotIn("private-provider-detail", private_error.message)

    def test_runtime_passes_history_to_engine_and_keeps_current_query_separate(self):
        context = RuntimeInvocationContext(
            **{
                **_context("run-history").model_dump(),
                "query": "current question",
                "history_messages": [
                    {"role": "user", "content": "previous question"},
                    {
                        "role": "assistant",
                        "content": "previous answer",
                        "reasoning_content": "previous reasoning",
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{"id": "call-01", "name": "lookup"}],
                    },
                    {
                        "role": "tool",
                        "content": "tool result",
                        "tool_call_id": "call-01",
                        "name": "lookup",
                    },
                    {"role": "user", "content": "current question"},
                ],
            }
        )

        state = JiuwenAsyncAgentRuntime._build_engine_state(context)

        self.assertEqual(state["user_request"], "current question")
        self.assertEqual(
            state["messages"],
            [message.model_dump(mode="python", exclude_none=True) for message in context.history_messages],
        )
        self.assertEqual(
            state["messages"][1]["reasoning_content"],
            "previous reasoning",
        )
        self.assertEqual(state["messages"][3]["tool_call_id"], "call-01")

    def test_runtime_injects_structured_interaction_only_into_internal_model_state(self):
        context = RuntimeInvocationContext(
            **{
                **_context("run-interaction-context").model_dump(),
                "query": "继续",
                "interaction_response": {
                    "interaction_id": "interaction_demo",
                    "type": "single_choice",
                    "option_id": "option_a",
                    "value": "value_a",
                },
            }
        )

        state = JiuwenAsyncAgentRuntime._build_engine_state(context)

        self.assertEqual(context.query, "继续")
        self.assertIn("<user-interaction-response>", state["user_request"])
        self.assertIn('"option_id": "option_a"', state["user_request"])
        self.assertNotIn("<user-interaction-response>", context.query)

        history = JiuwenAsyncAgentRuntime._history_from_state({
            **state,
            "messages": [{"role": "user", "content": "继续"}],
        })
        self.assertEqual(history[-1]["role"], "user")
        self.assertIn("<user-interaction-response>", history[-1]["content"])

    async def test_runtime_emits_fixed_event_flow_without_public_identity(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_ScriptedEngine,
        )
        events = [
            event
            async for event in runtime.astream(
                _context("run-events", include_trace=True)
            )
        ]

        self.assertEqual(
            [event.event_type for event in events],
            [
                "turn.started",
                "model.started",
                "tool.started",
                "tool.finished",
                "token.delta",
                "reasoning.delta",
                "reasoning.summary",
                "iteration.completed",
                "turn.finished",
            ],
        )
        self.assertEqual(events[1].payload["model_profile_id"], "accurate")
        reasoning_delta = next(
            event for event in events if event.event_type == "reasoning.delta"
        )
        self.assertEqual(reasoning_delta.payload["iteration"], 1)
        self.assertEqual(
            reasoning_delta.payload["reasoning_block_id"],
            "reasoning-01",
        )
        self.assertNotIn("event_id", events[0].model_dump())
        self.assertNotIn("seq", events[0].model_dump())
        self.assertEqual(events[-1].status, "completed")
        self.assertEqual(
            events[-1].payload["usage"],
            {"input_tokens": 10, "output_tokens": 2},
        )

    async def test_public_drafts_drop_private_fields_and_reasoning_by_default(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_ScriptedEngine,
        )
        runtime_events = [
            event
            async for event in runtime.astream(
                _context("run-public", include_trace=True)
            )
        ]
        adapter = PublicEventAdapter(include_trace=False, max_text_length=64)
        drafts = [draft for event in runtime_events if (draft := adapter.adapt(event))]

        self.assertEqual(
            [draft.event_type for draft in drafts],
            [
                "turn.started",
                "model.started",
                "tool.started",
                "tool.finished",
                "token.delta",
                "iteration.completed",
                "turn.finished",
            ],
        )
        started = next(draft for draft in drafts if draft.event_type == "tool.started")
        self.assertEqual(
            started.payload,
            {
                "name": "lookup",
                "tool_call_id": "tool-01",
                "iteration": 1,
                "batch_index": 1,
                "input_preview": {"password": "[REDACTED]"},
                "omitted": {
                    "text_fields_truncated": 0,
                    "sensitive_fields_redacted": 1,
                },
            },
        )
        finished = next(draft for draft in drafts if draft.event_type == "tool.finished")
        self.assertNotIn("tool-token", str(finished.payload))
        self.assertGreater(finished.payload["output_size"], 0)
        self.assertFalse(finished.payload["output_truncated"])
        self.assertEqual(
            sum(draft.event_type == "tool.finished" for draft in drafts),
            1,
        )
        iteration = next(
            draft for draft in drafts if draft.event_type == "iteration.completed"
        )
        self.assertEqual(iteration.payload["iteration"], 1)
        token = next(draft for draft in drafts if draft.event_type == "token.delta")
        self.assertEqual(token.payload["iteration"], 1)
        self.assertNotIn("reasoning.summary", [draft.event_type for draft in drafts])
        terminal = drafts[-1]
        self.assertNotIn("trace", terminal.payload)
        self.assertIsNone(terminal.event_id)
        self.assertIsNone(terminal.seq)

    async def test_trace_is_structurally_redacted_when_explicitly_enabled(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_ScriptedEngine,
        )
        events = [
            event
            async for event in runtime.astream(
                _context("run-trace", include_trace=True)
            )
        ]
        terminal = events[-1]
        draft = PublicEventAdapter(include_trace=True).adapt(terminal)

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.payload["trace"]["prompt"], "[REDACTED]")
        self.assertEqual(draft.payload["trace"]["headers"], "[REDACTED]")
        self.assertNotIn("trace-token", str(draft.payload))

        summary_event = next(
            event for event in events if event.event_type == "reasoning.summary"
        )
        summary_draft = PublicEventAdapter(include_trace=True).adapt(summary_event)
        self.assertIsNotNone(summary_draft)
        assert summary_draft is not None
        self.assertEqual(summary_draft.payload["summary"], "reasoning summary")
        reasoning_event = next(
            event for event in events if event.event_type == "reasoning.delta"
        )
        reasoning_draft = PublicEventAdapter(include_trace=True).adapt(
            reasoning_event
        )
        self.assertIsNotNone(reasoning_draft)
        assert reasoning_draft is not None
        self.assertEqual(
            reasoning_draft.payload["reasoning_block_id"],
            summary_draft.payload["reasoning_block_id"],
        )

        sink = ListRuntimeEventSink()
        result = await runtime.arun(
            _context("run-trace-result", include_trace=True),
            sink,
        )
        self.assertEqual(result.trace["prompt"], "[REDACTED]")
        self.assertEqual(result.trace["headers"], "[REDACTED]")
        self.assertNotIn("trace-token", str(result.trace))

    async def test_arun_honors_sink_backpressure_and_returns_result(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=_ScriptedEngine,
        )
        sink = ListRuntimeEventSink()
        result = await runtime.arun(_context("run-result"), sink)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.content, "answer")
        self.assertEqual(result.usage, {"input_tokens": 10, "output_tokens": 2})
        self.assertEqual(sink.events[-1].event_type, "turn.finished")

    async def test_truncated_model_output_is_failed_and_preserves_partial_content(self):
        class TruncatedEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del state, kwargs
                raise ModelOutputTruncatedError(
                    "synthetic partial answer",
                    finish_reason="length",
                )

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=TruncatedEngine,
        )
        sink = ListRuntimeEventSink()

        result = await runtime.arun(_context("run-truncated-output"), sink)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.content, "synthetic partial answer")
        self.assertEqual(result.error.error_code, "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(result.error.source, "model")
        self.assertTrue(result.error.retriable)
        terminal = sink.events[-1]
        self.assertEqual(terminal.status, "failed")
        self.assertEqual(terminal.payload["content"], "synthetic partial answer")

    async def test_runtime_result_keeps_long_content_while_sanitizing_secrets(self):
        long_content = "x" * 5000 + " token=content-secret"

        class LongResultEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del kwargs
                return {
                    **state,
                    "final_output": long_content,
                    "structured_content": {
                        "description": "y" * 5000,
                        "github_token": "structured-secret",
                    },
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "sessionToken": "usage-secret",
                    },
                }

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=LongResultEngine,
        )
        result = await runtime.arun(
            _context("run-long-result"),
            ListRuntimeEventSink(),
        )

        self.assertEqual(result.status, "completed")
        self.assertGreater(len(result.content), 4096)
        self.assertTrue(result.content.endswith("token=[REDACTED]"))
        self.assertEqual(len(result.structured_content["description"]), 5000)
        self.assertEqual(result.structured_content["github_token"], "[REDACTED]")
        self.assertEqual(result.usage["input_tokens"], 10)
        self.assertEqual(result.usage["output_tokens"], 20)
        self.assertEqual(result.usage["sessionToken"], "[REDACTED]")

    async def test_cancel_before_producer_starts_finishes_without_hanging(self):
        producer_gate = asyncio.Event()

        class UnreachableEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del state, kwargs
                raise AssertionError("cancelled run must not enter the engine")

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=UnreachableEngine,
        )
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()

        def delaying_task_factory(active_loop, coroutine, context=None):
            code = getattr(coroutine, "cr_code", None)
            if code is not None and code.co_name == "produce":
                original_coroutine = coroutine

                async def delayed_producer():
                    await producer_gate.wait()
                    return await original_coroutine

                coroutine = delayed_producer()
            return asyncio.Task(
                coroutine,
                loop=active_loop,
                context=context,
            )

        loop.set_task_factory(delaying_task_factory)
        task = asyncio.create_task(
            runtime.arun(
                _context("run-cancel-before-start"),
                ListRuntimeEventSink(),
            )
        )
        try:
            for _ in range(100):
                active = runtime._runs.get("run-cancel-before-start")
                if active is not None and active.task is not None:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("runtime producer was not registered")

            self.assertTrue(
                await runtime.cancel("run-cancel-before-start", "early cancel")
            )
            producer_gate.set()
            result = await asyncio.wait_for(task, timeout=2)
        finally:
            producer_gate.set()
            loop.set_task_factory(previous_factory)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error.message, "early cancel")
        self.assertEqual(runtime._runs, {})

    async def test_cancel_returns_false_after_terminal_is_committed(self):
        class ImmediateEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del kwargs
                return {**state, "final_output": "done"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=ImmediateEngine,
        )
        stream = runtime.astream(_context("run-terminal-cancel"))
        while True:
            event = await anext(stream)
            if event.event_type == "turn.finished":
                break

        self.assertFalse(
            await runtime.cancel("run-terminal-cancel", "too late")
        )
        await stream.aclose()

    async def test_injected_engine_owns_dependencies_without_subagent_registry(self):
        observed = {}

        class ManagerAwareEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                observed["manager"] = kwargs["subagent_manager"]
                return {**state, "final_output": "done"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=ManagerAwareEngine,
        )
        result = await runtime.arun(
            _context("run-manager-cleanup"),
            ListRuntimeEventSink(),
        )

        self.assertEqual(result.status, "completed")
        self.assertIsNone(observed["manager"])

    async def test_real_engine_requires_started_async_lifecycle(self):
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
        )
        stopped_lifecycle = SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                state="stopped",
                mode=None,
                owner_loop_id=None,
            )
        )

        with patch(
            "agent.runtime.jiuwen.get_jiuwen_runner_lifecycle",
            return_value=stopped_lifecycle,
        ):
            with self.assertRaisesRegex(
                JiuwenRuntimeLifecycleError,
                "jiuwen_runtime_lifespan",
            ):
                await anext(runtime.astream(_context("run-lifecycle-required")))

    async def test_cancel_cascades_and_returns_one_cancelled_terminal(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class BlockingEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del state, kwargs
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=BlockingEngine,
        )
        sink = ListRuntimeEventSink()
        task = asyncio.create_task(runtime.arun(_context("run-cancel"), sink))
        await asyncio.wait_for(started.wait(), timeout=2)
        accepted = await runtime.cancel("run-cancel", "user requested")
        result = await asyncio.wait_for(task, timeout=2)

        self.assertTrue(accepted)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.error.message, "user requested")
        terminals = [event for event in sink.events if event.event_type == "turn.finished"]
        self.assertEqual(len(terminals), 1)
        self.assertEqual(terminals[0].status, "cancelled")

    async def test_runtime_error_is_sanitized_before_public_adaptation(self):
        class FailingEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del state, kwargs
                raise ValueError("Authorization: Bearer runtime-secret")

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=FailingEngine,
        )
        events = [event async for event in runtime.astream(_context("run-failure"))]
        terminal = events[-1]
        draft = PublicEventAdapter().adapt(terminal)

        self.assertEqual(terminal.status, "failed")
        self.assertNotIn("runtime-secret", str(terminal.payload))
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertNotIn("runtime-secret", str(draft.payload))

    async def test_parallel_tool_completion_uses_arrival_order_and_call_ids(self):
        class ParallelToolsEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                self.assert_runtime_wiring(kwargs)
                protocol_event = kwargs["protocol_event_observer"]

                async def invoke(tool_call_id, delay):
                    protocol_event(
                        "tool.started",
                        {
                            "tool_name": "lookup",
                            "tool_call_id": tool_call_id,
                        },
                    )
                    await asyncio.sleep(delay)
                    protocol_event(
                        "tool.finished",
                        {
                            "tool_name": "lookup",
                            "tool_call_id": tool_call_id,
                            "status": "success",
                            "elapsed_ms": int(delay * 1000),
                        },
                    )

                async with asyncio.TaskGroup() as task_group:
                    task_group.create_task(invoke("tool-slow", 0.02))
                    task_group.create_task(invoke("tool-fast", 0.001))
                return {**state, "final_output": "done"}

            @staticmethod
            def assert_runtime_wiring(kwargs):
                if kwargs["tool_event_emitter"] is not None:
                    raise AssertionError("tool events must have one callback path")

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=ParallelToolsEngine,
        )
        events = [
            event
            async for event in runtime.astream(_context("run-parallel-tools"))
        ]
        tools = [event for event in events if event.event_type.startswith("tool.")]

        self.assertEqual(
            [(event.event_type, event.payload["tool_call_id"]) for event in tools],
            [
                ("tool.started", "tool-slow"),
                ("tool.started", "tool-fast"),
                ("tool.finished", "tool-fast"),
                ("tool.finished", "tool-slow"),
            ],
        )

    async def test_runtime_forwards_business_checkpoint(self):
        class CheckpointEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                kwargs["protocol_event_observer"](
                    "business.checkpoint",
                    {
                        "tool_name": "report_business_checkpoint",
                        "tool_call_id": "checkpoint-call",
                        "stage": "synthetic_stage",
                        "phase": "synthetic_stage.started",
                        "summary": "Synthetic checkpoint payload.",
                    },
                )
                return {**state, "final_output": "done"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=CheckpointEngine,
        )
        events = [
            event
            async for event in runtime.astream(_context("run-business-checkpoint"))
        ]

        self.assertEqual(
            [event.event_type for event in events],
            ["turn.started", "business.checkpoint", "turn.finished"],
        )
        checkpoint = events[1]
        self.assertEqual(checkpoint.status, "running")
        self.assertEqual(checkpoint.payload["stage"], "synthetic_stage")
        draft = PublicEventAdapter().adapt(checkpoint)
        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft.event_type, "business.checkpoint")
        self.assertEqual(draft.payload["tool_call_id"], "checkpoint-call")

    async def test_runtime_forwards_tool_progress_and_subagent_lifecycle(self):
        class ProgressEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                protocol_event = kwargs["protocol_event_observer"]
                protocol_event(
                    "tool.progress",
                    {
                        "tool_name": "lookup",
                        "tool_call_id": "tool-progress",
                        "status": "running",
                        "message": "synthetic progress",
                    },
                )
                protocol_event(
                    "subagent.started",
                    {
                        "subagent_id": "subagent-demo",
                        "task_id": "task-demo",
                        "task": "synthetic task",
                    },
                )
                protocol_event(
                    "subagent.delta",
                    {
                        "subagent_id": "subagent-demo",
                        "task_id": "task-demo",
                        "channel": "content",
                        "text": "synthetic output",
                    },
                )
                protocol_event(
                    "subagent.delta",
                    {
                        "subagent_id": "subagent-demo",
                        "task_id": "task-demo",
                        "channel": "reasoning",
                        "text": "synthetic reasoning",
                    },
                )
                protocol_event(
                    "subagent.finished",
                    {
                        "subagent_id": "subagent-demo",
                        "task_id": "task-demo",
                        "status": "completed",
                        "result": {"ok": True},
                    },
                )
                return {**state, "final_output": "done"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=ProgressEngine,
        )
        events = [
            event
            async for event in runtime.astream(_context("run-progress-events"))
        ]

        self.assertEqual(
            [event.event_type for event in events],
            [
                "turn.started",
                "tool.progress",
                "subagent.started",
                "subagent.delta",
                "subagent.delta",
                "subagent.finished",
                "turn.finished",
            ],
        )
        drafts = [
            draft
            for event in events
            if (draft := PublicEventAdapter(include_trace=False).adapt(event))
        ]
        self.assertEqual(
            [draft.event_type for draft in drafts],
            [
                "turn.started",
                "tool.progress",
                "subagent.started",
                "subagent.delta",
                "subagent.finished",
                "turn.finished",
            ],
        )
        public_subagent_delta = next(
            draft for draft in drafts if draft.event_type == "subagent.delta"
        )
        self.assertEqual(public_subagent_delta.payload["channel"], "content")

    async def test_bounded_buffer_fails_cleanly_without_orphan_producer(self):
        cleaned = asyncio.Event()

        class BurstEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                protocol_event = kwargs["protocol_event_observer"]

                def burst():
                    for index in range(20):
                        protocol_event(
                            "tool.started",
                            {
                                "tool_name": "lookup",
                                "tool_call_id": "tool-{}".format(index),
                            },
                        )

                try:
                    await run_sync_compat(burst)
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return {**state, "final_output": "unreachable"}

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=BurstEngine,
            max_event_buffer_size=1,
        )
        sink = ListRuntimeEventSink()
        result = await asyncio.wait_for(
            runtime.arun(_context("run-buffer-full"), sink),
            timeout=2,
        )

        self.assertTrue(cleaned.is_set())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.error_code, "RUNTIME_EVENT_BUFFER_FULL")
        terminals = [
            event for event in sink.events if event.event_type == "turn.finished"
        ]
        self.assertEqual(len(terminals), 1)

    async def test_threaded_runtime_burst_schedules_only_bounded_loop_callbacks(self):
        from agent.runtime.jiuwen_events import (
            JiuwenRuntimeEventBridge,
            RuntimeEventBufferFull,
        )

        bridge = JiuwenRuntimeEventBridge(
            _context("run-threaded-buffer-bound"),
            max_buffer_size=1,
        )
        scheduled = 0
        scheduled_lock = threading.Lock()
        real_call_soon_threadsafe = bridge._loop.call_soon_threadsafe

        def counted_call_soon_threadsafe(callback, *args):
            nonlocal scheduled
            with scheduled_lock:
                scheduled += 1
            return real_call_soon_threadsafe(callback, *args)

        def burst():
            try:
                for index in range(1000):
                    bridge.protocol_event(
                        "tool.started",
                        {
                            "tool_name": "lookup",
                            "tool_call_id": "tool-{}".format(index),
                        },
                    )
            except RuntimeEventBufferFull:
                pass

        with patch.object(
            bridge._loop,
            "call_soon_threadsafe",
            side_effect=counted_call_soon_threadsafe,
        ):
            worker = threading.Thread(target=burst)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

        await asyncio.sleep(0)
        self.assertLessEqual(scheduled, 2)
        first = await bridge.get()
        terminal = await bridge.get()
        closed = await bridge.get()
        self.assertEqual(first.event_type, "tool.started")
        self.assertEqual(terminal.event_type, "turn.finished")
        self.assertEqual(
            terminal.payload["error"]["error_code"],
            "RUNTIME_EVENT_BUFFER_FULL",
        )
        self.assertIsNone(closed)

    async def test_closing_stream_cancels_and_cleans_producer(self):
        started = asyncio.Event()
        cleaned = asyncio.Event()

        class BlockingEngine:
            def __init__(self, **kwargs):
                del kwargs

            async def arun(self, state, **kwargs):
                del state, kwargs
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=BlockingEngine,
        )
        stream = runtime.astream(_context("run-close-stream"))
        first = await anext(stream)
        self.assertEqual(first.event_type, "turn.started")
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(stream.aclose(), timeout=2)

        self.assertTrue(cleaned.is_set())
        self.assertFalse(await runtime.cancel("run-close-stream", "too late"))

    def test_text_redaction_covers_json_header_assignment_url_and_private_key(self):
        sensitive_text = "\n".join(
            [
                '{"password": "json-secret"}',
                "Authorization: Bearer header-secret",
                "client_secret=assignment-secret",
                "https://example.test/path?access_token=url-secret&safe=1",
                "postgresql://alice:db-secret@example.test/app",
                "-----BEGIN PRIVATE KEY-----private-secret-----END PRIVATE KEY-----",
            ]
        )
        event = RuntimeEvent(
            event_type="turn.finished",
            status="failed",
            run_id="run-redaction-forms",
            payload={
                "status": "failed",
                "error": {
                    "error_code": "FAILED",
                    "message": sensitive_text,
                    "source": "runtime",
                    "details": {},
                },
            },
        )
        draft = PublicEventAdapter(max_text_length=4096).adapt(event)

        self.assertIsNotNone(draft)
        assert draft is not None
        serialized = str(draft.payload)
        for secret in (
            "json-secret",
            "header-secret",
            "assignment-secret",
            "url-secret",
            "db-secret",
            "private-secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_reasoning_summaries_are_removed_even_when_trace_is_enabled(self):
        event = RuntimeEvent(
            event_type="turn.finished",
            status="completed",
            run_id="run-no-summaries",
            payload={
                "status": "completed",
                "content": "answer",
                "trace": {
                    "reasoning_summaries": ["must not be public"],
                    "nested": {"reasoning_summary": "also private"},
                    "step": "model",
                },
            },
        )
        draft = PublicEventAdapter(include_trace=True).adapt(event)

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(
            draft.payload["trace"],
            {"nested": {}, "step": "model"},
        )

    def test_structured_token_secrets_are_redacted_but_usage_counts_remain(self):
        event = RuntimeEvent(
            event_type="turn.finished",
            status="completed",
            run_id="run-token-fields",
            payload={
                "status": "completed",
                "content": "answer",
                "structured_content": {
                    "github_token": "secret-1",
                    "id_token": "secret-2",
                    "sessionToken": "secret-3",
                    "tokens": ["secret-4"],
                    "input_tokens": "secret-5",
                },
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "cached_tokens": "not-a-count-secret",
                },
            },
        )
        draft = PublicEventAdapter().adapt(event)

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(
            draft.payload["structured_content"],
            {
                "github_token": "[REDACTED]",
                "id_token": "[REDACTED]",
                "sessionToken": "[REDACTED]",
                "tokens": "[REDACTED]",
                "input_tokens": "[REDACTED]",
            },
        )
        self.assertEqual(
            draft.payload["usage"],
            {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "cached_tokens": "[REDACTED]",
            },
        )


if __name__ == "__main__":
    unittest.main()
