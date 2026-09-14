# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Regression tests for run-admission ontology execution pinning."""

from __future__ import annotations

from internal_access import internal
import asyncio
import hashlib
import os
import pickle
from dataclasses import replace
from operator import getitem
from pathlib import Path
from types import SimpleNamespace

import agent.tools.execute_script as execute_script_module
import pytest
from agent.core.shared.tool_context import (
    build_tool_context,
    current_ontology_preflight_tool_fields,
    ontology_preflight_tool_context,
)
from agent.runtime.contracts import RunPreflightSnapshot, RuntimeInvocationContext
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.tools.base import ToolContext
from backend_synthetic_credentials import synthetic_secret
from ontology.view_cache import (
    build_package_content_revision,
    build_package_revision_source_signature,
)

_ONTOLOGY_ID = "synthetic-ontology"
_ONTOLOGY_VERSION = "1.0.0"
_OPERATION_KEY = "synthetic-operation"


def _write_package(tmp_path: Path) -> tuple[Path, Path, Path]:
    version_dir = tmp_path / "ontologies" / _ONTOLOGY_ID / "versions" / _ONTOLOGY_VERSION
    scripts_dir = version_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script_path = scripts_dir / "run.py"
    descriptor_path = scripts_dir / "run.md"
    script_path.write_text("print('alpha')\n", encoding="utf-8")
    descriptor_path.write_text(
        "---\nbound_operation: synthetic-operation\nentrypoint: scripts/run.py\n---\n",
        encoding="utf-8",
    )
    (version_dir / "index.md").write_text("# Synthetic\n", encoding="utf-8")
    return version_dir, script_path, descriptor_path


def _manifest(version_dir: Path, *, ontology_id: str = _ONTOLOGY_ID, version: str = _ONTOLOGY_VERSION):
    return {
        "kind": "ontology-package",
        "schema_version": "0.2",
        "id": ontology_id,
        "name": "Synthetic ontology",
        "description": "Synthetic package",
        "version": version,
        "active_version": version,
        "versions": [version],
        "language": "en",
        "enabled": True,
        "scope": [],
        "manifest_path": str(version_dir / "manifest.yaml"),
        "ontology_dir": str(version_dir),
        "index_path": str(version_dir / "index.md"),
    }


def _binding(script_path: Path, *, secret: str | None = None):
    entry = {
        "operation_key": _OPERATION_KEY,
        "script_key": "run",
        "title": "Synthetic operation",
        "arguments": [],
        "parameters": [],
        "_script_path": "scripts/run.py",
        "_expected_script_path": str(script_path.resolve()),
        "_is_bound_operation": True,
        "data_access": "none",
        "_expected_script_digest": "sha256:" + hashlib.sha256(script_path.read_bytes()).hexdigest(),
    }
    if secret is not None:
        entry["_private_test_marker"] = secret
    return entry


def _snapshot(version_dir: Path, script_path: Path, *, secret: str | None = None):
    return RunPreflightSnapshot(
        workspace_id="synthetic-workspace",
        workspace_config_generation=1,
        system_config_generation=1,
        ontology_id=_ONTOLOGY_ID,
        ontology_version=_ONTOLOGY_VERSION,
        mutation_revision=7,
        ontology_manifest=_manifest(version_dir),
        ontology_source_signature=build_package_revision_source_signature(version_dir),
        ontology_content_revision=build_package_content_revision(version_dir),
        script_bindings={_OPERATION_KEY: _binding(script_path, secret=secret)},
    )


def _tool_context(**overrides) -> ToolContext:
    values = {
        "memory_manager": None,
        "messages": [],
        "run_id": "run-pin",
        "session_id": "session-pin",
        "turn_id": 1,
        "agent_config": SimpleNamespace(execute_script_timeout=30),
        "caller": "main",
        "workspace_id": "synthetic-workspace",
        "project_root": None,
        "selected_ontology_id": _ONTOLOGY_ID,
        "selected_ontology_version": _ONTOLOGY_VERSION,
    }
    values.update(overrides)
    return ToolContext(**values)


