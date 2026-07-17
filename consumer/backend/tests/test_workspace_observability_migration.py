from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from utils.security.secrets import encrypt_secret
from workspace.workspaces import create_workspace


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _setup_runtime(tmp_path, monkeypatch, *, with_key=True, workspace_ids=("alpha", "beta")):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    if with_key:
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    else:
        monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    for index, workspace_id in enumerate(workspace_ids):
        create_workspace(
            workspace_id.title(),
            workspace_id=workspace_id,
            set_as_default=index == 0,
        )
    return runtime_root, runtime_root / "config" / "system_config.json"


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


def _local_observability(retention_days=9):
    return {
        "enabled": True,
        "mode": "local",
        "local": {"retention_days": retention_days},
        "langfuse": {},
    }


def _tree_snapshot(*roots):
    result = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative = f"{root.name}/{path.relative_to(root)}"
            if path.is_symlink():
                result[relative] = ("symlink", path.readlink().as_posix())
            elif path.is_file():
                result[relative] = (
                    stat.S_IMODE(path.stat().st_mode),
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif path.is_dir():
                result[relative] = ("dir", stat.S_IMODE(path.stat().st_mode))
    return result


def _execute(runtime_root, *, dry_run, log_dir, writes_stopped=True):
    from migrations.observability_workspace_config_v1 import execute_migration

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=writes_stopped,
        from_version="v1",
        to_version="v2",
        log_dir=log_dir,
    )


