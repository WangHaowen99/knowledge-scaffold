# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Issue #14 model/profile and secret-encryption migration coverage."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from backend_synthetic_credentials import ENCRYPTION_KEY, synthetic_secret
from internal_access import internal

SECRET_TEST_KEY = ENCRYPTION_KEY


def _config_path(runtime_root: Path) -> Path:
    return runtime_root / "config" / "system_config.json"


def _configure_runtime(tmp_path: Path, monkeypatch, *, with_key: bool = True) -> Path:
    runtime_root = tmp_path / ".onto-runtime"
    config_path = _config_path(runtime_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(config_path))
    if with_key:
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    else:
        monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    return runtime_root


def _write_document(runtime_root: Path, document) -> bytes:
    path = _config_path(runtime_root)
    payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(payload)
    return payload


def _tree_snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _profile(
    profile_id: str,
    *,
    model: str = "main-model",
    api_key: str = "",
    base_url: str = "https://llm.example.net/v1",
):
    return {
        "id": profile_id,
        "name": profile_id,
        "provider": "openai_compatible",
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout": 600,
        "max_tokens": 4096,
        "temperature": 0.7,
        "enabled": True,
    }


def _execute_model(runtime_root: Path, *, dry_run: bool):
    from migrations.system_config_model_profiles_v1 import execute_migration

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=not dry_run,
    )


def _execute_secret(runtime_root: Path, *, dry_run: bool):
    from migrations.system_config_secret_encryption_v1 import execute_migration

    return execute_migration(
        runtime_root,
        dry_run=dry_run,
        writes_stopped=not dry_run,
    )


def _run_startup_in_process(runtime_root: Path, key: str):
    os.environ["PROJECT_ROOT"] = str(runtime_root)
    os.environ["SYSTEM_CONFIG_PATH"] = str(_config_path(runtime_root))
    os.environ["SECRET_ENCRYPTION_KEY"] = key
    os.environ["LOG_DIR"] = str(runtime_root / "legacy-logs")
    from migrations.runner import run_startup_migrations

    return run_startup_migrations(runtime_root)


def test_two_dry_runs_are_read_only_and_preview_over_limit_model_projection(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "1")
    _write_document(
        runtime_root,
        {
            "llm": {
                "base_url": "https://main.example.net/v1",
                "model": "main-model",
                "api_key": synthetic_secret("system_config_migrations-credential-05"),
            },
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
                "timeout": 45,
            },
            "llm_profiles": {
                "default_profile_id": "main",
                "profiles": [_profile("main", api_key=synthetic_secret("system_config_migrations-credential-09"))],
            },
            "auth": {
                "auth_secret": synthetic_secret("system_config_migrations-credential-03"),
                "auth_server_admin_token": synthetic_secret("system_config_migrations-credential-02"),
            },
            "web_search": {"api_key": synthetic_secret("system_config_migrations-credential-12")},
        },
    )
    before = _tree_snapshot(runtime_root)

    model_code, model_report = _execute_model(runtime_root, dry_run=True)
    after_model = _tree_snapshot(runtime_root)
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=True)

    assert model_code == 0
    assert model_report["status"] == "dry-run-ready"
    assert secret_code == 0
    assert secret_report["status"] == "dry-run-ready"
    assert secret_report["counts"]["projected_model_changes"] == 1
    assert secret_report["counts"]["plaintext_secrets"] == 6
    assert before == after_model == _tree_snapshot(runtime_root)


