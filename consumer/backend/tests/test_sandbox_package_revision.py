"""Synthetic checks for the verified package handoff and its mount boundary."""

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from integration.sandbox.config import SandboxConfig
from integration.sandbox.errors import SandboxUnavailableError
from integration.sandbox.executor import SandboxScriptExecutor
from integration.sandbox.policy_builder import OntoSandboxPolicyBuilder
import integration.sandbox.executor as executor_module
import integration.sandbox.package_revision as revisions


@pytest.fixture
def staged_package(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_demo"
    workspace = runtime / "workspaces" / "workspace_demo"
    workspace.mkdir(parents=True)
    root = runtime / ".ontology-run-staging" / "run-demo" / "ontology"
    script = root / "scripts" / "run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('synthetic result')\n")
    config = SandboxConfig(enabled=True, fallback_local=False)
    calls = []
    registry = SimpleNamespace(
        register_companion=lambda *args: calls.append(("register", args)),
        ensure=lambda ctx: calls.append(("policy", ctx)) or "sandbox_demo",
        exec_in_sandbox=lambda *args, **kwargs: SimpleNamespace(
            exit_code=0, stdout="synthetic result", stderr="",
        ),
    )
    monkeypatch.setattr(revisions, "load_sandbox_config", lambda: config)
    monkeypatch.setattr(revisions, "get_workspaces_root", lambda: runtime / "workspaces")
    monkeypatch.setattr(revisions, "get_run_sandbox_registry", lambda: registry)
    monkeypatch.setattr(revisions, "release_run_sandbox", lambda run_id, **kwargs: calls.append(("release", run_id)))
    monkeypatch.setattr(executor_module, "get_workspace_project_root", lambda _: workspace)
    monkeypatch.setattr(executor_module, "get_workspaces_root", lambda: runtime / "workspaces")
    monkeypatch.setattr(executor_module, "get_run_sandbox_registry", lambda: registry)
    kwargs = dict(
        script_path=str(script.relative_to(runtime)), cli_args=[], project_root=runtime,
        ontology_dir=root, run_id="run_demo", workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo", timeout_seconds=10,
        prepare_data_connection=False,
    )
    return SimpleNamespace(root=root, workspace=workspace, calls=calls, registry=registry,
                           executor=SandboxScriptExecutor(config), kwargs=kwargs)


def _scope(fixture):
    return revisions.verified_package_execution(
        fixture.root, workspace_id="workspace_demo", ontology_id="knowledge_package_demo",
        run_id="run_demo",
    )


def test_verified_copy_mounts_only_the_admitted_package_and_releases_each_call(staged_package):
    fixture = staged_package
    for _ in range(2):
        with _scope(fixture):
            assert fixture.executor.run_script(**fixture.kwargs).returncode == 0
    contexts = [value for kind, value in fixture.calls if kind == "policy"]
    assert len(contexts) == 2
    assert contexts[0].run_id != contexts[1].run_id
    for ctx in contexts:
        assert ctx.read_only_paths == (fixture.root,)
        assert ctx.writable_paths == ()
        mounts = OntoSandboxPolicyBuilder.build(ctx)["filesystem_policy"]["bind_mounts"]
        paths = {item["host_path"] for item in mounts}
        assert str(fixture.root) in paths
        assert str(fixture.workspace) not in paths
        assert str(fixture.root.parent) not in paths
        assert str(fixture.root.parent.parent) not in paths
    assert [value for kind, value in fixture.calls if kind == "release"] == [ctx.run_id for ctx in contexts]
    assert revisions._VERIFIED_PACKAGE.get() is None


def test_unscoped_staging_path_still_cannot_escape_workspace(staged_package):
    with pytest.raises(SandboxUnavailableError, match="outside the sandbox workspace"):
        staged_package.executor.run_script(**staged_package.kwargs)
    assert staged_package.calls == []


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "workspace_other"), ("ontology_id", "knowledge_package_other"),
    ("run_id", "run_other"), ("ontology_dir", None),
    ("ontology_dir", Path("knowledge_package_other")),
])
def test_verified_copy_cannot_cross_execution_scope(staged_package, field, value):
    with _scope(staged_package):
        with pytest.raises(SandboxUnavailableError, match="scope mismatch"):
            staged_package.executor.run_script(**{**staged_package.kwargs, field: value})
    assert not any(kind == "policy" for kind, _ in staged_package.calls)
    assert staged_package.calls[-1][0] == "release"


@pytest.mark.parametrize("field", ["read_only_paths", "writable_paths"])
def test_verified_copy_cannot_add_mounts(staged_package, field):
    with _scope(staged_package):
        with pytest.raises(SandboxUnavailableError, match="cannot add filesystem mounts"):
            staged_package.executor.run_script(**{**staged_package.kwargs, field: [staged_package.workspace]})


