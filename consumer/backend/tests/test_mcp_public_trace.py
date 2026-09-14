# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json
import threading

from agent.tools.events import build_tool_finished_payload
from api.protocols.mcp.public_trace import MCPPublicTraceCollector
from backend_synthetic_credentials import synthetic_invalid_password
from integration.observability.events import RuntimeEvent, RuntimeEventType


def test_public_trace_deduplicates_protocol_and_otel_tool_lifecycle_events():
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=200)
    span_id = "2" * 16
    base_payload = {"iteration": 1, "tool_name": "search", "tool_call_id": "tool-1"}
    lifecycle = [
        (RuntimeEventType.TOOL_STARTED, {}),
        (RuntimeEventType.TOOL_PROGRESS, {"status": "running"}),
        (RuntimeEventType.TOOL_PROGRESS, {"status": "running", "progress_index": 2}),
        (RuntimeEventType.TOOL_FINISHED, {"status": "completed", "elapsed_ms": 12}),
    ]
    for event_type, extra_payload in lifecycle:
        collector.consume(
            {
                "type": event_type,
                "span_id": span_id,
                "payload": {**base_payload, **extra_payload},
            }
        )
    for event_type, extra_payload in lifecycle:
        collector.consume_runtime_event(
            RuntimeEvent.from_otel(
                type=event_type,
                run_id="run-deduplicate",
                span_id=span_id,
                payload={**base_payload, **extra_payload},
            )
        )

    trace = collector.snapshot()
    assert trace["totals"]["tool_calls"] == 1
    assert trace["totals"]["tool_execution_elapsed_ms"] == 12
    assert trace["iterations"][0]["tools"]["count"] == 1
    assert trace["iterations"][0]["tools"]["calls"][0]["status"] == "completed"


from integration.mcp.config import load_mcp_runtime_config


def _event(event_type, payload, *, seq=1):
    return {
        "protocol": "agent-stream.v2",
        "type": event_type,
        "seq": seq,
        "payload": payload,
    }


