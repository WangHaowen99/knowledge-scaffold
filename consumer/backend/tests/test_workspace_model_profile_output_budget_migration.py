# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Regression coverage for workspace-private model-profile budgets."""

from __future__ import annotations

from internal_access import internal
import json
from pathlib import Path


def _workspace_dir(runtime_root: Path, workspace_id: str = "demo") -> Path:
    path = runtime_root / "workspaces" / workspace_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "workspace.json").write_text(
        json.dumps({"id": workspace_id, "name": "Demo"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _configure_runtime(tmp_path: Path, monkeypatch) -> Path:
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    return runtime_root


def _execute(runtime_root: Path, *, dry_run: bool):
    from migrations.workspace_model_profile_output_budget_v1 import execute_migration

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=not dry_run,
        from_version="0.26.7.0",
        to_version="0.26.8.0",
    )


def test_workspace_legacy_budgets_are_migrated_with_backup_and_idempotency(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    workspace_dir = _workspace_dir(runtime_root)
    path = workspace_dir / "llm_profiles.json"
    original = {
        "profiles": [
            {"id": "old-4k", "max_tokens": 4096},
            {"id": "old-8k", "max_tokens": 8192},
            {"id": "custom", "max_tokens": 16384},
            {"id": "missing"},
        ]
    }
    original_bytes = (json.dumps(original, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(original_bytes)

    code, preview = _execute(runtime_root, dry_run=True)

    assert code == 0
    assert preview["status"] == "dry-run-ready"
    assert preview["counts"]["profiles_migrated"] == 2
    assert preview["counts"]["profiles_defaulted"] == 1
    assert path.read_bytes() == original_bytes

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert [profile.get("max_tokens") for profile in persisted["profiles"]] == [
        32768,
        32768,
        16384,
        32768,
    ]
    assert report["counts"]["files_written"] == 1
    assert report["backup_paths"]
    assert Path(report["backup_paths"][0]).read_bytes() == original_bytes

    code, repeated = _execute(runtime_root, dry_run=False)
    assert code == 0
    assert repeated["status"] == "already-applied"


def test_workspace_migration_is_noop_without_profile_files(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _workspace_dir(runtime_root)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "noop"
    assert report["counts"]["files_scanned"] == 0


def test_startup_runner_migrates_workspace_profile_before_runtime_resolution(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    workspace_dir = _workspace_dir(runtime_root, "alpha")
    path = workspace_dir / "llm_profiles.json"
    path.write_text(
        json.dumps({"profiles": [{"id": "private", "max_tokens": 8192}]})
        + "\n",
        encoding="utf-8",
    )

    from migrations.runner import run_startup_migrations

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["profiles"][0]["max_tokens"] == 32768
    assert any(
        item["migration_id"] == "workspace-model-profile-output-budget-v1"
        for item in report["migrations"]
    )


def test_workspace_marker_retains_recovery_paths_across_incremental_runs(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    alpha = _workspace_dir(runtime_root, "alpha")
    (alpha / "llm_profiles.json").write_text(
        json.dumps({"profiles": [{"id": "a", "max_tokens": 4096}]}) + "\n",
        encoding="utf-8",
    )
    code, first = _execute(runtime_root, dry_run=False)
    assert code == 0
    assert first["status"] == "applied"

    beta = _workspace_dir(runtime_root, "beta")
    (beta / "llm_profiles.json").write_text(
        json.dumps({"profiles": [{"id": "b", "max_tokens": 8192}]}) + "\n",
        encoding="utf-8",
    )
    code, second = _execute(runtime_root, dry_run=False)
    assert code == 0
    assert second["status"] == "applied"

    marker_path = runtime_root / "config" / "migrations" / (
        "workspace-model-profile-output-budget-v1.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    backups = marker["manual_recovery_backups"]
    assert any("alpha" in path for path in backups)
    assert any("beta" in path for path in backups)


def test_workspace_partial_apply_is_retryable_and_retains_previous_backup(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    alpha = _workspace_dir(runtime_root, "alpha")
    beta = _workspace_dir(runtime_root, "beta")
    alpha_path = alpha / "llm_profiles.json"
    beta_path = beta / "llm_profiles.json"
    alpha_path.write_text(
        json.dumps({"profiles": [{"id": "a", "max_tokens": 4096}]}) + "\n",
        encoding="utf-8",
    )
    beta_path.write_text(
        json.dumps({"profiles": [{"id": "b", "max_tokens": 8192}]}) + "\n",
        encoding="utf-8",
    )

    import migrations.workspace_model_profile_output_budget_v1 as migration

    original_writer = internal(migration).write_bytes_atomic

    def fail_beta_target(path, payload):
        if path == beta_path:
            raise OSError("synthetic beta write failure")
        return original_writer(path, payload)

    monkeypatch.setattr(migration, "_write_bytes_atomic", fail_beta_target)
    code, partial = _execute(runtime_root, dry_run=False)

    assert code == 2
    assert partial["status"] == "blocked-partial"
    assert json.loads(alpha_path.read_text(encoding="utf-8"))["profiles"][0][
        "max_tokens"
    ] == 32768
    assert json.loads(beta_path.read_text(encoding="utf-8"))["profiles"][0][
        "max_tokens"
    ] == 8192

    monkeypatch.setattr(migration, "_write_bytes_atomic", original_writer)
    code, retry = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert retry["status"] == "applied"
    marker = json.loads(
        (runtime_root / "config" / "migrations" / f"{migration.MIGRATION_ID}.json").read_text()
    )
    assert any("alpha" in path for path in marker["manual_recovery_backups"])
    assert any("beta" in path for path in marker["manual_recovery_backups"])


def test_workspace_explicit_null_budget_is_rejected_and_source_is_preserved(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    workspace_dir = _workspace_dir(runtime_root)
    path = workspace_dir / "llm_profiles.json"
    original = {"profiles": [{"id": "broken", "max_tokens": None}]}
    original_bytes = (json.dumps(original, ensure_ascii=False) + "\n").encode()
    path.write_bytes(original_bytes)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 2
    assert report["status"] == "blocked"
    assert "WORKSPACE_MODEL_PROFILE_MAX_TOKENS_INVALID" in report["error_codes"]
    assert path.read_bytes() == original_bytes
