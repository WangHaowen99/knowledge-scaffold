"""Tests for MCP search tool catalog compatibility."""

from __future__ import annotations

from api.protocols.mcp import server as mcp_server
from api.protocols.mcp.tool_contracts import normalize_search_tool_args
from integration.mcp.service_catalog import list_search_tools, load_mcp_service_catalog


def test_mcp_search_extra_tools_are_advertised_as_supported():
    descriptions = {item["name"]: item["description"] for item in list_search_tools()}

    for name in ("hop_search", "shortest_path", "list_logic_titles", "get_logic_content"):
        assert name in descriptions
        assert "暂不支持" not in descriptions[name]


def test_mcp_pattern_search_description_guides_path_direction():
    descriptions = {item["name"]: item["description"] for item in list_search_tools()}

    assert "relation_triples" in descriptions["pattern_search"]
    assert "->" in descriptions["pattern_search"]
    assert "<-" in descriptions["pattern_search"]


def test_mcp_pattern_search_normalizes_json_string_and_rejects_wrapped_path():
    args, issue = normalize_search_tool_args(
        "pattern_search",
        {
            "path_pattern": '[["A",{}],["R","->"],["B",{}]]',
            "return_vars": '["a","r1","b"]',
        },
    )

    assert issue is None
    assert args["path_pattern"] == [["A", {}], ["R", "->"], ["B", {}]]
    assert args["return_vars"] == ["a", "r1", "b"]

    args, issue = normalize_search_tool_args(
        "pattern_search",
        {
            "path_pattern": [[["A", {}], ["R", "->"], ["B", {}]]],
            "return_vars": None,
        },
    )

    assert args is None
    assert issue is not None
    assert issue.field == "path_pattern"
    assert "多包了一层数组" in issue.detail


def test_mcp_service_catalog_can_load_yaml(monkeypatch, tmp_path):
    catalog = tmp_path / "mcp_services.yaml"
    catalog.write_text(
        """
services:
  - id: custom-search
    enabled: true
    toolset: search
    title: 自定义检索
    summary: 自定义原子工具集
    description: 自定义 MCP search 服务。
    icon: search
    tags: [custom]
    endpoint_prefix: /custom-search
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_SERVICES_CONFIG_FILE", str(catalog))

    items = load_mcp_service_catalog()

    assert len(items) == 1
    assert items[0].service_id == "custom-search"
    assert items[0].endpoint_prefix == "/custom-search"


def test_mcp_server_registers_exactly_fourteen_private_search_tools():
    registered = []

    class FakeMCP:
        @staticmethod
        def tool():
            def decorate(func):
                registered.append(func.__name__)
                return func

            return decorate

    mcp_server.register_search_tools(
        FakeMCP(),
        call_with_scope=lambda *_args, **_kwargs: {},
    )

    assert registered == [
        "get_object_types",
        "get_relation_types",
        "get_nodes_schema",
        "get_edges_schema",
        "property_filter",
        "pattern_search",
        "count_search",
        "aggregate_search",
        "sorted_search",
        "get_element_info",
        "hop_search",
        "shortest_path",
        "list_logic_titles",
        "get_logic_content",
    ]