def test_public_trace_extracts_safe_tool_evidence_and_timings():
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=200)
    collector.consume(_event("token.delta", {"channel": "reasoning", "text": "private chain"}))
    collector.consume(
        _event(
            "token.delta",
            {
                "channel": "content",
                "iteration": 1,
                "text": "检查业务数据，Authorization: Bearer top-secret-token",
            },
            seq=2,
        )
    )
    collector.consume(
        _event(
            "reasoning.summary",
            {"iteration": 1, "summary": "不应进入公共 Trace"},
            seq=3,
        )
    )
    collector.consume(
        _event(
            "tool.started",
            {
                "iteration": 1,
                "tool_call_id": "call-1",
                "tool_name": "business_data_search",
                "input": {
                    "ontology_id": "equipment",
                    "entity": "TargetItem",
                    "query": "查询 TargetItem，api_key=very-secret",
                    "password": synthetic_invalid_password("mcp_public_trace-credential-01"),
                },
            },
            seq=3,
        )
    )
    collector.consume(
        _event(
            "tool.progress",
            {
                "iteration": 1,
                "tool_call_id": "call-1",
                "tool_name": "business_data_search",
                "ontology_id": "equipment",
                "database_provider": "neo4j",
                "progress": {
                    "business_data_search_event": {
                        "type": "final",
                        "content": json.dumps(
                            {
                                "success": True,
                                "provider": "neo4j",
                                "result_count": 2,
                                "records": [{"serial": "RAW-RECORD-MUST-NOT-APPEAR"}],
                                "evidence": [
                                    {
                                        "evidence_id": "graph_ev_001",
                                        "query_digest": "sha256:database-query",
                                    }
                                ],
                                "executed_steps": [
                                    {
                                        "tool": "property_filter",
                                        "summary": "执行内部图工具 property_filter，返回 2 条，access_token=step-secret",
                                    }
                                ],
                                "warnings": ["retry client_secret=warning-secret"],
                            }
                        ),
                    }
                },
            },
            seq=4,
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            {
                "iteration": 1,
                "tool_call_id": "call-1",
                "tool_name": "business_data_search",
                "status": "completed",
                "elapsed_ms": 125,
                "output_preview": "not-json-and-not-public",
            },
            seq=5,
        )
    )
    collector.consume(
        _event(
            "iteration.completed",
            {
                "iteration": 1,
                "status": "completed",
                "prompt_build_ms": 10,
                "llm_elapsed_ms": 999,
                "llm_stream_elapsed_ms": 998,
                "llm_wall_elapsed_ms": 997,
                "model_elapsed_ms": 80,
                "tools_elapsed_ms": 125,
                "tools_wall_elapsed_ms": 124,
                "setup_elapsed_ms": 15,
                "total_elapsed_ms": 220,
                "tool_calls_count": 1,
                "tool_names": ["business_data_search"],
            },
            seq=6,
        )
    )

    trace = collector.snapshot()
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["schema_version"] == "5.0"
    assert trace["level"] == "summary"
    assert "top-secret-token" not in serialized
    assert "private chain" not in serialized
    assert synthetic_invalid_password("mcp_public_trace-credential-01") not in serialized
    assert "RAW-RECORD-MUST-NOT-APPEAR" not in serialized
    assert "step-secret" not in serialized
    assert "不应进入公共 Trace" not in serialized
    iteration = trace["iterations"][0]
    assert "reasoning_summaries" not in iteration
    assert iteration["agent_output"] == {
        "summary": "检查业务数据，Authorization: [REDACTED]",
        "source": "agent_content",
        "truncated": False,
    }
    assert iteration["tools"]["count"] == 1
    assert iteration["tools"]["succeeded"] == 1
    assert iteration["tools"]["failed"] == 0
    tool = iteration["tools"]["calls"][0]
    assert tool["name"] == "business_data_search"
    assert tool["ontology_id"] == "equipment"
    assert tool["entity"] == "TargetItem"
    assert tool["database_provider"] == "neo4j"
    assert tool["record_count"] == 2
    assert tool["executed_steps"] == [
        {
            "tool": "property_filter",
            "summary": "执行内部图工具 property_filter，返回 2 条，access_token=[REDACTED]",
        }
    ]
    assert "evidence_ids" not in tool
    assert tool["query_digest"] == "sha256:database-query"
    assert iteration["timing"] == {
        "total_elapsed_ms": 220,
        "model_elapsed_ms": 80,
        "tools_elapsed_ms": 125,
        "setup_elapsed_ms": 15,
    }
    assert trace["totals"]["tool_calls_succeeded"] == 1
    assert trace["totals"]["tool_execution_elapsed_ms"] == 125
    assert trace["totals"]["iterations"] == 1
    assert trace["totals"]["overall_elapsed_ms"] >= 0
    assert "events_observed" not in trace["totals"]
    assert "iteration_elapsed_ms" not in trace["totals"]
    assert trace["warnings"][0]["message"] == "retry client_secret=[REDACTED]"


