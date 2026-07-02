"""Tests for GraphQueryAgent internal tool loop."""

from __future__ import annotations

import time

import pytest

from agentic.business_data.providers.graph.agent import GraphAgentError
from agentic.business_data.providers.graph.agent import GraphAgentTimeoutError
from agentic.business_data.providers.graph.agent import GraphAgentToolInputError
from agentic.business_data.providers.graph.agent import GraphQueryAgent
from agentic.business_data.providers.graph.models import (
    GraphBackendResult,
    GraphEntityMapping,
    GraphScope,
    GraphSearchRuntimeContext,
)
from agentic.business_data.providers.graph.tools.executor import GraphToolExecutor


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


class _RecoveringBackend(_Backend):
    def __init__(self):
        super().__init__()
        self.fail_once = True

    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        if self.fail_once:
            self.fail_once = False
            raise ValueError("filter_dict.SourceItemDesc 包含不支持的操作符: '$contains'")
        return GraphBackendResult(records=[{"properties": {"TargetItemCode": "A001"}}], total=1)


class _FieldEchoBackend(_Backend):
    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        properties = {
            "AffectedObjectCode": "0001",
            "SourceItemVersionIDBefore": "VERSION0003",
            "SourceItemVersionIDAfter": "VERSION0006",
            "MaterialType": "物料替换",
        }
        if return_properties:
            properties = {key: value for key, value in properties.items() if key in return_properties}
        return GraphBackendResult(records=[{"properties": properties}], total=1)


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
        scope=GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a"),
        mapping=GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="path", source="test"),
        provider_config={},
    )


def test_graph_query_agent_executes_internal_tool_loop():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"CHANGE_ORDER_affected_obj",'
                                '"element_type":"NODE","filter_dict":{"CHANGE_ORDER_ID":"CHANGE_ORDER-001"},'
                                '"return_properties":["CHANGE_ORDER_ID"],"limit":20}]}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(
        entity="CHANGE_ORDER受影响对象",
        kind="path",
        source="test",
        property_map={"CHANGE_ORDER编码": "CHANGE_ORDER_ID"},
    )
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "property_filter"
    assert result.backend_result.records == [{"properties": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}]
    assert captured[0]["messages"][1]["content"]
    assert [tool["function"]["name"] for tool in captured[0]["tools"]] == [
        "get_nodes_schema",
        "get_edges_schema",
        "property_filter",
        "count_search",
        "aggregate_search",
        "sorted_search",
        "pattern_search",
        "get_element_info",
    ]
    assert backend.calls == [
        ("property_filter", "CHANGE_ORDER_affected_obj", "NODE", {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}, ["CHANGE_ORDER_ID"], 20, 0, "workspace-a")
    ]


def test_graph_query_agent_hides_schema_tools_when_schema_context_is_preloaded():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"TargetItem","element_type":"NODE",'
                                '"filter_dict":{"TargetItemCode":"A001"},"return_properties":["TargetItemCode"]}]}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="TargetItem",
        query="查询 TargetItemCode 为 A001 的物料",
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a"),
        mapping=GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem"),
        provider_config={},
        schema_context={
            "source": "provider_preload",
            "node_labels": ["TargetItem"],
            "nodes_schema": {
                "labels": ["TargetItem"],
                "schema": {"TargetItem": {"TargetItemCode": "STRING"}},
            },
            "disable_schema_tools": True,
        },
    )
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _Backend()

    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    tool_names = [tool["function"]["name"] for tool in captured[0]["tools"]]
    assert "get_nodes_schema" not in tool_names
    assert "get_edges_schema" not in tool_names
    system_prompt = captured[0]["messages"][0]["content"]
    agent_input = captured[0]["messages"][1]["content"]
    assert "预取图 schema 上下文" in system_prompt
    assert "TargetItemCode" in system_prompt
    assert '"in_system_prompt": true' in agent_input
    assert result.tool_name == "property_filter"
    assert backend.calls == [
        ("property_filter", "TargetItem", "NODE", {"TargetItemCode": "A001"}, ["TargetItemCode"], 100, 0, "workspace-a")
    ]