def test_two_migrations_apply_with_independent_backups_markers_and_contexts(
    tmp_path,
    monkeypatch,
):
    from migrations.system_config_model_profiles_v1 import BACKUP_SUFFIX as MODEL_BACKUP_SUFFIX
    from migrations.system_config_model_profiles_v1 import MIGRATION_ID as MODEL_ID
    from migrations.system_config_secret_encryption_v1 import BACKUP_SUFFIX as SECRET_BACKUP_SUFFIX
    from migrations.system_config_secret_encryption_v1 import MIGRATION_ID as SECRET_ID
    from utils.security.secrets import decrypt_secret

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = {
        "llm": {
            "base_url": "https://main.example.net/v1",
            "model": "main-model",
            "api_key": synthetic_secret("system_config_migrations-credential-05"),
        },
        "task_llm": {
            "base_url": "https://task.example.net/v1",
            "model": "task-model",
            "api_key": synthetic_secret("system_config_migrations-credential-13"),
            "timeout": 45,
        },
        "llm_profiles": {
            "default_profile_id": "main",
            "profiles": [_profile("main", api_key=synthetic_secret("system_config_migrations-credential-09"))],
        },
        "auth": {
            "auth_secret": synthetic_secret("system_config_migrations-credential-03"),
            "auth_server_admin_token": synthetic_secret("system_config_migrations-credential-02"),
        },
        "web_search": {"api_key": synthetic_secret("system_config_migrations-credential-12")},
        "observability": {"enabled": True, "mode": "local"},
        "future_section": {"preserved": True},
    }
    original_bytes = _write_document(runtime_root, original)

    model_code, model_report = _execute_model(runtime_root, dry_run=False)
    model_document_bytes = _config_path(runtime_root).read_bytes()
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=False)

    assert model_code == secret_code == 0
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert "task_llm" not in migrated
    assert "default_profile_id" not in migrated["llm_profiles"]
    assert migrated["llm_profiles"]["default_main_profile_ref"] == {
        "scope": "global",
        "id": "main",
    }
    assert migrated["llm_profiles"]["default_task_profile_ref"] == {
        "scope": "global",
        "id": "legacy-task-llm",
    }
    task_profile = next(
        profile for profile in migrated["llm_profiles"]["profiles"] if profile["id"] == "legacy-task-llm"
    )
    assert decrypt_secret(
        task_profile["api_key"],
        context="llm_profiles.profiles.api_key",
        allow_key_creation=False,
        runtime_root=runtime_root,
    ) == synthetic_secret("system_config_migrations-credential-13")
    assert task_profile["max_tokens"] == 4096
    assert migrated["observability"] == original["observability"]
    assert migrated["future_section"] == original["future_section"]
    assert synthetic_secret("system_config_migrations-credential-05") not in _config_path(runtime_root).read_text(
        encoding="utf-8"
    )
    assert synthetic_secret("system_config_migrations-credential-02") not in _config_path(runtime_root).read_text(
        encoding="utf-8"
    )

    model_backup = _config_path(runtime_root).with_name(f"system_config.json.{MODEL_BACKUP_SUFFIX}.bak")
    secret_backup = _config_path(runtime_root).with_name(f"system_config.json.{SECRET_BACKUP_SUFFIX}.bak")
    assert model_backup.read_bytes() == original_bytes
    assert secret_backup.read_bytes() == model_document_bytes
    if os.name == "posix":  # POSIX permission bits are meaningless on Windows
        assert stat.S_IMODE(model_backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(secret_backup.stat().st_mode) == 0o600
    assert model_report["backup_paths"] == [str(model_backup)]
    assert secret_report["backup_paths"] == [str(secret_backup)]
    for migration_id in (MODEL_ID, SECRET_ID):
        marker = runtime_root / "config" / "migrations" / f"{migration_id}.json"
        assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "completed"
        if os.name == "posix":
            assert stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_two_migrations_apply_without_posix_only_file_operations(
    tmp_path,
    monkeypatch,
):
    import migrations.system_config_v1_support as support
    from utils.security.secrets import is_encrypted_secret

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
            }
        },
    )

    real_open = support.os.open

    def windows_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path).is_dir():
            raise AssertionError("Windows migration path must not open directories")
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(support, "_WINDOWS", True)
    monkeypatch.delattr(support.os, "fchmod", raising=False)
    monkeypatch.setattr(support.os, "open", windows_open)

    model_code, model_report = _execute_model(runtime_root, dry_run=False)
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=False)

    assert model_code == secret_code == 0
    assert model_report["status"] == secret_report["status"] == "applied"
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert "task_llm" not in migrated
    assert is_encrypted_secret(migrated["llm_profiles"]["profiles"][0]["api_key"])


