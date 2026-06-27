"""Tests for internal graph tool adapters and executor."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentic.business_data.providers.graph.models import (
    GraphBackendResult,
    GraphEntityMapping,
    GraphScope,
    GraphSearchRuntimeContext,
)
from agentic.business_data.providers.graph.tools import (
    GraphToolExecutor,
    GraphToolInputError,
    aggregate_search,
    count_search,
    get_element_info,
    get_edges_schema,
    get_nodes_schema,
    pattern_search,
    property_filter,
    sorted_search,
)
from agentic.business_data.providers.graph.trace import GraphProviderTrace


class _ToolBackend:
    def __init__(self):
        self.calls = []

    def get_nodes_schema(self, *, scope, element_types=None):
        self.calls.append(("get_nodes_schema", scope.workspace_value, element_types))
        return {"labels": ["TargetItem"]}

    def get_edges_schema(self, *, scope, relation_types=None):
        self.calls.append(("get_edges_schema", scope.workspace_value, relation_types))
        return {"relationship_types": ["D2O_RELATION"]}

    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        return GraphBackendResult(records=[{"ok": True}], total=1)

    def count_search(self, *, element_class, element_type, filter_dict, scope):
        self.calls.append(("count_search", element_class, element_type, filter_dict, scope.workspace_value))
        return GraphBackendResult(total=1)

    def aggregate_search(self, *, element_class, element_type, target_property, agg, filter_dict, scope):
        self.calls.append(("aggregate_search", element_class, element_type, target_property, agg, filter_dict, scope.workspace_value))
        return GraphBackendResult(records=[{"value": 1}], total=1)

    def sorted_search(self, *, element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope):
        self.calls.append(("sorted_search", element_class, element_type, filter_dict, return_properties, sort_by, ascending, limit, offset, scope.workspace_value))
        return GraphBackendResult(records=[{"ok": True}], total=1)

    def pattern_search(self, *, path_pattern, return_vars, limit, scope, return_properties=None):
        self.calls.append(("pattern_search", path_pattern, return_vars, return_properties, limit, scope.workspace_value))
        return GraphBackendResult(records=[])

    def get_element_info(self, *, element_instance_id, element_class="", element_type="", scope=None):
        self.calls.append(("get_element_info", element_instance_id, element_class, element_type, scope.workspace_value))
        return {"found": True, "element_id": element_instance_id}

    def close(self):
        return None


class _BackendInputErrorBackend(_ToolBackend):
    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        raise ValueError("filter_dict.SourceItemDesc 包含不支持的操作符: '$contains'")


class _FallbackSampleBackend(_ToolBackend):
    def property_filter(self, *, element_class, element_type, filter_dict, return_properties, limit, offset, scope):
        self.calls.append(("property_filter", element_class, element_type, filter_dict, return_properties, limit, offset, scope.workspace_value))
        if filter_dict:
            return GraphBackendResult(records=[], total=0)
        records = [{"properties": {"TargetItemCode": "S{:03d}".format(index)}} for index in range(1, 11)]
        return GraphBackendResult(records=records[:limit], total=len(records))


def _context(scope):
    return GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="workspace-a",
        ontology_id="demo",
        workspace_root="/missing",
        entity="target_item",
        query="查询零件",
        timeout_seconds=60,
        max_result_rows=100,
        scope=scope,
        mapping=GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem"),
        provider_config={},
    )


class _Observer:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)
        return True


def test_internal_graph_tool_adapters_delegate_to_backend():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")

    assert get_nodes_schema(backend, scope=scope) == {"labels": ["TargetItem"]}
    assert get_edges_schema(backend, scope=scope) == {"relationship_types": ["D2O_RELATION"]}
    assert property_filter(
        backend=backend,
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={},
        return_properties=None,
        limit=100,
        offset=0,
        scope=scope,
    ).records
    assert count_search(
        backend=backend,
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={},
        scope=scope,
    ).total == 1
    assert aggregate_search(
        backend=backend,
        element_class="TargetItem",
        element_type="NODE",
        target_property="Weight",
        agg="SUM",
        filter_dict={},
        scope=scope,
    ).records == [{"value": 1}]
    assert sorted_search(
        backend=backend,
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={},
        return_properties=["TargetItemCode"],
        sort_by=["TargetItemCode"],
        ascending=True,
        limit=10,
        offset=0,
        scope=scope,
    ).records == [{"ok": True}]
    assert pattern_search(
        backend=backend,
        path_pattern=[["TargetItem"], ["D2O_RELATION", "->"], ["SourceItem"]],
        return_vars=None,
        return_properties={"a": ["TargetItemCode"]},
        limit=100,
        scope=scope,
    ).records == []
    assert get_element_info(backend=backend, element_instance_id="node-1", scope=scope) == {
        "found": True,
        "element_id": "node-1",
    }

    assert backend.calls == [
        ("get_nodes_schema", "workspace-a", None),
        ("get_edges_schema", "workspace-a", None),
        ("property_filter", "TargetItem", "NODE", {}, None, 100, 0, "workspace-a"),
        ("count_search", "TargetItem", "NODE", {}, "workspace-a"),
        ("aggregate_search", "TargetItem", "NODE", "Weight", "SUM", {}, "workspace-a"),
        ("sorted_search", "TargetItem", "NODE", {}, ["TargetItemCode"], ["TargetItemCode"], True, 10, 0, "workspace-a"),
        ("pattern_search", [["TargetItem"], ["D2O_RELATION", "->"], ["SourceItem"]], None, {"a": ["TargetItemCode"]}, 100, "workspace-a"),
        ("get_element_info", "node-1", "", "", "workspace-a"),
    ]


def test_graph_tool_executor_runs_jiutian_style_property_query_and_blocks_scope_fields():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    execution = executor.execute(
        "property_filter",
        {
            "queries": [
                {
                    "element_class": "TargetItem",
                    "element_type": "NODE",
                    "filter_dict": {"TargetItemCode": "P-001"},
                    "return_properties": ["TargetItemCode"],
                    "limit": 10,
                }
            ]
        },
    )

    assert execution.backend_result.records == [{"ok": True}]
    assert execution.payload["success"] is True
    assert backend.calls[-1] == (
        "property_filter",
        "TargetItem",
        "NODE",
        {"TargetItemCode": "P-001"},
        ["TargetItemCode"],
        10,
        0,
        "workspace-a",
    )

    with pytest.raises(GraphToolInputError, match="scope/internal"):
        executor.execute(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "TargetItem",
                        "element_type": "NODE",
                        "filter_dict": {"d2o_workspace_id": "workspace-b"},
                    }
                ]
            },
        )


def test_graph_tool_executor_passes_pattern_return_properties():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="target_item", kind="path", source="test")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    execution = executor.execute(
        "pattern_search",
        {
            "path_pattern": [["TargetItem"], ["D2O_RELATION", "->"], ["SourceItem"]],
            "return_vars": ["b"],
            "return_properties": {"b": ["SourceItemCode"]},
            "limit": 5,
        },
    )

    assert execution.payload["result"]["return_properties"] == {"b": ["SourceItemCode"]}
    assert backend.calls[-1] == (
        "pattern_search",
        [["TargetItem"], ["D2O_RELATION", "->"], ["SourceItem"]],
        ["b"],
        {"b": ["SourceItemCode"]},
        5,
        "workspace-a",
    )


def test_graph_tool_executor_validates_pattern_search_against_preloaded_schema():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem")
    context = replace(
        _context(scope),
        schema_context={
            "nodes_schema": {
                "labels": ["SourceItemVersion", "TargetItem"],
                "schema": {
                    "SourceItemVersion": {"SourceItemCode": "STRING"},
                    "TargetItem": {"TargetItemCode": "STRING"},
                },
            },
            "edges_schema": {
                "relationship_types": ["D2O_RELATION"],
                "schema": {"D2O_RELATION": {"relation_type": "STRING"}},
            },
        },
    )
    executor = GraphToolExecutor(backend=backend, context=context, mapping=mapping)

    execution = executor.execute(
        "pattern_search",
        {
            "path_pattern": [
                [{"label": "SourceItemVersion", "props": {"SourceItemCode": "101000-001"}}],
                ["D2O_RELATION", {"props": {"relation_type": "PROCESS_TO"}}],
                [{"label": "TargetItem"}],
            ],
            "return_vars": ["a", "r1", "b"],
            "return_properties": {"a": ["SourceItemCode"], "b": ["TargetItemCode"]},
        },
    )

    assert execution.tool_name == "pattern_search"
    with pytest.raises(GraphToolInputError, match="节点 label 不存在"):
        executor.execute(
            "pattern_search",
            {
                "path_pattern": [
                    [{"label": "MissingLabel"}],
                    ["D2O_RELATION", "->"],
                    [{"label": "TargetItem"}],
                ]
            },
        )
    with pytest.raises(GraphToolInputError, match="return_properties 不存在"):
        executor.execute(
            "pattern_search",
            {
                "path_pattern": [
                    [{"var": "a", "label": "SourceItemVersion"}],
                    ["D2O_RELATION", "->"],
                    [{"var": "b", "label": "TargetItem"}],
                ],
                "return_properties": {"a": ["MissingField"]},
            },
        )


def test_graph_tool_executor_rejects_wildcard_return_properties_before_backend():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="target_item", kind="path", source="test")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    with pytest.raises(GraphToolInputError, match=r"不能使用 \*"):
        executor.execute(
            "pattern_search",
            {
                "path_pattern": [["TargetItem"], ["D2O_RELATION", "->"], ["SourceItem"]],
                "return_vars": ["a", "b"],
                "return_properties": ["*"],
            },
        )

    assert backend.calls == []


def test_graph_tool_executor_wraps_backend_input_errors():
    backend = _BackendInputErrorBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    with pytest.raises(GraphToolInputError, match=r"\$contains"):
        executor.execute(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "TargetItem",
                        "element_type": "NODE",
                        "filter_dict": {"TargetItemDesc": {"$contains": "A001"}},
                    }
                ]
            },
        )

    assert backend.calls[-1][0] == "property_filter"


def test_graph_tool_executor_expands_simple_or_filter_into_batched_queries():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="source_item", kind="node", source="test", label="SourceItem")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    execution = executor.execute(
        "property_filter",
        {
            "queries": [
                {
                    "query_id": "code_or_desc",
                    "element_class": "SourceItem",
                    "element_type": "NODE",
                    "filter_dict": {
                        "$or": [
                            {"SourceItemCode": {"contains": "A001"}},
                            {"SourceItemDesc": {"contains": "A001"}},
                        ]
                    },
                    "return_properties": ["SourceItemID", "SourceItemCode", "SourceItemDesc"],
                    "limit": 20,
                }
            ]
        },
    )

    assert execution.backend_result.total == 2
    assert [call[3] for call in backend.calls] == [
        {"SourceItemCode": {"contains": "A001"}},
        {"SourceItemDesc": {"contains": "A001"}},
    ]
    result = execution.payload["result"]
    assert result["results"][0]["query_id"] == "code_or_desc_or1"
    assert result["results"][1]["query_id"] == "code_or_desc_or2"


def test_graph_tool_executor_rejects_nested_logical_filter():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="source_item", kind="node", source="test", label="SourceItem")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    with pytest.raises(GraphToolInputError, match="嵌套逻辑操作符"):
        executor.execute(
            "property_filter",
            {
                "queries": [
                    {
                        "element_class": "SourceItem",
                        "element_type": "NODE",
                        "filter_dict": {
                            "$or": [
                                {"$and": [{"SourceItemCode": "A001"}]},
                            ]
                        },
                    }
                ]
            },
        )

    assert backend.calls == []


def test_graph_tool_executor_ignores_empty_filter_fallback_samples_after_constrained_query():
    backend = _FallbackSampleBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="flight-material", kind="node", source="test", label="EquipmentMaterialStatus")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    execution = executor.execute(
        "property_filter",
        {
            "queries": [
                {
                    "query_id": "exact",
                    "element_class": "EquipmentMaterialStatus",
                    "element_type": "NODE",
                    "filter_dict": {"TargetItemCode": "B001"},
                    "return_properties": ["BatchID", "TargetItemCode"],
                    "limit": 20,
                },
                {
                    "query_id": "sample",
                    "element_class": "EquipmentMaterialStatus",
                    "element_type": "NODE",
                    "filter_dict": {},
                    "return_properties": ["BatchID", "TargetItemCode"],
                    "limit": 10,
                },
            ]
        },
    )

    assert execution.backend_result.records == []
    assert execution.backend_result.total == 0
    assert any("忽略空过滤兜底样本查询 sample" in warning for warning in execution.backend_result.warnings)
    results = execution.payload["result"]["results"]
    assert results[0]["query_id"] == "exact"
    assert results[0]["count"] == 0
    assert results[1]["query_id"] == "sample"
    assert results[1]["diagnostic"] is True
    assert results[1]["items"] == []
    assert results[1]["count"] == 0
    assert results[1]["diagnostic_sample_count"] == 10
    assert results[1]["ignored_count"] == 10


def test_graph_tool_executor_counts_single_empty_filter_query_as_intentional_broad_query():
    backend = _FallbackSampleBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    mapping = GraphEntityMapping(entity="flight-material", kind="node", source="test", label="EquipmentMaterialStatus")
    executor = GraphToolExecutor(backend=backend, context=_context(scope), mapping=mapping)

    execution = executor.execute(
        "property_filter",
        {
            "queries": [
                {
                    "query_id": "all",
                    "element_class": "EquipmentMaterialStatus",
                    "element_type": "NODE",
                    "filter_dict": {},
                    "return_properties": ["BatchID", "TargetItemCode"],
                    "limit": 10,
                }
            ]
        },
    )

    assert len(execution.backend_result.records) == 10
    assert execution.backend_result.total == 10
    assert execution.backend_result.warnings == []
    assert execution.payload["result"]["query_id"] == "all"
    assert execution.payload["result"]["count"] == 10
    assert "diagnostic" not in execution.payload["result"]


def test_graph_tool_executor_emits_internal_tool_trace_events():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    context = _context(scope)
    context.provider_config.update({
        "run_id": "run-graph",
        "session_id": "session-graph",
        "turn_id": 2,
    })
    observer = _Observer()
    trace = GraphProviderTrace(context, observer=observer)
    mapping = GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem")
    executor = GraphToolExecutor(backend=backend, context=context, mapping=mapping, trace=trace)

    execution = executor.execute(
        "count_search",
        {
            "queries": [
                {
                    "element_class": "TargetItem",
                    "element_type": "NODE",
                    "filter_dict": {"TargetItemCode": "P-001"},
                }
            ]
        },
    )

    assert execution.backend_result.total == 1
    assert [event.type for event in observer.events] == ["tool.started", "tool.finished"]
    assert observer.events[0].run_id == "run-graph"
    assert observer.events[0].session_id == "session-graph"
    assert observer.events[0].turn_id == 2
    assert observer.events[0].payload["tool_name"] == "count_search"
    assert observer.events[0].payload["display_name"] == "graph:count_search"
    assert "tool_args_digest" in observer.events[0].payload["input"]
    assert observer.events[1].payload["status"] == "success"
    assert observer.events[1].payload["result_count"] == 1


def test_graph_tool_executor_emits_error_trace_event():
    backend = _ToolBackend()
    scope = GraphScope(workspace_property="d2o_workspace_id", workspace_value="workspace-a")
    context = _context(scope)
    context.provider_config["run_id"] = "run-graph"
    observer = _Observer()
    trace = GraphProviderTrace(context, observer=observer)
    mapping = GraphEntityMapping(entity="target_item", kind="node", source="test", label="TargetItem")
    executor = GraphToolExecutor(backend=backend, context=context, mapping=mapping, trace=trace)

    with pytest.raises(GraphToolInputError):
        executor.execute("missing_tool", {})

    assert [event.type for event in observer.events] == ["tool.started", "tool.finished"]
    assert observer.events[1].payload["status"] == "error"
    assert observer.events[1].payload["error_type"] == "GraphToolInputError"
