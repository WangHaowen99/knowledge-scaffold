from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1]
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

import migrate_system_config as migration_module
from migrate_system_config import migrate


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_dry_run_reports_legacy_json_and_env_without_writing(tmp_path):
    config_path = tmp_path / ".onto-runtime" / "config" / "system_config.json"
    env_path = tmp_path / "backend" / ".env"
    _write_json(config_path, {"agent": {"context_max_rounds": 7, "context_prune_tools": "read_file"}})
    env_path.parent.mkdir(parents=True)
    env_path.write_text("KEEP=value\nAGENT_CONTEXT_PRUNE_TOOLS=read_file,list_files\n", encoding="utf-8")

    report = migrate(tmp_path, dry_run=True)

    assert report["changed"] is True
    assert report["system_config"]["removed"] is True
    assert report["backend_env"]["removed_count"] == 1
    assert "context_prune_tools" in config_path.read_text(encoding="utf-8")
    assert LEGACY_ENV_LINE in env_path.read_text(encoding="utf-8")


LEGACY_ENV_LINE = "AGENT_CONTEXT_PRUNE_TOOLS=read_file,list_files"


def test_apply_is_idempotent_and_preserves_other_configuration(tmp_path):
    config_path = tmp_path / ".onto-runtime" / "config" / "system_config.json"
    env_path = tmp_path / "backend" / ".env"
    original = {
        "agent": {"context_max_rounds": 7, "context_prune_tools": "read_file"},
        "llm": {"model": "model-a"},
    }
    _write_json(config_path, original)
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        "KEEP=value\n# AGENT_CONTEXT_PRUNE_TOOLS=documentation-only\n" + LEGACY_ENV_LINE + "\n",
        encoding="utf-8",
    )

    first = migrate(tmp_path, dry_run=False)
    first_config = json.loads(config_path.read_text(encoding="utf-8"))
    first_env = env_path.read_text(encoding="utf-8")
    second = migrate(tmp_path, dry_run=False)

    assert first["changed"] is True
    assert first_config == {"agent": {"context_max_rounds": 7}, "llm": {"model": "model-a"}}
    assert "KEEP=value" in first_env
    assert "# AGENT_CONTEXT_PRUNE_TOOLS=documentation-only" in first_env
    assert LEGACY_ENV_LINE not in first_env
    assert second["changed"] is False
    assert json.loads(config_path.read_text(encoding="utf-8")) == first_config
    assert env_path.read_text(encoding="utf-8") == first_env


def test_dry_run_reports_legacy_llm_budget_without_writing(tmp_path):
    env_path = tmp_path / "backend" / ".env"
    env_path.parent.mkdir(parents=True)
    original = "KEEP=value\nLLM_MAX_TOKENS=4096\n"
    env_path.write_text(original, encoding="utf-8")

    report = migrate(tmp_path, dry_run=True)

    assert report["changed"] is True
    assert report["backend_env"]["max_tokens_migrated_count"] == 1
    assert report["backend_env"]["max_tokens_from"] == 4096
    assert report["backend_env"]["max_tokens_to"] == 32768
    assert report["backend_env"]["max_tokens_planned_backup_path"].endswith(
        ".env.pre-model-profile-output-budget-v1.bak"
    )
    assert env_path.read_text(encoding="utf-8") == original


def test_apply_migrates_legacy_llm_budget_with_verified_backup(tmp_path):
    env_path = tmp_path / "backend" / ".env"
    env_path.parent.mkdir(parents=True)
    original = "KEEP=value\nexport LLM_MAX_TOKENS=\"8192\"\n"
    env_path.write_text(original, encoding="utf-8")
    original_bytes = env_path.read_bytes()

    report = migrate(tmp_path, dry_run=False)

    assert report["changed"] is True
    assert report["backend_env"]["max_tokens_migrated_count"] == 1
    assert env_path.read_text(encoding="utf-8") == (
        "KEEP=value\nexport LLM_MAX_TOKENS=\"32768\"\n"
    )
    backup_path = Path(report["backend_env"]["max_tokens_backup_path"])
    assert backup_path.read_bytes() == original_bytes

    repeated = migrate(tmp_path, dry_run=False)
    assert repeated["changed"] is False
    assert repeated["backend_env"]["max_tokens_migrated_count"] == 0