def test_dry_run_is_completely_read_only(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old_log = log_dir / "agent_perf-2026-01-01.log"
    old_log.write_text("old\n", encoding="utf-8")
    before = _tree_snapshot(runtime_root, log_dir)

    code, report = _execute(runtime_root, dry_run=True, log_dir=log_dir)

    assert code == 0
    assert report["ok"] is True
    assert report["phase"] == "dry-run"
    assert report["config_migration"]["status"] == "dry-run-ready"
    assert report["legacy_log_cleanup"]["counts"]["legacy_logs_found"] == 1
    assert report["backup_paths"] == []
    assert report["planned_backup_paths"]
    assert _tree_snapshot(runtime_root, log_dir) == before
    assert not (runtime_root / "config" / "secret.key").exists()


def test_apply_migrates_config_backs_up_sources_and_reports_steps_separately(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
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
    original_config = config_path.read_bytes()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old_log = log_dir / "agent_perf-2026-01-01.log"
    old_log.write_text("old-performance-data\n", encoding="utf-8")

    code, report = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert code == 0
    assert report["ok"] is True
    assert report["config_migration"]["status"] == "applied"
    assert report["legacy_log_cleanup"]["status"] == "applied"
    assert report["counts"] == {
        "workspaces_scanned": 2,
        "workspace_configs_written": 2,
        "workspace_configs_preserved": 0,
        "legacy_logs_found": 1,
        "legacy_logs_deleted": 1,
    }
    persisted_system_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted_system_config["llm"] == {"model": "keep-me"}
    assert "observability" not in persisted_system_config

    from integration.observability.workspace_config import get_workspace_observability_config
    from migrations.observability_workspace_config_v1 import MARKER_RELATIVE_PATH

    for workspace_id in ("alpha", "beta"):
        workspace_config = get_workspace_observability_config(workspace_id)
        assert workspace_config["langfuse"]["secret_key"] == "global-secret"
        target_text = (
            runtime_root / "workspaces" / workspace_id / "observability_config.json"
        ).read_text(encoding="utf-8")
        assert "global-secret" not in target_text

    marker = json.loads((runtime_root / MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["status"] == "completed"
    assert marker["workspace_ids"] == ["alpha", "beta"]

    system_backup = next(
        path for path in map(Path, report["backup_paths"]) if "system_config.json" in path.name
    )
    log_backup = next(
        path for path in map(Path, report["backup_paths"]) if "agent_perf-" in path.name
    )
    assert system_backup.read_bytes() == original_config
    assert log_backup.read_text(encoding="utf-8") == "old-performance-data\n"
    assert stat.S_IMODE(system_backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_backup.stat().st_mode) == 0o600
    assert not old_log.exists()

    rendered_report = json.dumps(report, ensure_ascii=False)
    assert "global-secret" not in rendered_report
    assert encrypted_secret not in rendered_report
    assert "token" not in rendered_report.lower()
    assert "hash" not in rendered_report.lower()


def test_apply_does_not_invoke_unrelated_system_config_read_migrations(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "llm": {
                    "model": "legacy-model",
                    "api_key": "unrelated-plaintext-secret",
                },
                "observability": _local_observability(),
            }
        ),
        encoding="utf-8",
    )

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 0
    assert report["config_migration"]["status"] == "applied"
    persisted = json.loads(config_path.read_text(encoding="utf-8"))
    assert "observability" not in persisted
    assert persisted["llm"]["api_key"] == "unrelated-plaintext-secret"
    assert "unrelated-plaintext-secret" not in json.dumps(report, ensure_ascii=False)


def test_no_legacy_data_is_a_repeatable_noop_without_marker(tmp_path, monkeypatch):
    runtime_root, _config_path = _setup_runtime(tmp_path, monkeypatch)
    log_dir = tmp_path / "logs"
    before = _tree_snapshot(runtime_root)

    first_code, first = _execute(runtime_root, dry_run=False, log_dir=log_dir)
    second_code, second = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert first_code == second_code == 0
    assert first["config_migration"]["status"] == "noop"
    assert second["config_migration"]["status"] == "noop"
    assert _tree_snapshot(runtime_root) == before


def test_completed_migration_repeats_without_changing_files(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    log_dir = tmp_path / "logs"
    first_code, _first = _execute(runtime_root, dry_run=False, log_dir=log_dir)
    assert first_code == 0
    before = _tree_snapshot(runtime_root, log_dir)

    second_code, second = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert second_code == 0
    assert second["config_migration"]["status"] == "complete"
    assert _tree_snapshot(runtime_root, log_dir) == before


def test_completed_marker_does_not_hide_damaged_target(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    log_dir = tmp_path / "logs"
    code, _report = _execute(runtime_root, dry_run=False, log_dir=log_dir)
    assert code == 0
    target = runtime_root / "workspaces" / "alpha" / "observability_config.json"
    target.write_text("{broken", encoding="utf-8")
    before = _tree_snapshot(runtime_root)

    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert retry_code == 2
    assert retry["ok"] is False
    assert "INVALID_WORKSPACE_OBSERVABILITY_TARGET" in retry["error_codes"]
    assert "workspaces/alpha/observability_config.json" in retry["failed_object_ids"]
    assert _tree_snapshot(runtime_root) == before


def test_invalid_marker_is_reported_without_touching_runtime_data(tmp_path, monkeypatch):
    runtime_root, _config_path = _setup_runtime(tmp_path, monkeypatch)
    from migrations.observability_workspace_config_v1 import MARKER_RELATIVE_PATH

    marker_path = runtime_root / MARKER_RELATIVE_PATH
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text('{"migration_id":"wrong"}', encoding="utf-8")
    before = _tree_snapshot(runtime_root)

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert "INVALID_MIGRATION_MARKER" in report["error_codes"]
    assert _tree_snapshot(runtime_root) == before


def test_invalid_system_config_blocks_config_writes_without_hiding_damage(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{invalid", encoding="utf-8")
    log_dir = tmp_path / "logs"
    before = _tree_snapshot(runtime_root)

    code, report = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert code == 2
    assert "INVALID_SYSTEM_CONFIG" in report["error_codes"]
    assert _tree_snapshot(runtime_root) == before
    assert not (runtime_root / "workspaces" / "alpha" / "observability_config.json").exists()


def test_invalid_existing_target_is_a_conflict_and_preserves_global_source(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    target = runtime_root / "workspaces" / "alpha" / "observability_config.json"
    target.write_text(json.dumps({"mode": "local"}), encoding="utf-8")
    before = _tree_snapshot(runtime_root)

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert "WORKSPACE_OBSERVABILITY_TARGET_CONFLICT" in report["error_codes"]
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))
    assert _tree_snapshot(runtime_root) == before


def test_existing_canonical_target_is_preserved_while_missing_targets_are_created(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability(retention_days=21))

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
            "langfuse": {
                "public_key": "",
                "secret_key": "alpha-secret",
                "base_url": "",
                "debug": False,
            },
        },
    )
    alpha_before = (
        runtime_root / "workspaces" / "alpha" / "observability_config.json"
    ).read_bytes()

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 0
    assert report["counts"]["workspace_configs_preserved"] == 1
    assert (
        runtime_root / "workspaces" / "alpha" / "observability_config.json"
    ).read_bytes() == alpha_before
    assert get_workspace_observability_config("alpha")["local"]["retention_days"] == 3
    assert get_workspace_observability_config("beta")["local"]["retention_days"] == 21


def test_partial_workspace_failure_keeps_source_and_restart_retries_safely(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability(retention_days=12))
    from migrations import observability_workspace_config_v1 as migration

    original_write = migration._write_workspace_target

    def fail_for_beta(target_root, workspace_id, config):
        if workspace_id == "beta":
            raise OSError("simulated")
        return original_write(target_root, workspace_id, config)

    monkeypatch.setattr(migration, "_write_workspace_target", fail_for_beta)
    first_code, first = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert first_code == 2
    assert first["config_migration"]["source_retained"] is True
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))
    assert (runtime_root / "workspaces" / "alpha" / "observability_config.json").is_file()
    assert not (runtime_root / "workspaces" / "beta" / "observability_config.json").exists()
    assert "workspaces/beta/observability_config.json" in first["failed_object_ids"]

    monkeypatch.setattr(migration, "_write_workspace_target", original_write)
    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert retry_code == 0
    assert retry["counts"]["workspace_configs_preserved"] == 1
    assert retry["counts"]["workspace_configs_written"] == 1
    assert "observability" not in json.loads(config_path.read_text(encoding="utf-8"))


def test_retry_blocks_when_retained_source_diverges_from_owned_target(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability(retention_days=12))
    from migrations import observability_workspace_config_v1 as migration

    original_write = migration._write_workspace_target

    def fail_for_beta(target_root, workspace_id, config):
        if workspace_id == "beta":
            raise OSError("simulated")
        return original_write(target_root, workspace_id, config)

    monkeypatch.setattr(migration, "_write_workspace_target", fail_for_beta)
    first_code, _first = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )
    assert first_code == 2
    alpha_target = (
        runtime_root / "workspaces" / "alpha" / "observability_config.json"
    )
    assert json.loads(alpha_target.read_text(encoding="utf-8"))["local"][
        "retention_days"
    ] == 12
    marker = json.loads(
        (runtime_root / migration.MARKER_RELATIVE_PATH).read_text(encoding="utf-8")
    )
    assert marker["owned_workspace_ids"] == ["alpha", "beta"]

    # A retained source may legitimately be repaired before the next startup.
    # Once a partial target exists, changing the source makes ownership
    # ambiguous. The migration must stop instead of overwriting that target.
    _write_global_config(config_path, _local_observability(retention_days=24))
    monkeypatch.setattr(migration, "_write_workspace_target", original_write)

    retry_code, retry = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )

    assert retry_code == 2
    assert retry["config_migration"]["source_retained"] is True
    assert "WORKSPACE_OBSERVABILITY_TARGET_CONFLICT" in retry["error_codes"]
    assert json.loads(alpha_target.read_text(encoding="utf-8"))["local"][
        "retention_days"
    ] == 12
    assert not (
        runtime_root / "workspaces" / "beta" / "observability_config.json"
    ).exists()
    persisted_source = json.loads(config_path.read_text(encoding="utf-8"))
    assert persisted_source["observability"]["local"]["retention_days"] == 24


