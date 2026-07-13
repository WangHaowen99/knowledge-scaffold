"""Tests for business_data_search runtime connection resolution."""

from __future__ import annotations

import json

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data_search import handle_business_data_search


def _fake_ontology(ontology_id):
    del ontology_id
    return object(), None


def _install_search_fakes(monkeypatch, *, mode: str):
    captured = {"events": []}

    def fake_resolve_business_data_source(*, workspace_id, ontology_id):
        captured["workspace_id"] = workspace_id
        captured["ontology_id"] = ontology_id
        return ResolvedBusinessDataSource(
            ontology_id=ontology_id,
            mode=mode,
            id="example-nl2sql" if mode == "connection_ref" else ontology_id,
            name="示例制造 NL2SQL" if mode == "connection_ref" else ontology_id,
            provider="nl2sql_http",
            config={
                "url": "https://nl2sql.equipment.local",
                "token": "secret-token",
                "scene": "example",
                "timeout_seconds": 120,
                "max_retries": 3,
            },
        )

    def fake_query_business_data_stream(request, *, connection):
        captured["request"] = request
        captured["connection"] = connection
        yield {"type": "start", "content": "开始查询"}
        yield {"type": "final", "content": "查询结果"}
        yield {"type": "done", "content": "完成"}

    monkeypatch.setattr("agent.tools.business_data_search.get_ontology_manifest", _fake_ontology)
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        fake_resolve_business_data_source,
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )
    return captured


def test_business_data_search_uses_workspace_direct_provider(monkeypatch):
    captured = _install_search_fakes(monkeypatch, mode="direct_provider")

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="  查询 TargetItem  ",
        workspace_id="demo-workspace",
        event_emitter=captured["events"].append,
    )

    assert result == "查询结果"
    assert captured["workspace_id"] == "demo-workspace"
    assert captured["ontology_id"] == "demo-ontology"
    assert captured["request"].workspace_id == "demo-workspace"
    assert captured["request"].ontology_id == "demo-ontology"
    assert captured["request"].entity == "TargetItem"
    assert captured["request"].query == "查询 TargetItem"
    assert captured["connection"].ontology_id == "demo-ontology"
    assert captured["connection"].mode == "direct_provider"
    assert [event["type"] for event in captured["events"]] == ["start", "final", "done"]


def test_business_data_search_uses_resolved_connection_ref(monkeypatch):
    captured = _install_search_fakes(monkeypatch, mode="connection_ref")

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "查询结果"
    assert captured["connection"].id == "example-nl2sql"
    assert captured["connection"].mode == "connection_ref"
    assert captured["connection"].provider == "nl2sql_http"
    assert captured["connection"].config["token"] == "secret-token"


def test_business_data_search_reports_missing_data_source(monkeypatch):
    from agent.tools.business_data.resolver import BusinessDataConnectionError

    monkeypatch.setattr("agent.tools.business_data_search.get_ontology_manifest", _fake_ontology)

    def fake_resolve_business_data_source(*, workspace_id, ontology_id):
        del workspace_id, ontology_id
        raise BusinessDataConnectionError("本体 'demo-ontology' 未配置/未绑定业务数据源")

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        fake_resolve_business_data_source,
    )

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "Error: 本体 'demo-ontology' 未配置/未绑定业务数据源"


def test_legacy_business_data_search_returns_structured_json_for_unexpected_runtime_error(monkeypatch):
    from agentic.business_data.resolver import ResolvedBusinessDataConnection
    from agentic.tools.business_data_search import handle_business_data_search as legacy_search

    def fake_resolve_business_data_connection(*, workspace_id, ontology_id):
        del workspace_id, ontology_id
        return ResolvedBusinessDataConnection(
            id="graph",
            name="Graph",
            provider="neo4j",
            config={},
        )

    def fake_query_business_data_stream(request, *, connection):
        del request, connection
        raise RuntimeError("filter_dict.SourceItemDesc 包含不支持的操作符: '$contains'")
        yield  # pragma: no cover

    monkeypatch.setattr("agentic.tools.business_data_search.get_ontology_manifest", _fake_ontology)
    monkeypatch.setattr(
        "agentic.tools.business_data_search.resolve_business_data_connection",
        fake_resolve_business_data_connection,
    )
    monkeypatch.setattr(
        "agentic.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )

    result = legacy_search(
        ontology_id="demo-ontology",
        entity="SourceItem",
        query='查询SourceItem中编码或名称为"A001"的物料信息',
        workspace_id="demo-workspace",
    )

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["provider"] == "neo4j"
    assert payload["error_code"] == "BUSINESS_DATA_SEARCH_UNEXPECTED_ERROR"
    assert payload["retryable"] is True
    assert "$contains" in payload["error"]


def test_legacy_query_business_data_stream_injects_graph_runtime_context(monkeypatch):
    from types import SimpleNamespace

    from agentic.business_data.provider_registry import BusinessDataProviderSpec
    from agentic.business_data.resolver import ResolvedBusinessDataConnection
    from agentic.business_data.service import BusinessDataSearchRequest, query_business_data_stream

    captured = {}

    class _FakeProvider:
        name = "neo4j"

        def __init__(self, config):
            captured["config"] = config

        def search_stream(self, *, query, entity):
            captured["query"] = query
            captured["entity"] = entity
            yield {"type": "final", "content": "ok"}

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "agentic.business_data.service.get_provider_spec",
        lambda provider: BusinessDataProviderSpec(
            name=provider,
            label="Neo4J",
            description="Graph",
            factory=_FakeProvider,
            fields=[],
        ),
    )
    monkeypatch.setattr(
        "agentic.business_data.service.get_workspace_paths",
        lambda workspace_id: SimpleNamespace(
            ontologies_dir="/runtime/{}/ontologies".format(workspace_id)
        ),
    )
    monkeypatch.setattr(
        "agentic.business_data.service.get_workspace",
        lambda workspace_id: {"id": workspace_id, "name": "示例工程变更空间"},
    )

    events = list(query_business_data_stream(
        BusinessDataSearchRequest(
            workspace_id="workspace-a",
            ontology_id="demo-ontology",
            entity="TargetItem",
            query="查询 TargetItem",
        ),
        connection=ResolvedBusinessDataConnection(
            id="graph",
            name="Graph",
            provider="neo4j",
            config={"uri": "bolt://neo4j.test", "workspace_property": "d2o_workspace_id"},
        ),
    ))

    provider_config = captured["config"]["ontology_search"]["neo4j"]
    assert provider_config["workspace_id"] == "workspace-a"
    assert provider_config["workspace_name"] == "示例工程变更空间"
    assert provider_config["ontology_id"] == "demo-ontology"
    assert provider_config["model_profile_id"] == ""
    assert provider_config["run_id"] == ""
    assert provider_config["session_id"] == ""
    assert provider_config["turn_id"] == 0
    assert provider_config["_workspace_root"] == "/runtime/workspace-a/ontologies"
    assert captured["query"] == "查询 TargetItem"
    assert captured["entity"] == "TargetItem"
    assert captured["closed"] is True
    assert events == [{"type": "final", "content": "ok"}]
