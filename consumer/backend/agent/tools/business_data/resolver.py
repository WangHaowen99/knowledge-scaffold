"""Resolve the effective business data source for an ontology."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent.tools.business_data.ontology_sources_store import (
    MODE_CONNECTION_REF,
    MODE_DIRECT_PROVIDER,
    get_ontology_data_source,
    build_ontology_business_data_summary,
)
from agent.tools.business_data.connections_store import (
    BusinessDataConnectionStoreError,
    get_data_connection,
)


class BusinessDataConnectionError(ValueError):
    """Raised when business data search cannot resolve a data source."""


@dataclass(frozen=True)
class ResolvedBusinessDataSource:
    ontology_id: str = ""
    mode: str = MODE_DIRECT_PROVIDER
    provider: str = ""
    config: Dict[str, Any] = field(default_factory=dict)
    id: str = ""
    name: str = ""


ResolvedBusinessDataConnection = ResolvedBusinessDataSource


def resolve_business_data_source(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
) -> ResolvedBusinessDataSource:
    resolved_ontology_id = str(ontology_id or "").strip()
    if not resolved_ontology_id:
        raise BusinessDataConnectionError("ontology_id 不能为空")

    source = get_ontology_data_source(
        resolved_ontology_id,
        workspace_id=workspace_id,
    )
    if source is None:
        raise BusinessDataConnectionError(
            "本体 '{}' 未配置/未绑定业务数据源".format(resolved_ontology_id)
        )

    if source.mode == MODE_CONNECTION_REF:
        if not source.connection_id:
            raise BusinessDataConnectionError(
                "本体 '{}' 的业务数据源引用无效".format(resolved_ontology_id)
            )
        try:
            connection = get_data_connection(source.connection_id, workspace_id=workspace_id)
        except BusinessDataConnectionStoreError as exc:
            raise BusinessDataConnectionError(str(exc)) from exc
        return ResolvedBusinessDataSource(
            ontology_id=source.ontology_id,
            mode=source.mode,
            provider=connection.provider,
            config=dict(connection.config),
            id=connection.id,
            name=connection.name,
        )

    if source.mode != MODE_DIRECT_PROVIDER:
        raise BusinessDataConnectionError(
            "本体 '{}' 的业务数据源尚未就绪".format(resolved_ontology_id)
        )
    if not source.provider:
        raise BusinessDataConnectionError(
            "本体 '{}' 的业务数据源配置无效".format(resolved_ontology_id)
        )

    return ResolvedBusinessDataSource(
        ontology_id=source.ontology_id,
        mode=source.mode,
        provider=source.provider,
        config=dict(source.config),
        id=source.ontology_id,
        name=source.ontology_id,
    )


def resolve_business_data_connection(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
) -> ResolvedBusinessDataSource:
    return resolve_business_data_source(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
