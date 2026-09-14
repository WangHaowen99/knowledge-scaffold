"""Tests for Consumer-owned openJiuwen tool scheduling."""

import asyncio
import json
import threading
import time
from collections import defaultdict
from operator import getitem
from types import SimpleNamespace

import pytest
from agent.core.jiuwen.concurrency import (
    JiuwenConcurrencyAbilityManager,
    JiuwenToolConcurrencyPolicy,
    bind_jiuwen_tool_call_identity,
    current_jiuwen_tool_call_id,
    current_jiuwen_tool_call_identity,
    install_jiuwen_concurrency_ability_manager,
)
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter
from agent.core.jiuwen_engine import (
    JiuwenReactLoopEngine,
    _JiuwenTerminalForceFinishBridge,
    _remember_jiuwen_terminal_tool_batch,
)
from agent.runtime.async_utils import (
    clear_bounded_offload_pools,
    configure_bounded_offload_pools,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.tools.base import ToolContext, ToolOutcome
from agent.tools.executor import aexecute_tool_batch
from agent.tools.registry import TOOL_SPECS
from agent.tools.scheduling import (
    normalize_parallelism_limit,
    plan_barriered_tool_batches,
)


@pytest.fixture(autouse=True)
def _lifespan_owned_offload_pools():
    def new_pool(name):
        return BoundedOffloadPool(
            name=name,
            max_workers=4,
            max_pending=16,
            capacity_wait_seconds=1.0,
            operation_timeout_seconds=5.0,
        )

    pools = tuple(new_pool(name) for name in ("concurrency-short", "concurrency-external", "concurrency-tool"))
    configure_bounded_offload_pools(
        short_io=pools[0],
        external_io=pools[1],
        tool_io=pools[2],
    )
    try:
        yield
    finally:

        async def close_pools():
            deadline = asyncio.get_running_loop().time() + 2.0
            await asyncio.gather(*(pool.close(deadline=deadline) for pool in pools))

        clear_bounded_offload_pools()
        asyncio.run(close_pools())


def _tool_call(call_id, name, arguments=None):
    return SimpleNamespace(
        id=call_id,
        name=name,
        arguments=dict(arguments or {}),
    )


class _ParentContext:
    def __init__(self):
        self.has_force_finish_request = False


class _CallbackContext:
    def __init__(self, tool_call):
        self.inputs = SimpleNamespace(tool_call=tool_call)
        self.exception = None
        self.force_finish_results = []

    def request_force_finish(self, result):
        self.force_finish_results.append(result)


def test_shared_planner_emits_safe_batches_and_unsafe_barriers():
    items = [
        ("safe-a", True),
        ("safe-b", True),
        ("unsafe-a", False),
        ("unsafe-b", False),
        ("safe-c", True),
    ]

    batches = plan_barriered_tool_batches(
        items,
        is_concurrency_safe=lambda item: item[1],
    )

    assert [([item[0] for item in batch.items], batch.parallel) for batch in batches] == [
        (["safe-a", "safe-b"], True),
        (["unsafe-a"], False),
        (["unsafe-b"], False),
        (["safe-c"], True),
    ]

    serial_batches = plan_barriered_tool_batches(
        items[:2],
        is_concurrency_safe=lambda item: item[1],
        parallel_enabled=False,
    )
    assert [([item[0] for item in batch.items], batch.parallel) for batch in serial_batches] == [
        (["safe-a"], False),
        (["safe-b"], False),
    ]


def test_shared_parallelism_limit_normalization():
    assert normalize_parallelism_limit(3) == 3
    assert normalize_parallelism_limit(0, fallback=4) == 4
    assert normalize_parallelism_limit("invalid", fallback=2) == 2
    assert normalize_parallelism_limit(-5) == 1


def test_jiuwen_scheduler_preserves_order_and_enforces_limits():
    active_total = 0
    max_active_total = 0
    active_by_tool = defaultdict(int)
    max_active_by_tool = defaultdict(int)
    observed_identities = []

    class _Delegate:
        async def execute(self, *, ctx, tool_call, session, tag=None):
            nonlocal active_total, max_active_total
            del ctx, session, tag
            identity = current_jiuwen_tool_call_identity()
            observed_identities.append(identity)
            active_total += 1
            active_by_tool[tool_call.name] += 1
            max_active_total = max(max_active_total, active_total)
            max_active_by_tool[tool_call.name] = max(
                max_active_by_tool[tool_call.name],
                active_by_tool[tool_call.name],
            )
            await asyncio.sleep(0.01)
            active_by_tool[tool_call.name] -= 1
            active_total -= 1
            return [
                (
                    tool_call.id,
                    SimpleNamespace(
                        content=tool_call.name,
                        tool_call_id=tool_call.id,
                    ),
                )
            ]

    def resolve_policy(tool_name):
        return JiuwenToolConcurrencyPolicy(
            concurrency_safe=True,
            max_parallelism=1 if tool_name == "limited" else 5,
        )

    calls = [
        _tool_call("call-1", "limited"),
        _tool_call("call-2", "limited"),
        _tool_call("call-3", "other"),
        _tool_call("call-4", "other"),
    ]
    manager = JiuwenConcurrencyAbilityManager(
        _Delegate(),
        max_parallel_workers=2,
        policy_resolver=resolve_policy,
    )

    results = asyncio.run(manager.execute(_ParentContext(), calls, object()))

    assert [result for result, _message in results] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
    ]
    assert max_active_total == 2
    assert max_active_by_tool["limited"] == 1
    assert sorted(identity.model_tool_call_id for identity in observed_identities) == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
    ]
    assert len({id(identity.correlation_token) for identity in observed_identities}) == 4
    assert current_jiuwen_tool_call_identity() is None


