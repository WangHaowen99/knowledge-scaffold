"""Tests for business_data_search runtime connection resolution."""

from __future__ import annotations

from agentic.business_data.resolver import ResolvedBusinessDataConnection
from agentic.tools.business_data_search import handle_business_data_search


def _fake_ontology(ontology_id):
    del ontology_id
    return object(), None


def test_business_data_search_uses_workspace_ontology_binding(monkeypatch):
    captured = {"events": []}

    def fake_resolve_business_data_connection(*, workspace_id, ontology_id):
        captured["workspace_id"] = workspace_id
        captured["ontology_id"] = ontology_id
        return ResolvedBusinessDataConnection(
            id="example-nl2sql",
            name="示例制造 NL2SQL",
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

    monkeypatch.setattr("agentic.tools.business_data_search.get_ontology_manifest", _fake_ontology)
    monkeypatch.setattr(
        "agentic.tools.business_data_search.resolve_business_data_connection",
        fake_resolve_business_data_connection,
    )
    monkeypatch.setattr(
        "agentic.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )

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
    assert captured["connection"].id == "example-nl2sql"
    assert [event["type"] for event in captured["events"]] == ["start", "final", "done"]


def test_business_data_search_reports_missing_binding(monkeypatch):
    from agentic.business_data.resolver import BusinessDataConnectionError

    monkeypatch.setattr("agentic.tools.business_data_search.get_ontology_manifest", _fake_ontology)

    def fake_resolve_business_data_connection(*, workspace_id, ontology_id):
        del workspace_id, ontology_id
        raise BusinessDataConnectionError("本体 'demo-ontology' 未绑定业务数据源")

    monkeypatch.setattr(
        "agentic.tools.business_data_search.resolve_business_data_connection",
        fake_resolve_business_data_connection,
    )

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "Error: 本体 'demo-ontology' 未绑定业务数据源"
