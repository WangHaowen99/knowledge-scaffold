"""Tests for the central, fail-closed startup migration runner."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest


def _workspace_dir(runtime_root, workspace_id="demo"):
    workspace_dir = runtime_root / "workspaces" / workspace_id
    workspace_dir.mkdir(parents=True)
    return workspace_dir


def _write_legacy_binding(workspace_dir, *, connection_id="shared-dataagent"):
    ontology_dir = workspace_dir / "ontologies" / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology-package\nid: demo-ontology\nname: Demo Ontology\n",
        encoding="utf-8",
    )
    (workspace_dir / "ontology-bindings.json").write_text(
        json.dumps(
            {"bindings": {"demo-ontology": {"connection_id": connection_id}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _write_legacy_connection(workspace_dir, *, connection_id="shared-dataagent"):
    (workspace_dir / "data-connections.json").write_text(
        json.dumps(
            {
                "default_connection_id": connection_id,
                "connections": [
                    {
                        "id": connection_id,
                        "name": "Shared DataAgent",
                        "provider": "dataagent",
                        "config": {
                            "url": "https://dataagent.example.com/",
                            "token": "legacy-secret",
                            "timeout_seconds": 60,
                        },
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-02T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_shared_runner_only_registers_data_source_migration():
    from migrations.ontology_data_sources_direct_v2 import MIGRATION_ID
    from migrations.runner import REGISTERED_MIGRATIONS

    assert [migration.migration_id for migration in REGISTERED_MIGRATIONS] == [MIGRATION_ID]


def test_shared_runner_is_noop_when_registry_is_empty(tmp_path, monkeypatch):
    import migrations.runner as runner_module

    monkeypatch.setattr(runner_module, "REGISTERED_MIGRATIONS", ())

    code, report = runner_module.execute_registered_migrations(
        tmp_path / ".onto-runtime",
        dry_run=True,
        writes_stopped=False,
    )

    assert code == 0
    assert report["ok"] is True
    assert report["migrations"] == []

    startup_report = runner_module.run_startup_migrations(tmp_path / ".onto-runtime")
    assert startup_report["ok"] is True
    assert startup_report["migrations"] == []


def test_startup_runner_wraps_lock_failure(tmp_path, monkeypatch):
    import migrations.runner as runner_module

    @contextmanager
    def broken_lock(_runtime_root):
        raise OSError("lock denied")
        yield  # pragma: no cover

    monkeypatch.setattr(runner_module, "_migration_lock", broken_lock)

    with pytest.raises(runner_module.StartupMigrationError, match="lock denied"):
        runner_module.run_startup_migrations(tmp_path / ".onto-runtime")


def test_startup_runner_skips_fresh_workspace_without_marker(tmp_path):
    from migrations.ontology_data_sources_direct_v2 import MARKER_FILE
    from migrations.runner import run_startup_migrations

    runtime_root = tmp_path / ".onto-runtime"
    workspace_dir = _workspace_dir(runtime_root)

    report = run_startup_migrations(runtime_root)

    workspace_report = report["migrations"][0]["workspaces"]["demo"]
    assert report["ok"] is True
    assert workspace_report["skipped"] is True
    assert not (workspace_dir / MARKER_FILE).exists()


def test_startup_runner_migrates_legacy_connection_and_binding(tmp_path):
    from migrations.ontology_data_sources_direct_v2 import MARKER_FILE
    from migrations.runner import run_startup_migrations

    runtime_root = tmp_path / ".onto-runtime"
    workspace_dir = _workspace_dir(runtime_root)
    _write_legacy_binding(workspace_dir)
    _write_legacy_connection(workspace_dir)
    connection_path = workspace_dir / "data-connections.json"
    bindings_path = workspace_dir / "ontology-bindings.json"
    connection_bytes = connection_path.read_bytes()
    bindings_bytes = bindings_path.read_bytes()

    report = run_startup_migrations(runtime_root)

    source_text = (workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    source = json.loads(source_text)["sources"]["demo-ontology"]
    workspace_report = report["migrations"][0]["workspaces"]["demo"]
    connection_backup = workspace_dir / "data-connections.json.pre-direct-v2.bak"
    bindings_backup = workspace_dir / "ontology-bindings.json.pre-direct-v2.bak"
    assert report["ok"] is True
    assert source["mode"] == "direct_provider"
    assert source["provider"] == "dataagent"
    assert "connection_id" not in source
    assert "legacy-secret" not in source_text
    assert source["config"]["token"].startswith("enc:v1:")
    assert workspace_report["removed_legacy_files"] == 2
    assert workspace_report["backup_paths"] == [str(connection_backup), str(bindings_backup)]
    assert connection_backup.read_bytes() == connection_bytes
    assert bindings_backup.read_bytes() == bindings_bytes
    assert not connection_path.exists()
    assert not bindings_path.exists()
    assert (workspace_dir / MARKER_FILE).is_file()


def test_startup_runner_blocks_startup_when_legacy_reference_is_invalid(tmp_path):
    from migrations.runner import StartupMigrationError, run_startup_migrations

    runtime_root = tmp_path / ".onto-runtime"
    workspace_dir = _workspace_dir(runtime_root)
    _write_legacy_binding(workspace_dir, connection_id="missing")

    with pytest.raises(StartupMigrationError, match="业务数据连接不存在"):
        run_startup_migrations(runtime_root)

    assert not (workspace_dir / "ontology-data-sources.json").exists()
    assert (workspace_dir / "ontology-bindings.json").is_file()


def test_create_app_runs_critical_startup_migrations_before_other_startup_work(monkeypatch):
    import app as app_module

    order = []

    def fake_runtime(*args, **kwargs):
        del args, kwargs
        order.append("build")
        return {
            "config": object(),
            "agent_config": object(),
            "llm_router": object(),
            "auth_manager": object(),
            "user_store_manager": object(),
        }

    monkeypatch.setattr(app_module, "run_startup_migrations", lambda: order.append("startup"))
    monkeypatch.setattr(
        app_module,
        "_run_startup_observability_migration",
        lambda: order.append("observability"),
    )
    monkeypatch.setattr(app_module, "build_runtime_services", fake_runtime)
    monkeypatch.setattr(app_module, "register_routes", lambda *args, **kwargs: order.append("routes"))
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **kwargs: order.append("warmup"))
    monkeypatch.setattr(
        app_module,
        "start_platform_observer_if_enabled",
        lambda: order.append("platform"),
    )

    app_module.create_app()

    assert order == ["startup", "observability", "build", "routes", "warmup", "platform"]


def test_create_app_does_not_build_runtime_when_critical_migration_fails(monkeypatch):
    import app as app_module
    from migrations.runner import StartupMigrationError

    def fail_startup():
        raise StartupMigrationError("data source migration failed")

    monkeypatch.setattr(app_module, "run_startup_migrations", fail_startup)
    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda *args, **kwargs: pytest.fail("runtime services must not be built"),
    )

    with pytest.raises(StartupMigrationError, match="data source migration failed"):
        app_module.create_app()