def test_public_trace_preserves_lookup_values_projection_fields():
    """lookup_values projection fields must survive the public trace filter."""
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=200)
    collector.consume(
        _event(
            "tool.started",
            {
                "iteration": 1,
                "tool_call_id": "call-lv",
                "tool_name": "business_data_search",
                "input": {
                    "ontology_id": "demo",
                    "entity": "flight",
                    "query": "查询 KUL 的中文名",
                    "action": "lookup_values",
                },
            },
            seq=1,
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            build_tool_finished_payload(
                tool_name="business_data_search",
                tool_call_id="call-lv",
                status="completed",
                raw_output=json.dumps(
                    {
                        "action": "lookup_values",
                        "entity": "flight",
                        "status": "completed",
                        "value_results": [
                            {
                                "binding_key": "airport-code",
                                "field_keys": ["LATEST_DEP_ARP_CD"],
                                "input_values": ["KUL"],
                                "content": "KUL 对应吉隆坡",
                            },
                        ],
                        "errors": [],
                    }
                ),
                error=None,
                elapsed_ms=50,
            ),
            seq=2,
        )
    )
    collector.consume(
        _event(
            "iteration.completed",
            {
                "iteration": 1,
                "status": "completed",
                "total_elapsed_ms": 100,
            },
            seq=3,
        )
    )

    trace = collector.snapshot()
    tool = trace["iterations"][0]["tools"]["calls"][0]
    assert tool["name"] == "business_data_search"
    assert tool["action"] == "lookup_values"
    assert tool["value_result_count"] == 1
    assert tool["binding_keys"] == ["airport-code"]
    assert tool["provider_request_count"] == 1
    assert "error_codes" not in tool


def test_public_trace_redacts_json_string_and_prefixed_secret_formats():
    collector = MCPPublicTraceCollector(max_text_chars=1000)
    collector.consume(
        _event(
            "token.delta",
            {
                "iteration": 1,
                "channel": "content",
                "text": (
                    "access_token=prod-token "
                    "client_secret=prod-secret "
                    "db_password=prod-pass "
                    '{"password":"prod-json-pass"} '
                    "private_key=prod-key"
                ),
            },
        )
    )

    serialized = json.dumps(collector.snapshot(), ensure_ascii=False)

    assert "prod-" not in serialized
    assert serialized.count("[REDACTED]") >= 5


def test_public_trace_uses_iteration_content_and_final_output_precedence():
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=200)
    collector.consume(
        _event(
            "token.delta",
            {"iteration": 1, "channel": "reasoning", "text": "private chain"},
            seq=1,
        )
    )
    collector.consume(
        _event(
            "token.delta",
            {"iteration": 1, "channel": "content", "text": "先读取"},
            seq=2,
        )
    )
    collector.consume(
        _event(
            "token.delta",
            {"iteration": 1, "channel": "content", "text": "本体上下文"},
            seq=3,
        )
    )
    collector.consume(
        _event(
            "iteration.completed",
            {
                "iteration": 1,
                "status": "continue",
                "agent_output": {"type": "tool_decision", "summary": "旧摘要"},
            },
            seq=4,
        )
    )
    collector.consume(
        _event(
            "token.delta",
            {"iteration": 2, "channel": "content", "text": "流式最终输出"},
            seq=5,
        )
    )
    collector.consume(
        _event(
            "turn.finished",
            {"iteration": 2, "status": "success", "final_output": "最终答案"},
            seq=6,
        )
    )

    trace = collector.snapshot()

    assert trace["iterations"][0]["agent_output"] == {
        "summary": "先读取本体上下文",
        "source": "agent_content",
        "truncated": False,
    }
    assert trace["iterations"][1]["agent_output"] == {
        "summary": "最终答案",
        "source": "final_output",
        "truncated": False,
    }
    assert "private chain" not in json.dumps(trace, ensure_ascii=False)


def test_public_trace_counts_only_retained_final_output_truncation():
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=32)
    collector.consume(
        _event(
            "token.delta",
            {"iteration": 1, "channel": "content", "text": "s" * 64},
        )
    )
    collector.consume(
        _event(
            "turn.finished",
            {"iteration": 1, "status": "success", "final_output": "f" * 64},
            seq=2,
        )
    )

    trace = collector.snapshot()

    assert trace["iterations"][0]["agent_output"] == {
        "summary": "f" * 32,
        "source": "final_output",
        "truncated": True,
    }
    assert trace["omitted"]["text_fields_truncated"] == 1


