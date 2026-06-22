"""Tests for the agent-stream.v2 protocol and registry event emission paths."""

from agentic.config import AgentConfig
from agentic.tools.base import ToolInvocation
from agentic.tools.registry import ToolContext, execute_tool_batch
from agentic.utils.streaming import build_agent_event, clear_run_sequence
from agentic.utils.streaming import stream_writer_context


def _build_tool_call(name: str, arguments: dict, call_id: str = "call_1") -> ToolInvocation:
    return ToolInvocation(
        name=name,
        args=arguments,
        tool_call_id=call_id,
    )


def test_build_agent_event_uses_monotonic_seq_per_run():
    run_id = "run-seq-test"
    clear_run_sequence(run_id)

    first = build_agent_event(
        run_id=run_id,
        session_id="s1",
        turn_id=1,
        event_type="turn.started",
        source={"node": "test", "component": "unit"},
        payload={"user_preview": "hello"},
    )
    second = build_agent_event(
        run_id=run_id,
        session_id="s1",
        turn_id=1,
        event_type="turn.finished",
        source={"node": "test", "component": "unit"},
        payload={"status": "success"},
    )

    assert first["event"] == "agent.v2"
    assert second["event"] == "agent.v2"
    assert first["data"]["protocol"] == "agent-stream.v2"
    assert second["data"]["protocol"] == "agent-stream.v2"
    assert first["data"]["seq"] == 1
    assert second["data"]["seq"] == 2
    assert first["data"]["event_id"] == "{}:1".format(run_id)
    assert second["data"]["event_id"] == "{}:2".format(run_id)


def test_execute_tool_batch_emits_v2_regular_tool_events():
    captured = []

    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-tool-v2",
        session_id="sess-v2",
        turn_id=3,
        ov_session_id=None,
        agent_config=AgentConfig.from_env(),
        caller="main",
    )

    with stream_writer_context(captured.append):
        result = execute_tool_batch(
            [_build_tool_call("business_data_search", {})],
            context,
        )

    assert result.messages[0]["name"] == "business_data_search"

    assert captured
    assert all(event.get("event") == "agent.v2" for event in captured)
    assert all(event.get("data", {}).get("protocol") == "agent-stream.v2" for event in captured)

    event_types = [event["data"]["type"] for event in captured]
    assert "tool.started" in event_types
    assert "tool.finished" in event_types

    seqs = [event["data"]["seq"] for event in captured]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
