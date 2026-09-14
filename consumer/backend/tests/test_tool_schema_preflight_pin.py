# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Regression tests for request-admitted dynamic tool products."""

from __future__ import annotations

from internal_access import internal
import asyncio
import json
import threading
from types import SimpleNamespace

import agent.core.shared.solver_route_guard  # noqa: F401
import agent.runtime.preflight as preflight_module
import agent.tools.execute_script as execute_script_module
import agent.tools.registry as tool_registry
import agent.tools.solver as solver_module
import pytest
from agent.core.jiuwen_engine import JiuwenReactLoopEngine
from agent.core.shared.tool_context import (
    build_tool_context,
    ontology_preflight_tool_context,
)
from agent.runtime.async_utils import (
    clear_bounded_offload_pools,
    configure_bounded_offload_pools,
)
from agent.runtime.contracts import RunPreflightSnapshot, RuntimeInvocationContext
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.offload import BoundedOffloadPool
from agent.tools.base import ToolInvocation
from agent.tools.executor import aexecute_tool_batch
from backend_synthetic_credentials import synthetic_secret

WORKSPACE_ID = "workspace-admitted"
ONTOLOGY_ID = "ontology-admitted"
ONTOLOGY_VERSION = "1.2.3"
OPERATION_KEY = "optimize-admitted-route"
CONTRACT_DIGEST = "sha256:" + "a" * 64


def _execute_script_schema():
    return execute_script_module.build_execute_script_tool_schema_from_catalog(
        ontology_ids=(ONTOLOGY_ID,),
        script_catalog=(
            {
                "operation_key": OPERATION_KEY,
                "parameters": (
                    {
                        "name": "batch_count",
                        "type": "integer",
                        "required": True,
                    },
                ),
            },
        ),
    )


def _solver_capability(*, package_id: str = ONTOLOGY_ID):
    return {
        "package_id": package_id,
        "package_revision": ONTOLOGY_VERSION,
        "operation_key": OPERATION_KEY,
        "decision_model_id": "model-admitted",
        "problem_family": "synthetic.v1",
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_count": {"type": "integer", "minimum": 1},
            },
            "required": ["batch_count"],
            "additionalProperties": False,
        },
        "result_schema": {"type": "object"},
        "contract_digest": CONTRACT_DIGEST,
        "validator_revision": "validator-admitted",
        "enabled": True,
    }


def _snapshot():
    return RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        mutation_revision=3,
        ontology_manifest={
            "id": ONTOLOGY_ID,
            "version": ONTOLOGY_VERSION,
            "ontology_dir": "C:/synthetic/admitted-package",
        },
        execute_script_tool_schema=_execute_script_schema(),
        solver_capabilities=(_solver_capability(),),
        model_profile={
            "id": "profile-secret",
            "api_key": synthetic_secret("tool_schema_preflight_pin-credential-01"),
        },
    )


def _state():
    return {
        "workspace_id": WORKSPACE_ID,
        "run_id": "run-admitted",
        "session_id": "session-admitted",
        "current_turn_id": 1,
        "user_id": "user-admitted",
        "messages": [],
        "user_context": {
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ONTOLOGY_VERSION,
            "agent_mode": "quick",
        },
    }


def _fail_live_read(*_args, **_kwargs):
    raise AssertionError("admitted tool products must not read the live package")


def _patch_live_package_reads_to_fail(monkeypatch):
    monkeypatch.setattr(
        execute_script_module.OntologyRegistry,
        "discover_ontologies",
        _fail_live_read,
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        _fail_live_read,
    )
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        _fail_live_read,
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.build_root_payload",
        _fail_live_read,
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.get_ontology_manifest",
        _fail_live_read,
    )
    monkeypatch.setattr(
        solver_module,
        "_load_public_capabilities",
        _fail_live_read,
    )


def _engine():
    engine = object.__new__(JiuwenReactLoopEngine)
    engine.agent_config = SimpleNamespace(
        web_search=SimpleNamespace(enabled=False),
    )
    return engine


