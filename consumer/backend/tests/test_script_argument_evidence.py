# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Synthetic regressions for ordinary Agent script evidence and public failures."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.core.jiuwen.tools import ConsecutiveToolFailureGuard, CurrentRuntimeToolAdapter
from agent.exceptions import AgentLoopStalledError
from agent.runtime.adapter import PublicEventAdapter
from agent.runtime.contracts import RuntimeInvocationContext
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.script_argument_errors import safe_script_argument_error
from agent.tools.base import ToolBatchResult, ToolContext, ToolInvocation
import agent.tools.execute_script as scripts
from ontology.ontology_catalog import load_script_catalog, public_script_catalog
from internal_access import internal


SYNTHETIC_UUID = "00000000-0000-4000-8000-000000000001"
REQUEST = "Process the synthetic records."


def _script_messages(payload, *, ontology_id="knowledge_package_demo", call_id="call_demo"):
    return [
        {"role": "assistant", "tool_calls": [{
            "id": call_id,
            "function": {"name": "execute_script", "arguments": json.dumps({
                "ontology_id": ontology_id, "operation_key": "create_result", "arguments": {},
            })},
        }]},
        {"role": "tool", "name": "execute_script", "tool_call_id": call_id,
         "content": json.dumps(payload)},
    ]


@pytest.fixture
def bound_script(tmp_path, monkeypatch):
    root = tmp_path / "knowledge_package_demo"
    directory = root / "scripts"
    directory.mkdir(parents=True)
    descriptor = directory / "run.md"
    descriptor.write_text(
        "---\nkey: run\nbound_operation: create_result\nentrypoint: scripts/run.py\n"
        "generated_identifiers:\n  record_json.calculation_result_id: uuid4\n---\n"
        "| name | flag | type | required |\n| --- | --- | --- | --- |\n"
        "| record_json | --record-json | object | true |\n",
        encoding="utf-8",
    )
    (directory / "run.py").write_text(
        "import argparse, json\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--record-json')\n"
        "args = parser.parse_args()\n"
        "print(json.dumps(json.loads(args.record_json)))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scripts, "get_ontology_manifest", lambda *_args, **_kwargs: (
        SimpleNamespace(id="knowledge_package_demo", version="1.0.0", ontology_dir=str(root)), None,
    ))
    monkeypatch.setattr("ontology.operation_execution.is_deterministic_operation", lambda *_args: False)
    monkeypatch.setattr(scripts, "_build_script_env_overrides", lambda **_kwargs: {})
    executions = []

    def execute_synthetic_script(**kwargs):
        executions.append(kwargs)
        # Exercise the actual bound script with synthetic input; sandbox policy is tested separately.
        result = subprocess.run(
            [sys.executable, str(Path(kwargs["base_path"]) / kwargs["script_path"]), *kwargs["args"]],
            capture_output=True, text=True, check=True, timeout=10,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        return result.stdout

    monkeypatch.setattr(scripts, "handle_execute_script_path", execute_synthetic_script)
    context = ToolContext(
        memory_manager=None, messages=[{"role": "user", "content": REQUEST}],
        run_id="run_demo", session_id="session_demo", turn_id=1,
        agent_config=SimpleNamespace(execute_script_timeout=10, deterministic_execution_enabled=False),
        caller="main", workspace_id="workspace_demo", project_root=str(tmp_path),
        user_id="operator_a", selected_ontology_id="knowledge_package_demo",
        selected_ontology_version="1.0.0",
    )
    return SimpleNamespace(root=root, descriptor=descriptor, context=context, executions=executions)


def _execute(fixture, record, *, operation_key="create_result"):
    return scripts.ExecuteScriptTool().execute(args={
        "ontology_id": "knowledge_package_demo", "operation_key": operation_key,
        "arguments": {"record_json": record},
    }, context=fixture.context, tool_call_id="call_current")


@pytest.mark.parametrize("deterministic_enabled", [False, True])
def test_declared_generated_identifier_runs_through_actual_tool(bound_script, deterministic_enabled):
    bound_script.context.agent_config.deterministic_execution_enabled = deterministic_enabled
    record = {"calculation_result_id": SYNTHETIC_UUID}
    outcome = _execute(bound_script, json.dumps(record))
    assert json.loads(outcome.content) == record
    assert len(bound_script.executions) == 1
    assert outcome.terminal is False
    catalog = public_script_catalog(load_script_catalog(bound_script.root))
    assert catalog[0]["generated_identifiers"] == {"record_json.calculation_result_id": "uuid4"}


@pytest.mark.parametrize("record, code, path", [
    ({"calculation_result_id": "synthetic_result_0001"}, "invalid_generated_identifier", "calculation_result_id"),
    ({"calculation_result_id": SYNTHETIC_UUID.replace("-", " ")},
     "invalid_generated_identifier", "calculation_result_id"),
    ({"calculation_result_id": "00000000-0000-1000-8000-000000000001"},
     "invalid_generated_identifier", "calculation_result_id"),
    ({"device_id": SYNTHETIC_UUID}, "ungrounded_identity", "device_id"),
    ({"other_result_id": SYNTHETIC_UUID}, "ungrounded_identity", "other_result_id"),
    ({"calculation_result_id": SYNTHETIC_UUID, "device_id": "device_other"}, "ungrounded_identity", "device_id"),
])
def test_generation_rule_does_not_exempt_other_identities(bound_script, record, code, path):
    outcome = _execute(bound_script, record)
    error = json.loads(outcome.content)["error"]
    assert error["code"] == "SCRIPT_ARGUMENT_VALIDATION_FAILED"
    assert error["correctable"] is True
    assert {"code": code, "path": "arguments.record_json." + path} in error["issues"]
    assert outcome.terminal is False
    assert bound_script.executions == []


def test_unregistered_generation_rule_is_not_an_exemption(bound_script):
    bound_script.descriptor.write_text(
        bound_script.descriptor.read_text().replace(": uuid4", ": arbitrary"), encoding="utf-8",
    )
    error = json.loads(_execute(bound_script, {"calculation_result_id": SYNTHETIC_UUID}).content)["error"]
    assert error["issues"][0]["code"] == "ungrounded_identity"
    assert bound_script.executions == []


@pytest.mark.parametrize("value", [None, "", False, {}, [], {"nested": SYNTHETIC_UUID}])
def test_declared_identifier_rejects_empty_or_container_values(bound_script, value):
    outcome = _execute(bound_script, {"calculation_result_id": value})
    assert json.loads(outcome.content)["error"]["issues"][0]["code"] == "invalid_generated_identifier"
    assert bound_script.executions == []


def test_generated_identifier_rule_supports_explicit_array_paths():
    issues = []
    assert internal(scripts).validate_structured_arguments_against_request(
        {"rows_json": json.dumps([{"calculation_result_id": SYNTHETIC_UUID}])}, REQUEST,
        generated_identifiers={"rows_json[].calculation_result_id": "uuid4"}, issues=issues,
    ) == []
    assert issues == []


def test_correlated_successful_script_output_supports_followup(bound_script):
    record = {"device_id": "device_synthetic_0001", "quantity": 12}
    bound_script.context.messages.extend(_script_messages(record))
    outcome = _execute(bound_script, record)
    assert json.loads(outcome.content) == record
    assert len(bound_script.executions) == 1


@pytest.mark.parametrize("failure", [
    {"ok": False}, {"success": False}, {"_success": False},
    {"status": "failed"}, {"status": "error"}, {"error": "synthetic_failure"},
])
def test_failed_tool_payload_never_supplies_identity_evidence(bound_script, failure):
    record = {"device_id": "device_synthetic_0001"}
    bound_script.context.messages.extend(_script_messages({**failure, **record}))
    outcome = _execute(bound_script, record)
    assert json.loads(outcome.content)["ok"] is False
    assert bound_script.executions == []


@pytest.mark.parametrize("source", [
    "uncorrelated", "assistant", "previous_turn", "other_package", "wrong_call", "no_user",
])
def test_untrusted_or_out_of_scope_results_do_not_supply_evidence(bound_script, source):
    record = {"device_id": "device_synthetic_0001"}
    messages = _script_messages(record)
    if source == "uncorrelated":
        messages = messages[1:]
    elif source == "assistant":
        messages = [{"role": "assistant", "content": json.dumps(record)}]
    elif source == "previous_turn":
        messages.append({"role": "user", "content": "Process a new synthetic request."})
    elif source == "other_package":
        messages = _script_messages(record, ontology_id="knowledge_package_other")
    elif source == "wrong_call":
        messages[-1]["tool_call_id"] = "call_unrelated"
    elif source == "no_user":
        bound_script.context.messages.clear()
    bound_script.context.messages.extend(messages)
    error = json.loads(_execute(bound_script, record).content)["error"]
    assert error["issues"][0]["code"] == "ungrounded_identity"
    assert bound_script.executions == []


def test_record_fields_cannot_be_mixed_across_successful_outputs(bound_script):
    rows = [
        {"device_id": "device_synthetic_0001", "category": "red"},
        {"device_id": "device_synthetic_0002", "category": "blue"},
    ]
    bound_script.context.messages.extend(_script_messages({"rows": rows}))
    outcome = _execute(bound_script, {**rows[0], "category": "blue"})
    assert json.loads(outcome.content)["error"]["issues"] == [{
        "code": "evidence_record_mismatch", "path": "arguments.record_json",
    }]
    assert bound_script.executions == []


def test_current_business_search_remains_a_supported_source(bound_script):
    record = {"device_id": "device_synthetic_0001"}
    bound_script.context.messages.append({
        "role": "tool", "name": "business_data_search", "content": json.dumps(record),
    })
    assert json.loads(_execute(bound_script, record).content) == record


def test_failed_record_inside_successful_batch_does_not_supply_evidence(bound_script):
    record = {"device_id": "device_synthetic_0001"}
    bound_script.context.messages.extend(_script_messages({
        "ok": True, "rows": [{"success": False, "data": record}],
    }))
    assert json.loads(_execute(bound_script, record).content)["error"]["issues"][0]["code"] == "ungrounded_identity"
    assert bound_script.executions == []


def test_tool_evidence_cannot_supply_a_value_under_a_different_field(bound_script):
    bound_script.context.messages.extend(_script_messages({"category": "device_synthetic_0001"}))
    outcome = _execute(bound_script, {"device_id": "device_synthetic_0001"})
    assert json.loads(outcome.content)["error"]["issues"][0]["code"] == "ungrounded_identity"


def test_tool_evidence_does_not_override_current_request_field_pairings():
    request = "Record 1: sample-a quantity 12.5 category red; Record 2: sample-b quantity 7.25 category blue"
    rows = [
        {"name": "sample-a", "quantity": 12.5, "category": "blue"},
        {"name": "sample-b", "quantity": 7.25, "category": "red"},
    ]
    issues = []
    errors = internal(scripts).validate_structured_arguments_against_request(
        {"rows": rows}, request, json.dumps(rows), issues=issues,
    )
    assert errors
    assert {"code": "request_field_mismatch", "path": "arguments.rows[0].category"} in issues


def _batch(invocation, outcome):
    return ToolBatchResult(messages=[{
        "role": "tool", "name": invocation.name, "tool_call_id": invocation.id,
        "content": outcome.content,
    }], state_updates=outcome.state_updates, terminal=outcome.terminal)


def _bound_tool_adapter(context, state):
    def context_factory():
        context.messages = state["messages"]
        return context

    def executor(invocations, tool_context):
        invocation = invocations[0]
        outcome = scripts.ExecuteScriptTool().execute(
            args=invocation.arguments, context=tool_context, tool_call_id=invocation.id,
        )
        return _batch(invocation, outcome)

    return CurrentRuntimeToolAdapter(
        tool_name="execute_script", state=state, context_factory=context_factory,
        tool_batch_executor=executor,
    )


def test_generated_result_flows_between_two_actual_bound_script_calls(bound_script):
    (bound_script.root / "scripts" / "consume.md").write_text(
        "---\nkey: consume\nbound_operation: consume_result\nentrypoint: scripts/run.py\n---\n"
        "| name | flag | type | required |\n| --- | --- | --- | --- |\n"
        "| record_json | --record-json | object | true |\n", encoding="utf-8",
    )
    state = {"messages": list(bound_script.context.messages), "results": {}}
    adapter = _bound_tool_adapter(bound_script.context, state)

    async def run():
        created = await adapter.invoke({
            "ontology_id": "knowledge_package_demo", "operation_key": "create_result",
            "arguments": {"record_json": {"calculation_result_id": SYNTHETIC_UUID}},
        })
        return await adapter.invoke({
            "ontology_id": "knowledge_package_demo", "operation_key": "consume_result",
            "arguments": {"record_json": json.loads(created)},
        })

    result = asyncio.run(run())
    assert json.loads(result) == {"calculation_result_id": SYNTHETIC_UUID}
    assert len(bound_script.executions) == 2


def test_changed_values_with_same_validation_cause_stop_and_preserve_safe_reason(bound_script):
    guard = ConsecutiveToolFailureGuard()
    for index in range(2):
        record = {"device_id": "device_synthetic_000{}".format(index)}
        invocation = ToolInvocation(id="call_{}".format(index), name="execute_script", arguments={
            "ontology_id": "knowledge_package_demo", "operation_key": "create_result",
            "arguments": {"record_json": record},
        })
        batch = _batch(invocation, _execute(bound_script, record))
        if index == 0:
            guard.record(invocation, batch)
        else:
            with pytest.raises(AgentLoopStalledError) as raised:
                guard.record(invocation, batch)
    error = internal(JiuwenAsyncAgentRuntime).runtime_error(raised.value)
    assert error.error_code == "AGENT_LOOP_STALLED"
    assert error.retriable is False
    validation = error.details["last_argument_validation"]
    assert validation["correctable"] is True
    assert validation["issues"] == [{"code": "ungrounded_identity", "path": "arguments.record_json.device_id"}]
    assert "device_synthetic" not in error.model_dump_json()
    assert bound_script.executions == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_actual_tool_to_runtime_public_failure_retains_validation(bound_script, wrapped):
    class SyntheticEngine:
        def __init__(self, **_kwargs):
            pass

        async def arun(self, state, **_kwargs):
            adapter = _bound_tool_adapter(bound_script.context, state)
            for index in range(2):
                try:
                    await adapter.invoke({
                        "ontology_id": "knowledge_package_demo", "operation_key": "create_result",
                        "arguments": {"record_json": {"device_id": "device_synthetic_000{}".format(index)}},
                    })
                except AgentLoopStalledError as exc:
                    if wrapped:
                        raise ExceptionGroup("synthetic group", [exc]) from exc
                    raise
            pytest.fail("Repeated invalid tool input must stop the runtime")

    async def run():
        runtime = JiuwenAsyncAgentRuntime(agent_config=SimpleNamespace(), engine_factory=SyntheticEngine)
        context = RuntimeInvocationContext(
            run_id="run_demo", session_id="session_demo", turn_id=1,
            workspace_id="workspace_demo", username="operator_a", query=REQUEST,
        )
        return [event async for event in runtime.astream(context)]

    events = asyncio.run(run())
    terminal = next(event for event in events if event.event_type == "turn.finished")
    public = PublicEventAdapter().adapt(terminal)
    error = public.payload["error"]
    assert error["error_code"] == "AGENT_LOOP_STALLED"
    assert error["details"]["last_argument_validation"]["issues"][0]["path"] == "arguments.record_json.device_id"
    assert "device_synthetic" not in json.dumps(public.payload)
    assert bound_script.executions == []


def test_mixed_unknown_exception_group_keeps_runtime_fallback():
    group = ExceptionGroup("synthetic group", [AgentLoopStalledError(), RuntimeError("synthetic unknown")])
    assert internal(JiuwenAsyncAgentRuntime).runtime_error(group).error_code == "INTERNAL_ERROR"


def test_multiple_known_stall_causes_keep_classification_and_last_reason():
    latest = AgentLoopStalledError(details={"last_argument_validation": {
        "code": "SCRIPT_ARGUMENT_VALIDATION_FAILED",
        "issues": [{"code": "ungrounded_identity", "path": "arguments.device_id"}],
    }})
    group = ExceptionGroup("synthetic group", [AgentLoopStalledError(), latest])
    error = internal(JiuwenAsyncAgentRuntime).runtime_error(group)
    assert error.error_code == "AGENT_LOOP_STALLED"
    assert error.details["last_argument_validation"]["issues"][0]["path"] == "arguments.device_id"


def test_public_validation_details_drop_arbitrary_text_and_invalid_paths():
    error = safe_script_argument_error({
        "code": "SCRIPT_ARGUMENT_VALIDATION_FAILED",
        "message": "synthetic_private_marker", "stderr": "synthetic_private_marker",
        "issues": [{
            "code": "ungrounded_identity", "path": "arguments.user@example.com", "value": "synthetic_private_marker",
        }],
    })
    assert error["issues"] == [{"code": "ungrounded_identity", "path": "arguments"}]
    assert "synthetic_private_marker" not in json.dumps(error)


@pytest.mark.parametrize("issue", [None, {}, {"code": []}, {"code": {}}, {"code": "unknown"}])
def test_malformed_public_validation_details_fail_closed(issue):
    assert safe_script_argument_error({
        "code": "SCRIPT_ARGUMENT_VALIDATION_FAILED", "issues": [issue],
    }) is None


@pytest.mark.parametrize("source", ["previous_turn", "failed_current_turn"])
def test_replay_guard_cannot_bypass_current_argument_validation(bound_script, source):
    record = {"device_id": "device_synthetic_0001"}
    payload = {"ok": source != "failed_current_turn", "record": record}
    messages = _script_messages(payload)
    messages[0]["tool_calls"][0]["function"]["arguments"] = json.dumps({
        "ontology_id": "knowledge_package_demo", "operation_key": "create_result",
        "arguments": {"record_json": record},
    })
    if source == "previous_turn":
        messages.append({"role": "user", "content": "Process a new synthetic request."})
    bound_script.context.messages.extend(messages)
    outcome = _execute(bound_script, record)
    error = json.loads(outcome.content)["error"]
    assert error["issues"][0]["code"] == "ungrounded_identity"
    assert bound_script.executions == []


@pytest.mark.parametrize("request_already_seeded", [False, True])
def test_restored_engine_history_gets_a_current_turn_boundary(bound_script, request_already_seeded):
    from agent.core.jiuwen_engine import JiuwenReactLoopEngine, _build_jiuwen_history_source_messages

    record = {"device_id": "device_synthetic_0001"}
    history = [{"role": "user", "content": "Previous synthetic request."}, *_script_messages(record)]
    request = "Process a new synthetic request."
    state = {"user_request": request, "messages": list(history)}
    if request_already_seeded:
        state["messages"].append({"role": "user", "content": request})
    engine = object.__new__(JiuwenReactLoopEngine)
    internal(engine).seed_initial_messages(state, system_prompt="Synthetic system instruction.")
    assert state["messages"] == [*history, {"role": "user", "content": request}]
    assert _build_jiuwen_history_source_messages(state) == history
    bound_script.context.messages = state["messages"]
    error = json.loads(_execute(bound_script, record).content)["error"]
    assert error["issues"][0]["code"] == "ungrounded_identity"
    assert bound_script.executions == []
