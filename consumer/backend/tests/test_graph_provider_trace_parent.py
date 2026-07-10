"""Regression tests for B2: graph provider tool spans must attach to the
parent tool_batch span (parent_key) and carry the agent iteration.

Before the fix, GraphProviderTrace emitted TOOL_STARTED/TOOL_FINISHED events
with no parent_key and no iteration, so graph tool spans appeared as orphans
and were not grouped under any iteration in local perf analysis.
"""

from __future__ import annotations

from typing import List

from agentic.observability.events import RuntimeEvent, RuntimeEventType
from agentic.business_data.providers.graph.models import GraphSearchRuntimeContext
from agentic.business_data.providers.graph.scope import build_graph_scope
from agentic.business_data.providers.graph.trace import GraphProviderTrace
from agentic.tools.business_data_search import _tool_batch_parent_span_key


class _CapturingObserver:
    """Fake observer that records every emitted RuntimeEvent."""

    enabled = True

    def __init__(self) -> None:
        self.events: List[RuntimeEvent] = []

    def emit(self, event: RuntimeEvent) -> bool:
        self.events.append(event)
        return True


def _make_context(provider_config: dict) -> GraphSearchRuntimeContext:
    return GraphSearchRuntimeContext(
        provider="neo4j",
        workspace_id="ws-1",
        ontology_id="onto-1",
        workspace_root="",
        entity="Part",
        query="q",
        timeout_seconds=60,
        max_result_rows=100,
        scope=build_graph_scope(provider_config),
        mapping=None,
        provider_config=provider_config,
    )


def test_parent_span_key_rebuild_matches_agent_trace_rules():
    # main caller
    assert _tool_batch_parent_span_key("main", None, 3) == "tools:main:3"
    # subagent with id
    assert _tool_batch_parent_span_key("subagent", {"subagent_id": "sa1"}, 2) == "tools:subagent:sa1:2"
    # subagent without id
    assert _tool_batch_parent_span_key("subagent", None, 2) == "tools:subagent:2"
    # default / empty caller
    assert _tool_batch_parent_span_key(None, None, 0) == "tools:main:0"


def test_graph_tool_spans_carry_parent_key_and_iteration():
    observer = _CapturingObserver()
    provider_config = {
        "run_id": "run-1",
        "session_id": "sess-1",
        "turn_id": 7,
        "parent_span_key": "tools:main:3",
        "agent_iteration": 3,
        "workspace_id": "ws-1",
    }
    trace = GraphProviderTrace(_make_context(provider_config), observer=observer)
    assert trace.enabled is True

    span_key, started = trace.tool_started(tool_name="property_filter", tool_args={"a": 1})
    trace.tool_finished(
        span_key=span_key,
        started=started,
        tool_name="property_filter",
        backend_result=None,
        payload={"result": {"count": 0}},
    )

    assert len(observer.events) == 2
    for event in observer.events:
        assert event.parent_key == "tools:main:3", "graph tool span must attach to parent tool_batch"
        assert event.payload.get("iteration") == 3, "graph tool span payload must carry iteration"
        assert event.span_key and event.span_key.startswith("tool:graph:"), "span_key format preserved"

    assert observer.events[0].type == RuntimeEventType.TOOL_STARTED
    assert observer.events[1].type == RuntimeEventType.TOOL_FINISHED


def test_graph_tool_failed_span_also_carries_parent_key():
    observer = _CapturingObserver()
    provider_config = {
        "run_id": "run-1",
        "parent_span_key": "tools:subagent:sa1:2",
        "agent_iteration": 2,
        "workspace_id": "ws-1",
    }
    trace = GraphProviderTrace(_make_context(provider_config), observer=observer)

    span_key, started = trace.tool_started(tool_name="count_search", tool_args={})
    trace.tool_failed(
        span_key=span_key,
        started=started,
        tool_name="count_search",
        error=ValueError("boom"),
    )

    assert len(observer.events) == 2
    for event in observer.events:
        assert event.parent_key == "tools:subagent:sa1:2"
        assert event.payload.get("iteration") == 2


def test_missing_parent_config_degrades_gracefully():
    """Without parent_span_key/iteration, spans still emit (no crash), just no linkage."""
    observer = _CapturingObserver()
    provider_config = {"run_id": "run-1", "workspace_id": "ws-1"}
    trace = GraphProviderTrace(_make_context(provider_config), observer=observer)

    span_key, started = trace.tool_started(tool_name="property_filter", tool_args={})
    assert len(observer.events) == 1
    assert observer.events[0].parent_key is None
    assert "iteration" not in observer.events[0].payload
