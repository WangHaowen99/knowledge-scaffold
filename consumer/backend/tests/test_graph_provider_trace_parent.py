"""Graph runtime spans must remain children of business_data_search."""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.business_data.providers.graph.models import GraphSearchRuntimeContext
from agent.tools.business_data.providers.graph.scope import build_graph_scope
from agent.tools.business_data.providers.graph.trace import (
    GraphToolTraceSink,
    build_graph_agent_runtime_trace,
)
from agent.tools.business_data_search import _build_provider_runtime_context
from integration.observability.events import RuntimeEvent, RuntimeEventType


class _CapturingObserver:
    enabled = True

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> bool:
        self.events.append(event)
        return True


def _make_context(provider_config: dict) -> GraphSearchRuntimeContext:
    scoped_config = {
        "workspace_id": "ws-1",
        "ontology_id": "onto-1",
        **provider_config,
    }
    return GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="ws-1",
        ontology_id="onto-1",
        workspace_root="",
        entity="Part",
        query="q",
        timeout_seconds=60,
        max_result_rows=100,
        scope=build_graph_scope(scoped_config),
        mapping=None,
        provider_config=scoped_config,
    )


def test_business_data_runtime_context_uses_outer_tool_span_as_parent():
    observer = _CapturingObserver()
    tool_context = SimpleNamespace(
        run_id="run-1",
        session_id="session-1",
        turn_id=7,
        caller="main",
        agent_iteration=3,
        subagent_lineage=None,
        model_profile_id="workspace:graph",
        observer=observer,
        cancel_event=None,
    )

    runtime = _build_provider_runtime_context(
        context=tool_context,
        tool_call_id="call-1",
    )

    assert runtime["parent_span_key"] == "tool:main:3:business_data_search:call-1"
    assert runtime["model_profile_id"] == "workspace:graph"
    assert runtime["_observer"] is observer


def test_graph_tool_spans_attach_to_outer_business_data_span():
    observer = _CapturingObserver()
    parent_key = "tool:main:3:business_data_search:call-1"
    sink = GraphToolTraceSink(
        _make_context({
            "run_id": "run-1",
            "session_id": "session-1",
            "turn_id": 7,
            "agent_iteration": 3,
            "parent_span_key": parent_key,
            "_observer": observer,
        })
    )

    sink({"type": "start", "content": "开始执行图工具: property_filter"})
    sink({"type": "intermediate", "content": "图工具执行完成: property_filter"})

    assert [event.type for event in observer.events] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_FINISHED,
    ]
    assert all(event.parent_key == parent_key for event in observer.events)
    assert all(event.payload["iteration"] == 3 for event in observer.events)
    assert observer.events[0].span_key == observer.events[1].span_key
    assert observer.events[0].span_key.startswith("tool:graph:property_filter:")


def test_graph_tool_failure_closes_same_child_span():
    observer = _CapturingObserver()
    sink = GraphToolTraceSink(
        _make_context({
            "run_id": "run-1",
            "parent_span_key": "tool:main:1:business_data_search:call-2",
            "_observer": observer,
        })
    )

    sink({"type": "start", "content": "开始执行图工具: count_search"})
    sink({"type": "error", "content": "图工具执行失败: count_search"})

    assert len(observer.events) == 2
    assert observer.events[1].payload["status"] == "error"
    assert observer.events[0].span_key == observer.events[1].span_key


def test_graph_model_trace_uses_outer_tool_span_as_parent():
    parent_key = "tool:main:2:business_data_search:call-3"
    observer = _CapturingObserver()
    runtime_trace = build_graph_agent_runtime_trace(
        _make_context({
            "run_id": "run-1",
            "parent_span_key": parent_key,
            "_observer": observer,
        })
    )

    assert runtime_trace.loop_span_key == parent_key
    runtime_trace.llm_started(
        iteration=1,
        call_index=1,
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        model="graph-model",
        request_label="jiuwen_react_model_call",
    )
    assert observer.events[0].type == RuntimeEventType.LLM_STARTED
    assert observer.events[0].parent_key == parent_key
    assert observer.events[0].span_key.startswith("llm:graph:")


def test_missing_trace_metadata_degrades_without_emitting():
    observer = _CapturingObserver()
    sink = GraphToolTraceSink(_make_context({}), observer=observer)

    sink({"type": "start", "content": "开始执行图工具: property_filter"})
    sink({"type": "intermediate", "content": "图工具执行完成: property_filter"})

    assert observer.events == []