def test_no_old_data_is_a_noop_without_marker_or_backup(tmp_path, monkeypatch):
    from migrations.system_config_model_profiles_v1 import MIGRATION_ID as MODEL_ID
    from migrations.system_config_secret_encryption_v1 import MIGRATION_ID as SECRET_ID

    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    original = _write_document(runtime_root, {"storage": {"project_root": "/runtime"}})

    model_code, model_report = _execute_model(runtime_root, dry_run=False)
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=False)

    assert model_code == secret_code == 0
    assert model_report["status"] == secret_report["status"] == "noop"
    assert _config_path(runtime_root).read_bytes() == original
    assert not (runtime_root / "config" / "migrations" / f"{MODEL_ID}.json").exists()
    assert not (runtime_root / "config" / "migrations" / f"{SECRET_ID}.json").exists()
    assert list(_config_path(runtime_root).parent.glob("*.bak*")) == []
    assert not (runtime_root / "config" / "secret.key").exists()


def test_repeated_execution_validates_target_without_rewriting(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
            }
        },
    )
    assert _execute_model(runtime_root, dry_run=False)[0] == 0
    assert _execute_secret(runtime_root, dry_run=False)[0] == 0
    before = _tree_snapshot(runtime_root)

    model_code, model_report = _execute_model(runtime_root, dry_run=False)
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=False)

    assert model_code == secret_code == 0
    assert model_report["status"] == secret_report["status"] == "already-applied"
    assert _tree_snapshot(runtime_root) == before


@pytest.mark.parametrize(
    ("raw_bytes", "error_code"),
    [
        (b"{invalid", "SYSTEM_CONFIG_INVALID_JSON"),
        (b"[]\n", "SYSTEM_CONFIG_NOT_OBJECT"),
    ],
)
@pytest.mark.parametrize("executor", [_execute_model, _execute_secret])
def test_invalid_json_or_non_object_blocks_without_writes(
    tmp_path,
    monkeypatch,
    raw_bytes,
    error_code,
    executor,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _config_path(runtime_root).write_bytes(raw_bytes)
    before = _tree_snapshot(runtime_root)

    code, report = executor(runtime_root, dry_run=False)

    assert code == 2
    assert error_code in report["error_codes"]
    assert _tree_snapshot(runtime_root) == before


@pytest.mark.parametrize("executor", [_execute_model, _execute_secret])
def test_system_config_symlink_is_rejected_without_touching_target(
    tmp_path,
    monkeypatch,
    executor,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    real_path = tmp_path / "external-system-config.json"
    original = json.dumps({"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}).encode()
    real_path.write_bytes(original)
    symlink_path = runtime_root / "config" / "linked-system-config.json"
    symlink_path.parent.mkdir(parents=True, exist_ok=True)
    symlink_path.symlink_to(real_path)
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(symlink_path))

    code, report = executor(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_UNSAFE_FILE" in report["error_codes"]
    assert real_path.read_bytes() == original
    assert symlink_path.is_symlink()


def test_same_legacy_task_and_referenced_profile_reuses_target(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    task = {
        "base_url": "https://task.example.net/v1",
        "model": "task-model",
        "api_key": synthetic_secret("system_config_migrations-credential-11"),
        "timeout": 600,
    }
    _write_document(
        runtime_root,
        {
            "task_llm": task,
            "llm_profiles": {
                "default_task_profile_ref": {"scope": "global", "id": "task"},
                "profiles": [
                    _profile(
                        "task",
                        model="task-model",
                        api_key=synthetic_secret("system_config_migrations-credential-11"),
                        base_url="https://task.example.net/v1",
                    )
                ],
            },
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 0
    assert report["counts"]["profiles_created"] == 0
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert "task_llm" not in migrated
    assert [profile["id"] for profile in migrated["llm_profiles"]["profiles"]] == ["task"]


def test_conflicting_legacy_task_and_referenced_profile_blocks(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "old-task",
                "api_key": synthetic_secret("system_config_migrations-credential-07"),
            },
            "llm_profiles": {
                "default_task_profile_ref": {"scope": "global", "id": "task"},
                "profiles": [
                    _profile(
                        "task", model="new-task", api_key=synthetic_secret("system_config_migrations-credential-06")
                    )
                ],
            },
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_TASK_PROFILE_CONFLICT" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_stable_task_profile_id_collision_blocks_without_overwrite(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "legacy-task",
            },
            "llm_profiles": {"profiles": [_profile("legacy-task-llm", model="unrelated-model")]},
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_TASK_PROFILE_ID_CONFLICT" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_startup_runner_migrates_over_limit_profiles_without_blocking_runtime(
    tmp_path,
    monkeypatch,
):
    import config.system_config as system_config_module
    from migrations.runner import run_startup_migrations

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "10")
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
            },
            "llm_profiles": {"profiles": [_profile("profile-{}".format(index)) for index in range(10)]},
        },
    )

    report = run_startup_migrations(runtime_root)

    assert report["ok"] is True
    model_report = next(
        item for item in report["migrations"] if item["migration_id"] == "system-config-model-profiles-v1"
    )
    assert model_report["status"] == "applied"
    config_path = _config_path(runtime_root)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    effective = system_config_module.load_system_config()
    assert len(effective["llm_profiles"]["profiles"]) == 11


def test_encrypted_legacy_task_secret_is_rebound_to_profile_context(
    tmp_path,
    monkeypatch,
):
    from utils.security.secrets import SecretEncryptionError, decrypt_secret, encrypt_secret

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    legacy_ciphertext = encrypt_secret(
        "legacy-encrypted-secret",
        context="task_llm.api_key",
        allow_key_creation=False,
        runtime_root=runtime_root,
    )
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": legacy_ciphertext,
            }
        },
    )

    code, _report = _execute_model(runtime_root, dry_run=False)

    assert code == 0
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    rebound = migrated["llm_profiles"]["profiles"][0]["api_key"]
    assert rebound != legacy_ciphertext
    assert (
        decrypt_secret(
            rebound,
            context="llm_profiles.profiles.api_key",
            allow_key_creation=False,
            runtime_root=runtime_root,
        )
        == "legacy-encrypted-secret"
    )
    with pytest.raises(SecretEncryptionError):
        decrypt_secret(
            rebound,
            context="task_llm.api_key",
            allow_key_creation=False,
            runtime_root=runtime_root,
        )


