"""Resolve the effective business data connection for an ontology."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .bindings_store import get_ontology_data_binding
from .connections_store import BusinessDataConnectionStoreError, get_data_connection


class BusinessDataConnectionError(ValueError):
    """Raised when business data search cannot resolve a data connection."""


@dataclass(frozen=True)
class ResolvedBusinessDataConnection:
    id: str
    name: str
    provider: str
    config: Dict[str, Any]


def resolve_business_data_connection(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
) -> ResolvedBusinessDataConnection:
    resolved_ontology_id = str(ontology_id or "").strip()
    if not resolved_ontology_id:
        raise BusinessDataConnectionError("ontology_id 不能为空")

    binding = get_ontology_data_binding(
        resolved_ontology_id,
        workspace_id=workspace_id,
    )
    if not binding:
        raise BusinessDataConnectionError(
            "本体 '{}' 未绑定业务数据源".format(resolved_ontology_id)
        )

    connection_id = str(binding.get("connection_id") or "").strip()
    if not connection_id:
        raise BusinessDataConnectionError(
            "本体 '{}' 的业务数据源绑定无效".format(resolved_ontology_id)
        )

    try:
        connection = get_data_connection(connection_id, workspace_id=workspace_id)
    except BusinessDataConnectionStoreError as exc:
        raise BusinessDataConnectionError(str(exc)) from exc

    return ResolvedBusinessDataConnection(
        id=connection.id,
        name=connection.name,
        provider=connection.provider,
        config=dict(connection.config),
    )


def build_ontology_business_data_summary(
    ontology_id: str,
    *,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    binding = get_ontology_data_binding(ontology_id, workspace_id=workspace_id)
    if not binding:
        return {
            "connection_id": "",
            "connection_name": "",
            "provider": "",
            "ready": False,
        }

    connection_id = str(binding.get("connection_id") or "").strip()
    try:
        connection = get_data_connection(connection_id, workspace_id=workspace_id)
    except BusinessDataConnectionStoreError:
        return {
            "connection_id": connection_id,
            "connection_name": "",
            "provider": "",
            "ready": False,
        }

    return {
        "connection_id": connection.id,
        "connection_name": connection.name,
        "provider": connection.provider,
        "ready": True,
    }
