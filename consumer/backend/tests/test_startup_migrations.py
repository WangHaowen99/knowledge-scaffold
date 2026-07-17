"""Tests for the central phase- and failure-policy-aware migration runner."""

from __future__ import annotations

import json
import os
from concurrent.futures import ProcessPoolExecutor
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


def _run_startup_in_process(runtime_root, secret_key):
    os.environ["PROJECT_ROOT"] = str(runtime_root)
    os.environ["SECRET_ENCRYPTION_KEY"] = secret_key
    os.environ["LOG_DIR"] = str(runtime_root / "legacy-logs")
    from migrations.runner import run_startup_migrations

    return run_startup_migrations(runtime_root)


def test_shared_runner_registers_explicit_phase_and_failure_policy():
    from migrations.observability_workspace_config_v1 import MIGRATION_ID as OBSERVABILITY_ID
    from migrations.ontology_data_sources_direct_v2 import MIGRATION_ID as DATA_SOURCE_ID
    from migrations.system_config_model_profiles_v1 import MIGRATION_ID as MODEL_CONFIG_ID
    from migrations.system_config_secret_encryption_v1 import MIGRATION_ID as SECRET_CONFIG_ID
    from migrations.runner import (
        REGISTERED_MIGRATIONS,
        MigrationFailurePolicy,
        MigrationPhase,
    )

    assert [migration.migration_id for migration in REGISTERED_MIGRATIONS] == [
        DATA_SOURCE_ID,
        OBSERVABILITY_ID,
        MODEL_CONFIG_ID,
        SECRET_CONFIG_ID,
    ]
    assert [migration.phase for migration in REGISTERED_MIGRATIONS] == [
        MigrationPhase.STARTUP,
        MigrationPhase.STARTUP,
        MigrationPhase.STARTUP,
        MigrationPhase.STARTUP,
    ]
    assert [migration.failure_policy for migration in REGISTERED_MIGRATIONS] == [
        MigrationFailurePolicy.BLOCKING,
        MigrationFailurePolicy.NON_BLOCKING,
        MigrationFailurePolicy.BLOCKING,
        MigrationFailurePolicy.BLOCKING,
    ]


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

    with pytest.raises(runner_module.StartupMigrationError, match="OSError"):
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

    with pytest.raises(StartupMigrationError, match="ontology-data-sources-direct-v2"):
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
    monkeypatch.setattr(app_module, "build_runtime_services", fake_runtime)
    monkeypatch.setattr(app_module, "register_routes", lambda *args, **kwargs: order.append("routes"))
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **kwargs: order.append("warmup"))
    monkeypatch.setattr(
        app_module,
        "start_platform_observer_if_enabled",
        lambda: order.append("platform"),
    )

    app_module.create_app()

    assert order == ["startup", "build", "routes", "warmup", "platform"]


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


def test_non_blocking_failure_is_reported_and_later_blocking_migrations_continue(
    tmp_path,
    monkeypatch,
    caplog,
):
    import migrations.runner as runner_module

    non_blocking = runner_module.RegisteredMigration(
        migration_id="optional",
        module="tests.optional",
        phase=runner_module.MigrationPhase.STARTUP,
        failure_policy=runner_module.MigrationFailurePolicy.NON_BLOCKING,
    )
    blocking = runner_module.RegisteredMigration(
        migration_id="required",
        module="tests.required",
        phase=runner_module.MigrationPhase.STARTUP,
        failure_policy=runner_module.MigrationFailurePolicy.BLOCKING,
    )
    calls = []

    def load_executor(migration):
        if migration.migration_id == "optional":
            return lambda *_args, **_kwargs: (
                2,
                {
                    "migration_id": "optional",
                    "ok": False,
                    "errors": ["sanitized failure"],
                    "error_codes": ["OPTIONAL_PENDING"],
                },
            )

        def execute_required(*_args, **_kwargs):
            calls.append("required")
            return 0, {"migration_id": "required", "ok": True, "errors": []}

        return execute_required

    monkeypatch.setattr(runner_module, "REGISTERED_MIGRATIONS", (non_blocking, blocking))
    monkeypatch.setattr(runner_module, "_load_executor", load_executor)

    with caplog.at_level("WARNING"):
        report = runner_module.run_startup_migrations(tmp_path / ".onto-runtime")

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["warnings"] == ["optional：OPTIONAL_PENDING"]
    assert calls == ["required"]
    assert "optional：OPTIONAL_PENDING" in caplog.text


