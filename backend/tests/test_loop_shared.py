"""Shared loop helper contracts used by Jiuwen runtime code."""

from __future__ import annotations

import importlib.util

from agentic.tools.base import ToolBatchResult


def test_shared_loop_modules_are_available():
    for module_name in (
        "agentic.loop.shared",
        "agentic.loop.shared.events",
        "agentic.loop.shared.messages",
        "agentic.loop.shared.modes",
        "agentic.loop.shared.ontology_context",
        "agentic.loop.shared.prompting",
        "agentic.loop.shared.state_updates",
        "agentic.loop.shared.timing",
        "agentic.loop.shared.tool_context",
    ):
        assert importlib.util.find_spec(module_name) is not None


def test_resolve_agent_mode_normalizes_supported_modes():
    from agentic.loop.shared.modes import resolve_agent_mode

    assert resolve_agent_mode({"agent_mode": "thinking"}) == "thinking"
    assert resolve_agent_mode({"agent_mode": "MULTI_AGENT"}) == "multi_agent"
    assert resolve_agent_mode({"agent_mode": "unknown"}) == "quick"
    assert resolve_agent_mode(None) == "quick"


def test_apply_tool_batch_result_to_state_merges_runtime_fields():
    from agentic.loop.shared.state_updates import apply_tool_batch_result_to_state

    state = {
        "messages": [{"role": "user", "content": "hello"}],
        "results": {"existing": 1},
        "completed_ids": ["old"],
        "evolution_recall_records": [{"id": "existing"}],
    }
    batch_result = ToolBatchResult(
        messages=[{"role": "tool", "content": "tool output"}],
        state_updates={
            "results": {"new": 2},
            "completed_ids": ["new-id"],
            "failed_ids": ["failed-id"],
            "errors": ["err"],
            "messages": [{"role": "tool", "content": "extra output"}],
            "read_document_records": [
                {
                    "path": "ontologies/demo/objects/widget.md",
                    "normalized_path": "ontologies/demo/objects/widget.md",
                    "doc_type": "object_doc",
                    "source": "ontology_context",
                }
            ],
            "evolution_recall_records": [{"id": "next"}],
            "plain": "value",
        },
    )

    apply_tool_batch_result_to_state(state, batch_result)

    assert state["results"] == {"existing": 1, "new": 2}
    assert state["completed_ids"] == ["old", "new-id"]
    assert state["failed_ids"] == ["failed-id"]
    assert state["errors"] == ["err"]
    assert [message["content"] for message in state["messages"]] == [
        "hello",
        "tool output",
        "extra output",
    ]
    assert "ontologies/demo/objects/widget.md" in state["read_documents"]["by_path"]
    assert state["evolution_recall_records"] == [{"id": "existing"}, {"id": "next"}]
    assert state["plain"] == "value"


def test_build_tool_timing_metrics_computes_overlap_and_parallel_execution():
    from agentic.loop.shared.timing import build_tool_timing_metrics

    metrics = build_tool_timing_metrics(
        [
            {"started_at_ms": 1000, "finished_at_ms": 1300, "elapsed_ms": 300},
            {"started_at_ms": 1200, "finished_at_ms": 1600, "elapsed_ms": 400},
        ],
        tools_window_elapsed_ms=600,
        llm_started_at_ms=900,
        llm_finished_at_ms=1250,
    )

    assert metrics == {
        "tools_window_elapsed_ms": 600,
        "tools_wall_elapsed_ms": 600,
        "tools_execution_elapsed_ms": 700,
        "tools_overlap_llm_ms": 250,
        "tools_net_wait_ms": 350,
    }


def test_iteration_event_can_override_main_source_node():
    from agentic.loop.shared.events import emit_iteration_completed_event

    events = []
    emit_iteration_completed_event(
        {"run_id": "run-shared-event", "session_id": "sess-shared-event", "current_turn_id": 3},
        caller="main",
        event_writer=events.append,
        iteration=1,
        status="success",
        prompt_elapsed_ms=1,
        llm_elapsed_ms=2,
        llm_stream_elapsed_ms=2,
        llm_wall_elapsed_ms=2,
        tools_elapsed_ms=3,
        total_elapsed_ms=6,
        tool_names=["tool_a"],
        source_node="jiuwen_react",
    )

    assert events[0]["data"]["source"] == {"node": "jiuwen_react", "component": "iteration"}
