"""Tests for GraphSearchRuntime orchestration."""

from __future__ import annotations

import json

from agentic.business_data.providers.graph.agent import GraphAgentError, GraphAgentTimeoutError
from agentic.business_data.providers.graph.models import (
    GraphAgentResult,
    GraphBackendResult,
    GraphEntityMapping,
)
from agentic.business_data.providers.graph.runtime import GraphSearchRuntime


class _StaticResolver:
    def __init__(self, mapping):
        self.mapping = mapping

    def resolve(self, entity):
        self.entity = entity
        return self.mapping


class _FakeBackend:
    def __init__(self):
        self.calls = []

    def get_nodes_schema(self, *, scope, element_types=None):
        self.calls.append(("get_nodes_schema", scope.workspace_value, element_types))
        return {"labels": ["TargetItem"], "schema": {"TargetItem": {"TargetItemCode": "STRING"}}}

    def get_edges_schema(self, *, scope, relation_types=None):
        self.calls.append(("get_edges_schema", scope.workspace_value, relation_types))
        return {
            "relationship_types": ["D2O_RELATION"],
            "schema": {"D2O_RELATION": {"relation_type": "STRING"}},
            "relation_triples": [
                {
                    "source_label": "SourceItemVersion",
                    "relation_type": "PROCESS_TO",
                    "type": "D2O_RELATION",
                    "target_label": "TargetItem",
                    "edge_count": 3,
                }
            ],
        }

    def property_filter(self, **kwargs):
        self.calls.append(("property_filter", kwargs))
        return GraphBackendResult(records=[{"properties": {"eco_id": "CHANGE_ORDER-001"}}], total=1)

    def count_search(self, **kwargs):
        self.calls.append(("count_search", kwargs))
        return GraphBackendResult(total=3)

    def pattern_search(self, **kwargs):
        self.calls.append(("pattern_search", kwargs))
        return GraphBackendResult(records=[])

    def close(self):
        return None


class _StaticGraphAgent:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _TimeoutGraphAgent:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        raise GraphAgentTimeoutError("GraphQueryAgent 调用超时（1s）")


class _ErrorGraphAgent:
    def run(self, **kwargs):
        del kwargs
        raise GraphAgentError("GraphQueryAgent 未调用可用图检索工具")


def _provider_config():
    return {
        "workspace_id": "workspace-a",
        "ontology_id": "demo-ontology",
        "_workspace_root": "/tmp/ontologies",
        "workspace_property": "d2o_workspace_id",
    }


def _write_runtime_ontology(tmp_path):
    workspace_root = tmp_path / "ontologies"
    ontology_root = workspace_root / "demo-ontology"
    version_dir = ontology_root / "versions" / "1.0.0"
    (version_dir / "operations").mkdir(parents=True)
    (version_dir / "rules").mkdir()
    (version_dir / "scripts").mkdir()
    ontology_root.mkdir(parents=True, exist_ok=True)
    (ontology_root / "manifest.yaml").write_text(
        "\n".join([
            "kind: ontology",
            "id: demo-ontology",
            "name: Demo",
            "description: Demo",
            "version: 1.0.0",
            "active_version: 1.0.0",
            "versions:",
            "  - 1.0.0",
            "language: zh-CN",
            "",
        ]),
        encoding="utf-8",
    )
    (version_dir / "operations-list.md").write_text(
        "| 名称 | 业务描述 |\n| --- | --- |\n| TargetItem成本计算 | 使用 TargetItem 计算成本 |",
        encoding="utf-8",
    )
    (version_dir / "operations" / "TargetItem成本计算.md").write_text(
        "# TargetItem成本计算\n\n使用 [[TargetItem]] 的 TargetItemCode 和 MaterialCost 计算成本。",
        encoding="utf-8",
    )
    (version_dir / "rules" / "TargetItem成本规则.md").write_text(
        "# TargetItem成本规则\n\nTargetItem 成本只能使用 MaterialCost 字段。",
        encoding="utf-8",
    )
    (version_dir / "scripts-list.md").write_text(
        "- `TargetItem成本计算` -> `scripts/target_item_cost.py`",
        encoding="utf-8",
    )
    (version_dir / "scripts" / "target_item_cost.py").write_text(
        "# TargetItem cost script\nARG = 'TargetItemCode'\n",
        encoding="utf-8",
    )
    return workspace_root


def _agent_result(tool_name="property_filter"):
    return GraphAgentResult(
        tool_name=tool_name,
        tool_args={"queries": [{"element_class": "ChangeOrderAffectedObject", "element_type": "NODE"}]},
        backend_result=GraphBackendResult(
            records=[{"properties": {"eco_id": "CHANGE_ORDER-001"}}],
            total=1,
            truncated=False,
        ),
        executed_steps=[{"tool": tool_name, "summary": "执行内部图工具 {}".format(tool_name)}],
    )


def test_runtime_uses_graph_agent_internal_tool_result_with_forced_scope():
    backend = _FakeBackend()
    mapping = GraphEntityMapping(
        entity="eco_affected_object",
        kind="node",
        source="test",
        label="ChangeOrderAffectedObject",
        property_map={"CHANGE_ORDER_ID": "eco_id"},
    )
    agent = _StaticGraphAgent(_agent_result())
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=_provider_config(),
        backend=backend,
        mapping_resolver=_StaticResolver(mapping),
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询 CHANGE_ORDER_ID 为 CHANGE_ORDER-001 的对象", entity="eco_affected_object"))
    final = json.loads(events[-1]["content"])

    assert final["success"] is True
    assert final["provider"] == "neo4j"
    assert final["records"] == [{"properties": {"eco_id": "CHANGE_ORDER-001"}}]
    assert final["evidence"][0]["tool"] == "property_filter"
    assert set(final["evidence"][0]) == {
        "evidence_id",
        "provider",
        "entity",
        "tool",
        "mapping_source",
        "scope",
        "record_count",
        "query_digest",
    }
    assert agent.calls[0]["context"].scope.workspace_value == "workspace-a"


