"""Synthetic checks for the verified package handoff and its mount boundary."""

from types import SimpleNamespace

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


def test_handoff_rejects_a_symlink_to_another_staging_directory(staged_package):
    root = staged_package.root
    renamed = root.with_name("other")
    root.rename(renamed)
    root.symlink_to(renamed, target_is_directory=True)
    with pytest.raises(SandboxUnavailableError, match="staging boundary"):
        with _scope(staged_package):
            pytest.fail("unsafe scope admitted")
    assert staged_package.calls == []
