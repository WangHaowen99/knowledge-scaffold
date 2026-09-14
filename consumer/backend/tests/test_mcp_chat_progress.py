"""MCP chat coarse progress projection tests."""

from __future__ import annotations

from agent.tools.executor import _build_business_data_search_event_emitter
from api.protocols.mcp.progress import (
    MCPChatProgressProjector,
    build_public_summary,
)


def test_public_summary_appends_only_allowed_safe_preview():
    assert (
        build_public_summary(
            template="正在查询业务数据",
            candidate="模板未命中，改用自然语言查询",
            candidate_allowed=True,
        )
        == "正在查询业务数据：模板未命中，改用自然语言查询"
    )

    for unsafe in (
        "SELECT demo_field FROM demo_table",
        "https://service.example.net/internal",
        "password=<redacted>",
        "/srv/private/config/runtime.json",
    ):
        assert (
            build_public_summary(
                template="正在查询业务数据",
                candidate=unsafe,
                candidate_allowed=True,
            )
            == "正在查询业务数据"
        )

    assert (
        build_public_summary(
            template="正在查询业务数据",
            candidate="允许的候选",
            candidate_allowed=False,
        )
        == "正在查询业务数据"
    )


def test_public_summary_redacts_before_bounded_preview():
    summary = build_public_summary(
        template="正在执行相关能力",
        candidate="A" * 200,
        candidate_allowed=True,
        preview_limit=20,
        summary_limit=60,
    )

    assert summary.startswith("正在执行相关能力：")
    assert summary.endswith("...")
    assert len(summary) <= 60


def test_projector_returns_coarse_trace_without_raw_inputs_or_results():
    projector = MCPChatProgressProjector()
    events = []
    events.append(projector.emit_run_started(run_id="run_demo"))
    events.append(
        projector.emit_ontology_ready(
            run_id="run_demo",
            elapsed_ms=12,
        )
    )
    events.extend(
        projector.consume(
            event_type="model.started",
            payload={"model_profile_id": "profile_demo"},
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="token.delta",
            payload={"text": "pre-tool model text"},
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="tool.started",
            payload={
                "name": "business_data_search",
                "tool_call_id": "call_demo",
                "input_preview": {
                    "query": "synthetic private query",
                    "password": "<redacted>",
                },
            },
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="tool.progress",
            payload={
                "tool_name": "business_data_search",
                "tool_call_id": "call_demo",
                "database_provider": "nl2sql_template",
                "progress": {
                    "business_data_search_event": {
                        "type": "intermediate",
                        "content": "SELECT demo_field FROM demo_table",
                        "public_summary": "模板未命中，改用自然语言查询",
                        "public_summary_allowed": True,
                    }
                },
            },
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="tool.progress",
            payload={
                "tool_name": "business_data_search",
                "tool_call_id": "call_demo",
                "database_provider": "nl2sql_template",
                "progress": {
                    "business_data_search_event": {
                        "type": "final",
                        "content": '{"success":true,"data":[{"id":"DEMO-001"},{"id":"DEMO-002"}]}',
                    }
                },
            },
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="tool.finished",
            payload={
                "name": "business_data_search",
                "tool_call_id": "call_demo",
                "status": "completed",
                "elapsed_ms": 25,
                "output_preview": "synthetic private result",
            },
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="model.started",
            payload={"model_profile_id": "profile_demo"},
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="token.delta",
            payload={"text": "ignored second delta"},
            run_id="run_demo",
        )
    )
    events.extend(
        projector.consume(
            event_type="turn.finished",
            payload={"status": "completed", "content": "synthetic full answer"},
            run_id="run_demo",
        )
    )
    projected = [item for item in events if item is not None]

    assert [item["event"] for item in projected] == [
        "run.started",
        "ontology.ready",
        "analysis.started",
        "tool.started",
        "tool.progress",
        "tool.completed",
        "answer.preparing",
        "run.completed",
    ]
    completed = next(item for item in projected if item["event"] == "tool.completed")
    querying = next(item for item in projected if item["event"] == "tool.progress")
    assert querying["provider"] == "nl2sql_template"
    assert querying["summary"] == "已选择 NL2SQL Template 数据源，正在查询业务数据：模板未命中，改用自然语言查询"
    assert completed["provider"] == "nl2sql_template"
    assert completed["record_count"] == 2
    assert completed["elapsed_ms"] == 25
    assert completed["summary"] == "业务数据查询完成，命中 2 条记录，用时 25 ms"
    serialized = str(projected)
    assert "synthetic private query" not in serialized
    assert "synthetic private result" not in serialized
    assert "SELECT demo_field" not in serialized
    assert "pre-tool model text" not in serialized
    assert [item["seq"] for item in projected] == list(range(1, 9))


def test_projector_keeps_terminal_event_after_event_budget():
    projector = MCPChatProgressProjector(max_events=1)

    started = projector.emit_run_started(run_id="run_demo")
    skipped = projector.emit_ontology_ready(run_id="run_demo")
    terminal = projector.emit_terminal(run_id="run_demo", status="failed")

    assert started is not None
    assert skipped is None
    assert terminal is not None
    assert terminal["event"] == "run.failed"
    assert terminal["seq"] == 2


