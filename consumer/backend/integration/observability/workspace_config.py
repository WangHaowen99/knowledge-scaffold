"""Workspace-scoped observability configuration."""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from utils.security.secrets import (
    SECRET_ENCRYPTION_KEY_ENV,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
)
from workspace.workspaces import (
    WorkspaceNotFoundError,
    get_runtime_root,
    get_workspace_paths,
    validate_workspace_id,
    workspace_exists,
)


DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "local": {
        "retention_days": 30,
    },
    "speedscope": {
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
_SPEEDSCOPE_KEYS = set(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["speedscope"].keys())
_LANGFUSE_KEYS = set(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["langfuse"].keys())
_LEGACY_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS - {"speedscope"}
_MODE_KEY = "mode"
_LEGACY_MODE_TOP_LEVEL_KEYS = _LEGACY_TOP_LEVEL_KEYS | {_MODE_KEY}
_CANONICAL_MODE_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS | {_MODE_KEY}


class WorkspaceObservabilityMigrationRequiredError(ValueError):
    """Raised when a persisted workspace document is not canonical."""

    code = "WORKSPACE_OBSERVABILITY_MIGRATION_REQUIRED"

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = validate_workspace_id(workspace_id)
        self.detail = (
            f"工作空间 '{self.workspace_id}' 的观测配置不是规范格式，需要先完成迁移或人工修复"
        )
        super().__init__(self.detail)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
        }


def get_workspace_observability_config_path(workspace_id: str) -> Path:
    resolved_id = _require_workspace(workspace_id)
    return get_workspace_paths(resolved_id).workspace_dir / "observability_config.json"


def get_workspace_observability_log_dir(workspace_id: str) -> Path:
    resolved_id = _require_workspace(workspace_id)
    return get_workspace_paths(resolved_id).runtime_dir / "observability"


def get_workspace_observability_config(workspace_id: str) -> dict[str, Any]:
    resolved_id = _require_workspace(workspace_id)
    config_path = get_workspace_observability_config_path(resolved_id)
    if config_path.is_symlink():
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        return decode_workspace_observability_document(
            resolved_id,
            raw,
            runtime_root=config_path.parents[2],
        )
    except WorkspaceObservabilityMigrationRequiredError:
        raise
    except Exception as exc:
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id) from exc


def decode_workspace_observability_document(
    workspace_id: str,
    raw: Any,
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Validate and decrypt a canonical persisted document without writing."""
    resolved_id = validate_workspace_id(workspace_id)
    if not isinstance(raw, dict):
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)
    local = raw.get("local")
    speedscope = raw.get("speedscope")
    langfuse = raw.get("langfuse")
    raw_keys = set(raw)
    is_legacy_document = (
        raw_keys == _LEGACY_TOP_LEVEL_KEYS
        or raw_keys == _LEGACY_MODE_TOP_LEVEL_KEYS
    )
    if (
        (
            raw_keys != _TOP_LEVEL_KEYS
            and raw_keys != _CANONICAL_MODE_TOP_LEVEL_KEYS
            and not is_legacy_document
        )
        or not isinstance(local, dict)
        or set(local) != _LOCAL_KEYS
        or (speedscope is not None and not isinstance(speedscope, dict))
        or (isinstance(speedscope, dict) and set(speedscope) != _SPEEDSCOPE_KEYS)
        or not isinstance(langfuse, dict)
        or set(langfuse) != _LANGFUSE_KEYS
        or not isinstance(raw.get("enabled"), bool)
        or ("mode" in raw and raw.get("mode") not in {"local", "langfuse"})
    ):
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)
    retention_days = local.get("retention_days")
    speedscope_retention_days = (
        speedscope.get("retention_days")
        if isinstance(speedscope, dict)
        else DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["speedscope"]["retention_days"]
    )
    if (
        isinstance(retention_days, bool)
        or not isinstance(retention_days, int)
        or retention_days < 1
        or isinstance(speedscope_retention_days, bool)
        or not isinstance(speedscope_retention_days, int)
        or speedscope_retention_days < 1
        or not isinstance(langfuse.get("debug"), bool)
        or any(
            not isinstance(langfuse.get(field_name), str)
            for field_name in ("public_key", "secret_key", "base_url")
        )
    ):
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)

    result = copy.deepcopy(raw)
    result.pop("mode", None)
    if "speedscope" not in result:
        result["speedscope"] = copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG["speedscope"])
    raw_secret = str(result["langfuse"].get("secret_key") or "")
    if raw_secret and not is_encrypted_secret(raw_secret):
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)
    if raw_secret and not _secret_encryption_key_available(runtime_root):
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id)
    try:
        result["langfuse"]["secret_key"] = decrypt_secret(
            raw_secret,
            context=workspace_langfuse_secret_context(resolved_id),
        )
    except Exception as exc:
        raise WorkspaceObservabilityMigrationRequiredError(resolved_id) from exc
    return result


def _secret_encryption_key_available(runtime_root: Path | None = None) -> bool:
    if str(os.getenv(SECRET_ENCRYPTION_KEY_ENV) or "").strip():
        return True
    resolved_root = Path(runtime_root).resolve() if runtime_root else get_runtime_root()
    key_path = resolved_root / "config" / "secret.key"
    return key_path.is_file() and not key_path.is_symlink()


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

    langfuse_config = config.get("langfuse") if isinstance(config.get("langfuse"), dict) else {}
    langfuse_fields = {
        name: str(langfuse_config.get(name) or "").strip()
        for name in ("public_key", "secret_key", "base_url")
    }
    configured_langfuse = all(langfuse_fields.values())
    if not any(langfuse_fields.values()):
        return {"success": True, "message": "本地观测日志已启用", "tested_at": tested_at}
    if not configured_langfuse:
        missing = [name for name, value in langfuse_fields.items() if not value]
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
    _write_json_atomic(config_path, raw)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Persist secret-bearing workspace config with an atomic durable replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("observability config path must not be a symbolic link")
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(
                (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


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
        elif key == "local":
            normalized[key] = _normalize_local(value, strict=strict)
        elif key == "speedscope":
            normalized[key] = _normalize_speedscope(value, strict=strict)
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


def _normalize_speedscope(value: Any, *, strict: bool) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("speedscope must be an object")
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if key not in _SPEEDSCOPE_KEYS:
            if strict:
                raise ValueError(f"Unknown observability config key: speedscope.{key}")
            continue
        if key == "retention_days":
            retention_days = _coerce_int(item, "speedscope.retention_days")
            if retention_days < 1:
                raise ValueError("speedscope.retention_days must be greater than or equal to 1")
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
