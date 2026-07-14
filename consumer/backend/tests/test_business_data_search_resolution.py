"""Tests for business_data_search runtime connection resolution."""

from __future__ import annotations

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
