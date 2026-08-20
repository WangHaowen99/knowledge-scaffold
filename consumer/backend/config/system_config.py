"""Persistent system configuration for onto-agent.

This module provides a UI-editable config layer with the following precedence:
saved system config > environment variables > built-in defaults.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from threading import Lock
from typing import Any
from datetime import datetime, timezone

import requests

from config.ops_config import resolve_model_profile_max_count
from utils.security.secrets import (
    SecretEncryptionError,
    decrypt_secret,
    encrypt_secret,
    is_encrypted_secret,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def _resolve_default_runtime_root(backend_root: Path | None = None) -> Path:
    """Resolve the default runtime root for both repo and flat container layouts."""
    resolved_backend_root = (backend_root or BACKEND_ROOT).resolve()
    repo_root = resolved_backend_root.parent

    # In Docker the backend is copied directly into /app, so repo_root becomes /.
    # Use /app/.onto-runtime instead of accidentally falling back to /.onto-runtime.
    if repo_root == Path("/"):
        return resolved_backend_root / ".onto-runtime"

    return repo_root / ".onto-runtime"


ONTO_RUNTIME_ROOT = _resolve_default_runtime_root()
PROJECT_ROOT_ENV = "PROJECT_ROOT"
SYSTEM_CONFIG_PATH_ENV = "SYSTEM_CONFIG_PATH"


def _resolve_config_runtime_root() -> Path:
    configured_root = os.getenv(PROJECT_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser()
    return ONTO_RUNTIME_ROOT


DEFAULT_CONFIG_DIR = _resolve_config_runtime_root() / "config"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "system_config.json"


def _resolve_config_path() -> Path:
    configured_path = os.getenv(SYSTEM_CONFIG_PATH_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return DEFAULT_CONFIG_PATH


CONFIG_PATH = _resolve_config_path()
CONFIG_DIR = CONFIG_PATH.parent
_CONFIG_LOCK = Lock()

SECTION_KEYS = (
    "llm",
    "llm_profiles",
    "storage",
    "auth",
    "agent",
    "memory",
    "web_search",
    "direct_execution",
    "skill_evolution",
)
_LEGACY_IGNORED_TOP_LEVEL_KEYS = {"_status", "mindsdb", "nl2sql_http"}
_LLM_PROFILES_SECTION = "llm_profiles"
_DEFAULT_LEGACY_PROFILE_ID = "default"
_SUPPORTED_LLM_PROVIDERS = {"deepseek", "glm", "openai_compatible"}
_SUPPORTED_WEB_SEARCH_PROVIDERS = {"tavily"}
_SUPPORTED_WEB_SEARCH_DEPTHS = {"basic", "advanced", "fast", "ultra-fast"}

_SECTION_FIELDS: dict[str, dict[str, Any]] = {
    "llm": {
        "base_url": {"env": "LLM_BASE_URL", "type": "str", "default": ""},
        "model": {"env": "LLM_MODEL", "type": "str", "default": ""},
        "api_key": {"env": "LLM_API_KEY", "type": "secret", "default": ""},
        "timeout": {"env": "LLM_TIMEOUT", "type": "int", "default": 600},
        "max_tokens": {"env": "LLM_MAX_TOKENS", "type": "int", "default": 4096},
        "temperature": {"env": "LLM_TEMPERATURE", "type": "float", "default": 0.7},
    },
    "storage": {
        "chat_storage_dir": {"env": "CHAT_STORAGE_DIR", "type": "str", "default": "./data/chats"},
        "execution_storage_dir": {"env": "EXECUTION_STORAGE_DIR", "type": "str", "default": "./data/executions"},
        "project_root": {"env": "PROJECT_ROOT", "type": "str", "default": str(ONTO_RUNTIME_ROOT)},
        "auth_users_file": {"env": "AUTH_USERS_FILE", "type": "str", "default": str(ONTO_RUNTIME_ROOT / "users.json")},
    },
    "auth": {
        "auth_secret": {"env": "AUTH_SECRET", "type": "secret", "default": ""},
        "auth_token_ttl_seconds": {"env": "AUTH_TOKEN_TTL", "type": "int", "default": 43200},
        "auth_server_url": {"env": "AUTH_SERVER_URL", "type": "str", "default": ""},
        "auth_server_admin_token": {
            "env": "AUTH_SERVER_ADMIN_TOKEN",
            "type": "secret",
            "default": "",
        },
        "auth_server_audience": {
            "env": "AUTH_SERVER_AUDIENCE",
            "type": "str",
            "default": "meta-wise",
        },
        # 留空时 RemoteAuthBackend 使用 auth_server_url 作为 issuer。
        "auth_server_issuer": {"env": "AUTH_SERVER_ISSUER", "type": "str", "default": ""},
    },
    "agent": {
        "llm_connection_max_retries": {"env": "AGENT_LLM_CONNECTION_MAX_RETRIES", "type": "int", "default": 3},
        "main_agent_max_iterations": {"env": "AGENT_MAIN_LOOP_MAX_ITERATIONS", "type": "int", "default": 50},
        "sub_agent_max_iterations": {"env": "AGENT_SUB_AGENT_MAX_ITERATIONS", "type": "int", "default": 20},
        "sub_agent_max_concurrency": {"env": "AGENT_SUB_AGENT_MAX_CONCURRENCY", "type": "int", "default": 6},
        "sub_agent_max_depth": {"env": "AGENT_SUB_AGENT_MAX_DEPTH", "type": "int", "default":10},
        "parallel_default_max_workers": {"env": "AGENT_PARALLEL_DEFAULT_MAX_WORKERS", "type": "int", "default": 10},
        "tool_timeout_seconds": {"env": "AGENT_TOOL_TIMEOUT_SECONDS", "type": "int", "default": 600},
        "shutdown_total_deadline_seconds": {"env": "AGENTIC_SHUTDOWN_TOTAL_DEADLINE_SECONDS", "type": "float", "default": 30.0},
        "sse_subscriber_queue_size": {"env": "AGENTIC_SSE_SUBSCRIBER_QUEUE_SIZE", "type": "int", "default": 512},
        "sse_max_subscribers_per_run": {"env": "AGENTIC_SSE_MAX_SUBSCRIBERS_PER_RUN", "type": "int", "default": 3},
        "sse_max_subscribers_total": {"env": "AGENTIC_SSE_MAX_SUBSCRIBERS_TOTAL", "type": "int", "default": 100},
        "completed_replay_ttl_seconds": {"env": "AGENTIC_RUN_COMPLETED_REPLAY_TTL_SECONDS", "type": "float", "default": 120.0},
        "short_io_workers": {"env": "AGENTIC_SHORT_IO_WORKERS", "type": "int", "default": 8},
        "short_io_max_pending": {"env": "AGENTIC_SHORT_IO_MAX_PENDING", "type": "int", "default": 64},
        "short_io_capacity_wait_seconds": {"env": "AGENTIC_SHORT_IO_CAPACITY_WAIT_SECONDS", "type": "float", "default": 1.0},
        "short_io_operation_timeout_seconds": {"env": "AGENTIC_SHORT_IO_OPERATION_TIMEOUT_SECONDS", "type": "float", "default": 30.0},
        "external_io_workers": {"env": "AGENTIC_EXTERNAL_IO_WORKERS", "type": "int", "default": 16},
        "external_io_max_pending": {"env": "AGENTIC_EXTERNAL_IO_MAX_PENDING", "type": "int", "default": 16},
        "external_io_capacity_wait_seconds": {"env": "AGENTIC_EXTERNAL_IO_CAPACITY_WAIT_SECONDS", "type": "float", "default": 0.1},
        "external_io_operation_timeout_seconds": {"env": "AGENTIC_EXTERNAL_IO_OPERATION_TIMEOUT_SECONDS", "type": "float", "default": 300.0},
        "tool_io_workers": {"env": "AGENTIC_TOOL_IO_WORKERS", "type": "int", "default": 8},
        "tool_io_max_pending": {"env": "AGENTIC_TOOL_IO_MAX_PENDING", "type": "int", "default": 16},
        "tool_io_capacity_wait_seconds": {"env": "AGENTIC_TOOL_IO_CAPACITY_WAIT_SECONDS", "type": "float", "default": 0.1},
        "tool_io_operation_timeout_seconds": {"env": "AGENTIC_TOOL_IO_OPERATION_TIMEOUT_SECONDS", "type": "float", "default": 600.0},
        "context_max_rounds": {"env": "AGENT_CONTEXT_MAX_ROUNDS", "type": "int", "default": 10},
        "runtime_warmup_enabled": {"env": "AGENT_RUNTIME_WARMUP_ENABLED", "type": "bool", "default": True},
        # Operation evolution thresholds
        "evolution_min_occurrences": {"env": "EVO_MIN_OCCURRENCES", "type": "int", "default": 3},
        "evolution_min_applied": {"env": "EVO_MIN_APPLIED", "type": "int", "default": 2},
        "evolution_max_rejection": {"env": "EVO_MAX_REJECTION", "type": "int", "default": 0},
        "evolution_passive_reflection_enabled": {"env": "EVO_PASSIVE_REFLECTION_ENABLED", "type": "bool", "default": False},
        "evolution_patch_auto_apply": {"env": "EVO_PATCH_AUTO_APPLY", "type": "bool", "default": False},
        "evolution_patch_cooldown_sec": {"env": "EVO_PATCH_COOLDOWN_SEC", "type": "int", "default": 3600},
        "evolution_sweep_interval_sec": {"env": "EVO_SWEEP_INTERVAL_SEC", "type": "int", "default": 1800},
        "execute_script_timeout": {"env": "EXECUTE_SCRIPT_TIMEOUT", "type": "int", "default": 300},
    },
    "memory": {
        "recall_topk": {"env": "AGENT_MEMORY_RECALL_TOPK", "type": "int", "default": 3},
        "recall_threshold": {"env": "AGENT_MEMORY_RECALL_THRESHOLD", "type": "str", "default": ""},
        "workspace_root": {"env": "MEMORY_WORKSPACE_ROOT", "type": "str", "default": ""},
    },
    "web_search": {
        "enabled": {"env": "WEB_SEARCH_ENABLED", "type": "bool", "default": False},
        "provider": {"env": "WEB_SEARCH_PROVIDER", "type": "str", "default": "tavily"},
        "base_url": {"env": "WEB_SEARCH_BASE_URL", "type": "str", "default": "https://api.tavily.com"},
        "api_key": {"env": "WEB_SEARCH_API_KEY", "type": "secret", "default": ""},
        "timeout_seconds": {"env": "WEB_SEARCH_TIMEOUT_SECONDS", "type": "int", "default": 20},
        "default_max_results": {"env": "WEB_SEARCH_DEFAULT_MAX_RESULTS", "type": "int", "default": 5},
        "max_results_limit": {"env": "WEB_SEARCH_MAX_RESULTS_LIMIT", "type": "int", "default": 10},
        "search_depth": {"env": "WEB_SEARCH_DEPTH", "type": "str", "default": "basic"},
        "allowed_domains": {"env": "WEB_SEARCH_ALLOWED_DOMAINS", "type": "str", "default": ""},
        "blocked_domains": {"env": "WEB_SEARCH_BLOCKED_DOMAINS", "type": "str", "default": ""},
    },
    "direct_execution": {
        "enabled": {"env": "DIRECT_EXECUTION_ENABLED", "type": "bool", "default": False},
        "selected_ontology_only": {"env": "DIRECT_EXECUTION_SELECTED_ONTOLOGY_ONLY", "type": "bool", "default": True},
    },
    "skill_evolution": {
        "enabled": {"env": "SKILL_EVOLUTION_ENABLED", "type": "bool", "default": False},
        "base_url": {"env": "SKILL_EVOLVER_BASE_URL", "type": "str", "default": "http://127.0.0.1:8765"},
        "timeout_seconds": {"env": "SKILL_EVOLVER_TIMEOUT_SECONDS", "type": "int", "default": 30},
        "poll_interval_seconds": {"env": "SKILL_EVOLUTION_POLL_INTERVAL_SECONDS", "type": "int", "default": 10},
        "task_timeout_seconds": {"env": "SKILL_EVOLUTION_TASK_TIMEOUT_SECONDS", "type": "int", "default": 1800},
        "max_iterations": {"env": "SKILL_EVOLUTION_MAX_ITERATIONS", "type": "int", "default": 15},
    },
}

_CLEAR_FIELDS = {
    "llm": {"clear_api_key": "api_key"},
    "auth": {"clear_auth_secret": "auth_secret"},
    "web_search": {"clear_api_key": "api_key"},
}


_LLM_PROFILE_DEFAULTS: dict[str, Any] = {
    "id": _DEFAULT_LEGACY_PROFILE_ID,
    "name": "默认模型",
    "provider": "openai_compatible",
    "base_url": "",
    "model": "",
    "api_key": "",
    "timeout": 600,
    "max_tokens": 4096,
    "temperature": 0.7,
    "enabled": True,
}


class SystemConfigValidationError(ValueError):
    """Raised when system configuration payload is invalid."""


class SystemConfigReadError(RuntimeError):
    """Sanitized persisted-config error that must never trigger fallback."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def _coerce_string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _coerce_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _coerce_string(value).lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise SystemConfigValidationError(f"{field_name} 必须是布尔值")


