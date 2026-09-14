"""Tests for jiuwenbox sandbox integration."""

from __future__ import annotations

from internal_access import internal
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import integration.sandbox.executor as sandbox_executor_module
import onto_sandbox_runtime.bounded_runner as bounded_runner_module
import pytest
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from backend_synthetic_credentials import synthetic_invalid_password, synthetic_secret
from integration.sandbox.client import JiuwenboxClient
from integration.sandbox.config import SandboxConfig, load_sandbox_config
from integration.sandbox.data_proxy import (
    DataConnectionSecretManager,
    sandbox_reachable_url,
)
from integration.sandbox.env_allowlist import build_script_env
from integration.sandbox.errors import (
    SandboxCreateError,
    SandboxExecError,
    SandboxUnavailableError,
    SandboxUnsupportedProviderError,
)
from integration.sandbox.executor import SandboxScriptExecutor
from integration.sandbox.paths import to_sandbox_script_path
from integration.sandbox.policy_builder import (
    OntoSandboxPolicyBuilder,
    PolicyContext,
    build_egress_allowed_ports,
    build_egress_target_rules,
    extract_port_from_url,
)
from integration.sandbox.registry import RunSandboxRegistry, sandbox_id_for_run


def _resolved_source(*, ontology_id: str, name: str, provider: str, config: dict):
    del name
    return ResolvedBusinessDataSource(
        ontology_id=ontology_id,
        mode="direct_provider",
        provider=provider,
        config=config,
    )


def test_sandbox_id_for_run_is_stable_and_valid_length():
    run_id = "run-abc-123"
    first = sandbox_id_for_run(run_id)
    second = sandbox_id_for_run(run_id)
    assert first == second
    assert first.startswith("onto-")
    assert 4 <= len(first) <= 16
    assert all(ch in "0123456789abcdefghijklmnopqrstuvwxyz_-" for ch in first)


def test_sandbox_reachable_url_resolves_scheme_less_host_without_adding_protocol(
    monkeypatch,
):
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.socket.getaddrinfo",
        lambda host, port, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.44", port))],
    )

    resolved = sandbox_reachable_url("service.example.net:7443/path")

    assert resolved == "192.0.2.44:7443/path"


def test_sandbox_reachable_url_preserves_explicit_secure_protocol(monkeypatch):
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.socket.getaddrinfo",
        lambda host, port, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.44", port))],
    )

    resolved = sandbox_reachable_url("https://service.example.net:7443/path")

    assert resolved == "https://192.0.2.44:7443/path"


def test_sandbox_reachable_url_fails_closed_when_dns_resolution_fails(monkeypatch):
    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror("synthetic resolution failure")

    monkeypatch.setattr(
        "integration.sandbox.data_proxy.socket.getaddrinfo",
        fail_resolution,
    )

    with pytest.raises(SandboxUnavailableError, match="could not be resolved"):
        sandbox_reachable_url("https://service.example.net/path")


def test_sandbox_reachable_url_pins_ipv6_only_endpoint(monkeypatch):
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.socket.getaddrinfo",
        lambda host, port, **_kwargs: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::44", port, 0, 0))],
    )

    resolved = sandbox_reachable_url("https://service.example.net:7443/path")

    assert resolved == "https://[2001:db8::44]:7443/path"


def test_create_sandbox_treats_409_as_conflict():
    class ConflictHttpClient:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def post(self, path, **kwargs):
            return _FakeResponse({"detail": "sandbox already exists"}, status_code=409)

    client = JiuwenboxClient.__new__(JiuwenboxClient)
    internal(client).client = ConflictHttpClient()

    with pytest.raises(SandboxCreateError, match=r"failed \(409\): sandbox already exists"):
        client.create_sandbox("onto-conflict", {"version": 1})


def test_jiuwenbox_control_plane_client_ignores_environment_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://192.0.2.10:1080")

    client = JiuwenboxClient("http://127.0.0.1:8321")
    try:
        assert internal(internal(client).client).trust_env is False
    finally:
        internal(client).client.close()


def test_jiuwenbox_control_plane_requires_explicit_protocol():
    with pytest.raises(ValueError, match="explicitly use HTTP or HTTPS"):
        JiuwenboxClient("localhost:8321")


def test_registry_single_flights_same_run_creation(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())
    create_started = threading.Event()
    allow_create_to_finish = threading.Event()

    class BlockingSandboxClient:
        def __init__(self):
            self.create_calls = []

        def create_sandbox(self, sandbox_id, policy, policy_mode):
            self.create_calls.append((sandbox_id, policy, policy_mode))
            create_started.set()
            assert allow_create_to_finish.wait(timeout=5)
            return {"id": sandbox_id, "phase": "ready"}

    fake_client = BlockingSandboxClient()
    internal(registry).client = fake_client
    ctx = _policy_context(tmp_path, run_id="run-concurrent")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.ensure, ctx)
        assert create_started.wait(timeout=5)
        second = pool.submit(registry.ensure, ctx)
        allow_create_to_finish.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert results == [sandbox_id_for_run(ctx.run_id)] * 2
    assert len(fake_client.create_calls) == 1


def test_registry_rejects_scope_change_for_existing_run(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())

    class ReadySandboxClient:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            return {"id": sandbox_id, "phase": "ready"}

    internal(registry).client = ReadySandboxClient()
    first_ctx = _policy_context(tmp_path, run_id="run-scope", ontology_id="ontology-a")
    second_ctx = _policy_context(tmp_path, run_id="run-scope", ontology_id="ontology-b")

    registry.ensure(first_ctx)

    with pytest.raises(SandboxCreateError, match="scope changed within the same run"):
        registry.ensure(second_ctx)