@pytest.mark.parametrize(
    ("main_ref", "expected_code"),
    [
        ({"scope": "global", "id": "main"}, None),
        ({"scope": "global", "id": "other"}, "SYSTEM_CONFIG_DEFAULT_PROFILE_REF_CONFLICT"),
    ],
)
def test_legacy_default_id_same_or_conflicting_new_ref(
    tmp_path,
    monkeypatch,
    main_ref,
    expected_code,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root,
        {
            "llm_profiles": {
                "default_profile_id": "main",
                "default_main_profile_ref": main_ref,
                "profiles": [_profile("main"), _profile("other")],
            }
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    if expected_code:
        assert code == 2
        assert expected_code in report["error_codes"]
        assert _config_path(runtime_root).read_bytes() == original
    else:
        assert code == 0
        migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
        assert "default_profile_id" not in migrated["llm_profiles"]


@pytest.mark.parametrize(
    ("profiles_section", "expected_code"),
    [
        (
            {"profiles": [_profile("duplicate"), _profile("duplicate")]},
            "SYSTEM_CONFIG_PROFILE_ID_DUPLICATE",
        ),
        (
            {
                "default_main_profile_ref": {"scope": "global", "id": "missing"},
                "profiles": [_profile("main")],
            },
            "SYSTEM_CONFIG_MAIN_PROFILE_TARGET_MISSING",
        ),
        (
            {
                "default_task_profile_ref": {"scope": "global", "id": "missing"},
                "profiles": [_profile("main")],
            },
            "SYSTEM_CONFIG_TASK_PROFILE_TARGET_MISSING",
        ),
    ],
)
def test_duplicate_profile_ids_and_missing_targets_block(
    tmp_path,
    monkeypatch,
    profiles_section,
    expected_code,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(runtime_root, {"llm_profiles": profiles_section})

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 2
    assert expected_code in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


@pytest.mark.parametrize(
    "task_section",
    [
        [],
        {"base_url": ["invalid"], "model": "task"},
        {"base_url": "https://task.example.net/v1", "model": ""},
        {"base_url": "", "model": "", "timeout": 45},
        {"base_url": "https://task.example.net/v1", "model": "task", "timeout": "600"},
        {"base_url": "https://task.example.net/v1", "model": "task", "timeout": {}},
    ],
)
def test_legacy_task_invalid_types_or_incomplete_fields_block(
    tmp_path,
    monkeypatch,
    task_section,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(runtime_root, {"task_llm": task_section})

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 2
    assert report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_default_only_legacy_task_section_is_safely_removed(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "provider": "openai_compatible",
                "base_url": "",
                "model": "",
                "api_key": "",
                "timeout": 600,
                "max_tokens": 4096,
                "temperature": 0.7,
                "enabled": True,
            }
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    assert "task_llm" not in json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))


def test_legacy_main_without_max_tokens_migrates_to_current_default(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    _write_document(
        runtime_root,
        {
            "llm": {
                "base_url": "https://main.example.net/v1",
                "model": "synthetic-main-model",
            },
            "task_llm": {
                "max_tokens": 4096,
            },
        },
    )

    code, report = _execute_model(runtime_root, dry_run=False)

    assert code == 0
    assert report["status"] == "applied"
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    main_profile = next(profile for profile in migrated["llm_profiles"]["profiles"] if profile["id"] == "default")
    assert main_profile["max_tokens"] == 32768


def test_plaintext_encrypted_and_mixed_secrets_are_fully_validated_and_encrypted(
    tmp_path,
    monkeypatch,
):
    from utils.security.secrets import decrypt_secret, encrypt_secret

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    encrypted_auth = encrypt_secret(
        synthetic_secret("system_config_migrations-credential-03"),
        context="auth.auth_secret",
        allow_key_creation=False,
        runtime_root=runtime_root,
    )
    encrypted_profile = encrypt_secret(
        synthetic_secret("system_config_migrations-credential-09"),
        context="llm_profiles.profiles.api_key",
        allow_key_creation=False,
        runtime_root=runtime_root,
    )
    _write_document(
        runtime_root,
        {
            "llm": {"api_key": synthetic_secret("system_config_migrations-credential-05")},
            "llm_profiles": {"profiles": [_profile("main", api_key=encrypted_profile)]},
            "auth": {
                "auth_secret": encrypted_auth,
                "auth_server_admin_token": synthetic_secret("system_config_migrations-credential-01"),
            },
            "web_search": {"api_key": synthetic_secret("system_config_migrations-credential-12")},
        },
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 0
    assert report["counts"]["plaintext_secrets"] == 3
    assert report["counts"]["encrypted_secrets"] == 2
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    expected = {
        ("llm", "api_key"): (synthetic_secret("system_config_migrations-credential-05"), "llm.api_key"),
        ("auth", "auth_secret"): (synthetic_secret("system_config_migrations-credential-03"), "auth.auth_secret"),
        ("auth", "auth_server_admin_token"): (
            synthetic_secret("system_config_migrations-credential-01"),
            "auth.auth_server_admin_token",
        ),
        ("web_search", "api_key"): (synthetic_secret("system_config_migrations-credential-12"), "web_search.api_key"),
    }
    for (section, field), (plaintext, context) in expected.items():
        assert (
            decrypt_secret(
                migrated[section][field],
                context=context,
                allow_key_creation=False,
                runtime_root=runtime_root,
            )
            == plaintext
        )


@pytest.mark.parametrize(
    ("key_value", "expected_code"),
    [
        (None, "SYSTEM_CONFIG_SECRET_KEY_MISSING"),
        ("not-base64", "SYSTEM_CONFIG_SECRET_KEY_INVALID"),
    ],
)
def test_missing_or_invalid_key_blocks_without_creating_key_or_writing(
    tmp_path,
    monkeypatch,
    key_value,
    expected_code,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    if key_value is not None:
        monkeypatch.setenv("SECRET_ENCRYPTION_KEY", key_value)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert expected_code in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original
    assert not (runtime_root / "config" / "secret.key").exists()


def test_symlink_key_is_invalid_and_never_followed(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    external_key = tmp_path / "external-secret.key"
    external_key.write_text(SECRET_TEST_KEY, encoding="utf-8")
    key_path = runtime_root / "config" / "secret.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.symlink_to(external_key)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_SECRET_KEY_INVALID" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original
    assert key_path.is_symlink()
    assert external_key.read_text(encoding="utf-8") == SECRET_TEST_KEY


def test_ciphertext_context_mismatch_blocks_without_writing(tmp_path, monkeypatch):
    from utils.security.secrets import encrypt_secret

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    wrong_context = encrypt_secret(
        "context-secret",
        context="wrong.context",
        allow_key_creation=False,
        runtime_root=runtime_root,
    )
    original = _write_document(runtime_root, {"llm": {"api_key": wrong_context}})

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_SECRET_DECRYPT_FAILED" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_encryption_failure_is_detected_before_backup_or_overwrite(tmp_path, monkeypatch):
    import migrations.system_config_v1_support as support
    from utils.security.secrets import SecretEncryptionError

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )

    def fail_encryption(*_args, **_kwargs):
        raise SecretEncryptionError("sensitive provider detail")

    monkeypatch.setattr(support, "encrypt_secret", fail_encryption)

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_SECRET_ENCRYPTION_FAILED" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original
    assert report["backup_paths"] == []
    assert "sensitive provider detail" not in json.dumps(report, ensure_ascii=False)


def test_backup_failure_preserves_original(tmp_path, monkeypatch):
    import migrations.system_config_v1_support as support

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )
    monkeypatch.setattr(
        support,
        "_create_verified_backup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("backup denied")),
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_BACKUP_FAILED" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_backup_verification_failure_preserves_original(tmp_path, monkeypatch):
    import migrations.system_config_v1_support as support

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )
    monkeypatch.setattr(
        support,
        "_ensure_private_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mode check denied")),
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_BACKUP_FAILED" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == original


def test_source_change_after_backup_is_reported_as_target_conflict(
    tmp_path,
    monkeypatch,
):
    import migrations.system_config_v1_support as support

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )
    replacement = json.dumps({"external_change": True}).encode()
    real_create_backup = internal(support).create_verified_backup

    def create_backup_then_change_source(source_path, source_bytes, base_path):
        result = real_create_backup(source_path, source_bytes, base_path)
        source_path.write_bytes(replacement)
        return result

    monkeypatch.setattr(
        support,
        "_create_verified_backup",
        create_backup_then_change_source,
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_TARGET_CONFLICT" in report["error_codes"]
    assert _config_path(runtime_root).read_bytes() == replacement
    assert Path(report["backup_paths"][0]).read_bytes() == original


def test_atomic_replace_failure_preserves_original_and_verified_backup(
    tmp_path,
    monkeypatch,
):
    import migrations.system_config_v1_support as support

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    config_path = _config_path(runtime_root)
    original = _write_document(
        runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}}
    )
    real_replace = support.os.replace

    def fail_target_replace(source, target):
        if Path(target) == config_path:
            raise OSError("replace denied")
        return real_replace(source, target)

    monkeypatch.setattr(support.os, "replace", fail_target_replace)

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_ATOMIC_WRITE_FAILED" in report["error_codes"]
    assert config_path.read_bytes() == original
    assert len(report["backup_paths"]) == 1
    assert Path(report["backup_paths"][0]).read_bytes() == original


def test_marker_write_failure_blocks_then_retry_completes_marker(
    tmp_path,
    monkeypatch,
):
    import migrations.system_config_v1_support as support

    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_document(runtime_root, {"llm": {"api_key": synthetic_secret("system_config_migrations-credential-08")}})
    real_write_marker = internal(support).write_marker
    monkeypatch.setattr(
        support,
        "_write_marker",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("marker denied")),
    )

    code, report = _execute_secret(runtime_root, dry_run=False)

    assert code == 2
    assert "SYSTEM_CONFIG_MIGRATION_MARKER_WRITE_FAILED" in report["error_codes"]
    migrated_bytes = _config_path(runtime_root).read_bytes()
    assert b"plaintext-secret" not in migrated_bytes

    monkeypatch.setattr(support, "_write_marker", real_write_marker)
    retry_code, retry_report = _execute_secret(runtime_root, dry_run=False)

    assert retry_code == 0
    assert retry_report["status"] == "completed-after-retry"
    assert _config_path(runtime_root).read_bytes() == migrated_bytes


