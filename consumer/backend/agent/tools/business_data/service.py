"""Business data search service backed by ontology data sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from agent.tools.business_data.providers.base import OntologySearchEvent
from workspace.workspaces import get_workspace_paths
from agent.tools.business_data.provider_registry import get_provider_spec
from agent.tools.business_data.resolver import ResolvedBusinessDataSource


@dataclass(frozen=True)
class BusinessDataSearchRequest:
    workspace_id: str
    ontology_id: str
    entity: str
    query: str
    provider_runtime_context: Mapping[str, Any] = field(default_factory=dict)


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

    scoped_provider_config: dict[str, Any] = {
        **dict(connection.config),
        "_workspace_root": workspace_root,
        "workspace_id": request.workspace_id,
        "ontology_id": request.ontology_id,
    }
    if connection.provider in {"neo4j", "hugegraph"}:
        scoped_provider_config.update({
            key: value
            for key, value in dict(request.provider_runtime_context or {}).items()
            if key in {
                "run_id",
                "session_id",
                "turn_id",
                "parent_span_key",
                "agent_iteration",
                "model_profile_id",
                "_observer",
            }
        })

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