def test_registry_strict_release_failure_keeps_mapping_for_retry(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())

    class RetryDeleteSandboxClient:
        def __init__(self):
            self.create_calls = 0
            self.delete_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            self.create_calls += 1
            return {"id": sandbox_id, "phase": "ready"}

        def delete_sandbox(self, _sandbox_id):
            self.delete_calls += 1
            if self.delete_calls == 1:
                raise RuntimeError("temporary delete failure")

    fake_client = RetryDeleteSandboxClient()
    internal(registry).client = fake_client
    ctx = _policy_context(tmp_path, run_id="run-release-retry")
    sandbox_id = registry.ensure(ctx)

    with pytest.raises(RuntimeError, match="temporary delete failure"):
        registry.release(ctx.run_id, strict=True)

    assert registry.ensure(ctx) == sandbox_id
    assert fake_client.create_calls == 1
    registry.release(ctx.run_id, strict=True)
    assert fake_client.delete_calls == 2


def test_registry_releases_only_companions_registered_to_parent(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())

    class TrackingSandboxClient:
        def __init__(self):
            self.deleted = []

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            del policy, policy_mode
            return {"id": sandbox_id, "phase": "ready"}

        def delete_sandbox(self, sandbox_id):
            self.deleted.append(sandbox_id)

    fake_client = TrackingSandboxClient()
    internal(registry).client = fake_client
    unrelated_main = _policy_context(tmp_path, run_id="job-det-parser")
    companion = _policy_context(tmp_path, run_id="det-parser-opaque")
    unrelated_sandbox_id = registry.ensure(unrelated_main)
    registry.register_companion("job", companion.run_id)
    companion_sandbox_id = registry.ensure(companion)

    registry.release_companions("job", strict=True)

    assert fake_client.deleted == [companion_sandbox_id]
    assert registry.ensure(unrelated_main) == unrelated_sandbox_id
    assert registry.companion_run_ids("job") == ()


def test_registry_failed_companion_release_remains_registered_for_parent_retry(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())

    class RetryDeleteSandboxClient:
        def __init__(self):
            self.delete_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            del policy, policy_mode
            return {"id": sandbox_id, "phase": "ready"}

        def delete_sandbox(self, _sandbox_id):
            self.delete_calls += 1
            if self.delete_calls == 1:
                raise RuntimeError("temporary delete failure")

    fake_client = RetryDeleteSandboxClient()
    internal(registry).client = fake_client
    companion = _policy_context(tmp_path, run_id="det-parser-retry")
    registry.register_companion("parent-run", companion.run_id)
    registry.ensure(companion)

    with pytest.raises(RuntimeError, match="temporary delete failure"):
        registry.release_companions("parent-run", strict=True)

    assert registry.companion_run_ids("parent-run") == (companion.run_id,)
    registry.release_companions("parent-run", strict=True)
    assert fake_client.delete_calls == 2
    assert registry.companion_run_ids("parent-run") == ()


def test_registry_concurrent_release_waits_and_retries_failed_companion_delete(tmp_path):
    registry = RunSandboxRegistry(_sandbox_config())
    first_delete_started = threading.Event()
    allow_first_delete = threading.Event()
    second_release_started = threading.Event()
    second_release_finished = threading.Event()

    class BlockingRetryDeleteClient:
        def __init__(self):
            self.delete_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            del policy, policy_mode
            return {"id": sandbox_id, "phase": "ready"}

        def delete_sandbox(self, _sandbox_id):
            self.delete_calls += 1
            if self.delete_calls == 1:
                first_delete_started.set()
                assert allow_first_delete.wait(timeout=5)
                raise RuntimeError("temporary delete failure")

    fake_client = BlockingRetryDeleteClient()
    internal(registry).client = fake_client
    companion = _policy_context(tmp_path, run_id="det-parser-concurrent-release")
    registry.register_companion("parent-run", companion.run_id)
    registry.ensure(companion)

    def second_release():
        second_release_started.set()
        try:
            registry.release(companion.run_id, strict=True)
        finally:
            second_release_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.release, companion.run_id, strict=True)
        assert first_delete_started.wait(timeout=5)
        second = pool.submit(second_release)
        assert second_release_started.wait(timeout=5)
        assert not second_release_finished.wait(timeout=0.2)
        allow_first_delete.set()
        with pytest.raises(RuntimeError, match="temporary delete failure"):
            first.result(timeout=5)
        second.result(timeout=5)

    assert fake_client.delete_calls == 2
    assert registry.companion_run_ids("parent-run") == ()


def test_to_sandbox_script_path_uses_workspace_same_path(tmp_path):
    workspace_root = tmp_path / ".onto-runtime" / "workspaces" / "ws1"
    script_path = workspace_root / "ontologies" / "demo" / "scripts" / "check.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("print('ok')", encoding="utf-8")

    sandbox_path = to_sandbox_script_path(
        "ontologies/demo/scripts/check.py",
        project_root=workspace_root,
    )

    assert sandbox_path == str(script_path.resolve()).replace("\\", "/")


