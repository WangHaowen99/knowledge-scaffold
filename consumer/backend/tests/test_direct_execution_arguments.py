"""Regression tests for direct-execution script argument normalization."""

from __future__ import annotations

import pytest
import json
from types import SimpleNamespace

import agent.direct_execution.router as direct_router_module
import agent.direct_execution.runner as direct_runner_module
from agent.direct_execution.arguments import (
    build_direct_cli_args,
    extract_business_placeholder_keys,
    extract_runtime_requirements,
    runtime_context_field,
)
from agent.direct_execution.models import RouteDecision, RuntimeContext
from agent.direct_execution.router import OperationDirectRouter
from agent.tools.script_parameters import is_runtime_parameter, runtime_parameter_field


@pytest.mark.parametrize(
    ("parameter_type", "value", "expected"),
    [
        ("bool", True, ["--enabled", "true"]),
        ("bool", False, ["--enabled", "false"]),
        ("boolean", True, ["--enabled", "true"]),
        ("boolean", False, ["--enabled", "false"]),
    ],
)
def test_value_style_boolean_keeps_explicit_value(parameter_type, value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "enabled",
                "flag": "--enabled",
                "required": False,
                "type": parameter_type,
            }
        ],
        business_arguments={"enabled": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, ["--verbose"]), (False, [])],
)
def test_store_true_boolean_uses_flag_presence(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "verbose",
                "flag": "--verbose",
                "required": False,
                "type": "boolean",
                "action": "store_true",
            }
        ],
        business_arguments={"verbose": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, []), (False, ["--cache"])],
)
def test_store_false_boolean_uses_flag_presence(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "cache",
                "flag": "--cache",
                "required": False,
                "type": "bool",
                "action": "store_false",
            }
        ],
        business_arguments={"cache": value},
    )

    assert (args, missing, error) == (expected, [], None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, ["--json", "true"]), (False, ["--json", "false"])],
)
def test_json_boolean_keeps_value_convention_without_explicit_action(value, expected):
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "json",
                "flag": "--json",
                "required": False,
                "type": "boolean",
            }
        ],
        business_arguments={"json": value},
    )

    assert (args, missing, error) == (expected, [], None)


def test_direct_runtime_parameter_cannot_be_overridden_by_business_arguments():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "required": True,
                "type": "string",
                "runtime": True,
            },
            {"name": "payload", "flag": "--payload", "required": True, "type": "string"},
        ],
        business_arguments={"session_id": "forged", "payload": "value"},
        runtime_context=RuntimeContext(
            session_id="real-session",
            run_id="run-1",
            turn_id=1,
            user_id="user-1",
            workspace_id="workspace-1",
            selected_ontology_id="ontology-1",
        ),
    )

    assert (args, missing, error) == (
        ["--session-id", "real-session", "--payload", "value"],
        [],
        None,
    )


def test_direct_missing_runtime_context_is_system_error_not_missing_business_input():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {"name": "session_id", "flag": "--session-id", "required": True, "type": "string"},
        ],
        business_arguments={"session_id": "forged"},
        runtime_context=None,
    )

    assert args == []
    assert missing == []
    assert error == "Runtime context missing 'session_id'"


def test_direct_object_and_array_parameters_are_json_encoded():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {"name": "filters", "flag": "--filters", "required": True, "type": "object"},
            {"name": "ids", "flag": "--ids", "required": True, "type": "array"},
        ],
        business_arguments={"filters": {"plant": "A"}, "ids": [1, 2]},
    )

    assert missing == []
    assert error is None
    assert json.loads(args[1]) == {"plant": "A"}
    assert json.loads(args[3]) == [1, 2]


def test_direct_optional_runtime_parameter_can_be_omitted_without_context():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "required": False,
                "type": "string",
                "runtime": True,
            }
        ],
        business_arguments={},
        runtime_context=None,
    )

    assert (args, missing, error) == ([], [], None)


def test_structured_runtime_requirement_is_checked_with_canonical_context_field():
    router = OperationDirectRouter.__new__(OperationDirectRouter)
    context = RuntimeContext(
        session_id=None,  # type: ignore[arg-type]
        run_id="run-1",
        turn_id=1,
    )

    assert extract_runtime_requirements(
        script_parameters=[
            {"name": "session_id", "flag": "--session-id", "required": True}
        ]
    ) == ["session_id"]
    assert router._check_runtime_requirements(["session_id"], context) == ["session_id"]


