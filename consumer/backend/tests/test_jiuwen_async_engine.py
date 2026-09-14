"""Phase-2 tests for the native async Jiuwen Engine entry points."""

from __future__ import annotations

from internal_access import internal
import asyncio
import threading
import unittest
from contextlib import ExitStack, contextmanager
from types import MethodType, SimpleNamespace
from unittest.mock import patch

import agent.core.jiuwen_engine as jiuwen_engine
from agent.core.jiuwen_engine import (
    JiuwenEngineEventBufferFull,
    JiuwenReactLoopEngine,
)
from agent.runtime.async_utils import run_sync_compat


def _agent_config():
    return SimpleNamespace(
        main_agent_max_iterations=1,
        sub_agent_max_iterations=1,
        context_max_rounds=10,
        web_search=SimpleNamespace(enabled=False),
        limits=SimpleNamespace(model_output_token_limit=4096),
    )


def _llm_config():
    return SimpleNamespace(
        provider="openai",
        model="offline",
        api_key="",
        base_url="http://127.0.0.1/unused",
        timeout=1,
        temperature=0,
        max_tokens=64,
    )


def _state(run_id: str, *, agent_mode: str = "thinking"):
    return {
        "run_id": run_id,
        "session_id": "business-session",
        "current_turn_id": 1,
        "user_request": "offline request",
        "messages": [],
        "results": {},
        "user_context": {
            "agent_mode": agent_mode,
            "llm_request_mode": "sync",
        },
    }


@contextmanager
def _patched_engine(
    create_agent,
    dispose,
    *,
    register_tools=None,
    unregister_tools=None,
):
    with ExitStack() as stack:
        stack.enter_context(patch.object(jiuwen_engine, "is_openjiuwen_available", return_value=True))
        stack.enter_context(patch.object(jiuwen_engine, "_resolve_jiuwen_llm_config", return_value=_llm_config()))
        stack.enter_context(patch.object(jiuwen_engine, "create_default_jiuwen_agent", side_effect=create_agent))
        stack.enter_context(
            patch.object(
                jiuwen_engine,
                "register_current_runtime_tools",
                side_effect=register_tools or (lambda *_args, **_kwargs: []),
            )
        )
        stack.enter_context(
            patch.object(
                jiuwen_engine,
                "unregister_current_runtime_tools",
                side_effect=unregister_tools or (lambda *_args, **_kwargs: None),
            )
        )
        stack.enter_context(patch.object(jiuwen_engine, "_dispose_owned_jiuwen_runtime", dispose))
        stack.enter_context(patch.object(JiuwenReactLoopEngine, "_tools_schema_for_state", return_value=[]))
        stack.enter_context(patch.object(JiuwenReactLoopEngine, "_jiuwen_tool_infos_for_state", return_value=[]))
        stack.enter_context(
            patch.object(
                JiuwenReactLoopEngine,
                "_hydrate_selected_ontology_read_documents",
                return_value=None,
            )
        )
        yield


