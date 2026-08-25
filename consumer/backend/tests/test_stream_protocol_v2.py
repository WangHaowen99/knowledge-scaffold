"""Tests for the agent-stream.v2 protocol and registry event emission paths."""

import json

from config import AgentConfig
from agent.tools.base import ToolInvocation
from agent.tools.events import (
    build_tool_finished_payload,
    emit_tool_finished,
    emit_tool_started,
    tool_output_artifact_writer_context,
)
from agent.tools.registry import ToolContext
from tool_execution_test_adapter import execute_tool_batch
from utils.streaming import build_agent_event, clear_run_sequence
from utils.streaming import stream_writer_context


def _build_tool_call(
    name: str,
    arguments: dict,
    call_id: str = "call_1",
) -> ToolInvocation:
    return ToolInvocation(id=call_id, name=name, arguments=arguments)


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
    assert len(first["data"]["trace_id"]) == 32
    assert len(first["data"]["span_id"]) == 16
    assert first["data"]["trace_id"] == second["data"]["trace_id"]
    assert first["data"]["span_id"] == second["data"]["span_id"]
    assert "span_key" not in first["data"]
    assert "parent_key" not in first["data"]


def test_build_agent_event_preserves_optional_trace_tree_keys():
    event = build_agent_event(
        run_id="run-trace-tree",
        session_id="s-trace-tree",
        turn_id=2,
        event_type="iteration.completed",
        source={"node": "jiuwen_react", "component": "iteration"},
        payload={"status": "success"},
        span_key="iteration:main:1",
        parent_key="agent_loop:main",
    )

    assert event["data"]["span_key"] == "iteration:main:1"
    assert event["data"]["parent_key"] == "agent_loop:main"


def test_tool_finished_keeps_sse_preview_bounded_and_exposes_artifact_reference():
    complete_output = "工具输出🙂" * 1200
    persisted = []

    def write_artifact(tool_call_id, output):
        persisted.append((tool_call_id, output))
        return {
            "output_ref": "a" * 32,
            "output_size": len(output.encode("utf-8")),
        }

    with tool_output_artifact_writer_context(write_artifact):
        payload = build_tool_finished_payload(
            tool_name="execute_script",
            tool_call_id="call-long",
            status="completed",
            raw_output=complete_output,
            error=None,
            elapsed_ms=17,
        )

    assert persisted == [("call-long", complete_output)]
    assert payload["output_preview"] == complete_output[:4096]
    assert len(payload["output_preview"]) == 4096
    assert payload["output_truncated"] is True
    assert payload["output_ref"] == "a" * 32
    assert payload["output_size"] == len(complete_output.encode("utf-8"))
    assert "output" not in payload


def test_tool_finished_does_not_persist_output_that_fits_in_preview():
    persisted = []

    def write_artifact(tool_call_id, output):
        persisted.append((tool_call_id, output))
        return {"output_ref": "a" * 32, "output_size": len(output.encode("utf-8"))}

    with tool_output_artifact_writer_context(write_artifact):
        payload = build_tool_finished_payload(
            tool_name="execute_script",
            tool_call_id="call-short",
            status="completed",
            raw_output="short output",
            error=None,
            elapsed_ms=3,
        )

    assert persisted == []
    assert payload["output_truncated"] is False
    assert "output_ref" not in payload


def test_tool_finished_redacts_sensitive_preview_projection_error_and_artifact():
    persisted = []
    raw_output = json.dumps({
        "success": True,
        "provider": "neo4j",
        "executed_steps": [{
            "tool": "property_filter",
            "summary": "access_token=prod-token " + ("x" * 5000),
        }],
        "warnings": ["client_secret=prod-secret"],
        "password": "prod-password",
    })

    def write_artifact(tool_call_id, output):
        persisted.append((tool_call_id, output))
        return {
            "output_ref": "a" * 32,
            "output_size": len(output.encode("utf-8")),
        }

    with tool_output_artifact_writer_context(write_artifact):
        payload = build_tool_finished_payload(
            tool_name="business_data_search",
            tool_call_id="call-sensitive",
            status="error",
            raw_output=raw_output,
            error="db_password=prod-db-pass",
            elapsed_ms=7,
        )

    serialized = json.dumps(payload, ensure_ascii=False)
    persisted_output = persisted[0][1]

    assert "prod-" not in serialized
    assert "prod-" not in persisted_output
    assert payload["error"] == "db_password=[REDACTED]"
    assert payload["output_projection"]["executed_steps"][0]["summary"].startswith(
        "access_token=[REDACTED]"
    )
    assert payload["output_projection"]["warnings"] == [
        "client_secret=[REDACTED]"
    ]


