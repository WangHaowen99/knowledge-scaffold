from __future__ import annotations

from typing import Any, Callable, Mapping
from urllib.parse import quote

import requests

from integration.platform.config import PlatformConfig
from integration.platform.exporter import _environment_headers, _join_url


PlatformGetTransport = Callable[[str, dict[str, str], float], dict[str, Any]]


OPS_CONFIG_LIMIT_FIELDS = {
    "run_tps",
    "workspace_max_count",
    "ontology_max_count_per_workspace",
    "ontology_object_max_count",
    "ontology_operation_max_count",
    "entity_attribute_max_count",
    "data_connection_configs_max_per_workspace",
    "model_profile_max_count",
    "model_output_token_limit",
    "session_max_per_user_workspace",
    "operation_max_chars",
    "agent_log_retention_days",
    "agent_log_compress_old",
}

LEGACY_LIMIT_FIELD_MAP = {
    "data_connection_max_count_per_workspace": "data_connection_configs_max_per_workspace",
    "session_retention_count": "session_max_per_user_workspace",
    "log_retention_days": "agent_log_retention_days",
}


def fetch_policy_snapshot(
    config: PlatformConfig | None = None,
    *,
    transport: PlatformGetTransport | None = None,
) -> dict[str, Any]:
    resolved = config or PlatformConfig.from_env()
    if not resolved.available:
        raise RuntimeError("onto-platform connector is not configured")
    request = transport or _default_get
    url = _join_url(
        resolved.base_url,
        "/api/v1/runtime/policy-snapshot?runtime_id={}".format(
            quote(resolved.instance_id, safe="")
        ),
    )
    return request(
        url,
        _environment_headers(resolved, accept_json=True),
        resolved.timeout_seconds,
    )


def map_policy_snapshot_to_agent_config(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    limits = snapshot.get("limits") if isinstance(snapshot.get("limits"), dict) else {}
    mapped_limits = {
        field_name: limits[field_name]
        for field_name in OPS_CONFIG_LIMIT_FIELDS
        if field_name in limits
    }
    for platform_field, agent_field in LEGACY_LIMIT_FIELD_MAP.items():
        if agent_field not in mapped_limits and platform_field in limits:
            mapped_limits[agent_field] = limits[platform_field]
    return {
        "snapshot_key": snapshot.get("snapshot_key"),
        "version": snapshot.get("version"),
        "checksum": snapshot.get("checksum"),
        "limits": mapped_limits,
    }


def _default_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()