def test_retry_never_overwrites_workspace_config_changed_after_partial_failure(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability(retention_days=12))
    from migrations import observability_workspace_config_v1 as migration

    original_write = migration._write_workspace_target

    def fail_for_beta(target_root, workspace_id, config):
        if workspace_id == "beta":
            raise OSError("simulated")
        return original_write(target_root, workspace_id, config)

    monkeypatch.setattr(migration, "_write_workspace_target", fail_for_beta)
    first_code, _first = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )
    assert first_code == 2

    from integration.observability.workspace_config import (
        get_workspace_observability_config,
        update_workspace_observability_config,
    )

    update_workspace_observability_config(
        "alpha",
        {"local": {"retention_days": 99}},
    )
    monkeypatch.setattr(migration, "_write_workspace_target", original_write)

    retry_code, retry = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )

    assert retry_code == 2
    assert retry["config_migration"]["source_retained"] is True
    assert "WORKSPACE_OBSERVABILITY_TARGET_CONFLICT" in retry["error_codes"]
    assert get_workspace_observability_config("alpha")["local"]["retention_days"] == 99
    assert not (
        runtime_root / "workspaces" / "beta" / "observability_config.json"
    ).exists()
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))


def test_failed_source_survives_unrelated_system_config_rewrite(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "llm": {
                    "model": "legacy-model",
                    "api_key": "legacy-plaintext-secret",
                },
                "observability": _local_observability(retention_days=12),
            }
        ),
        encoding="utf-8",
    )
    from config import system_config as system_config_module
    from migrations import observability_workspace_config_v1 as migration

    original_write = migration._write_workspace_target

    def fail_for_beta(target_root, workspace_id, config):
        if workspace_id == "beta":
            raise OSError("simulated")
        return original_write(target_root, workspace_id, config)

    monkeypatch.setattr(migration, "_write_workspace_target", fail_for_beta)
    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert report["config_migration"]["source_retained"] is True

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    system_config_module.load_raw_system_config()
    system_config_module.update_system_config_section("web_search", {"enabled": True})

    rewritten = json.loads(config_path.read_text(encoding="utf-8"))
    assert "legacy-plaintext-secret" not in config_path.read_text(encoding="utf-8")
    assert "observability" in rewritten

    monkeypatch.setattr(migration, "_write_workspace_target", original_write)
    retry_code, _retry = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )
    assert retry_code == 0
    assert "observability" not in json.loads(config_path.read_text(encoding="utf-8"))