def test_tool_started_redacts_sensitive_input_without_trace_collector():
    events = []
    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-sensitive-input",
        session_id="session-sensitive-input",
        turn_id=1,
        agent_config=AgentConfig.from_env(),
        caller="main",
    )

    with stream_writer_context(events.append):
        emit_tool_started(
            context=context,
            tool_name="business_data_search",
            tool_call_id="call-sensitive-input",
            tool_input={
                "access_token": "prod-token",
                "query": "client_secret=prod-secret",
            },
            message=None,
        )

    payload = events[0]["data"]["payload"]

    assert payload["input"]["access_token"] == "[REDACTED]"
    assert payload["input"]["query"] == "client_secret=[REDACTED]"


def test_tool_finished_redacts_protocol_and_observability_payloads():
    protocol_events = []
    runtime_events = []

    class Observer:
        enabled = True

        def emit(self, event):
            runtime_events.append(event)
            return True

    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-sensitive-output",
        session_id="session-sensitive-output",
        turn_id=1,
        agent_config=AgentConfig.from_env(),
        caller="main",
        agent_iteration=1,
        observer=Observer(),
    )

    with stream_writer_context(protocol_events.append):
        emit_tool_finished(
            context=context,
            tool_name="business_data_search",
            tool_call_id="call-sensitive-output",
            status="completed",
            raw_output="access_token=prod-token",
            error=None,
            elapsed_ms=9,
        )

    protocol_payload = protocol_events[0]["data"]["payload"]
    observability_payload = runtime_events[0].payload

    assert protocol_payload["output_preview"] == "access_token=[REDACTED]"
    assert observability_payload["output_full"] == "access_token=[REDACTED]"
    assert "prod-token" not in json.dumps(protocol_payload)
    assert "prod-token" not in json.dumps(observability_payload)


def test_web_search_finished_keeps_bounded_structured_projection_when_preview_is_truncated():
    raw_output = json.dumps({
        "ok": True,
        "query": "long result query",
        "provider": "tavily",
        "response_time": 0.42,
        "results": [
            {
                "title": "Unsafe data URL",
                "url": "data:text/html,<script>alert(1)</script>",
                "snippet": "must be omitted",
            },
            {
                "title": "Unsafe file URL",
                "url": "file:///etc/passwd",
                "snippet": "must be omitted",
            },
        ] + [
            {
                "title": "Result {}".format(index),
                "url": "https://example.com/{}/article".format(index),
                "snippet": "摘要" * 900,
            }
            for index in range(8)
        ],
    }, ensure_ascii=False)

    payload = build_tool_finished_payload(
        tool_name="web_search",
        tool_call_id="call-web-long",
        status="completed",
        raw_output=raw_output,
        error=None,
        elapsed_ms=420,
    )

    assert payload["output_truncated"] is True
    assert len(payload["output_preview"]) == 4096
    assert payload["output_projection"]["provider"] == "tavily"
    assert payload["output_projection"]["response_time"] == 0.42
    assert payload["output_projection"]["result_count"] == 10
    assert len(payload["output_projection"]["results"]) == 8
    assert all(
        result["url"].startswith("https://")
        for result in payload["output_projection"]["results"]
    )
    assert payload["output_projection"]["results"][0]["domain"] == "example.com"
    assert len(payload["output_projection"]["results"][0]["snippet"]) == 320


def test_web_search_projection_replaces_non_finite_response_time():
    for response_time in (float("nan"), float("inf"), float("-inf")):
        raw_output = json.dumps({
            "ok": True,
            "query": "safe json",
            "provider": "tavily",
            "response_time": response_time,
            "results": [],
        })

        payload = build_tool_finished_payload(
            tool_name="web_search",
            tool_call_id="call-web-non-finite",
            status="completed",
            raw_output=raw_output,
            error=None,
            elapsed_ms=10,
        )

        assert payload["output_projection"]["response_time"] is None
        json.dumps(payload, allow_nan=False)


