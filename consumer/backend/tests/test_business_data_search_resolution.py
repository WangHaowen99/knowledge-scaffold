"""Tests for business_data_search runtime data source resolution."""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data_search import BusinessDataSearchTool, handle_business_data_search


def _fake_ontology(ontology_id, *, workspace_id=None):
    del ontology_id, workspace_id
    return object(), None


def _install_search_fakes(monkeypatch):
    captured = {"events": []}

    def fake_resolve_business_data_source(*, workspace_id, ontology_id):
        captured["workspace_id"] = workspace_id
        captured["ontology_id"] = ontology_id
        return ResolvedBusinessDataSource(
            ontology_id=ontology_id,
            mode="direct_provider",
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


def test_non_retryable_graph_timeout_stops_the_main_agent_tool_loop(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search",
        lambda **_kwargs: (
            '{"success":false,"engine":"graph_search_runtime",'
            '"error_code":"GRAPH_BACKEND_TIMEOUT","retryable":false}'
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search._build_provider_runtime_context",
        lambda **_kwargs: {},
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "refinery_unit",
            "query": "query refinery units",
        },
        context=SimpleNamespace(workspace_id="demo-workspace", agent_iteration=0),
    )

    assert outcome.terminal is True
    assert outcome.state_updates["terminal_error"] == {
        "error_code": "GRAPH_BACKEND_TIMEOUT",
        "message": "Graph query timed out.",
        "tool_name": "business_data_search",
    }


def test_business_data_search_uses_workspace_direct_provider(monkeypatch):
    captured = _install_search_fakes(monkeypatch)

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


def test_business_data_search_uses_ontology_identity_for_resolved_source(monkeypatch):
    captured = _install_search_fakes(monkeypatch)

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "查询结果"
    assert captured["connection"].id == "demo-ontology"
    assert captured["connection"].name == "demo-ontology"
    assert captured["connection"].mode == "direct_provider"
    assert captured["connection"].provider == "nl2sql_http"
    assert captured["connection"].config["token"] == "secret-token"


def test_business_data_search_reports_missing_data_source(monkeypatch):
    from agent.tools.business_data.resolver import BusinessDataConnectionError

    monkeypatch.setattr("agent.tools.business_data_search.get_ontology_manifest", _fake_ontology)

    def fake_resolve_business_data_source(*, workspace_id, ontology_id):
        del workspace_id, ontology_id
        raise BusinessDataConnectionError("本体 'demo-ontology' 未配置业务数据源")

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

    assert result == "Error: 本体 'demo-ontology' 未配置业务数据源"


def test_business_data_search_reports_provider_stream_error(monkeypatch):
    _install_search_fakes(monkeypatch)

    def fake_query_business_data_stream(request, *, connection):
        del request, connection
        yield {"type": "start", "content": ""}
        yield {"type": "error", "content": "MindsDB request failed"}
        yield {"type": "done", "content": ""}

    monkeypatch.setattr(
        "agent.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "Error: 业务数据查询失败：MindsDB request failed"


def test_business_data_search_reports_missing_final_event(monkeypatch):
    _install_search_fakes(monkeypatch)

    def fake_query_business_data_stream(request, *, connection):
        del request, connection
        yield {"type": "start", "content": "开始查询"}
        yield {"type": "done", "content": "查询流程结束"}

    monkeypatch.setattr(
        "agent.tools.business_data_search.query_business_data_stream",
        fake_query_business_data_stream,
    )

    result = handle_business_data_search(
        ontology_id="demo-ontology",
        entity="TargetItem",
        query="查询 TargetItem",
        workspace_id="demo-workspace",
    )

    assert result == "Error: 业务数据查询失败：数据源未返回查询结果"