def test_direct_business_parameter_is_not_overridden_by_runtime_context():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "session_id",
                "flag": "--session-id",
                "required": True,
                "type": "string",
                "source": "business",
            }
        ],
        business_arguments={"session_id": "business-session"},
        runtime_context=RuntimeContext(
            session_id="runtime-session",
            run_id="run-1",
            turn_id=1,
        ),
    )

    assert (args, missing, error) == (
        ["--session-id", "business-session"],
        [],
        None,
    )


def test_direct_legacy_runtime_parameter_cannot_be_overridden_by_duplicate_user_flag():
    args, missing, error = build_direct_cli_args(
        script_args=["--session-id", "<runtime.session_id>"],
        business_arguments={"session_id": "forged-session"},
        runtime_context=RuntimeContext(
            session_id="runtime-session",
            run_id="run-1",
            turn_id=1,
        ),
    )

    assert (args, missing, error) == (
        ["--session-id", "runtime-session"],
        [],
        None,
    )


def test_optional_runtime_parameter_is_not_a_direct_routing_requirement():
    assert extract_runtime_requirements(
        script_parameters=[
            {
                "name": "workspace_id",
                "flag": "--workspace-id",
                "required": False,
                "runtime": True,
            }
        ]
    ) == []


def test_structured_business_key_extraction_preserves_all_supported_aliases():
    keys = extract_business_placeholder_keys(
        script_parameters=[
            {
                "name": "batch_code_cn",
                "flag": "--batch-code",
                "aliases": ["batch_code", "batch-code-alias"],
                "placeholder": "<batch-code-placeholder>",
            }
        ]
    )

    assert {
        "batch_code_cn",
        "--batch-code",
        "batch-code",
        "batch_code",
        "batch-code-alias",
        "batch_code_alias",
        "batch-code-placeholder",
        "batch_code_placeholder",
    }.issubset(keys)


def test_legacy_business_key_extraction_preserves_flag_and_placeholder_aliases():
    keys = extract_business_placeholder_keys(
        script_args=["--batch-code", "<batch-code>"],
    )

    assert {
        "--batch-code",
        "batch-code",
        "batch_code",
    }.issubset(keys)


def test_direct_structured_business_placeholder_resolves_argument_value():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "batch_code_cn",
                "flag": "--flight",
                "placeholder": "<batch_code>",
                "required": True,
                "type": "string",
            }
        ],
        business_arguments={"batch_code": "BATCH123"},
    )

    assert (args, missing, error) == (["--flight", "BATCH123"], [], None)


def test_direct_explicit_runtime_ontology_uses_selected_ontology_context():
    args, missing, error = build_direct_cli_args(
        script_parameters=[
            {
                "name": "ontology_id",
                "flag": "--ontology-id",
                "runtime": True,
                "required": True,
                "type": "string",
            }
        ],
        business_arguments={"ontology_id": "forged-ontology"},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            selected_ontology_id="selected-ontology",
        ),
    )

    assert (args, missing, error) == (
        ["--ontology-id", "selected-ontology"],
        [],
        None,
    )


def test_explicit_runtime_user_id_uses_backend_context():
    parameter = {
        "name": "user_id",
        "flag": "--user-id",
        "runtime": True,
        "required": True,
    }

    assert is_runtime_parameter(parameter) is True
    assert runtime_parameter_field(parameter) == "user_id"

    args, missing, error = build_direct_cli_args(
        script_parameters=[parameter],
        business_arguments={"user_id": "forged-user"},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            user_id="real-user",
        ),
    )

    assert (args, missing, error) == (["--user-id", "real-user"], [], None)


@pytest.mark.parametrize("runtime_key", ["session-id", "session_id"])
def test_runtime_key_aliases_share_one_canonical_context_mapping(runtime_key):
    assert runtime_context_field(runtime_key) == "session_id"
    assert extract_runtime_requirements(
        script_args=["--session-id", "<runtime.{}>".format(runtime_key)],
    ) == ["session_id"]


@pytest.mark.parametrize("runtime_key", ["ontology-id", "ontology_id"])
def test_runtime_ontology_aliases_resolve_to_selected_ontology(runtime_key):
    assert runtime_context_field(runtime_key) == "selected_ontology_id"


@pytest.mark.parametrize("disabled", [False, "false", "no", "0", "off"])
def test_explicit_false_runtime_marker_disables_name_convention(disabled):
    parameter = {
        "name": "session_id",
        "flag": "--session-id",
        "runtime": disabled,
    }

    assert is_runtime_parameter(parameter) is False
    assert runtime_parameter_field(parameter) is None