def _coerce_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise SystemConfigValidationError(f"{field_name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SystemConfigValidationError(f"{field_name} 必须是整数") from exc


def _coerce_float(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise SystemConfigValidationError(f"{field_name} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SystemConfigValidationError(f"{field_name} 必须是数字") from exc


def _normalize_profile_id(value: Any, *, field_name: str = "profile_id", allow_colon: bool = True) -> str:
    profile_id = _coerce_string(value)
    if not profile_id:
        raise SystemConfigValidationError(f"{field_name} 不能为空")
    if any(ch.isspace() for ch in profile_id):
        raise SystemConfigValidationError(f"{field_name} 不能包含空白字符")
    if not allow_colon and ":" in profile_id:
        raise SystemConfigValidationError(f"{field_name} 不能包含冒号")
    if len(profile_id) > 80:
        raise SystemConfigValidationError(f"{field_name} 长度不能超过 80")
    return profile_id


def _generate_profile_id(prefix: str = "model") -> str:
    return _normalize_profile_id(f"{prefix}_{uuid.uuid4().hex[:12]}")


def _coerce_llm_provider(value: Any) -> str:
    provider = _coerce_string(value).lower() or "openai_compatible"
    if provider not in _SUPPORTED_LLM_PROVIDERS:
        raise SystemConfigValidationError(
            "provider 仅支持 {}".format(" | ".join(sorted(_SUPPORTED_LLM_PROVIDERS)))
        )
    return provider


def _read_json_document(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise SystemConfigReadError(
            "SYSTEM_CONFIG_INVALID",
            "系统配置文件无效，请修复配置后重试",
        )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            mode=0o600,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("system config is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            raw = json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemConfigReadError(
            "SYSTEM_CONFIG_INVALID",
            "系统配置文件无效，请修复配置后重试",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(raw, dict):
        raise SystemConfigReadError(
            "SYSTEM_CONFIG_INVALID",
            "系统配置文件必须是 JSON object",
        )
    return raw


def _is_plaintext_secret_value(value: Any) -> bool:
    secret_value = _coerce_string(value)
    return bool(secret_value) and not is_encrypted_secret(secret_value)


def _has_plaintext_secret_values_recursive(
    specs: dict[str, Any],
    values: dict[str, Any],
) -> bool:
    for field_name, field_spec in specs.items():
        if isinstance(field_spec, dict) and "type" not in field_spec:
            nested = values.get(field_name)
            if isinstance(nested, dict) and _has_plaintext_secret_values_recursive(field_spec, nested):
                return True
            continue
        if field_spec.get("type") == "secret" and field_name in values:
            if _is_plaintext_secret_value(values.get(field_name)):
                return True
    return False


def _llm_profiles_have_plaintext_secrets(section: dict[str, Any]) -> bool:
    profiles = section.get("profiles")
    if not isinstance(profiles, list):
        return False
    for profile in profiles:
        if isinstance(profile, dict) and "api_key" in profile:
            if _is_plaintext_secret_value(profile.get("api_key")):
                return True
    return False


def _document_has_plaintext_secrets(document: dict[str, Any]) -> bool:
    for section in SECTION_KEYS:
        section_config = document.get(section)
        if not isinstance(section_config, dict):
            continue
        if section == _LLM_PROFILES_SECTION:
            if _llm_profiles_have_plaintext_secrets(section_config):
                return True
            continue
        if _has_plaintext_secret_values_recursive(_SECTION_FIELDS[section], section_config):
            return True
    return False


def _document_has_legacy_model_fields(document: dict[str, Any]) -> bool:
    if "task_llm" in document:
        return True
    profiles_section = document.get(_LLM_PROFILES_SECTION)
    if isinstance(profiles_section, dict) and "default_profile_id" in profiles_section:
        return True
    return False


def _validate_persisted_field_type(field_spec: dict[str, Any], value: Any) -> bool:
    field_type = field_spec.get("type")
    if field_type in {"str", "secret"}:
        return isinstance(value, str)
    if field_type == "bool":
        return isinstance(value, bool)
    if field_type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _validate_persisted_fields(
    specs: dict[str, Any],
    values: dict[str, Any],
) -> None:
    for field_name, field_spec in specs.items():
        if field_name not in values:
            continue
        value = values[field_name]
        if isinstance(field_spec, dict) and "type" not in field_spec:
            if not isinstance(value, dict):
                raise SystemConfigReadError(
                    "SYSTEM_CONFIG_INVALID",
                    "系统配置字段类型无效",
                )
            _validate_persisted_fields(field_spec, value)
            continue
        if not _validate_persisted_field_type(field_spec, value):
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "系统配置字段类型无效",
            )


def _validate_persisted_profiles(section: dict[str, Any]) -> None:
    from config.model_profile_refs import parse_model_profile_ref

    profiles = section.get("profiles", [])
    if not isinstance(profiles, list):
        raise SystemConfigReadError("SYSTEM_CONFIG_INVALID", "模型 profiles 必须是数组")
    profile_ids: set[str] = set()
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            raise SystemConfigReadError("SYSTEM_CONFIG_INVALID", "模型 profile 必须是 object")
        profile_id = profile.get("id")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise SystemConfigReadError("SYSTEM_CONFIG_INVALID", "模型 profile id 无效")
        for field_name, default in _LLM_PROFILE_DEFAULTS.items():
            if field_name not in profile:
                continue
            value = profile[field_name]
            if isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, int):
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif isinstance(default, float):
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise SystemConfigReadError(
                    "SYSTEM_CONFIG_INVALID",
                    "模型 profile 字段类型无效",
                )
        try:
            normalized_id = _normalize_profile_id(
                profile_id,
                field_name="profiles[{}].id".format(index),
            )
            if normalized_id != profile_id:
                raise SystemConfigValidationError("模型 profile id 必须是规范值")
            if "provider" in profile:
                _coerce_llm_provider(profile["provider"])
        except SystemConfigValidationError as exc:
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "模型 profile 语义无效",
            ) from exc
        if normalized_id in profile_ids:
            raise SystemConfigReadError("SYSTEM_CONFIG_INVALID", "模型 profile id 重复")
        profile_ids.add(normalized_id)
    for field_name in ("default_main_profile_ref", "default_task_profile_ref"):
        if field_name not in section or section[field_name] is None:
            continue
        value = section[field_name]
        if not isinstance(value, dict):
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "模型 profile ref 必须是 object",
            )
        try:
            parsed = parse_model_profile_ref(value)
        except ValueError as exc:
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "模型 profile ref 无效",
            ) from exc
        if parsed is None or parsed.scope != "global" or parsed.id not in profile_ids:
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "模型 profile ref 指向的目标无效",
            )