def test_pure_execute_schema_builder_does_not_mutate_admitted_catalog():
    catalog = [
        {
            "operation_key": OPERATION_KEY,
            "parameters": [{"name": "batch_count", "type": "integer", "required": True}],
        }
    ]

    schema = execute_script_module.build_execute_script_tool_schema_from_catalog(
        ontology_ids=(ONTOLOGY_ID,),
        script_catalog=catalog,
    )
    schema["function"]["parameters"]["properties"]["operation_key"]["enum"].append("mutated")

    assert catalog == [
        {
            "operation_key": OPERATION_KEY,
            "parameters": [{"name": "batch_count", "type": "integer", "required": True}],
        }
    ]


def test_default_preflight_package_loader_freezes_dynamic_tool_products(
    monkeypatch,
    tmp_path,
):
    script_path = tmp_path / "run.py"
    script_path.write_text("print('admitted')\n", encoding="utf-8")
    manifest = SimpleNamespace(
        id=ONTOLOGY_ID,
        version=ONTOLOGY_VERSION,
        ontology_dir=str(tmp_path),
    )
    root_payload = {
        "script_catalog": [
            {
                "operation_key": OPERATION_KEY,
                "parameters": [
                    {
                        "name": "batch_count",
                        "type": "integer",
                        "required": True,
                    }
                ],
            },
            {
                "operation_key": "unsafe-operation",
                "parameters": [
                    {"name": "unsafe_value", "type": "string"},
                ],
            },
        ],
        "solver_catalog": [_solver_capability()],
    }
    monkeypatch.setattr(
        "ontology.ontology_catalog.build_root_payload",
        lambda **_kwargs: root_payload,
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.load_script_catalog",
        lambda _root: [
            {
                "operation_key": OPERATION_KEY,
                "_is_bound_operation": True,
                "_script_path": "run.py",
            },
            {
                "operation_key": "unsafe-operation",
                "_is_bound_operation": True,
                "_script_path": "missing.py",
            },
        ],
    )
    monkeypatch.setattr(
        "ontology.ontology_catalog.resolve_ontology_script_path",
        lambda _root, path: script_path if path == "run.py" else None,
    )
    monkeypatch.setattr(
        "ontology.ontologies.build_selected_ontology_preload_context_from_manifest",
        lambda *_args, **_kwargs: "admitted context",
    )
    monkeypatch.setattr(
        "ontology.ontologies.build_selected_ontology_preload_records_from_manifest",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        "ontology.view_cache.build_package_revision_source_signature",
        lambda _root: ("stable-signature",),
    )
    monkeypatch.setattr(
        "ontology.view_cache.build_package_content_revision",
        lambda _root: "sha256:" + "b" * 64,
    )

    product = internal(preflight_module).default_package_loader(manifest, WORKSPACE_ID)

    assert product["execute_script_tool_schema"]["function"]["parameters"]["properties"]["operation_key"]["enum"] == [
        OPERATION_KEY
    ]
    assert product["solver_capabilities"] == (_solver_capability(),)
    assert product["script_bindings"][OPERATION_KEY]["_expected_script_path"] == str(script_path.resolve())


