"""Regression tests for repeated Jiuwen tool-failure protection."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from agent import exceptions as agent_exceptions
from agent.core.jiuwen_engine import JiuwenReactLoopEngine, _JiuwenTerminalForceFinishBridge
from agent.core.jiuwen.tools import (
    ConsecutiveToolFailureGuard,
    CurrentRuntimeToolAdapter,
    register_current_runtime_tools,
)
from agent.core.orchestrator import _public_workflow_error_message
from agent.tools.base import ToolBatchResult, ToolInvocation
from config.settings import AgentConfig
from integration.observability import RuntimeEventType, RuntimeStatus


_REAL_IMPORT_MODULE = importlib.import_module


def _error_batch(tool_calls, content="Error: arguments must be a JSON object"):
    return ToolBatchResult(
        messages=[
            {
                "role": "tool",
                "content": content,
                "tool_call_id": tool_calls[0].id,
                "name": tool_calls[0].name,
            }
        ]
    )


def test_loop_stall_error_is_exposed_as_its_safe_public_message():
    exception_type = getattr(agent_exceptions, "AgentLoopStalledError", None)

    assert exception_type is not None
    exc = exception_type("Tool execution stalled after a repeated failure.")
    assert _public_workflow_error_message(exc) == exc.message


def test_force_finish_bridge_skips_event_loading_without_callback_manager():
    def unexpected_event_loading():
        raise AssertionError("event class must not load without callback support")

    bridge = _JiuwenTerminalForceFinishBridge(
        {},
        event_class_provider=unexpected_event_loading,
    )

    assert asyncio.run(bridge.register(SimpleNamespace())) is False


def test_identical_tool_failure_stops_after_second_attempt_with_stable_arg_order():
    calls = []

    def fake_executor(tool_calls, context):
        del context
        calls.append(tool_calls[0])
        return _error_batch(tool_calls)

    adapter = CurrentRuntimeToolAdapter(
        tool_name="execute_script",
        state={"messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )
    first_args = {
        "ontology_id": "demo",
        "operation_key": "split_material",
        "arguments": {"month": 3, "options": {"b": 2, "a": 1}},
    }
    second_args = {
        "arguments": {"options": {"a": 1, "b": 2}, "month": 3},
        "operation_key": "split_material",
        "ontology_id": "demo",
    }

    first = asyncio.run(adapter.invoke(first_args))
    assert first.startswith("Error:")

    with pytest.raises(agent_exceptions.AgentLoopStalledError) as raised:
        asyncio.run(adapter.invoke(second_args))

    assert len(calls) == 2
    assert "execute_script" in raised.value.message
    assert "arguments must be a JSON object" in raised.value.message


def test_stalled_failure_snapshots_the_tool_result_before_propagating():
    calls = []
    snapshots = []

    def fake_executor(tool_calls, context):
        del context
        calls.append(tool_calls[0])
        return _error_batch(tool_calls)

    adapter = CurrentRuntimeToolAdapter(
        tool_name="execute_script",
        state={"messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
        state_snapshot_callback=snapshots.append,
    )
    arguments = {"operation_key": "split_material", "arguments": {}}

    asyncio.run(adapter.invoke(arguments))
    with pytest.raises(agent_exceptions.AgentLoopStalledError):
        asyncio.run(adapter.invoke(arguments))

    last_message = snapshots[-1]["messages"][-1]
    assert last_message["role"] == "tool"
    assert last_message["tool_call_id"] == calls[-1].id


def test_changed_arguments_do_not_trip_the_consecutive_failure_guard():
    def fake_executor(tool_calls, context):
        del context
        return _error_batch(tool_calls)

    adapter = CurrentRuntimeToolAdapter(
        tool_name="execute_script",
        state={"messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    for month in (3, 4, 3):
        result = asyncio.run(
            adapter.invoke(
                {
                    "ontology_id": "demo",
                    "operation_key": "split_material",
                    "arguments": {"month": month},
                }
            )
        )
        assert result.startswith("Error:")


def test_terminal_result_resets_the_failure_guard():
    guard = ConsecutiveToolFailureGuard()
    invocation = ToolInvocation(
        id="call-1",
        name="execute_script",
        arguments={"operation_key": "split_material"},
    )

    guard.record(invocation, _error_batch([invocation]))
    guard.record(invocation, ToolBatchResult(terminal=True))
    guard.record(invocation, _error_batch([invocation]))


@pytest.mark.parametrize(
    ("first_content", "second_content"),
    [
        (
            '{"success":false,"error":"backend failed","meta":{"b":2,"a":1}}',
            '{"meta":{"a":1,"b":2},"error":"backend failed","success":false}',
        ),
        ('{"ok":false,"message":"query failed"}', '{"message":"query failed","ok":false}'),
        ('{"_success":false,"detail":"query failed"}', '{"detail":"query failed","_success":false}'),
        ('{"status":"error","detail":"query failed"}', '{"detail":"query failed","status":"error"}'),
        ('{"error":"query failed"}', '{"error":"query failed"}'),
        ('{"_error":"query failed"}', '{"_error":"query failed"}'),
    ],
)
def test_json_tool_errors_are_canonicalized_before_repeat_detection(first_content, second_content):
    guard = ConsecutiveToolFailureGuard()
    invocation = ToolInvocation(
        id="call-json-error",
        name="business_data_search",
        arguments={"entity": "生产流程装置"},
    )

    guard.record(invocation, _error_batch([invocation], content=first_content))
    with pytest.raises(agent_exceptions.AgentLoopStalledError):
        guard.record(invocation, _error_batch([invocation], content=second_content))


def test_registered_adapters_share_success_resets_across_tools():
    created = {}

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceManager:
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tools"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        def add(self, cards):
            created["cards"] = cards

    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent-shared-failure-guard"),
        ability_manager=FakeAbilityManager(),
    )
    modules = {
        "openjiuwen.core.foundation.tool": SimpleNamespace(
            Tool=FakeTool,
            ToolCard=FakeToolCard,
        ),
        "openjiuwen.core.runner": SimpleNamespace(
            Runner=SimpleNamespace(resource_mgr=FakeResourceManager())
        ),
    }

    def fake_executor(tool_calls, context):
        del context
        if tool_calls[0].name == "execute_script":
            return _error_batch(tool_calls)
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": '{"success": true}',
                    "tool_call_id": tool_calls[0].id,
                    "name": tool_calls[0].name,
                }
            ]
        )

    register_current_runtime_tools(
        agent,
        state={"run_id": "run-shared-guard", "messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_infos=[
            {
                "name": "execute_script",
                "description": "Execute a script.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "business_data_search",
                "description": "Search business data.",
                "parameters": {"type": "object", "properties": {}},
            },
        ],
        tool_batch_executor=fake_executor,
        import_module=lambda name: modules[name],
    )
    args = {"operation_key": "split_material", "arguments": {}}

    assert asyncio.run(created["tools"]["execute_script"].invoke(args)).startswith("Error:")
    assert asyncio.run(created["tools"]["business_data_search"].invoke({"query": "reset"})) == (
        '{"success": true}'
    )
    assert asyncio.run(created["tools"]["execute_script"].invoke(args)).startswith("Error:")


def test_jiuwen_loop_propagates_stall_when_runtime_swallows_tool_exception(monkeypatch, tmp_path):
    from workspace.workspaces import create_workspace

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    created = {}

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"
        AFTER_TOOL_CALL = "after_tool_call"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

        async def unregister(self, event, callback):
            if self.callbacks.get(event) == callback:
                self.callbacks.pop(event, None)

    class FakeToolCallbackContext:
        def __init__(self, *, tool_call_id, exception):
            self.inputs = SimpleNamespace(
                tool_call=SimpleNamespace(id=tool_call_id, name="execute_script")
            )
            self.exception = exception
            self.force_finish_result = None

        def request_force_finish(self, result):
            self.force_finish_result = result

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceManager:
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tools"] = {item.card.name: item for item in tools}

        def remove_tool(self, tool_id, tag=None, skip_if_tag_not_exists=True):
            del tool_id, tag, skip_if_tag_not_exists

    class FakeAbilityManager:
        def add(self, cards):
            created["cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-repeated-failure")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()
            self.agent_callback_manager = FakeCallbackManager()
            self.force_finish_results = []

        async def stream(self, inputs):
            del inputs
            tool = created["tools"]["execute_script"]
            arguments = {
                "ontology_id": "demo",
                "operation_key": "split_material",
                "arguments": {},
            }
            await tool.invoke(arguments)
            try:
                await tool.invoke(arguments)
            except agent_exceptions.AgentLoopStalledError as exc:
                callback = self.agent_callback_manager.callbacks[
                    FakeAgentCallbackEvent.AFTER_TOOL_CALL
                ]
                callback_context = FakeToolCallbackContext(
                    tool_call_id=created["last_tool_call_id"],
                    exception=exc,
                )
                result = callback(callback_context)
                if asyncio.iscoroutine(result):
                    await result
                if callback_context.force_finish_result is not None:
                    self.force_finish_results.append(callback_context.force_finish_result)
                    yield SimpleNamespace(
                        type="answer",
                        payload=callback_context.force_finish_result,
                    )
                    return
                # Emulate openJiuwen converting the exception into an ordinary
                # tool message and continuing the model loop.
            yield SimpleNamespace(
                type="answer",
                payload={"output": "should not continue", "result_type": "answer"},
            )

    resource_manager = FakeResourceManager()
    fake_agent = FakeAgent()

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(
                Tool=FakeTool,
                ToolCard=FakeToolCard,
            ),
            "openjiuwen.core.runner": SimpleNamespace(
                Runner=SimpleNamespace(resource_mgr=resource_manager)
            ),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        created["last_tool_call_id"] = tool_calls[0].id
        return _error_batch(tool_calls)

    tool_info = {
        "type": "function",
        "name": "execute_script",
        "description": "Execute an ontology script.",
        "parameters": {"type": "object", "properties": {}},
    }
    tool_schema = {
        "type": "function",
        "function": {
            "name": "execute_script",
            "description": tool_info["description"],
            "parameters": tool_info["parameters"],
        },
    }
    config = AgentConfig(
        llm_connection_max_retries=1,
        main_agent_max_iterations=4,
        sub_agent_max_iterations=2,
        sub_agent_max_concurrency=1,
        sub_agent_max_depth=1,
        parallel_default_max_workers=1,
        context_max_rounds=4,
    )

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_agent_callback_event_class",
        lambda: FakeAgentCallbackEvent,
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            provider="openai",
            model="fake-model",
            max_tokens=1024,
        ),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.execute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=config)
    monkeypatch.setattr(engine, "_tools_schema_for_state", lambda *_args, **_kwargs: [tool_schema])
    monkeypatch.setattr(engine, "_jiuwen_tool_infos_for_state", lambda *_args, **_kwargs: [tool_info])
    state = {
        "run_id": "run-repeated-failure",
        "session_id": "sess-repeated-failure",
        "current_turn_id": 1,
        "workspace_id": "demo",
        "user_request": "execute the script",
        "messages": [],
        "results": {},
    }
    observer = RecordingObserver()

    with pytest.raises(agent_exceptions.AgentLoopStalledError, match="execute_script"):
        engine.run(
            state,
            caller="main",
            observer=observer,
            system_prompt="Use the execute_script tool.",
        )

    assert fake_agent.force_finish_results
    assert fake_agent.force_finish_results[-1]["result_type"] == "error"
    assert "final_output" not in state
    loop_finished = [
        event
        for event in observer.events
        if event.type == RuntimeEventType.AGENT_LOOP_FINISHED
    ]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.ERROR
