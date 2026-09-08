"""Regression coverage for legacy model-profile output budgets."""

from __future__ import annotations

import json
import os
from pathlib import Path


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _config_path(runtime_root: Path) -> Path:
    return runtime_root / "config" / "system_config.json"


def _configure_runtime(tmp_path: Path, monkeypatch) -> Path:
    import config.system_config as system_config_module

    runtime_root = tmp_path / ".onto-runtime"
    config_path = _config_path(runtime_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    return runtime_root


def _write_config(runtime_root: Path, *, max_tokens: int) -> bytes:
    document = {
        "llm_profiles": {
            "default_main_profile_ref": {"scope": "global", "id": "dsv4"},
            "profiles": [
                {
                    "id": "dsv4",
                    "name": "DeepSeek V4",
                    "provider": "openai_compatible",
                    "base_url": "https://llm.example/v1",
                    "model": "deepseek-v4",
                    "api_key": "",
                    "timeout": 600,
                    "max_tokens": max_tokens,
                    "temperature": 0.7,
                    "enabled": True,
                }
            ],
        },
        "future_section": {"preserved": True},
    }
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    _config_path(runtime_root).write_bytes(payload)
    return payload


def _execute(runtime_root: Path, *, dry_run: bool):
    from migrations.system_config_model_profile_output_budget_v1 import (
        execute_migration,
    )

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=not dry_run,
        from_version="0.26.7.0",
        to_version="0.26.8.0",
    )


def _execute_env(runtime_root: Path, *, dry_run: bool, writes_stopped: bool = True):
    from migrations.system_config_model_profile_output_budget_env_v1 import (
        execute_migration,
    )

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=writes_stopped,
        from_version="0.26.7.0",
        to_version="0.26.8.0",
    )