def test_graph_query_agent_retries_pattern_search_for_path_intent_after_record_lookup():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"SourceItem_TargetItem","element_type":"NODE",'
                                '"filter_dict":{"SourceItemCode":"101000-001"},"return_properties":["SourceItemCode","TargetItemCode"]}]}'
                            ),
                        },
                    }
                ],
            }
        if len(captured) == 2:
            return {"content": '{"done":true}', "tool_calls": []}
        assert "尚未调用 pattern_search" in captured[-1]["messages"][-1]["content"]
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {
                        "name": "pattern_search",
                        "arguments": (
                            '{"path_pattern":[[{"label":"SourceItemVersion","props":{"SourceItemCode":"101000-001"}}],'
                            '["D2O_RELATION",{"props":{"relation_type":"PROCESS_TO"}}],[{"label":"TargetItem"}]],'
                            '"return_vars":["a","r1","b"],'
                            '"return_properties":{"a":["SourceItemCode"],"b":["TargetItemCode"]},"limit":20}'
                        ),
                    },
                }
            ],
        }

    context = GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="SourceItem与TargetItem工艺关系",
        query="查询 SourceItem 101000-001 对应的 TargetItem",
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a"),
        mapping=GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="node", source="test", label="SourceItem_TargetItem"),
        provider_config={},
        schema_context={
            "source": "provider_preload",
            "node_labels": ["SourceItemVersion", "SourceItem_TargetItem", "TargetItem"],
            "relationship_types": ["D2O_RELATION"],
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
    mapping = GraphEntityMapping(
        entity="SourceItem与TargetItem工艺关系",
        kind="node",
        source="test",
        label="SourceItem_TargetItem",
        property_map={"SourceItem编码": "SourceItemCode", "TargetItem编码": "TargetItemCode"},
    )
    backend = _Backend()

    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert [record["tool"] for record in result.backend_result.records] == ["property_filter", "pattern_search"]
    assert any(step["tool"] == "GraphQueryAgent" and "追加一次路径检索修正" in step["summary"] for step in result.executed_steps)
    assert len(captured) == 3


def test_graph_query_agent_does_not_retry_pattern_when_property_filter_covers_requested_fields():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"CHANGE_ORDER_affected_obj","element_type":"NODE",'
                                '"filter_dict":{"CHANGE_ORDER_ID":"CHANGE_ORDER-001"},'
                                '"return_properties":["AffectedObjectCode","SourceItemVersionIDBefore",'
                                '"SourceItemVersionIDAfter","MaterialType"]}]}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo-ontology",
        workspace_root="/missing",
        entity="CHANGE_ORDER受影响对象",
        query=(
            "查询CHANGE_ORDER编码为'CHANGE_ORDER-001'的所有受影响对象记录，包括受影响编码、"
            "变更前SourceItem版本编号、变更后SourceItem版本编号、物料变更类型"
        ),
        timeout_seconds=60,
        max_result_rows=100,
        scope=GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a"),
        mapping=GraphEntityMapping(entity="CHANGE_ORDER受影响对象", kind="node", source="test", label="CHANGE_ORDER_affected_obj"),
        provider_config={},
        schema_context={
            "source": "provider_preload",
            "node_labels": ["CHANGE_ORDER_affected_obj", "CHANGE_ORDER", "SourceItemVersion"],
            "relationship_types": ["D2O_RELATION"],
            "nodes_schema": {
                "labels": ["CHANGE_ORDER_affected_obj", "CHANGE_ORDER", "SourceItemVersion"],
                "schema": {
                    "CHANGE_ORDER_affected_obj": {
                        "AffectedObjectCode": "STRING",
                        "SourceItemVersionIDBefore": "STRING",
                        "SourceItemVersionIDAfter": "STRING",
                        "MaterialType": "STRING",
                    },
                    "CHANGE_ORDER": {"CHANGE_ORDER_ID": "STRING"},
                    "SourceItemVersion": {"SourceItemVersionID": "STRING"},
                },
            },
            "edges_schema": {
                "relationship_types": ["D2O_RELATION"],
                "schema": {"D2O_RELATION": {"relation_type": "STRING"}},
                "relation_triples": [
                    {
                        "source_label": "CHANGE_ORDER",
                        "relation_type": "CHANGE_ORDER_AFFECTS_BEFORE",
                        "type": "D2O_RELATION",
                        "target_label": "SourceItemVersion",
                    }
                ],
            },
            "query_analysis": {"path_intent": True, "preferred_tool": "pattern_search"},
            "disable_schema_tools": True,
        },
    )
    mapping = GraphEntityMapping(
        entity="CHANGE_ORDER受影响对象",
        kind="node",
        source="test",
        label="CHANGE_ORDER_affected_obj",
        property_map={
            "受影响编码": "AffectedObjectCode",
            "变更前SourceItem版本编号": "SourceItemVersionIDBefore",
            "变更后SourceItem版本编号": "SourceItemVersionIDAfter",
            "物料变更类型": "MaterialType",
            "CHANGE_ORDER编码": "CHANGE_ORDER_ID",
        },
    )
    backend = _FieldEchoBackend()

    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "property_filter"
    assert len(captured) == 1
    assert not any("追加一次路径检索修正" in step["summary"] for step in result.executed_steps)
    assert result.warnings == []
    assert backend.calls == [
        (
            "property_filter",
            "CHANGE_ORDER_affected_obj",
            "NODE",
            {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"},
            ["AffectedObjectCode", "SourceItemVersionIDBefore", "SourceItemVersionIDAfter", "MaterialType"],
            100,
            0,
            "workspace-a",
        )
    ]


def test_graph_query_agent_preserves_multiple_data_tool_results():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "aggregate_search",
                            "arguments": (
                                '{"element_class":"InventoryDetail","element_type":"NODE",'
                                '"target_property":"InventoryNum","agg":"SUM",'
                                '"filter_dict":{"TargetItemCode":"A001","InventoryStatus":"在库"}}'
                            ),
                        },
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "aggregate_search",
                            "arguments": (
                                '{"element_class":"InventoryDetail","element_type":"NODE",'
                                '"target_property":"InventoryNum","agg":"SUM",'
                                '"filter_dict":{"TargetItemCode":"A001","InventoryStatus":"在制"}}'
                            ),
                        },
                    },
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(entity="物料库存明细", kind="node", source="test", label="InventoryDetail")
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert result.backend_result.total == 2
    assert [record["tool"] for record in result.backend_result.records] == [
        "aggregate_search",
        "aggregate_search",
    ]
    assert result.backend_result.records[0]["result"]["value"] == 1
    assert result.backend_result.records[1]["result"]["value"] == 1