def _execute_tool(context: ToolContext):
    return execute_script_module.ExecuteScriptTool().execute(
        args={
            "ontology_id": _ONTOLOGY_ID,
            "operation_key": _OPERATION_KEY,
            "arguments": {},
        },
        context=context,
        tool_call_id="call-pin",
    )


def _call_pinned_handler(
    *,
    version_dir: Path,
    script_path: Path,
    source_signature,
    content_revision,
    binding,
    monkeypatch,
):
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_mutation_revision",
        lambda _workspace_id, _ontology_id: 7,
    )
    monkeypatch.setattr(
        "ontology.operation_execution.is_deterministic_operation",
        lambda _ontology_dir, _operation_key: False,
    )
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        lambda **_kwargs: "executed",
    )
    return execute_script_module.handle_execute_script(
        ontology_id=_ONTOLOGY_ID,
        ontology_version=_ONTOLOGY_VERSION,
        operation_key=_OPERATION_KEY,
        arguments={},
        base_path=version_dir.parent,
        workspace_id="synthetic-workspace",
        preflight_ontology_manifest=_manifest(version_dir),
        preflight_ontology_source_signature=source_signature,
        preflight_ontology_content_revision=content_revision,
        preflight_ontology_script_entry=binding,
        preflight_ontology_mutation_revision=7,
    )


def test_pinned_execution_uses_snapshot_metadata_without_reloading_live_catalog(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *_args, **_kwargs: pytest.fail("live manifest must not be reloaded"),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda *_args, **_kwargs: pytest.fail("live catalog must not be reloaded"),
    )

    result = _call_pinned_handler(
        version_dir=version_dir,
        script_path=script_path,
        source_signature=source_signature,
        content_revision=content_revision,
        binding=binding,
        monkeypatch=monkeypatch,
    )

    assert result == "executed"


def test_workspace_pinned_execution_uses_runtime_staging_boundary(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "synthetic-workspace"
    version_dir, script_path, _descriptor_path = _write_package(workspace_root)
    observed = {}

    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_mutation_revision",
        lambda _workspace_id, _ontology_id: 7,
    )
    monkeypatch.setattr(
        "ontology.operation_execution.is_deterministic_operation",
        lambda _ontology_dir, _operation_key: False,
    )

    def execute_from_staging(**kwargs):
        base_path = Path(kwargs["base_path"]).resolve()
        resolved = (base_path / kwargs["script_path"]).resolve()
        observed.update(base_path=base_path, resolved=resolved)
        return resolved.read_text(encoding="utf-8")

    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        execute_from_staging,
    )

    result = execute_script_module.handle_execute_script(
        ontology_id=_ONTOLOGY_ID,
        ontology_version=_ONTOLOGY_VERSION,
        operation_key=_OPERATION_KEY,
        arguments={},
        base_path=workspace_root,
        workspace_id="synthetic-workspace",
        preflight_ontology_manifest=_manifest(version_dir),
        preflight_ontology_source_signature=(build_package_revision_source_signature(version_dir)),
        preflight_ontology_content_revision=build_package_content_revision(version_dir),
        preflight_ontology_script_entry=_binding(script_path),
        preflight_ontology_mutation_revision=7,
    )

    assert result == "print('alpha')\n"
    assert observed["base_path"] == runtime_root.resolve()
    assert ".ontology-run-staging" in observed["resolved"].parts
    assert not observed["resolved"].exists()