def test_apply_preserves_backend_env_owner_during_atomic_replacement(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / "backend" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")
    source_stat = env_path.stat(follow_symlinks=False)
    expected_owner = (source_stat.st_uid, source_stat.st_gid)
    replacements: list[tuple[Path, tuple[int, int] | None]] = []
    atomic_write_bytes = migration_module._atomic_write_bytes

    def record_atomic_replacement(
        path: Path,
        payload: bytes,
        *,
        mode: int = 0o600,
        owner: tuple[int, int] | None = None,
    ) -> None:
        replacements.append((path, owner))
        atomic_write_bytes(path, payload, mode=mode, owner=owner)

    monkeypatch.setattr(
        migration_module,
        "_atomic_write_bytes",
        record_atomic_replacement,
    )

    migrate(tmp_path, dry_run=False)

    assert (env_path, expected_owner) in replacements


def test_apply_can_place_env_backup_in_separate_writable_root(tmp_path):
    env_path = tmp_path / "backend" / ".env"
    backup_root = tmp_path / "migration-backups" / "system-config"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("LLM_MAX_TOKENS=4096\n", encoding="utf-8")

    report = migrate(tmp_path, dry_run=False, backup_root=backup_root)

    backup_path = Path(report["backend_env"]["max_tokens_backup_path"])
    assert backup_path.parent == backup_root
    assert backup_path.name == ".env.pre-model-profile-output-budget-v1.bak"
    assert backup_path.is_file()
    assert backup_path.read_text(encoding="utf-8") == "LLM_MAX_TOKENS=4096\n"


def test_custom_or_commented_llm_budget_is_not_rewritten(tmp_path):
    env_path = tmp_path / "backend" / ".env"
    env_path.parent.mkdir(parents=True)
    original = (
        "# LLM_MAX_TOKENS=4096\n"
        "LLM_MAX_TOKENS=16384 # intentional custom budget\n"
    )
    env_path.write_text(original, encoding="utf-8")

    report = migrate(tmp_path, dry_run=False)

    assert report["changed"] is False
    assert report["backend_env"]["max_tokens_migrated_count"] == 0
    assert env_path.read_text(encoding="utf-8") == original


def test_cleaned_files_leave_legacy_backend_with_empty_prune_set(tmp_path, monkeypatch):
    config_path = tmp_path / ".onto-runtime" / "config" / "system_config.json"
    env_path = tmp_path / "backend" / ".env"
    _write_json(config_path, {"agent": {"context_prune_tools": "read_file"}})
    env_path.parent.mkdir(parents=True)
    env_path.write_text(LEGACY_ENV_LINE + "\n", encoding="utf-8")

    migrate(tmp_path, dry_run=False)
    cleaned_config = json.loads(config_path.read_text(encoding="utf-8"))
    cleaned_env = env_path.read_text(encoding="utf-8")
    monkeypatch.delenv("AGENT_CONTEXT_PRUNE_TOOLS", raising=False)

    legacy_json_value = (cleaned_config.get("agent") or {}).get("context_prune_tools", "")
    legacy_env_value = os.getenv("AGENT_CONTEXT_PRUNE_TOOLS", "") if LEGACY_ENV_LINE not in cleaned_env else "unexpected"
    legacy_prune_tools = {
        item.strip()
        for item in (legacy_json_value or legacy_env_value).split(",")
        if item.strip()
    }
    assert legacy_prune_tools == set()


def test_release_migration_entrypoint_invokes_system_config_cleanup():
    migrate_shell = (DEPLOY_DIR / "migrate.sh").read_text(encoding="utf-8")
    assert "python /deploy-tools/migrate_system_config.py" in migrate_shell
    assert '"${RUNTIME_STORAGE_SOURCE}:/release/.onto-runtime:${DATA_ACCESS}"' in migrate_shell
    assert '"${AUTH_STORAGE_SOURCE}:/release/.onto-auth:${DATA_ACCESS}"' in migrate_shell
    assert 'SYSTEM_CONFIG_DOCKER_ARGS=("${MIGRATION_DOCKER_ARGS[@]}")' in migrate_shell
    assert 'docker run "${SYSTEM_CONFIG_DOCKER_ARGS[@]}"' in migrate_shell
    assert "--backup-root /migration-backups/system-config" in migrate_shell
    assert "SYSTEM_CONFIG_ARGS+=(--dry-run)" in migrate_shell