def test_matching_snapshot_drives_real_agent_schema_and_solver_lookup_without_live_reads(
    monkeypatch,
):
    snapshot = _snapshot()
    with pytest.raises(TypeError):
        snapshot.execute_script_tool_schema["function"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.solver_capabilities[0]["enabled"] = False
    monkeypatch.setattr(solver_module, "_SOLVER_COORDINATION_SERVICE", SimpleNamespace())
    _patch_live_package_reads_to_fail(monkeypatch)

    with ontology_preflight_tool_context(snapshot):
        schemas = internal(_engine()).tools_schema_for_state(_state(), caller="main")
        tool_infos = internal(_engine()).jiuwen_tool_infos_for_state(
            _state(),
            caller="main",
        )
        execute_schema = next(item for item in schemas if item["function"]["name"] == "execute_script")
        solver_schema = next(item for item in schemas if str(item["function"]["name"]).startswith("solve_"))
        solver_spec = tool_registry.get_tool_spec(
            solver_schema["function"]["name"],
            selected_ontology_id=ONTOLOGY_ID,
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )

    assert execute_schema["function"]["parameters"]["properties"]["operation_key"]["enum"] == [OPERATION_KEY]
    assert execute_schema["function"]["parameters"]["properties"]["arguments"]["properties"] == {
        "batch_count": {"type": "integer"}
    }
    assert (
        next(info for info in tool_infos if info["name"] == "execute_script")["parameters"]
        == execute_schema["function"]["parameters"]
    )
    assert any(str(info["name"]).startswith("solve_") for info in tool_infos)
    execute_schema["function"]["parameters"]["properties"]["operation_key"]["enum"].append("mutated-operation")
    with ontology_preflight_tool_context(snapshot):
        fresh_execute_schema = tool_registry.get_tool_schema_for_context(
            tool_registry.TOOL_SPECS["execute_script"],
            selected_ontology_id=ONTOLOGY_ID,
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )
    assert fresh_execute_schema["function"]["parameters"]["properties"]["operation_key"]["enum"] == [OPERATION_KEY]
    assert solver_spec is not None
    assert (
        tool_registry.validate_tool_arguments(
            solver_spec.name,
            {"batch_count": 2},
            tool_schema=solver_schema,
        )
        is None
    )
    assert "must be integer" in str(
        tool_registry.validate_tool_arguments(
            solver_spec.name,
            {"batch_count": "2"},
            tool_schema=solver_schema,
        )
    )
    solver_spec.schema["function"]["parameters"]["properties"]["batch_count"]["minimum"] = 999
    with ontology_preflight_tool_context(snapshot):
        fresh_spec = tool_registry.get_tool_spec(
            solver_spec.name,
            selected_ontology_id=ONTOLOGY_ID,
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )
    assert fresh_spec is not None
    assert fresh_spec.schema["function"]["parameters"]["properties"]["batch_count"]["minimum"] == 1


def test_executor_lookup_validation_and_solver_dispatch_share_frozen_binding(
    monkeypatch,
):
    calls = []

    class Service:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def start_or_resume(self, **kwargs):
            kwargs["worker_thread_id"] = threading.get_ident()
            calls.append(kwargs)
            return {
                "status": "completed",
                "authoritative": True,
                "run_id": kwargs["run_id"],
                "job_id": "job-admitted",
                "summary": {"text": "admitted result"},
            }

    monkeypatch.setattr(solver_module, "_SOLVER_COORDINATION_SERVICE", Service())
    _patch_live_package_reads_to_fail(monkeypatch)

    async def exercise():
        caller_thread_id = threading.get_ident()
        pools = [
            BoundedOffloadPool(
                name=name,
                max_workers=1,
                max_pending=4,
                capacity_wait_seconds=1,
                operation_timeout_seconds=5,
            )
            for name in ("pin-short", "pin-external", "pin-tool")
        ]
        configure_bounded_offload_pools(
            short_io=pools[0],
            external_io=pools[1],
            tool_io=pools[2],
        )
        try:
            with ontology_preflight_tool_context(_snapshot()):
                context = build_tool_context(
                    _state(),
                    memory_manager=None,
                    agent_config=SimpleNamespace(
                        tool_timeout_seconds=5,
                        execute_script_timeout=5,
                        parallel_default_max_workers=1,
                    ),
                    caller="main",
                    agent_iteration=1,
                    workspace_id=WORKSPACE_ID,
                    project_root=None,
                )
                (spec,) = tool_registry.get_solver_tool_specs_for_context(
                    selected_ontology_id=ONTOLOGY_ID,
                    selected_ontology_version=ONTOLOGY_VERSION,
                    workspace_id=WORKSPACE_ID,
                )
                invalid = await aexecute_tool_batch(
                    [
                        ToolInvocation(
                            id="call-invalid",
                            name=spec.name,
                            arguments={"batch_count": "bad"},
                        )
                    ],
                    context,
                )
                valid = await aexecute_tool_batch(
                    [
                        ToolInvocation(
                            id="call-valid",
                            name=spec.name,
                            arguments={"batch_count": 2},
                        )
                    ],
                    context,
                )
                return invalid, valid, caller_thread_id
        finally:
            clear_bounded_offload_pools()
            deadline = asyncio.get_running_loop().time() + 5
            for pool in pools:
                await pool.close(deadline=deadline)

    invalid, valid, caller_thread_id = asyncio.run(exercise())

    assert len(calls) == 1
    assert calls[0]["worker_thread_id"] != caller_thread_id
    assert calls[0]["contract_digest"] == CONTRACT_DIGEST
    assert "must be integer" in invalid.messages[0]["content"]
    assert json.loads(valid.messages[0]["content"])["authoritative"] is True


def test_execute_script_executor_validation_uses_frozen_schema_without_live_read(
    monkeypatch,
):
    handler_calls = []
    _patch_live_package_reads_to_fail(monkeypatch)
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script",
        lambda **kwargs: handler_calls.append(kwargs) or "executed",
    )

    async def exercise():
        with ontology_preflight_tool_context(_snapshot()):
            context = build_tool_context(
                _state(),
                memory_manager=None,
                agent_config=SimpleNamespace(
                    tool_timeout_seconds=5,
                    execute_script_timeout=5,
                    parallel_default_max_workers=1,
                ),
                caller="main",
                agent_iteration=1,
                workspace_id=WORKSPACE_ID,
                project_root=None,
            )
            invalid = await aexecute_tool_batch(
                [
                    ToolInvocation(
                        id="script-invalid",
                        name="execute_script",
                        arguments={
                            "ontology_id": ONTOLOGY_ID,
                            "operation_key": OPERATION_KEY,
                            "arguments": {
                                "batch_count": 2,
                                "live_only": True,
                            },
                        },
                    )
                ],
                context,
            )
            valid = await aexecute_tool_batch(
                [
                    ToolInvocation(
                        id="script-valid",
                        name="execute_script",
                        arguments={
                            "ontology_id": ONTOLOGY_ID,
                            "operation_key": OPERATION_KEY,
                            "arguments": {"batch_count": 2},
                        },
                    )
                ],
                context,
            )
            return invalid, valid

    invalid, valid = asyncio.run(exercise())

    assert len(handler_calls) == 1
    assert handler_calls[0]["operation_key"] == OPERATION_KEY
    assert "unexpected field" in invalid.messages[0]["content"]
    assert valid.messages[0]["content"] == "executed"