@pytest.mark.parametrize("changed_path", ["descriptor", "package"])
def test_pinned_execution_rejects_metadata_changed_after_admission(
    tmp_path,
    monkeypatch,
    changed_path,
):
    version_dir, script_path, descriptor_path = _write_package(tmp_path)
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    target = descriptor_path if changed_path == "descriptor" else version_dir / "index.md"
    target.write_text(target.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

    result = _call_pinned_handler(
        version_dir=version_dir,
        script_path=script_path,
        source_signature=source_signature,
        content_revision=content_revision,
        binding=binding,
        monkeypatch=monkeypatch,
    )

    assert result == execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR


def test_pinned_execution_rejects_same_metadata_signature_script_drift(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    original_stat = script_path.stat()
    script_path.write_text("print('bravo')\n", encoding="utf-8")
    os.utime(
        script_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert build_package_revision_source_signature(version_dir) == source_signature

    result = _call_pinned_handler(
        version_dir=version_dir,
        script_path=script_path,
        source_signature=source_signature,
        content_revision=content_revision,
        binding=binding,
        monkeypatch=monkeypatch,
    )

    assert result == execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR


def test_pinned_execution_rejects_control_file_stat_collision(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    control_path = version_dir / "index.md"
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    original_stat = control_path.stat()
    control_path.write_text("# SynthetiC\n", encoding="utf-8")
    os.utime(
        control_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert control_path.stat().st_size == original_stat.st_size
    assert build_package_revision_source_signature(version_dir) == source_signature

    result = _call_pinned_handler(
        version_dir=version_dir,
        script_path=script_path,
        source_signature=source_signature,
        content_revision=content_revision,
        binding=binding,
        monkeypatch=monkeypatch,
    )

    assert result == execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR


def test_pinned_execution_rejects_malformed_source_signature(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    source_signature = build_package_revision_source_signature(version_dir)

    result = _call_pinned_handler(
        version_dir=version_dir,
        script_path=script_path,
        source_signature=list(source_signature),
        content_revision=build_package_content_revision(version_dir),
        binding=_binding(script_path),
        monkeypatch=monkeypatch,
    )

    assert result == execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR


def test_pinned_execution_never_runs_source_replaced_after_final_digest_check(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    original_stage = execute_script_module.stage_verified_package_revision

    from contextlib import contextmanager

    @contextmanager
    def replace_after_stage(**kwargs):
        with original_stage(**kwargs) as staged_root:
            script_path.write_text("print('bravo')\n", encoding="utf-8")
            yield staged_root

    executed = {}

    def execute_from_path(**kwargs):
        resolved = (Path(kwargs["base_path"]) / kwargs["script_path"]).resolve()
        executed["path"] = resolved
        executed["content"] = resolved.read_text(encoding="utf-8")
        return executed["content"]

    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_mutation_revision",
        lambda _workspace_id, _ontology_id: 7,
    )
    monkeypatch.setattr(
        "ontology.operation_execution.is_deterministic_operation",
        lambda _ontology_dir, _operation_key: False,
    )
    monkeypatch.setattr(
        execute_script_module,
        "stage_verified_package_revision",
        replace_after_stage,
    )
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script_path",
        execute_from_path,
    )

    result = execute_script_module.handle_execute_script(
        ontology_id=_ONTOLOGY_ID,
        ontology_version=_ONTOLOGY_VERSION,
        operation_key=_OPERATION_KEY,
        arguments={},
        base_path=version_dir.parent,
        workspace_id="synthetic-workspace",
        preflight_ontology_manifest=_manifest(version_dir),
        preflight_ontology_source_signature=source_signature,
        preflight_ontology_content_revision=content_revision,
        preflight_ontology_script_entry=binding,
        preflight_ontology_mutation_revision=7,
    )

    assert result in {
        execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR,
        "print('alpha')\n",
    }
    assert executed.get("content") != "print('bravo')\n"
    if executed:
        assert executed["path"] != script_path.resolve()
        assert ".ontology-run-staging" in executed["path"].parts
        assert not executed["path"].exists()


@pytest.mark.parametrize("execution_channel", ["local", "sandbox"])
def test_pinned_execution_uses_staged_package_at_each_process_boundary(
    execution_channel,
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    project_root = version_dir.parent
    source_signature = build_package_revision_source_signature(version_dir)
    content_revision = build_package_content_revision(version_dir)
    binding = _binding(script_path)
    observed = {}

    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_mutation_revision",
        lambda _workspace_id, _ontology_id: 7,
    )
    monkeypatch.setattr(
        "ontology.operation_execution.is_deterministic_operation",
        lambda _ontology_dir, _operation_key: False,
    )
    monkeypatch.setattr(execute_script_module, "SCRIPT_LIVE_LOG_ENABLED", False)

    if execution_channel == "local":
        monkeypatch.setattr(
            execute_script_module,
            "load_sandbox_config",
            lambda: SimpleNamespace(enabled=False, execution_mode="legacy"),
        )

        def run_local(command, **_kwargs):
            staged_script = Path(command[1])
            observed["script"] = staged_script
            observed["ontology_dir"] = staged_script.parent.parent
            observed["content"] = staged_script.read_text(encoding="utf-8")
            return execute_script_module.subprocess.CompletedProcess(
                command,
                0,
                stdout="local-ok\n",
                stderr="",
            )

        monkeypatch.setattr(execute_script_module.subprocess, "run", run_local)
    else:
        monkeypatch.setattr(execute_script_module.sys, "platform", "linux")
        monkeypatch.setattr(
            execute_script_module,
            "load_sandbox_config",
            lambda: SimpleNamespace(
                enabled=True,
                execution_mode="prefer",
                fallback_local=False,
            ),
        )

        class FakeSandboxExecutor:
            # pylint: disable-next=add-staticmethod-or-classmethod-decorator
            def run_script(self, **kwargs):
                staged_script = (Path(kwargs["project_root"]) / kwargs["script_path"]).resolve()
                observed["script"] = staged_script
                observed["ontology_dir"] = Path(kwargs["ontology_dir"])
                observed["content"] = staged_script.read_text(encoding="utf-8")
                return execute_script_module.subprocess.CompletedProcess(
                    [kwargs["script_path"]],
                    0,
                    stdout="sandbox-ok\n",
                    stderr="",
                )

        monkeypatch.setattr(
            execute_script_module,
            "SandboxScriptExecutor",
            FakeSandboxExecutor,
        )

    result = execute_script_module.handle_execute_script(
        ontology_id=_ONTOLOGY_ID,
        ontology_version=_ONTOLOGY_VERSION,
        operation_key=_OPERATION_KEY,
        arguments={},
        base_path=project_root,
        run_id="run-process-boundary",
        workspace_id="synthetic-workspace",
        preflight_ontology_manifest=_manifest(version_dir),
        preflight_ontology_source_signature=source_signature,
        preflight_ontology_content_revision=content_revision,
        preflight_ontology_script_entry=binding,
        preflight_ontology_mutation_revision=7,
    )

    assert result == "{}-ok\n".format(execution_channel)
    assert observed["content"] == "print('alpha')\n"
    assert observed["script"].is_relative_to(observed["ontology_dir"])
    assert ".ontology-run-staging" in observed["script"].parts
    assert not observed["ontology_dir"].exists()


def test_tool_context_receives_private_snapshot_without_state_or_repr_leak(tmp_path):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    secret = synthetic_secret("execute_script_preflight_pin-credential-02")
    snapshot = _snapshot(version_dir, script_path, secret=secret)
    runtime_context = RuntimeInvocationContext(
        run_id="run-private-pin",
        session_id="session-private-pin",
        turn_id=1,
        workspace_id="synthetic-workspace",
        username="synthetic-user",
        query="run synthetic operation",
        arguments={
            "selected_ontology_id": _ONTOLOGY_ID,
            "selected_ontology_version": _ONTOLOGY_VERSION,
        },
        preflight_snapshot=snapshot,
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(runtime_context)
    assert secret not in repr(state)
    assert "preflight_snapshot" not in state

    with ontology_preflight_tool_context(snapshot):
        context = build_tool_context(
            state,
            memory_manager=None,
            agent_config=SimpleNamespace(execute_script_timeout=30),
            caller="main",
            agent_iteration=1,
            workspace_id="synthetic-workspace",
            project_root=str(version_dir),
        )

    assert context.preflight_ontology_manifest["id"] == _ONTOLOGY_ID
    assert context.preflight_ontology_content_revision == (snapshot.ontology_content_revision)
    assert context.preflight_ontology_script_bindings[_OPERATION_KEY]["_private_test_marker"] == secret
    assert secret not in repr(context)


def test_preflight_tool_context_is_pickle_safe_for_runtime_tool_scheduling(tmp_path):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    snapshot = _snapshot(version_dir, script_path)

    with ontology_preflight_tool_context(snapshot):
        fields = current_ontology_preflight_tool_fields()
        restored = pickle.loads(pickle.dumps(fields))
        fields["preflight_ontology_manifest"]["id"] = "run-local-change"

    assert restored["preflight_ontology_manifest"]["id"] == _ONTOLOGY_ID
    assert restored["preflight_ontology_script_bindings"][_OPERATION_KEY]["operation_key"] == _OPERATION_KEY
    assert snapshot.ontology_manifest["id"] == _ONTOLOGY_ID


def test_preflight_engine_state_is_pickle_safe_for_openjiuwen_checkpoint(tmp_path):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    snapshot = replace(
        _snapshot(version_dir, script_path),
        selected_ontology_context="<Selected-Ontology />",
        preload_records=(
            {
                "path": "ontologies/synthetic-ontology/index.md",
                "normalized_path": "ontologies/synthetic-ontology/index.md",
                "source": "preload",
                "metadata": {"section": "root"},
            },
        ),
    )
    runtime_context = RuntimeInvocationContext(
        run_id="run-pickle-safe-state",
        session_id="session-pickle-safe-state",
        turn_id=1,
        workspace_id="synthetic-workspace",
        username="synthetic-user",
        query="run synthetic operation",
        arguments={
            "selected_ontology_id": _ONTOLOGY_ID,
            "selected_ontology_version": _ONTOLOGY_VERSION,
        },
        preflight_snapshot=snapshot,
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(runtime_context)
    restored = pickle.loads(pickle.dumps(state))
    state["_preflight_selected_ontology_preload_records"][0]["metadata"]["section"] = "run-local-change"

    assert restored["_preflight_selected_ontology_preload_records"][0]["metadata"]["section"] == "root"
    assert snapshot.preload_records[0]["metadata"]["section"] == "root"


def test_ontology_tool_context_never_carries_preflight_model_secret(tmp_path):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    model_secret = synthetic_secret("execute_script_preflight_pin-credential-01")
    snapshot = replace(
        _snapshot(version_dir, script_path),
        model_profile={
            "id": "synthetic-model",
            "api_key": model_secret,
        },
    )

    with ontology_preflight_tool_context(snapshot):
        fields = current_ontology_preflight_tool_fields()

    assert model_secret not in repr(fields)
    assert all(value is None for value in current_ontology_preflight_tool_fields().values())


@pytest.mark.parametrize(
    ("manifest_id", "manifest_version"),
    [("other-ontology", _ONTOLOGY_VERSION), (_ONTOLOGY_ID, "2.0.0")],
)
def test_execute_script_does_not_reuse_snapshot_for_identity_mismatch(
    tmp_path,
    monkeypatch,
    manifest_id,
    manifest_version,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    captured = {}
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script",
        lambda **kwargs: captured.update(kwargs) or "legacy",
    )
    context = _tool_context(
        preflight_ontology_manifest=_manifest(
            version_dir,
            ontology_id=manifest_id,
            version=manifest_version,
        ),
        preflight_ontology_source_signature=build_package_revision_source_signature(version_dir),
        preflight_ontology_script_bindings={_OPERATION_KEY: _binding(script_path)},
        preflight_ontology_mutation_revision=7,
    )

    outcome = _execute_tool(context)

    assert outcome.content == "legacy"
    assert "preflight_ontology_manifest" not in captured
    assert "preflight_ontology_script_entry" not in captured


def test_execute_script_passes_matching_private_binding_to_handler(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    snapshot = _snapshot(version_dir, script_path)
    captured = {}
    monkeypatch.setattr(
        execute_script_module,
        "handle_execute_script",
        lambda **kwargs: captured.update(kwargs) or "pinned",
    )
    with ontology_preflight_tool_context(snapshot):
        context = build_tool_context(
            {
                "run_id": "run-matching-pin",
                "session_id": "session-matching-pin",
                "current_turn_id": 1,
                "messages": [],
                "user_context": {
                    "selected_ontology_id": _ONTOLOGY_ID,
                    "selected_ontology_version": _ONTOLOGY_VERSION,
                },
            },
            memory_manager=None,
            agent_config=SimpleNamespace(execute_script_timeout=30),
            caller="main",
            agent_iteration=1,
            workspace_id="synthetic-workspace",
            project_root=str(version_dir),
        )

    outcome = _execute_tool(context)

    assert outcome.content == "pinned"
    assert getitem(getitem(captured, "preflight_ontology_manifest"), "id") == _ONTOLOGY_ID
    assert getitem(getitem(captured, "preflight_ontology_script_entry"), "operation_key") == (_OPERATION_KEY)
    assert getitem(captured, "preflight_ontology_source_signature") == (snapshot.ontology_source_signature)
    assert getitem(captured, "preflight_ontology_content_revision") == (snapshot.ontology_content_revision)
    assert getitem(captured, "preflight_ontology_mutation_revision") == 7


def test_execute_script_revision_drift_is_terminal_and_never_runs_live_metadata(
    tmp_path,
    monkeypatch,
):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    snapshot = _snapshot(version_dir, script_path)
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_mutation_revision",
        lambda _workspace_id, _ontology_id: 8,
    )
    monkeypatch.setattr(
        execute_script_module,
        "get_ontology_manifest",
        lambda *_args, **_kwargs: pytest.fail("live manifest must not be reloaded"),
    )
    monkeypatch.setattr(
        execute_script_module,
        "load_script_catalog",
        lambda *_args, **_kwargs: pytest.fail("live catalog must not be reloaded"),
    )
    with ontology_preflight_tool_context(snapshot):
        context = build_tool_context(
            {
                "run_id": "run-revision-drift",
                "session_id": "session-revision-drift",
                "current_turn_id": 1,
                "messages": [],
                "user_context": {
                    "selected_ontology_id": _ONTOLOGY_ID,
                    "selected_ontology_version": _ONTOLOGY_VERSION,
                },
            },
            memory_manager=None,
            agent_config=SimpleNamespace(execute_script_timeout=30),
            caller="main",
            agent_iteration=1,
            workspace_id="synthetic-workspace",
            project_root=str(version_dir),
        )

    outcome = _execute_tool(context)

    assert outcome.content == execute_script_module.ONTOLOGY_PREFLIGHT_CHANGED_ERROR
    assert outcome.terminal is True
    assert outcome.state_updates["terminal_error"]["error_code"] == "EXECUTE_SCRIPT_FAILED"


def test_jiuwen_runtime_binds_and_resets_snapshot_context(tmp_path):
    version_dir, script_path, _descriptor_path = _write_package(tmp_path)
    secret = synthetic_secret("execute_script_preflight_pin-credential-03")
    snapshot = _snapshot(version_dir, script_path, secret=secret)
    observed = []

    class Engine:
        def __init__(self, **_kwargs):
            pass

        async def arun(self, state, **_kwargs):
            context = build_tool_context(
                state,
                memory_manager=None,
                agent_config=SimpleNamespace(execute_script_timeout=30),
                caller="main",
                agent_iteration=1,
                workspace_id="synthetic-workspace",
                project_root=str(version_dir),
            )
            observed.append(context.preflight_ontology_manifest)
            return {**state, "final_output": "done"}

    invocation = RuntimeInvocationContext(
        run_id="run-runtime-pin",
        session_id="session-runtime-pin",
        turn_id=1,
        workspace_id="synthetic-workspace",
        username="synthetic-user",
        query="run",
        arguments={
            "selected_ontology_id": _ONTOLOGY_ID,
            "selected_ontology_version": _ONTOLOGY_VERSION,
        },
        preflight_snapshot=snapshot,
    )

    async def exercise():
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=Engine,
        )
        return [event async for event in runtime.astream(invocation)]

    events = asyncio.run(exercise())

    assert events[-1].status == "completed"
    assert secret not in repr(events)
    assert observed[0]["version"] == _ONTOLOGY_VERSION
    outside = build_tool_context(
        {},
        memory_manager=None,
        agent_config=SimpleNamespace(execute_script_timeout=30),
        caller="main",
        agent_iteration=1,
        workspace_id="synthetic-workspace",
        project_root=str(version_dir),
    )
    assert outside.preflight_ontology_manifest is None
