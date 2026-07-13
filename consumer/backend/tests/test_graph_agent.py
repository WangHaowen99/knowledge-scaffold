"""Tests for JiuwenGraphQueryAgent internal graph tool loop."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent.exceptions import WorkflowCancelledError
from agent.tools.business_data.providers.graph.agent import (
    GraphAgentTimeoutError,
    _GRAPH_TOOL_SCHEMAS,
)
from agent.tools.business_data.providers.graph.jiuwen_agent import (
    GraphJiuwenToolAdapter,
    JiuwenGraphQueryAgent,
    _resolve_graph_jiuwen_llm_config,
    register_graph_runtime_tools,
)
from agent.tools.business_data.providers.graph.models import (
    GraphBackendResult,
    GraphEntityMapping,
    GraphScope,
    GraphSearchRuntimeContext,
)
from agent.tools.business_data.providers.graph.tools.executor import GraphToolExecutor
from agent.tools.base import ToolInvocation
from agent.tools.registry import get_tool_spec


class _Backend:
    def __init__(self):
        self.calls = []

    def get_nodes_schema(self, *, scope, element_types=None):
        self.calls.append(("get_nodes_schema", scope.workspace_value, element_types))
        return {"labels": ["CHANGE_ORDER_affected_obj"], "schema": {"CHANGE_ORDER_affected_obj": {"CHANGE_ORDER_ID": "STRING"}}}

    def get_edges_schema(self, *, scope, relation_types=None):
        self.calls.append(("get_edges_schema", scope.workspace_value, relation_types))
        return {"relationship_types": ["D2O_RELATION"]}

    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        return GraphBackendResult(records=[{"properties": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}], total=1)

    def count_search(self, *, element_class, element_type, filter_dict, scope):
        self.calls.append(("count_search", element_class, element_type, filter_dict, scope.workspace_value))
        return GraphBackendResult(total=1)

    def aggregate_search(self, *, element_class, element_type, target_property, agg, filter_dict, scope):
        self.calls.append(("aggregate_search", element_class, element_type, target_property, agg, filter_dict, scope.workspace_value))
        return GraphBackendResult(records=[{"value": 1, "agg": agg}], total=1)

    def sorted_search(self, *, element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope):
        self.calls.append(("sorted_search", element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope.workspace_value))
        return GraphBackendResult(records=[{"properties": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}], total=1)

    def pattern_search(self, *, path_pattern, return_vars, limit, scope, return_properties=None):
        self.calls.append(("pattern_search", path_pattern, return_vars, return_properties, limit, scope.workspace_value))
        return GraphBackendResult(
            records=[
                {
                    "nodes": [
                        {"element_id": "v-e1", "labels": ["SourceItemVersion"], "properties": {"SourceItemCode": "101000-001"}},
                        {"element_id": "v-m1", "labels": ["TargetItem"], "properties": {"TargetItemCode": "A001"}},
                    ],
                    "relationships": [
                        {"element_id": "r1", "type": "D2O_RELATION", "properties": {"relation_type": "PROCESS_TO"}},
                    ],
                }
            ],
            total=1,
        )

    def get_element_info(self, *, element_instance_id, element_class="", element_type="", scope):
        self.calls.append(("get_element_info", element_instance_id, element_class, element_type, scope.workspace_value))
        return {"found": False, "element_id": element_instance_id}


class _DuplicateTargetItemBackend(_Backend):
    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        return GraphBackendResult(
            records=[{"element_id": "m-part-a001", "properties": {"TargetItemCode": "A001"}}],
            total=1,
        )

    def sorted_search(self, *, element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope):
        self.calls.append(("sorted_search", element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope.workspace_value))
        return GraphBackendResult(
            records=[{"element_id": "m-part-a001", "properties": {"TargetItemCode": "A001"}}],
            total=1,
        )


def _context():
    return GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="CHANGE_ORDER受影响对象",
        query="查询CHANGE_ORDER受影响对象中CHANGE_ORDER编码为\"CHANGE_ORDER-001\"的所有记录",
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(
            workspace_property="d2o_workspace_id",
            workspace_value="workspace-a",
            ontology_property="d2o_ontology_id",
            ontology_value="demo-ontology",
        ),
        mapping=GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="path", source="test"),
        provider_config={},
    )


def _executor(backend, context, mapping):
    return GraphToolExecutor(backend=backend, context=context, mapping=mapping)


def _record_context():
    return GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="SourceItem与TargetItem工艺关系",
        query='查询SourceItem编码为"101001-001"且SourceItem版本为"A"的工艺关系记录',
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(
            workspace_property="d2o_workspace_id",
            workspace_value="workspace-a",
            ontology_property="d2o_ontology_id",
            ontology_value="demo-ontology",
        ),
        mapping=GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="node", source="test", label="SourceItem_TargetItem"),
        provider_config={},
        schema_context={
            "source": "provider_preload",
            "nodes_schema": {
                "labels": ["SourceItem_TargetItem"],
                "schema": {"SourceItem_TargetItem": {"SourceItemCode": "STRING", "TargetItemCode": "STRING", "SourceItemVersion": "STRING"}},
            },
            "edges_schema": {
                "relationship_types": ["D2O_RELATION"],
                "schema": {"D2O_RELATION": {"relation_type": "STRING"}},
                "relation_triples": [
                    {
                        "source_label": "SourceItemVersion",
                        "relation_type": "PROCESS_TO",
                        "type": "D2O_RELATION",
                        "target_label": "TargetItem",
                    }
                ],
            },
            "query_analysis": {
                "path_intent": False,
                "preferred_tool": "property_filter",
                "record_query": True,
            },
            "mapping_intent": {
                "resource_kind": "record_node",
                "is_relationship_record": True,
                "record_query": True,
                "associated_object_detail": False,
                "prefer_tool": "property_filter",
                "pattern_search_required": False,
                "property_filter_sufficient_after_hit": True,
            },
            "disable_schema_tools": True,
        },
    )


def test_jiuwen_graph_query_agent_executes_private_graph_tool_adapter():
    invocations = []

    def fake_invoker(payload):
        invocations.append(payload)
        assert payload["attempt"] == 1
        return payload["adapter"]._invoke_sync(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "CHANGE_ORDER_affected_obj",
                        "element_type": "NODE",
                        "filter_dict": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"},
                        "return_properties": ["CHANGE_ORDER_ID"],
                        "limit": 20,
                    }
                ]
            },
        )

    context = _context()
    mapping = GraphEntityMapping(
        entity="CHANGE_ORDER受影响对象",
        kind="node",
        source="test",
        label="CHANGE_ORDER_affected_obj",
        property_map={"CHANGE_ORDER编码": "CHANGE_ORDER_ID"},
    )
    backend = _Backend()

    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert result.backend_result.records == [{"properties": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}]
    assert result.executed_steps == [
        {"tool": "property_filter", "summary": "执行内部图工具 property_filter，返回 1 条"}
    ]
    assert len(invocations) == 1
    assert backend.calls == [
        ("property_filter", "CHANGE_ORDER_affected_obj", "NODE", {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}, ["CHANGE_ORDER_ID"], 20, 0, "workspace-a")
    ]


def test_jiuwen_graph_query_agent_retries_when_model_does_not_call_tool():
    invocations = []

    def fake_invoker(payload):
        invocations.append(payload)
        if payload["attempt"] == 1:
            return "查询完成，但没有调用内部图工具"
        return payload["adapter"]._invoke_sync(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "CHANGE_ORDER_affected_obj",
                        "element_type": "NODE",
                        "filter_dict": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"},
                        "return_properties": ["CHANGE_ORDER_ID"],
                        "limit": 20,
                    }
                ]
            },
        )

    context = _context()
    mapping = GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="node", source="test", label="CHANGE_ORDER_affected_obj")
    backend = _Backend()

    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert len(invocations) == 2
    assert invocations[0]["invocation_id"] != invocations[1]["invocation_id"]
    assert "上一轮没有调用任何内部图检索工具" in invocations[1]["query"]
    assert result.executed_steps == [
        {"tool": "GraphQueryAgent", "summary": "上一轮未调用内部图工具，使用隔离会话重试"},
        {"tool": "property_filter", "summary": "执行内部图工具 property_filter，返回 1 条"},
    ]


def test_jiuwen_graph_query_agent_retries_pattern_search_for_path_intent():
    invocations = []

    def fake_invoker(payload):
        invocations.append(payload)
        if payload["attempt"] == 1:
            return payload["adapter"]._invoke_sync(
                "property_filter",
                {
                    "queries": [
                        {
                            "element_class": "SourceItem_TargetItem",
                            "element_type": "NODE",
                            "filter_dict": {"SourceItemCode": "101000-001"},
                            "return_properties": ["SourceItemCode", "TargetItemCode"],
                        }
                    ]
                },
            )
        return payload["adapter"]._invoke_sync(
            "pattern_search",
            {
                "path_pattern": [
                    [{"label": "SourceItemVersion", "props": {"SourceItemCode": "101000-001"}}],
                    ["D2O_RELATION", {"props": {"relation_type": "PROCESS_TO"}}],
                    [{"label": "TargetItem"}],
                ],
                "return_vars": ["a", "r1", "b"],
                "return_properties": {"a": ["SourceItemCode"], "b": ["TargetItemCode"]},
                "limit": 20,
            },
        )

    context = GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="SourceItem与TargetItem工艺关系",
        query="查询 SourceItem 101000-001 对应的 TargetItem",
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(
            workspace_property="d2o_workspace_id",
            workspace_value="workspace-a",
            ontology_property="d2o_ontology_id",
            ontology_value="demo-ontology",
        ),
        mapping=GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="node", source="test", label="SourceItem_TargetItem"),
        provider_config={},
        schema_context={
            "source": "provider_preload",
            "nodes_schema": {
                "labels": ["SourceItemVersion", "SourceItem_TargetItem", "TargetItem"],
                "schema": {
                    "SourceItemVersion": {"SourceItemCode": "STRING"},
                    "SourceItem_TargetItem": {"SourceItemCode": "STRING", "TargetItemCode": "STRING"},
                    "TargetItem": {"TargetItemCode": "STRING"},
                },
            },
            "edges_schema": {
                "relationship_types": ["D2O_RELATION"],
                "schema": {"D2O_RELATION": {"relation_type": "STRING"}},
                "relation_triples": [
                    {
                        "source_label": "SourceItemVersion",
                        "relation_type": "PROCESS_TO",
                        "type": "D2O_RELATION",
                        "target_label": "TargetItem",
                    }
                ],
            },
            "query_analysis": {"path_intent": True, "preferred_tool": "pattern_search"},
            "disable_schema_tools": True,
        },
    )
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="node", source="test", label="SourceItem_TargetItem")
    backend = _Backend()

    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert [record["tool"] for record in result.backend_result.records] == ["property_filter", "pattern_search"]
    assert len(invocations) == 2
    assert any("追加一次路径检索修正" in step["summary"] for step in result.executed_steps)


def test_jiuwen_graph_query_agent_skips_pattern_search_after_sufficient_record_hit():
    def fake_invoker(payload):
        property_payload = payload["adapter"]._invoke_sync(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "SourceItem_TargetItem",
                        "element_type": "NODE",
                        "filter_dict": {"SourceItemCode": "101001-001", "SourceItemVersion": "A"},
                        "return_properties": ["SourceItemCode", "TargetItemCode", "SourceItemVersion"],
                    }
                ]
            },
        )
        skipped_payload = payload["adapter"]._invoke_sync(
            "pattern_search",
            {
                "path_pattern": [
                    {"var": "e", "label": "REF_SOURCE_ITEM", "props": {"SourceItemCode": "101001-001"}},
                    {"var": "r", "type": "D2O_RELATION", "props": {"relation_type": "PROCESS_TO"}},
                    {"var": "m", "label": "TargetItem"},
                ],
                "return_vars": ["e", "r", "m"],
            },
        )
        assert '"sufficient_for_query": true' in property_payload
        assert '"skipped": true' in skipped_payload
        assert "record_query_already_satisfied_by_property_filter" in skipped_payload
        return skipped_payload

    context = _record_context()
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="node", source="test", label="SourceItem_TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert result.backend_result.payload["sufficient_for_query"] is True
    assert result.backend_result.payload["recommended_next_action"] == "final"
    assert [call[0] for call in backend.calls] == ["property_filter"]
    assert result.executed_steps == [
        {"tool": "property_filter", "summary": "执行内部图工具 property_filter，返回 1 条"}
    ]


def test_jiuwen_graph_query_agent_uses_isolated_conversation_for_each_run():
    created_agents = []
    registrar_calls = []

    class _FakeAgent:
        def __init__(self):
            self.card = SimpleNamespace(id="")
            self.inputs = []
            self.adapter = None

        def invoke(self, inputs):
            self.inputs.append(inputs)
            return self.adapter._invoke_sync(
                "property_filter",
                {
                    "queries": [
                        {
                            "element_class": "CHANGE_ORDER_affected_obj",
                            "element_type": "NODE",
                            "filter_dict": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"},
                            "return_properties": ["CHANGE_ORDER_ID"],
                            "limit": 20,
                        }
                    ]
                },
            )

    def fake_agent_factory():
        agent = _FakeAgent()
        created_agents.append(agent)
        return agent

    def fake_tool_registrar(agent, **kwargs):
        registrar_calls.append(kwargs)
        agent.adapter = kwargs["adapter"]
        agent.card.id = "fake-agent.{}".format(kwargs["invocation_id"])
        return []

    context = _context()
    mapping = GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="node", source="test", label="CHANGE_ORDER_affected_obj")

    for _ in range(2):
        JiuwenGraphQueryAgent(
            agent_factory=fake_agent_factory,
            tool_registrar=fake_tool_registrar,
        ).run(
            context=context,
            mapping=mapping,
            tool_executor=_executor(_Backend(), context, mapping),
        )

    invocation_ids = [call["invocation_id"] for call in registrar_calls]
    conversation_ids = [agent.inputs[0]["conversation_id"] for agent in created_agents]
    assert len(set(invocation_ids)) == 2
    assert len(set(conversation_ids)) == 2
    assert all(invocation_id in conversation_id for invocation_id, conversation_id in zip(invocation_ids, conversation_ids))


def test_jiuwen_graph_query_agent_skips_duplicate_property_filter_after_hit():
    args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001"},
                "return_properties": ["TargetItemCode"],
                "limit": 20,
            }
        ]
    }

    def fake_invoker(payload):
        payload["adapter"]._invoke_sync("property_filter", args)
        return payload["adapter"]._invoke_sync("property_filter", args)

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert len(backend.calls) == 1
    assert any("跳过重复 property_filter 子查询 1 个" in step["summary"] for step in result.executed_steps)


def test_jiuwen_graph_query_agent_skips_duplicate_property_filter_query_in_same_call():
    args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001"},
                "return_properties": ["TargetItemCode"],
                "limit": 20,
            },
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001"},
                "return_properties": ["TargetItemCode"],
                "limit": 20,
            },
        ]
    }

    def fake_invoker(payload):
        return payload["adapter"]._invoke_sync("property_filter", args)

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert len(backend.calls) == 1
    assert any("跳过重复 property_filter 子查询 1 个" in step["summary"] for step in result.executed_steps)


def test_jiuwen_graph_query_agent_skips_broader_fuzzy_property_filter_after_exact_hit():
    exact_args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001"},
                "return_properties": ["TargetItemCode", "TargetItemName"],
                "limit": 20,
            }
        ]
    }
    fuzzy_args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": {"contains": "A001"}},
                "return_properties": ["TargetItemCode"],
                "limit": 20,
            }
        ]
    }

    def fake_invoker(payload):
        payload["adapter"]._invoke_sync("property_filter", exact_args)
        return payload["adapter"]._invoke_sync("property_filter", fuzzy_args)

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "property_filter"
    assert len(backend.calls) == 1
    assert any("跳过重复 property_filter 子查询 1 个" in step["summary"] for step in result.executed_steps)


def test_jiuwen_graph_query_agent_keeps_narrower_property_filter_after_exact_hit():
    exact_args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001"},
                "return_properties": ["TargetItemCode"],
                "limit": 20,
            }
        ]
    }
    narrower_args = {
        "queries": [
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": "A001", "InventoryStatus": {"ne": "总装在制"}},
                "return_properties": ["TargetItemCode", "InventoryStatus"],
                "limit": 20,
            }
        ]
    }

    def fake_invoker(payload):
        payload["adapter"]._invoke_sync("property_filter", exact_args)
        return payload["adapter"]._invoke_sync("property_filter", narrower_args)

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert len(backend.calls) == 2
    assert not any("跳过重复 property_filter" in step["summary"] for step in result.executed_steps)


def test_jiuwen_graph_query_agent_deduplicates_overlapping_data_tool_results():
    def fake_invoker(payload):
        payload["adapter"]._invoke_sync(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "TargetItem",
                        "element_type": "NODE",
                        "filter_dict": {"TargetItemCode": "A001"},
                        "return_properties": ["TargetItemCode"],
                        "limit": 20,
                    }
                ]
            },
        )
        return payload["adapter"]._invoke_sync(
            "sorted_search",
            {
                "element_class": "TargetItem",
                "element_type": "NODE",
                "filter_dict": {"TargetItemCode": {"contains": "A001"}},
                "return_properties": ["TargetItemCode"],
                "sort_by": ["TargetItemCode"],
                "limit": 20,
            },
        )

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _DuplicateTargetItemBackend()
    result = JiuwenGraphQueryAgent(invoker=fake_invoker).run(
        context=context,
        mapping=mapping,
        tool_executor=_executor(backend, context, mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert result.backend_result.total == 1
    assert result.backend_result.payload["result"]["raw_total_record_count"] == 2
    assert result.backend_result.payload["result"]["deduplicated"] is True
    assert result.backend_result.records[0]["record_count"] == 1
    assert result.backend_result.records[1]["record_count"] == 0
    assert len(backend.calls) == 2


def test_jiuwen_graph_query_agent_timeout_raises_structured_error():
    def slow_invoker(payload):
        del payload
        time.sleep(0.05)
        return ""

    context = _context()
    context.provider_config["graph_agent_timeout_seconds"] = 0.001
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")

    with pytest.raises(GraphAgentTimeoutError) as exc_info:
        JiuwenGraphQueryAgent(invoker=slow_invoker).run(
            context=context,
            mapping=mapping,
            tool_executor=_executor(_Backend(), context, mapping),
        )

    assert exc_info.value.error_code == "GRAPH_AGENT_TIMEOUT"


def test_graph_agent_exposes_exact_private_fourteen_tool_contract():
    names = [schema["function"]["name"] for schema in _GRAPH_TOOL_SCHEMAS]

    assert names == [
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
    assert all(get_tool_spec(name) is None for name in names)


def test_graph_agent_reuses_current_jiuwen_workspace_profile_resolution(monkeypatch):
    calls = []

    def fake_resolve(state, *, caller, agent_config=None):
        calls.append((state, caller, agent_config))
        return SimpleNamespace(
            provider="openai",
            model="graph-model",
            max_tokens=8192,
            temperature=0.9,
        )

    monkeypatch.setattr(
        "agent.tools.business_data.providers.graph.jiuwen_agent._resolve_jiuwen_llm_config",
        fake_resolve,
    )

    config = _resolve_graph_jiuwen_llm_config(
        {
            "model_profile_id": "workspace:graph",
            "graph_agent_max_tokens": 1200,
            "graph_agent_temperature": 0.1,
        },
        workspace_id="workspace-a",
    )

    assert calls == [(
        {
            "workspace_id": "workspace-a",
            "user_context": {"model_profile_id": "workspace:graph"},
        },
        "subagent",
        None,
    )]
    assert config.model == "graph-model"
    assert config.max_tokens == 1200
    assert config.temperature == 0.1


def test_graph_private_registration_passes_explicit_tools_to_current_jiuwen_boundary(monkeypatch):
    captured = {}

    def fake_register(agent, **kwargs):
        captured["agent"] = agent
        captured.update(kwargs)
        return [SimpleNamespace(id="private-card")]

    monkeypatch.setattr(
        "agent.tools.business_data.providers.graph.jiuwen_agent.register_current_runtime_tools",
        fake_register,
    )
    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    adapter = GraphJiuwenToolAdapter(
        tool_executor=_executor(_DuplicateTargetItemBackend(), context, mapping),
        executions=[],
        executed_steps=[],
    )
    agent = SimpleNamespace(card=SimpleNamespace(id="graph-private"))

    cards = register_graph_runtime_tools(
        agent,
        tool_schemas=_GRAPH_TOOL_SCHEMAS,
        adapter=adapter,
        context=context,
    )

    assert [card.id for card in cards] == ["private-card"]
    assert [tool["name"] for tool in captured["tool_infos"]] == [
        schema["function"]["name"] for schema in _GRAPH_TOOL_SCHEMAS
    ]
    assert captured["state"]["messages"] == []
    private_context = captured["context_factory"]()
    assert private_context.caller == "graph"
    assert private_context.observer is None

    result = captured["tool_batch_executor"](
        [
            ToolInvocation(
                id="graph-call-1",
                name="property_filter",
                arguments={
                    "element_class": "TargetItem",
                    "element_type": "NODE",
                    "filter_dict": {"TargetItemCode": "A001"},
                    "return_properties": ["TargetItemCode"],
                },
            )
        ],
        private_context,
    )
    assert len(result.messages) == 1
    assert '"success": true' in result.messages[0]["content"]


def test_graph_agent_cancellation_waits_for_owned_worker_to_exit():
    finished = threading.Event()

    def slow_invoker(_payload):
        time.sleep(0.02)
        finished.set()
        return ""

    context = _context()
    cancel_event = threading.Event()
    cancel_event.set()
    context.provider_config["_cancel_event"] = cancel_event
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")

    with pytest.raises(WorkflowCancelledError):
        JiuwenGraphQueryAgent(invoker=slow_invoker).run(
            context=context,
            mapping=mapping,
            tool_executor=_executor(_Backend(), context, mapping),
        )

    assert finished.is_set()
    assert "_graph_cancel_event" not in context.provider_config