def _validate_persisted_document(document: dict[str, Any]) -> None:
    for section in SECTION_KEYS:
        if section not in document:
            continue
        section_value = document[section]
        if not isinstance(section_value, dict):
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "系统配置 section 必须是 object",
            )
        if section == _LLM_PROFILES_SECTION:
            _validate_persisted_profiles(section_value)
        else:
            _validate_persisted_fields(_SECTION_FIELDS[section], section_value)
        try:
            if section in {"agent", "web_search", "skill_evolution"}:
                # Cross-field rules must use the same persisted > env > default
                # precedence as runtime; secrets are validated separately below.
                semantic_specs = {
                    field_name: field_spec
                    for field_name, field_spec in _SECTION_FIELDS[section].items()
                    if field_spec.get("type") != "secret"
                }
                effective_section = _process_fields_recursive(
                    section,
                    semantic_specs,
                    section_value,
                    [section],
                )
                if section == "agent":
                    _normalize_agent_effective(effective_section)
                elif section == "web_search":
                    _normalize_web_search_effective(effective_section)
                else:
                    _normalize_skill_evolution_effective(effective_section)
        except SystemConfigValidationError as exc:
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_INVALID",
                "系统配置语义无效，请修复配置后重试",
            ) from exc


def _validate_encrypted_secrets(document: dict[str, Any]) -> None:
    def validate_recursive(
        specs: dict[str, Any],
        values: dict[str, Any],
        path: list[str],
    ) -> None:
        for field_name, field_spec in specs.items():
            if field_name not in values:
                continue
            current_path = [*path, field_name]
            value = values[field_name]
            if isinstance(field_spec, dict) and "type" not in field_spec:
                validate_recursive(field_spec, value, current_path)
                continue
            if field_spec.get("type") != "secret" or not value:
                continue
            decrypt_secret(
                value,
                context=_secret_context(current_path),
                allow_key_creation=False,
            )

    try:
        for section in SECTION_KEYS:
            section_value = document.get(section)
            if not isinstance(section_value, dict):
                continue
            if section == _LLM_PROFILES_SECTION:
                for profile in section_value.get("profiles", []):
                    value = profile.get("api_key", "")
                    if value:
                        decrypt_secret(
                            value,
                            context="llm_profiles.profiles.api_key",
                            allow_key_creation=False,
                        )
                continue
            validate_recursive(_SECTION_FIELDS[section], section_value, [section])
    except SecretEncryptionError as exc:
        raise SystemConfigReadError(
            "SYSTEM_CONFIG_SECRET_UNSAFE",
            "系统配置 secret 不安全或无法解密，请检查加密密钥",
        ) from exc


def _read_config_document_unlocked() -> dict[str, Any]:
    if CONFIG_PATH.exists() or CONFIG_PATH.is_symlink():
        document = _read_json_document(CONFIG_PATH)
        _validate_persisted_document(document)
        if _document_has_legacy_model_fields(document):
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_MODEL_MIGRATION_REQUIRED",
                "系统模型配置需要先完成迁移",
            )
        if _document_has_plaintext_secrets(document):
            raise SystemConfigReadError(
                "SYSTEM_CONFIG_SECRET_MIGRATION_REQUIRED",
                "系统配置包含明文 secret，需要先完成安全迁移",
            )
        _validate_encrypted_secrets(document)
        return document
    return {}


def load_raw_system_config() -> dict[str, dict[str, Any]]:
    with _CONFIG_LOCK:
        return _load_raw_system_config_unlocked()


def _load_raw_system_config_unlocked() -> dict[str, dict[str, Any]]:
    document = _read_config_document_unlocked()
    config = {
        section: dict(document.get(section) or {})
        for section in SECTION_KEYS
    }

    return config


def _get_env_value(path: list[str], field_spec: dict[str, Any]) -> Any:
    env_name = field_spec.get("env")
    if env_name and os.getenv(env_name) is not None:
        return os.getenv(env_name)
    return None


def _secret_context(path: list[str]) -> str:
    if len(path) >= 3 and path[0] == _LLM_PROFILES_SECTION and path[1] == "profiles":
        return "llm_profiles.profiles.api_key"
    return ".".join(path)


def _coerce_secret_value(value: Any, *, path: list[str]) -> str:
    return decrypt_secret(
        _coerce_string(value),
        context=_secret_context(path),
        allow_key_creation=False,
    )


def _coerce_section_value(
    field_name: str,
    field_spec: dict[str, Any],
    value: Any,
    *,
    path: list[str] | None = None,
) -> Any:
    field_type = field_spec.get("type")
    if field_type == "bool":
        return _coerce_bool(value, field_name=field_name)
    if field_type == "int":
        return _coerce_int(value, field_name=field_name)
    if field_type == "float":
        return _coerce_float(value, field_name=field_name)
    if field_type == "secret":
        return _coerce_secret_value(value, path=path or [field_name])
    return _coerce_string(value)


def _process_fields_recursive(
    section: str,
    specs: dict[str, Any],
    raw_data: dict[str, Any],
    path: list[str],
) -> dict[str, Any]:
    result = {}
    for field_name, field_spec in specs.items():
        current_path = path + [field_name]
        # 如果 spec 还是一个 dict 且不含 "type" 键，说明是嵌套字段。
        if isinstance(field_spec, dict) and "type" not in field_spec:
            sub_raw = raw_data.get(field_name) or {}
            result[field_name] = _process_fields_recursive(
                section,
                field_spec,
                sub_raw,
                current_path,
            )
            continue

        # 普通字段处理
        val = raw_data.get(field_name)
        if val is None:
            env_val = _get_env_value(current_path, field_spec)
            if env_val is not None:
                val = env_val
        if val is None:
            val = field_spec.get("default")

        coerced = _coerce_section_value(field_name, field_spec, val, path=current_path)
        result[field_name] = _normalize_effective_value(section, field_name, coerced)
    return result


def _normalize_effective_value(section: str, field_name: str, value: Any) -> Any:
    if section != "storage":
        return value

    if REPO_ROOT != Path("/"):
        return value

    normalized = _coerce_string(value)
    if field_name == "project_root" and normalized == "/.onto-runtime":
        return str(ONTO_RUNTIME_ROOT)
    if field_name == "auth_users_file" and normalized == "/.onto-runtime/users.json":
        return str(ONTO_RUNTIME_ROOT / "users.json")
    return value


def _legacy_llm_profile(llm_config: dict[str, Any]) -> dict[str, Any]:
    profile = dict(_LLM_PROFILE_DEFAULTS)
    profile.update(
        {
            "id": _DEFAULT_LEGACY_PROFILE_ID,
            "name": "默认模型",
            "provider": "openai_compatible",
            "base_url": _coerce_string(llm_config.get("base_url")),
            "model": _coerce_string(llm_config.get("model")),
            "api_key": _coerce_string(llm_config.get("api_key")),
            "timeout": int(llm_config.get("timeout") or _LLM_PROFILE_DEFAULTS["timeout"]),
            "max_tokens": int(llm_config.get("max_tokens") or _LLM_PROFILE_DEFAULTS["max_tokens"]),
            "temperature": float(llm_config.get("temperature") if llm_config.get("temperature") is not None else _LLM_PROFILE_DEFAULTS["temperature"]),
            "enabled": True,
        }
    )
    return profile


def _is_llm_configured(payload: dict[str, Any] | None) -> bool:
    config = payload if isinstance(payload, dict) else {}
    return bool(_coerce_string(config.get("base_url")) and _coerce_string(config.get("model")))