def test_progress_marker_failure_happens_before_any_workspace_write(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    from migrations import observability_workspace_config_v1 as migration

    original_write_marker = migration._write_marker

    def fail_prepared(plan, *, status, backup_paths, existing=None):
        if status == migration.MARKER_PREPARED:
            raise OSError("simulated")
        return original_write_marker(
            plan,
            status=status,
            backup_paths=backup_paths,
            existing=existing,
        )

    monkeypatch.setattr(migration, "_write_marker", fail_prepared)
    code, report = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )

    assert code == 2
    assert "MIGRATION_MARKER_PREPARE_FAILED" in report["error_codes"]
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))
    assert not (
        runtime_root / "workspaces" / "alpha" / "observability_config.json"
    ).exists()
    assert not (runtime_root / migration.MARKER_RELATIVE_PATH).exists()

    monkeypatch.setattr(migration, "_write_marker", original_write_marker)
    retry_code, _retry = _execute(
        runtime_root,
        dry_run=False,
        log_dir=tmp_path / "logs",
    )
    assert retry_code == 0


def test_backup_creation_failure_never_removes_source_and_retry_succeeds(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    from migrations import observability_workspace_config_v1 as migration

    original_backup = migration._create_verified_bytes_backup
    monkeypatch.setattr(
        migration,
        "_create_verified_bytes_backup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("simulated")),
    )

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert "SYSTEM_CONFIG_COMMIT_FAILED" in report["error_codes"]
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))
    assert report["backup_paths"] == []

    monkeypatch.setattr(migration, "_create_verified_bytes_backup", original_backup)
    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")
    assert retry_code == 0
    assert retry["config_migration"]["source_retained"] is False