def test_build_script_env_uses_runtime_sdk_and_ontology_only(tmp_path):
    ontology_dir = str((tmp_path / "ontologies" / "demo").resolve()).replace("\\", "/")
    env = build_script_env(
        env_overrides={
            SCRIPT_BUSINESS_DATA_CONNECTION_ENV: '{"id":"x"}',
            "DETERMINISTIC_EXECUTION": "1",
            "ONTOLOGY_VERSION": "1.0.0",
            "RUN_ARTIFACT_DIR": "/tmp/artifacts",
            "RUN_ARTIFACT_MAX_FILE_BYTES": "10485760",
            "RUN_ARTIFACT_MAX_TOTAL_BYTES": "20971520",
            "RUN_ARTIFACT_MAX_COUNT": "32",
            "AUTH_SECRET": synthetic_secret("sandbox-credential-01"),
            "ONTO_X": synthetic_secret("sandbox-credential-01"),
        },
        ontology_dir=ontology_dir,
        runtime_python_path="/app/sandbox-runtime/python",
    )

    assert env["PYTHONPATH"] == "/app/sandbox-runtime/python:{}".format(ontology_dir)
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert SCRIPT_BUSINESS_DATA_CONNECTION_ENV in env
    assert env["DETERMINISTIC_EXECUTION"] == "1"
    assert env["ONTOLOGY_VERSION"] == "1.0.0"
    assert env["RUN_ARTIFACT_DIR"] == "/tmp/artifacts"
    assert env["RUN_ARTIFACT_MAX_FILE_BYTES"] == "10485760"
    assert env["RUN_ARTIFACT_MAX_TOTAL_BYTES"] == "20971520"
    assert env["RUN_ARTIFACT_MAX_COUNT"] == "32"
    assert "AUTH_SECRET" not in env
    assert "ONTO_X" not in env
    assert not env["PYTHONPATH"].startswith("/backend:")
    assert "/backend/.python-deps" not in env["PYTHONPATH"]


def test_policy_builder_mounts_workspace_and_runtime_same_path(tmp_path):
    workspace_root = tmp_path / ".onto-runtime" / "workspaces" / "ws"
    workspace_root.mkdir(parents=True)
    runtime_python = Path("/app/sandbox-runtime/python")

    ctx = PolicyContext(
        run_id="run-1",
        workspace_id="ws",
        ontology_id="demo",
        workspace_root=workspace_root,
        sandbox_runtime_python_dir=runtime_python,
        data_proxy_url="http://192.0.2.44:8322/onto-data/run/conn",
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
    )
    policy = OntoSandboxPolicyBuilder.build(ctx)

    bind_mounts = policy["filesystem_policy"]["bind_mounts"]
    bind_by_sandbox_path = {item["sandbox_path"]: item for item in bind_mounts}
    workspace_path = str(workspace_root.resolve()).replace("\\", "/")

    assert bind_by_sandbox_path[workspace_path]["host_path"] == workspace_path
    assert (
        bind_by_sandbox_path["/app/sandbox-runtime/python"]["host_path"]
        == "/app/sandbox-runtime/python"
    )
    assert (
        bind_by_sandbox_path["/app/sandbox-runtime/runtime-contract.json"]["host_path"]
        == "/app/sandbox-runtime/runtime-contract.json"
    )
    assert (
        "/app/sandbox-runtime/runtime-contract.json"
        in policy["filesystem_policy"]["read_only"]
    )
    assert all(
        item["sandbox_path"] not in {"/backend", "/workspace", "/ontology"}
        for item in bind_mounts
    )
    assert policy["network"]["egress"]["default"] == "deny"
    assert policy["network"]["egress"]["allowed_domains"] == []
    assert policy["network"]["egress"]["allowed_ips"] == ["192.0.2.44/32"]
    assert policy["network"]["egress"]["allowed_ports"] == [8322]
    assert policy["network"]["egress"]["blocked_ips"] == ["169.254.169.254/32"]
    assert policy["network"]["egress"]["blocked_ports"] == [53]


def test_policy_builder_mounts_only_explicit_artifact_directory_read_write(tmp_path):
    workspace_root = tmp_path / "workspace"
    artifact_root = tmp_path / "artifacts"
    workspace_root.mkdir()
    artifact_root.mkdir()
    ctx = PolicyContext(
        run_id="run-artifact",
        workspace_id="ws",
        ontology_id="demo",
        workspace_root=workspace_root,
        sandbox_runtime_python_dir=Path("/app/sandbox-runtime/python"),
        data_proxy_url=None,
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
        writable_paths=(artifact_root,),
    )

    policy = OntoSandboxPolicyBuilder.build(ctx)
    artifact_path = str(artifact_root.resolve()).replace("\\", "/")
    mounts = {item["sandbox_path"]: item for item in policy["filesystem_policy"]["bind_mounts"]}

    assert mounts[artifact_path]["mode"] == "rw"
    assert artifact_path in policy["filesystem_policy"]["read_write"]


def test_executor_mounts_only_workspace_for_global_project_root(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "ws"
    ontology_dir = workspace_root / "ontologies" / "demo"
    script = ontology_dir / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    provider_socket_dir = tmp_path / "provider-capability"
    provider_socket_dir.mkdir()

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspace_project_root",
        lambda workspace_id: workspace_root,
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspaces_root",
        lambda: runtime_root / "workspaces",
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_data_connection_secret_manager",
        lambda: SimpleNamespace(prepare=lambda **kwargs: SimpleNamespace(env={}, data_proxy_url=None)),
    )

    captured = {}

    class RecordingRegistry:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def ensure(self, ctx):
            captured["ctx"] = ctx
            return "onto-test"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def exec_in_sandbox(self, sandbox_id, command, **kwargs):
            captured["sandbox_id"] = sandbox_id
            captured["command"] = command
            captured.update(kwargs)
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_run_sandbox_registry",
        lambda: RecordingRegistry(),
    )

    result = SandboxScriptExecutor(_sandbox_config()).run_script(
        script_path="workspaces/ws/ontologies/demo/scripts/check.py",
        cli_args=[],
        project_root=runtime_root,
        ontology_dir=ontology_dir,
        run_id="run-workspace-mount",
        workspace_id="ws",
        ontology_id="demo",
        timeout_seconds=30,
        network_endpoint="neo4j+s://192.0.2.24:7687",
        read_only_paths=[provider_socket_dir],
    )

    assert result.returncode == 0
    assert captured["ctx"].workspace_root == workspace_root.resolve()
    assert captured["ctx"].data_proxy_url == "neo4j+s://192.0.2.24:7687"
    assert captured["ctx"].read_only_paths == (
        workspace_root.resolve(),
        provider_socket_dir.resolve(),
    )
    assert captured["workdir"] == str(workspace_root.resolve()).replace("\\", "/")
    assert captured["command"][2] == str(script.resolve()).replace("\\", "/")
    policy = OntoSandboxPolicyBuilder.build(captured["ctx"])
    mounted_paths = {item["sandbox_path"] for item in policy["filesystem_policy"]["bind_mounts"]}
    assert str(workspace_root.resolve()).replace("\\", "/") in mounted_paths
    assert str(provider_socket_dir.resolve()).replace("\\", "/") in mounted_paths
    assert str(runtime_root.resolve()).replace("\\", "/") not in mounted_paths


