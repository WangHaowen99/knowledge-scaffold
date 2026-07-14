"""Service-bound scope and runtime metadata for graph providers."""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.service import (
    BusinessDataSearchRequest,
    query_business_data_stream,
)


class _CapturingProvider:
    configs: list[dict] = []

    def __init__(self, config: dict) -> None:
        self.configs.append(config)

    def search_stream(self, *, query: str, entity: str):
        yield {"type": "final", "content": "{}:{}".format(entity, query)}


def test_service_overrides_persisted_graph_scope_with_request_scope(monkeypatch, tmp_path):
    _CapturingProvider.configs.clear()
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(ontologies_dir=tmp_path / "ontologies"),
    )
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_provider_spec",
        lambda _provider: SimpleNamespace(factory=_CapturingProvider),
    )
    observer = object()
    request = BusinessDataSearchRequest(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        entity="Part",
        query="query",
        provider_runtime_context={
            "run_id": "run-1",
            "parent_span_key": "tool:main:2:business_data_search:call-1",
            "agent_iteration": 2,
            "_observer": observer,
            "untrusted": "drop-me",
        },
    )
    connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="neo4j",
        config={
            "uri": "bolt://graph",
            "workspace_id": "persisted-wrong-workspace",
            "ontology_id": "persisted-wrong-ontology",
        },
    )

    assert list(query_business_data_stream(request, connection=connection)) == [
        {"type": "final", "content": "Part:query"}
    ]
    config = _CapturingProvider.configs[0]["ontology_search"]["neo4j"]
    assert config["workspace_id"] == "workspace-a"
    assert config["ontology_id"] == "ontology-a"
    assert config["_workspace_root"] == str(tmp_path / "ontologies")
    assert config["parent_span_key"] == "tool:main:2:business_data_search:call-1"
    assert config["_observer"] is observer
    assert "untrusted" not in config


def test_service_does_not_forward_graph_trace_metadata_to_other_providers(monkeypatch):
    _CapturingProvider.configs.clear()
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_provider_spec",
        lambda _provider: SimpleNamespace(factory=_CapturingProvider),
    )
    request = BusinessDataSearchRequest(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        entity="Part",
        query="query",
        provider_runtime_context={
            "run_id": "run-1",
            "parent_span_key": "tool:main:2:business_data_search:call-1",
        },
    )
    connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="dataagent",
        config={"url": "https://dataagent.example"},
    )

    list(query_business_data_stream(request, connection=connection))
    config = _CapturingProvider.configs[0]["ontology_search"]["dataagent"]
    assert config["workspace_id"] == "workspace-a"
    assert config["ontology_id"] == "ontology-a"
    assert "run_id" not in config
    assert "parent_span_key" not in config