def test_jiuwen_scheduler_treats_terminal_unsafe_tool_as_barrier_and_stops():
    timeline = []

    class _Delegate:
        async def execute(self, *, ctx, tool_call, session, tag=None):
            del session, tag
            timeline.append("{}:start".format(tool_call.id))
            await asyncio.sleep(0.01)
            if tool_call.name == "interaction":
                ctx.has_force_finish_request = True
            timeline.append("{}:end".format(tool_call.id))
            return [
                (
                    tool_call.id,
                    SimpleNamespace(
                        content=tool_call.name,
                        tool_call_id=tool_call.id,
                    ),
                )
            ]

    manager = JiuwenConcurrencyAbilityManager(
        _Delegate(),
        policy_resolver=lambda tool_name: JiuwenToolConcurrencyPolicy(concurrency_safe=tool_name != "interaction"),
    )
    calls = [
        _tool_call("safe-1", "lookup"),
        _tool_call("safe-2", "lookup"),
        _tool_call("interaction-1", "interaction"),
        _tool_call("trailing-1", "lookup"),
    ]

    results = asyncio.run(manager.execute(_ParentContext(), calls, object()))

    assert timeline.index("interaction-1:start") > timeline.index("safe-1:end")
    assert timeline.index("interaction-1:start") > timeline.index("safe-2:end")
    assert "trailing-1:start" not in timeline
    assert [result for result, _message in results] == [
        "safe-1",
        "safe-2",
        "interaction-1",
        None,
    ]
    skipped_message = results[-1][1]
    assert skipped_message.tool_call_id == "trailing-1"
    assert json.loads(skipped_message.content)["status"] == "skipped"


def test_jiuwen_scheduler_accepts_openjiuwen_014_positional_tag():
    observed = {}

    class _Delegate:
        async def execute(self, *, ctx, tool_call, session, tag=None):
            del ctx, session
            observed["tag"] = tag
            observed["tool_call_id"] = current_jiuwen_tool_call_id()
            return [(tool_call.id, SimpleNamespace(tool_call_id=tool_call.id))]

    manager = JiuwenConcurrencyAbilityManager(
        _Delegate(),
        policy_resolver=lambda _name: JiuwenToolConcurrencyPolicy(True),
    )

    asyncio.run(
        manager.execute(
            _ParentContext(),
            _tool_call("legacy-call", "lookup"),
            object(),
            "legacy-tag",
        )
    )

    assert observed == {
        "tag": "legacy-tag",
        "tool_call_id": "legacy-call",
    }