def test_executor_can_restrict_mount_and_workdir_to_ontology(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "ws"
    ontology_dir = workspace_root / "ontologies" / "demo" / "versions" / "1.0.0"
    script = ontology_dir / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspace_project_root",
        lambda _workspace_id: workspace_root,
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspaces_root",
        lambda: runtime_root / "workspaces",
    )
    captured = {}

    class RecordingRegistry:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def ensure(self, ctx):
            captured["ctx"] = ctx
            return "onto-restricted"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def exec_in_sandbox(self, _sandbox_id, _command, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_run_sandbox_registry",
        lambda: RecordingRegistry(),
    )

    result = SandboxScriptExecutor(_sandbox_config()).run_script(
        script_path="workspaces/ws/ontologies/demo/versions/1.0.0/scripts/check.py",
        cli_args=[],
        project_root=runtime_root,
        ontology_dir=ontology_dir,
        run_id="run-restricted",
        workspace_id="ws",
        ontology_id="demo",
        timeout_seconds=30,
        prepare_data_connection=False,
        restrict_filesystem_to_ontology=True,
    )

    assert result.returncode == 0
    ontology_path = str(ontology_dir.resolve()).replace("\\", "/")
    workspace_path = str(workspace_root.resolve()).replace("\\", "/")
    assert captured["workdir"] == ontology_path
    assert captured["ctx"].read_only_paths == (ontology_dir.resolve(),)
    policy = OntoSandboxPolicyBuilder.build(captured["ctx"])
    mounted_paths = {item["sandbox_path"] for item in policy["filesystem_policy"]["bind_mounts"]}
    assert ontology_path in mounted_paths
    assert workspace_path not in mounted_paths


def test_sandbox_executor_wraps_commands_with_output_limits(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "ws"
    ontology_dir = workspace_root / "ontologies" / "demo"
    script = ontology_dir / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspace_project_root",
        lambda _workspace_id: workspace_root,
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspaces_root",
        lambda: runtime_root / "workspaces",
    )
    captured = {}
    attestations = []

    monkeypatch.setattr(
        sandbox_executor_module,
        "_attest_sandbox_runtime",
        lambda registry, sandbox_id, **kwargs: attestations.append((registry, sandbox_id, kwargs)),
    )

    class RecordingRegistry:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def ensure(self, _ctx):
            return "onto-bounded"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def exec_in_sandbox(self, _sandbox_id, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_run_sandbox_registry",
        lambda: RecordingRegistry(),
    )

    SandboxScriptExecutor(_sandbox_config()).run_script(
        script_path="workspaces/ws/ontologies/demo/scripts/check.py",
        cli_args=[],
        project_root=runtime_root,
        ontology_dir=ontology_dir,
        run_id="run-bounded",
        workspace_id="ws",
        ontology_id="demo",
        timeout_seconds=30,
        prepare_data_connection=False,
        env_overrides={
            "DETERMINISTIC_EXECUTION": "1",
            SCRIPT_BUSINESS_DATA_CONNECTION_ENV: '{"password":"synthetic-secret"}',
        },
        stdout_limit_bytes=16,
        stderr_limit_bytes=8,
    )

    assert captured["command"][:3] == [
        "/usr/bin/python3",
        "-m",
        "onto_sandbox_runtime.bounded_runner",
    ]
    assert captured["command"][3:7] == [
        "--stdout-limit-bytes",
        "16",
        "--stderr-limit-bytes",
        "8",
    ]
    assert captured["command"][7:9] == [
        "--file-size-limit-bytes",
        "0",
    ]
    bounded_separator = captured["command"].index("--")
    assert captured["command"][bounded_separator + 1 :] == [
        "/usr/bin/python3",
        "-u",
        "-P",
        "-m",
        "onto_sandbox_runtime.deterministic_runner",
        "--",
        str(script.resolve()).replace("\\", "/"),
    ]
    assert len(attestations) == 1
    assert attestations[0][1] == "onto-bounded"
    assert SCRIPT_BUSINESS_DATA_CONNECTION_ENV not in captured["env"]


