"""Regression tests for execute_script boolean CLI argument semantics."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

import agent.tools.execute_script as execute_script_module
from agent.tools.execute_script import _build_cli_args_from_parameters


@pytest.mark.parametrize(
    ("parameter", "value", "expected"),
    [
        ({"name": "enabled", "flag": "--enabled", "type": "bool"}, True, ["--enabled", "true"]),
        ({"name": "enabled", "flag": "--enabled", "type": "boolean"}, False, ["--enabled", "false"]),
        (
            {"name": "verbose", "flag": "--verbose", "type": "boolean", "action": "store_true"},
            True,
            ["--verbose"],
        ),
        (
            {"name": "verbose", "flag": "--verbose", "type": "boolean", "action": "store_true"},
            False,
            [],
        ),
        (
            {"name": "cache", "flag": "--cache", "type": "bool", "action": "store_false"},
            True,
            [],
        ),
        (
            {"name": "cache", "flag": "--cache", "type": "bool", "action": "store_false"},
            False,
            ["--cache"],
        ),
        # The parameter name must not change argparse's value-style semantics.
        ({"name": "json", "flag": "--json", "type": "boolean"}, True, ["--json", "true"]),
        ({"name": "json", "flag": "--json", "type": "boolean"}, False, ["--json", "false"]),
    ],
)
def test_execute_script_builds_boolean_cli_arguments(parameter, value, expected):
    parameter = {**parameter, "required": False}

    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[parameter],
        arguments={parameter["name"]: value},
    )

    assert (args, missing, invalid) == (expected, [], [])


def test_structured_runtime_parameters_use_backend_context_over_user_value():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "type": "string",
                "required": True,
                "runtime": True,
            },
            {"name": "payload", "flag": "--payload", "type": "string", "required": True},
        ],
        arguments={"session_id": "forged-session", "payload": "business-value"},
        runtime_context={"session_id": "real-session"},
    )

    assert (args, missing, invalid) == (
        ["--session-id", "real-session", "--payload", "business-value"],
        [],
        [],
    )


def test_structured_runtime_parameters_are_not_user_arguments_when_context_missing():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "type": "string",
                "required": True,
                "runtime": True,
            },
        ],
        arguments={"session_id": "forged-session"},
        runtime_context=None,
    )

    assert args == []
    assert missing == []
    assert invalid == ["Runtime context missing 'session_id'"]


def test_ontology_id_named_business_parameter_is_not_runtime_without_explicit_marker():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {"name": "ontology_id", "flag": "--ontology-id", "type": "string", "required": True},
        ],
        arguments={"ontology_id": "business-ontology"},
        runtime_context={"ontology_id": "real-ontology"},
    )

    assert (args, missing, invalid) == (["--ontology-id", "business-ontology"], [], [])


def test_object_and_array_parameters_are_json_encoded():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {"name": "filters", "flag": "--filters", "type": "object", "required": True},
            {"name": "ids", "flag": "--ids", "type": "array", "required": True},
        ],
        arguments={"filters": {"plant": "A", "enabled": True}, "ids": [1, 2]},
    )

    assert missing == []
    assert invalid == []
    assert args[0] == "--filters"
    assert args[2] == "--ids"
    import json

    assert json.loads(args[1]) == {"plant": "A", "enabled": True}
    assert json.loads(args[3]) == [1, 2]


def test_optional_runtime_parameter_can_be_omitted_without_context():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "type": "string",
                "required": False,
                "runtime": True,
            },
        ],
        arguments={},
        runtime_context=None,
    )

    assert (args, missing, invalid) == ([], [], [])


def test_legacy_runtime_placeholder_cannot_fall_back_to_user_argument():
    args, missing, invalid = execute_script_module._build_cli_args(
        script_args=["--session-id", "<runtime.session_id>"],
        script_parameters=[],
        arguments={"session_id": "forged"},
        runtime_context=None,
    )

    assert args == []
    assert missing == []
    assert invalid == ["Runtime context missing 'session_id'"]


def test_legacy_runtime_user_id_uses_backend_context_when_available():
    args, missing, invalid = execute_script_module._build_cli_args(
        script_args=["--user-id", "<runtime.user_id>"],
        script_parameters=[],
        arguments={"user_id": "forged-user"},
        runtime_context={"user_id": "backend-user"},
    )

    assert args == ["--user-id", "backend-user"]
    assert missing == []
    assert invalid == []


def test_structured_business_placeholder_resolves_argument_value():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {
                "name": "batch_code_cn",
                "flag": "--batch",
                "placeholder": "<batch_code>",
                "required": True,
                "type": "string",
            }
        ],
        arguments={"batch_code": "BATCH123"},
    )

    assert (args, missing, invalid) == (["--batch", "BATCH123"], [], [])


def test_execute_script_tool_injects_selected_operation_ontology_as_runtime_ontology(
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script",
        lambda **kwargs: captured.update(kwargs) or "ok",
    )
    context = SimpleNamespace(
        project_root="/runtime/workspaces/demo",
        messages=[],
        run_id="run-1",
        session_id="session-1",
        turn_id=2,
        user_id="user-1",
        workspace_id="workspace-1",
        selected_ontology_id="demo",
        selected_ontology_version=None,
        caller="main",
        agent_config=SimpleNamespace(execute_script_timeout=30),
    )

    outcome = execute_script_module.ExecuteScriptTool().execute(
        args={"ontology_id": "demo", "operation_key": "run", "arguments": {}},
        context=context,
    )

    assert outcome.content == "ok"
    assert captured["runtime_context"]["ontology_id"] == "demo"


def test_explicit_runtime_user_id_uses_backend_context():
    args, missing, invalid = _build_cli_args_from_parameters(
        script_parameters=[
            {
                "name": "user_id",
                "flag": "--user-id",
                "runtime": True,
                "required": True,
                "type": "string",
            }
        ],
        arguments={"user_id": "forged-user"},
        runtime_context={"user_id": "real-user"},
    )

    assert args == ["--user-id", "real-user"]
    assert missing == []
    assert invalid == []