def test_model_success_secret_failure_is_retained_and_retryable(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch, with_key=False)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
            }
        },
    )

    model_code, _ = _execute_model(runtime_root, dry_run=False)
    after_model = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=False)

    assert model_code == 0
    assert "task_llm" not in after_model
    assert secret_code == 2
    assert "SYSTEM_CONFIG_SECRET_KEY_MISSING" in secret_report["error_codes"]
    assert json.loads(_config_path(runtime_root).read_text(encoding="utf-8")) == after_model
    assert not (runtime_root / "config" / "secret.key").exists()

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    retry_code, retry_report = _execute_secret(runtime_root, dry_run=False)
    assert retry_code == 0
    assert retry_report["status"] == "applied"


def test_marker_does_not_hide_reintroduced_legacy_or_plaintext_data(
    tmp_path,
    monkeypatch,
):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
            }
        },
    )
    assert _execute_model(runtime_root, dry_run=False)[0] == 0
    assert _execute_secret(runtime_root, dry_run=False)[0] == 0
    damaged = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    damaged["task_llm"] = {
        "base_url": "https://conflict.example.net/v1",
        "model": "conflict-model",
    }
    damaged.setdefault("llm", {})["api_key"] = synthetic_secret("system_config_migrations-credential-10")
    _write_document(runtime_root, damaged)

    model_code, model_report = _execute_model(runtime_root, dry_run=True)
    secret_code, secret_report = _execute_secret(runtime_root, dry_run=True)

    assert model_code == 2
    assert "SYSTEM_CONFIG_TASK_PROFILE_CONFLICT" in model_report["error_codes"]
    assert secret_code == 2
    assert secret_report["error_codes"]