def test_graph_query_agent_early_stops_only_after_primary_path_result():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"TargetItem","element_type":"NODE",'
                                '"filter_dict":{"TargetItemCode":"A001"},'
                                '"return_properties":["TargetItemCode"]}]}'
                            ),
                        },
                    }
                ],
            }
        if len(captured) == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "pattern_search",
                            "arguments": (
                                '{"path_pattern":[["SourceItemVersion"],["D2O_RELATION","->"],["TargetItem"]],'
                                '"return_vars":["a","b"],'
                                '"return_properties":{"a":["SourceItemCode"],"b":["TargetItemCode"]},"limit":20}'
                            ),
                        },
                    }
                ],
            }
        raise AssertionError("GraphQueryAgent should stop after the primary path result")

    context = _context()
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="path", source="test")
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "multi_tool_search"
    assert [record["tool"] for record in result.backend_result.records] == ["property_filter", "pattern_search"]
    assert len(captured) == 2


def test_graph_query_agent_compacts_duplicate_payloads_in_multi_tool_results():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"element_class":"SourceItemVersion","element_type":"NODE",'
                                '"filter_dict":{"SourceItemCode":"101000-001"},'
                                '"return_properties":["SourceItemCode"],"limit":20}'
                            ),
                        },
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "pattern_search",
                            "arguments": (
                                '{"path_pattern":[[{"label":"SourceItemVersion","filters":{"SourceItemCode":"101000-001"}}],'
                                '["PROCESS_TO","->"],[{"label":"TargetItem"}]],'
                                '"return_vars":["a","r1","b"],"limit":20}'
                            ),
                        },
                    },
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="path", source="test")
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    property_record, pattern_record = result.backend_result.records
    assert property_record["tool"] == "property_filter"
    assert "items" not in property_record["result"]
    assert property_record["records"] == [{"properties": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}]
    assert pattern_record["tool"] == "pattern_search"
    assert "paths" not in pattern_record["result"]
    assert pattern_record["result"] == {"count": 1, "return_vars": ["a", "r1", "b"], "truncated": False}
    assert pattern_record["records"][0]["nodes"][1]["properties"]["TargetItemCode"] == "A001"


