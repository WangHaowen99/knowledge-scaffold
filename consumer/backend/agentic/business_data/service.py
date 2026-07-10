"""Business data search service backed by workspace data connections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .provider_base import BusinessDataSearchEvent
from ..workspaces import get_workspace, get_workspace_paths
from .provider_registry import get_provider_spec
from .resolver import ResolvedBusinessDataConnection


@dataclass(frozen=True)
class BusinessDataSearchRequest:
    workspace_id: str
    ontology_id: str
    entity: str
    query: str
    model_profile_id: str = ""
    run_id: str = ""
    session_id: str = ""
    turn_id: int = 0
    # Trace linkage: span_key of the outer tool_batch that invoked this search,
    # and the agent iteration index, so provider-internal (e.g. graph) tool spans
    # can attach to the parent span tree instead of appearing as orphans.
    parent_span_key: str = ""
    agent_iteration: int = 0


def query_business_data_stream(
    request: BusinessDataSearchRequest,
    *,
    connection: ResolvedBusinessDataConnection,
) -> Iterator[BusinessDataSearchEvent]:
    spec = get_provider_spec(connection.provider)

    # Inject workspace context for providers that need it (e.g., nl2sql_template)
    workspace_root = ""
    workspace_name = ""
    if request.workspace_id:
        try:
            workspace_paths = get_workspace_paths(request.workspace_id)
            workspace_root = str(workspace_paths.ontologies_dir)
        except Exception:
            pass
        try:
            workspace_name = str(get_workspace(request.workspace_id).get("name") or "").strip()
        except Exception:
            workspace_name = ""

    provider_config = {
        "ontology_search": {
            connection.provider: {
                **dict(connection.config),
                "_workspace_root": workspace_root,
                "workspace_id": request.workspace_id,
                "workspace_name": workspace_name,
                "ontology_id": request.ontology_id,
                "model_profile_id": request.model_profile_id,
                "run_id": request.run_id,
                "session_id": request.session_id,
                "turn_id": request.turn_id,
                "parent_span_key": request.parent_span_key,
                "agent_iteration": request.agent_iteration,
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