def test_direct_router_keeps_alias_argument(monkeypatch):
    script_entry = {
        "operation_key": "run_batch",
        "arguments": [],
        "parameters": [
            {
                "name": "batch_code_cn",
                "flag": "--batch-code",
                "aliases": ["batch_code"],
                "required": True,
                "type": "string",
            }
        ],
    }
    monkeypatch.setattr(
        direct_router_module,
        "find_best_script_operation",
        lambda **kwargs: (script_entry, "run_batch", 1.0),
    )
    router = OperationDirectRouter(
        SimpleNamespace(enabled=True, selected_ontology_only=True, confidence_threshold=0.8)
    )

    decision = router.route(
        user_request="run_batch",
        context={"selected_ontology_id": "demo"},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            selected_ontology_id="demo",
        ),
        arguments={"batch_code": "BATCH123"},
    )

    assert decision.route == "direct_script"
    assert decision.business_arguments == {"batch_code": "BATCH123"}


def test_direct_router_allows_missing_optional_runtime_context(monkeypatch):
    script_entry = {
        "operation_key": "run_optional",
        "arguments": [],
        "parameters": [
            {
                "name": "workspace_id",
                "flag": "--workspace-id",
                "runtime": True,
                "required": False,
                "type": "string",
            }
        ],
    }
    monkeypatch.setattr(
        direct_router_module,
        "find_best_script_operation",
        lambda **kwargs: (script_entry, "run_optional", 1.0),
    )
    router = OperationDirectRouter(
        SimpleNamespace(enabled=True, selected_ontology_only=True, confidence_threshold=0.8)
    )

    decision = router.route(
        user_request="run_optional",
        context={"selected_ontology_id": "demo"},
        runtime_context=RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            workspace_id=None,
            selected_ontology_id="demo",
        ),
    )

    assert decision.route == "direct_script"


def test_direct_runner_rejects_script_outside_current_ontology_scripts(
    monkeypatch,
    tmp_path,
):
    project_root = tmp_path / "runtime"
    ontology_dir = project_root / "workspaces" / "workspace-a" / "ontologies" / "demo"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    outside_script = project_root / "workspaces" / "workspace-b" / "outside.py"
    outside_script.parent.mkdir(parents=True)
    outside_script.write_text("print('outside')\n", encoding="utf-8")

    monkeypatch.setattr(
        direct_runner_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="demo", ontology_dir=str(ontology_dir)),
            None,
        ),
    )
    executed = []
    monkeypatch.setattr(
        direct_runner_module,
        "handle_execute_script_path",
        lambda **kwargs: executed.append(kwargs) or "outside-executed",
    )
    decision = RouteDecision(
        route="direct_script",
        reason="test",
        confidence=1.0,
        ontology_id="demo",
        operation_key="run",
        script_entry={
            "_script_path": "../../../../workspace-b/outside.py",
            "_resolved_script_path": str(outside_script),
            "arguments": [],
            "parameters": [],
        },
    )

    _events, result = direct_runner_module.run_direct_script(
        decision,
        RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            workspace_id="workspace-a",
            selected_ontology_id="demo",
        ),
        project_root=str(project_root),
    )

    assert result.status == "error"
    assert "outside ontology scripts" in result.output.lower()
    assert executed == []


def test_direct_runner_redacts_unexpected_exception_from_result_and_events(
    monkeypatch,
    tmp_path,
):
    project_root = tmp_path / "runtime"
    ontology_dir = project_root / "ontologies" / "demo"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "operation.py"
    script.write_text("print('unused')\n", encoding="utf-8")

    monkeypatch.setattr(
        direct_runner_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="demo", ontology_dir=str(ontology_dir)),
            None,
        ),
    )
    monkeypatch.setattr(direct_runner_module, "_build_script_env_overrides", lambda **kwargs: {})
    monkeypatch.setattr(
        direct_runner_module,
        "handle_execute_script_path",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError("ACCESS_TOKEN=direct-runner-secret")
        ),
    )
    decision = RouteDecision(
        route="direct_script",
        reason="test",
        confidence=1.0,
        ontology_id="demo",
        operation_key="run",
        script_entry={
            "_script_path": "scripts/operation.py",
            "arguments": [],
            "parameters": [],
        },
    )

    events, result = direct_runner_module.run_direct_script(
        decision,
        RuntimeContext(
            session_id="session-1",
            run_id="run-1",
            turn_id=1,
            selected_ontology_id="demo",
        ),
        project_root=str(project_root),
    )

    serialized_events = json.dumps(events, ensure_ascii=False)
    assert result.status == "error"
    assert result.output == "Error: Direct script execution failed."
    assert result.error == "direct_script_execution_failed"
    assert "direct-runner-secret" not in serialized_events
    assert "direct-runner-secret" not in result.output
    assert "direct-runner-secret" not in str(result.error)