def test_terminal_bridge_correlates_model_and_consumer_call_ids():
    terminal_result = {}
    batch_result = SimpleNamespace(
        terminal_content="interaction required",
        terminal_tool_call_id="consumer-internal-call",
        messages=[],
    )
    model_call = _tool_call("", "interaction")
    unrelated_call = _tool_call("other-model-call", "lookup")
    matching_context = _CallbackContext(model_call)
    unrelated_context = _CallbackContext(unrelated_call)
    bridge = _JiuwenTerminalForceFinishBridge(
        terminal_result,
        event_class_provider=lambda: None,
    )

    with bind_jiuwen_tool_call_identity(model_call):
        _remember_jiuwen_terminal_tool_batch(terminal_result, batch_result)
        with bind_jiuwen_tool_call_identity(unrelated_call):
            asyncio.run(bridge._on_after_tool_call(unrelated_context))
        asyncio.run(bridge._on_after_tool_call(matching_context))

    assert getitem(terminal_result, "tool_call_id") == "consumer-internal-call"
    assert getitem(terminal_result, "model_tool_call_id") == ""
    assert unrelated_context.force_finish_results == []
    assert matching_context.force_finish_results == [
        {
            "output": "interaction required",
            "result_type": "answer",
        }
    ]


def test_jiuwen_scheduler_installer_is_idempotent():
    delegate = SimpleNamespace(marker="delegate")
    agent = SimpleNamespace(ability_manager=delegate)

    assert install_jiuwen_concurrency_ability_manager(
        agent,
        max_parallel_workers=2,
    )
    installed = agent.ability_manager
    assert isinstance(installed, JiuwenConcurrencyAbilityManager)
    assert installed.marker == "delegate"

    assert install_jiuwen_concurrency_ability_manager(
        agent,
        max_parallel_workers=4,
    )
    assert agent.ability_manager is installed
    assert installed._max_parallel_workers == 4


def test_jiuwen_engine_installs_scheduler_on_factory_agent():
    delegate = SimpleNamespace(marker="factory-delegate")
    agent = SimpleNamespace(ability_manager=delegate)
    engine = JiuwenReactLoopEngine(
        agent_config=SimpleNamespace(parallel_default_max_workers=3),
        agent_factory=lambda: agent,
    )

    created_agent = engine._create_agent(max_iterations=1)

    assert created_agent is agent
    assert isinstance(agent.ability_manager, JiuwenConcurrencyAbilityManager)
    assert agent.ability_manager.marker == "factory-delegate"
    assert agent.ability_manager._max_parallel_workers == 3