def test_deterministic_runtime_attestation_rejects_mismatched_image(monkeypatch):
    class MismatchedRegistry:
        def __init__(self):
            self.command = None
            self.kwargs = {}

        def exec_in_sandbox(self, _sandbox_id, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            return SimpleNamespace(
                exit_code=0,
                stdout="sha256:stale-image\n",
                stderr="",
            )

    registry = MismatchedRegistry()
    monkeypatch.setattr(
        sandbox_executor_module,
        "sandbox_runtime_revision",
        lambda: "sha256:expected-image",
    )

    with pytest.raises(SandboxUnavailableError, match="runtime revision mismatch"):
        internal(sandbox_executor_module).attest_sandbox_runtime(
            registry,
            "onto-stale",
            config=_sandbox_config(),
            timeout_seconds=30,
        )

    assert registry.command[-1] == "/app/sandbox-runtime/python/.runtime-revision"
    assert registry.kwargs["timeout_seconds"] == 5


def test_deterministic_runtime_attestation_accepts_matching_image(monkeypatch):
    class MatchingRegistry:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def exec_in_sandbox(self, _sandbox_id, _command, **_kwargs):
            return SimpleNamespace(
                exit_code=0,
                stdout="sha256:expected-image\n",
                stderr="",
            )

    monkeypatch.setattr(
        sandbox_executor_module,
        "sandbox_runtime_revision",
        lambda: "sha256:expected-image",
    )

    internal(sandbox_executor_module).attest_sandbox_runtime(
        MatchingRegistry(),
        "onto-current",
        config=_sandbox_config(),
        timeout_seconds=30,
    )


def test_bounded_sandbox_runner_stops_oversized_output():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "onto_sandbox_runtime.bounded_runner",
            "--stdout-limit-bytes",
            "32",
            "--stderr-limit-bytes",
            "32",
            "--",
            sys.executable,
            "-c",
            "print('x' * 10000)",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 125
    assert completed.stdout == ""
    assert "ONTO_PUBLIC_ERROR:" in completed.stderr
    assert '"code":"OUTPUT_LIMIT"' in completed.stderr
    assert "stdout output exceeds limit" in completed.stderr


def test_bounded_sandbox_runner_rejects_non_python_executable():
    with pytest.raises(ValueError, match="trusted Python interpreter"):
        bounded_runner_module.run_bounded(
            [sys.executable + ".untrusted", "--version"],
            stdout_limit_bytes=32,
            stderr_limit_bytes=32,
        )


def test_deterministic_runtime_runner_allows_contract_capabilities(tmp_path):
    (tmp_path / "helper.py").write_text(
        "def normalize(value):\n    return value.strip()\n",
        encoding="utf-8",
    )
    script = tmp_path / "handler.py"
    script.write_text(
        "import argparse\n"
        "import json\n"
        "from helper import normalize\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--code', required=True)\n"
        "args = parser.parse_args()\n"
        "print(json.dumps({'value': normalize(args.code)}))\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "onto_sandbox_runtime.deterministic_runner",
            "--",
            str(script),
            "--code",
            "synthetic",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"value": "synthetic"}


def test_deterministic_runtime_runner_blocks_dynamic_import_bypass(tmp_path):
    script = tmp_path / "handler.py"
    script.write_text(
        "loader = __builtins__['__import__']\nmodule = loader('os')\nprint(module.getpid())\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "onto_sandbox_runtime.deterministic_runner",
            "--",
            str(script),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "outside the deterministic runtime capability contract" in completed.stderr


@pytest.mark.parametrize(
    "handler_source",
    (
        "from requests.utils import os\nprint(os.urandom(8).hex())\n",
        "from requests import utils\nprint(utils.os.urandom(8).hex())\n",
    ),
)
def test_deterministic_runtime_runner_blocks_external_module_reexport(
    tmp_path,
    handler_source,
):
    script = tmp_path / "handler.py"
    script.write_text(handler_source, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "onto_sandbox_runtime.deterministic_runner",
            "--",
            str(script),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert (
        "deterministic runtime capability contract" in completed.stderr
        or "unavailable to deterministic package code" in completed.stderr
    )


def test_deterministic_runtime_runner_rejects_local_module_collision(tmp_path):
    (tmp_path / "json.py").write_text("value = 'shadowed'\n", encoding="utf-8")
    script = tmp_path / "handler.py"
    script.write_text("import json\nprint(json.dumps({'value': 'ok'}))\n", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])

    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            "-m",
            "onto_sandbox_runtime.deterministic_runner",
            "--",
            str(script),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "collides with a runtime module" in completed.stderr


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX resource limits")
def test_bounded_sandbox_runner_stops_oversized_artifact_file(tmp_path):
    artifact_path = tmp_path / "oversized.bin"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "onto_sandbox_runtime.bounded_runner",
            "--stdout-limit-bytes",
            "1024",
            "--stderr-limit-bytes",
            "4096",
            "--file-size-limit-bytes",
            "128",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; Path({!r}).write_bytes(b'x' * 4096)".format(str(artifact_path)),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 126
    assert '"code":"ARTIFACT_LIMIT"' in completed.stderr
    assert "artifact file exceeds limit" in completed.stderr
    assert artifact_path.stat().st_size <= 128


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires POSIX process groups")
def test_bounded_sandbox_runner_stops_descendants_after_leader_exits(tmp_path):
    marker = tmp_path / "late-child-output.txt"
    child_code = (
        "import pathlib,time; time.sleep(0.8); pathlib.Path({!r}).write_text('late', encoding='utf-8')"
    ).format(str(marker))
    leader_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable, '-c', {!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print('ok')"
    ).format(child_code)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "onto_sandbox_runtime.bounded_runner",
            "--stdout-limit-bytes",
            "1024",
            "--stderr-limit-bytes",
            "1024",
            "--",
            sys.executable,
            "-c",
            leader_code,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "ok\n"
    time.sleep(1)
    assert not marker.exists()


def test_executor_cancellation_releases_running_sandbox(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "ws"
    ontology_dir = workspace_root / "ontologies" / "demo"
    script = ontology_dir / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspace_project_root",
        lambda _workspace_id: workspace_root,
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_workspaces_root",
        lambda: runtime_root / "workspaces",
    )
    monkeypatch.setattr(
        sandbox_executor_module,
        "get_data_connection_secret_manager",
        lambda: SimpleNamespace(prepare=lambda **_kwargs: SimpleNamespace(env={}, data_proxy_url=None)),
    )
    started = threading.Event()
    released = threading.Event()

    class BlockingRegistry:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def ensure(self, _ctx):
            return "onto-cancel"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def exec_in_sandbox(self, _sandbox_id, _command, **_kwargs):
            started.set()
            released.wait(timeout=2)
            return SimpleNamespace(exit_code=1, stdout="", stderr="cancelled")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def release(self, run_id):
            assert run_id == "run-cancel"
            released.set()

    monkeypatch.setattr(
        sandbox_executor_module,
        "get_run_sandbox_registry",
        lambda: BlockingRegistry(),
    )
    cancel_event = threading.Event()

    def cancel_after_start():
        assert started.wait(timeout=1)
        cancel_event.set()

    threading.Thread(target=cancel_after_start, daemon=True).start()

    with pytest.raises(SandboxExecError, match="cancelled"):
        SandboxScriptExecutor(_sandbox_config()).run_script(
            script_path="workspaces/ws/ontologies/demo/scripts/check.py",
            cli_args=[],
            project_root=runtime_root,
            ontology_dir=ontology_dir,
            run_id="run-cancel",
            workspace_id="ws",
            ontology_id="demo",
            timeout_seconds=30,
            cancel_event=cancel_event,
        )

    assert released.is_set()


def test_executor_requires_workspace_id_before_creating_resources(tmp_path):
    executor = SandboxScriptExecutor(_sandbox_config())

    with pytest.raises(SandboxUnavailableError, match="workspace_id is required"):
        executor.run_script(
            script_path="script.py",
            cli_args=[],
            project_root=tmp_path,
            ontology_dir=None,
            run_id="run-missing-workspace",
            workspace_id=None,
            ontology_id="demo",
            timeout_seconds=30,
        )


def test_policy_builder_allows_ip_data_proxy(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    ctx = PolicyContext(
        run_id="run-ip",
        workspace_id="ws",
        ontology_id="demo",
        workspace_root=workspace_root,
        sandbox_runtime_python_dir=Path("/app/sandbox-runtime/python"),
        data_proxy_url="http://192.0.2.8:8322/onto-data/run/conn",
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
    )
    policy = OntoSandboxPolicyBuilder.build(ctx)

    assert policy["network"]["egress"]["allowed_domains"] == []
    assert policy["network"]["egress"]["allowed_ips"] == ["192.0.2.8/32"]
    assert policy["network"]["egress"]["allowed_ports"] == [8322]


def test_egress_target_rules_use_exact_host_prefixes_for_ipv4_and_ipv6():
    assert build_egress_target_rules("http://192.0.2.8:8322/path") == (
        [],
        ["192.0.2.8/32"],
    )
    assert build_egress_target_rules("bolt://[2001:db8::1]:7687") == (
        [],
        ["2001:db8::1/128"],
    )


def test_policy_builder_never_combines_domain_resolution_with_pinned_ip(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    ctx = PolicyContext(
        run_id="run-tls",
        workspace_id="ws",
        ontology_id="demo",
        workspace_root=workspace_root,
        sandbox_runtime_python_dir=Path("/app/sandbox-runtime/python"),
        data_proxy_url="neo4j+s://192.0.2.24:7687",
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
    )

    policy = OntoSandboxPolicyBuilder.build(ctx)
    egress = policy["network"]["egress"]

    assert egress["allowed_domains"] == []
    assert egress["allowed_ips"] == ["192.0.2.24/32"]
    assert egress["allowed_ports"] == [7687]
    assert egress["blocked_ports"] == [53]


def test_policy_builder_rejects_unpinned_domain_target():
    with pytest.raises(ValueError, match="pinned to an IP"):
        build_egress_target_rules("neo4j+s://snapshot.example.com:7687")


def test_extract_port_from_url_defaults_by_scheme():
    assert extract_port_from_url("https://proxy.example.com") == 443
    assert extract_port_from_url("http://proxy.example.com") == 80
    assert extract_port_from_url("neo4j://graph.example.com") == 7687
    assert extract_port_from_url("bolt+s://graph.example.com") == 7687
    assert extract_port_from_url("http://proxy.example.com:8322") == 8322
    assert extract_port_from_url("proxy.example.com:8322") == 8322


def test_build_egress_allowed_ports_denies_when_no_proxy():
    assert build_egress_allowed_ports(None) == []


def test_data_proxy_manager_creates_safe_env_without_raw_secret(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": synthetic_secret("sandbox-credential-04"),
            "scene": "demo",
            "timeout_seconds": 20,
            "max_retries": 1,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    prepared = manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")
    payload = json.loads(prepared.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV])

    assert prepared.data_proxy_url.startswith("http://")
    assert ":8322/onto-" in prepared.data_proxy_url
    assert payload["version"] == 1
    assert payload["endpoint"] == prepared.data_proxy_url
    assert payload["auth"]["token"].startswith("onto-placeholder-")
    assert synthetic_secret("sandbox-credential-04") not in prepared.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "https://nl2sql.example.com" not in prepared.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert fake_client.created[0]["api_key"] == synthetic_secret("sandbox-credential-04")
    assert fake_client.started


@pytest.mark.parametrize(
    "provider",
    ["dataagent", "nl2sql_template", "neo4j", "hugegraph"],
)
def test_data_proxy_manager_rejects_script_disallowed_providers(monkeypatch, provider):
    connection = _resolved_source(
        ontology_id="disallowed-ontology",
        name="Disallowed ontology",
        provider=provider,
        config={
            "url": "https://dataagent.example.com",
            "token": synthetic_secret("sandbox-credential-04"),
            "timeout_seconds": 20,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(
        SandboxUnsupportedProviderError,
        match="unsupported sandbox business data provider '{}'".format(provider),
    ) as exc_info:
        manager.prepare(run_id="run-disallowed", workspace_id="ws", ontology_id="demo")

    assert exc_info.value.provider == provider
    assert fake_client.created == []
    assert fake_client.started == []


def test_data_proxy_manager_fails_closed_when_route_create_returns_400(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn-conflict",
        name="Conflicting route",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": synthetic_secret("sandbox-credential-04"),
        },
    )

    class ConflictClient(_FakeJiuwenboxClient):
        def create_proxy(self, **payload):
            self.created.append(payload)
            raise SandboxUnavailableError("create proxy failed (400): route already exists")

    fake_client = ConflictClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match=r"failed \(400\)"):
        manager.prepare(run_id="run-conflict", workspace_id="ws", ontology_id="demo")

    assert len(fake_client.created) == 1
    assert fake_client.started == []
    assert internal(manager).routes_by_run.get("run-conflict") in (None, {})


def test_data_proxy_manager_rolls_back_when_route_start_fails(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn-start-fail",
        name="Start failure route",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": synthetic_secret("sandbox-credential-04"),
        },
    )

    class StartFailureClient(_FakeJiuwenboxClient):
        def start_proxy(self, proxy_name):
            self.started.append(proxy_name)
            raise SandboxUnavailableError("start proxy failed (503): jiuwenbox unavailable")

    fake_client = StartFailureClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match=r"start proxy failed \(503\)"):
        manager.prepare(run_id="run-start-fail", workspace_id="ws", ontology_id="demo")

    assert len(fake_client.created) == 1
    assert fake_client.deleted == fake_client.started
    assert internal(manager).routes_by_run.get("run-start-fail") in (None, {})


def test_data_proxy_manager_authenticates_mindsdb_once_per_run(monkeypatch):
    connection = _resolved_source(
        ontology_id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "https://mindsdb.example.com",
            "login": "mindsdb-user",
            "password": synthetic_invalid_password("sandbox-credential-03"),
            "requires_auth": True,
            "default_database": "analytics",
            "timeout_seconds": 600,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    login_calls = []

    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    def fake_post(url, **kwargs):
        login_calls.append((url, kwargs))
        return _FakeResponse({"token": synthetic_secret("sandbox-credential-02")})

    monkeypatch.setattr("integration.sandbox.data_proxy.requests.post", fake_post)

    first = manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")
    second = manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")
    payload = json.loads(first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV])

    assert first == second
    assert login_calls == [
        (
            "https://mindsdb.example.com/api/login",
            {
                "json": {"username": "mindsdb-user", "password": synthetic_invalid_password("sandbox-credential-03")},
                "timeout": 10,
                "verify": True,
            },
        )
    ]
    assert fake_client.created[0]["api_key"] == synthetic_secret("sandbox-credential-02")
    assert payload["version"] == 1
    assert payload["endpoint"] == first.data_proxy_url
    assert payload["auth"]["token"].startswith("onto-placeholder-")
    assert payload["options"]["default_database"] == "analytics"
    assert synthetic_invalid_password("sandbox-credential-03") not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "mindsdb-user" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert synthetic_secret("sandbox-credential-02") not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "https://mindsdb.example.com" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]


def test_data_proxy_manager_rejects_failed_mindsdb_login(monkeypatch):
    connection = _resolved_source(
        ontology_id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "https://mindsdb.example.com",
            "login": "mindsdb-user",
            "password": synthetic_invalid_password("sandbox-credential-05"),
            "requires_auth": True,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.requests.post",
        lambda *_, **__: _FakeResponse({"error": "unauthorized"}, status_code=401),
    )

    with pytest.raises(SandboxUnavailableError, match=r"authentication failed \(HTTP 401\)"):
        manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")

    assert fake_client.created == []


def test_data_proxy_manager_blocks_http_mindsdb_password_before_login(monkeypatch):
    connection = _resolved_source(
        ontology_id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "http://mindsdb.internal:47334",
            "login": "mindsdb-user",
            "password": synthetic_invalid_password("sandbox-credential-03"),
            "requires_auth": True,
        },
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=False))
    internal(manager).client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    def unexpected_post(*args, **kwargs):
        raise AssertionError("MindsDB login must not run before transport validation")

    monkeypatch.setattr("integration.sandbox.data_proxy.requests.post", unexpected_post)

    with pytest.raises(SandboxUnavailableError, match="requires TLS"):
        manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")