def _public_llm_profile(profile: dict[str, Any], *, include_connection: bool = True) -> dict[str, Any]:
    public = {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "provider": profile.get("provider"),
        "model": profile.get("model"),
        "enabled": bool(profile.get("enabled")),
        "has_api_key": bool(profile.get("api_key")),
    }
    if include_connection:
        public.update(
            {
                "base_url": profile.get("base_url"),
                "timeout": profile.get("timeout"),
                "max_tokens": profile.get("max_tokens"),
                "temperature": profile.get("temperature"),
            }
        )
    return public


def _normalize_llm_profile(
    raw_profile: Any,
    *,
    legacy_llm: dict[str, Any],
    existing_profile: dict[str, Any] | None = None,
    index: int = 0,
    allow_colon_id: bool = True,
) -> dict[str, Any]:
    if not isinstance(raw_profile, dict):
        raise SystemConfigValidationError("profiles[{}] 必须是对象".format(index))

    profile = dict(_LLM_PROFILE_DEFAULTS)

    # 仅在“同 ID 的已有 profile”场景下做字段回退，
    # 避免新建 profile（尤其未填写 api_key 时）错误继承 legacy llm 的密钥。
    if existing_profile:
        for field_name in _LLM_PROFILE_DEFAULTS:
            if field_name in existing_profile:
                profile[field_name] = existing_profile[field_name]

    raw_profile_id = _coerce_string(raw_profile.get("id")) if "id" in raw_profile else ""
    existing_profile_id = _coerce_string(existing_profile.get("id")) if existing_profile else ""
    if raw_profile_id:
        profile["id"] = _normalize_profile_id(
            raw_profile.get("id"),
            field_name="profiles[{}].id".format(index),
            allow_colon=allow_colon_id,
        )
    elif existing_profile_id:
        profile["id"] = _normalize_profile_id(
            existing_profile_id,
            field_name="profiles[{}].id".format(index),
            allow_colon=allow_colon_id,
        )
    else:
        profile["id"] = _generate_profile_id("model")

    if "name" in raw_profile:
        profile["name"] = _coerce_string(raw_profile.get("name")) or profile["id"]
    elif not profile.get("name"):
        profile["name"] = profile["id"]

    if "provider" in raw_profile:
        profile["provider"] = _coerce_llm_provider(raw_profile.get("provider"))
    else:
        profile["provider"] = _coerce_llm_provider(profile.get("provider"))

    for field_name in ("base_url", "model"):
        if field_name in raw_profile:
            profile[field_name] = _coerce_string(raw_profile.get(field_name))
        else:
            profile[field_name] = _coerce_string(profile.get(field_name))

    if "api_key" in raw_profile:
        incoming_api_key = _coerce_secret_value(
            raw_profile.get("api_key"),
            path=[_LLM_PROFILES_SECTION, "profiles", "api_key"],
        )
        if incoming_api_key:
            profile["api_key"] = incoming_api_key
        elif raw_profile.get("clear_api_key"):
            profile["api_key"] = ""
    else:
        profile["api_key"] = _coerce_secret_value(
            profile.get("api_key"),
            path=[_LLM_PROFILES_SECTION, "profiles", "api_key"],
        )

    if "clear_api_key" in raw_profile and _coerce_bool(raw_profile.get("clear_api_key"), field_name="profiles[{}].clear_api_key".format(index)):
        profile["api_key"] = ""

    for field_name in ("timeout", "max_tokens"):
        if field_name in raw_profile:
            profile[field_name] = _coerce_int(raw_profile.get(field_name), field_name="profiles[{}].{}".format(index, field_name))
        else:
            profile[field_name] = int(profile.get(field_name) or _LLM_PROFILE_DEFAULTS[field_name])

    if "temperature" in raw_profile:
        profile["temperature"] = _coerce_float(raw_profile.get("temperature"), field_name="profiles[{}].temperature".format(index))
    else:
        profile["temperature"] = float(profile.get("temperature") if profile.get("temperature") is not None else _LLM_PROFILE_DEFAULTS["temperature"])

    for field_name in ("enabled",):
        if field_name in raw_profile:
            profile[field_name] = _coerce_bool(raw_profile.get(field_name), field_name="profiles[{}].{}".format(index, field_name))
        else:
            profile[field_name] = bool(profile.get(field_name))

    return profile


def _normalize_default_profile_ref(
    ref_value: Any,
    *,
    profile_ids: set[str],
    profiles: list[dict[str, Any]],
    allow_empty: bool,
) -> dict[str, str] | None:
    """Normalize a global default profile ref.

    Global default refs must point to a global-scope profile id present in ``profile_ids``.
    ``allow_empty=False`` (main role) falls back to the first enabled profile when unset.
    """
    from config.model_profile_refs import ModelProfileRef, model_profile_ref_to_dict, parse_model_profile_ref

    if ref_value:
        try:
            parsed = parse_model_profile_ref(ref_value)
        except ValueError as exc:
            raise SystemConfigValidationError(str(exc)) from exc
        if parsed is not None:
            if parsed.scope != "global":
                raise SystemConfigValidationError("全局默认模型 ref 必须指向 global scope profile")
            if parsed.id not in profile_ids:
                raise SystemConfigValidationError("全局默认模型 ref 指向的 profile 不存在: {}".format(parsed.id))
            return model_profile_ref_to_dict(parsed)

    if allow_empty or not profiles:
        return None
    enabled_profile = next((profile for profile in profiles if profile.get("enabled")), profiles[0])
    return model_profile_ref_to_dict(ModelProfileRef(scope="global", id=enabled_profile["id"]))


def _normalize_llm_profiles_section(
    raw_section: Any,
    *,
    legacy_llm: dict[str, Any],
    current_raw_section: dict[str, Any] | None = None,
    allow_profile_id_colon: bool = True,
) -> dict[str, Any]:
    section = raw_section if isinstance(raw_section, dict) else {}
    current_section = current_raw_section if isinstance(current_raw_section, dict) else {}
    raw_profiles = section.get("profiles")
    if raw_profiles is None:
        raw_profiles = current_section.get("profiles")

    if raw_profiles is None:
        profiles = [_legacy_llm_profile(legacy_llm)] if _is_llm_configured(legacy_llm) else []
    else:
        if not isinstance(raw_profiles, list):
            raise SystemConfigValidationError("llm_profiles.profiles 必须是数组")
        existing_by_id = {
            _coerce_string(profile.get("id")): profile
            for profile in (current_section.get("profiles") or [])
            if isinstance(profile, dict) and _coerce_string(profile.get("id"))
        }
        profiles = []
        seen_ids: set[str] = set()
        for index, raw_profile in enumerate(raw_profiles):
            candidate_id = _coerce_string(raw_profile.get("id")) if isinstance(raw_profile, dict) else ""
            profile = _normalize_llm_profile(
                raw_profile,
                legacy_llm=legacy_llm,
                existing_profile=existing_by_id.get(candidate_id),
                index=index,
                allow_colon_id=allow_profile_id_colon,
            )
            if profile["id"] in seen_ids:
                raise SystemConfigValidationError("llm_profiles.profiles 中存在重复 id: {}".format(profile["id"]))
            seen_ids.add(profile["id"])
            profiles.append(profile)
    profile_ids = {profile["id"] for profile in profiles}
    max_profiles = resolve_model_profile_max_count()
    target_profile_count = len(profiles)
    persisted_profiles = current_section.get("profiles")
    persisted_profile_count = (
        len(persisted_profiles) if isinstance(persisted_profiles, list) else 0
    )
    if (
        target_profile_count > max_profiles
        and target_profile_count > persisted_profile_count
    ):
        raise SystemConfigValidationError("模型数量已达上限: {}".format(max_profiles))

    default_main_profile_ref = _normalize_default_profile_ref(
        section.get("default_main_profile_ref", current_section.get("default_main_profile_ref")),
        profile_ids=profile_ids,
        profiles=profiles,
        allow_empty=not profiles,
    )
    default_task_profile_ref = _normalize_default_profile_ref(
        section.get("default_task_profile_ref", current_section.get("default_task_profile_ref")),
        profile_ids=profile_ids,
        profiles=profiles,
        allow_empty=True,
    )

    return {
        "default_main_profile_ref": default_main_profile_ref,
        "default_task_profile_ref": default_task_profile_ref,
        "profiles": profiles,
    }