def test_public_trace_accepts_async_runtime_public_event_fields():
    collector = MCPPublicTraceCollector(
        max_events=20,
        max_bytes=32768,
        max_text_chars=200,
    )
    collector.consume(
        _event(
            "tool.started",
            {
                "name": "ontology_context",
                "tool_call_id": "call-public",
                "input_preview": {
                    "ontology_id": "ontology_demo",
                    "resource": "object",
                    "key": "SyntheticObject",
                },
            },
            seq=1,
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            {
                "name": "ontology_context",
                "tool_call_id": "call-public",
                "status": "completed",
                "elapsed_ms": 5,
            },
            seq=2,
        )
    )
    collector.consume(
        _event(
            "turn.finished",
            {"status": "completed", "content": "公共异步结果"},
            seq=3,
        )
    )

    trace = collector.snapshot()

    assert trace["iterations"][0]["agent_output"] == {
        "summary": "公共异步结果",
        "source": "final_output",
        "truncated": False,
    }
    assert trace["iterations"][0]["status"] == "final_output"
    tool_call = trace["iterations"][0]["tools"]["calls"][0]
    assert tool_call["name"] == "ontology_context"
    assert tool_call["ontology_id"] == "ontology_demo"
    assert tool_call["resource"] == "object"
    assert tool_call["key"] == "SyntheticObject"

    replacement_collector = MCPPublicTraceCollector(
        max_events=20,
        max_bytes=32768,
        max_text_chars=32,
    )
    replacement_collector.consume(
        _event(
            "token.delta",
            {"iteration": 1, "channel": "content", "text": "s" * 64},
        )
    )
    replacement_collector.consume(
        _event(
            "turn.finished",
            {"iteration": 1, "status": "success", "final_output": "short answer"},
            seq=2,
        )
    )

    replacement_trace = replacement_collector.snapshot()

    assert replacement_trace["iterations"][0]["agent_output"]["truncated"] is False
    assert replacement_trace["truncated"] is False
    assert replacement_trace["omitted"] == {}


def test_public_trace_bounds_and_redacts_agent_content():
    collector = MCPPublicTraceCollector(max_events=20, max_bytes=32768, max_text_chars=32)
    collector.consume(
        _event(
            "token.delta",
            {
                "iteration": 1,
                "channel": "content",
                "text": "token=very-secret " + ("x" * 64),
            },
        )
    )

    trace = collector.snapshot()
    agent_output = trace["iterations"][0]["agent_output"]

    assert agent_output["source"] == "agent_content"
    assert agent_output["truncated"] is True
    assert "very-secret" not in agent_output["summary"]
    assert trace["truncated"] is True
    assert trace["omitted"]["text_fields_truncated"] == 1


def test_public_trace_reports_truncation_for_query_fallback_summary():
    query_collector = MCPPublicTraceCollector(max_text_chars=32)
    query_collector.consume(
        _event(
            "tool.started",
            {
                "iteration": 1,
                "tool_call_id": "query-call",
                "tool_name": "business_data_search",
                "input": {"query": "q" * 64},
            },
        )
    )
    query_collector.consume(
        _event(
            "tool.finished",
            {
                "iteration": 1,
                "tool_call_id": "query-call",
                "tool_name": "business_data_search",
                "status": "completed",
            },
        )
    )

    query_trace = query_collector.snapshot()
    query_output = query_trace["iterations"][0]["agent_output"]

    assert query_output == {
        "summary": "q" * 32,
        "source": "tool_query_summary",
        "truncated": True,
    }
    assert "_query_summary_truncated" not in query_trace["iterations"][0]["tools"]["calls"][0]