class JiuwenAsyncEngineTests(unittest.TestCase):
    def test_runtime_history_source_drops_only_the_duplicate_current_user(self):
        state = {
            **_state("run-history-source"),
            "user_request": "current question",
            "messages": [
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

        history = internal(jiuwen_engine).build_jiuwen_history_source_messages(state)

        self.assertEqual(len(history), 4)
        self.assertEqual(history[1]["reasoning_content"], "previous reasoning")
        self.assertEqual(history[2]["tool_calls"][0]["id"], "call-01")
        self.assertEqual(history[3]["tool_call_id"], "call-01")
        self.assertNotEqual(history[-1].get("role"), "user")

    def test_arun_executes_on_the_callers_loop(self):
        observed = {}
        disposed = []

        class OfflineAgent:
            card = SimpleNamespace(id="async-direct-agent")

            async def invoke(self, inputs, session=None):
                del inputs, session
                observed["loop"] = asyncio.get_running_loop()
                observed["thread"] = threading.current_thread().name
                await asyncio.sleep(0)
                return {"output": "async answer", "result_type": "answer"}

        async def dispose(agent, *, session_id):
            disposed.append((agent.card.id, session_id))

        with _patched_engine(lambda **_kwargs: OfflineAgent(), dispose):
            engine = JiuwenReactLoopEngine(agent_config=_agent_config())
            state = _state("run-async-direct")

            async def exercise():
                caller_loop = asyncio.get_running_loop()
                result = await engine.arun(state, caller="main", system_prompt="offline")
                return caller_loop, result

            caller_loop, result = asyncio.run(exercise())

        self.assertIs(result, state)
        self.assertEqual(state["final_output"], "async answer")
        self.assertIs(observed["loop"], caller_loop)
        self.assertEqual(observed["thread"], threading.current_thread().name)
        self.assertEqual(len(disposed), 1)

    def test_repeated_arun_modes_are_equivalent(self):
        created = []
        create_options = []

        class OfflineAgent:
            def __init__(self, agent_id):
                self.card = SimpleNamespace(id=agent_id)

            async def invoke(self, inputs, session=None):
                del session
                return {
                    "output": "answer:{}".format(inputs["query"].splitlines()[0]),
                    "result_type": "answer",
                }

        def create_agent(**kwargs):
            agent = OfflineAgent("equivalent-agent-{}".format(len(created) + 1))
            created.append(agent)
            create_options.append(dict(kwargs))
            return agent

        async def dispose(agent, *, session_id):
            del agent, session_id

        with _patched_engine(create_agent, dispose):
            engine = JiuwenReactLoopEngine(agent_config=_agent_config())
            for mode in ("quick", "thinking", "multi_agent"):

                async def exercise(selected_mode=mode):
                    first = await engine.arun(
                        _state("run-first-{}".format(selected_mode), agent_mode=selected_mode),
                        caller="main",
                        system_prompt="offline",
                    )
                    second = await engine.arun(
                        _state("run-second-{}".format(selected_mode), agent_mode=selected_mode),
                        caller="main",
                        system_prompt="offline",
                    )
                    return first, second

                first_result, second_result = asyncio.run(exercise())
                with self.subTest(agent_mode=mode):
                    self.assertEqual(
                        first_result["final_output"],
                        second_result["final_output"],
                    )
                    self.assertEqual(
                        first_result["messages"],
                        second_result["messages"],
                    )
                    self.assertEqual(
                        first_result["results"],
                        second_result["results"],
                    )

        reasoning_flags = [options["enable_reasoning"] for options in create_options]
        self.assertEqual(reasoning_flags, [False, False, True, True, True, True])

    def test_astream_yields_events_from_the_same_arun_core(self):
        class OfflineAgent:
            card = SimpleNamespace(id="astream-agent")

            async def invoke(self, inputs, session=None):
                del inputs, session
                await asyncio.sleep(0)
                return {"output": "streamed answer", "result_type": "answer"}

        async def dispose(agent, *, session_id):
            del agent, session_id

        with _patched_engine(lambda **_kwargs: OfflineAgent(), dispose):
            engine = JiuwenReactLoopEngine(agent_config=_agent_config())
            state = _state("run-astream")
            mirrored_events = []

            async def exercise():
                event_stream = engine.astream(
                    state,
                    caller="main",
                    system_prompt="offline",
                    event_writer=mirrored_events.append,
                )
                return [event async for event in event_stream]

            events = asyncio.run(exercise())

        self.assertTrue(events)
        self.assertEqual(events, mirrored_events)
        self.assertEqual(state["final_output"], "streamed answer")

    def test_astream_aclose_cancels_producer_without_exception_group(self):
        engine = object.__new__(JiuwenReactLoopEngine)

        async def exercise():
            cleaned = asyncio.Event()

            async def fake_arun(_self, state, *, event_writer=None, **kwargs):
                del kwargs
                assert event_writer is not None
                event_writer({"type": "started"})
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return state

            engine.arun = MethodType(fake_arun, engine)
            stream = engine.astream(_state("run-astream-close"), caller="main")
            self.assertEqual(await anext(stream), {"type": "started"})
            await asyncio.wait_for(stream.aclose(), timeout=2)
            self.assertTrue(cleaned.is_set())

        asyncio.run(exercise())

    def test_astream_bounded_buffer_cancels_threaded_producer(self):
        engine = object.__new__(JiuwenReactLoopEngine)

        async def exercise():
            cleaned = asyncio.Event()

            async def fake_arun(_self, state, *, event_writer=None, **kwargs):
                del kwargs
                assert event_writer is not None

                def burst():
                    for index in range(20):
                        event_writer({"index": index})

                try:
                    await run_sync_compat(burst)
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                return state

            engine.arun = MethodType(fake_arun, engine)
            events = []
            with self.assertRaisesRegex(
                JiuwenEngineEventBufferFull,
                "max_buffer_size=1",
            ):
                async for event in engine.astream(
                    _state("run-astream-overflow"),
                    caller="main",
                    max_event_buffer_size=1,
                ):
                    events.append(event)
                    await asyncio.sleep(0)

            self.assertTrue(events)
            self.assertEqual(events[0], {"index": 0})
            self.assertLess(len(events), 20)
            self.assertTrue(cleaned.is_set())

        asyncio.run(exercise())

    def test_engine_threaded_burst_schedules_only_bounded_callbacks(self):
        from agent.core.jiuwen_engine import _JiuwenEngineEventBuffer

        async def exercise():
            buffer = _JiuwenEngineEventBuffer(max_buffer_size=1)
            scheduled = 0
            scheduled_lock = threading.Lock()
            real_call_soon_threadsafe = internal(buffer).loop.call_soon_threadsafe

            def counted_call_soon_threadsafe(callback, *args):
                nonlocal scheduled
                with scheduled_lock:
                    scheduled += 1
                return real_call_soon_threadsafe(callback, *args)

            def burst():
                for index in range(1000):
                    try:
                        buffer.publish({"index": index})
                    except JiuwenEngineEventBufferFull:
                        continue

            with patch.object(
                internal(buffer).loop,
                "call_soon_threadsafe",
                side_effect=counted_call_soon_threadsafe,
            ):
                worker = threading.Thread(target=burst)
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())

            await asyncio.sleep(0)
            self.assertLessEqual(scheduled, 2)
            self.assertEqual(await buffer.get(), {"index": 0})
            self.assertIsNone(await buffer.get())

        asyncio.run(exercise())

    def test_arun_cancellation_runs_owned_runtime_cleanup(self):
        disposed = []

        class SlowAgent:
            card = SimpleNamespace(id="async-cancel-agent")

            def __init__(self):
                self.started = asyncio.Event()

            async def invoke(self, inputs, session=None):
                del inputs, session
                self.started.set()
                await asyncio.Event().wait()

        agent = SlowAgent()

        async def dispose(owned_agent, *, session_id):
            disposed.append((owned_agent.card.id, session_id))

        with _patched_engine(lambda **_kwargs: agent, dispose):
            engine = JiuwenReactLoopEngine(agent_config=_agent_config())

            async def exercise():
                task = asyncio.create_task(
                    engine.arun(
                        _state("run-async-cancel"),
                        caller="main",
                        system_prompt="offline",
                    )
                )
                await asyncio.wait_for(agent.started.wait(), timeout=2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(exercise())

        self.assertEqual(len(disposed), 1)
        self.assertEqual(disposed[0][0], "async-cancel-agent")

    def test_repeated_runs_release_callbacks_tool_cards_and_sessions(self):
        callback_event_class = SimpleNamespace(
            BEFORE_MODEL_CALL="before_model",
            AFTER_MODEL_CALL="after_model",
            ON_MODEL_EXCEPTION="model_exception",
            AFTER_TOOL_CALL="after_tool",
        )
        managers = []
        registered_cards = []
        unregistered_cards = []
        disposed_sessions = []

        class CallbackManager:
            def __init__(self):
                self.callbacks = []

            def register_callback(self, event, callback, priority=0):
                self.callbacks.append((event, callback, priority))

            def unregister(self, event, callback):
                self.callbacks = [item for item in self.callbacks if item[:2] != (event, callback)]

        class OfflineAgent:
            def __init__(self, index):
                self.card = SimpleNamespace(id="resource-agent-{}".format(index))
                self.agent_callback_manager = CallbackManager()
                managers.append(self.agent_callback_manager)

            async def invoke(self, inputs, session=None):
                del inputs, session
                return {"output": "ok", "result_type": "answer"}

        def create_agent(**_kwargs):
            return OfflineAgent(len(managers) + 1)

        def register_tools(agent, **_kwargs):
            cards = ["card:{}".format(agent.card.id)]
            registered_cards.extend(cards)
            return cards

        def unregister_tools(agent, cards, **_kwargs):
            del agent
            unregistered_cards.extend(cards)

        async def dispose(agent, *, session_id):
            disposed_sessions.append((agent.card.id, session_id))

        with _patched_engine(
            create_agent,
            dispose,
            register_tools=register_tools,
            unregister_tools=unregister_tools,
        ), patch.object(
            jiuwen_engine,
            "_jiuwen_agent_callback_event_class",
            return_value=callback_event_class,
        ):
            engine = JiuwenReactLoopEngine(agent_config=_agent_config())

            async def exercise():
                for index in range(20):
                    result = await engine.arun(
                        _state("run-resource-{}".format(index)),
                        caller="main",
                        system_prompt="offline",
                    )
                    self.assertEqual(result["final_output"], "ok")

            asyncio.run(exercise())

        self.assertEqual(len(managers), 20)
        self.assertTrue(all(not manager.callbacks for manager in managers))
        self.assertEqual(unregistered_cards, registered_cards)
        self.assertEqual(len(disposed_sessions), 20)
        self.assertEqual(
            len({session_id for _agent_id, session_id in disposed_sessions}),
            20,
        )


if __name__ == "__main__":
    unittest.main()