def _normalize_web_search_effective(section: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(section or {})
    provider = _coerce_string(normalized.get("provider")).lower() or "tavily"
    if provider not in _SUPPORTED_WEB_SEARCH_PROVIDERS:
        raise SystemConfigValidationError(
            "web_search.provider 仅支持 {}".format(" | ".join(sorted(_SUPPORTED_WEB_SEARCH_PROVIDERS)))
        )
    search_depth = _coerce_string(normalized.get("search_depth")).lower() or "basic"
    if search_depth not in _SUPPORTED_WEB_SEARCH_DEPTHS:
        raise SystemConfigValidationError(
            "web_search.search_depth 仅支持 {}".format(" | ".join(sorted(_SUPPORTED_WEB_SEARCH_DEPTHS)))
        )

    timeout_seconds = int(normalized.get("timeout_seconds") or 20)
    default_max_results = int(normalized.get("default_max_results") or 5)
    max_results_limit = int(normalized.get("max_results_limit") or 10)
    if timeout_seconds < 1:
        raise SystemConfigValidationError("web_search.timeout_seconds 必须大于等于 1")
    if default_max_results < 1:
        raise SystemConfigValidationError("web_search.default_max_results 必须大于等于 1")
    if max_results_limit < 1:
        raise SystemConfigValidationError("web_search.max_results_limit 必须大于等于 1")
    if default_max_results > max_results_limit:
        raise SystemConfigValidationError("web_search.default_max_results 不能大于 max_results_limit")

    normalized.update(
        {
            "provider": provider,
            "base_url": (_coerce_string(normalized.get("base_url")) or "https://api.tavily.com").rstrip("/"),
            "api_key": _coerce_string(normalized.get("api_key")),
            "timeout_seconds": timeout_seconds,
            "default_max_results": default_max_results,
            "max_results_limit": max_results_limit,
            "search_depth": search_depth,
            "allowed_domains": _coerce_string(normalized.get("allowed_domains")),
            "blocked_domains": _coerce_string(normalized.get("blocked_domains")),
        }
    )
    return normalized


def _normalize_agent_effective(section: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(section or {})
    positive_fields = (
        "shutdown_total_deadline_seconds",
        "sse_subscriber_queue_size",
        "sse_max_subscribers_per_run",
        "sse_max_subscribers_total",
        "completed_replay_ttl_seconds",
        "short_io_workers",
        "short_io_max_pending",
        "short_io_capacity_wait_seconds",
        "short_io_operation_timeout_seconds",
        "external_io_workers",
        "external_io_max_pending",
        "external_io_capacity_wait_seconds",
        "external_io_operation_timeout_seconds",
        "tool_io_workers",
        "tool_io_max_pending",
        "tool_io_capacity_wait_seconds",
        "tool_io_operation_timeout_seconds",
    )
    for field_name in positive_fields:
        if float(normalized.get(field_name) or 0) <= 0:
            raise SystemConfigValidationError(
                f"agent.{field_name} 必须大于 0"
            )
    if int(normalized["sse_max_subscribers_total"]) < int(
        normalized["sse_max_subscribers_per_run"]
    ):
        raise SystemConfigValidationError(
            "sse_max_subscribers_total 不能小于 sse_max_subscribers_per_run"
        )
    return normalized


def _normalize_skill_evolution_effective(section: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(section or {})
    base_url = _coerce_string(normalized.get("base_url")).rstrip("/")
    timeout_seconds = int(normalized.get("timeout_seconds") or 30)
    poll_interval_seconds = int(normalized.get("poll_interval_seconds") or 10)
    task_timeout_seconds = int(normalized.get("task_timeout_seconds") or 1800)
    max_iterations = int(normalized.get("max_iterations") or 15)
    if timeout_seconds < 1:
        raise SystemConfigValidationError("skill_evolution.timeout_seconds 必须大于等于 1")
    if poll_interval_seconds < 1:
        raise SystemConfigValidationError("skill_evolution.poll_interval_seconds 必须大于等于 1")
    if task_timeout_seconds < 60:
        raise SystemConfigValidationError("skill_evolution.task_timeout_seconds 必须大于等于 60")
    if not (1 <= max_iterations <= 30):
        raise SystemConfigValidationError("skill_evolution.max_iterations 必须在 1-30 之间")
    normalized.update(
        {
            "enabled": bool(normalized.get("enabled", False)),
            "base_url": base_url,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "task_timeout_seconds": task_timeout_seconds,
            "max_iterations": max_iterations,
        }
    )
    return normalized


def load_system_config() -> dict[str, dict[str, Any]]:
    with _CONFIG_LOCK:
        return _load_system_config_unlocked()


def _load_system_config_unlocked() -> dict[str, dict[str, Any]]:
    raw = _load_raw_system_config_unlocked()
    try:
        effective: dict[str, dict[str, Any]] = {}
        for section, specs in _SECTION_FIELDS.items():
            if section == _LLM_PROFILES_SECTION:
                continue
            raw_section = raw.get(section) or {}
            effective[section] = _process_fields_recursive(
                section,
                specs,
                raw_section,
                [section],
            )
            if section == "agent":
                effective[section] = _normalize_agent_effective(effective[section])
            if section == "web_search":
                effective[section] = _normalize_web_search_effective(effective[section])
            if section == "skill_evolution":
                effective[section] = _normalize_skill_evolution_effective(effective[section])

        # 特殊处理 llm_profiles
        effective[_LLM_PROFILES_SECTION] = _normalize_llm_profiles_section(
            raw.get(_LLM_PROFILES_SECTION),
            legacy_llm=effective.get("llm", {}),
            current_raw_section=raw.get(_LLM_PROFILES_SECTION),
        )
        return effective
    except SystemConfigValidationError as exc:
        raise SystemConfigReadError(
            "SYSTEM_CONFIG_INVALID",
            "系统配置语义无效，请修复配置后重试",
        ) from exc


def _publicize_fields(specs: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for field_name, field_spec in specs.items():
        value = values.get(field_name)
        if isinstance(field_spec, dict) and "type" not in field_spec:
            payload[field_name] = _publicize_fields(field_spec, value if isinstance(value, dict) else {})
            continue
        if field_spec.get("type") == "secret":
            payload[f"has_{field_name}"] = bool(value)
        else:
            payload[field_name] = value
    return payload


def _public_section(section: str, config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if section == _LLM_PROFILES_SECTION:
        section_config = config.get(_LLM_PROFILES_SECTION) or {}
        default_main_ref = section_config.get("default_main_profile_ref")
        default_main_id = default_main_ref.get("id") if isinstance(default_main_ref, dict) else None
        return {
            "default_main_profile_ref": default_main_ref,
            "default_task_profile_ref": section_config.get("default_task_profile_ref"),
            "profiles": [
                {
                    **_public_llm_profile(profile, include_connection=True),
                    "is_default": profile.get("id") == default_main_id,
                }
                for profile in (section_config.get("profiles") or [])
            ],
        }

    section_values = config.get(section) or {}
    return _publicize_fields(_SECTION_FIELDS[section], section_values if isinstance(section_values, dict) else {})


def get_public_system_config() -> dict[str, dict[str, Any]]:
    config = load_system_config()
    return {section: _public_section(section, config) for section in SECTION_KEYS}


def get_system_config_template() -> dict[str, dict[str, Any]]:
    template: dict[str, dict[str, Any]] = {}
    for section, fields in _SECTION_FIELDS.items():
        template[section] = {}
        for field_name, field_spec in fields.items():
            if isinstance(field_spec, dict) and "type" not in field_spec:
                template[section][field_name] = {
                    nested_name: nested_spec["default"]
                    for nested_name, nested_spec in field_spec.items()
                }
            else:
                template[section][field_name] = field_spec["default"]
    template[_LLM_PROFILES_SECTION] = {
        "default_main_profile_ref": None,
        "default_task_profile_ref": None,
        "profiles": [],
    }
    return template


def _merge_section_update(
    section: str,
    data: dict[str, Any],
    current_raw_section: dict[str, Any],
) -> dict[str, Any]:
    allowed_fields = set(_SECTION_FIELDS[section].keys())
    clear_fields = _CLEAR_FIELDS.get(section, {})
    unknown_fields = set(data.keys()) - allowed_fields - set(clear_fields.keys())
    if unknown_fields:
        bad_fields = ", ".join(sorted(unknown_fields))
        raise SystemConfigValidationError(f"{section} 不支持字段: {bad_fields}")

    merged = {
        field_name: current_raw_section[field_name]
        for field_name in allowed_fields
        if field_name in current_raw_section
    }
    for field_name in allowed_fields:
        if field_name not in data:
            continue
        field_spec = _SECTION_FIELDS[section][field_name]
        value = _coerce_section_value(
            field_name,
            field_spec,
            data[field_name],
            path=[section, field_name],
        )
        if field_spec["type"] == "secret" and value == "":
            clear_name = next((key for key, target in clear_fields.items() if target == field_name), None)
            if clear_name:
                raise SystemConfigValidationError(f"{field_name} 不能为空；如需清空请使用 {clear_name}")
        merged[field_name] = value

    for clear_name, target_field in clear_fields.items():
        if clear_name in data and _coerce_bool(data[clear_name], field_name=clear_name):
            merged[target_field] = ""

    if section == "agent":
        _normalize_agent_effective(
            _process_fields_recursive(
                "agent",
                _SECTION_FIELDS["agent"],
                merged,
                ["agent"],
            )
        )
    if section == "web_search":
        merged = _normalize_web_search_effective(merged)
    if section == "skill_evolution":
        merged = _normalize_skill_evolution_effective(merged)

    return merged


def _encrypt_secret_values_recursive(
    specs: dict[str, Any],
    values: dict[str, Any],
    path: list[str],
) -> dict[str, Any]:
    result = dict(values or {})
    for field_name, field_spec in specs.items():
        current_path = path + [field_name]
        if isinstance(field_spec, dict) and "type" not in field_spec:
            nested = result.get(field_name)
            if isinstance(nested, dict):
                result[field_name] = _encrypt_secret_values_recursive(
                    field_spec,
                    nested,
                    current_path,
                )
            continue
        if field_spec.get("type") == "secret" and field_name in result:
            result[field_name] = encrypt_secret(
                _coerce_string(result.get(field_name)),
                context=_secret_context(current_path),
            )
    return result


def _encrypt_llm_profiles_section(section: dict[str, Any]) -> dict[str, Any]:
    result = dict(section or {})
    if "profiles" not in result:
        return result
    profiles = []
    for profile in result.get("profiles") or []:
        next_profile = dict(profile or {})
        if "api_key" in next_profile:
            next_profile["api_key"] = encrypt_secret(
                _coerce_string(next_profile.get("api_key")),
                context=_secret_context([_LLM_PROFILES_SECTION, "profiles", "api_key"]),
            )
        profiles.append(next_profile)
    result["profiles"] = profiles
    return result


def _encrypt_config_document(config: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    document: dict[str, dict[str, Any]] = {}
    for section in SECTION_KEYS:
        section_config = config.get(section, {})
        if section == _LLM_PROFILES_SECTION:
            document[section] = _encrypt_llm_profiles_section(section_config)
        else:
            document[section] = _encrypt_secret_values_recursive(
                _SECTION_FIELDS[section],
                section_config if isinstance(section_config, dict) else {},
                [section],
            )
    return document


def _write_config_document_unlocked(
    config: dict[str, dict[str, Any]],
    *,
    source_document: dict[str, Any],
    drop_top_level_keys: set[str] | None = None,
) -> None:
    """Write owned sections without deleting unrelated migration sources.

    A section writer is not authorized to discard top-level data that it does
    not own.  Concrete migrations explicitly remove their own legacy source;
    callers may name other keys that the current operation intentionally
    retires.
    """
    removed = set(drop_top_level_keys or ())
    document = {
        key: value
        for key, value in source_document.items()
        if key not in SECTION_KEYS and key not in removed
    }
    document.update(_encrypt_config_document(config))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def build_effective_section_config(section: str, data: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    normalized_section = _coerce_string(section).lower()
    if normalized_section not in SECTION_KEYS:
        raise SystemConfigValidationError("section 非法，仅支持 {}".format(" | ".join(SECTION_KEYS)))
    if data is not None and not isinstance(data, dict):
        raise SystemConfigValidationError("data 必须是对象")

    config = load_system_config()
    raw = load_raw_system_config()

    if normalized_section == _LLM_PROFILES_SECTION:
        next_effective = _normalize_llm_profiles_section(
            data or {},
            legacy_llm=config["llm"],
            current_raw_section=raw.get(normalized_section, {}),
            allow_profile_id_colon=False,
        )
        return normalized_section, next_effective
    merged_raw = _merge_section_update(
        normalized_section,
        data or {},
        raw.get(normalized_section, {}),
    )
    next_effective = dict(config[normalized_section])
    for field_name in _SECTION_FIELDS[normalized_section]:
        if field_name not in merged_raw:
            continue
        field_spec = _SECTION_FIELDS[normalized_section][field_name]
        if isinstance(field_spec, dict) and "type" not in field_spec:
            next_effective[field_name] = {
                nested_name: _coerce_section_value(
                    nested_name,
                    nested_spec,
                    (merged_raw[field_name] or {}).get(nested_name),
                    path=[normalized_section, field_name, nested_name],
                )
                for nested_name, nested_spec in field_spec.items()
            }
            continue
        next_effective[field_name] = _coerce_section_value(
            field_name,
            field_spec,
            merged_raw[field_name],
            path=[normalized_section, field_name],
        )
    if normalized_section == "agent":
        next_effective = _normalize_agent_effective(next_effective)
    if normalized_section == "web_search":
        next_effective = _normalize_web_search_effective(next_effective)
    if normalized_section == "skill_evolution":
        next_effective = _normalize_skill_evolution_effective(next_effective)
    return normalized_section, next_effective


def update_system_config_section(section: str, data: dict[str, Any]) -> dict[str, Any]:
    normalized_section = _coerce_string(section).lower()
    if normalized_section not in SECTION_KEYS:
        raise SystemConfigValidationError("section 非法，仅支持 {}".format(" | ".join(SECTION_KEYS)))
    if not isinstance(data, dict):
        raise SystemConfigValidationError("data 必须是对象")

    with _CONFIG_LOCK:
        document = _read_config_document_unlocked()
        raw_config = {section_key: dict(document.get(section_key) or {}) for section_key in SECTION_KEYS}
        effective = _load_system_config_unlocked()

        if normalized_section == _LLM_PROFILES_SECTION:
            raw_config[normalized_section] = _normalize_llm_profiles_section(
                data,
                legacy_llm=effective["llm"],
                current_raw_section=raw_config[normalized_section],
                allow_profile_id_colon=False,
            )
        else:
            raw_config[normalized_section] = _merge_section_update(
                normalized_section,
                data,
                raw_config[normalized_section],
            )
        _write_config_document_unlocked(raw_config, source_document=document)

    return _public_section(normalized_section, load_system_config())


def import_system_config(config_payload: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    if not isinstance(config_payload, dict):
        raise SystemConfigValidationError("config 必须是对象")
    imported_sections = [section for section in SECTION_KEYS if section in config_payload]
    unknown_sections = set(config_payload.keys()) - set(SECTION_KEYS) - _LEGACY_IGNORED_TOP_LEVEL_KEYS
    if unknown_sections:
        raise SystemConfigValidationError("config 不支持 section: {}".format(", ".join(sorted(unknown_sections))))
    if not imported_sections:
        raise SystemConfigValidationError("config 至少需要包含一个合法 section")

    with _CONFIG_LOCK:
        document = _read_config_document_unlocked()
        raw_config = {section_key: dict(document.get(section_key) or {}) for section_key in SECTION_KEYS}
        effective = _load_system_config_unlocked()
        for section in imported_sections:
            section_data = config_payload.get(section)
            if not isinstance(section_data, dict):
                raise SystemConfigValidationError(f"{section} 必须是对象")
            if section == _LLM_PROFILES_SECTION:
                raw_config[section] = _normalize_llm_profiles_section(
                    section_data,
                    legacy_llm=effective["llm"],
                    current_raw_section=raw_config[section],
                    allow_profile_id_colon=False,
                )
            else:
                raw_config[section] = _merge_section_update(
                    section,
                    section_data,
                    raw_config[section],
                )
        _write_config_document_unlocked(raw_config, source_document=document)

    return imported_sections, get_public_system_config()


def resolve_role_config(
    role: str,
    workspace_id: str | None = None,
    explicit_ref: Any = None,
    *,
    allow_default_fallback: bool = True,
) -> dict[str, Any]:
    """Resolve a model profile config by role (main | task).

    MAIN: explicit_ref > workspace default_main > global default_main > first candidate.
    TASK: explicit_ref > workspace default_task > global default_task > fallback to MAIN
    (task follows main's resolved profile/scope when unset).
    """
    from config.model_profile_refs import (
        encode_model_profile_ref,
        model_profile_ref_to_dict,
        parse_model_profile_ref,
    )

    normalized_role = (role or "main").strip().lower()
    if normalized_role not in ("main", "task"):
        raise SystemConfigValidationError("role 仅支持 main | task")

    global_profiles, global_main_ref, global_task_ref = _enabled_global_profiles()
    workspace_profiles = _enabled_workspace_profiles(workspace_id)

    parsed = None
    if explicit_ref:
        try:
            parsed = parse_model_profile_ref(explicit_ref)
        except ValueError as exc:
            raise SystemConfigValidationError(str(exc)) from exc

    if parsed is None and allow_default_fallback:
        default_ref = _default_ref_for_role(
            normalized_role,
            workspace_id=workspace_id,
            global_profiles=global_profiles,
            workspace_profiles=workspace_profiles,
            global_main_ref=global_main_ref,
            global_task_ref=global_task_ref,
        )
        if default_ref is not None:
            try:
                parsed = parse_model_profile_ref(default_ref)
            except ValueError as exc:
                raise SystemConfigValidationError(str(exc)) from exc

    if parsed is None:
        if normalized_role == "task":
            return resolve_role_config("main", workspace_id=workspace_id, allow_default_fallback=True)
        raise SystemConfigValidationError("当前工作空间没有可用模型配置")

    candidates = workspace_profiles if parsed.scope == "workspace" else global_profiles
    resolved = next((dict(profile) for profile in candidates if profile.get("id") == parsed.id), None)
    if resolved is None:
        raise SystemConfigValidationError(
            "模型配置不存在或未启用: {}".format(encode_model_profile_ref(parsed))
        )

    encoded_ref = encode_model_profile_ref(parsed)
    resolved["profile_id"] = resolved["id"]
    resolved["profile_scope"] = parsed.scope
    resolved["model_profile_ref"] = model_profile_ref_to_dict(parsed)
    resolved["id"] = encoded_ref
    return resolved


def resolve_llm_profile_config(
    profile_id: str | None = None,
    *,
    allow_default_fallback: bool = True,
) -> dict[str, Any]:
    """Resolve an enabled global MAIN model profile (no workspace context)."""
    return resolve_role_config(
        "main",
        workspace_id=None,
        explicit_ref=profile_id,
        allow_default_fallback=allow_default_fallback,
    )


def _decorate_scoped_profile(
    profile: dict[str, Any],
    *,
    scope: str,
    include_connection: bool,
    is_default: bool = False,
) -> dict[str, Any]:
    from config.model_profile_refs import ModelProfileRef, encode_model_profile_ref

    raw_id = _coerce_string(profile.get("id"))
    scoped_id = encode_model_profile_ref(ModelProfileRef(scope=scope, id=raw_id))
    return {
        **_public_llm_profile(profile, include_connection=include_connection),
        "id": scoped_id,
        "scope": scope,
        "profile_id": raw_id,
        "is_default": is_default,
    }


def _workspace_default_ref(workspace_id: str | None, role: str) -> dict[str, str] | None:
    if not workspace_id:
        return None
    try:
        from workspace.workspaces import get_workspace_runtime_config

        llm_config = (get_workspace_runtime_config(workspace_id).get("llm") or {})
        default_ref = llm_config.get("default_{}_profile_ref".format(role))
    except Exception:
        return None
    if not default_ref:
        return None
    try:
        from config.model_profile_refs import model_profile_ref_to_dict

        return model_profile_ref_to_dict(default_ref)
    except ValueError:
        return None


def _enabled_global_profiles() -> tuple[list[dict[str, Any]], dict[str, str] | None, dict[str, str] | None]:
    config = load_system_config()
    profiles_config = config.get(_LLM_PROFILES_SECTION) or {}
    profiles = [
        dict(profile)
        for profile in (profiles_config.get("profiles") or [])
        if profile.get("enabled") and _is_llm_configured(profile)
    ]
    if not profiles and _is_llm_configured(config.get("llm")):
        profiles = [_legacy_llm_profile(config["llm"])]
    return (
        profiles,
        profiles_config.get("default_main_profile_ref"),
        profiles_config.get("default_task_profile_ref"),
    )


def _enabled_workspace_profiles(workspace_id: str | None) -> list[dict[str, Any]]:
    if not workspace_id:
        return []
    try:
        from config.workspace_llm_profiles import get_enabled_workspace_llm_profiles

        return get_enabled_workspace_llm_profiles(workspace_id)
    except KeyError:
        return []


def _find_profile_by_ref(
    ref: dict[str, str] | None,
    *,
    global_profiles: list[dict[str, Any]],
    workspace_profiles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not ref:
        return None
    scope = _coerce_string(ref.get("scope"))
    profile_id = _coerce_string(ref.get("id"))
    candidates = workspace_profiles if scope == "workspace" else global_profiles if scope == "global" else []
    return next((profile for profile in candidates if profile.get("id") == profile_id), None)


def _default_ref_for_role(
    role: str,
    *,
    workspace_id: str | None,
    global_profiles: list[dict[str, Any]],
    workspace_profiles: list[dict[str, Any]],
    global_main_ref: dict[str, str] | None,
    global_task_ref: dict[str, str] | None,
) -> dict[str, str] | None:
    """Resolve the default profile ref for a role.

    main: workspace default_main > global default_main > first candidate.
    task: workspace default_task > global default_task > None (caller falls back to main).
    """
    from config.model_profile_refs import ModelProfileRef, model_profile_ref_to_dict

    if role == "task":
        ws_ref = _workspace_default_ref(workspace_id, "task")
        if _find_profile_by_ref(ws_ref, global_profiles=global_profiles, workspace_profiles=workspace_profiles):
            return ws_ref
        if global_task_ref and _find_profile_by_ref(
            global_task_ref, global_profiles=global_profiles, workspace_profiles=workspace_profiles
        ):
            return global_task_ref
        return None

    ws_ref = _workspace_default_ref(workspace_id, "main")
    if _find_profile_by_ref(ws_ref, global_profiles=global_profiles, workspace_profiles=workspace_profiles):
        return ws_ref
    if global_main_ref and _find_profile_by_ref(
        global_main_ref, global_profiles=global_profiles, workspace_profiles=workspace_profiles
    ):
        return global_main_ref
    first_global = global_profiles[0] if global_profiles else None
    if first_global:
        return model_profile_ref_to_dict(ModelProfileRef(scope="global", id=first_global["id"]))
    first_workspace = workspace_profiles[0] if workspace_profiles else None
    if first_workspace:
        return model_profile_ref_to_dict(ModelProfileRef(scope="workspace", id=first_workspace["id"]))
    return None


def get_public_llm_profiles(workspace_id: str | None = None) -> dict[str, Any]:
    """Return enabled model profiles for chat model selection."""
    from config.model_profile_refs import (
        ModelProfileRef,
        encode_model_profile_ref,
        model_profile_ref_to_dict,
    )

    global_profiles, global_main_ref, global_task_ref = _enabled_global_profiles()

    if workspace_id:
        workspace_profiles = _enabled_workspace_profiles(workspace_id)
        default_main_ref = _default_ref_for_role(
            "main",
            workspace_id=workspace_id,
            global_profiles=global_profiles,
            workspace_profiles=workspace_profiles,
            global_main_ref=global_main_ref,
            global_task_ref=global_task_ref,
        )
        default_task_ref = _default_ref_for_role(
            "task",
            workspace_id=workspace_id,
            global_profiles=global_profiles,
            workspace_profiles=workspace_profiles,
            global_main_ref=global_main_ref,
            global_task_ref=global_task_ref,
        )
        profiles = [
            _decorate_scoped_profile(
                profile,
                scope="global",
                include_connection=False,
                is_default=default_main_ref == {"scope": "global", "id": profile.get("id")}
                or default_task_ref == {"scope": "global", "id": profile.get("id")},
            )
            for profile in global_profiles
        ] + [
            _decorate_scoped_profile(
                profile,
                scope="workspace",
                include_connection=False,
                is_default=default_main_ref == {"scope": "workspace", "id": profile.get("id")}
                or default_task_ref == {"scope": "workspace", "id": profile.get("id")},
            )
            for profile in workspace_profiles
        ]
        ws_configured_main_ref = _workspace_default_ref(workspace_id, "main")
        invalid_default_profile_ref = (
            None
            if ws_configured_main_ref is None
            or _find_profile_by_ref(
                ws_configured_main_ref,
                global_profiles=global_profiles,
                workspace_profiles=workspace_profiles,
            )
            else ws_configured_main_ref
        )
        return {
            "default_profile_id": encode_model_profile_ref(default_main_ref) if default_main_ref else "",
            "default_main_profile_ref": default_main_ref,
            "default_task_profile_ref": default_task_ref,
            "profiles": profiles,
            "invalid_default_profile_ref": invalid_default_profile_ref,
        }

    default_main_id = global_main_ref.get("id") if isinstance(global_main_ref, dict) else None
    profiles = [
        {
            **_public_llm_profile(profile, include_connection=False),
            "is_default": profile.get("id") == default_main_id,
        }
        for profile in global_profiles
    ]
    if not any(profile.get("is_default") for profile in profiles) and profiles:
        profiles[0]["is_default"] = True
        default_main_id = profiles[0]["id"]
    default_main_ref = (
        model_profile_ref_to_dict(ModelProfileRef(scope="global", id=default_main_id))
        if default_main_id
        else None
    )
    return {
        "default_profile_id": default_main_id or "",
        "default_main_profile_ref": default_main_ref,
        "default_task_profile_ref": global_task_ref,
        "profiles": profiles,
    }


def resolve_effective_llm_profile_config(
    profile_ref: Any = None,
    *,
    workspace_id: str | None,
    allow_default_fallback: bool = True,
) -> dict[str, Any]:
    """Resolve global or workspace-private MAIN model profile to a private config."""
    return resolve_role_config(
        "main",
        workspace_id=workspace_id,
        explicit_ref=profile_ref,
        allow_default_fallback=allow_default_fallback,
    )


def get_system_config_readiness() -> dict[str, Any]:
    """Return user-facing readiness for runtime-critical external config."""
    config = load_system_config()
    model = _model_readiness(config)
    return {
        "ready": bool(model["ready"]),
        "model": model,
    }


def _model_readiness(config: dict[str, dict[str, Any]]) -> dict[str, Any]:
    profiles_config = config.get(_LLM_PROFILES_SECTION) or {}
    profiles = [
        profile
        for profile in (profiles_config.get("profiles") or [])
        if profile.get("enabled") and _is_llm_configured(profile)
    ]
    default_main_ref = profiles_config.get("default_main_profile_ref")
    default_main_id = default_main_ref.get("id") if isinstance(default_main_ref, dict) else None
    if profiles:
        if not default_main_id or not any(profile.get("id") == default_main_id for profile in profiles):
            default_main_id = _coerce_string(profiles[0].get("id"))
            default_main_ref = {"scope": "global", "id": default_main_id} if default_main_id else None
        return {
            "ready": True,
            "message": "模型配置已就绪",
            "missing_fields": [],
            "default_profile_id": default_main_id,
            "default_main_profile_ref": default_main_ref,
            "profile_count": len(profiles),
        }
    return {
        "ready": False,
        "message": "请先配置可用模型",
        "missing_fields": ["llm_profiles.profiles"],
        "default_profile_id": "",
        "default_main_profile_ref": None,
        "profile_count": 0,
    }


def _agent_config_for_connection_check(effective: dict[str, Any]) -> Any:
    from dataclasses import replace

    from config.settings import AgentConfig

    base = AgentConfig.from_env()
    return replace(
        base,
        llm_connection_max_retries=int(effective["llm_connection_max_retries"]),
        main_agent_max_iterations=int(effective["main_agent_max_iterations"]),
        sub_agent_max_iterations=int(effective["sub_agent_max_iterations"]),
        sub_agent_max_concurrency=int(effective["sub_agent_max_concurrency"]),
        sub_agent_max_depth=int(effective["sub_agent_max_depth"]),
        parallel_default_max_workers=int(effective["parallel_default_max_workers"]),
        tool_timeout_seconds=max(1, int(effective.get("tool_timeout_seconds") or 600)),
        context_max_rounds=int(effective["context_max_rounds"]),
        runtime_warmup_enabled=bool(effective.get("runtime_warmup_enabled", True)),
        execute_script_timeout=int(effective.get("execute_script_timeout") or 300),
        evolution_min_occurrences=int(effective.get("evolution_min_occurrences", 3)),
        evolution_min_applied=int(effective.get("evolution_min_applied", 2)),
        evolution_max_rejection=int(effective.get("evolution_max_rejection", 0)),
        evolution_passive_reflection_enabled=bool(
            effective.get("evolution_passive_reflection_enabled", False)
        ),
        evolution_patch_auto_apply=bool(effective.get("evolution_patch_auto_apply", False)),
        evolution_patch_cooldown_sec=int(effective.get("evolution_patch_cooldown_sec", 3600)),
        evolution_sweep_interval_sec=int(effective.get("evolution_sweep_interval_sec", 1800)),
    )


def _validate_jiuwen_tools_without_runtime_delegates(
    _agent: Any,
    *,
    tool_infos: list[dict[str, Any]] | None = None,
    **_kwargs: Any,
) -> list[Any]:
    """Validate the runtime tool descriptors without touching Jiuwen globals."""
    from types import SimpleNamespace

    cards: list[Any] = []
    for index, tool_info in enumerate(tool_infos or []):
        if not isinstance(tool_info, dict):
            raise ValueError("Jiuwen 工具描述必须是对象")
        name = _coerce_string(tool_info.get("name"))
        description = _coerce_string(tool_info.get("description"))
        parameters = tool_info.get("parameters")
        if not name or not description or not isinstance(parameters, dict):
            raise ValueError("Jiuwen 工具描述缺少 name、description 或 parameters")
        cards.append(SimpleNamespace(id=f"config-check-{index}", name=name))
    return cards


def test_system_config_connection(section: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized_section = _coerce_string(section).lower()
    if normalized_section not in SECTION_KEYS:
        raise SystemConfigValidationError("section 非法，仅支持 {}".format(" | ".join(SECTION_KEYS)))

    tested_at = datetime.now(timezone.utc).isoformat()
    effective = build_effective_section_config(normalized_section, data)[1]

    if normalized_section == _LLM_PROFILES_SECTION:
        requested_profile_id = _coerce_string((data or {}).get("profile_id"))
        if requested_profile_id:
            profile = next(
                (item for item in (effective.get("profiles") or []) if item.get("id") == requested_profile_id),
                None,
            )
        else:
            default_main_ref = effective.get("default_main_profile_ref")
            default_main_id = default_main_ref.get("id") if isinstance(default_main_ref, dict) else None
            profile = next(
                (item for item in (effective.get("profiles") or []) if item.get("id") == default_main_id),
                None,
            )
        if not profile:
            raise SystemConfigValidationError("未找到要校验的模型配置")
        effective = profile

    if normalized_section in {"llm", _LLM_PROFILES_SECTION}:
        base_url = _coerce_string(effective.get("base_url"))
        model = _coerce_string(effective.get("model"))
        api_key = _coerce_string(effective.get("api_key"))
        if not base_url or not model:
            raise SystemConfigValidationError(f"{normalized_section} 需要填写 base_url 和 model")
        if not api_key:
            return {"success": False, "message": "API Key 未配置，无法测试 LLM 连接", "tested_at": tested_at}

        try:
            response = requests.post(
                "{}/chat/completions".format(base_url.rstrip("/")),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(api_key),
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK only."}],
                    "temperature": 0,
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=15,
            )
            response.raise_for_status()
            return {
                "success": True,
                "message": "LLM 连接成功，模型可用：{}".format(model),
                "tested_at": tested_at,
            }
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            detail = ""
            try:
                body = exc.response.json() if exc.response is not None else {}
                if isinstance(body, dict):
                    detail = _coerce_string(body.get("message") or body.get("error", ""))
            except Exception:
                detail = _coerce_string(getattr(exc.response, "text", ""))
            message = "LLM 连接失败（HTTP {}）".format(status_code)
            if detail:
                message = "{}：{}".format(message, detail[:200])
            return {"success": False, "message": message, "tested_at": tested_at}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "message": "LLM 请求失败：{}".format(str(exc)), "tested_at": tested_at}

    if normalized_section == "agent":
        try:
            from agent.core.jiuwen.smoke import run_jiuwen_smoke

            smoke_result = run_jiuwen_smoke(
                agent_config=_agent_config_for_connection_check(effective),
                agent_mode="quick",
                register_tools=_validate_jiuwen_tools_without_runtime_delegates,
            )
            tool_count = int(smoke_result.get("registered_tool_count") or 0)
            return {
                "success": True,
                "message": "Jiuwen ReAct 主链路可用，已校验 {} 个工具".format(tool_count),
                "tested_at": tested_at,
            }
        except Exception as exc:
            return {
                "success": False,
                "message": "Jiuwen ReAct 主链路不可用：{}".format(str(exc)),
                "tested_at": tested_at,
            }

    if normalized_section == "web_search":
        if not bool(effective.get("enabled", False)):
            return {"success": True, "message": "联网搜索已关闭", "tested_at": tested_at}
        provider = _coerce_string(effective.get("provider")).lower()
        if provider != "tavily":
            return {"success": False, "message": "不支持的联网搜索 provider: {}".format(provider), "tested_at": tested_at}
        api_key = _coerce_string(effective.get("api_key"))
        if not api_key:
            return {"success": False, "message": "联网搜索 API Key 未配置", "tested_at": tested_at}
        base_url = (_coerce_string(effective.get("base_url")) or "https://api.tavily.com").rstrip("/")
        try:
            response = requests.post(
                "{}/search".format(base_url),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {}".format(api_key),
                },
                json={
                    "query": "connection test",
                    "max_results": 1,
                    "search_depth": _coerce_string(effective.get("search_depth")) or "basic",
                    "include_answer": False,
                    "include_raw_content": False,
                },
                timeout=min(max(1, int(effective.get("timeout_seconds") or 20)), 15),
            )
            response.raise_for_status()
            return {"success": True, "message": "联网搜索连接成功", "tested_at": tested_at}
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            detail = ""
            try:
                body = exc.response.json() if exc.response is not None else {}
                if isinstance(body, dict):
                    detail = _coerce_string(body.get("message") or body.get("detail") or body.get("error", ""))
            except Exception:
                detail = _coerce_string(getattr(exc.response, "text", ""))
            message = "联网搜索连接失败（HTTP {}）".format(status_code)
            if detail:
                message = "{}：{}".format(message, detail[:200])
            return {"success": False, "message": message, "tested_at": tested_at}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "message": "联网搜索请求失败：{}".format(str(exc)), "tested_at": tested_at}

    if normalized_section == "skill_evolution":
        if not bool(effective.get("enabled", False)):
            return {"success": True, "message": "本体进化已关闭", "tested_at": tested_at}
        base_url = _coerce_string(effective.get("base_url")).rstrip("/")
        if not base_url:
            return {"success": False, "message": "未配置 skill-evolver 服务地址（base_url）", "tested_at": tested_at}
        # Lightweight reachability probe: GET skill-evolver's documented
        # /api/status endpoint (returns Evolver + Evaluator config status) with
        # a short timeout. 2xx = up and responding; any other HTTP response =
        # reachable but degraded; a connection error / timeout = not reachable.
        # Full validation still happens on the actual /start call.
        probe_url = "{}/api/status".format(base_url)
        try:
            timeout = min(max(1, int(effective.get("timeout_seconds") or 30)), 5)
        except (TypeError, ValueError):
            timeout = 5
        try:
            response = requests.get(probe_url, headers={"Accept": "application/json"}, timeout=timeout)
            code = response.status_code
            if 200 <= code < 300:
                return {"success": True, "message": "skill-evolver 连接成功", "tested_at": tested_at}
            return {
                "success": True,
                "message": "skill-evolver 可达（HTTP {}，仅作连通性校验）".format(code),
                "tested_at": tested_at,
            }
        except requests.exceptions.RequestException as exc:
            return {"success": False, "message": "无法连接 skill-evolver：{}".format(str(exc)), "tested_at": tested_at}

    return {
        "success": True,
        "message": "配置校验通过",
        "tested_at": tested_at,
    }
