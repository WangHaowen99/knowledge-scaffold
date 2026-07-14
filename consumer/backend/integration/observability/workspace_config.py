"""Workspace-scoped observability configuration."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from utils.security.secrets import decrypt_secret, encrypt_secret
from workspace.workspaces import WorkspaceNotFoundError, get_workspace_paths, validate_workspace_id, workspace_exists


DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "local",
    "local": {
        "retention_days": 30,
    },
    "langfuse": {
        "public_key": "",
        "secret_key": "",
        "base_url": "",
        "debug": False,
    },
}

_TOP_LEVEL_KEYS = set(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG.keys())
_LOCAL_KEYS = set(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["local"].keys())
_LANGFUSE_KEYS = set(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["langfuse"].keys())


def get_workspace_observability_config_path(workspace_id: str) -> Path:
    resolved_id = _require_workspace(workspace_id)
    return get_workspace_paths(resolved_id).workspace_dir / "observability_config.json"


def get_workspace_observability_log_dir(workspace_id: str) -> Path:
    resolved_id = _require_workspace(workspace_id)
    return get_workspace_paths(resolved_id).runtime_dir / "observability"


def get_workspace_observability_config(workspace_id: str) -> dict[str, Any]:
    resolved_id = _require_workspace(workspace_id)
    config_path = get_workspace_observability_config_path(resolved_id)
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)

    merged = copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    _deep_update(merged, _normalize_observability_patch(raw, strict=False))
    merged["langfuse"]["secret_key"] = decrypt_secret(
        str(merged.get("langfuse", {}).get("secret_key") or ""),
        context=workspace_langfuse_secret_context(resolved_id),
    )
    return merged


def get_public_workspace_observability_config(workspace_id: str) -> dict[str, Any]:
    config = get_workspace_observability_config(workspace_id)
    langfuse = dict(config.get("langfuse") or {})
    secret = str(langfuse.pop("secret_key", "") or "")
    langfuse["has_secret_key"] = bool(secret)
    return {
        **config,
        "langfuse": langfuse,
    }


def update_workspace_observability_config(workspace_id: str, data: dict[str, Any]) -> dict[str, Any]:
    resolved_id = _require_workspace(workspace_id)
    current = build_workspace_observability_config(resolved_id, data)
    _write_workspace_observability_config(resolved_id, current)
    return current


def build_workspace_observability_config(
    workspace_id: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_id = _require_workspace(workspace_id)
    current = get_workspace_observability_config(resolved_id)
    if data is None:
        return current
    normalized = _normalize_observability_patch(data, strict=True)
    _deep_update(current, normalized)
    return current


def test_workspace_observability_connection(
    workspace_id: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    config = build_workspace_observability_config(workspace_id, data or {})
    if not bool(config.get("enabled")):
        return {"success": True, "message": "工作空间观测已关闭", "tested_at": tested_at}

    mode = str(config.get("mode") or "local").strip().lower()
    if mode == "local":
        return {"success": True, "message": "本地观测日志已启用", "tested_at": tested_at}
    if mode != "langfuse":
        return {"success": False, "message": f"不支持的观测 mode: {mode}", "tested_at": tested_at}

    langfuse_config = config.get("langfuse") if isinstance(config.get("langfuse"), dict) else {}
    missing = [
        name
        for name in ("public_key", "secret_key", "base_url")
        if not str(langfuse_config.get(name) or "").strip()
    ]
    if missing:
        return {
            "success": False,
            "message": "Langfuse 配置缺少字段: {}".format(", ".join(missing)),
            "tested_at": tested_at,
        }

    base_url = str(langfuse_config.get("base_url") or "").strip().rstrip("/")
    public_key = str(langfuse_config.get("public_key") or "").strip()
    secret_key = str(langfuse_config.get("secret_key") or "").strip()
    try:
        response = requests.get(
            "{}/api/public/projects".format(base_url),
            auth=(public_key, secret_key),
            timeout=10,
        )
        response.raise_for_status()
        return {"success": True, "message": "Langfuse 连接成功", "tested_at": tested_at}
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        detail = ""
        try:
            body = exc.response.json() if exc.response is not None else {}
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or "").strip()
        except Exception:
            detail = str(getattr(exc.response, "text", "") or "").strip()
        message = "Langfuse 连接失败（HTTP {}）".format(status_code)
        if detail:
            message = "{}：{}".format(message, detail[:200])
        return {"success": False, "message": message, "tested_at": tested_at}
    except requests.exceptions.RequestException as exc:
        return {"success": False, "message": "Langfuse 请求失败：{}".format(str(exc)), "tested_at": tested_at}


def write_workspace_observability_config(workspace_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """Write a complete effective config for migrations and tests."""
    resolved_id = _require_workspace(workspace_id)
    normalized = copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    _deep_update(normalized, _normalize_observability_patch(config, strict=True))
    _write_workspace_observability_config(resolved_id, normalized)
    return normalized


def workspace_langfuse_secret_context(workspace_id: str) -> str:
    resolved_id = validate_workspace_id(workspace_id)
    return f"workspace_observability.{resolved_id}.langfuse.secret_key"


def _write_workspace_observability_config(workspace_id: str, config: dict[str, Any]) -> None:
    config_path = get_workspace_observability_config_path(workspace_id)
    raw = copy.deepcopy(config)
    raw["langfuse"]["secret_key"] = encrypt_secret(
        str(raw.get("langfuse", {}).get("secret_key") or ""),
        context=workspace_langfuse_secret_context(workspace_id),
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_observability_patch(data: dict[str, Any], *, strict: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("observability config patch must be an object")

    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if key not in _TOP_LEVEL_KEYS:
            if strict:
                raise ValueError(f"Unknown observability config key: {key}")
            continue

        if key == "enabled":
            normalized[key] = _coerce_bool(value, "enabled")
        elif key == "mode":
            mode = str(value or "").strip().lower()
            if mode not in {"local", "langfuse"}:
                raise ValueError("mode must be local or langfuse")
            normalized[key] = mode
        elif key == "local":
            normalized[key] = _normalize_local(value, strict=strict)
        elif key == "langfuse":
            normalized[key] = _normalize_langfuse(value, strict=strict)
    return normalized


def _normalize_local(value: Any, *, strict: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("local must be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _LOCAL_KEYS:
            if strict:
                raise ValueError(f"Unknown observability config key: local.{key}")
            continue
        if key == "retention_days":
            retention_days = _coerce_int(item, "local.retention_days")
            if retention_days < 1:
                raise ValueError("local.retention_days must be greater than or equal to 1")
            normalized[key] = retention_days
    return normalized


def _normalize_langfuse(value: Any, *, strict: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("langfuse must be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _LANGFUSE_KEYS:
            if strict:
                raise ValueError(f"Unknown observability config key: langfuse.{key}")
            continue
        if key == "debug":
            normalized[key] = _coerce_bool(item, "langfuse.debug")
        else:
            normalized[key] = str(item or "").strip()
    return normalized


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _require_workspace(workspace_id: str) -> str:
    resolved_id = validate_workspace_id(workspace_id)
    if not workspace_exists(resolved_id):
        raise WorkspaceNotFoundError(resolved_id)
    return resolved_id


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