def test_verified_copy_cannot_execute_a_sibling_package(staged_package):
    with _scope(staged_package):
        with pytest.raises(SandboxUnavailableError, match="outside the sandbox workspace"):
            staged_package.executor.run_script(**{
                **staged_package.kwargs,
                "script_path": ".ontology-run-staging/run-other/ontology/scripts/run.py",
            })


def test_failed_exec_releases_companion_and_clears_scope(staged_package):
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic execution failure")
    staged_package.registry.exec_in_sandbox = fail
    with pytest.raises(RuntimeError, match="synthetic execution failure"):
        with _scope(staged_package):
            staged_package.executor.run_script(**staged_package.kwargs)
    assert staged_package.calls[-1][0] == "release"
    assert revisions._VERIFIED_PACKAGE.get() is None


@pytest.mark.parametrize("parent_depth", [0, 1, 2])
def test_handoff_rejects_a_symlink_to_another_staging_directory(staged_package, parent_depth):
    root = staged_package.root
    for _ in range(parent_depth):
        root = root.parent
    renamed = root.with_name("other")
    root.rename(renamed)
    root.symlink_to(renamed, target_is_directory=True)
    with pytest.raises(SandboxUnavailableError, match="staging boundary"):
        with _scope(staged_package):
            pytest.fail("unsafe scope admitted")
    assert staged_package.calls == []


@pytest.mark.parametrize("field,value", [
    ("run_id", None), ("run_id", ""), ("workspace_id", ""), ("ontology_id", ""),
])
def test_handoff_rejects_missing_execution_identity(staged_package, field, value):
    kwargs = dict(
        workspace_id="workspace_demo", ontology_id="knowledge_package_demo", run_id="run_demo",
    )
    kwargs[field] = value
    with pytest.raises(SandboxUnavailableError, match="staging boundary"):
        with revisions.verified_package_execution(staged_package.root, **kwargs):
            pytest.fail("incomplete scope admitted")
    assert staged_package.calls == []
    assert revisions._VERIFIED_PACKAGE.get() is None


@pytest.mark.parametrize("relative_path,create", [
    (".ontology-run-staging/run-demo/other", True),
    (".ontology-run-staging/other/ontology", True),
    ("other/run-demo/ontology", True),
    (".ontology-run-staging/run-missing/ontology", False),
])
def test_handoff_rejects_invalid_staging_paths(staged_package, relative_path, create):
    staged_package.root = staged_package.root.parents[2] / relative_path
    if create:
        staged_package.root.mkdir(parents=True)
    with pytest.raises(SandboxUnavailableError, match="staging boundary"):
        with _scope(staged_package):
            pytest.fail("unsafe scope admitted")
    assert staged_package.calls == []
    assert revisions._VERIFIED_PACKAGE.get() is None


def _admission(fixture):
    return revisions.admitted_package_revision(
        fixture.root, workspace_id="workspace_demo", ontology_id="knowledge_package_demo", run_id="run_demo",
    )


def _deterministic_scope(fixture, **overrides):
    kwargs = dict(
        package_root=fixture.root, workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo", run_id="run_demo",
    )
    kwargs.update(overrides)
    return revisions.verified_deterministic_execution(**kwargs)


@pytest.fixture
def artifact_directory(tmp_path):
    root = tmp_path / "deterministic-artifact-staging-synthetic" / ("run-" + "a" * 64)
    root.mkdir(parents=True)
    return root


@pytest.mark.parametrize("broker", [False, True])
def test_admitted_parser_and_handler_have_separate_bounded_mounts(staged_package, artifact_directory, broker):
    fixture = staged_package
    read_only = (artifact_directory.parent,) if broker else ()
    with _admission(fixture):
        with _deterministic_scope(fixture, sandbox_run_id="parser_demo"):
            fixture.executor.run_script(**{**fixture.kwargs, "run_id": "parser_demo"})
        assert fixture.calls[-1][0] == "release"
        with _deterministic_scope(fixture, read_only_paths=read_only, writable_paths=(artifact_directory,)):
            fixture.executor.run_script(**{
                **fixture.kwargs, "read_only_paths": read_only, "writable_paths": (artifact_directory,),
            })
        assert fixture.calls[-1][0] == "release"
    contexts = [value for kind, value in fixture.calls if kind == "policy"]
    parser, handler = contexts
    assert parser.run_id != handler.run_id
    assert parser.read_only_paths == (fixture.root,)
    assert parser.writable_paths == ()
    assert handler.read_only_paths == (fixture.root, *read_only)
    assert handler.writable_paths == (artifact_directory,)
    assert all(value[0] == "run_demo" for kind, value in fixture.calls if kind == "register")
    assert revisions._ADMITTED_PACKAGE.get() is None
    assert revisions._VERIFIED_PACKAGE.get() is None


@pytest.mark.parametrize("field,value", [
    ("workspace_id", "workspace_other"), ("ontology_id", "knowledge_package_other"),
    ("run_id", "run_other"), ("package_root", Path("/synthetic-other-package")),
])
def test_deterministic_execution_cannot_reuse_another_admission(staged_package, field, value):
    with _admission(staged_package):
        with pytest.raises(SandboxUnavailableError, match="admission scope mismatch"):
            with _deterministic_scope(staged_package, **{field: value}):
                pytest.fail("mismatched admission accepted")
    assert staged_package.calls == []


