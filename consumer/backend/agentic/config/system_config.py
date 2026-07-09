"""Persistent system configuration for onto-agent.

This module provides a UI-editable config layer with the following precedence:
saved system config > environment variables > built-in defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any
from datetime import datetime, timezone

import requests

from .ops_config import load_ops_config
from ..security.secrets import decrypt_secret, encrypt_secret, is_encrypted_secret


BACKEND_ROOT = Path(__file__).resolve().parents[2]
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
LEGACY_CONFIG_PATH = BACKEND_ROOT / "config" / "system_config.json"


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
    "task_llm",
    "storage",
    "auth",
    "agent",
    "memory",
    "observability",
    "web_search",
    "direct_execution",
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
    "task_llm": {
        "base_url": {"env": "TASK_LLM_BASE_URL", "type": "str", "default": ""},
        "model": {"env": "TASK_LLM_MODEL", "type": "str", "default": ""},
        "api_key": {"env": "TASK_LLM_API_KEY", "type": "secret", "default": ""},
        "timeout": {"env": "TASK_LLM_TIMEOUT", "type": "int", "default": 600},
    },
    "storage": {
        # Chat and execution history are stored under project_root/workspaces/{id}/runtime/
        # and are managed by UserStoreManager via get_workspace_paths(); no separate config needed.
        "project_root": {"env": "PROJECT_ROOT", "type": "str", "default": str(ONTO_RUNTIME_ROOT)},
        "auth_users_file": {"env": "AUTH_USERS_FILE", "type": "str", "default": str(ONTO_RUNTIME_ROOT / "users.json")},
    },
    "auth": {
        "auth_secret": {"env": "AUTH_SECRET", "type": "secret", "default": ""},
        "auth_token_ttl_seconds": {"env": "AUTH_TOKEN_TTL", "type": "int", "default": 43200},
        # local=内置 AuthManager；remote=对接独立 auth server（施工步骤 4 起可用）
        "auth_mode": {"env": "AUTH_MODE", "type": "str", "default": "local"},
    },
    "agent": {
        "llm_connection_max_retries": {"env": "AGENT_LLM_CONNECTION_MAX_RETRIES", "type": "int", "default": 3},
        "main_agent_max_iterations": {"env": "AGENT_MAIN_LOOP_MAX_ITERATIONS", "type": "int", "default": 50},
        "sub_agent_max_iterations": {"env": "AGENT_SUB_AGENT_MAX_ITERATIONS", "type": "int", "default": 20},
        "sub_agent_max_concurrency": {"env": "AGENT_SUB_AGENT_MAX_CONCURRENCY", "type": "int", "default": 6},
        "sub_agent_max_depth": {"env": "AGENT_SUB_AGENT_MAX_DEPTH", "type": "int", "default":10},
        "parallel_default_max_workers": {"env": "AGENT_PARALLEL_DEFAULT_MAX_WORKERS", "type": "int", "default": 10},
        "context_max_rounds": {"env": "AGENT_CONTEXT_MAX_ROUNDS", "type": "int", "default": 10},
        "context_prune_tools": {"env": "AGENT_CONTEXT_PRUNE_TOOLS", "type": "str", "default": ""},
        # Operation evolution thresholds (see docs/operation-evolution.md §七)
        "evolution_min_occurrences": {"env": "EVO_MIN_OCCURRENCES", "type": "int", "default": 3},
        "evolution_min_applied": {"env": "EVO_MIN_APPLIED", "type": "int", "default": 2},
        "evolution_max_rejection": {"env": "EVO_MAX_REJECTION", "type": "int", "default": 0},
        "evolution_passive_reflection_enabled": {"env": "EVO_PASSIVE_REFLECTION_ENABLED", "type": "bool", "default": False},
        "evolution_patch_auto_apply": {"env": "EVO_PATCH_AUTO_APPLY", "type": "bool", "default": False},
        "evolution_patch_cooldown_sec": {"env": "EVO_PATCH_COOLDOWN_SEC", "type": "int", "default": 3600},
        "evolution_sweep_interval_sec": {"env": "EVO_SWEEP_INTERVAL_SEC", "type": "int", "default": 1800},
        "execute_script_timeout": {"env": "EXECUTE_SCRIPT_TIMEOUT", "type": "int", "default": 300},
        "runtime_warmup_enabled": {"env": "AGENT_RUNTIME_WARMUP_ENABLED", "type": "bool", "default": True},
    },
    "memory": {
        "mode": {"env": "MEMORY_MODE", "type": "str", "default": "filesystem"},
        "recall_topk": {"env": "AGENT_MEMORY_RECALL_TOPK", "type": "int", "default": 3},
        "recall_threshold": {"env": "AGENT_MEMORY_RECALL_THRESHOLD", "type": "str", "default": ""},
        "workspace_root": {"env": "MEMORY_WORKSPACE_ROOT", "type": "str", "default": ""},
    },
    "observability": {
        "enabled": {"type": "bool", "default": False},
        "mode": {"type": "str", "default": "local"},
        "local": {
            "retention_days": {"type": "int", "default": 30},
        },
        "langfuse": {
            "public_key": {"type": "str", "default": ""},
            "secret_key": {"type": "secret", "default": ""},
            "base_url": {"type": "str", "default": ""},
            "debug": {"type": "bool", "default": False},
        },
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
}

_CLEAR_FIELDS = {
    "llm": {"clear_api_key": "api_key"},
    "task_llm": {"clear_api_key": "api_key"},
    "auth": {"clear_auth_secret": "auth_secret"},
    "web_search": {"clear_api_key": "api_key"},
}
_OBSERVABILITY_CLEAR_SECRET_FIELD = "clear_langfuse_secret_key"


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


def _normalize_profile_id(value: Any, *, field_name: str = "profile_id") -> str:
    profile_id = _coerce_string(value)
    if not profile_id:
        raise SystemConfigValidationError(f"{field_name} 不能为空")
    if any(ch.isspace() for ch in profile_id):
        raise SystemConfigValidationError(f"{field_name} 不能包含空白字符")
    if len(profile_id) > 80:
        raise SystemConfigValidationError(f"{field_name} 长度不能超过 80")
    return profile_id


def _coerce_llm_provider(value: Any) -> str:
    provider = _coerce_string(value).lower() or "openai_compatible"
    if provider not in _SUPPORTED_LLM_PROVIDERS:
        raise SystemConfigValidationError(
            "provider 仅支持 {}".format(" | ".join(sorted(_SUPPORTED_LLM_PROVIDERS)))
        )
    return provider


def _read_json_document(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, dict) else {}


def _should_migrate_legacy_config() -> bool:
    if os.getenv(SYSTEM_CONFIG_PATH_ENV):
        return False
    return (
        CONFIG_PATH == DEFAULT_CONFIG_PATH
        and LEGACY_CONFIG_PATH != CONFIG_PATH
        and LEGACY_CONFIG_PATH.exists()
    )


def _migrate_legacy_config_unlocked() -> dict[str, Any]:
    document = _read_json_document(LEGACY_CONFIG_PATH)
    raw_config = {
        section_key: dict(document.get(section_key) or {})
        for section_key in SECTION_KEYS
    }
    _write_config_document_unlocked(raw_config)
    return _read_json_document(CONFIG_PATH)


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


def _read_config_document_unlocked() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        document = _read_json_document(CONFIG_PATH)
        if _document_has_plaintext_secrets(document):
            raw_config = {
                section_key: dict(document.get(section_key) or {})
                for section_key in SECTION_KEYS
            }
            _write_config_document_unlocked(raw_config)
            return _read_json_document(CONFIG_PATH)
        return document
    if _should_migrate_legacy_config():
        return _migrate_legacy_config_unlocked()
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
    return decrypt_secret(_coerce_string(value), context=_secret_context(path))


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

    if "id" in raw_profile:
        profile["id"] = _normalize_profile_id(raw_profile.get("id"), field_name="profiles[{}].id".format(index))
    else:
        profile["id"] = _normalize_profile_id(profile.get("id"), field_name="profiles[{}].id".format(index))

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


def _normalize_llm_profiles_section(
    raw_section: Any,
    *,
    legacy_llm: dict[str, Any],
    current_raw_section: dict[str, Any] | None = None,
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
            )
            if profile["id"] in seen_ids:
                raise SystemConfigValidationError("llm_profiles.profiles 中存在重复 id: {}".format(profile["id"]))
            seen_ids.add(profile["id"])
            profiles.append(profile)
        if not profiles:
            raise SystemConfigValidationError("llm_profiles.profiles 至少需要一个模型")

    requested_default = section.get("default_profile_id", current_section.get("default_profile_id"))
    default_profile_id = _coerce_string(requested_default)
    profile_ids = {profile["id"] for profile in profiles}
    max_profiles = max(1, int(load_ops_config()["limits"]["model_profile_max_count"]))
    if len(profiles) > max_profiles:
        raise SystemConfigValidationError("模型数量已达上限: {}".format(max_profiles))
    if not profiles:
        default_profile_id = ""
    elif default_profile_id not in profile_ids:
        enabled_profile = next((profile for profile in profiles if profile.get("enabled")), profiles[0])
        default_profile_id = enabled_profile["id"]

    return {
        "default_profile_id": default_profile_id,
        "profiles": profiles,
    }


def _merge_observability_update(
    data: dict[str, Any],
    current_raw_section: dict[str, Any],
    current_effective_section: dict[str, Any],
    *,
    allow_unknown_fields: bool = False,
) -> dict[str, Any]:
    """Merge UI/API observability updates while preserving nested secrets."""
    top_level_fields = set(_SECTION_FIELDS["observability"].keys())
    allowed_fields = top_level_fields | {_OBSERVABILITY_CLEAR_SECRET_FIELD}
    unknown_fields = set(data.keys()) - allowed_fields
    if unknown_fields and not allow_unknown_fields:
        bad_fields = ", ".join(sorted(unknown_fields))
        raise SystemConfigValidationError(f"observability 不支持字段: {bad_fields}")

    local_specs = _SECTION_FIELDS["observability"]["local"]
    langfuse_specs = _SECTION_FIELDS["observability"]["langfuse"]
    merged = {
        field_name: current_raw_section[field_name]
        for field_name in ("enabled", "mode")
        if field_name in current_raw_section
    }
    current_local = current_raw_section.get("local")
    if isinstance(current_local, dict):
        merged["local"] = {
            field_name: current_local[field_name]
            for field_name in local_specs
            if field_name in current_local
        }
    current_langfuse = current_raw_section.get("langfuse")
    if isinstance(current_langfuse, dict):
        merged["langfuse"] = {
            field_name: current_langfuse[field_name]
            for field_name in langfuse_specs
            if field_name in current_langfuse
        }

    for field_name in ("enabled", "mode"):
        if field_name not in data:
            continue
        merged[field_name] = _coerce_section_value(
            field_name,
            _SECTION_FIELDS["observability"][field_name],
            data[field_name],
            path=["observability", field_name],
        )

    if "local" in data:
        raw_local = data.get("local")
        if not isinstance(raw_local, dict):
            raise SystemConfigValidationError("observability.local 必须是对象")
        unknown_local_fields = set(raw_local.keys()) - set(local_specs.keys())
        if unknown_local_fields and not allow_unknown_fields:
            bad_fields = ", ".join(sorted(unknown_local_fields))
            raise SystemConfigValidationError(f"observability.local 不支持字段: {bad_fields}")

        merged_local = dict(
            merged.get("local")
            if isinstance(merged.get("local"), dict)
            else {}
        )
        for field_name, field_spec in local_specs.items():
            if field_name not in raw_local:
                continue
            merged_local[field_name] = _coerce_section_value(
                field_name,
                field_spec,
                raw_local[field_name],
                path=["observability", "local", field_name],
            )
            if field_name == "retention_days" and int(merged_local[field_name]) < 1:
                raise SystemConfigValidationError("observability.local.retention_days 必须大于等于 1")
        merged["local"] = merged_local

    if "langfuse" in data:
        raw_langfuse = data.get("langfuse")
        if not isinstance(raw_langfuse, dict):
            raise SystemConfigValidationError("observability.langfuse 必须是对象")
        unknown_langfuse_fields = set(raw_langfuse.keys()) - set(langfuse_specs.keys())
        if unknown_langfuse_fields and not allow_unknown_fields:
            bad_fields = ", ".join(sorted(unknown_langfuse_fields))
            raise SystemConfigValidationError(f"observability.langfuse 不支持字段: {bad_fields}")

        merged_langfuse = dict(
            merged.get("langfuse")
            if isinstance(merged.get("langfuse"), dict)
            else {}
        )
        for field_name, field_spec in langfuse_specs.items():
            if field_name not in raw_langfuse:
                continue
            value = _coerce_section_value(
                field_name,
                field_spec,
                raw_langfuse[field_name],
                path=["observability", "langfuse", field_name],
            )
            if field_name == "secret_key" and value == "":
                raise SystemConfigValidationError(
                    "secret_key 不能为空；如需清空请使用 clear_langfuse_secret_key"
                )
            merged_langfuse[field_name] = value
        merged["langfuse"] = merged_langfuse

    if _OBSERVABILITY_CLEAR_SECRET_FIELD in data and _coerce_bool(
        data.get(_OBSERVABILITY_CLEAR_SECRET_FIELD),
        field_name=_OBSERVABILITY_CLEAR_SECRET_FIELD,
    ):
        merged_langfuse = dict(
            merged.get("langfuse")
            if isinstance(merged.get("langfuse"), dict)
            else current_effective_section.get("langfuse", {})
        )
        merged_langfuse["secret_key"] = ""
        merged["langfuse"] = merged_langfuse

    return merged


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


def load_system_config() -> dict[str, dict[str, Any]]:
    with _CONFIG_LOCK:
        return _load_system_config_unlocked()


def _load_system_config_unlocked() -> dict[str, dict[str, Any]]:
    raw = _load_raw_system_config_unlocked()
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
        if section == "web_search":
            effective[section] = _normalize_web_search_effective(effective[section])

    # 特殊处理 llm_profiles
    effective[_LLM_PROFILES_SECTION] = _normalize_llm_profiles_section(
        raw.get(_LLM_PROFILES_SECTION),
        legacy_llm=effective.get("llm", {}),
        current_raw_section=raw.get(_LLM_PROFILES_SECTION),
    )
    return effective


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
        default_profile_id = _coerce_string(section_config.get("default_profile_id"))
        return {
            "default_profile_id": default_profile_id,
            "profiles": [
                {
                    **_public_llm_profile(profile, include_connection=True),
                    "is_default": profile.get("id") == default_profile_id,
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
        "default_profile_id": "",
        "profiles": [],
    }
    return template


def _merge_section_update(
    section: str,
    data: dict[str, Any],
    current_raw_section: dict[str, Any],
    current_effective_section: dict[str, Any],
    *,
    allow_unknown_fields: bool = False,
) -> dict[str, Any]:
    allowed_fields = set(_SECTION_FIELDS[section].keys())
    clear_fields = _CLEAR_FIELDS.get(section, {})
    unknown_fields = set(data.keys()) - allowed_fields - set(clear_fields.keys())
    if unknown_fields and not allow_unknown_fields:
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

    if section == "task_llm":
        # Keep task_llm semantics explicit in storage: save the effective values the user saw/edited.
        for field_name in _SECTION_FIELDS[section]:
            if field_name not in merged and field_name in current_effective_section:
                merged[field_name] = current_effective_section[field_name]
    if section == "web_search":
        merged = _normalize_web_search_effective(merged)

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
) -> None:
    # 现在的 config 已经是嵌套好的，直接按 SECTION_KEYS 过滤保存即可
    document = _encrypt_config_document(config)
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
        )
        return normalized_section, next_effective
    if normalized_section == "observability":
        merged_raw = _merge_observability_update(
            data or {},
            raw.get(normalized_section, {}),
            config[normalized_section],
        )
        next_effective = _process_fields_recursive(
            normalized_section,
            _SECTION_FIELDS[normalized_section],
            merged_raw,
            [normalized_section],
        )
        return normalized_section, next_effective

    merged_raw = _merge_section_update(
        normalized_section,
        data or {},
        raw.get(normalized_section, {}),
        config[normalized_section],
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
    if normalized_section == "web_search":
        next_effective = _normalize_web_search_effective(next_effective)
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
            )
        elif normalized_section == "observability":
            raw_config[normalized_section] = _merge_observability_update(
                data,
                raw_config[normalized_section],
                effective[normalized_section],
            )
        else:
            raw_config[normalized_section] = _merge_section_update(
                normalized_section,
                data,
                raw_config[normalized_section],
                effective[normalized_section],
            )
        _write_config_document_unlocked(raw_config)

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
                )
            elif section == "observability":
                raw_config[section] = _merge_observability_update(
                    section_data,
                    raw_config[section],
                    effective[section],
                    allow_unknown_fields=True,
                )
            else:
                raw_config[section] = _merge_section_update(
                    section,
                    section_data,
                    raw_config[section],
                    effective[section],
                    allow_unknown_fields=True,
                )
        _write_config_document_unlocked(raw_config)

    return imported_sections, get_public_system_config()


def resolve_llm_profile_config(
    profile_id: str | None = None,
    *,
    allow_default_fallback: bool = True,
) -> dict[str, Any]:
    """Resolve an enabled model profile to a private LLM config dictionary."""
    config = load_system_config()
    profiles_config = config.get(_LLM_PROFILES_SECTION) or {}
    profiles = list(profiles_config.get("profiles") or [])
    if not profiles and _is_llm_configured(config["llm"]):
        profiles = [_legacy_llm_profile(config["llm"])]

    requested_id = _coerce_string(profile_id)
    default_id = _coerce_string(profiles_config.get("default_profile_id"))

    def find_enabled(target_id: str) -> dict[str, Any] | None:
        for profile in profiles:
            if profile.get("id") == target_id and profile.get("enabled"):
                return dict(profile)
        return None

    resolved = find_enabled(requested_id) if requested_id else None
    if resolved is None and allow_default_fallback:
        resolved = find_enabled(default_id)
        if resolved is None:
            resolved = next((dict(profile) for profile in profiles if profile.get("enabled")), None)
    if resolved is None:
        raise SystemConfigValidationError("模型配置不存在或未启用: {}".format(requested_id or default_id or "default"))
    return resolved


def get_public_llm_profiles() -> dict[str, Any]:
    """Return enabled model profiles for chat model selection."""
    profiles_config = load_system_config().get(_LLM_PROFILES_SECTION) or {}
    default_profile_id = _coerce_string(profiles_config.get("default_profile_id"))
    profiles = [
        {
            **_public_llm_profile(profile, include_connection=False),
            "is_default": profile.get("id") == default_profile_id,
        }
        for profile in (profiles_config.get("profiles") or [])
        if profile.get("enabled") and _is_llm_configured(profile)
    ]
    if not any(profile.get("is_default") for profile in profiles) and profiles:
        profiles[0]["is_default"] = True
        default_profile_id = profiles[0]["id"]
    return {
        "default_profile_id": default_profile_id,
        "profiles": profiles,
    }


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
    default_profile_id = _coerce_string(profiles_config.get("default_profile_id"))
    if profiles:
        if not any(profile.get("id") == default_profile_id for profile in profiles):
            default_profile_id = _coerce_string(profiles[0].get("id"))
        return {
            "ready": True,
            "message": "模型配置已就绪",
            "missing_fields": [],
            "default_profile_id": default_profile_id,
            "profile_count": len(profiles),
        }
    return {
        "ready": False,
        "message": "请先配置可用模型",
        "missing_fields": ["llm_profiles.profiles"],
        "default_profile_id": "",
        "profile_count": 0,
    }


def _agent_config_for_connection_check(effective: dict[str, Any]) -> Any:
    from dataclasses import replace

    from .settings import AgentConfig

    base = AgentConfig.from_env()
    return replace(
        base,
        llm_connection_max_retries=int(effective["llm_connection_max_retries"]),
        main_agent_max_iterations=int(effective["main_agent_max_iterations"]),
        sub_agent_max_iterations=int(effective["sub_agent_max_iterations"]),
        sub_agent_max_concurrency=int(effective["sub_agent_max_concurrency"]),
        sub_agent_max_depth=int(effective["sub_agent_max_depth"]),
        parallel_default_max_workers=int(effective["parallel_default_max_workers"]),
        context_max_rounds=int(effective["context_max_rounds"]),
        context_prune_tools={
            item.strip()
            for item in _coerce_string(effective.get("context_prune_tools")).split(",")
            if item.strip()
        },
        execute_script_timeout=int(effective.get("execute_script_timeout") or 300),
        evolution_min_occurrences=int(effective.get("evolution_min_occurrences", 3)),
        evolution_min_applied=int(effective.get("evolution_min_applied", 2)),
        evolution_max_rejection=int(effective.get("evolution_max_rejection", 0)),
        evolution_passive_reflection_enabled=bool(effective.get("evolution_passive_reflection_enabled", False)),
        evolution_patch_auto_apply=bool(effective.get("evolution_patch_auto_apply", False)),
        evolution_patch_cooldown_sec=int(effective.get("evolution_patch_cooldown_sec", 3600)),
        evolution_sweep_interval_sec=int(effective.get("evolution_sweep_interval_sec", 1800)),
    )


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
            profile = next(
                (item for item in (effective.get("profiles") or []) if item.get("id") == effective.get("default_profile_id")),
                None,
            )
        if not profile:
            raise SystemConfigValidationError("未找到要校验的模型配置")
        effective = profile

    if normalized_section in {"llm", "task_llm", _LLM_PROFILES_SECTION}:
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
            from ..loop.jiuwen.smoke import run_jiuwen_smoke

            smoke_result = run_jiuwen_smoke(
                agent_config=_agent_config_for_connection_check(effective),
                agent_mode="quick",
            )
            tool_count = int(smoke_result.get("registered_tool_count") or 0)
            return {
                "success": True,
                "message": "Jiuwen ReAct 主链路可用，已注册 {} 个工具".format(tool_count),
                "tested_at": tested_at,
            }
        except Exception as exc:
            return {
                "success": False,
                "message": "Jiuwen ReAct 主链路不可用：{}".format(str(exc)),
                "tested_at": tested_at,
            }

    if normalized_section == "observability":
        if not bool(effective.get("enabled", False)):
            return {"success": True, "message": "观测已关闭", "tested_at": tested_at}
        mode = _coerce_string(effective.get("mode")).lower()
        if mode == "local":
            return {"success": True, "message": "本地性能日志已启用", "tested_at": tested_at}
        if mode != "langfuse":
            return {"success": False, "message": "不支持的观测 mode: {}".format(mode), "tested_at": tested_at}
        langfuse_config = effective.get("langfuse") if isinstance(effective.get("langfuse"), dict) else {}
        missing = [
            name
            for name in ("public_key", "secret_key", "base_url")
            if not _coerce_string(langfuse_config.get(name))
        ]
        if missing:
            return {
                "success": False,
                "message": "Langfuse 配置缺少字段: {}".format(", ".join(missing)),
                "tested_at": tested_at,
            }
        base_url = _coerce_string(langfuse_config.get("base_url")).rstrip("/")
        public_key = _coerce_string(langfuse_config.get("public_key"))
        secret_key = _coerce_string(langfuse_config.get("secret_key"))
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
                    detail = _coerce_string(body.get("message") or body.get("error", ""))
            except Exception:
                detail = _coerce_string(getattr(exc.response, "text", ""))
            message = "Langfuse 连接失败（HTTP {}）".format(status_code)
            if detail:
                message = "{}：{}".format(message, detail[:200])
            return {"success": False, "message": message, "tested_at": tested_at}
        except requests.exceptions.RequestException as exc:
            return {"success": False, "message": "Langfuse 请求失败：{}".format(str(exc)), "tested_at": tested_at}

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

    return {
        "success": True,
        "message": "配置校验通过",
        "tested_at": tested_at,
    }