def test_atomic_source_rewrite_failure_leaves_prepared_marker_and_is_retryable(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    from migrations import observability_workspace_config_v1 as migration

    original_atomic = migration._write_bytes_atomic

    def fail_source(path, payload, *, mode=0o600):
        if Path(path) == config_path:
            raise OSError("simulated")
        return original_atomic(path, payload, mode=mode)

    monkeypatch.setattr(migration, "_write_bytes_atomic", fail_source)
    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert report["config_migration"]["source_retained"] is True
    marker = json.loads((runtime_root / migration.MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["status"] == "prepared"
    assert "observability" in json.loads(config_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(migration, "_write_bytes_atomic", original_atomic)
    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")
    assert retry_code == 0
    assert retry["config_migration"]["status"] == "applied"


def test_marker_finalize_failure_is_recovered_without_using_backup_as_rollback(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    from migrations import observability_workspace_config_v1 as migration

    original_write_marker = migration._write_marker

    def fail_completed(plan, *, status, backup_paths, existing=None):
        if status == migration.MARKER_COMPLETED:
            raise OSError("simulated")
        return original_write_marker(
            plan,
            status=status,
            backup_paths=backup_paths,
            existing=existing,
        )

    monkeypatch.setattr(migration, "_write_marker", fail_completed)
    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert report["config_migration"]["source_retained"] is False
    assert "observability" not in json.loads(config_path.read_text(encoding="utf-8"))
    marker = json.loads((runtime_root / migration.MARKER_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert marker["status"] == "prepared"

    monkeypatch.setattr(migration, "_write_marker", original_write_marker)
    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")
    assert retry_code == 0
    assert retry["config_migration"]["status"] == "completed-from-prepared-marker"


def test_log_cleanup_failure_is_best_effort_and_retries_independently(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old_log = log_dir / "agent_perf-2026-01-01.log"
    old_log.write_text("old\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_old_log(path, *args, **kwargs):
        if path == old_log:
            raise OSError("simulated")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_log)
    code, report = _execute(runtime_root, dry_run=False, log_dir=log_dir)

    assert code == 0
    assert report["ok"] is True
    assert report["legacy_log_cleanup"]["ok"] is False
    assert "LEGACY_LOG_CLEANUP_FAILED" in report["legacy_log_cleanup"]["error_codes"]
    assert report["warnings"]
    assert old_log.exists()
    assert "observability" not in json.loads(config_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(Path, "unlink", original_unlink)
    retry_code, retry = _execute(runtime_root, dry_run=False, log_dir=log_dir)
    assert retry_code == 0
    assert retry["counts"]["legacy_logs_deleted"] == 1
    assert not old_log.exists()


def test_missing_secret_key_blocks_without_creating_key_or_writing_files(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch, with_key=False)
    _write_global_config(
        config_path,
        {
            "enabled": True,
            "mode": "langfuse",
            "langfuse": {"secret_key": "plaintext-secret"},
        },
    )
    before = _tree_snapshot(runtime_root)

    code, report = _execute(runtime_root, dry_run=True, log_dir=tmp_path / "logs")

    assert code == 2
    assert "SECRET_KEY_REQUIRED" in report["error_codes"]
    assert _tree_snapshot(runtime_root) == before
    assert not (runtime_root / "config" / "secret.key").exists()
    assert "plaintext-secret" not in json.dumps(report, ensure_ascii=False)


def test_legacy_default_workspace_is_not_implicitly_moved_by_observability_migration(
    tmp_path,
    monkeypatch,
):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch, workspace_ids=())
    legacy_dir = runtime_root / "workspaces" / "default"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "workspace.json").write_text(
        json.dumps({"id": "default", "name": "Legacy"}),
        encoding="utf-8",
    )
    _write_global_config(config_path, _local_observability())
    before = _tree_snapshot(runtime_root)

    code, report = _execute(runtime_root, dry_run=False, log_dir=tmp_path / "logs")

    assert code == 2
    assert "LEGACY_DEFAULT_WORKSPACE_REQUIRES_MIGRATION" in report["error_codes"]
    assert legacy_dir.is_dir()
    assert _tree_snapshot(runtime_root) == before


def test_normal_workspace_observability_reads_never_modify_persisted_files(
    tmp_path,
    monkeypatch,
):
    runtime_root, _config_path = _setup_runtime(
        tmp_path,
        monkeypatch,
        workspace_ids=("demo",),
    )
    from integration.observability.workspace_config import (
        get_public_workspace_observability_config,
        get_workspace_observability_config,
        test_workspace_observability_connection,
        write_workspace_observability_config,
    )

    write_workspace_observability_config(
        "demo",
        {
            "enabled": True,
            "mode": "local",
            "local": {"retention_days": 7},
            "langfuse": {
                "public_key": "",
                "secret_key": "read-only-secret",
                "base_url": "",
                "debug": False,
            },
        },
    )
    before = _tree_snapshot(runtime_root)

    assert get_workspace_observability_config("demo")["local"]["retention_days"] == 7
    assert get_public_workspace_observability_config("demo")["langfuse"]["has_secret_key"] is True
    assert test_workspace_observability_connection("demo")["success"] is True

    assert _tree_snapshot(runtime_root) == before


def test_apply_requires_explicit_writes_stopped_confirmation(tmp_path, monkeypatch):
    runtime_root, config_path = _setup_runtime(tmp_path, monkeypatch)
    _write_global_config(config_path, _local_observability())
    before = _tree_snapshot(runtime_root)

    code, report = _execute(
        runtime_root,
        dry_run=False,
        writes_stopped=False,
        log_dir=tmp_path / "logs",
    )

    assert code == 2
    assert report["error_codes"] == ["WRITES_NOT_STOPPED"]
    assert _tree_snapshot(runtime_root) == before
