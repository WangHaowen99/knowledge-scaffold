"""Business data search service backed by ontology data sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

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

    provider_config = {
        "ontology_search": {
            connection.provider: {
                **dict(connection.config),
                "_workspace_root": workspace_root,
                "ontology_id": request.ontology_id,
            },
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