def test_matching_snapshot_with_missing_schema_fails_closed_without_live_read(
    monkeypatch,
):
    snapshot = RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        mutation_revision=3,
        ontology_manifest={"id": ONTOLOGY_ID, "version": ONTOLOGY_VERSION},
    )
    monkeypatch.setattr(
        tool_registry,
        "build_execute_script_tool_schema",
        _fail_live_read,
    )

    with ontology_preflight_tool_context(snapshot):
        schema = tool_registry.get_tool_schema_for_context(
            tool_registry.TOOL_SPECS["execute_script"],
            selected_ontology_id=ONTOLOGY_ID,
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )

    operation_schema = schema["function"]["parameters"]["properties"]["operation_key"]
    assert "enum" not in operation_schema


def test_mismatched_state_tool_context_does_not_receive_private_products():
    state = _state()
    state["user_context"] = {
        **state["user_context"],
        "selected_ontology_id": "different-ontology",
    }

    with ontology_preflight_tool_context(_snapshot()):
        context = build_tool_context(
            state,
            memory_manager=None,
            agent_config=SimpleNamespace(execute_script_timeout=5),
            caller="main",
            agent_iteration=1,
            workspace_id=WORKSPACE_ID,
            project_root=None,
        )

    assert context.preflight_ontology_manifest is None
    assert context.preflight_ontology_script_bindings is None
    assert context.preflight_execute_script_tool_schema is None
    assert context.preflight_solver_capabilities is None