def test_projector_returns_ontology_resource_name_and_resolved_key():
    projector = MCPChatProgressProjector()

    started = projector.consume(
        event_type="tool.started",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context",
            "input_preview": {
                "resource": "object",
                "key": "Demo Material",
            },
        },
        run_id="run_demo",
    )[0]
    completed = projector.consume(
        event_type="tool.finished",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context",
            "status": "completed",
            "elapsed_ms": 8,
            "output_projection": {
                "resource": "object",
                "key": "demo_material",
            },
        },
        run_id="run_demo",
    )[0]

    assert started["summary"] == "正在读取知识包内容：业务对象，对象名=Demo Material，key=Demo Material"
    assert completed["summary"] == "知识包内容读取完成，用时 8 ms：业务对象，对象名=Demo Material，key=demo_material"


def test_projector_keeps_parallel_ontology_context_calls_separate():
    projector = MCPChatProgressProjector()

    first_started = projector.consume(
        event_type="tool.started",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context_a",
            "input_preview": {
                "resource": "object",
                "key": "Demo Material",
            },
        },
        run_id="run_demo",
    )[0]
    second_started = projector.consume(
        event_type="tool.started",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context_b",
            "input_preview": {
                "resource": "object",
                "key": "Demo Supplier",
            },
        },
        run_id="run_demo",
    )[0]
    second_completed = projector.consume(
        event_type="tool.finished",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context_b",
            "status": "completed",
            "output_projection": {
                "resource": "object",
                "key": "demo_supplier",
            },
        },
        run_id="run_demo",
    )[0]
    first_completed = projector.consume(
        event_type="tool.finished",
        payload={
            "name": "ontology_context",
            "tool_call_id": "call_context_a",
            "status": "completed",
            "output_projection": {
                "resource": "object",
                "key": "demo_material",
            },
        },
        run_id="run_demo",
    )[0]

    lifecycle_events = (
        first_started,
        second_started,
        second_completed,
        first_completed,
    )
    assert [event["tool_call_id"] for event in lifecycle_events] == [
        "call_context_a",
        "call_context_b",
        "call_context_b",
        "call_context_a",
    ]
    assert "Demo Material" in first_started["summary"]
    assert "key=demo_material" in first_completed["summary"]
    assert "Demo Supplier" in second_started["summary"]
    assert "key=demo_supplier" in second_completed["summary"]


def test_projector_deduplicates_identical_provider_progress():
    projector = MCPChatProgressProjector()
    projector.consume(
        event_type="tool.started",
        payload={
            "name": "business_data_search",
            "tool_call_id": "call_search",
        },
        run_id="run_demo",
    )
    provider_payload = {
        "tool_name": "business_data_search",
        "tool_call_id": "call_search",
        "database_provider": "neo4j",
        "progress": {
            "business_data_search_event": {
                "type": "intermediate",
                "content": "synthetic private graph detail",
            }
        },
    }

    selected = projector.consume(
        event_type="tool.progress",
        payload=provider_payload,
        run_id="run_demo",
    )
    duplicate_selected = projector.consume(
        event_type="tool.progress",
        payload=provider_payload,
        run_id="run_demo",
    )
    planning_payload = {
        **provider_payload,
        "progress": {
            "business_data_search_event": {
                "type": "intermediate",
                "content": "MATCH (private_node) RETURN private_node",
                "public_summary": "准备查询实体关联关系",
                "public_summary_allowed": True,
            }
        },
    }
    planning = projector.consume(
        event_type="tool.progress",
        payload=planning_payload,
        run_id="run_demo",
    )
    duplicate_planning = projector.consume(
        event_type="tool.progress",
        payload=planning_payload,
        run_id="run_demo",
    )
    completed = projector.consume(
        event_type="tool.finished",
        payload={
            "name": "business_data_search",
            "tool_call_id": "call_search",
            "status": "completed",
            "elapsed_ms": 12,
            "output_projection": {"result_count": 1},
        },
        run_id="run_demo",
    )[0]

    assert selected[0]["summary"] == "已选择 Neo4j 数据源，正在查询业务数据"
    assert duplicate_selected == []
    assert planning[0]["summary"] == "已选择 Neo4j 数据源，正在查询业务数据：准备查询实体关联关系"
    assert duplicate_planning == []
    assert "MATCH" not in str(selected + planning)
    assert completed["summary"] == "业务数据查询完成，命中 1 条记录，用时 12 ms"
    assert "准备" not in completed["summary"]


def test_business_data_event_projects_only_explicit_public_summary():
    emitted = []
    emitter = _build_business_data_search_event_emitter(
        stream_event_emitter=lambda event_type, payload: emitted.append((event_type, payload)),
        tool_call_id="call_demo",
        entity="DemoEntity",
        query="synthetic query",
    )

    emitter(
        {
            "type": "intermediate",
            "content": "SELECT demo_field FROM demo_table",
            "provider": "nl2sql_template",
            "public_summary": "模板命中，正在执行预定义查询",
            "public_summary_allowed": True,
        }
    )

    event_type, payload = emitted[0]
    business_event = payload["progress"]["business_data_search_event"]
    assert event_type == "tool.progress"
    assert business_event["public_summary"] == "模板命中，正在执行预定义查询"
    assert business_event["public_summary_allowed"] is True
