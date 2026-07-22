"""Tests for jiuwenbox sandbox integration."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import integration.sandbox.executor as sandbox_executor_module
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV
from integration.sandbox.config import SandboxConfig, load_sandbox_config
from integration.sandbox.client import JiuwenboxClient
from integration.sandbox.data_proxy import DataConnectionSecretManager
from integration.sandbox.env_allowlist import build_script_env, collect_script_config_env
from integration.sandbox.errors import SandboxCreateError, SandboxUnavailableError
from integration.sandbox.executor import SandboxScriptExecutor
from integration.sandbox.paths import to_sandbox_script_path
from integration.sandbox.policy_builder import (
    OntoSandboxPolicyBuilder,
    PolicyContext,
    build_egress_allowed_ports,
    extract_host_from_url,
    extract_port_from_url,
)
from integration.sandbox.registry import RunSandboxRegistry, sandbox_id_for_run


def _resolved_source(*, id: str, name: str, provider: str, config: dict):
    del name
    return ResolvedBusinessDataSource(
        ontology_id=id,
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


def test_create_sandbox_treats_409_as_conflict():
    class ConflictHttpClient:
        def post(self, path, **kwargs):
            return _FakeResponse({"detail": "sandbox already exists"}, status_code=409)

    client = JiuwenboxClient.__new__(JiuwenboxClient)
    client._client = ConflictHttpClient()

    with pytest.raises(SandboxCreateError, match=r"failed \(409\): sandbox already exists"):
        client.create_sandbox("onto-conflict", {"version": 1})


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
    registry._client = fake_client
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
        def create_sandbox(self, sandbox_id, policy, policy_mode):
            return {"id": sandbox_id, "phase": "ready"}

    registry._client = ReadySandboxClient()
    first_ctx = _policy_context(tmp_path, run_id="run-scope", ontology_id="ontology-a")
    second_ctx = _policy_context(tmp_path, run_id="run-scope", ontology_id="ontology-b")

    registry.ensure(first_ctx)

    with pytest.raises(SandboxCreateError, match="scope changed within the same run"):
        registry.ensure(second_ctx)


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
            "AUTH_SECRET": "must-not-appear",
            "ONTO_X": "must-not-appear",
        },
        ontology_dir=ontology_dir,
        runtime_python_path="/app/sandbox-runtime/python",
    )

    assert env["PYTHONPATH"] == "/app/sandbox-runtime/python:{}".format(ontology_dir)
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert SCRIPT_BUSINESS_DATA_CONNECTION_ENV in env
    assert "AUTH_SECRET" not in env
    assert "ONTO_X" not in env
    assert not env["PYTHONPATH"].startswith("/backend:")
    assert "/backend/.python-deps" not in env["PYTHONPATH"]


def test_script_config_env_allows_valid_domain_table_prefix_only():
    assert collect_script_config_env({}) == {}
    assert collect_script_config_env({
        "DOMAIN_TABLE_PREFIX": "custom.example_db",
        "SECRET_ENCRYPTION_KEY": "must-not-appear",
    }) == {"DOMAIN_TABLE_PREFIX": "custom.example_db"}
    assert collect_script_config_env({"DOMAIN_TABLE_PREFIX": "custom.example_db;DROP"}) == {}

    env = build_script_env(
        env_overrides={
            "DOMAIN_TABLE_PREFIX": "custom.example_db.",
            "SECRET_ENCRYPTION_KEY": "must-not-appear",
        },
        ontology_dir=None,
    )

    assert env["DOMAIN_TABLE_PREFIX"] == "custom.example_db."
    assert "SECRET_ENCRYPTION_KEY" not in env


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
        data_proxy_url="http://jiuwenbox:8322/onto-data/run/conn",
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
    )
    policy = OntoSandboxPolicyBuilder.build(ctx)

    bind_mounts = policy["filesystem_policy"]["bind_mounts"]
    bind_by_sandbox_path = {item["sandbox_path"]: item for item in bind_mounts}
    workspace_path = str(workspace_root.resolve()).replace("\\", "/")

    assert bind_by_sandbox_path[workspace_path]["host_path"] == workspace_path
    assert bind_by_sandbox_path["/app/sandbox-runtime/python"]["host_path"] == "/app/sandbox-runtime/python"
    assert all(item["sandbox_path"] not in {"/backend", "/workspace", "/ontology"} for item in bind_mounts)
    assert policy["network"]["egress"]["default"] == "deny"
    assert policy["network"]["egress"]["allowed_domains"] == ["jiuwenbox"]
    assert policy["network"]["egress"]["allowed_ips"] == []
    assert policy["network"]["egress"]["allowed_ports"] == [8322]
    assert policy["network"]["egress"]["blocked_ips"] == ["169.254.169.254/32"]
    assert policy["network"]["egress"]["blocked_ports"] == [53]


def test_executor_mounts_workspace_and_passes_domain_table_prefix(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    workspace_root = runtime_root / "workspaces" / "ws"
    ontology_dir = workspace_root / "ontologies" / "demo"
    script = ontology_dir / "scripts" / "check.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('ok')", encoding="utf-8")
    monkeypatch.setenv("DOMAIN_TABLE_PREFIX", "custom.example_db")

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
        lambda: SimpleNamespace(
            prepare=lambda **kwargs: SimpleNamespace(env={}, data_proxy_url=None)
        ),
    )

    captured = {}

    class RecordingRegistry:
        def ensure(self, ctx):
            captured["ctx"] = ctx
            return "onto-test"

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
    )

    assert result.returncode == 0
    assert captured["ctx"].workspace_root == workspace_root.resolve()
    assert captured["env"]["DOMAIN_TABLE_PREFIX"] == "custom.example_db"
    assert captured["workdir"] == str(workspace_root.resolve()).replace("\\", "/")
    assert captured["command"][2] == str(script.resolve()).replace("\\", "/")
    policy = OntoSandboxPolicyBuilder.build(captured["ctx"])
    mounted_paths = {
        item["sandbox_path"] for item in policy["filesystem_policy"]["bind_mounts"]
    }
    assert str(workspace_root.resolve()).replace("\\", "/") in mounted_paths
    assert str(runtime_root.resolve()).replace("\\", "/") not in mounted_paths


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
        data_proxy_url="http://127.20.0.8:8322/onto-data/run/conn",
        memory_max="256M",
        cpu_max=0.5,
        pids_max=32,
    )
    policy = OntoSandboxPolicyBuilder.build(ctx)

    assert policy["network"]["egress"]["allowed_domains"] == []
    assert policy["network"]["egress"]["allowed_ips"] == ["127.20.0.8/32"]
    assert policy["network"]["egress"]["allowed_ports"] == [8322]


def test_extract_port_from_url_defaults_by_scheme():
    assert extract_port_from_url("https://proxy.example.com") == 443
    assert extract_port_from_url("http://proxy.example.com") == 80
    assert extract_port_from_url("http://proxy.example.com:8322") == 8322
    assert extract_port_from_url("proxy.example.com:8322") == 8322


def test_build_egress_allowed_ports_denies_when_no_proxy():
    assert build_egress_allowed_ports(None) == []


def test_data_proxy_manager_creates_safe_env_without_raw_secret(monkeypatch):
    connection = _resolved_source(
        id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": "real-token",
            "scene": "demo",
            "timeout_seconds": 20,
            "max_retries": 1,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
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
    assert "real-token" not in prepared.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "https://nl2sql.example.com" not in prepared.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert fake_client.created[0]["api_key"] == "real-token"
    assert fake_client.started


@pytest.mark.parametrize("provider", ["dataagent", "nl2sql_template"])
def test_data_proxy_manager_rejects_script_disallowed_providers(monkeypatch, provider):
    connection = _resolved_source(
        id="disallowed-ontology",
        name="Disallowed ontology",
        provider=provider,
        config={
            "url": "https://dataagent.example.com",
            "token": "real-token",
            "timeout_seconds": 20,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(
        SandboxUnavailableError,
        match="unsupported sandbox business data provider '{}'".format(provider),
    ):
        manager.prepare(run_id="run-disallowed", workspace_id="ws", ontology_id="demo")

    assert fake_client.created == []
    assert fake_client.started == []


def test_data_proxy_manager_fails_closed_when_route_create_returns_400(monkeypatch):
    connection = _resolved_source(
        id="conn-conflict",
        name="Conflicting route",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": "real-token",
        },
    )

    class ConflictClient(_FakeJiuwenboxClient):
        def create_proxy(self, **payload):
            self.created.append(payload)
            raise SandboxUnavailableError(
                "create proxy failed (400): route already exists"
            )

    fake_client = ConflictClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match=r"failed \(400\)"):
        manager.prepare(run_id="run-conflict", workspace_id="ws", ontology_id="demo")

    assert len(fake_client.created) == 1
    assert fake_client.started == []
    assert manager._routes_by_run.get("run-conflict") in (None, {})


def test_data_proxy_manager_rolls_back_when_route_start_fails(monkeypatch):
    connection = _resolved_source(
        id="conn-start-fail",
        name="Start failure route",
        provider="nl2sql_http",
        config={
            "url": "https://nl2sql.example.com",
            "token": "real-token",
        },
    )

    class StartFailureClient(_FakeJiuwenboxClient):
        def start_proxy(self, proxy_name):
            self.started.append(proxy_name)
            raise SandboxUnavailableError(
                "start proxy failed (503): jiuwenbox unavailable"
            )

    fake_client = StartFailureClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match=r"start proxy failed \(503\)"):
        manager.prepare(run_id="run-start-fail", workspace_id="ws", ontology_id="demo")

    assert len(fake_client.created) == 1
    assert fake_client.deleted == fake_client.started
    assert manager._routes_by_run.get("run-start-fail") in (None, {})


def test_data_proxy_manager_authenticates_mindsdb_once_per_run(monkeypatch):
    connection = _resolved_source(
        id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "https://mindsdb.example.com",
            "login": "mindsdb-user",
            "password": "secret-password",
            "requires_auth": True,
            "default_database": "analytics",
            "timeout_seconds": 600,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
    login_calls = []

    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    def fake_post(url, **kwargs):
        login_calls.append((url, kwargs))
        return _FakeResponse({"token": "run-jwt-token"})

    monkeypatch.setattr("integration.sandbox.data_proxy.requests.post", fake_post)

    first = manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")
    second = manager.prepare(run_id="run-auth", workspace_id="ws", ontology_id="demo")
    payload = json.loads(first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV])

    assert first == second
    assert login_calls == [
        (
            "https://mindsdb.example.com/api/login",
            {
                "json": {"username": "mindsdb-user", "password": "secret-password"},
                "timeout": 10,
                "verify": True,
            },
        )
    ]
    assert fake_client.created[0]["api_key"] == "run-jwt-token"
    assert payload["version"] == 1
    assert payload["endpoint"] == first.data_proxy_url
    assert payload["auth"]["token"].startswith("onto-placeholder-")
    assert payload["options"]["default_database"] == "analytics"
    assert "secret-password" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "mindsdb-user" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "run-jwt-token" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]
    assert "https://mindsdb.example.com" not in first.env[SCRIPT_BUSINESS_DATA_CONNECTION_ENV]


def test_data_proxy_manager_rejects_failed_mindsdb_login(monkeypatch):
    connection = _resolved_source(
        id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "https://mindsdb.example.com",
            "login": "mindsdb-user",
            "password": "wrong-password",
            "requires_auth": True,
        },
    )
    fake_client = _FakeJiuwenboxClient()
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = fake_client
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
        id="mindsdb-conn",
        name="Authenticated MindsDB",
        provider="mindsdb",
        config={
            "url": "http://mindsdb.internal:47334",
            "login": "mindsdb-user",
            "password": "secret-password",
            "requires_auth": True,
        },
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=False))
    manager._client = _FakeJiuwenboxClient()
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
        id="conn1",
        name="Conn 1",
        provider="mindsdb",
        config={"url": "http://198.51.100.27:47334", "requires_auth": False},
    )
    manager = DataConnectionSecretManager(_sandbox_config())
    manager._client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")
    with pytest.raises(SandboxUnavailableError):
        manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="other")


def test_data_proxy_manager_blocks_http_secret_when_disabled_globally(monkeypatch):
    connection = _resolved_source(
        id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={"url": "http://10.0.0.8:8080", "token": "real-token"},
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=False))
    manager._client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    with pytest.raises(SandboxUnavailableError, match="requires TLS"):
        manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")


def test_data_proxy_manager_allows_http_secret_when_enabled_globally(monkeypatch):
    connection = _resolved_source(
        id="conn1",
        name="Conn 1",
        provider="nl2sql_http",
        config={"url": "http://10.0.0.8:8080", "token": "real-token"},
    )
    manager = DataConnectionSecretManager(_sandbox_config(secret_allow_plain_http=True))
    manager._client = _FakeJiuwenboxClient()
    monkeypatch.setattr(
        "integration.sandbox.data_proxy.resolve_business_data_connection",
        lambda **_: connection,
    )

    prepared = manager.prepare(run_id="run-1", workspace_id="ws", ontology_id="demo")

    assert prepared.data_proxy_url.startswith("http://")
    assert ":8322/onto-" in prepared.data_proxy_url
    assert manager._client.created[0]["api_key"] == "real-token"


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
        data_proxy_url="http://127.20.0.8:8322/onto-route",
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
        "data_proxy_base_url": "http://jiuwenbox:8322",
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