def test_matching_incomplete_snapshot_never_live_loads_solver_root(monkeypatch):
    snapshot = RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        mutation_revision=3,
        ontology_manifest={"id": ONTOLOGY_ID, "version": ONTOLOGY_VERSION},
    )
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard._load_solver_root_payload",
        _fail_live_read,
    )
    invocation = RuntimeInvocationContext(
        run_id="run-incomplete-admitted",
        session_id="session-incomplete-admitted",
        turn_id=1,
        workspace_id=WORKSPACE_ID,
        username="user-admitted",
        query="optimize admitted route",
        arguments={
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ONTOLOGY_VERSION,
        },
        preflight_snapshot=snapshot,
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(invocation)

    assert state["workspace_id"] == WORKSPACE_ID


def test_manifest_identity_mismatch_preserves_legacy_solver_root_loader(monkeypatch):
    live_calls = []
    snapshot = RunPreflightSnapshot(
        workspace_id=WORKSPACE_ID,
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id=ONTOLOGY_ID,
        ontology_version=ONTOLOGY_VERSION,
        mutation_revision=3,
        ontology_manifest={
            "id": "different-ontology",
            "version": ONTOLOGY_VERSION,
        },
        root_payload={},
        selected_ontology_context="mismatched private context",
    )
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard._load_solver_root_payload",
        lambda **kwargs: live_calls.append(kwargs) or {},
    )
    invocation = RuntimeInvocationContext(
        run_id="run-manifest-mismatch",
        session_id="session-manifest-mismatch",
        turn_id=1,
        workspace_id=WORKSPACE_ID,
        username="user-admitted",
        query="optimize admitted route",
        arguments={
            "selected_ontology_id": ONTOLOGY_ID,
            "selected_ontology_version": ONTOLOGY_VERSION,
        },
        preflight_snapshot=snapshot,
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(invocation)

    assert live_calls == [
        {
            "package_id": ONTOLOGY_ID,
            "package_revision": ONTOLOGY_VERSION,
            "workspace_id": WORKSPACE_ID,
        }
    ]
    assert "_selected_ontology_context_cache" not in state


@pytest.mark.parametrize("bind_snapshot", [False, True])
def test_missing_or_identity_mismatched_snapshot_preserves_legacy_loaders(
    monkeypatch,
    bind_snapshot,
):
    execute_calls = []
    solver_calls = []
    legacy_schema = {
        "type": "function",
        "function": {
            "name": "execute_script",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    def legacy_execute_schema(*args, **kwargs):
        execute_calls.append((args, kwargs))
        return legacy_schema

    def legacy_capabilities(**kwargs):
        solver_calls.append(kwargs)
        return [_solver_capability(package_id="ontology-live")]

    monkeypatch.setattr(
        tool_registry,
        "build_execute_script_tool_schema",
        legacy_execute_schema,
    )
    monkeypatch.setattr(solver_module, "_load_public_capabilities", legacy_capabilities)
    monkeypatch.setattr(solver_module, "_SOLVER_COORDINATION_SERVICE", SimpleNamespace())
    context = ontology_preflight_tool_context(_snapshot()) if bind_snapshot else ontology_preflight_tool_context(None)

    with context:
        execute = tool_registry.get_tool_schema_for_context(
            tool_registry.TOOL_SPECS["execute_script"],
            selected_ontology_id="ontology-live",
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )
        specs = tool_registry.get_solver_tool_specs_for_context(
            selected_ontology_id="ontology-live",
            selected_ontology_version=ONTOLOGY_VERSION,
            workspace_id=WORKSPACE_ID,
        )

    assert execute is legacy_schema
    assert len(execute_calls) == 1
    assert len(solver_calls) == 1
    assert len(specs) == 1
