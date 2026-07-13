"""Migration helpers for moving observability config to workspace scope."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from config import system_config as system_config_module
from utils.security.secrets import decrypt_secret
from utils.logging import LOG_DIR
from workspace.workspaces import list_workspaces
from integration.observability.workspace_config import (
    DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG,
    get_workspace_observability_config_path,
    write_workspace_observability_config,
)


GLOBAL_LANGFUSE_SECRET_CONTEXT = "observability.langfuse.secret_key"


def migrate_global_observability_to_workspaces(
    *,
    log_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Move saved global observability config into each workspace.

    Workspace config writes happen before the global section is removed. If any
    workspace write fails, the global config remains as the retry source of
    truth. Old perf log cleanup is best-effort and independent from config
    migration success.
    """
    with system_config_module._CONFIG_LOCK:
        document = _read_system_config_document()
    global_observability = document.get("observability")
    workspace_ids: list[str] = []
    errors: list[str] = []
    migrated = False

    if isinstance(global_observability, dict):
        try:
            workspace_config = _workspace_config_from_global(global_observability)
        except Exception as exc:
            errors.append(f"build workspace observability config failed: {exc}")
        else:
            workspace_ids, write_errors = _write_missing_workspace_configs(workspace_config)
            errors.extend(write_errors)
            if not write_errors:
                with system_config_module._CONFIG_LOCK:
                    latest_document = _read_system_config_document()
                    if isinstance(latest_document.get("observability"), dict):
                        latest_document.pop("observability", None)
                        try:
                            _write_system_config_document(latest_document)
                        except Exception as exc:
                            errors.append(f"remove global observability config failed: {exc}")
                        else:
                            migrated = True

    deleted_logs, delete_errors = _delete_old_agent_perf_logs(_resolve_old_log_dir(log_dir))
    errors.extend(delete_errors)
    return {
        "migrated": migrated,
        "workspace_ids": workspace_ids,
        "deleted_perf_logs": deleted_logs,
        "errors": errors,
    }


def _read_system_config_document() -> dict[str, Any]:
    config_path = Path(system_config_module.CONFIG_PATH)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_system_config_document(document: dict[str, Any]) -> None:
    config_path = Path(system_config_module.CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_missing_workspace_configs(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    migrated_ids: list[str] = []
    errors: list[str] = []
    try:
        workspaces = list_workspaces()
    except Exception as exc:
        return migrated_ids, [f"list workspaces failed: {exc}"]

    for workspace in workspaces:
        workspace_id = str(workspace.get("id") or "").strip()
        if not workspace_id:
            continue
        try:
            if get_workspace_observability_config_path(workspace_id).exists():
                continue
            write_workspace_observability_config(workspace_id, config)
        except Exception as exc:
            errors.append(f"write workspace observability config failed: workspace_id={workspace_id}: {exc}")
            continue
        migrated_ids.append(workspace_id)
    return migrated_ids, errors


def _workspace_config_from_global(global_observability: dict[str, Any]) -> dict[str, Any]:
    config = copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)

    if "enabled" in global_observability:
        config["enabled"] = global_observability.get("enabled")
    if "mode" in global_observability:
        config["mode"] = global_observability.get("mode")

    raw_local = global_observability.get("local")
    if isinstance(raw_local, dict):
        for key in config["local"]:
            if key in raw_local:
                config["local"][key] = raw_local[key]

    raw_langfuse = global_observability.get("langfuse")
    if isinstance(raw_langfuse, dict):
        for key in config["langfuse"]:
            if key in raw_langfuse:
                config["langfuse"][key] = raw_langfuse[key]
        config["langfuse"]["secret_key"] = decrypt_secret(
            str(config["langfuse"].get("secret_key") or ""),
            context=GLOBAL_LANGFUSE_SECRET_CONTEXT,
        )

    return config


def _resolve_old_log_dir(log_dir: str | os.PathLike[str] | None) -> Path:
    if log_dir is not None:
        return Path(log_dir)
    configured = os.getenv("LOG_DIR")
    if configured:
        return Path(configured)
    return Path(LOG_DIR)


def _delete_old_agent_perf_logs(log_dir: Path) -> tuple[int, list[str]]:
    if not log_dir.exists() or not log_dir.is_dir():
        return 0, []

    deleted = 0
    errors: list[str] = []
    for path in log_dir.glob("agent_perf-*"):
        if not path.is_file():
            continue
        try:
            path.unlink()
        except Exception as exc:
            errors.append(f"delete old perf log failed: path={path}: {exc}")
            continue
        deleted += 1
    return deleted, errors
