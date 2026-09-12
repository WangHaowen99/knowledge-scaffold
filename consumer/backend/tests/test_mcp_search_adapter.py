from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from api.protocols.mcp.search_adapter import _public_tool_payload, execute_search_tool
from integration.mcp.scope import MCPRequestScope


def test_mcp_search_injects_ontology_version_into_runtime_and_mapping(monkeypatch, tmp_path):
    captured = {}

    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter.resolve_business_data_source",
        lambda *, workspace_id, ontology_id: ResolvedBusinessDataSource(
            ontology_id=ontology_id,
            provider="neo4j",
            config={},
        ),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter.get_workspace_paths",
        lambda _workspace_id: SimpleNamespace(ontologies_dir=tmp_path / "ontologies"),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter.get_workspace",
        lambda _workspace_id: {"name": "Workspace A"},
    )
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter.get_ontology_manifest",
        lambda ontology_id, *, version=None, workspace_id=None: (
            SimpleNamespace(
                id=ontology_id,
                version="1.2.3",
                active_version="1.2.3",
            ),
            None,
        ),
    )

    def fake_resolve_mapping(**kwargs):
        captured["mapping_kwargs"] = kwargs
        return SimpleNamespace()

    monkeypatch.setattr("api.protocols.mcp.search_adapter._resolve_mapping", fake_resolve_mapping)

    def fake_execute_with_runtime(tool_name, args, runtime):
        captured["runtime"] = runtime
        return SimpleNamespace(
            tool_name=tool_name,
            tool_args=dict(args),
            payload={"success": True, "result": {"items": ["Part"], "total": 1}, "message": {"description": "ok"}},
            backend_result=SimpleNamespace(records=[{"label": "Part"}], total=1, truncated=False),
        )

    monkeypatch.setattr("api.protocols.mcp.search_adapter._execute_with_runtime", fake_execute_with_runtime)

    response = execute_search_tool(
        "get_object_types",
        {"ontology_id": "demo-ontology"},
        scope=MCPRequestScope(
            username="alice",
            workspace_id="workspace-a",
            scopes=["search"],
        ),
    )

    assert response["ok"] is True
    assert response["result"]["items"] == ["Part"]
    assert response["upstream"]["provider"] == "neo4j"
    assert captured["mapping_kwargs"]["ontology_version"] == "1.2.3"
    assert captured["runtime"].context.ontology_version == "1.2.3"
    assert captured["runtime"].context.provider_config["ontology_version"] == "1.2.3"


def test_bound_search_uses_token_ontology_when_argument_is_omitted(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter.validate_runtime_ontology_availability",
        lambda **kwargs: captured.update(validated=kwargs),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter._build_runtime",
        lambda **kwargs: captured.update(runtime=kwargs)
        or SimpleNamespace(
            provider="neo4j",
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.search_adapter._execute_with_runtime",
        lambda *_args: SimpleNamespace(
            tool_name="get_object_types",
            payload={"success": True, "result": {"items": []}},
            backend_result=None,
        ),
    )

    result = execute_search_tool(
        "get_object_types",
        {},
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
            ontology_id="knowledge_package_demo",
        ),
    )

    assert result["ok"] is True
    assert captured["runtime"]["ontology_id"] == "knowledge_package_demo"
    assert captured["validated"]["ontology_id"] == "knowledge_package_demo"


def test_bound_search_rejects_conflicting_ontology_before_runtime():
    result = execute_search_tool(
        "get_object_types",
        {"ontology_id": "knowledge_package_other"},
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
            ontology_id="knowledge_package_demo",
        ),
    )

    assert result["ok"] is False
    assert result["error"]["error_code"] == "MCP_ONTOLOGY_SCOPE_MISMATCH"


def test_unbound_search_still_requires_explicit_ontology():
    result = execute_search_tool(
        "get_object_types",
        {},
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
        ),
    )

    assert result["ok"] is False
    assert result["error"]["error_code"] == "MCP_ONTOLOGY_REQUIRED"


def test_public_payload_uses_mcp_alias_for_compatibility_tool():
    execution = SimpleNamespace(
        tool_name="get_edges_schema",
        tool_args={"mcp_alias": "get_relation_types"},
        payload={
            "success": True,
            "result": {"items": ["RELATION"], "total": 1},
            "message": {"description": "ok"},
        },
        backend_result=None,
    )

    response = _public_tool_payload(
        execution,
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
        ),
        provider="neo4j",
        ontology_id="knowledge_package_demo",
    )

    assert response["tool"] == "get_relation_types"


def test_public_payload_falls_back_to_internal_tool_name_without_alias():
    execution = SimpleNamespace(
        tool_name="get_edges_schema",
        tool_args={},
        payload={"success": True, "result": {}, "message": {"description": "ok"}},
        backend_result=None,
    )

    response = _public_tool_payload(
        execution,
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
        ),
        provider="neo4j",
        ontology_id="knowledge_package_demo",
    )

    assert response["tool"] == "get_edges_schema"


def test_public_payload_ignores_unrecognized_alias():
    execution = SimpleNamespace(
        tool_name="get_edges_schema",
        tool_args={"mcp_alias": "property_filter"},
        payload={"success": True, "result": {}, "message": {"description": "ok"}},
        backend_result=None,
    )

    response = _public_tool_payload(
        execution,
        scope=MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:search"],
        ),
        provider="neo4j",
        ontology_id="knowledge_package_demo",
    )

    assert response["tool"] == "get_edges_schema"
