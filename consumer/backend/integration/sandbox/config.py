"""Sandbox integration configuration loaded from cold-start environment and defaults."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class SandboxConfig:
    """Sandbox settings. Field defaults are the single source of truth."""

    enabled: bool = False
    jiuwenbox_url: str = "unix:///run/jiuwenbox/jiuwenbox.sock"
    fallback_local: bool = True
    memory_max: str = "512M"
    cpu_max: float = 1.0
    pids_max: int = 64
    client_timeout_seconds: float = 30.0
    python_bin: str = "/usr/bin/python3"
    sandbox_runtime_root: str = "/app/sandbox-runtime"
    data_proxy_base_url: str = "http://jiuwenbox:8322"
    secret_allow_plain_http: bool = True


# Cold-start environment variable backing for operator-tunable fields.
# Deployment topology and image-contract fields intentionally have no env
# override; keep those defaults aligned with docker-compose and the Onto-owned
# jiuwenbox image.
_ENV_FOR_FIELD = {
    "enabled": "SANDBOX_ENABLED",
    "fallback_local": "SANDBOX_FALLBACK_LOCAL",
    "memory_max": "SANDBOX_MEMORY_MAX",
    "cpu_max": "SANDBOX_CPU_MAX",
    "pids_max": "SANDBOX_PIDS_MAX",
    "client_timeout_seconds": "SANDBOX_CLIENT_TIMEOUT",
    "secret_allow_plain_http": "SANDBOX_SECRET_ALLOW_PLAIN_HTTP",
}

# String fields whose value should be trimmed of a trailing slash.
_PATH_FIELDS = frozenset({"sandbox_runtime_root", "data_proxy_base_url"})


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _coerce_str(value: Any, default: str, *, rstrip_slash: bool = False) -> str:
    if value is None:
        return default
    text = str(value).strip()
    if rstrip_slash:
        text = text.rstrip("/")
    return text or default


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def load_sandbox_config() -> SandboxConfig:
    """Load sandbox settings from cold-start env > field defaults."""
    values: dict[str, Any] = {}
    for field in dataclasses.fields(SandboxConfig):
        default = field.default
        env_name = _ENV_FOR_FIELD.get(field.name)
        raw = os.getenv(env_name) if env_name else None
        ftype = field.type
        if ftype is bool or ftype == "bool":
            values[field.name] = _coerce_bool(raw, default)
        elif ftype is int or ftype == "int":
            values[field.name] = _coerce_int(raw, default)
        elif ftype is float or ftype == "float":
            values[field.name] = _coerce_float(raw, default)
        else:
            values[field.name] = _coerce_str(
                raw, default, rstrip_slash=field.name in _PATH_FIELDS
            )
    return SandboxConfig(**values)
