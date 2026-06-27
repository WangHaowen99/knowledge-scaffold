"""Tests for GraphSearchProvider wrapper behavior."""

from __future__ import annotations

import json

from agentic.business_data.providers.graph.models import GraphAgentResult, GraphBackendResult
from agentic.business_data.providers.graph.provider import GraphSearchProvider


class _ProviderBackend:
    def __init__(self):
        self.closed = False

    def get_nodes_schema(self, *, scope, element_types=None):
        return {"labels": ["TargetItem"]}

    def get_edges_schema(self, *, scope, relation_types=None):
        return {"relationship_types": []}

    def close(self):
        self.closed = True


class _ProviderGraphAgent:
    def run(self, **kwargs):
        del kwargs
        return GraphAgentResult(
            tool_name="property_filter",
            tool_args={"queries": [{"element_class": "TargetItem", "element_type": "NODE"}]},
            backend_result=GraphBackendResult(records=[{"properties": {"target_item_code": "M-001"}}], total=1),
            executed_steps=[{"tool": "property_filter", "summary": "执行内部图工具 property_filter"}],
        )


def test_graph_search_provider_yields_standard_events_and_closes_backend():
    backend = _ProviderBackend()
    provider = GraphSearchProvider(
        provider_config={
            "workspace_id": "workspace-a",
            "ontology_id": "demo-ontology",
            "_workspace_root": "/missing",
            "workspace_property": "d2o_workspace_id",
        },
        backend=backend,
        graph_agent=_ProviderGraphAgent(),
    )

    events = list(provider.search_stream(query="查询 TargetItem", entity="TargetItem"))
    provider.close()

    assert [event["type"] for event in events] == [
        "start",
        "intermediate",
        "intermediate",
        "intermediate",
        "final",
        "done",
    ]
    final = json.loads(events[-2]["content"])
    assert final["success"] is True
    assert final["provider"] == "graph"
    assert final["records"] == [{"properties": {"target_item_code": "M-001"}}]
    assert backend.closed is True