@pytest.mark.parametrize("invalid_mount", ["workspace", "package", "parent", "extra", "reader", "symlink"])
def test_deterministic_mounts_cannot_expand_package_or_artifact_access(
    staged_package, artifact_directory, invalid_mount,
):
    writable = (artifact_directory,)
    read_only = ()
    if invalid_mount == "workspace":
        writable = (staged_package.workspace,)
    elif invalid_mount == "package":
        writable = (staged_package.root,)
    elif invalid_mount == "parent":
        writable = (artifact_directory.parent,)
    elif invalid_mount == "extra":
        writable = (artifact_directory, staged_package.workspace)
    elif invalid_mount == "reader":
        read_only = (staged_package.workspace,)
    else:
        artifact_directory.rmdir()
        artifact_directory.symlink_to(staged_package.workspace, target_is_directory=True)
    with _admission(staged_package):
        with pytest.raises(SandboxUnavailableError, match="artifact mounts are invalid"):
            with _deterministic_scope(staged_package, writable_paths=writable, read_only_paths=read_only):
                pytest.fail("unbounded mounts accepted")
    assert staged_package.calls == []


def test_executor_rejects_mount_changes_after_deterministic_binding(staged_package, artifact_directory):
    with _admission(staged_package):
        with _deterministic_scope(staged_package, writable_paths=(artifact_directory,)):
            with pytest.raises(SandboxUnavailableError, match="cannot add filesystem mounts"):
                staged_package.executor.run_script(**{
                    **staged_package.kwargs, "writable_paths": (staged_package.workspace,),
                })
    assert not any(kind == "policy" for kind, _value in staged_package.calls)


def test_cleanup_failure_rejects_deterministic_output_and_restores_scope(
    staged_package, monkeypatch,
):
    def fail_release(*_args, **_kwargs):
        raise RuntimeError("synthetic release failure")

    monkeypatch.setattr(revisions, "release_run_sandbox", fail_release)
    with _admission(staged_package):
        with pytest.raises(SandboxUnavailableError, match="sandbox cleanup failed"):
            with _deterministic_scope(staged_package):
                staged_package.executor.run_script(**staged_package.kwargs)
        assert revisions._VERIFIED_PACKAGE.get() is None
    assert revisions._ADMITTED_PACKAGE.get() is None


@pytest.mark.parametrize("parser", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_deterministic_script_boundary_uses_admitted_copy_and_finishes_cleanup(
    staged_package, artifact_directory, monkeypatch, parser, cleanup_fails,
):
    from agent.direct_execution.service import DeterministicExecutionService
    import agent.tools.execute_script as scripts

    fixture = staged_package
    monkeypatch.setattr(scripts, "load_sandbox_config", lambda: fixture.executor._config)
    monkeypatch.setattr(scripts, "SandboxScriptExecutor", lambda: fixture.executor)
    monkeypatch.setattr(executor_module, "sandbox_runtime_revision", lambda: "synthetic_revision")

    def sandbox_exec(_sandbox_id, command, **_kwargs):
        stdout = "synthetic_revision" if "-c" in command else json.dumps({"value": "synthetic_value"})
        return SimpleNamespace(exit_code=0, stdout=stdout, stderr="")

    def failed_release(*_args, **_kwargs):
        raise RuntimeError("synthetic release failure")

    fixture.registry.exec_in_sandbox = sandbox_exec
    if cleanup_fails:
        monkeypatch.setattr(revisions, "release_run_sandbox", failed_release)
    request = SimpleNamespace(runtime_context=SimpleNamespace(
        workspace_id="workspace_demo", selected_ontology_id="knowledge_package_demo",
        run_id="run_demo", session_id="session_demo", turn_id=1,
    ))
    with _admission(fixture):
        result = DeterministicExecutionService._run_script(
            script_relative="scripts/run.py", cli_args=[], ontology_dir=fixture.root,
            project_root=fixture.kwargs["project_root"], request=request, timeout_seconds=5,
            env_overrides={"DETERMINISTIC_EXECUTION": "1"},
            caller="deterministic_parser" if parser else "deterministic_service", stdout_limit_bytes=1024,
            sandbox_run_id="parser_demo" if parser else None,
            sandbox_writable_paths=[] if parser else [artifact_directory],
        )
    if cleanup_fails:
        assert result.startswith("Error: Sandbox unavailable")
    else:
        assert json.loads(result) == {"value": "synthetic_value"}
        assert fixture.calls[-1][0] == "release"
    assert any(kind == "policy" for kind, _value in fixture.calls)
    assert revisions._ADMITTED_PACKAGE.get() is None
    assert revisions._VERIFIED_PACKAGE.get() is None
