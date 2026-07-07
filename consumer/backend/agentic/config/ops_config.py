"""Deployment operations configuration for onto-agent.

This module holds runtime policy that is intentionally not exposed through the
system settings UI/API. Precedence is field-level:
ops_config.json > environment variable > built-in default.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
PROJECT_ROOT_ENV = "PROJECT_ROOT"
OPS_CONFIG_PATH_ENV = "OPS_CONFIG_PATH"


def _resolve_default_runtime_root(backend_root: Path | None = None) -> Path:
    resolved_backend_root = (backend_root or BACKEND_ROOT).resolve()
    repo_root = resolved_backend_root.parent
    if repo_root == Path("/"):
        return resolved_backend_root / ".onto-runtime"
    return repo_root / ".onto-runtime"


ONTO_RUNTIME_ROOT = _resolve_default_runtime_root()


def _resolve_config_runtime_root() -> Path:
    configured_root = os.getenv(PROJECT_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser()
    return ONTO_RUNTIME_ROOT


def _resolve_ops_config_path() -> Path:
    configured_path = os.getenv(OPS_CONFIG_PATH_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    return _resolve_config_runtime_root() / "config" / "ops_config.json"


_CONFIG_LOCK = Lock()

OPS_SECTION_KEYS = ("limits",)
_SECTION_FIELDS: dict[str, dict[str, Any]] = {
    "limits": {
        "run_tps": {"env": "AGENT_RUN_TPS", "type": "int", "default": 50},
        "user_max_count": {"env": "USER_MAX_COUNT", "type": "int", "default": 10},
        "workspace_max_count": {"env": "WORKSPACE_MAX_COUNT", "type": "int", "default": 5},
        "ontology_max_count_per_workspace": {"env": "ONTOLOGY_MAX_COUNT_PER_WORKSPACE", "type": "int", "default": 10},
        "ontology_object_max_count": {"env": "ONTOLOGY_OBJECT_MAX_COUNT", "type": "int", "default": 200},
        "ontology_operation_max_count": {"env": "ONTOLOGY_OPERATION_MAX_COUNT", "type": "int", "default": 100},
        "entity_attribute_max_count": {"env": "ENTITY_ATTRIBUTE_MAX_COUNT", "type": "int", "default": 100},
        "data_connection_configs_max_per_workspace": {"env": "DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE", "type": "int", "default": 10},
        "model_profile_max_count": {"env": "MODEL_PROFILE_MAX_COUNT", "type": "int", "default": 10},
        "model_input_token_limit": {"env": "MODEL_INPUT_TOKEN_LIMIT", "type": "int", "default": 50000},
        "model_output_token_limit": {"env": "MODEL_OUTPUT_TOKEN_LIMIT", "type": "int", "default": 100000},
        "session_max_per_user_workspace": {"env": "SESSION_MAX_PER_USER_WORKSPACE", "type": "int", "default": 100},
        "operation_max_chars": {"env": "ONTOLOGY_OPERATION_MAX_CHARS", "type": "int", "default": 15000},
        "agent_log_retention_days": {"env": "AGENT_LOG_RETENTION_DAYS", "type": "int", "default": 180},
        "agent_log_compress_old": {"env": "AGENT_LOG_COMPRESS_OLD", "type": "bool", "default": True},
    },
}


class OpsConfigValidationError(ValueError):
    """Raised when operations configuration is invalid."""


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
    raise OpsConfigValidationError(f"{field_name} 必须是布尔值")


def _coerce_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise OpsConfigValidationError(f"{field_name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OpsConfigValidationError(f"{field_name} 必须是整数") from exc


def _read_json_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return raw if isinstance(raw, dict) else {}


def _get_env_value(field_spec: dict[str, Any]) -> Any:
    env_name = field_spec.get("env")
    if env_name and os.getenv(env_name) is not None:
        return os.getenv(env_name)
    return None


def _coerce_section_value(field_name: str, field_spec: dict[str, Any], value: Any) -> Any:
    field_type = field_spec.get("type")
    if field_type == "bool":
        return _coerce_bool(value, field_name=field_name)
    if field_type == "int":
        return _coerce_int(value, field_name=field_name)
    return _coerce_string(value)


def _process_section(
    specs: dict[str, Any],
    raw_data: dict[str, Any],
) -> dict[str, Any]:
    result = {}
    for field_name, field_spec in specs.items():
        val = raw_data.get(field_name)
        if val is None:
            env_val = _get_env_value(field_spec)
            if env_val is not None:
                val = env_val
        if val is None:
            val = field_spec.get("default")
        result[field_name] = _coerce_section_value(field_name, field_spec, val)
    return result


def load_ops_config() -> dict[str, dict[str, Any]]:
    with _CONFIG_LOCK:
        document = _read_json_document(_resolve_ops_config_path())
        return {
            section: _process_section(
                _SECTION_FIELDS[section],
                document.get(section) if isinstance(document.get(section), dict) else {},
            )
            for section in OPS_SECTION_KEYS
        }