def test_environment_budget_dry_run_is_read_only(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")

    code, report = _execute_env(runtime_root, dry_run=True)

    assert code == 0
    assert report["status"] == "dry-run-ready"
    assert report["counts"]["variables_pinned"] == 1
    assert os.environ["LLM_MAX_TOKENS"] == "4096"


def test_environment_budget_preserves_custom_lower_value_with_warning(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_MAX_TOKENS", "16384")

    code, report = _execute_env(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "noop"
    assert report["counts"]["custom_below_current_default"] == 1
    assert os.environ["LLM_MAX_TOKENS"] == "16384"
    assert report["warnings"]


def test_environment_budget_invalid_value_blocks_without_mutation(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_MAX_TOKENS", "not-a-number")

    code, report = _execute_env(runtime_root, dry_run=False)

    assert code == 2
    assert report["status"] == "blocked"
    assert "SYSTEM_CONFIG_ENV_MAX_TOKENS_INVALID" in report["error_codes"]
    assert os.environ["LLM_MAX_TOKENS"] == "not-a-number"


def test_legacy_explicit_profile_budget_is_migrated_atomically(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_config(runtime_root, max_tokens=4096)

    code, preview = _execute(runtime_root, dry_run=True)

    assert code == 0
    assert preview["status"] == "dry-run-ready"
    assert preview["counts"]["profiles_migrated"] == 1
    assert _config_path(runtime_root).read_bytes() == original

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    profile = migrated["llm_profiles"]["profiles"][0]
    assert profile["max_tokens"] == 32768
    assert migrated["future_section"] == {"preserved": True}
    assert report["counts"]["profiles_migrated"] == 1
    assert report["backup_paths"]

    code, repeated = _execute(runtime_root, dry_run=False)
    assert code == 0
    assert repeated["status"] == "already-applied"


def test_reapply_preserves_previous_marker_recovery_backup_paths(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original_bytes = _write_config(runtime_root, max_tokens=4096)

    code, first_report = _execute(runtime_root, dry_run=False)

    assert code == 0
    first_backup = Path(first_report["backup_paths"][0])
    assert first_backup.read_bytes() == original_bytes

    manually_changed = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    manually_changed["llm_profiles"]["profiles"][0]["max_tokens"] = 8192
    manually_changed_bytes = (
        json.dumps(manually_changed, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    _config_path(runtime_root).write_bytes(manually_changed_bytes)

    code, second_report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert second_report["status"] == "applied"
    marker_path = runtime_root / "config" / "migrations" / (
        "system-config-model-profile-output-budget-v1.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    recovery_paths = [Path(path) for path in marker["manual_recovery_backups"]]
    assert first_backup in recovery_paths
    assert any(
        path.read_bytes() == manually_changed_bytes and path != first_backup
        for path in recovery_paths
    )


def test_invalid_marker_recovery_paths_block_without_touching_config(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_config(runtime_root, max_tokens=4096)
    code, _ = _execute(runtime_root, dry_run=False)
    assert code == 0

    marker_path = runtime_root / "config" / "migrations" / (
        "system-config-model-profile-output-budget-v1.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["manual_recovery_backups"] = None
    marker_path.write_text(json.dumps(marker) + "\n", encoding="utf-8")
    before = _config_path(runtime_root).read_bytes()

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_MIGRATION_MARKER_INVALID" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == before


def test_intermediate_8192_profile_budget_is_migrated(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_config(runtime_root, max_tokens=8192)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    persisted = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert persisted["llm_profiles"]["profiles"][0]["max_tokens"] == 32768


def test_non_legacy_explicit_profile_budget_is_preserved(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_config(runtime_root, max_tokens=16384)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "noop"
    assert report["counts"]["profiles_migrated"] == 0
    assert report["warnings"] == [
        "1 model profile(s) use a custom max_tokens below 32768"
    ]
    assert _config_path(runtime_root).read_bytes() == original


def test_legacy_top_level_llm_budget_is_migrated_when_profiles_are_absent(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    document = {
        "llm": {
            "base_url": "https://llm.example/v1",
            "model": "deepseek-v4",
            "api_key": "",
            "max_tokens": 4096,
        }
    }
    _config_path(runtime_root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    persisted = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert persisted["llm"]["max_tokens"] == 32768


def test_legacy_top_level_llm_missing_budget_preserves_environment_precedence(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_MAX_TOKENS", "16384")
    document = {
        "llm": {
            "base_url": "https://llm.example/v1",
            "model": "deepseek-v4",
            "api_key": "",
        }
    }
    original = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    _config_path(runtime_root).write_bytes(original)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "noop"
    assert _config_path(runtime_root).read_bytes() == original


def test_startup_pins_legacy_environment_when_top_level_budget_is_missing(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4")
    monkeypatch.setenv("LLM_MAX_TOKENS", "8192")
    document = {
        "llm": {
            "base_url": "https://llm.example/v1",
            "model": "deepseek-v4",
            "api_key": "",
        }
    }
    _config_path(runtime_root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    from migrations.runner import run_startup_migrations

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    environment_report = next(
        item
        for item in report["migrations"]
        if item["migration_id"] == "system-config-model-profile-output-budget-env-v1"
    )
    assert environment_report["counts"]["variables_pinned"] == 1
    from config.system_config import resolve_role_config

    assert resolve_role_config("main")["max_tokens"] == 32768


def test_startup_preserves_custom_environment_for_legacy_generated_main_profile(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("LLM_MAX_TOKENS", "16384")
    document = {
        "llm": {
            "base_url": "https://llm.example/v1",
            "model": "deepseek-v4",
            "api_key": "",
        },
        "task_llm": {
            "base_url": "https://task.example/v1",
            "model": "task-model",
            "api_key": "",
        },
    }
    _config_path(runtime_root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    from migrations.runner import run_startup_migrations

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    persisted = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    main_profile = next(
        profile
        for profile in persisted["llm_profiles"]["profiles"]
        if profile["id"] == "default"
    )
    assert main_profile["max_tokens"] == 16384
    from config.system_config import resolve_role_config

    assert resolve_role_config("main")["max_tokens"] == 16384


def test_explicit_null_budget_is_rejected_and_source_is_preserved(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_config(runtime_root, max_tokens=4096)
    document = json.loads(original.decode("utf-8"))
    document["llm_profiles"]["profiles"][0]["max_tokens"] = None
    original = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    _config_path(runtime_root).write_bytes(original)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 2
    assert report["status"] == "blocked"
    assert "SYSTEM_CONFIG_MODEL_PROFILE_MAX_TOKENS_INVALID" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_legacy_task_profile_budget_is_preserved_for_compatibility(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    document = {
        "llm_profiles": {
            "default_task_profile_ref": {"scope": "global", "id": "legacy-task-llm"},
            "profiles": [
                {
                    "id": "legacy-task-llm",
                    "name": "Legacy task",
                    "provider": "openai_compatible",
                    "base_url": "https://llm.example/v1",
                    "model": "task-model",
                    "max_tokens": 4096,
                    "timeout": 600,
                    "temperature": 0.7,
                    "enabled": True,
                }
            ],
        }
    }
    _config_path(runtime_root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "noop"
    assert report["warnings"] == []
    persisted = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert persisted["llm_profiles"]["profiles"][0]["max_tokens"] == 4096


def test_legacy_task_profile_missing_budget_gets_compatibility_default(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    document = {
        "llm_profiles": {
            "profiles": [
                {
                    "id": "legacy-task-llm",
                    "base_url": "https://llm.example/v1",
                    "model": "task-model",
                }
            ]
        }
    }
    _config_path(runtime_root).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 0
    assert report["counts"]["legacy_task_profiles_defaulted"] == 1
    persisted = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert persisted["llm_profiles"]["profiles"][0]["max_tokens"] == 4096


def test_legacy_task_profile_invalid_budget_is_rejected(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    document = {
        "llm_profiles": {
            "profiles": [{"id": "legacy-task-llm", "max_tokens": None}]
        }
    }
    original = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    _config_path(runtime_root).write_bytes(original)

    code, report = _execute(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_MODEL_PROFILE_MAX_TOKENS_INVALID" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_startup_resolves_migrated_profile_with_current_budget(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_config(runtime_root, max_tokens=4096)

    from migrations.runner import run_startup_migrations

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    budget_report = next(
        item
        for item in report["migrations"]
        if item["migration_id"] == "system-config-model-profile-output-budget-v1"
    )
    assert budget_report["counts"]["profiles_migrated"] == 1

    from config.system_config import resolve_role_config

    resolved = resolve_role_config("main")
    assert resolved["max_tokens"] == 32768


def test_startup_pins_legacy_process_env_when_persisted_config_is_absent(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.delenv("SYSTEM_CONFIG_PATH", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4")
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")

    # Prime the config snapshot with the old process value. Startup handling
    # must invalidate that snapshot after pinning the compatibility value.
    from config.system_config import resolve_role_config

    assert resolve_role_config("main")["max_tokens"] == 4096

    from migrations.runner import run_startup_migrations

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    environment_report = next(
        item
        for item in report["migrations"]
        if item["migration_id"] == "system-config-model-profile-output-budget-env-v1"
    )
    assert environment_report["counts"]["variables_pinned"] == 1
    assert os.environ["LLM_MAX_TOKENS"] == "32768"
    assert resolve_role_config("main")["max_tokens"] == 32768
