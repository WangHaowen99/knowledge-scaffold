import json
from pathlib import Path

import pytest

from config import system_config as system_config_module
from utils.security.secrets import encrypt_secret
from workspace.workspaces import create_workspace


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _setup_runtime(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    config_path = runtime_root / "config" / "system_config.json"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    create_workspace("Beta", workspace_id="beta")
    return runtime_root, config_path


def _write_global_config(config_path, observability):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "llm": {"model": "keep-me"},
                "observability": observability,
                "web_search": {"enabled": False},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_migrates_global_local_observability_to_each_workspace(tmp_path, monkeypatch):
    _runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(
        config_path,
        {
            "enabled": True,
            "mode": "local",
            "local": {"retention_days": 9},
            "langfuse": {},
        },
    )

    from integration.observability.migration import migrate_global_observability_to_workspaces
    from integration.observability.workspace_config import get_workspace_observability_config

    result = migrate_global_observability_to_workspaces()

    assert result["migrated"] is True
    assert result["workspace_ids"] == ["alpha", "beta"]
    assert get_workspace_observability_config("alpha")["local"]["retention_days"] == 9
    assert get_workspace_observability_config("beta")["local"]["retention_days"] == 9
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert raw["llm"] == {"model": "keep-me"}
    assert "observability" not in raw


def test_migrates_langfuse_secret_from_global_to_workspace_context(tmp_path, monkeypatch):
    _runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    encrypted_secret = encrypt_secret(
        "global-secret",
        context="observability.langfuse.secret_key",
    )
    _write_global_config(
        config_path,
        {
            "enabled": True,
            "mode": "langfuse",
            "local": {"retention_days": 30},
            "langfuse": {
                "public_key": "pk-global",
                "secret_key": encrypted_secret,
                "base_url": "https://langfuse.example",
                "debug": True,
            },
        },
    )

    from integration.observability.migration import migrate_global_observability_to_workspaces
    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        workspace_langfuse_secret_context,
    )
    from utils.security.secrets import decrypt_secret

    migrate_global_observability_to_workspaces()

    alpha_config = get_workspace_observability_config("alpha")
    assert alpha_config["langfuse"]["secret_key"] == "global-secret"

    raw_alpha_path = (
        tmp_path
        / ".onto-runtime"
        / "workspaces"
        / "alpha"
        / "observability_config.json"
    )
    raw_alpha = json.loads(raw_alpha_path.read_text(encoding="utf-8"))
    raw_secret = raw_alpha["langfuse"]["secret_key"]
    assert raw_secret.startswith("enc:v1:")
    assert decrypt_secret(
        raw_secret,
        context=workspace_langfuse_secret_context("alpha"),
    ) == "global-secret"
    with pytest.raises(Exception, match="secret 解密失败"):
        decrypt_secret(raw_secret, context="observability.langfuse.secret_key")


def test_migration_does_not_overwrite_existing_workspace_config(tmp_path, monkeypatch):
    _runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(
        config_path,
        {
            "enabled": True,
            "mode": "local",
            "local": {"retention_days": 21},
            "langfuse": {"secret_key": "global-secret"},
        },
    )

    from integration.observability.migration import migrate_global_observability_to_workspaces
    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        write_workspace_observability_config,
    )

    write_workspace_observability_config(
        "alpha",
        {
            "enabled": False,
            "mode": "local",
            "local": {"retention_days": 3},
            "langfuse": {"secret_key": "alpha-secret"},
        },
    )

    migrate_global_observability_to_workspaces()

    alpha_config = get_workspace_observability_config("alpha")
    beta_config = get_workspace_observability_config("beta")
    assert alpha_config["local"]["retention_days"] == 3
    assert alpha_config["langfuse"]["secret_key"] == "alpha-secret"
    assert beta_config["local"]["retention_days"] == 21
    assert beta_config["langfuse"]["secret_key"] == "global-secret"


