"""Regression tests for request-scoped ``execute_script`` schemas."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import json

import pytest

from agent.core.jiuwen.tools import get_jiuwen_tool_infos
from agent.core.shared.tool_context import build_tool_context
from agent.core.jiuwen_engine import JiuwenReactLoopEngine
import agent.tools.execute_script as execute_script_module
import ontology.ontology_catalog as ontology_catalog_module
from agent.tools.execute_script import handle_execute_script
from agent.tools.registry import get_tool_schema, get_tools_schema, validate_tool_arguments
from config.settings import AgentConfig
from ontology.ontology_catalog import find_script_entry
from workspace.workspaces import get_workspace_paths


def test_execute_script_runtime_context_uses_backend_identity(monkeypatch):
    captured = {}

    def fake_handle_execute_script(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(execute_script_module, "handle_execute_script", fake_handle_execute_script)
    context = build_tool_context(
        {
            "run_id": "run-runtime",
            "session_id": "session-runtime",
            "user_id": "user-runtime",
            "current_turn_id": 4,
            "messages": [],
            "user_context": {"selected_ontology_id": "selected-ontology"},
        },
        memory_manager=None,
        agent_config=SimpleNamespace(execute_script_timeout=30),
        caller="main",
        agent_iteration=1,
        workspace_id="workspace-runtime",
        project_root=None,
    )

    outcome = execute_script_module.ExecuteScriptTool().execute(
        args={
            "ontology_id": "model-provided-ontology",
            "operation_key": "run",
            "arguments": {},
        },
        context=context,
    )

    assert outcome.content == "ok"
    assert captured["runtime_context"] == {
        "session_id": "session-runtime",
        "run_id": "run-runtime",
        "turn_id": "4",
        "user_id": "user-runtime",
        "workspace_id": "workspace-runtime",
        "selected_ontology_id": "selected-ontology",
        "ontology_id": "selected-ontology",
    }


def _write_ontology(
    workspace_id: str,
    ontology_id: str,
    operations: list[tuple[str, list[dict[str, object]]]],
) -> None:
    ontology_dir = get_workspace_paths(workspace_id).ontologies_dir / ontology_id
    version_dir = ontology_dir / "versions" / "1.0.0"
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: {}".format(ontology_id),
                "name: {}".format(ontology_id),
                "description: Dynamic schema test ontology",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version_dir / "index.md").write_text("# {}\n".format(ontology_id), encoding="utf-8")

    for index, (operation_key, parameters) in enumerate(operations, start=1):
        script_key = "script_{}".format(index)
        (scripts_dir / "{}.py".format(script_key)).write_text("print('ok')\n", encoding="utf-8")
        rows = [
            "| name | flag | type | required | multiple | description | runtime |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for parameter in parameters:
            name = str(parameter["name"])
            rows.append(
                "| {} | --{} | {} | {} | {} | {} | {} |".format(
                    name,
                    name.replace("_", "-"),
                    parameter.get("type", "string"),
                    str(parameter.get("required", True)).lower(),
                    str(parameter.get("multiple", False)).lower(),
                    parameter.get("description", ""),
                    str(parameter.get("runtime", False)).lower(),
                )
            )
        (scripts_dir / "{}.md".format(script_key)).write_text(
            "\n".join(
                [
                    "---",
                    "resource: script",
                    "key: {}".format(script_key),
                    "title: {}".format(operation_key),
                    "entrypoint: {}.py".format(script_key),
                    "bound_operation: {}".format(operation_key),
                    "---",
                    "",
                    *rows,
                ]
            ),
            encoding="utf-8",
        )


def _write_scriptless_ontology(workspace_id: str, ontology_id: str) -> None:
    ontology_dir = get_workspace_paths(workspace_id).ontologies_dir / ontology_id
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True, exist_ok=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: {}".format(ontology_id),
                "name: {}".format(ontology_id),
                "description: Scriptless test ontology",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "language: zh-CN",
            ]
        ),
        encoding="utf-8",
    )
    (version_dir / "index.md").write_text("# {}\n".format(ontology_id), encoding="utf-8")


def _execute_script_schema(schemas):
    return next(schema for schema in schemas if schema["function"]["name"] == "execute_script")


def _execute_script_tool_info(tool_infos):
    return next(info for info in tool_infos if info["name"] == "execute_script")


def _agent_config() -> AgentConfig:
    return AgentConfig(
        llm_connection_max_retries=3,
        main_agent_max_iterations=50,
        sub_agent_max_iterations=5,
        sub_agent_max_concurrency=4,
        sub_agent_max_depth=2,
        parallel_default_max_workers=10,
        context_max_rounds=10,
    )


def test_selected_ontology_builds_script_parameter_schema_without_global_mutation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    common = {
        "name": "batches_json",
        "type": "string",
        "required": True,
        "description": "JSON batches payload",
    }
    _write_ontology(
        "workspace-a",
        "demo",
        [
            (
                "split_material",
                [common, {"name": "month", "type": "integer", "required": True}],
            ),
            (
                "check_capacity",
                [
                    common,
                    {
                        "name": "tank_names",
                        "type": "string",
                        "required": False,
                        "multiple": True,
                        "description": "Tank names",
                    },
                ],
            ),
        ],
    )

    dynamic = _execute_script_schema(
        get_tools_schema(
            "main",
            selected_ontology_id="demo",
            workspace_id="workspace-a",
        )
    )["function"]["parameters"]
    arguments = dynamic["properties"]["arguments"]

    assert dynamic["properties"]["ontology_id"]["enum"] == ["demo"]
    assert set(dynamic["properties"]["operation_key"]["enum"]) == {
        "split_material",
        "check_capacity",
    }
    assert "arguments" in dynamic["required"]
    assert arguments["properties"]["batches_json"]["type"] == "string"
    assert arguments["properties"]["month"]["type"] == "integer"
    assert arguments["properties"]["tank_names"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tank names",
    }
    assert arguments["required"] == ["batches_json"]
    assert arguments["additionalProperties"] is False

    static_arguments = get_tool_schema("execute_script")["function"]["parameters"]["properties"]["arguments"]
    assert "batches_json" not in (static_arguments.get("properties") or {})


def test_jiuwen_and_token_schemas_use_selected_ontology_and_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_ontology(
        "workspace-a",
        "demo",
        [("run", [{"name": "workspace_input", "type": "string", "required": True}])],
    )
    state = {
        "workspace_id": "workspace-a",
        "user_context": {"selected_ontology_id": "demo", "agent_mode": "quick"},
    }
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())

    token_parameters = _execute_script_schema(
        engine._tools_schema_for_state(state, caller="main")
    )["function"]["parameters"]
    tool_info_parameters = _execute_script_tool_info(
        engine._jiuwen_tool_infos_for_state(state, caller="main")
    )["parameters"]

    assert tool_info_parameters == token_parameters
    assert "workspace_input" in tool_info_parameters["properties"]["arguments"]["properties"]


def test_dynamic_schema_is_isolated_by_workspace_under_concurrency(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_ontology(
        "workspace-a",
        "shared-ontology",
        [("run", [{"name": "alpha_input", "required": True}])],
    )
    _write_ontology(
        "workspace-b",
        "shared-ontology",
        [("run", [{"name": "beta_input", "required": True}])],
    )

    def argument_names(workspace_id: str) -> set[str]:
        parameters = _execute_script_schema(
            get_tools_schema(
                "main",
                selected_ontology_id="shared-ontology",
                workspace_id=workspace_id,
            )
        )["function"]["parameters"]
        jiuwen_parameters = _execute_script_tool_info(
            get_jiuwen_tool_infos(
                "main",
                selected_ontology_id="shared-ontology",
                workspace_id=workspace_id,
            )
        )["parameters"]
        assert jiuwen_parameters == parameters
        return set(parameters["properties"]["arguments"]["properties"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(argument_names, "workspace-a")
        future_b = executor.submit(argument_names, "workspace-b")

    assert future_a.result() == {"alpha_input"}
    assert future_b.result() == {"beta_input"}


def test_workspace_fallback_aggregates_only_scripted_ontologies(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_ontology(
        "workspace-target",
        "ontology-alpha",
        [("run-alpha", [{"name": "alpha_input", "required": True}])],
    )
    _write_ontology(
        "workspace-target",
        "ontology-beta",
        [("run-beta", [{"name": "beta_input", "required": True}])],
    )
    _write_scriptless_ontology("workspace-target", "ontology-empty")
    _write_ontology(
        "workspace-other",
        "ontology-gamma",
        [("run-gamma", [{"name": "gamma_input", "required": True}])],
    )

    parameters = _execute_script_schema(
        get_tools_schema("main", workspace_id="workspace-target")
    )["function"]["parameters"]

    assert set(parameters["properties"]["ontology_id"]["enum"]) == {
        "ontology-alpha",
        "ontology-beta",
    }
    assert set(parameters["properties"]["operation_key"]["enum"]) == {
        "run-alpha",
        "run-beta",
    }
    assert set(parameters["properties"]["arguments"]["properties"]) == {
        "alpha_input",
        "beta_input",
    }


def test_zero_argument_operation_publishes_closed_empty_arguments_schema(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_ontology("workspace-a", "demo", [("run", [])])

    schema = _execute_script_schema(
        get_tools_schema(
            "main",
            selected_ontology_id="demo",
            workspace_id="workspace-a",
        )
    )
    arguments = schema["function"]["parameters"]["properties"]["arguments"]

    assert arguments["properties"] == {}
    assert arguments["required"] == []
    assert arguments["additionalProperties"] is False
    assert validate_tool_arguments(
        "execute_script",
        {"ontology_id": "demo", "operation_key": "run", "arguments": {}},
        tool_schema=schema,
    ) is None


def test_execute_script_requires_arguments_but_accepts_empty_object():
    missing = validate_tool_arguments(
        "execute_script",
        {"ontology_id": "demo", "operation_key": "run"},
    )
    present = validate_tool_arguments(
        "execute_script",
        {"ontology_id": "demo", "operation_key": "run", "arguments": {}},
    )

    assert missing is not None and "arguments" in missing
    assert present is None


def test_execute_script_rejects_non_object_arguments():
    error = validate_tool_arguments(
        "execute_script",
        {"ontology_id": "demo", "operation_key": "run", "arguments": "not-an-object"},
    )

    assert error is not None
    assert "arguments.arguments must be object" in error


def test_dynamic_schema_deduplicates_parameter_descriptions():
    merged = execute_script_module._merge_script_parameter_property_schema(
        None,
        {"type": "string", "description": "First format"},
    )
    merged = execute_script_module._merge_script_parameter_property_schema(
        merged,
        {"type": "string", "description": "Second format"},
    )
    merged = execute_script_module._merge_script_parameter_property_schema(
        merged,
        {"type": "string", "description": "First format"},
    )

    assert merged["description"] == "First format / Second format"


def test_dynamic_schema_hides_backend_runtime_parameters(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_ontology(
        "workspace-runtime",
        "demo",
        [
            (
                "run",
                [
                    {"name": "session_id", "type": "string", "required": True, "runtime": True},
                    {"name": "payload", "type": "string", "required": True},
                ],
            )
        ],
    )

    parameters = _execute_script_schema(
        get_tools_schema(
            "main",
            selected_ontology_id="demo",
            workspace_id="workspace-runtime",
        )
    )["function"]["parameters"]
    argument_schema = parameters["properties"]["arguments"]

    assert "session_id" not in argument_schema["properties"]
    assert argument_schema["required"] == ["payload"]


def test_find_script_entry_can_require_bound_operation():
    catalog = [
        {"operation_key": "helper", "script_key": "helper", "_is_bound_operation": False},
        {"operation_key": "public", "script_key": "public", "_is_bound_operation": True},
    ]

    assert find_script_entry(catalog, "helper", bound_only=True) is None
    assert find_script_entry(catalog, "public", bound_only=True)["operation_key"] == "public"


def test_handle_execute_script_rejects_unbound_operation_before_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="demo", ontology_dir=str(tmp_path)),
            None,
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda _ontology_dir: [
            {
                "operation_key": "internal_helper",
                "script_key": "internal_helper",
                "_is_bound_operation": False,
                "_script_path": "scripts/internal_helper.py",
                "arguments": [],
                "parameters": [],
            }
        ],
    )
    executed = []
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: executed.append(kwargs) or "ok",
    )

    result = handle_execute_script(
        ontology_id="demo",
        operation_key="internal_helper",
        arguments={},
        base_path=tmp_path,
    )

    assert result.startswith(
        "Error: Operation 'internal_helper' in ontology 'demo' has no registered script implementation"
    )
    assert "not a bound operation" in result
    assert executed == []


def test_handle_execute_script_rejects_script_outside_ontology_scripts(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    (ontology_dir / "scripts").mkdir(parents=True)
    outside_script = tmp_path / "outside.py"
    outside_script.write_text("print('outside')\n", encoding="utf-8")
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="demo", ontology_dir=str(ontology_dir)),
            None,
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda _ontology_dir: [
            {
                "operation_key": "run",
                "script_key": "run",
                "_is_bound_operation": True,
                "_script_path": "../outside.py",
                "arguments": [],
                "parameters": [],
            }
        ],
    )
    executed = []
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: executed.append(kwargs) or "outside-executed",
    )

    result = handle_execute_script(
        ontology_id="demo",
        operation_key="run",
        arguments={},
        base_path=tmp_path,
    )

    assert result == "Error: Registered script for operation 'run' is outside ontology scripts directory."
    assert executed == []


def test_handle_execute_script_fails_before_required_business_data_script(
    monkeypatch,
    tmp_path,
):
    ontology_dir = tmp_path / "ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "operation.py").write_text("print('unused')\n", encoding="utf-8")
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="knowledge_package_demo", ontology_dir=str(ontology_dir)),
            None,
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda _ontology_dir: [
            {
                "operation_key": "run",
                "script_key": "run",
                "_is_bound_operation": True,
                "_script_path": "scripts/operation.py",
                "arguments": [],
                "parameters": [],
                "data_access": "workspace_provider",
            }
        ],
    )
    monkeypatch.setattr(
        execute_script_module,
        "resolve_business_data_source",
        lambda **kwargs: (_ for _ in ()).throw(
            execute_script_module.BusinessDataConnectionError(
                "synthetic unavailable"
            )
        ),
    )
    executed = []
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: executed.append(kwargs) or "unexpected",
    )

    result = handle_execute_script(
        ontology_id="knowledge_package_demo",
        operation_key="run",
        arguments={},
        base_path=tmp_path,
        workspace_id="workspace_demo",
    )

    assert result == (
        "Error: [BUSINESS_DATA_CONNECTION_UNAVAILABLE] "
        "Required business data connection is unavailable."
    )
    assert executed == []


@pytest.mark.parametrize("error_type", [OSError, ValueError, TimeoutError])
def test_required_connection_error_preserves_internal_cause(monkeypatch, error_type):
    root_error = error_type("synthetic connection failure")
    monkeypatch.setattr(
        execute_script_module,
        "resolve_business_data_source",
        lambda **_kwargs: (_ for _ in ()).throw(root_error),
    )

    with pytest.raises(execute_script_module.BusinessDataConnectionError) as exc:
        execute_script_module._build_script_env_overrides(
            workspace_id="workspace_demo",
            ontology_id="knowledge_package_demo",
            required=True,
        )

    assert str(exc.value) == "Required business data connection is unavailable."
    assert exc.value.__cause__ is root_error
    assert "synthetic connection failure" not in str(exc.value)


def test_handle_execute_script_discards_success_stderr(monkeypatch, tmp_path):
    ontology_dir = tmp_path / "ontology"
    scripts_dir = ontology_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "operation.py").write_text("print('unused')\n", encoding="utf-8")
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *args, **kwargs: (
            SimpleNamespace(id="knowledge_package_demo", ontology_dir=str(ontology_dir)),
            None,
        ),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda _ontology_dir: [
            {
                "operation_key": "run",
                "script_key": "run",
                "_is_bound_operation": True,
                "_script_path": "scripts/operation.py",
                "arguments": [],
                "parameters": [],
                "data_access": "none",
            }
        ],
    )
    captured = {}
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **kwargs: captured.update(kwargs) or "ok",
    )

    result = handle_execute_script(
        ontology_id="knowledge_package_demo",
        operation_key="run",
        arguments={},
        base_path=tmp_path,
        workspace_id="workspace_demo",
    )

    assert result == "ok"
    assert captured["include_stderr_on_success"] is False


@pytest.mark.parametrize(
    "entrypoint",
    [
        r"..\outside.py",
        r"scripts\..\outside.py",
        r"C:outside.py",
        "/absolute/outside.py",
    ],
)
def test_normalize_script_entrypoint_rejects_cross_platform_escapes(entrypoint):
    assert ontology_catalog_module._normalize_script_entrypoint(entrypoint) is None


def test_normalize_script_entrypoint_accepts_windows_separators_inside_scripts():
    assert (
        ontology_catalog_module._normalize_script_entrypoint(r"subdir\run.py")
        == "scripts/subdir/run.py"
    )


@pytest.mark.parametrize("link_kind", ["scripts_root", "script_file"])
def test_resolve_ontology_script_path_rejects_symlink_escape(tmp_path, link_kind):
    ontology_dir = tmp_path / "ontology"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_script = outside_dir / "run.py"
    outside_script.write_text("print('outside')\n", encoding="utf-8")

    try:
        if link_kind == "scripts_root":
            (ontology_dir / "scripts").parent.mkdir(parents=True, exist_ok=True)
            (ontology_dir / "scripts").symlink_to(outside_dir, target_is_directory=True)
        else:
            scripts_dir = ontology_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "run.py").symlink_to(outside_script)
    except OSError as exc:
        pytest.skip("symlink creation is unavailable: {}".format(exc))

    assert ontology_catalog_module.resolve_ontology_script_path(
        ontology_dir,
        "scripts/run.py",
    ) is None


def test_script_parameter_parser_preserves_explicit_runtime_false():
    parameters = ontology_catalog_module._parse_script_parameters_from_markdown(
        "\n".join(
            [
                "| name | flag | type | required | runtime |",
                "| --- | --- | --- | --- | --- |",
                "| session_id | --session-id | string | true | false |",
            ]
        )
    )

    assert parameters[0]["runtime"] is False
