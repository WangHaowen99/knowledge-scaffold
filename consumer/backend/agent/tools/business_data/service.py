"""Business data search service backed by ontology data sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator, Mapping, Tuple

from agent.runtime.async_utils import run_sync_compat
from agent.tools.business_data.effective_connection import build_effective_connection
from agent.tools.business_data.providers.base import OntologySearchEvent
from agent.tools.business_data.provider_registry import get_provider_spec
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from workspace.workspaces import get_workspace, get_workspace_paths


@dataclass(frozen=True)
class BusinessDataSearchRequest:
    workspace_id: str
    ontology_id: str
    entity: str
    query: str
    provider_runtime_context: Mapping[str, Any] = field(default_factory=dict)
    ontology_version: str = ""
    field_alias_mappings: Tuple[Tuple[str, str], ...] = ()
    stage: str = "business_query"


def query_business_data_stream(
    request: BusinessDataSearchRequest,
    *,
    connection: ResolvedBusinessDataSource,
) -> Iterator[OntologySearchEvent]:
    spec = get_provider_spec(connection.provider)

    # Inject workspace context for providers that need it (e.g., nl2sql_template)
    workspace_root = ""
    if request.workspace_id:
        try:
            workspace_root = str(get_workspace_paths(request.workspace_id).ontologies_dir)
        except Exception:
            pass

    if connection.provider not in {"neo4j", "hugegraph"}:
        if connection.provider == "nl2sql_template":
            provider = spec.factory(
                build_effective_connection(connection, provider_override="nl2sql_http"),
                workspace_root=workspace_root,
                workspace_id=request.workspace_id,
                ontology_id=request.ontology_id,
                ontology_version=request.ontology_version,
            )
        else:
            provider = spec.factory(build_effective_connection(connection))
        provider_query = (
            _append_field_alias_mappings(
                request.query,
                request.field_alias_mappings,
            )
            if connection.provider == "mindsdb"
            else request.query
        )
        try:
            yield from provider.search_stream(
                query=provider_query,
                entity=request.entity,
            )
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
        return

    scoped_provider_config: dict[str, Any] = {
        **dict(connection.config),
        "_workspace_root": workspace_root,
        "workspace_id": request.workspace_id,
        "ontology_id": request.ontology_id,
        "ontology_version": request.ontology_version,
    }
    workspace_property = str(
        scoped_provider_config.get("workspace_property") or "d2o_workspace_id",
    ).strip()
    if workspace_property == "workspace_name":
        try:
            scoped_provider_config["workspace_name"] = str(
                get_workspace(request.workspace_id).get("name") or "",
            ).strip()
        except Exception:
            scoped_provider_config["workspace_name"] = ""
    runtime_context_keys = {
        "run_id",
        "session_id",
        "turn_id",
        "parent_span_key",
        "agent_iteration",
        "model_profile_id",
        "_observer",
    }
    scoped_provider_config.update(
        {
            key: value
            for key, value in dict(request.provider_runtime_context or {}).items()
            if key in runtime_context_keys
        }
    )

    provider_config = {
        "ontology_search": {
            connection.provider: scoped_provider_config,
        }
    }
    provider = spec.factory(provider_config)
    try:
        yield from provider.search_stream(
            query=request.query,
            entity=request.entity,
        )
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


async def query_business_data_async(
    request: BusinessDataSearchRequest,
    *,
    connection: ResolvedBusinessDataSource,
) -> AsyncIterator[OntologySearchEvent]:
    """Execute providers without moving a nested Agent onto a worker loop."""

    if connection.provider not in {"neo4j", "hugegraph"}:
        events = await run_sync_compat(
            lambda: list(query_business_data_stream(request, connection=connection)),
            run_id=str(request.provider_runtime_context.get("run_id") or "") or None,
            task_name="business-data-provider",
            offload_kind="tool",
        )
        for event in events:
            yield event
        return

    provider = await run_sync_compat(
        _build_graph_provider,
        request,
        connection,
        run_id=str(request.provider_runtime_context.get("run_id") or "") or None,
        task_name="graph-provider-setup",
        offload_kind="tool",
    )
    try:
        async for event in provider.asearch_stream(
            query=request.query,
            entity=request.entity,
        ):
            yield event
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            await run_sync_compat(
                close,
                run_id=str(request.provider_runtime_context.get("run_id") or "") or None,
                task_name="graph-provider-close",
                offload_kind="tool",
            )


def _build_graph_provider(
    request: BusinessDataSearchRequest,
    connection: ResolvedBusinessDataSource,
):
    """Build graph provider state inside the bounded tool pool."""

    spec = get_provider_spec(connection.provider)
    workspace_root = ""
    if request.workspace_id:
        try:
            workspace_root = str(get_workspace_paths(request.workspace_id).ontologies_dir)
        except Exception:
            pass
    scoped_provider_config: dict[str, Any] = {
        **dict(connection.config),
        "_workspace_root": workspace_root,
        "workspace_id": request.workspace_id,
        "ontology_id": request.ontology_id,
        "ontology_version": request.ontology_version,
    }
    workspace_property = str(scoped_provider_config.get("workspace_property") or "d2o_workspace_id").strip()
    if workspace_property == "workspace_name":
        try:
            scoped_provider_config["workspace_name"] = str(
                get_workspace(request.workspace_id).get("name") or ""
            ).strip()
        except Exception:
            scoped_provider_config["workspace_name"] = ""
    allowed_runtime_context_keys = {
        "run_id",
        "session_id",
        "turn_id",
        "parent_span_key",
        "agent_iteration",
        "model_profile_id",
        "_observer",
    }
    for key, value in dict(request.provider_runtime_context or {}).items():
        if key in allowed_runtime_context_keys:
            scoped_provider_config[key] = value
    return spec.factory(
        {
            "ontology_search": {
                connection.provider: scoped_provider_config,
            }
        }
    )


def _append_field_alias_mappings(
    query: str,
    mappings: Tuple[Tuple[str, str], ...],
) -> str:
    normalized_query = str(query or "").strip()
    valid_mappings = set()
    for name, field_key in mappings:
        normalized_name = str(name or "").strip()
        normalized_key = str(field_key or "").strip()
        if normalized_name and normalized_key and len(normalized_name) <= 128 and len(normalized_key) <= 128:
            valid_mappings.add((normalized_name, normalized_key))
    normalized_mappings = sorted(
        valid_mappings,
        key=lambda item: (
            item[1].casefold(),
            item[0].casefold(),
            item[1],
            item[0],
        ),
    )[:20]
    if not normalized_mappings:
        return normalized_query
    lines = [
        normalized_query,
        "",
        "字段映射（使用右侧正式字段 key）：",
        *["- {} -> {}".format(name, field_key) for name, field_key in normalized_mappings],
    ]
    return "\n".join(lines)
