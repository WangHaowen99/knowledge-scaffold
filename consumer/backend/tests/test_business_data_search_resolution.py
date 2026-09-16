# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for business_data_search runtime data source resolution."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data_search import BusinessDataSearchTool as NativeBusinessDataSearchTool
from agent.tools.business_data_search import (
    handle_business_data_search,
)
from ontology.ontology_catalog import (
    ObjectReferenceLookupResult,
    ObjectReferenceStatus,
)


class BusinessDataSearchTool(NativeBusinessDataSearchTool):
    """Synchronous test adapter around the production async-only tool."""

    def execute(self, **kwargs):
        return asyncio.run(self.aexecute(**kwargs))


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
                "url": "https://service.example.net",
                "token": "your-api-key-here",
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


def _business_data_tool_context():
    return SimpleNamespace(
        workspace_id="demo-workspace",
        run_id="run-1",
        session_id="session-1",
        turn_id=1,
        caller="main",
        agent_iteration=2,
        model_profile_id=None,
        observer=None,
    )


def _write_object_alias_package(tmp_path, *, ambiguous=False):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    rows = ["| 正式对象 | 制造物料 | 制造件 |"]
    if ambiguous:
        rows.append("| 备选对象 | 备选物料 | 制造件 |")
    (objects_dir / "index.md").write_text(
        "| object key | title | alias |\n| --- | --- | --- |\n{}\n".format("\n".join(rows)),
        encoding="utf-8",
    )
    (objects_dir / "formal_object.md").write_text(
        "---\nresource: object\nkey: 正式对象\naliases:\n  - 制造件\n---\n"
        "\n# 字段\n\n"
        "| 字段 key | 中文名 | 别名 |\n"
        "| --- | --- | --- |\n"
        "| MaterialCode | 物料编码 | 料号 |\n",
        encoding="utf-8",
    )
    if ambiguous:
        (objects_dir / "alternative.md").write_text(
            "---\nresource: object\nkey: 备选对象\naliases:\n  - 制造件\n---\n",
            encoding="utf-8",
        )


def _write_object_mapping(tmp_path, object_key):
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir(exist_ok=True)
    (mappings_dir / "object-data-mappings.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "objects": {
                    object_key: {
                        "resource": "LegacyObject",
                        "fields": {},
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_non_retryable_graph_timeout_stops_the_main_agent_tool_loop(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir="."),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_object_reference_result",
        lambda *_args, **_kwargs: ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.RESOLVED,
            resolution=SimpleNamespace(object_key="refinery_unit"),
        ),
    )

    async def fake_handle_business_data_search_async(**_kwargs):
        return (
            '{"success":false,"engine":"graph_search_runtime","error_code":"GRAPH_BACKEND_TIMEOUT","retryable":false}'
        )

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search_async",
        fake_handle_business_data_search_async,
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


def test_business_data_search_keeps_workspace_id_at_legacy_positional_slot(monkeypatch):
    captured = _install_search_fakes(monkeypatch)

    result = handle_business_data_search(
        "demo-ontology",
        "鏌ヨ TargetItem",
        "TargetItem",
        "demo-workspace",
        captured["events"].append,
        "tool-1",
        {"run_id": "run-1"},
    )

    assert result == "查询结果"
    assert captured["request"].workspace_id == "demo-workspace"
    assert captured["request"].provider_runtime_context == {"run_id": "run-1"}
    assert captured["request"].ontology_version == ""


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
    assert captured["connection"].config["token"] == "your-api-key-here"


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


def test_business_data_search_tool_normalizes_alias_before_provider(tmp_path, monkeypatch):
    _write_object_alias_package(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    async def fake_handle_business_data_search(**kwargs):
        captured.update(kwargs)
        return "查询结果"

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search_async",
        fake_handle_business_data_search,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "制造件",
            "query": "  查询制造件  ",
        },
        context=_business_data_tool_context(),
    )

    assert outcome.content == "查询结果"
    assert captured["entity"] == "正式对象"
    assert captured["query"] == "查询制造件"
    assert outcome.state_updates["object_context_usage"] == [{"entity_name": "正式对象", "iteration": 2}]


def test_business_data_search_tool_passes_matched_field_aliases_internally(
    tmp_path,
    monkeypatch,
):
    _write_object_alias_package(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    async def fake_handle_business_data_search(**kwargs):
        captured.update(kwargs)
        return "查询结果"

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search_async",
        fake_handle_business_data_search,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "制造件",
            "query": "查询料号对应的物料编码",
        },
        context=_business_data_tool_context(),
    )

    assert outcome.content == "查询结果"
    assert captured["entity"] == "正式对象"
    assert captured["query"] == "查询料号对应的物料编码"
    assert captured["_field_alias_mappings"] == (
        ("料号", "MaterialCode"),
        ("物料编码", "MaterialCode"),
    )


def test_business_data_search_tool_stops_before_provider_for_ambiguous_alias(
    tmp_path,
    monkeypatch,
):
    _write_object_alias_package(tmp_path, ambiguous=True)
    _write_object_mapping(tmp_path, "制造件")
    called = False
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    def fake_handle_business_data_search(**kwargs):
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search",
        fake_handle_business_data_search,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "制造件",
            "query": "查询制造件",
        },
        context=_business_data_tool_context(),
    )

    assert called is False
    assert outcome.content is not None
    assert "ambiguous" in outcome.content
    assert "正式对象" in outcome.content
    assert "备选对象" in outcome.content
    assert "object_context_usage" not in outcome.state_updates