def test_data_proxy_manager_rejects_cross_scope_run_reuse(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn1",
        name="Conn 1",
        provider="mindsdb",
        config={"url": "http://198.51.100.27:47334", "requires_auth": False},
    )
    manager = DataConnectionSecretManager(_sandbox_config())
    internal(manager).client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")
    with pytest.raises(SandboxUnavailableError):
        manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="other")


def test_data_proxy_manager_blocks_http_secret_when_disabled_globally(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={"url": "http://192.0.2.8:8080", "token": synthetic_secret("sandbox-credential-04")},
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=False))
    internal(manager).client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match="requires TLS"):
        manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")


def test_data_proxy_manager_allows_http_secret_when_enabled_globally(monkeypatch):
    connection = _resolved_source(
        ontology_id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={"url": "http://192.0.2.8:8080", "token": synthetic_secret("sandbox-credential-04")},
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=True))
    internal(manager).client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    prepared = manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")

    assert prepared.data_proxy_url.startswith("http://")
    assert ":8322/onto-" in prepared.data_proxy_url
    assert internal(manager).client.created[0]["api_key"] == synthetic_secret("sandbox-credential-04")


def test_sandbox_config_ignores_system_config_section(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        json.dumps(
            {
                "sandbox": {
                    "enabled": True,
                    "fallback_local": False,
                    "memory_max": "2G",
                    "cpu_max": 4.0,
                    "pids_max": 256,
                    "client_timeout_seconds": 99.0,
                    "secret_allow_plain_http": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    for env_name in (
        "SANDBOX_EXECUTION_MODE",
        "SANDBOX_ENABLED",
        "SANDBOX_FALLBACK_LOCAL",
        "SANDBOX_MEMORY_MAX",
        "SANDBOX_CPU_MAX",
        "SANDBOX_PIDS_MAX",
        "SANDBOX_CLIENT_TIMEOUT",
        "SANDBOX_SECRET_ALLOW_PLAIN_HTTP",
    ):
        monkeypatch.delenv(env_name, raising=False)

    load_sandbox_config.cache_clear()
    try:
        config = load_sandbox_config()
    finally:
        load_sandbox_config.cache_clear()

    assert config == SandboxConfig()


@pytest.mark.parametrize(
    ("mode", "expected_enabled"),
    [("legacy", False), ("prefer", True)],
)
def test_sandbox_execution_mode_overrides_legacy_boolean_flags(
    monkeypatch,
    mode,
    expected_enabled,
):
    monkeypatch.setenv("SANDBOX_EXECUTION_MODE", mode)
    monkeypatch.setenv("SANDBOX_ENABLED", "false" if expected_enabled else "true")
    monkeypatch.setenv("SANDBOX_FALLBACK_LOCAL", "false")

    load_sandbox_config.cache_clear()
    try:
        config = load_sandbox_config()
    finally:
        load_sandbox_config.cache_clear()

    assert config.execution_mode == mode
    assert config.enabled is expected_enabled
    assert config.fallback_local is True


def test_sandbox_execution_mode_absence_preserves_legacy_boolean_flags(monkeypatch):
    monkeypatch.delenv("SANDBOX_EXECUTION_MODE", raising=False)
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_FALLBACK_LOCAL", "false")

    load_sandbox_config.cache_clear()
    try:
        config = load_sandbox_config()
    finally:
        load_sandbox_config.cache_clear()

    assert config.enabled is True
    assert config.fallback_local is False


@pytest.mark.parametrize("invalid_mode", ["mandatory", "enforce"])
def test_invalid_sandbox_execution_mode_falls_back_to_legacy(
    monkeypatch,
    caplog,
    invalid_mode,
):
    monkeypatch.setenv("SANDBOX_EXECUTION_MODE", invalid_mode)
    monkeypatch.setenv("SANDBOX_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_FALLBACK_LOCAL", "false")

    load_sandbox_config.cache_clear()
    try:
        with caplog.at_level(logging.WARNING, logger="integration.sandbox.config"):
            config = load_sandbox_config()
    finally:
        load_sandbox_config.cache_clear()

    assert config.execution_mode == "legacy"
    assert config.enabled is False
    assert config.fallback_local is True
    record = caplog.records[-1]
    assert record.security_event == "sandbox_invalid_execution_mode"
    assert record.execution_mode == "legacy"
    assert invalid_mode not in record.getMessage()


def test_sandbox_config_keeps_topology_fields_fixed(monkeypatch):
    monkeypatch.setenv("JIUWENBOX_URL", "http://unexpected-jiuwenbox:8080")
    monkeypatch.setenv("SANDBOX_PYTHON_BIN", "/custom/python")
    monkeypatch.setenv("SANDBOX_RUNTIME_ROOT", "/custom/runtime")
    monkeypatch.setenv("SANDBOX_DATA_PROXY_BASE_URL", "http://unexpected-proxy:8322")

    load_sandbox_config.cache_clear()
    try:
        config = load_sandbox_config()
    finally:
        load_sandbox_config.cache_clear()

    assert config.jiuwenbox_url == "unix:///run/jiuwenbox/jiuwenbox.sock"
    assert config.python_bin == "/usr/bin/python3"
    assert config.sandbox_runtime_root == "/app/sandbox-runtime"
    assert config.data_proxy_base_url == "http://jiuwenbox:8322"


def _policy_context(tmp_path, *, run_id: str, ontology_id: str = "demo"):
    workspace_root = tmp_path / "workspace"
    runtime_python_dir = tmp_path / "sandbox-runtime" / "python"
    workspace_root.mkdir(parents=True, exist_ok=True)
    runtime_python_dir.mkdir(parents=True, exist_ok=True)
    return PolicyContext(
        run_id=run_id,
        workspace_id="workspace-a",
        ontology_id=ontology_id,
        workspace_root=workspace_root,
        sandbox_runtime_python_dir=runtime_python_dir,
        data_proxy_url="http://192.0.2.8:8322/onto-route",
        memory_max="512M",
        cpu_max=1.0,
        pids_max=64,
    )


def _sandbox_config(**overrides):
    values = {
        "enabled": True,
        "jiuwenbox_url": "http://jiuwenbox:8080",
        "fallback_local": True,
        "memory_max": "512M",
        "cpu_max": 1.0,
        "pids_max": 64,
        "client_timeout_seconds": 30.0,
        "python_bin": "/usr/bin/python3",
        "sandbox_runtime_root": "/app/sandbox-runtime",
        "data_proxy_base_url": "http://192.0.2.44:8322",
        "secret_allow_plain_http": True,
    }
    values.update(overrides)
    return SandboxConfig(**values)


class _FakeJiuwenboxClient:
    def __init__(self):
        self.created = []
        self.started = []
        self.deleted = []

    def create_proxy(self, **payload):
        self.created.append(payload)
        return {"name": "proxy"}

    def start_proxy(self, proxy_name):
        self.started.append(proxy_name)
        return {"status": "running"}

    def delete_proxy(self, proxy_name):
        self.deleted.append(proxy_name)


class _FakeResponse:
    def __init__(self, payload, *, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self._payload

    def close(self):
        self.closed = True