def test_run_context_excludes_deployment_and_external_admin_from_app_startup(
    tmp_path,
    monkeypatch,
):
    import migrations.runner as runner_module

    registrations = tuple(
        runner_module.RegisteredMigration(
            migration_id=phase.value,
            module=f"tests.{phase.value}",
            phase=phase,
            failure_policy=runner_module.MigrationFailurePolicy.BLOCKING,
        )
        for phase in runner_module.MigrationPhase
    )

    def load_executor(migration):
        return lambda *_args, **_kwargs: (
            0,
            {"migration_id": migration.migration_id, "ok": True, "errors": []},
        )

    monkeypatch.setattr(runner_module, "REGISTERED_MIGRATIONS", registrations)
    monkeypatch.setattr(runner_module, "_load_executor", load_executor)

    startup = runner_module.run_startup_migrations(tmp_path / ".onto-runtime")
    deployment_code, deployment = runner_module.execute_registered_migrations(
        tmp_path / ".onto-runtime",
        dry_run=True,
        writes_stopped=False,
    )
    external_code, external = runner_module.execute_registered_migrations(
        tmp_path / ".onto-runtime",
        dry_run=True,
        writes_stopped=False,
        run_context=runner_module.MigrationRunContext.EXTERNAL_ADMIN,
    )

    assert [item["migration_id"] for item in startup["migrations"]] == ["startup"]
    assert deployment_code == 0
    assert [item["migration_id"] for item in deployment["migrations"]] == [
        "startup",
        "deployment",
    ]
    assert external_code == 0
    assert [item["migration_id"] for item in external["migrations"]] == [
        "external-admin"
    ]


def test_concurrent_process_startup_is_serialized_and_idempotent(tmp_path):
    from migrations.observability_workspace_config_v1 import MARKER_RELATIVE_PATH

    runtime_root = tmp_path / ".onto-runtime"
    for workspace_id in ("alpha", "beta"):
        workspace_dir = _workspace_dir(runtime_root, workspace_id)
        (workspace_dir / "workspace.json").write_text(
            json.dumps({"id": workspace_id, "name": workspace_id.title()}),
            encoding="utf-8",
        )
    config_path = runtime_root / "config" / "system_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "observability": {
                    "enabled": True,
                    "mode": "local",
                    "local": {"retention_days": 8},
                }
            }
        ),
        encoding="utf-8",
    )
    secret_key = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="

    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_run_startup_in_process, runtime_root, secret_key)
            for _ in range(2)
        ]
        reports = [future.result(timeout=20) for future in futures]

    assert all(report["ok"] for report in reports)
    assert "observability" not in json.loads(config_path.read_text(encoding="utf-8"))
    marker = json.loads((runtime_root / MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["status"] == "completed"
    for workspace_id in ("alpha", "beta"):
        target = runtime_root / "workspaces" / workspace_id / "observability_config.json"
        assert json.loads(target.read_text(encoding="utf-8"))["local"]["retention_days"] == 8


def test_registered_issue_1_13_and_14_migrations_preserve_each_others_data(
    tmp_path,
    monkeypatch,
):
    from migrations.runner import run_startup_migrations

    runtime_root = tmp_path / ".onto-runtime"
    workspace_dir = _workspace_dir(runtime_root)
    (workspace_dir / "workspace.json").write_text(
        json.dumps({"id": "demo", "name": "Demo"}),
        encoding="utf-8",
    )
    sources_path = workspace_dir / "ontology-data-sources.json"
    sources_bytes = json.dumps(
        {"version": 2, "sources": {}},
        ensure_ascii=False,
    ).encode()
    sources_path.write_bytes(sources_bytes)
    config_path = runtime_root / "config" / "system_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "observability": {
                    "enabled": True,
                    "mode": "local",
                    "local": {"retention_days": 8},
                },
                "task_llm": {
                    "base_url": "https://task.example/v1",
                    "model": "task-model",
                    "api_key": "task-secret",
                },
                "future_section": {"preserved": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LOG_DIR", str(runtime_root / "legacy-logs"))

    report = run_startup_migrations(runtime_root)

    assert [item["migration_id"] for item in report["migrations"]] == [
        "ontology-data-sources-direct-v2",
        "observability-workspace-config-v1",
        "system-config-model-profiles-v1",
        "system-config-secret-encryption-v1",
    ]
    assert sources_path.read_bytes() == sources_bytes
    migrated = json.loads(config_path.read_text(encoding="utf-8"))
    assert migrated["future_section"] == {"preserved": True}
    assert "observability" not in migrated
    assert "task_llm" not in migrated
    assert migrated["llm_profiles"]["profiles"][0]["api_key"].startswith("enc:v1:")
    workspace_observability = json.loads(
        (workspace_dir / "observability_config.json").read_text(encoding="utf-8")
    )
    assert workspace_observability["local"]["retention_days"] == 8
