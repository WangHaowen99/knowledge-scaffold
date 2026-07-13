"""Workspace-local business data connection storage."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import LimitsConfig
from workspace.workspaces import get_workspace_paths
from agent.tools.business_data.provider_registry import (
    BusinessDataProviderError,
    get_provider_spec,
    normalize_provider_config,
    normalize_provider_name,
    publicize_provider_config,
)
from utils.security.secrets import decrypt_secret_fields, encrypt_secret_fields, is_encrypted_secret


_CONNECTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")
_CONNECTION_ID_MAX_LEN = 80


@dataclass(frozen=True)
class BusinessDataConnection:
    id: str
    name: str
    provider: str
    config: Dict[str, Any]
    created_at: str
    updated_at: str


class BusinessDataConnectionStoreError(ValueError):
    """Raised when a business data connection cannot be stored."""


class BusinessDataConnectionConflictError(BusinessDataConnectionStoreError):
    """Raised when a connection is still referenced by ontology data sources."""

    def __init__(self, connection_id: str, bound_ontology_ids: List[str]) -> None:
        self.connection_id = connection_id
        self.bound_ontology_ids = list(bound_ontology_ids)
        super().__init__("该连接仍被本体绑定，不能删除")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connections_path(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "data-connections.json"


def _read_document(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    path = _connections_path(workspace_id)
    if not path.exists():
        return {"default_connection_id": "", "connections": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {"default_connection_id": "", "connections": []}
    if not isinstance(payload, dict):
        return {"default_connection_id": "", "connections": []}
    connections = payload.get("connections")
    if not isinstance(connections, list):
        connections = []
    document = {
        "default_connection_id": str(payload.get("default_connection_id") or "").strip(),
        "connections": [item for item in connections if isinstance(item, dict)],
    }
    if _document_has_plaintext_connection_secrets(document):
        document = _encrypt_connection_document(document)
        _write_document(document, workspace_id)
    return document


def _write_document(payload: Dict[str, Any], workspace_id: Optional[str] = None) -> None:
    path = _connections_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_connection_id(value: Any) -> str:
    connection_id = str(value or "").strip().lower()
    if not connection_id:
        raise BusinessDataConnectionStoreError("连接 ID 不能为空")
    if len(connection_id) > _CONNECTION_ID_MAX_LEN:
        raise BusinessDataConnectionStoreError("连接 ID 长度不能超过 {} 字符".format(_CONNECTION_ID_MAX_LEN))
    if not _CONNECTION_ID_PATTERN.match(connection_id):
        raise BusinessDataConnectionStoreError("连接 ID 必须以英文小写字母开头，只能包含英文、数字、下划线或中划线")
    return connection_id


def _slugify_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(name or "").strip())
    ascii_name = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    candidate = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name or normalized).strip("-").lower()
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate or not candidate[0].isalpha():
        candidate = "conn-{}".format(uuid.uuid4().hex[:8])
    return _normalize_connection_id(candidate[:_CONNECTION_ID_MAX_LEN])


def _generate_unique_connection_id(name: str, existing_ids: set[str]) -> str:
    base_id = _slugify_name(name)
    if base_id not in existing_ids:
        return base_id
    for index in range(2, len(existing_ids) + 2):
        suffix = "-{}".format(index)
        candidate_base = base_id[: _CONNECTION_ID_MAX_LEN - len(suffix)].rstrip("-_")
        candidate = "{}{}".format(candidate_base, suffix)
        if candidate not in existing_ids:
            return _normalize_connection_id(candidate)
    return _normalize_connection_id("conn-{}".format(uuid.uuid4().hex[:8]))


def _normalize_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise BusinessDataConnectionStoreError("连接名称不能为空")
    return name


def _normalize_name_key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "").strip()).casefold()


def _from_payload(payload: Dict[str, Any]) -> BusinessDataConnection:
    provider = normalize_provider_name(payload.get("provider"))
    return BusinessDataConnection(
        id=str(payload.get("id") or "").strip(),
        name=str(payload.get("name") or "").strip(),
        provider=provider,
        config=_decrypt_provider_config(provider, dict(payload.get("config") or {})),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def _to_payload(connection: BusinessDataConnection) -> Dict[str, Any]:
    return {
        "id": connection.id,
        "name": connection.name,
        "provider": connection.provider,
        "config": _encrypt_provider_config(connection.provider, dict(connection.config)),
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def _provider_secret_fields(provider: str) -> set[str]:
    try:
        spec = get_provider_spec(provider)
    except BusinessDataProviderError:
        return set()
    return {field.name for field in spec.fields if field.secret}


def _provider_secret_context(provider: str) -> str:
    return "business_data.{}".format(normalize_provider_name(provider) or "unknown")


def _encrypt_provider_config(provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return encrypt_secret_fields(
        config,
        _provider_secret_fields(provider),
        context=_provider_secret_context(provider),
    )


def _decrypt_provider_config(provider: str, config: Dict[str, Any]) -> Dict[str, Any]:
    return decrypt_secret_fields(
        config,
        _provider_secret_fields(provider),
        context=_provider_secret_context(provider),
    )


def _connection_payload_has_plaintext_secrets(payload: Dict[str, Any]) -> bool:
    provider = normalize_provider_name(payload.get("provider"))
    config = payload.get("config")
    if not isinstance(config, dict):
        return False
    for field_name in _provider_secret_fields(provider):
        value = str(config.get(field_name) or "").strip()
        if value and not is_encrypted_secret(value):
            return True
    return False


def _document_has_plaintext_connection_secrets(document: Dict[str, Any]) -> bool:
    return any(
        _connection_payload_has_plaintext_secrets(item)
        for item in document.get("connections", [])
        if isinstance(item, dict)
    )


def _encrypt_connection_document(document: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "default_connection_id": str(document.get("default_connection_id") or "").strip(),
        "connections": [
            _to_payload(_from_payload(item))
            for item in document.get("connections", [])
            if isinstance(item, dict)
        ],
    }


def publicize_connection(connection: BusinessDataConnection) -> Dict[str, Any]:
    return {
        "id": connection.id,
        "name": connection.name,
        "provider": connection.provider,
        "config": publicize_provider_config(connection.provider, connection.config),
        "created_at": connection.created_at,
        "updated_at": connection.updated_at,
    }


def list_data_connections(
    workspace_id: Optional[str] = None,
    *,
    include_secrets: bool = False,
) -> Dict[str, Any]:
    document = _read_document(workspace_id)
    connections = [_from_payload(item) for item in document["connections"]]
    if include_secrets:
        public_connections = [_to_payload(connection) for connection in connections]
    else:
        public_connections = [publicize_connection(connection) for connection in connections]
    return {
        "default_connection_id": document["default_connection_id"],
        "connections": public_connections,
    }


def get_data_connection(
    connection_id: str,
    workspace_id: Optional[str] = None,
) -> BusinessDataConnection:
    resolved_id = _normalize_connection_id(connection_id)
    document = _read_document(workspace_id)
    for item in document["connections"]:
        connection = _from_payload(item)
        if connection.id == resolved_id:
            return connection
    raise BusinessDataConnectionStoreError("业务数据连接不存在: {}".format(resolved_id))


def create_data_connection(
    data: Dict[str, Any],
    workspace_id: Optional[str] = None,
) -> BusinessDataConnection:
    document = _read_document(workspace_id)
    max_connections = LimitsConfig.from_env().data_connection_configs_max_per_workspace
    if len(document["connections"]) >= max_connections:
        raise BusinessDataConnectionStoreError(
            "数据连接配置数量已达上限: {}".format(max_connections)
        )
    name = _normalize_name(data.get("name"))
    existing_connections = [_from_payload(item) for item in document["connections"]]
    existing_ids = {connection.id for connection in existing_connections}
    raw_connection_id = str(data.get("id") or "").strip()
    if raw_connection_id:
        connection_id = _normalize_connection_id(raw_connection_id)
    else:
        connection_id = _generate_unique_connection_id(name, existing_ids)
    if connection_id in existing_ids:
        raise BusinessDataConnectionStoreError("业务数据连接已存在: {}".format(connection_id))
    name_key = _normalize_name_key(name)
    if any(_normalize_name_key(connection.name) == name_key for connection in existing_connections):
        raise BusinessDataConnectionStoreError("业务数据连接名称已存在: {}".format(name))
    provider = normalize_provider_name(data.get("provider"))
    try:
        config = normalize_provider_config(provider, data.get("config") or {})
    except BusinessDataProviderError as exc:
        raise BusinessDataConnectionStoreError(str(exc)) from exc
    now = _utc_now()
    connection = BusinessDataConnection(
        id=connection_id,
        name=name,
        provider=provider,
        config=config,
        created_at=now,
        updated_at=now,
    )
    document["connections"].append(_to_payload(connection))
    if data.get("set_as_default") or not document.get("default_connection_id"):
        document["default_connection_id"] = connection.id
    _write_document(document, workspace_id)
    return connection


def update_data_connection(
    connection_id: str,
    data: Dict[str, Any],
    workspace_id: Optional[str] = None,
) -> BusinessDataConnection:
    resolved_id = _normalize_connection_id(connection_id)
    document = _read_document(workspace_id)
    existing_connections = [_from_payload(item) for item in document["connections"]]
    next_connections: List[Dict[str, Any]] = []
    updated: Optional[BusinessDataConnection] = None
    for current in existing_connections:
        if current.id != resolved_id:
            next_connections.append(_to_payload(current))
            continue
        next_name = _normalize_name(data.get("name", current.name))
        next_name_key = _normalize_name_key(next_name)
        if any(
            other.id != resolved_id and _normalize_name_key(other.name) == next_name_key
            for other in existing_connections
        ):
            raise BusinessDataConnectionStoreError("业务数据连接名称已存在: {}".format(next_name))
        provider = normalize_provider_name(data.get("provider") or current.provider)
        try:
            config = normalize_provider_config(
                provider,
                data.get("config") or {},
                existing_config=current.config if provider == current.provider else None,
            )
        except BusinessDataProviderError as exc:
            raise BusinessDataConnectionStoreError(str(exc)) from exc
        updated = BusinessDataConnection(
            id=current.id,
            name=next_name,
            provider=provider,
            config=config,
            created_at=current.created_at,
            updated_at=_utc_now(),
        )
        next_connections.append(_to_payload(updated))
    if updated is None:
        raise BusinessDataConnectionStoreError("业务数据连接不存在: {}".format(resolved_id))
    document["connections"] = next_connections
    _write_document(document, workspace_id)
    return updated


def delete_data_connection(connection_id: str, workspace_id: Optional[str] = None) -> None:
    resolved_id = _normalize_connection_id(connection_id)
    from agent.tools.business_data.ontology_sources_store import list_bound_ontology_ids

    bound_ontology_ids = list_bound_ontology_ids(resolved_id, workspace_id=workspace_id)
    if bound_ontology_ids:
        raise BusinessDataConnectionConflictError(resolved_id, bound_ontology_ids)
    document = _read_document(workspace_id)
    next_connections = [
        item for item in document["connections"]
        if _from_payload(item).id != resolved_id
    ]
    if len(next_connections) == len(document["connections"]):
        raise BusinessDataConnectionStoreError("业务数据连接不存在: {}".format(resolved_id))
    document["connections"] = next_connections
    if document.get("default_connection_id") == resolved_id:
        document["default_connection_id"] = ""
    _write_document(document, workspace_id)


def set_default_data_connection(connection_id: str, workspace_id: Optional[str] = None) -> str:
    resolved_id = get_data_connection(connection_id, workspace_id).id
    document = _read_document(workspace_id)
    document["default_connection_id"] = resolved_id
    _write_document(document, workspace_id)
    return resolved_id
