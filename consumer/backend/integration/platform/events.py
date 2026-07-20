from __future__ import annotations

import ipaddress
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from integration.observability.events import RuntimeEvent, RuntimeEventType
from ontology.ontologies import OntologyRegistry
from workspace.workspaces import get_current_workspace_id, get_workspace, get_workspace_ontologies_root
from integration.platform.config import PlatformConfig
from integration.platform.contracts import normalize_platform_status


RunContext = Mapping[str, Any] | None


def to_platform_event(
    event: RuntimeEvent,
    config: PlatformConfig,
    *,
    run_context: RunContext = None,
) -> dict[str, Any] | None:
    context = dict(run_context or {})
    if event.type in {
        RuntimeEventType.RUN_FINISHED,
        RuntimeEventType.RUN_FAILED,
        RuntimeEventType.RUN_CANCELLED,
    }:
        return _envelope(
            event,
            config,
            event_type="run.finished",
            run_context=context,
            payload=_run_finished_payload(event, context),
        )
    if event.type == RuntimeEventType.TOOL_FINISHED:
        return _envelope(
            event,
            config,
            event_type="tool.finished",
            run_context=context,
            payload=_tool_finished_payload(event),
        )
    if event.type == RuntimeEventType.LLM_FINISHED:
        return _envelope(
            event,
            config,
            event_type="model.usage",
            run_context=context,
            payload=_model_usage_payload(event),
        )
    return None