def test_business_data_search_tool_allows_exact_mapping_only_legacy_entity(
    tmp_path,
    monkeypatch,
):
    _write_object_mapping(tmp_path, "legacy_entity")
    captured = _install_search_fakes(monkeypatch)
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "legacy_entity",
            "query": "查询历史对象",
        },
        context=_business_data_tool_context(),
    )

    assert outcome.content == "查询结果"
    assert captured["request"].entity == "legacy_entity"
    assert outcome.state_updates["object_context_usage"] == [{"entity_name": "legacy_entity", "iteration": 2}]


def test_business_data_search_tool_rejects_non_exact_mapping_only_entity(
    tmp_path,
    monkeypatch,
):
    _write_object_mapping(tmp_path, "legacy_entity")
    called = False
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    def fake_handle_business_data_search(**kwargs):
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search",
        fake_handle_business_data_search,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "LEGACY_ENTITY",
            "query": "查询历史对象",
        },
        context=_business_data_tool_context(),
    )

    assert called is False
    assert outcome.content == "Error: object directory not found."
    assert "object_context_usage" not in outcome.state_updates


@pytest.mark.parametrize("with_object_catalog", [True, False])
def test_business_data_search_tool_allows_graph_schema_only_entity(
    tmp_path,
    monkeypatch,
    with_object_catalog,
):
    if with_object_catalog:
        objects_dir = tmp_path / "objects"
        objects_dir.mkdir()
        (objects_dir / "index.md").write_text(
            "| object key |\n| --- |\n| DeclaredObject |\n",
            encoding="utf-8",
        )
        (objects_dir / "declared.md").write_text(
            "---\nresource: object\nkey: DeclaredObject\n---\n",
            encoding="utf-8",
        )
    captured = {"resolve_count": 0}
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    def fake_resolve_business_data_source(**kwargs):
        captured["resolve_count"] += 1
        return ResolvedBusinessDataSource(
            ontology_id=kwargs["ontology_id"],
            mode="direct_provider",
            provider="neo4j",
            config={},
        )

    async def fake_handle_business_data_search_async(**kwargs):
        captured.update(kwargs)
        return "查询结果"

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        fake_resolve_business_data_source,
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search_async",
        fake_handle_business_data_search_async,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "SchemaOnlyEntity",
            "query": "查询图模式对象",
        },
        context=_business_data_tool_context(),
    )

    assert outcome.content == "查询结果"
    assert captured["entity"] == "SchemaOnlyEntity"
    assert captured["_resolved_connection"].provider == "neo4j"
    assert captured["resolve_count"] == 1
    assert "object_context_usage" not in outcome.state_updates


def test_business_data_search_tool_rejects_schema_only_entity_for_non_graph_provider(
    tmp_path,
    monkeypatch,
):
    called = False
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        lambda **kwargs: ResolvedBusinessDataSource(
            ontology_id=kwargs["ontology_id"],
            mode="direct_provider",
            provider="nl2sql_http",
            config={},
        ),
    )

    def fake_handle_business_data_search(**kwargs):
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search",
        fake_handle_business_data_search,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "SchemaOnlyEntity",
            "query": "查询非图数据源对象",
        },
        context=_business_data_tool_context(),
    )

    assert called is False
    assert outcome.content == "Error: object directory not found."
    assert "object_context_usage" not in outcome.state_updates


def test_business_data_search_tool_stops_on_object_catalog_read_error(
    tmp_path,
    monkeypatch,
):
    objects_dir = tmp_path / "objects"
    objects_dir.mkdir()
    (objects_dir / "index.md").write_bytes(b"\xff\xfe")
    source_called = False
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    def fake_resolve_business_data_source(**kwargs):
        nonlocal source_called
        source_called = True
        return ResolvedBusinessDataSource(
            ontology_id=kwargs["ontology_id"],
            mode="direct_provider",
            provider="neo4j",
            config={},
        )

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        fake_resolve_business_data_source,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "PossibleAlias",
            "query": "查询图对象",
        },
        context=_business_data_tool_context(),
    )

    assert source_called is False
    assert outcome.content == "Error: Failed to decode objects/index.md as UTF-8."
    assert "object_context_usage" not in outcome.state_updates


def test_business_data_search_tool_rejects_invalid_utf8_mapping_before_provider(
    tmp_path,
    monkeypatch,
):
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir()
    (mappings_dir / "object-data-mappings.json").write_bytes(b"\xff\xfe")
    source_called = False
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )

    def fake_resolve_business_data_source(**kwargs):
        nonlocal source_called
        source_called = True
        return ResolvedBusinessDataSource(
            ontology_id=kwargs["ontology_id"],
            mode="direct_provider",
            provider="nl2sql_http",
            config={},
        )

    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_business_data_source",
        fake_resolve_business_data_source,
    )

    outcome = BusinessDataSearchTool().execute(
        args={
            "ontology_id": "demo-ontology",
            "entity": "LegacyEntity",
            "query": "查询历史对象",
        },
        context=_business_data_tool_context(),
    )

    assert source_called is False
    assert outcome.content == "Error: Failed to decode mappings/object-data-mappings.json as UTF-8."
    assert "object_context_usage" not in outcome.state_updates


def test_business_data_search_tool_validates_required_text_before_loading_ontology(
    monkeypatch,
):
    manifest_called = False

    def fake_get_ontology_manifest(ontology_id, *, version=None, workspace_id=None):
        nonlocal manifest_called
        del version, workspace_id
        manifest_called = True
        return object(), None

    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        fake_get_ontology_manifest,
    )
    tool = BusinessDataSearchTool()
    context = _business_data_tool_context()

    missing_entity = tool.execute(
        args={"ontology_id": "demo", "entity": "  ", "query": "查询"},
        context=context,
    )
    missing_query = tool.execute(
        args={"ontology_id": "demo", "entity": "ObjectA", "query": "  "},
        context=context,
    )

    assert missing_entity.content == "Error: entity cannot be empty"
    assert "query is required" in missing_query.content
    assert manifest_called is False
