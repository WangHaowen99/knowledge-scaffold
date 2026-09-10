from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parents[1]
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

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
    legacy_env_value = (
        os.getenv("AGENT_CONTEXT_PRUNE_TOOLS", "") if LEGACY_ENV_LINE not in cleaned_env else "unexpected"
    )
    legacy_prune_tools = {item.strip() for item in (legacy_json_value or legacy_env_value).split(",") if item.strip()}
    assert legacy_prune_tools == set()


def test_release_migration_entrypoint_invokes_system_config_cleanup():
    migrate_shell = (DEPLOY_DIR / "migrate.sh").read_text(encoding="utf-8")
    assert "python /deploy-tools/migrate_system_config.py" in migrate_shell
    assert '"${RUNTIME_STORAGE_SOURCE}:/release/.onto-runtime:${DATA_ACCESS}"' in migrate_shell
    assert '"${AUTH_STORAGE_SOURCE}:/release/.onto-auth:${DATA_ACCESS}"' in migrate_shell
    assert "SYSTEM_CONFIG_ARGS+=(--dry-run)" in migrate_shell


def test_release_backend_migration_uses_writable_log_dir_in_read_only_sandbox():
    migrate_shell = (DEPLOY_DIR / "migrate.sh").read_text(encoding="utf-8")

    assert "LOG_DIR=/tmp/logs" in migrate_shell