def test_runtime_missing_mapping_does_not_call_graph_agent_or_backend():
    backend = _FakeBackend()
    agent = _StaticGraphAgent(_agent_result())
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=_provider_config(),
        backend=backend,
        mapping_resolver=_StaticResolver(None),
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询对象", entity="missing"))
    final = json.loads(events[-1]["content"])

    assert final["success"] is False
    assert final["error_code"] == "GRAPH_MAPPING_NOT_FOUND"
    assert backend.calls == []
    assert agent.calls == []


def test_runtime_can_fallback_to_exact_schema_label():
    backend = _FakeBackend()
    agent = _StaticGraphAgent(_agent_result())
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=_provider_config(),
        backend=backend,
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询 TargetItem", entity="TargetItem"))
    final = json.loads(events[-1]["content"])

    assert final["success"] is True
    assert final["entity"] == "TargetItem"
    assert backend.calls[0] == ("get_nodes_schema", "workspace-a", None)
    assert agent.calls[0]["mapping"].source == "graph_schema_exact_match"


def test_runtime_stops_when_graph_agent_times_out():
    backend = _FakeBackend()
    mapping = GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="path", source="test")
    agent = _TimeoutGraphAgent()
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=_provider_config(),
        backend=backend,
        mapping_resolver=_StaticResolver(mapping),
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询对象", entity="CHANGE_ORDER受影响对象"))
    final = json.loads(events[-1]["content"])

    assert final["success"] is False
    assert final["error_code"] == "GRAPH_AGENT_TIMEOUT"
    assert len(agent.calls) == 1


def test_runtime_returns_agent_error_without_backend_query():
    backend = _FakeBackend()
    mapping = GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="path", source="test")
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=_provider_config(),
        backend=backend,
        mapping_resolver=_StaticResolver(mapping),
        graph_agent=_ErrorGraphAgent(),
    )

    events = list(runtime.search_stream(query="查询对象", entity="CHANGE_ORDER受影响对象"))
    final = json.loads(events[-1]["content"])

    assert final["success"] is False
    assert final["error_code"] == "GRAPH_AGENT_ERROR"


def test_runtime_loads_operation_rule_script_context(tmp_path):
    workspace_root = _write_runtime_ontology(tmp_path)
    backend = _FakeBackend()
    mapping = GraphEntityMapping(
        entity="TargetItem",
        kind="node",
        source="test",
        label="TargetItem",
        property_map={"TargetItem编码": "TargetItemCode", "物料成本": "MaterialCost"},
    )
    agent = _StaticGraphAgent(_agent_result())
    provider_config = {
        **_provider_config(),
        "_workspace_root": str(workspace_root),
    }
    runtime = GraphSearchRuntime(
        provider="neo4j",
        provider_config=provider_config,
        backend=backend,
        mapping_resolver=_StaticResolver(mapping),
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询 TargetItem 的物料成本", entity="TargetItem"))
    final = json.loads(events[-1]["content"])
    business_context = agent.calls[0]["context"].business_context
    schema_context = agent.calls[0]["context"].schema_context

    assert final["success"] is True
    assert business_context["catalogs"][0]["path"] == "operations-list.md"
    assert business_context["operations"][0]["path"] == "operations/TargetItem成本计算.md"
    assert "MaterialCost" in business_context["rules"][0]["content"]
    assert business_context["scripts"][0]["path"] == "scripts/target_item_cost.py"
    assert schema_context["node_labels"] == ["TargetItem"]
    assert schema_context["nodes_schema"]["schema"]["TargetItem"]["TargetItemCode"] == "STRING"
    assert schema_context["edges_schema"]["relation_triples"][0]["relation_type"] == "PROCESS_TO"
    assert schema_context["disable_schema_tools"] is True


def test_runtime_marks_path_intent_and_preloads_relation_triples():
    backend = _FakeBackend()
    mapping = GraphEntityMapping(
        entity="SourceItem与TargetItem工艺关系",
        kind="node",
        source="mappings/object-data-mappings.json",
        label="SourceItem_TargetItem",
        property_map={"SourceItem编码": "SourceItemCode", "TargetItem编码": "TargetItemCode"},
    )
    agent = _StaticGraphAgent(_agent_result())
    runtime = GraphSearchRuntime(
        provider="hugegraph",
        provider_config=_provider_config(),
        backend=backend,
        mapping_resolver=_StaticResolver(mapping),
        graph_agent=agent,
    )

    events = list(runtime.search_stream(query="查询 SourceItem 与 TargetItem 的工艺关系", entity="SourceItem与TargetItem工艺关系"))
    final = json.loads(events[-1]["content"])
    schema_context = agent.calls[0]["context"].schema_context

    assert final["success"] is True
    assert schema_context["query_analysis"]["path_intent"] is True
    assert schema_context["query_analysis"]["preferred_tool"] == "pattern_search"
    assert schema_context["edges_schema"]["relation_triples"][0]["source_label"] == "SourceItemVersion"
    assert schema_context["focused_node_labels"] == ["SourceItem_TargetItem"]