def test_concurrent_startup_serializes_and_applies_each_step_once(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    _write_document(
        runtime_root,
        {
            "task_llm": {
                "base_url": "https://task.example.net/v1",
                "model": "task-model",
                "api_key": synthetic_secret("system_config_migrations-credential-13"),
            },
            "auth": {"auth_secret": synthetic_secret("system_config_migrations-credential-03")},
        },
    )

    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_run_startup_in_process, runtime_root, SECRET_TEST_KEY) for _ in range(2)]
        reports = [future.result(timeout=30) for future in futures]

    assert all(report["ok"] for report in reports)
    migrated = json.loads(_config_path(runtime_root).read_text(encoding="utf-8"))
    assert "task_llm" not in migrated
    assert migrated["auth"]["auth_secret"].startswith("enc:v1:")
    backups = list(_config_path(runtime_root).parent.glob("system_config.json.*.bak*"))
    assert len(backups) == 2


def test_reports_are_structured_and_redacted(tmp_path, monkeypatch):
    runtime_root = _configure_runtime(tmp_path, monkeypatch)
    secret = synthetic_secret("system_config_migrations-credential-04")
    _write_document(runtime_root, {"llm": {"api_key": secret}})

    dry_code, dry_report = _execute_secret(runtime_root, dry_run=True)
    apply_code, apply_report = _execute_secret(runtime_root, dry_run=False)
    rendered = json.dumps([dry_report, apply_report], ensure_ascii=False)

    assert dry_code == apply_code == 0
    assert secret not in rendered
    assert "enc:v1:" not in rendered
    assert "sha256" not in rendered.lower()
    for report in (dry_report, apply_report):
        assert report["migration_id"] == "system-config-secret-encryption-v1"
        assert isinstance(report["counts"], dict)
        assert isinstance(report["backup_paths"], list)
        assert isinstance(report["error_codes"], list)
        assert isinstance(report["failed_object_ids"], list)
