"""Service-bound scope and runtime metadata for graph providers."""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.service import (
    BusinessDataSearchRequest,
    query_business_data_stream,
)


class _CapturingProvider:
    configs: list[object] = []
    queries: list[str] = []

    def __init__(self, config: object) -> None:
        self.configs.append(config)

    def search_stream(self, *, query: str, entity: str):
        self.queries.append(query)
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
        ontology_version="1.5.0",
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
    assert config["ontology_version"] == "1.5.0"
    assert config["_workspace_root"] == str(tmp_path / "ontologies")
    assert config["parent_span_key"] == "tool:main:2:business_data_search:call-1"
    assert config["_observer"] is observer
    assert "untrusted" not in config


def test_service_injects_trusted_workspace_name_for_graph_scope(monkeypatch, tmp_path):
    _CapturingProvider.configs.clear()
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(ontologies_dir=tmp_path / "ontologies"),
    )
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_workspace",
        lambda workspace_id: {"id": workspace_id, "name": "示例工程变更空间"},
    )
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_provider_spec",
        lambda _provider: SimpleNamespace(factory=_CapturingProvider),
    )
    request = BusinessDataSearchRequest(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        entity="Part",
        query="query",
    )
    connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="neo4j",
        config={
            "uri": "bolt://graph",
            "workspace_property": "workspace_name",
            "workspace_name": "persisted-untrusted-name",
        },
    )

    assert list(query_business_data_stream(request, connection=connection)) == [
        {"type": "final", "content": "Part:query"}
    ]
    config = _CapturingProvider.configs[0]["ontology_search"]["neo4j"]
    assert config["workspace_id"] == "workspace-a"
    assert config["workspace_name"] == "示例工程变更空间"


def test_service_does_not_forward_graph_trace_metadata_to_other_providers(monkeypatch):
    _CapturingProvider.configs.clear()
    _CapturingProvider.queries.clear()
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
    effective_connection = _CapturingProvider.configs[0]
    assert effective_connection.provider == "dataagent"
    assert effective_connection.endpoint == "https://dataagent.example"
    assert "run_id" not in effective_connection.options
    assert "parent_span_key" not in effective_connection.options


def test_service_appends_field_alias_mappings_only_for_mindsdb(monkeypatch):
    _CapturingProvider.configs.clear()
    _CapturingProvider.queries.clear()
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_provider_spec",
        lambda _provider: SimpleNamespace(factory=_CapturingProvider),
    )
    request = BusinessDataSearchRequest(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        entity="AffectedObject",
        query="查询变更单号对应的物料变更类型",
        field_alias_mappings=(
            ("物料变更类型", "MaterialType"),
            ("变更单号", "CHANGE_ORDER_ID"),
        ),
    )

    mindsdb_connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="mindsdb",
        config={"url": "https://mindsdb.example"},
    )
    list(query_business_data_stream(request, connection=mindsdb_connection))

    expected_query = (
        "查询变更单号对应的物料变更类型\n\n"
        "字段映射（使用右侧正式字段 key）：\n"
        "- 变更单号 -> CHANGE_ORDER_ID\n"
        "- 物料变更类型 -> MaterialType"
    )
    assert _CapturingProvider.queries == [expected_query]

    _CapturingProvider.queries.clear()
    dataagent_connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="dataagent",
        config={"url": "https://dataagent.example"},
    )
    list(query_business_data_stream(request, connection=dataagent_connection))

    assert _CapturingProvider.queries == ["查询变更单号对应的物料变更类型"]


def test_service_field_alias_mapping_sort_uses_original_text_as_tiebreaker(
    monkeypatch,
):
    _CapturingProvider.queries.clear()
    monkeypatch.setattr(
        "agent.tools.business_data.service.get_provider_spec",
        lambda _provider: SimpleNamespace(factory=_CapturingProvider),
    )
    request = BusinessDataSearchRequest(
        workspace_id="workspace-a",
        ontology_id="ontology-a",
        entity="AffectedObject",
        query="查询 Straße 和 STRASSE",
        field_alias_mappings=(
            ("Straße", "StreetCode"),
            ("STRASSE", "StreetCode"),
        ),
    )
    connection = ResolvedBusinessDataSource(
        ontology_id="ontology-a",
        provider="mindsdb",
        config={"url": "https://mindsdb.example"},
    )

    list(query_business_data_stream(request, connection=connection))

    assert _CapturingProvider.queries == [
        "查询 Straße 和 STRASSE\n\n"
        "字段映射（使用右侧正式字段 key）：\n"
        "- STRASSE -> StreetCode\n"
        "- Straße -> StreetCode"
    ]


def test_request_positional_construction_keeps_runtime_context_slot():
    runtime_context = {"run_id": "run-legacy"}
    request = BusinessDataSearchRequest(
        "workspace-a",
        "ontology-a",
        "Part",
        "query",
        runtime_context,
    )

    assert request.workspace_id == "workspace-a"
    assert request.ontology_id == "ontology-a"
    assert request.entity == "Part"
    assert request.query == "query"
    assert request.provider_runtime_context == runtime_context
    assert request.ontology_version == ""
