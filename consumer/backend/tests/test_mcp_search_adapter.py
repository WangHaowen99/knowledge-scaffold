from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from api.protocols.mcp.search_adapter import execute_search_tool
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