def test_public_trace_uses_structured_business_projection():
    collector = MCPPublicTraceCollector()
    collector.consume(
        _event(
            "tool.started",
            {
                "iteration": 1,
                "tool_call_id": "projected-call",
                "tool_name": "business_data_search",
                "input": {
                    "ontology_id": "equipment",
                    "entity": "TargetItem",
                    "query": "查询 TargetItem",
                },
            },
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            {
                "iteration": 1,
                "tool_call_id": "projected-call",
                "tool_name": "business_data_search",
                "status": "completed",
                "output_projection": {
                    "provider": "neo4j",
                    "result_count": 1,
                    "query_digest": "sha256:projected-query",
                    "has_evidence": True,
                    "executed_steps": [
                        {
                            "tool": "property_filter",
                            "summary": "执行内部图工具 property_filter，返回 1 条",
                        }
                    ],
                },
            },
        )
    )

    tool = collector.snapshot()["iterations"][0]["tools"]["calls"][0]

    assert tool["query_digest"] == "sha256:projected-query"
    assert tool["executed_steps"][0]["tool"] == "property_filter"
    assert tool["record_count"] == 1
    assert "evidence_ids" not in tool


def test_public_trace_limits_prioritize_failures_and_evidence():
    collector = MCPPublicTraceCollector(max_events=2, max_bytes=32768, max_text_chars=100)
    for index in range(5):
        collector.consume(
            _event(
                "iteration.completed",
                {
                    "iteration": index + 1,
                    "status": "continue",
                    "total_elapsed_ms": index + 1,
                },
                seq=index + 1,
            )
        )

    collector.consume(
        _event(
            "tool.started",
            {
                "tool_call_id": "evidence-call",
                "tool_name": "business_data_search",
                "input": {"query": "query-with-evidence"},
            },
            seq=10,
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            {
                "tool_call_id": "evidence-call",
                "tool_name": "business_data_search",
                "status": "completed",
                "output_preview": json.dumps(
                    {
                        "result_count": 1,
                        "evidence": [{"evidence_id": "ev-1"}],
                    }
                ),
            },
            seq=11,
        )
    )
    collector.consume(
        _event(
            "tool.started",
            {"tool_call_id": "failed-call", "tool_name": "business_data_search"},
            seq=12,
        )
    )
    collector.consume(
        _event(
            "tool.finished",
            {
                "tool_call_id": "failed-call",
                "tool_name": "business_data_search",
                "status": "error",
                "error": "GRAPH_BACKEND_TIMEOUT",
                "output_preview": json.dumps({"error_code": "GRAPH_BACKEND_TIMEOUT"}),
            },
            seq=13,
        )
    )

    trace = collector.snapshot()

    assert trace["truncated"] is True
    calls = trace["iterations"][0]["tools"]["calls"]
    assert {tool["tool_call_id"] for tool in calls} == {
        "evidence-call",
        "failed-call",
    }
    assert trace["omitted"]["iterations"] == 5