def build_heartbeat_event(
    config: PlatformConfig,
    *,
    status: str = "online",
    version: str | None = None,
    base_url: str | None = None,
    started_at: datetime | None = None,
    config_summary: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = occurred_at or datetime.now(UTC)
    timestamp_ms = int(timestamp.timestamp() * 1000)
    return {
        "event_id": f"{config.instance_id}:heartbeat:{timestamp_ms}",
        "event_type": "runtime.heartbeat",
        "schema_version": "v1",
        "occurred_at": _format_timestamp(timestamp_ms),
        "runtime_id": config.instance_id,
        "organization_id": config.organization_id,
        "workspace_id": None,
        "user_id": None,
        "session_id": None,
        "run_id": None,
        "trace_id": None,
        "payload": {
            "status": status,
            "version": version,
            "environment": config.environment_id or None,
            "hostname": _hostname(),
            "private_ips": _private_ips(),
            "base_url": base_url,
            "started_at": _format_datetime(started_at) if started_at else None,
            "config_summary": dict(config_summary or {}),
            "metrics": dict(metrics or {}),
        },
    }


def build_ontology_snapshot_event(
    config: PlatformConfig,
    *,
    workspace_id: str | None = None,
    ontology_root: Path | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    timestamp = occurred_at or datetime.now(UTC)
    timestamp_ms = int(timestamp.timestamp() * 1000)
    resolved_workspace_id = _workspace_id(workspace_id)
    if ontology_root is None:
        ontology_root = get_workspace_ontologies_root(resolved_workspace_id)

    registry = OntologyRegistry(ontology_dirs=[ontology_root])
    registry.discover_ontologies()
    assets = [_ontology_asset(ontology) for ontology in registry.ontologies.values()]
    assets.sort(key=lambda item: item["id"])

    return {
        "event_id": (
            f"{config.instance_id}:asset.snapshot:ontology:"
            f"{resolved_workspace_id}:{timestamp_ms}"
        ),
        "event_type": "asset.snapshot",
        "schema_version": "v1",
        "occurred_at": _format_datetime(timestamp),
        "runtime_id": config.instance_id,
        "organization_id": config.organization_id,
        "workspace_id": resolved_workspace_id,
        "user_id": None,
        "session_id": None,
        "run_id": None,
        "trace_id": None,
        "payload": {
            "asset_type": "ontology",
            "snapshot_mode": "full",
            "workspace": _workspace_snapshot(resolved_workspace_id),
            "assets": assets,
        },
    }


def _envelope(
    event: RuntimeEvent,
    config: PlatformConfig,
    *,
    event_type: str,
    run_context: Mapping[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    event_payload = dict(event.payload or {})
    return {
        "event_id": _event_id(config.instance_id, event, event_type),
        "event_type": event_type,
        "schema_version": "v1",
        "occurred_at": _format_timestamp(event.timestamp_ms),
        "runtime_id": config.instance_id,
        "organization_id": config.organization_id,
        "workspace_id": _first_text(event_payload, run_context, "workspace_id"),
        "user_id": _first_text(event_payload, run_context, "user_id"),
        "session_id": event.session_id,
        "run_id": event.run_id,
        "trace_id": _first_text(event_payload, run_context, "trace_id"),
        "payload": payload,
    }


def _event_id(instance_id: str, event: RuntimeEvent, event_type: str) -> str:
    suffix = str(event.span_key or event.timestamp_ms).strip()
    return f"{instance_id}:{event.run_id}:{event_type}:{suffix}"


def _run_finished_payload(event: RuntimeEvent, run_context: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(event.payload or {})
    result = {
        "status": _status(payload),
        "duration_ms": _optional_non_negative_int(payload.get("duration_ms")),
        "turn_count": _non_negative_int(event.turn_id),
        "tool_call_count": _non_negative_int(
            payload.get("tool_call_count", run_context.get("tool_call_count"))
        ),
        "model_call_count": _non_negative_int(
            payload.get("model_call_count", run_context.get("model_call_count"))
        ),
    }
    _copy_optional(result, "first_token_latency_ms", payload.get("first_token_latency_ms"))
    _copy_optional(result, "error_code", payload.get("error_code") or payload.get("error_type"))
    _copy_optional(result, "error_message", payload.get("error_message"))
    return result


def _tool_finished_payload(event: RuntimeEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    return {
        "tool_name": str(payload.get("tool_name") or "unknown_tool"),
        "tool_type": payload.get("tool_type"),
        "status": _status(payload),
        "duration_ms": _optional_non_negative_int(
            payload.get("duration_ms", payload.get("elapsed_ms"))
        ),
        "error_code": payload.get("error_code") or payload.get("error_type"),
        "error_message": payload.get("error_message"),
    }


def _model_usage_payload(event: RuntimeEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    model_name = str(payload.get("model") or payload.get("response_model") or "unknown")
    return {
        "provider": payload.get("model_provider") or payload.get("provider"),
        "model_name": model_name,
        "profile_name": payload.get("profile_name") or payload.get("request_label"),
        "input_tokens": _non_negative_int(
            usage.get("input_tokens", usage.get("prompt_tokens"))
        ),
        "output_tokens": _non_negative_int(
            usage.get("output_tokens", usage.get("completion_tokens"))
        ),
        "total_tokens": _non_negative_int(usage.get("total_tokens")),
        "duration_ms": _optional_non_negative_int(
            payload.get("duration_ms", payload.get("wall_duration_ms"))
        ),
        "status": _status(payload),
        "error_code": payload.get("error_code") or payload.get("error_type"),
        "error_message": payload.get("error_message"),
    }


def _status(payload: Mapping[str, Any]) -> str:
    return normalize_platform_status(payload.get("status"))


def _workspace_id(workspace_id: str | None) -> str:
    text = str(workspace_id or "").strip()
    if text:
        return text
    return get_current_workspace_id()


def _workspace_snapshot(workspace_id: str) -> dict[str, Any]:
    try:
        workspace = get_workspace(workspace_id)
    except Exception:
        workspace = {}
    return {
        "id": workspace_id,
        "name": str(workspace.get("name") or workspace_id),
        "status": "active",
    }


def _ontology_asset(ontology) -> dict[str, Any]:
    metadata = {
        "description": ontology.description,
        "language": ontology.language,
    }
    if ontology.scope:
        metadata["scope"] = list(ontology.scope)
    return {
        "id": ontology.id,
        "name": ontology.name,
        "version": ontology.version,
        "active_version": ontology.active_version,
        "versions": list(ontology.versions or []),
        "status": "active" if ontology.enabled else "disabled",
        "metadata": metadata,
    }


def _first_text(
    payload: Mapping[str, Any],
    run_context: Mapping[str, Any],
    key: str,
) -> str | None:
    value = payload.get(key)
    if value is None:
        value = run_context.get(key)
    text = str(value or "").strip()
    return text or None


def _copy_optional(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _format_timestamp(timestamp_ms: int) -> str:
    timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
    return _format_datetime(timestamp)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hostname() -> str | None:
    try:
        return socket.gethostname()
    except OSError:
        return None


def _private_ips() -> list[str]:
    hostname = _hostname()
    if not hostname:
        return []

    try:
        addresses = socket.gethostbyname_ex(hostname)[2]
    except OSError:
        return []

    private_ips: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 4 and ip.is_private and not ip.is_loopback:
            private_ips.append(str(ip))
    return sorted(set(private_ips))
