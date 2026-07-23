"""Build whitelisted environment variables for sandbox and local script execution."""

from __future__ import annotations

import os
import re
from typing import Mapping


_LOCAL_HOST_ENV_ALLOWLIST = frozenset({
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USER",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
})

_SCRIPT_CONFIG_ENV_VALIDATORS = {
    "DOMAIN_TABLE_PREFIX": re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\.?$"),
}


def _is_allowed_script_config(key: str, value: object) -> bool:
    validator = _SCRIPT_CONFIG_ENV_VALIDATORS.get(key)
    if validator is None:
        return False
    normalized = str(value or "").strip()
    return bool(normalized and validator.fullmatch(normalized))


def collect_script_config_env(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return non-sensitive script config explicitly allowed in sandboxes."""
    values = dict(os.environ if source is None else source)
    return {
        key: str(value).strip()
        for key, value in values.items()
        if _is_allowed_script_config(str(key), value)
    }


def _is_allowed_override(key: str, value: object) -> bool:
    from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV

    return key == SCRIPT_BUSINESS_DATA_CONNECTION_ENV or _is_allowed_script_config(
        key,
        value,
    )


def _override_value(key: str, value: object) -> str:
    if key in _SCRIPT_CONFIG_ENV_VALIDATORS:
        return str(value).strip()
    return str(value)


def build_script_env(
    *,
    env_overrides: Mapping[str, str] | None = None,
    ontology_dir: str | None = None,
    runtime_python_path: str = "/app/sandbox-runtime/python",
) -> dict[str, str]:
    """Return the minimal env dict passed into jiuwenbox exec."""
    pythonpath_parts = [runtime_python_path]
    if ontology_dir:
        pythonpath_parts.append(str(ontology_dir))
    env: dict[str, str] = {
        "PYTHONPATH": ":".join(pythonpath_parts),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                continue
            name = str(key)
            if not _is_allowed_override(name, value):
                continue
            env[name] = _override_value(name, value)
    return env


def build_local_script_env(
    *,
    base_env: Mapping[str, str] | None = None,
    env_overrides: Mapping[str, str] | None = None,
    pythonpath_parts: list[str] | None = None,
) -> dict[str, str]:
    """Return the minimal host env dict passed into a local script subprocess."""
    source = dict(os.environ if base_env is None else base_env)
    env = {
        key: str(value)
        for key, value in source.items()
        if key in _LOCAL_HOST_ENV_ALLOWLIST and value not in {None, ""}
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(item) for item in (pythonpath_parts or []) if str(item or "").strip()]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(collect_script_config_env(source))
    if env_overrides:
        for key, value in env_overrides.items():
            if value is None:
                continue
            name = str(key)
            if not _is_allowed_override(name, value):
                continue
            env[name] = _override_value(name, value)
    return env