def test_migration_deletes_only_old_agent_perf_logs_after_success(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(
        config_path,
        {
            "enabled": False,
            "mode": "local",
            "local": {"retention_days": 30},
        },
    )
    log_dir = runtime_root / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "agent_perf-2026-01-01.log").write_text("old\n", encoding="utf-8")
    (log_dir / "agent_perf-2026-01-01.log.1").write_text("old\n", encoding="utf-8")
    (log_dir / "agent_perf.log").write_text("base\n", encoding="utf-8")
    (log_dir / "agent-2026-01-01.log").write_text("keep\n", encoding="utf-8")
    (log_dir / "other.log").write_text("keep\n", encoding="utf-8")

    from integration.observability.migration import migrate_global_observability_to_workspaces

    result = migrate_global_observability_to_workspaces(log_dir=log_dir)

    assert result["deleted_perf_logs"] == 2
    assert not (log_dir / "agent_perf-2026-01-01.log").exists()
    assert not (log_dir / "agent_perf-2026-01-01.log.1").exists()
    assert (log_dir / "agent_perf.log").exists()
    assert (log_dir / "agent-2026-01-01.log").exists()
    assert (log_dir / "other.log").exists()


def test_migration_keeps_global_config_when_workspace_write_fails(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(
        config_path,
        {
            "enabled": True,
            "mode": "local",
            "local": {"retention_days": 12},
        },
    )
    log_dir = runtime_root / "logs"
    log_dir.mkdir(parents=True)
    old_perf = log_dir / "agent_perf-2026-01-01.log"
    old_perf.write_text("old\n", encoding="utf-8")

    from integration.observability import migration

    def fail_for_beta(workspace_id, config):
        if workspace_id == "beta":
            raise OSError("cannot write beta")
        return config

    monkeypatch.setattr(migration, "write_workspace_observability_config", fail_for_beta)

    result = migration.migrate_global_observability_to_workspaces(log_dir=log_dir)

    assert result["migrated"] is False
    assert result["workspace_ids"] == ["alpha"]
    assert result["deleted_perf_logs"] == 1
    assert any("workspace_id=beta" in error for error in result["errors"])
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert "observability" in raw
    assert not old_perf.exists()


def test_migration_does_not_raise_when_old_perf_log_delete_fails(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(
        config_path,
        {
            "enabled": False,
            "mode": "local",
            "local": {"retention_days": 30},
        },
    )
    log_dir = runtime_root / "logs"
    log_dir.mkdir(parents=True)
    old_perf = log_dir / "agent_perf-2026-01-01.log"
    old_perf.write_text("old\n", encoding="utf-8")

    original_unlink = Path.unlink

    def fail_for_old_perf(path, *args, **kwargs):
        if path.name == old_perf.name:
            raise OSError("locked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_for_old_perf)

    from integration.observability import migration

    result = migration.migrate_global_observability_to_workspaces(log_dir=log_dir)

    assert result["migrated"] is True
    assert result["deleted_perf_logs"] == 0
    assert any(str(old_perf) in error for error in result["errors"])
    assert old_perf.exists()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert "observability" not in raw


def test_create_app_runs_observability_migration_before_runtime_services(monkeypatch):
    import app as app_module

    order = []

    def fake_migrate():
        order.append("migration")

    def fake_build_runtime_services(*args, **kwargs):
        del args, kwargs
        order.append("build")
        return {
            "config": object(),
            "agent_config": object(),
            "llm_router": object(),
            "auth_manager": object(),
            "user_store_manager": object(),
        }

    monkeypatch.setattr(
        app_module,
        "migrate_global_observability_to_workspaces",
        fake_migrate,
        raising=False,
    )
    monkeypatch.setattr(app_module, "build_runtime_services", fake_build_runtime_services)
    monkeypatch.setattr(app_module, "register_routes", lambda *args, **kwargs: order.append("routes"))
    monkeypatch.setattr(app_module, "start_platform_observer_if_enabled", lambda: order.append("platform"))

    app_module.create_app()

    assert order[:2] == ["migration", "build"]


def test_create_app_continues_when_observability_migration_fails(monkeypatch):
    import app as app_module

    order = []

    def fake_migrate():
        order.append("migration")
        raise RuntimeError("migration failed")

    def fake_build_runtime_services(*args, **kwargs):
        del args, kwargs
        order.append("build")
        return {
            "config": object(),
            "agent_config": object(),
            "llm_router": object(),
            "auth_manager": object(),
            "user_store_manager": object(),
        }

    monkeypatch.setattr(
        app_module,
        "migrate_global_observability_to_workspaces",
        fake_migrate,
        raising=False,
    )
    monkeypatch.setattr(app_module, "build_runtime_services", fake_build_runtime_services)
    monkeypatch.setattr(app_module, "register_routes", lambda *args, **kwargs: order.append("routes"))
    monkeypatch.setattr(app_module, "start_platform_observer_if_enabled", lambda: order.append("platform"))

    app_module.create_app()

    assert order == ["migration", "build", "routes", "platform"]