def test_mock_jiuwen_end_to_end_serializes_unsafe_calls_and_stops_after_interaction(
    monkeypatch,
):
    """Exercise the full local Jiuwen-to-Consumer boundary without an LLM."""

    execution_events = []
    execution_lock = threading.Lock()
    active_tools = 0
    max_active_tools = 0

    class _SyntheticTool:
        allowed_callers = {"main"}
        category = "regular"

        def __init__(self, name, *, concurrency_safe, max_parallelism):
            self.name = name
            self.concurrency_safe = concurrency_safe
            self.max_parallelism = max_parallelism
            self.schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Synthetic scheduling test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
            }

        async def aexecute(
            self,
            *,
            args,
            context,
            tool_call_id=None,
            event_emitter=None,
        ):
            nonlocal active_tools, max_active_tools
            del args, context, event_emitter
            with execution_lock:
                active_tools += 1
                max_active_tools = max(max_active_tools, active_tools)
                execution_events.append((self.name, "start", tool_call_id))
            await asyncio.sleep(0.02)
            with execution_lock:
                execution_events.append((self.name, "end", tool_call_id))
                active_tools -= 1
            return ToolOutcome(content="completed:{}".format(self.name))

    safe_tool_name = "mock_safe_lookup"
    first_unsafe_name = "mock_unsafe_first"
    second_unsafe_name = "mock_unsafe_second"
    monkeypatch.setitem(
        TOOL_SPECS,
        safe_tool_name,
        _SyntheticTool(
            safe_tool_name,
            concurrency_safe=True,
            max_parallelism=2,
        ),
    )
    monkeypatch.setitem(
        TOOL_SPECS,
        first_unsafe_name,
        _SyntheticTool(
            first_unsafe_name,
            concurrency_safe=False,
            max_parallelism=1,
        ),
    )
    monkeypatch.setitem(
        TOOL_SPECS,
        second_unsafe_name,
        _SyntheticTool(
            second_unsafe_name,
            concurrency_safe=False,
            max_parallelism=1,
        ),
    )

    state = {
        "run_id": "mock-e2e-run",
        "session_id": "mock-e2e-session",
        "messages": [],
        "results": {},
    }
    terminal_result = {}

    def remember_terminal_tool_batch(batch_result):
        _remember_jiuwen_terminal_tool_batch(
            terminal_result,
            batch_result,
        )

    def build_tool_context():
        return ToolContext(
            memory_manager=None,
            messages=state["messages"],
            run_id=state["run_id"],
            session_id=state["session_id"],
            turn_id=1,
            agent_config=SimpleNamespace(parallel_default_max_workers=2),
            caller="main",
            supports_user_interaction=True,
        )

    tool_names = (
        safe_tool_name,
        first_unsafe_name,
        second_unsafe_name,
        "request_user_interaction",
    )
    adapters = {
        tool_name: CurrentRuntimeToolAdapter(
            tool_name=tool_name,
            state=state,
            context_factory=build_tool_context,
            tool_batch_executor=aexecute_tool_batch,
            terminal_tool_batch_callback=remember_terminal_tool_batch,
        )
        for tool_name in tool_names
    }

    class _CallbackEvents:
        AFTER_TOOL_CALL = "after_tool_call"

    class _MockCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

        async def unregister(self, event, callback):
            if self.callbacks.get(event) == callback:
                self.callbacks.pop(event, None)

    delegate_events = []
    callback_manager = _MockCallbackManager()

    class _MockOriginalAbilityManager:
        async def execute(self, *, ctx, tool_call, session, tag=None):
            del tag
            delegate_events.append("{}:start".format(tool_call.id))
            tool_result = await adapters[tool_call.name].invoke(
                tool_call.arguments,
                session=session,
            )
            callback_context = _CallbackContext(tool_call)
            callback = callback_manager.callbacks[_CallbackEvents.AFTER_TOOL_CALL]
            await callback(callback_context)
            if callback_context.force_finish_results:
                ctx.has_force_finish_request = True
                ctx.force_finish_result = callback_context.force_finish_results[-1]
            delegate_events.append("{}:end".format(tool_call.id))
            return [
                (
                    tool_result,
                    SimpleNamespace(
                        content=tool_result,
                        tool_call_id=tool_call.id,
                    ),
                )
            ]

    agent = SimpleNamespace(
        ability_manager=_MockOriginalAbilityManager(),
        agent_callback_manager=callback_manager,
    )
    engine = JiuwenReactLoopEngine(
        agent_config=SimpleNamespace(parallel_default_max_workers=2),
        agent_factory=lambda: agent,
    )
    scheduled_agent = engine._create_agent(max_iterations=1)
    bridge = _JiuwenTerminalForceFinishBridge(
        terminal_result,
        event_class_provider=lambda: _CallbackEvents,
    )
    model_calls = [
        _tool_call("model-safe-1", safe_tool_name),
        _tool_call("model-safe-2", safe_tool_name),
        _tool_call("model-unsafe-1", first_unsafe_name),
        _tool_call("model-unsafe-2", second_unsafe_name),
        _tool_call(
            "model-interaction",
            "request_user_interaction",
            {
                "type": "input",
                "prompt": "Provide a synthetic value to continue.",
            },
        ),
        _tool_call("model-trailing", safe_tool_name),
    ]
    parent_context = _ParentContext()

    async def run_scenario():
        await bridge.register(scheduled_agent)
        try:
            return await scheduled_agent.ability_manager.execute(
                parent_context,
                model_calls,
                object(),
            )
        finally:
            await bridge.unregister()

    results = asyncio.run(run_scenario())

    assert max_active_tools == 2
    assert delegate_events.index("model-unsafe-1:start") > max(
        delegate_events.index("model-safe-1:end"),
        delegate_events.index("model-safe-2:end"),
    )
    assert delegate_events.index("model-unsafe-2:start") > delegate_events.index("model-unsafe-1:end")
    assert delegate_events.index("model-interaction:start") > delegate_events.index("model-unsafe-2:end")
    assert "model-trailing:start" not in delegate_events
    assert (
        sum(
            1
            for tool_name, event, _tool_call_id in execution_events
            if tool_name == safe_tool_name and event == "start"
        )
        == 2
    )

    assert parent_context.has_force_finish_request is True
    assert parent_context.force_finish_result["result_type"] == "answer"
    assert state["pending_user_interaction"]["prompt"] == ("Provide a synthetic value to continue.")
    assert terminal_result["model_tool_call_id"] == "model-interaction"
    assert terminal_result["tool_call_id"] != "model-interaction"
    assert terminal_result["correlation_token"] is not None

    assert [message.tool_call_id for _result, message in results] == [
        "model-safe-1",
        "model-safe-2",
        "model-unsafe-1",
        "model-unsafe-2",
        "model-interaction",
        "model-trailing",
    ]
    assert results[-1][0] is None
    assert json.loads(results[-1][1].content)["status"] == "skipped"