def test_public_trace_enforces_serialized_byte_limit():
    collector = MCPPublicTraceCollector(max_events=30, max_bytes=1024, max_text_chars=500)
    for index in range(30):
        collector.consume(
            _event(
                "token.delta",
                {
                    "channel": "content",
                    "iteration": index + 1,
                    "text": "x" * 500,
                },
                seq=index + 1,
            )
        )

    trace = collector.snapshot()

    assert len(json.dumps(trace, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 1024
    assert trace["truncated"] is True
    assert trace["omitted"]["byte_limit_events"] > 0


def test_public_trace_collector_is_thread_safe():
    collector = MCPPublicTraceCollector(max_events=100, max_bytes=65536, max_text_chars=100)

    def emit_tools(offset):
        for index in range(20):
            call_id = "call-{}-{}".format(offset, index)
            collector.consume(
                _event(
                    "tool.started",
                    {"tool_call_id": call_id, "tool_name": "ontology_context"},
                )
            )
            collector.consume(
                _event(
                    "tool.finished",
                    {
                        "tool_call_id": call_id,
                        "tool_name": "ontology_context",
                        "status": "completed",
                        "elapsed_ms": 1,
                    },
                )
            )

    threads = [threading.Thread(target=emit_tools, args=(offset,)) for offset in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    trace = collector.snapshot()

    assert trace["totals"]["tool_calls"] == 80
    assert trace["totals"]["tool_calls_succeeded"] == 80
    assert trace["totals"]["iterations"] == 1
    assert len(trace["iterations"][0]["tools"]["calls"]) == 80


def test_public_trace_drops_stack_traces_and_does_not_infer_non_query_retries():
    collector = MCPPublicTraceCollector()
    for call_id in ("read-1", "read-2"):
        collector.consume(
            _event(
                "tool.started",
                {"tool_call_id": call_id, "tool_name": "ontology_context"},
            )
        )
        collector.consume(
            _event(
                "tool.finished",
                {
                    "tool_call_id": call_id,
                    "tool_name": "ontology_context",
                    "status": "error",
                    "error": "Traceback (most recent call last):\n  private frame\nValueError: failed",
                },
            )
        )

    trace = collector.snapshot()
    serialized = json.dumps(trace)

    assert trace["totals"]["retries"] == 0
    calls = trace["iterations"][0]["tools"]["calls"]
    assert all(tool["error_summary"] == "Tool execution failed" for tool in calls)
    assert "private frame" not in serialized


def test_public_trace_does_not_treat_long_queries_with_different_suffixes_as_retries():
    collector = MCPPublicTraceCollector(max_text_chars=500)
    shared_prefix = "q" * 500
    for index, suffix in enumerate(("first", "second"), start=1):
        call_id = "query-{}".format(index)
        collector.consume(
            _event(
                "tool.started",
                {
                    "iteration": index,
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                    "input": {
                        "ontology_id": "ontology-1",
                        "entity": "Product",
                        "query": shared_prefix + suffix,
                    },
                },
            )
        )
        collector.consume(
            _event(
                "tool.finished",
                {
                    "iteration": index,
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                    "status": "completed",
                },
            )
        )

    trace = collector.snapshot()
    calls = [iteration["tools"]["calls"][0] for iteration in trace["iterations"]]
    serialized = json.dumps(trace, ensure_ascii=False)

    assert trace["totals"]["retries"] == 0
    assert all("retry_count" not in call for call in calls)
    assert all(call["query_summary"] == shared_prefix for call in calls)
    assert "_retry_fingerprint" not in serialized


def test_public_trace_counts_identical_full_queries_as_retries():
    collector = MCPPublicTraceCollector(max_text_chars=32)
    query = "same query " + ("x" * 64)
    for index in range(1, 3):
        call_id = "retry-{}".format(index)
        collector.consume(
            _event(
                "tool.started",
                {
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                    "input": {
                        "ontology_id": "ontology-1",
                        "entity": "Product",
                        "query": query,
                    },
                },
            )
        )
        collector.consume(
            _event(
                "tool.finished",
                {
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                    "status": "completed",
                },
            )
        )

    trace = collector.snapshot()
    calls = trace["iterations"][0]["tools"]["calls"]

    assert trace["totals"]["retries"] == 1
    assert "retry_count" not in calls[0]
    assert calls[1]["retry_count"] == 1


def test_public_trace_uses_full_result_query_for_retry_fingerprint():
    collector = MCPPublicTraceCollector(max_text_chars=32)
    shared_prefix = "r" * 32
    for index, suffix in enumerate(("first", "second"), start=1):
        call_id = "result-query-{}".format(index)
        collector.consume(
            _event(
                "tool.started",
                {
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                },
            )
        )
        collector.consume(
            _event(
                "tool.finished",
                {
                    "tool_call_id": call_id,
                    "tool_name": "business_data_search",
                    "status": "completed",
                    "output_projection": {
                        "query": shared_prefix + suffix,
                    },
                },
            )
        )

    trace = collector.snapshot()

    assert trace["totals"]["retries"] == 0
    assert all("retry_count" not in call for call in trace["iterations"][0]["tools"]["calls"])


def test_public_trace_merges_iteration_snapshots_and_reports_wall_time(monkeypatch):
    ticks = iter((10.0, 10.25))
    monkeypatch.setattr(
        "api.protocols.mcp.public_trace.time.perf_counter",
        lambda: next(ticks),
    )
    collector = MCPPublicTraceCollector()
    tool_durations = (16, 20, 15)
    for tool_index, tools_elapsed_ms in enumerate((19, 31, 33), start=1):
        call_id = "context-{}".format(tool_index)
        collector.consume(
            _event(
                "tool.started",
                {
                    "iteration": 2,
                    "tool_call_id": call_id,
                    "tool_name": "ontology_context",
                    "input": {
                        "ontology_id": "equipment",
                        "resource": "object",
                        "key": "TargetItem",
                    },
                },
            )
        )
        collector.consume(
            _event(
                "tool.finished",
                {
                    "iteration": 2,
                    "tool_call_id": call_id,
                    "tool_name": "ontology_context",
                    "status": "completed",
                    "elapsed_ms": tool_durations[tool_index - 1],
                },
            )
        )
        collector.consume(
            _event(
                "iteration.completed",
                {
                    "iteration": 2,
                    "status": "continue",
                    "model_elapsed_ms": 5395,
                    "tools_elapsed_ms": tools_elapsed_ms,
                    "setup_elapsed_ms": 5,
                    "total_elapsed_ms": 5400 + tools_elapsed_ms,
                    "tool_calls_count": tool_index,
                    "tool_names": ["ontology_context"] * tool_index,
                },
                seq=tool_index,
            )
        )

    trace = collector.snapshot()

    assert len(trace["iterations"]) == 1
    assert trace["iterations"][0]["iteration"] == 2
    assert trace["iterations"][0]["timing"] == {
        "total_elapsed_ms": 5433,
        "model_elapsed_ms": 5395,
        "tools_elapsed_ms": 33,
        "setup_elapsed_ms": 5,
    }
    assert trace["iterations"][0]["agent_output"] == {
        "summary": "调用工具：ontology_context",
        "source": "tool_name",
        "truncated": False,
    }
    assert trace["iterations"][0]["tools"]["count"] == 3
    assert len(trace["iterations"][0]["tools"]["calls"]) == 3
    assert trace["iterations"][0]["tools"]["calls"][0]["resource"] == "object"
    assert trace["iterations"][0]["tools"]["calls"][0]["key"] == "TargetItem"
    assert trace["totals"]["iterations"] == 1
    assert trace["totals"]["tool_calls"] == 3
    assert trace["totals"]["tool_execution_elapsed_ms"] == sum(tool_durations)
    assert trace["totals"]["overall_elapsed_ms"] == 250


def test_mcp_trace_limit_configuration_loads_and_clamps_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("integration.mcp.config._load_local_dotenv", lambda: None)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("MCP_TOKEN_SECRET", "test-secret-at-least-32-characters-long")
    monkeypatch.setenv("MCP_CHAT_TRACE_MAX_EVENTS", "0")
    monkeypatch.setenv("MCP_CHAT_TRACE_MAX_BYTES", "100")
    monkeypatch.setenv("MCP_CHAT_TRACE_MAX_TEXT_CHARS", "99999")
    monkeypatch.setenv("MCP_CHAT_PROGRESS_MAX_EVENTS", "99999")
    monkeypatch.setenv("MCP_CHAT_PROGRESS_MAX_MESSAGE_BYTES", "100")
    monkeypatch.setenv("MCP_CHAT_PROGRESS_QUEUE_SIZE", "1")
    monkeypatch.setenv("MCP_CHAT_PROGRESS_HEARTBEAT_SECONDS", "999")

    config = load_mcp_runtime_config()

    assert not hasattr(config, "chat_trace_enabled")
    assert config.chat_trace_max_events == 1
    assert config.chat_trace_max_bytes == 1024
    assert config.chat_trace_max_text_chars == 4000
    assert config.chat_progress_max_events == 500
    assert config.chat_progress_max_message_bytes == 1024
    assert config.chat_progress_queue_size == 16
    assert config.chat_progress_heartbeat_seconds == 60