def test_graph_query_agent_recovers_from_wildcard_return_properties():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "pattern_search",
                            "arguments": (
                                '{"path_pattern":[["SourceItemVersion"],["D2O_RELATION","->"],["TargetItem"]],'
                                '"return_vars":["a","b"],"return_properties":["*"],"limit":20}'
                            ),
                        },
                    }
                ],
            }
        if len(captured) == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "pattern_search",
                            "arguments": (
                                '{"path_pattern":[["SourceItemVersion"],["D2O_RELATION","->"],["TargetItem"]],'
                                '"return_vars":["a","b"],'
                                '"return_properties":{"a":["SourceItemCode"],"b":["TargetItemCode"]},"limit":20}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="path", source="test")
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "pattern_search"
    assert backend.calls == [
        (
            "pattern_search",
            [["SourceItemVersion"], ["D2O_RELATION", "->"], ["TargetItem"]],
            ["a", "b"],
            {"a": ["SourceItemCode"], "b": ["TargetItemCode"]},
            20,
            "workspace-a",
        )
    ]
    assert "GRAPH_TOOL_INPUT_ERROR" in str(captured[1]["messages"])
    assert "不能使用 *" in str(captured[1]["messages"])


def test_graph_query_agent_recovers_from_backend_input_error():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"TargetItem","element_type":"NODE",'
                                '"filter_dict":{"TargetItemDesc":{"$contains":"A001"}},'
                                '"return_properties":["TargetItemCode"]}]}'
                            ),
                        },
                    }
                ],
            }
        if len(captured) == 2:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "property_filter",
                            "arguments": (
                                '{"queries":[{"element_class":"TargetItem","element_type":"NODE",'
                                '"filter_dict":{"TargetItemCode":"A001"},'
                                '"return_properties":["TargetItemCode"]}]}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":true}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")
    backend = _RecoveringBackend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "property_filter"
    assert result.backend_result.records == [{"properties": {"TargetItemCode": "A001"}}]
    assert "GRAPH_TOOL_INPUT_ERROR" in str(captured[1]["messages"])
    assert "$contains" in str(captured[1]["messages"])
    assert len(backend.calls) == 2


def test_graph_query_agent_expands_or_filter_without_tool_input_error():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "property_filter",
                        "arguments": (
                            '{"queries":[{"element_class":"SourceItem","element_type":"NODE",'
                            '"filter_dict":{"$or":[{"SourceItemCode":{"contains":"A001"}},'
                            '{"SourceItemDesc":{"contains":"A001"}}]},'
                            '"return_properties":["SourceItemID","SourceItemCode","SourceItemDesc"]}]}'
                        ),
                    },
                }
            ],
        }

    context = _context()
    mapping = GraphEntityMapping(entity="SourceItem", kind="node", source="test", label="SourceItem")
    backend = _Backend()
    result = GraphQueryAgent(llm_call=fake_llm_call).run(
        context=context,
        mapping=mapping,
        tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
    )

    assert result.tool_name == "property_filter"
    assert [call[3] for call in backend.calls] == [
        {"SourceItemCode": {"contains": "A001"}},
        {"SourceItemDesc": {"contains": "A001"}},
    ]
    assert not any("内部图工具参数错误" in step["summary"] for step in result.executed_steps)
    assert len(captured) == 1


def test_graph_query_agent_surfaces_unrecovered_tool_input_error():
    captured = []

    def fake_llm_call(payload):
        captured.append(payload)
        if len(captured) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "pattern_search",
                            "arguments": (
                                '{"path_pattern":[["SourceItemVersion"],["D2O_RELATION","->"],["TargetItem"]],'
                                '"return_vars":["a","b"],"return_properties":["*"]}'
                            ),
                        },
                    }
                ],
            }
        return {"content": '{"done":false}', "tool_calls": []}

    context = _context()
    mapping = GraphEntityMapping(entity="SourceItem与TargetItem工艺关系", kind="path", source="test")
    backend = _Backend()

    with pytest.raises(GraphAgentToolInputError) as exc_info:
        GraphQueryAgent(llm_call=fake_llm_call).run(
            context=context,
            mapping=mapping,
            tool_executor=GraphToolExecutor(backend=backend, context=context, mapping=mapping),
        )

    assert exc_info.value.error_code == "GRAPH_TOOL_INPUT_ERROR"
    assert "不能使用 *" in str(exc_info.value)
    assert backend.calls == []


def test_graph_query_agent_timeout_raises_structured_error():
    def slow_llm_call(payload):
        del payload
        time.sleep(0.05)
        return "{}"

    context = _context()
    context.provider_config["graph_agent_timeout_seconds"] = 0.001
    mapping = GraphEntityMapping(
        entity="CHANGE_ORDER受影响对象",
        kind="path",
        source="test",
        property_map={"CHANGE_ORDER编码": "CHANGE_ORDER_ID"},
    )

    with pytest.raises(GraphAgentTimeoutError) as exc_info:
        GraphQueryAgent(llm_call=slow_llm_call).run(
            context=context,
            mapping=mapping,
            tool_executor=GraphToolExecutor(backend=_Backend(), context=context, mapping=mapping),
        )

    assert exc_info.value.error_code == "GRAPH_AGENT_TIMEOUT"


def test_graph_query_agent_surfaces_safe_llm_exception_detail():
    def failing_llm_call(payload):
        del payload
        raise ValueError("bad json from model token=secret-token password=plain-text")

    context = _context()
    mapping = GraphEntityMapping(entity="TargetItem", kind="node", source="test", label="TargetItem")

    with pytest.raises(GraphAgentError) as exc_info:
        GraphQueryAgent(llm_call=failing_llm_call).run(
            context=context,
            mapping=mapping,
            tool_executor=GraphToolExecutor(backend=_Backend(), context=context, mapping=mapping),
        )

    message = str(exc_info.value)
    assert exc_info.value.error_code == "GRAPH_AGENT_ERROR"
    assert "GraphQueryAgent 调用失败" in message
    assert "ValueError" in message
    assert "bad json from model" in message
    assert "secret-token" not in message
    assert "plain-text" not in message