def test_web_search_projection_deduplicates_normalized_urls():
    raw_output = json.dumps({
        "ok": True,
        "query": "deduplicate",
        "provider": "tavily",
        "results": [
            {"title": "First", "url": "https://EXAMPLE.com:443/article#summary"},
            {"title": "Duplicate", "url": "https://example.com/article#details"},
            {"title": "Different query", "url": "https://example.com/article?page=2"},
            {"title": "Different path", "url": "https://example.com/article/"},
        ],
    })

    payload = build_tool_finished_payload(
        tool_name="web_search",
        tool_call_id="call-web-deduplicate",
        status="completed",
        raw_output=raw_output,
        error=None,
        elapsed_ms=10,
    )

    projected = payload["output_projection"]["results"]
    assert [result["title"] for result in projected] == [
        "First",
        "Different query",
        "Different path",
    ]
    assert projected[0]["url"] == "https://EXAMPLE.com:443/article#summary"


def test_business_data_search_finished_exposes_bounded_execution_projection():
    raw_output = json.dumps({
        "success": True,
        "provider": "neo4j",
        "entity": "CHANGE_ORDER变更技术修改执行周期",
        "query": "查询 CHANGE_ORDER-001 的执行周期",
        "result_count": 1,
        "records": [{"raw": "x" * 5000}],
        "evidence": [{
            "evidence_id": "graph_ev_001",
            "query_digest": "sha256:query",
        }],
        "executed_steps": [
            {
                "tool": "property_filter",
                "summary": "执行内部图工具 property_filter，返回 1 条",
            },
            {
                "tool": "GraphQueryAgent",
                "summary": "s" * 500,
            },
        ],
    }, ensure_ascii=False)

    payload = build_tool_finished_payload(
        tool_name="business_data_search",
        tool_call_id="call-business-long",
        status="completed",
        raw_output=raw_output,
        error=None,
        elapsed_ms=100,
    )

    projection = payload["output_projection"]
    assert payload["output_truncated"] is True
    assert projection["provider"] == "neo4j"
    assert projection["result_count"] == 1
    assert projection["query_digest"] == "sha256:query"
    assert projection["has_evidence"] is True
    assert projection["executed_steps"][0] == {
        "tool": "property_filter",
        "summary": "执行内部图工具 property_filter，返回 1 条",
    }
    assert len(projection["executed_steps"][1]["summary"]) == 300
    assert "records" not in projection
    assert "evidence" not in projection


def test_lookup_values_projection_counts_real_provider_requests():
    raw_output = json.dumps({
        "action": "lookup_values",
        "entity": "object_demo",
        "status": "partial",
        "value_results": [
            {
                "binding_key": "binding_a",
                "field_keys": ["field_a"],
                "input_values": ["value_a"],
                "content": "result_a",
            }
        ],
        "errors": [
            {
                "code": "VALUE_PROVIDER_FAILED",
                "message": "request failed",
                "binding_key": "binding_b",
            },
            {
                "code": "VALUE_INPUT_INVALID",
                "message": "invalid input",
                "binding_key": "",
            },
        ],
    })

    payload = build_tool_finished_payload(
        tool_name="business_data_search",
        tool_call_id="call-value-lookup",
        status="completed",
        raw_output=raw_output,
        error=None,
        elapsed_ms=100,
    )

    projection = payload["output_projection"]
    assert projection["action"] == "lookup_values"
    assert projection["status"] == "partial"
    assert projection["provider_request_count"] == 2
    assert projection["error_count"] == 2


def test_execute_tool_batch_emits_v2_regular_tool_events():
    captured = []

    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-tool-v2",
        session_id="sess-v2",
        turn_id=3,
        agent_config=AgentConfig.from_env(),
        caller="main",
        agent_iteration=4,
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

    tool_events = [
        event["data"]
        for event in captured
        if event["data"]["type"] in {"tool.started", "tool.finished"}
    ]
    assert tool_events
    assert all(
        event["span_key"] == "tool:main:4:business_data_search:call_1"
        for event in tool_events
    )
    assert all(event["parent_key"] == "iteration:main:4" for event in tool_events)
