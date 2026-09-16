# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import os
import uuid
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from urllib.parse import quote

import requests

from utils.security.secrets import decrypt_secret, encrypt_secret
from workspace.workspaces import get_runtime_root


PLATFORM_CONFIG_PATH_ENV = "ONTO_PLATFORM_CONFIG_PATH"
_PLATFORM_CONFIG_LOCK = RLock()
_API_KEY_SECRET_CONTEXT = "platform.api_key"


@dataclass(frozen=True)
class PlatformConfig:
    enabled: bool = False
    base_url: str = ""
    environment_id: str = ""
    instance_id: str = ""
    api_key: str = ""
    organization_id: str | None = None
    batch_size: int = 20
    timeout_seconds: float = 5.0
    heartbeat_interval_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "PlatformConfig":
        saved_config = load_platform_config()
        enabled = _config_bool(saved_config, "enabled", _env_bool("ONTO_PLATFORM_ENABLED", False))
        base_url = _config_text(saved_config, "base_url", _env_text("ONTO_PLATFORM_BASE_URL"))
        environment_id = _config_text(
            saved_config,
            "environment_id",
            _env_text("ONTO_PLATFORM_ENVIRONMENT_ID"),
        )
        api_key = _config_text(saved_config, "api_key", _env_text("ONTO_PLATFORM_API_KEY"))
        configured_instance_id = _config_text(
            saved_config,
            "instance_id",
            _env_text("ONTO_PLATFORM_INSTANCE_ID"),
        )
        instance_id = configured_instance_id
        requires_instance_id = (
            enabled
            and bool(base_url)
            and bool(environment_id)
            and bool(api_key)
            and not instance_id
        )
        if requires_instance_id:
            instance_id = _load_or_create_instance_id(_instance_id_file())

        return cls(
            enabled=enabled,
            base_url=base_url,
            environment_id=environment_id,
            instance_id=instance_id,
            api_key=api_key,
            organization_id=_config_text(
                saved_config,
                "organization_id",
                _env_text("ONTO_PLATFORM_ORGANIZATION_ID"),
            )
            or None,
            batch_size=max(1, _config_int(saved_config, "batch_size", _env_int("ONTO_PLATFORM_BATCH_SIZE", 20))),
            timeout_seconds=max(
                0.1,
                _config_float(
                    saved_config,
                    "timeout_seconds",
                    _env_float("ONTO_PLATFORM_TIMEOUT_SECONDS", 5.0),
                ),
            ),
            heartbeat_interval_seconds=max(
                0.01,
                _config_float(
                    saved_config,
                    "heartbeat_interval_seconds",
                    _env_float("ONTO_PLATFORM_HEARTBEAT_INTERVAL_SECONDS", 30.0),
                ),
            ),
        )

    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and self.base_url.strip()
            and self.environment_id.strip()
            and self.instance_id.strip()
            and self.api_key.strip()
        )


def load_platform_config() -> dict[str, Any]:
    with _PLATFORM_CONFIG_LOCK:
        path = _platform_config_path()
        if not path.exists():
            return {}
        document = _read_json_document(path)
        api_key = str(document.get("api_key") or "").strip()
        if api_key:
            document["api_key"] = decrypt_secret(api_key, context=_API_KEY_SECRET_CONTEXT)
        return document


def get_public_platform_config() -> dict[str, Any]:
    config = PlatformConfig.from_env()
    return _public_config(config)


