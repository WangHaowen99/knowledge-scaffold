"""Build whitelisted environment variables for sandbox script execution."""

from __future__ import annotations

from typing import Mapping


_DETERMINISTIC_ENV_NAMES = frozenset(
    {
        "PYTHONHASHSEED",
        "PYTHONDONTWRITEBYTECODE",
        "TZ",
        "LC_ALL",
        "LANG",
        "DETERMINISTIC_EXECUTION",
        "ONTOLOGY_ID",
        "ONTOLOGY_VERSION",
        "PACKAGE_REVISION",
        "RUN_ARTIFACT_DIR",
        "RUN_ARTIFACT_MAX_FILE_BYTES",
        "RUN_ARTIFACT_MAX_TOTAL_BYTES",
        "RUN_ARTIFACT_MAX_COUNT",
    }
)


def _is_allowed_override(key: str) -> bool:
    from agent.tools.business_data.script_env import SCRIPT_BUSINESS_DATA_CONNECTION_ENV

    return key == SCRIPT_BUSINESS_DATA_CONNECTION_ENV or key in _DETERMINISTIC_ENV_NAMES


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
            if not _is_allowed_override(name):
                continue
            env[name] = str(value)
    return env