def save_platform_config(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise PlatformConfigValidationError("配置必须是对象")
    with _PLATFORM_CONFIG_LOCK:
        current = load_platform_config()
        next_config = _merge_platform_config(current, data)
        _write_platform_config(next_config)
    return _public_config(PlatformConfig.from_env())


PlatformTestTransport = Callable[[str, dict[str, str], float], Any]


def test_platform_config_connection(
    data: dict[str, Any],
    *,
    transport: PlatformTestTransport | None = None,
) -> dict[str, Any]:
    tested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    try:
        config = build_platform_config(data)
    except PlatformConfigValidationError as exc:
        return {"success": False, "message": str(exc), "tested_at": tested_at}
    if not config.enabled:
        return {"success": True, "message": "Platform 接入未启用", "tested_at": tested_at}
    missing = _missing_required_fields(config)
    if missing:
        return {
            "success": False,
            "message": "缺少必填字段: {}".format("、".join(missing)),
            "tested_at": tested_at,
        }
    request = transport or _default_get
    try:
        request(
            _join_url(
                config.base_url,
                "/api/v1/runtime/policy-snapshot?runtime_id={}".format(
                    quote(config.instance_id, safe="")
                ),
            ),
            _environment_headers(config),
            config.timeout_seconds,
        )
    except requests.HTTPError as exc:
        status_code = getattr(exc.response, "status_code", None)
        if status_code == 404:
            return {
                "success": True,
                "message": "连接成功，当前实例暂无策略快照",
                "tested_at": tested_at,
            }
        if status_code in {401, 403}:
            return {"success": False, "message": "认证失败，请检查环境 Key", "tested_at": tested_at}
        return {
            "success": False,
            "message": "连接失败（HTTP {}）".format(status_code or "unknown"),
            "tested_at": tested_at,
        }
    except Exception as exc:
        return {"success": False, "message": "连接失败: {}".format(exc), "tested_at": tested_at}
    return {"success": True, "message": "连接成功", "tested_at": tested_at}


def build_platform_config(data: dict[str, Any] | None = None) -> PlatformConfig:
    base = PlatformConfig.from_env()
    if data is None:
        return base
    if not isinstance(data, dict):
        raise PlatformConfigValidationError("配置必须是对象")
    merged = _merge_platform_config(_config_to_dict(base), data)
    instance_id = str(merged["instance_id"] or "").strip()
    requires_instance_id = (
        merged["enabled"]
        and bool(merged["base_url"])
        and bool(merged["environment_id"])
        and bool(merged["api_key"])
        and not instance_id
    )
    if requires_instance_id:
        instance_id = _load_or_create_instance_id(_instance_id_file())
    return PlatformConfig(
        enabled=bool(merged["enabled"]),
        base_url=str(merged["base_url"] or "").strip(),
        environment_id=str(merged["environment_id"] or "").strip(),
        instance_id=instance_id,
        api_key=str(merged["api_key"] or "").strip(),
        organization_id=str(merged["organization_id"] or "").strip() or None,
        batch_size=max(1, int(merged["batch_size"])),
        timeout_seconds=max(0.1, float(merged["timeout_seconds"])),
        heartbeat_interval_seconds=max(0.01, float(merged["heartbeat_interval_seconds"])),
    )


class PlatformConfigValidationError(ValueError):
    pass


def _env_text(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _instance_id_file() -> Path:
    configured_path = _env_text("ONTO_PLATFORM_INSTANCE_ID_FILE")
    if configured_path:
        return Path(configured_path).expanduser()
    return get_runtime_root() / "platform" / "instance_id"


def _load_or_create_instance_id(path: Path) -> str:
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    instance_id = "inst_" + uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(instance_id, encoding="utf-8")
    return instance_id


def _platform_config_path() -> Path:
    configured_path = _env_text(PLATFORM_CONFIG_PATH_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return get_runtime_root() / "config" / "platform_config.json"


def _read_json_document(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(document, dict):
        return {}
    return document


def _write_platform_config(config: dict[str, Any]) -> None:
    document = dict(config)
    document["api_key"] = encrypt_secret(
        str(document.get("api_key") or ""),
        context=_API_KEY_SECRET_CONTEXT,
    )
    path = _platform_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_platform_config(current: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "enabled",
        "base_url",
        "environment_id",
        "instance_id",
        "api_key",
        "clear_api_key",
        "organization_id",
        "batch_size",
        "timeout_seconds",
        "heartbeat_interval_seconds",
    }
    bad_fields = sorted(set(data) - allowed_fields)
    if bad_fields:
        raise PlatformConfigValidationError("Platform 接入不支持字段: {}".format(", ".join(bad_fields)))
    merged = {
        "enabled": _config_bool(current, "enabled", False),
        "base_url": _config_text(current, "base_url", ""),
        "environment_id": _config_text(current, "environment_id", ""),
        "instance_id": _config_text(current, "instance_id", ""),
        "api_key": _config_text(current, "api_key", ""),
        "organization_id": _config_text(current, "organization_id", ""),
        "batch_size": _config_int(current, "batch_size", 20),
        "timeout_seconds": _config_float(current, "timeout_seconds", 5.0),
        "heartbeat_interval_seconds": _config_float(current, "heartbeat_interval_seconds", 30.0),
    }
    if "enabled" in data:
        merged["enabled"] = bool(data.get("enabled"))
    for field_name in ("base_url", "environment_id", "instance_id", "organization_id"):
        if field_name in data:
            merged[field_name] = str(data.get(field_name) or "").strip()
    if data.get("clear_api_key"):
        merged["api_key"] = ""
    elif "api_key" in data:
        next_api_key = str(data.get("api_key") or "").strip()
        if next_api_key:
            merged["api_key"] = next_api_key
    for field_name in ("batch_size",):
        if field_name in data:
            merged[field_name] = max(1, _coerce_int(data.get(field_name), field_name))
    for field_name in ("timeout_seconds", "heartbeat_interval_seconds"):
        if field_name in data:
            min_value = 0.01 if field_name == "heartbeat_interval_seconds" else 0.1
            merged[field_name] = max(min_value, _coerce_float(data.get(field_name), field_name))
    return merged


def _public_config(config: PlatformConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "environment_id": config.environment_id,
        "instance_id": config.instance_id,
        "organization_id": config.organization_id or "",
        "batch_size": config.batch_size,
        "timeout_seconds": config.timeout_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "has_api_key": bool(config.api_key),
        "available": config.available,
    }


def _config_to_dict(config: PlatformConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "base_url": config.base_url,
        "environment_id": config.environment_id,
        "instance_id": config.instance_id,
        "api_key": config.api_key,
        "organization_id": config.organization_id or "",
        "batch_size": config.batch_size,
        "timeout_seconds": config.timeout_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
    }


def _missing_required_fields(config: PlatformConfig) -> list[str]:
    missing = []
    if not config.base_url.strip():
        missing.append("Platform 地址")
    if not config.environment_id.strip():
        missing.append("环境标识")
    if not config.api_key.strip():
        missing.append("环境 Key")
    if not config.instance_id.strip():
        missing.append("实例标识")
    return missing


def _environment_headers(config: PlatformConfig) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Environment-Id": config.environment_id,
        "X-Environment-Api-Key": config.api_key,
    }


def _join_url(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _default_get(url: str, headers: dict[str, str], timeout: float):
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json() if response.content else {}


def _config_text(config: dict[str, Any], field_name: str, default: str) -> str:
    if field_name not in config:
        return default
    return str(config.get(field_name) or "").strip()


def _config_bool(config: dict[str, Any], field_name: str, default: bool) -> bool:
    if field_name not in config:
        return default
    value = config.get(field_name)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _config_int(config: dict[str, Any], field_name: str, default: int) -> int:
    if field_name not in config:
        return default
    try:
        return int(config.get(field_name))
    except (TypeError, ValueError):
        return default


def _config_float(config: dict[str, Any], field_name: str, default: float) -> float:
    if field_name not in config:
        return default
    try:
        return float(config.get(field_name))
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PlatformConfigValidationError(f"{field_name} 必须是整数") from exc


def _coerce_float(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PlatformConfigValidationError(f"{field_name} 必须是数字") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
