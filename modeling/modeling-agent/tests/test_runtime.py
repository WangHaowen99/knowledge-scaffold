from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import json

import modeling_agent.tools as tools_module
from modeling_agent.agent import _system_prompt, build_model
from modeling_agent.rails import (
    AskRail,
    ConfirmationRail,
    ReleaseConfirmationRail,
    claims_confirmation_card,
    requests_user_input,
    tool_trace_group_id,
)
from modeling_agent.errors import AgentRuntimeError
from modeling_agent.schemas import RUN_RUNNING, RunRecord
from modeling_agent.service import LiveRun
from modeling_agent.tools import (
    CORE_TOOL_SPECS,
    PROJECT_STATE_TOOL_SPECS,
    CoreTool,
    ProjectStateTool,
    RequestConfirmationTool,
    CandidateValidationRepairState,
    ToolContext,
)

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


pytestmark = pytest.mark.asyncio


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


async def test_model_uses_stream_specific_timeouts(settings) -> None:
    model = build_model(settings)

    assert model.model_client_config.stream_first_chunk_timeout == 120.0
    assert model.model_client_config.stream_idle_timeout == 60.0


async def test_paged_trace_groups_ignore_offsets_but_keep_resource_identity() -> None:
    first = tool_trace_group_id("read_document", {"filename": "a.docx", "offset": 0, "limit": 20_000})
    second = tool_trace_group_id("read_document", {"filename": "a.docx", "offset": 20_000, "limit": 20_000})
    other = tool_trace_group_id("read_document", {"filename": "b.docx", "offset": 0, "limit": 20_000})

    assert first == second
    assert first != other
    assert tool_trace_group_id("read_file", {"path": "rules/example.md"}) == ""


async def test_held_text_is_flushed_when_required_tool_finishes_at_stream_boundary(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    run = runtime.store.create_run(RunRecord(
        id="run_boundary",
        project_id="project_boundary",
        session_id="session_boundary",
        status=RUN_RUNNING,
        prompt="核对资料",
        token_fingerprint="fingerprint",
    ))
    live = LiveRun(
        run_id=run.id,
        project_id=run.project_id,
        token="token_1",
        required_tools={"read_document"},
    )
    live.bundle = SimpleNamespace(agent=object())

    class BoundaryRunner:
        @staticmethod
        async def run_agent_streaming(*_args, **_kwargs):
            yield SimpleNamespace(type="llm_output", payload="已找到相关原文依据。")
            live.observed_tools.add("read_document")

    result = await runtime._run_streaming(live, run, BoundaryRunner, "核对资料")  # noqa: SLF001

    assert result["streamed"] is True
    events = runtime.store.events_after(run.project_id, 0)
    assert "".join(
        event.payload.get("delta", "") for event in events if event.kind == "message_delta"
    ) == "已找到相关原文依据。"
    assert len([event for event in events if event.kind == "message_done"]) == 1


async def test_completed_run_never_ends_without_visible_assistant_output(settings) -> None:
    model = ScriptedModel(
        [[], []],
        final="",
        content_by_call={1: "已核对当前进展，现有处理记录均已保留。下一步可以从当前阶段继续。"},
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_empty_response",
            token="token_1",
            message="告诉我当前情况",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        events = runtime.store.events_after("project_empty_response", 0)
        visible = "".join(
            event.payload.get("delta", "") for event in events if event.kind == "message_delta"
        )
        assert visible == "已核对当前进展，现有处理记录均已保留。下一步可以从当前阶段继续。"
        assert len([event for event in events if event.kind == "message_done"]) == 1
        compact_calls = [
            event for event in events
            if event.kind == "model_call" and event.payload.get("actor") == "compact_summary"
        ]
        assert compact_calls[-1].payload["outcome"] == "content"
        assert compact_calls[-1].payload["toolDefinitions"] == 0
        assert model.calls == 2
    finally:
        await runtime.shutdown()


async def test_empty_compact_summary_marks_run_failed_with_visible_retry_path(settings) -> None:
    model = ScriptedModel([[], []], final="")
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_empty_summary",
            token="token_1",
            message="告诉我当前情况",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "failed"
        events = runtime.store.events_after("project_empty_summary", 0)
        visible = "".join(
            event.payload.get("delta", "") for event in events if event.kind == "message_delta"
        )
        assert "模型没有返回可读摘要" in visible
        assert not any(
            event.kind == "status" and event.payload.get("status") == "completed"
            for event in events
        )
        compact_calls = [
            event for event in events
            if event.kind == "model_call" and event.payload.get("actor") == "compact_summary"
        ]
        assert compact_calls[-1].payload["outcome"] == "empty_content"
    finally:
        await runtime.shutdown()


async def test_reasoning_is_streamed_separately_and_private_segments_are_removed(settings) -> None:
    model = ScriptedModel(
        [[]],
        final="已读完资料，正在核对字段映射。",
        reasoning_by_call={
            0: "先梳理文档结构。\nsystem prompt 与 api_key=top-secret 不应展示。\n再比对字段含义。",
        },
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_reasoning",
            token="token_1",
            message="核对资料",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        events = runtime.store.events_after("project_reasoning", 0)
        reasoning = "".join(
            event.payload.get("delta", "") for event in events if event.kind == "reasoning_delta"
        )
        assert "先梳理文档结构" in reasoning
        assert "再比对字段含义" in reasoning
        assert "内部配置已隐藏" in reasoning
        assert "top-secret" not in reasoning
        assert "system prompt" not in reasoning
        assert len([event for event in events if event.kind == "reasoning_done"]) == 1
        telemetry = [
            event.payload for event in events
            if event.kind == "model_call" and event.payload.get("actor") == "modeling_leader"
        ]
        assert telemetry[-1]["outcome"] == "content"
        assert any(item.get("reasoningChars", 0) > 0 for item in telemetry)
        assert "content" not in telemetry[-1]
        assert "reasoning" not in telemetry[-1]
    finally:
        await runtime.shutdown()


async def test_package_contract_is_injected_into_initial_agent_context() -> None:
    prompt = _system_prompt("建模方法", {
        "validatorVersion": "package-format-v1",
        "manifest": {
            "requiredFields": ["kind", "status"],
            "platformManagedFields": ["kind", "status"],
        },
        "markdown": {
            "details": {
                "operation": {
                    "requiredSections": ["业务目标", "输入", "输出"],
                },
            },
        },
    })

    assert "本轮知识包生成契约（来自核心确定性校验器）" in prompt
    assert '"validatorVersion": "package-format-v1"' in prompt
    assert '"requiredFields"' in prompt
    assert '"platformManagedFields"' in prompt
    assert '"requiredSections"' in prompt
    assert '"业务目标"' in prompt
    assert "生成、修正和确认候选都必须以这份" in prompt
    assert "diagrams/*.mmd 时只提交裸 Mermaid 源码" in prompt
    assert "绝不能生成 ```mermaid / ``` 等 Markdown 代码围栏" in prompt


async def test_candidate_validation_repair_aggregates_parallel_files_as_one_round() -> None:
    state = CandidateValidationRepairState()
    state.begin_round("model-1")

    for index in range(1, 5):
        result = state.record({
            "error": "CANDIDATE_FORMAT_INVALID",
            "validatorVersion": "package-v1",
            "findings": [{"ruleId": "required-section", "path": f"file-{index}.md"}],
        })
        assert result["repairAttempt"] == 1
        assert result["repairRoundId"] == "model-1"
        assert result["repairStopped"] is False

    state.begin_round("model-2")
    assert state.attempts == 1
    assert state.stopped is False
    assert state.last_report["findingCount"] == 4
    assert [item["path"] for item in state.last_report["findings"]] == [
        "file-1.md", "file-2.md", "file-3.md", "file-4.md",
    ]


async def test_candidate_validation_repair_stops_after_three_distinct_model_rounds() -> None:
    state = CandidateValidationRepairState()

    for attempt in range(1, 4):
        state.begin_round(f"model-{attempt}")
        result = state.record({
            "error": "CANDIDATE_FORMAT_INVALID",
            "validatorVersion": "package-v1",
            "findings": [{"ruleId": f"rule-{attempt}", "path": f"file-{attempt}.md"}],
        })
        assert result["repairAttempt"] == attempt
        assert result["repairStopped"] is False

    state.begin_round("model-4")
    assert state.attempts == 3
    assert state.repeated_findings is False
    assert state.stopped is True
    assert state.blocked_result()["repairStopped"] is True


async def test_candidate_validation_repair_stops_on_repeated_round_and_recovery_resets() -> None:
    state = CandidateValidationRepairState()
    report = {
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findings": [{"ruleId": "same-rule", "path": "objects/customer.md"}],
    }

    state.begin_round("model-1")
    state.record(report)
    state.begin_round("model-2")
    state.record(report)
    state.begin_round("model-3")

    assert state.attempts == 2
    assert state.repeated_findings is True
    assert state.stopped is True

    state.record_success("discard_rejected_candidate_changes")

    assert state.attempts == 0
    assert state.stopped is False
    assert state.current_round_id == "model-3"


async def test_tool_context_applies_repair_policy_from_package_contract() -> None:
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        package_contract={
            "confirmation": {
                "maxAutomaticRepairRounds": 4,
                "stopOnRepeatedFindingFingerprint": False,
            },
        },
    )
    report = {
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findings": [{"ruleId": "same-rule", "path": "objects/customer.md"}],
    }

    context.validation_repair.begin_round("model-1")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-2")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-3")

    assert context.validation_repair.max_attempts == 4
    assert context.validation_repair.stop_on_repeated_fingerprint is False
    assert context.validation_repair.repeated_findings is True
    assert context.validation_repair.stopped is False


async def test_quick_file_validation_error_reaches_agent_with_full_findings_and_repair_state() -> None:
    finding = {
        "ruleId": "knowledge-markdown.frontmatter",
        "path": "objects/inventory.md",
        "message": "objects/inventory.md 缺少 YAML frontmatter",
        "suggestion": "补齐本文件 frontmatter",
    }
    gateway = FakeGateway({
        "write_file": {
            "error": "KNOWLEDGE_FILE_INVALID",
            "reason": "候选文件快速校验未通过",
            "retryable": True,
            "validatorVersion": "knowledge-markdown-v1",
            "findingCount": 1,
            "findings": [finding],
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "write_file")
    tool = CoreTool(spec, context)

    result = json.loads(await tool.invoke({
        "path": "objects/inventory.md",
        "content": "# 库存对象",
        "change_group": "inventory",
    }))

    assert result["error"] == "KNOWLEDGE_FILE_INVALID"
    assert result["findings"] == [finding]
    assert result["repairAttempt"] == 1
    assert result["repairRoundId"] == "unscoped"
    assert result["repairStopped"] is False
    assert "完整处理 findings" in result["instruction"]


async def test_parallel_quick_validation_failures_share_one_repair_round() -> None:
    class PathValidationGateway(FakeGateway):
        async def call(self, **kwargs):
            path = str(kwargs["arguments"].get("path") or "")
            return {
                "error": "KNOWLEDGE_FILE_INVALID",
                "reason": f"{path} 缺少必需章节",
                "retryable": True,
                "validatorVersion": "knowledge-markdown-v1",
                "findingCount": 1,
                "findings": [{
                    "ruleId": "object.required_section",
                    "path": path,
                    "message": f"{path} 缺少必需章节",
                    "suggestion": "补齐必需章节",
                }],
            }

    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=PathValidationGateway(),
        emit=lambda _kind, _payload: None,
    )
    context.validation_repair.begin_round("model-parallel")
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "write_file")
    tool = CoreTool(spec, context)
    paths = [
        "objects/customer.md",
        "objects/product.md",
        "objects/ipo.md",
        "objects/company.md",
    ]

    results = await asyncio.gather(*(
        tool.invoke({
            "path": path,
            "content": "# 对象",
            "change_group": "objects",
            "evidence": ["user-decision:test"],
        })
        for path in paths
    ))

    delivered = [json.loads(item) for item in results]
    assert {item["repairAttempt"] for item in delivered} == {1}
    assert {item["repairRoundId"] for item in delivered} == {"model-parallel"}
    assert all(item["repairStopped"] is False for item in delivered)
    context.validation_repair.begin_round("model-repair")
    assert context.validation_repair.attempts == 1
    assert context.validation_repair.stopped is False
    assert context.validation_repair.last_report["findingCount"] == 4
    assert {item["path"] for item in context.validation_repair.last_report["findings"]} == set(paths)


async def test_successful_candidate_discard_resets_stopped_repair_episode() -> None:
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway({
            "discard_rejected_candidate_changes": {
                "ok": True,
                "display": {"text": "已放弃退回的候选"},
            },
        }),
        emit=lambda _kind, _payload: None,
    )
    report = {
        "error": "KNOWLEDGE_FILE_INVALID",
        "retryable": True,
        "validatorVersion": "knowledge-markdown-v1",
        "findings": [{"ruleId": "same", "path": "objects/customer.md"}],
    }
    context.validation_repair.begin_round("model-1")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-2")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-3")
    assert context.validation_repair.stopped is True
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "discard_rejected_candidate_changes")

    result = json.loads(await CoreTool(spec, context).invoke({}))

    assert result["ok"] is True
    assert context.validation_repair.stopped is False
    assert context.validation_repair.attempts == 0


async def test_real_openjiuwen_runner_focus_interrupt_and_resume(settings, caplog) -> None:
    model = ScriptedModel([
        [ToolTurn("focus", {"kind": "flow", "target": "diagrams/procurement.mmd", "node": "D2"})],
        [ToolTurn("ask", {
            "question": "安全库存按什么范围计算？",
            "kind": "business_decision",
            "options": [
                {"label": "按仓库", "description": "每个仓库独立判断", "value": "warehouse"},
                {"label": "按全局", "description": "全部仓库合并判断", "value": "global"},
            ],
        })],
        [],
        [ToolTurn("ask", {
            "question": "下一步先确认哪个流程范围？",
            "kind": "business_decision",
            "options": [
                {"label": "采购流程", "value": "procurement"},
                {"label": "仓储流程", "value": "warehouse"},
            ],
        })],
    ])
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_demo",
            token="token_before",
            message="请根据资料开始建模",
        )
        assert started["sessionId"].endswith(f":run:{started['id']}")
        waiting = await wait_for_status(runtime, started["id"], {"waiting"})
        assert waiting["interactionId"]
        interaction = runtime.store.get_interaction(waiting["interactionId"])
        assert interaction is not None
        assert interaction.payload["allowFreeText"] is True

        focus_events = [event for event in runtime.store.events_after("project_demo", 0) if event.kind == "focus"]
        assert focus_events[0].payload["target"]["node"] == "D2"

        await runtime.answer_interaction(
            project_id="project_demo",
            interaction_id=interaction.id,
            token="token_after",
            answer={"value": "warehouse"},
        )
        next_waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert next_waiting["status"] == "waiting", next_waiting["error"]
        next_interaction = runtime.store.pending_interaction("project_demo")
        assert next_interaction is not None
        assert next_interaction.id != interaction.id
        assert next_interaction.payload["question"] == "下一步先确认哪个流程范围？"
        events = runtime.store.events_after("project_demo", 0)
        deltas = [event.payload["delta"] for event in events if event.kind == "message_delta"]
        assert len(deltas) > 1
        assert "下一步" in "".join(deltas)
        assert len([event for event in events if event.kind == "message_done"]) == 1
        assert runtime.project_state("project_demo")["lastSeq"] == events[-1].seq
        assert not any(
            event.kind == "message" and event.payload.get("role") == "assistant"
            and event.payload.get("blocks", [{}])[0].get("text") == "".join(deltas)
            for event in events
        )
        assert not any("Failed to load description" in record.getMessage() for record in caplog.records)
    finally:
        await runtime.shutdown()


async def test_material_request_adds_oral_later_and_skip_options() -> None:
    rail = AskRail()
    tool_call = type("Call", (), {
        "arguments": json.dumps({
            "question": "请补资料",
            "kind": "material_request",
            "options": [
                {"label": "上传文件", "value": "upload"},
                {"label": "口述背景", "value": "verbal"},
                {"label": "没有资料", "value": "skip"},
            ],
        }, ensure_ascii=False),
    })()
    decision = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]
    assert [item["value"] for item in decision.request.ui_options] == ["oral", "later", "skip"]
    assert [item["label"] for item in decision.request.ui_options] == ["我口述", "让我先找找", "跳过"]
    assert decision.request.ui_options[0]["requiresText"] is True
    assert decision.request.ui_options[0]["textPrompt"] == "请口述需要补充的业务背景或做法"


async def test_ask_option_can_require_follow_up_text() -> None:
    rail = AskRail()
    tool_call = type("Call", (), {
        "arguments": json.dumps({
            "question": "知识内容是否需要调整？",
            "kind": "clarification",
            "options": [{
                "label": "内容需要修改",
                "description": "说明需要修改的位置和内容",
                "value": "content_fix",
                "requiresText": True,
                "textPrompt": "请描述具体需要修改的地方",
            }],
        }, ensure_ascii=False),
    })()

    decision = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]

    option = decision.request.ui_options[0]
    assert option["value"] == "content_fix"
    assert option["requiresText"] is True
    assert option["textPrompt"] == "请描述具体需要修改的地方"
    assert decision.request.payload_schema["properties"]["text"]["minLength"] == 1


async def test_material_parse_event_is_durable_idempotent_and_continues_without_fake_user(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("analyze_material_impact", {"source": "source-new"})],
        [],
    ])
    gateway = FakeGateway({
        "claim_candidate_workspace": {"ok": True},
        "analyze_material_impact": {
            "semanticChanged": True,
            "view": {
                "intact": {"n": 1, "items": ["objects/kept.md"]},
                "todo": {"n": 1, "decide": 0, "items": [{"t": "rules/review.md", "s": "需复核"}]},
                "chains": [],
            },
            "display": {"text": "1 项保持有效，1 项需要复核"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_material_event")
        runtime.store.transition_project_state(
            "project_material_event",
            expected_revision=initial.revision,
            next_state="release:idle",
            cause="test_released",
        )
        event = {
            "eventId": "material:doc-new:terminal",
            "status": "parsed",
            "documentId": "doc-new",
            "sourceKey": "source-new",
            "filename": "补充规程.docx",
            "revisionNo": 1,
            "purpose": "evidence",
            "readingMode": "on_demand",
            "autoContinue": True,
            "semanticChanged": True,
        }

        accepted = await runtime.handle_material_event(
            project_id="project_material_event", token="token_1", event=event,
        )
        active = runtime.store.active_run("project_material_event")
        assert active is not None
        completed = await wait_for_status(runtime, active.id, {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert accepted["continued"] is True
        assert runtime.store.project_state("project_material_event").state == "knowledge:completed"

        events = runtime.store.events_after("project_material_event", 0)
        results = [
            item for item in events
            if item.kind == "message"
            and item.payload.get("blocks", [{}])[0].get("type") == "uploadResult"
        ]
        assert len(results) == 1
        assert results[0].payload["blocks"][0]["details"][0]["documentId"] == "doc-new"
        assert not any(
            item.kind == "message" and item.payload.get("role") == "user"
            for item in events
        )
        assert [call["tool"] for call in gateway.calls].count("analyze_material_impact") == 1

        duplicate = await runtime.handle_material_event(
            project_id="project_material_event", token="token_2", event=event,
        )
        assert duplicate == {"accepted": True, "duplicate": True, "continued": False}
        after_retry = runtime.store.events_after("project_material_event", 0)
        assert len([
            item for item in after_retry
            if item.kind == "message"
            and item.payload.get("blocks", [{}])[0].get("type") == "uploadResult"
        ]) == 1
        run_count = runtime.store._db.execute(  # noqa: SLF001 - idempotence invariant
            "SELECT COUNT(*) AS n FROM agent_runs WHERE project_id = ?",
            ("project_material_event",),
        ).fetchone()["n"]
        assert run_count == 1
    finally:
        await runtime.shutdown()


async def test_material_batch_waits_for_all_terminal_events_and_continues_once(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("analyze_material_impact", {"source": "source-ok"})],
        [],
    ])
    gateway = FakeGateway({
        "claim_candidate_workspace": {"ok": True},
        "analyze_material_impact": {
            "semanticChanged": True,
            "view": {"intact": {"n": 0, "items": []}, "todo": {"n": 1, "items": []}, "chains": []},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        common = {
            "uploadBatchId": "batch-six",
            "uploadBatchSize": 2,
            "purpose": "evidence",
            "readingMode": "on_demand",
            "autoContinue": True,
            "revisionNo": 1,
        }
        first = await runtime.handle_material_event(
            project_id="project_material_batch",
            token="token_1",
            event={
                **common,
                "eventId": "material:doc-ok:terminal",
                "status": "parsed",
                "documentId": "doc-ok",
                "sourceKey": "source-ok",
                "filename": "可读资料.docx",
                "semanticChanged": True,
            },
        )
        assert first["waitingFor"] == 1
        assert runtime.store.active_run("project_material_batch") is None
        assert not any(
            event.kind == "message" and event.payload.get("blocks", [{}])[0].get("type") == "uploadResult"
            for event in runtime.store.events_after("project_material_batch", 0)
        )

        second = await runtime.handle_material_event(
            project_id="project_material_batch",
            token="token_2",
            event={
                **common,
                "eventId": "material:doc-failed:terminal",
                "status": "failed",
                "documentId": "doc-failed",
                "sourceKey": "source-failed",
                "filename": "扫描件.pdf",
                "parseError": "扫描版 PDF 无可读正文",
                "semanticChanged": False,
            },
        )
        active = runtime.store.active_run("project_material_batch")
        assert active is not None
        completed = await wait_for_status(runtime, active.id, {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert second["continued"] is True
        events = runtime.store.events_after("project_material_batch", 0)
        results = [
            event.payload["blocks"][0]
            for event in events
            if event.kind == "message"
            and event.payload.get("blocks", [{}])[0].get("type") == "uploadResult"
        ]
        assert len(results) == 1
        assert results[0]["parsed"] == 1
        assert results[0]["failed"] == 1
        assert [item["name"] for item in results[0]["details"]] == ["可读资料.docx", "扫描件.pdf"]
        run_count = runtime.store._db.execute(  # noqa: SLF001 - batch flood invariant
            "SELECT COUNT(*) AS n FROM agent_runs WHERE project_id = ?",
            ("project_material_batch",),
        ).fetchone()["n"]
        assert run_count == 1
    finally:
        await runtime.shutdown()


async def test_package_import_event_initializes_single_increment_baseline_once(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    await runtime.startup()
    try:
        event = {
            "eventId": "package-import:project_imported:baseline-1",
            "baselineRevision": "baseline-1",
            "packageName": "现有知识包",
            "packageVersion": "1.2.0",
            "sourceFilename": "existing-package.zip",
            "fileCount": 42,
        }
        first = await runtime.handle_package_import_event(
            project_id="project_imported", token="token_1", event=event,
        )
        duplicate = await runtime.handle_package_import_event(
            project_id="project_imported", token="token_2", event=event,
        )

        assert first["initialized"] is True
        assert first["state"]["state"] == "increment:idle"
        assert duplicate["duplicate"] is True
        assert duplicate["initialized"] is False
        assert duplicate["preserved"] is True
        assert runtime.store.project_state("project_imported").state == "increment:idle"
        transitions = runtime.store._db.execute(  # noqa: SLF001 - unique-state invariant
            """SELECT cause, metadata_json FROM project_agent_state_transitions
            WHERE project_id = ? ORDER BY id""",
            ("project_imported",),
        ).fetchall()
        assert len(transitions) == 1
        assert transitions[0]["cause"] == "package_imported"
        assert json.loads(transitions[0]["metadata_json"])["baselineRevision"] == "baseline-1"
        messages = [
            item for item in runtime.store.events_after("project_imported", 0)
            if item.kind == "message" and item.payload.get("role") == "assistant"
        ]
        assert len(messages) == 1
        assert "直接补充或替换资料" in messages[0].payload["blocks"][0]["text"]
        assert runtime.store.active_run("project_imported") is None

        existing = runtime.store.project_state("project_in_progress")
        runtime.store.transition_project_state(
            "project_in_progress",
            expected_revision=existing.revision,
            next_state="goal:idle",
            cause="existing_business_progress",
        )
        preserved = await runtime.handle_package_import_event(
            project_id="project_in_progress",
            token="token_3",
            event={**event, "eventId": "package-import:project_in_progress:baseline-2"},
        )
        assert preserved["initialized"] is False
        assert preserved["preserved"] is True
        assert runtime.store.project_state("project_in_progress").state == "goal:idle"
        assert not runtime.store.events_after("project_in_progress", 0)
    finally:
        await runtime.shutdown()


async def test_studio_review_event_reads_real_diff_and_opens_one_confirmation_without_fake_user(settings) -> None:
    group = {"id": "studio-edit", "tier": "structural", "title": "确认 Studio 的 1 个编辑"}
    model = ScriptedModel([
        [ToolTurn("request_confirmation", {"summary": "请确认库存定义的整体修改"})],
        [ToolTurn("diff", {"rev_b": "candidate"})],
        [ToolTurn("ask", {
            "question": "是否要继续复核？",
            "kind": "business_decision",
            "options": [{"label": "继续", "value": "continue"}],
        })],
        [ToolTurn("request_confirmation", {"summary": "请确认库存定义的整体修改"})],
    ])
    gateway = FakeGateway({
        "claim_candidate_workspace": {"ok": True},
        "diff": {
            "from": "base",
            "to": "candidate",
            "files": [{"path": "objects/inventory.md", "status": "modified"}],
            "display": {"text": "正在对照 1 个变更文件"},
        },
        "prepare_confirmation_groups": {"change_groups": [group]},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-studio",
            "change_groups": [group],
            "auto_approved": False,
            "focus": {"kind": "diff", "target": "objects/inventory.md"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    event = {
        "eventId": "studio-review:project-studio-event:review-1",
        "clientMutationId": "review-1",
        "requestHash": "a" * 64,
        "workingRevision": "sha256:studio-working",
        "changedPaths": ["objects/inventory.md", "objects/index.md"],
        "elementIds": ["object:inventory"],
        "summary": "已形成 1 个编辑的待确认修改，确认后才会保存为新版本",
    }
    try:
        accepted = await runtime.handle_studio_review_event(
            project_id="project-studio-event", token="token-1", event=event,
        )
        active = runtime.store.active_run("project-studio-event")
        assert active is not None
        waiting = await wait_for_status(runtime, active.id, {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        assert accepted == {"accepted": True, "duplicate": False, "continued": True}

        interaction = runtime.store.pending_interaction("project-studio-event")
        assert interaction is not None
        assert interaction.kind == "confirmation"
        assert interaction.payload["changeGroups"] == [group]
        assert interaction.payload["candidateRevision"] == "candidate-studio"

        events = runtime.store.events_after("project-studio-event", 0)
        studio_messages = [
            item for item in events
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]
        assert len(studio_messages) == 1
        assert studio_messages[0].payload["role"] == "system"
        visible = studio_messages[0].payload["blocks"][0]["text"]
        assert "交给助手复核" in visible
        assert "确认前不会改变当前版本" in visible
        assert not any(item.kind == "message" and item.payload.get("role") == "user" for item in events)
        tools = [call["tool"] for call in gateway.calls]
        assert tools.count("diff") == 1
        assert tools.count("request_confirmation_preflight") == 1
        assert tools.count("request_confirmation") == 0
        assert tools.index("diff") < tools.index("prepare_confirmation_groups")

        duplicate = await runtime.handle_studio_review_event(
            project_id="project-studio-event", token="token-2", event=event,
        )
        assert duplicate == {"accepted": True, "duplicate": True, "continued": False}
        assert len([
            item for item in runtime.store.events_after("project-studio-event", 0)
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]) == 1
        run_count = runtime.store._db.execute(  # noqa: SLF001 - event continuation invariant
            "SELECT COUNT(*) AS n FROM agent_runs WHERE project_id = ?",
            ("project-studio-event",),
        ).fetchone()["n"]
        assert run_count == 1
    finally:
        await runtime.shutdown()


async def test_studio_review_event_keeps_existing_confirmation_open_and_pending_for_redelivery(settings) -> None:
    original_group = {"id": "original", "tier": "caliber", "title": "确认原业务口径"}
    model = ScriptedModel([
        [ToolTurn("request_confirmation", {"summary": "请先确认原业务口径"})],
    ])
    gateway = FakeGateway({
        "claim_candidate_workspace": {"ok": True},
        "prepare_confirmation_groups": {"change_groups": [original_group]},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-original",
            "change_groups": [original_group],
            "auto_approved": False,
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project-studio-blocked", token="token-before", message="整理现有业务口径",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        original = runtime.store.pending_interaction("project-studio-blocked")
        assert original is not None and original.kind == "confirmation"

        event = {
            "eventId": "studio-review:project-studio-blocked:review-late",
            "clientMutationId": "review-late",
            "requestHash": "b" * 64,
            "workingRevision": "sha256:studio-late",
            "changedPaths": ["rules/payment.md", "rules/index.md"],
            "elementIds": ["rule:payment"],
            "summary": "已形成规则编辑的待确认修改",
        }
        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.handle_studio_review_event(
                project_id="project-studio-blocked", token="token-after", event=event,
            )
        assert raised.value.code == "STUDIO_EVENT_WAITING_CONFIRMATION"
        assert runtime.store.pending_interaction("project-studio-blocked").id == original.id
        assert runtime.store.external_event_pending(event["eventId"], "project-studio-blocked") is True

        with pytest.raises(AgentRuntimeError):
            await runtime.handle_studio_review_event(
                project_id="project-studio-blocked", token="token-retry", event=event,
            )
        messages = [
            item for item in runtime.store.events_after("project-studio-blocked", 0)
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]
        assert len(messages) == 1
        assert runtime.store.pending_interaction("project-studio-blocked").id == original.id
    finally:
        await runtime.shutdown()


async def test_fusion_review_event_attaches_real_candidate_and_opens_one_confirmation_without_fake_user(settings) -> None:
    group = {"id": "fusion-batch", "tier": "caliber", "title": "确认 3 个来源知识包的一次性融合"}
    model = ScriptedModel([
        [ToolTurn("compile_package_fusion_plan", {"plan_id": "fusion-plan-1"})],
        [ToolTurn("diff", {"rev_b": "candidate"})],
        [ToolTurn("request_confirmation", {"summary": "请确认多来源知识包的整体融合结果"})],
    ])
    gateway = FakeGateway({
        "claim_candidate_workspace": {"ok": True},
        "compile_package_fusion_plan": {
            "candidateRevision": "candidate-fusion",
            "changeGroups": [group],
            "display": {"text": "融合候选已挂接，等待整体确认"},
        },
        "diff": {
            "from": "base",
            "to": "candidate-fusion",
            "files": [{"path": "mappings/package-fusion.json", "status": "added"}],
            "display": {"text": "正在对照整体融合结果"},
        },
        "prepare_confirmation_groups": {"change_groups": [group]},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-fusion",
            "change_groups": [group],
            "auto_approved": False,
            "focus": {"kind": "diff", "target": "mappings/package-fusion.json"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    event = {
        "eventId": "fusion-review:project-fusion-event:fusion-plan-1",
        "planId": "fusion-plan-1",
        "candidateId": "candidate-fusion",
        "outputName": "多来源融合知识包",
        "sourceCount": 3,
        "summary": "已生成多来源融合知识包的整体候选",
    }
    try:
        initial = runtime.store.project_state("project-fusion-event")
        runtime.store.transition_project_state(
            "project-fusion-event",
            expected_revision=initial.revision,
            next_state="knowledge:idle",
            cause="test_fusion_ready",
        )
        accepted = await runtime.handle_fusion_review_event(
            project_id="project-fusion-event", token="token-1", event=event,
        )
        active = runtime.store.active_run("project-fusion-event")
        assert active is not None
        waiting = await wait_for_status(runtime, active.id, {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        assert accepted == {"accepted": True, "duplicate": False, "continued": True}

        interaction = runtime.store.pending_interaction("project-fusion-event")
        assert interaction is not None and interaction.kind == "confirmation"
        assert interaction.payload["changeGroups"] == [group]
        assert interaction.payload["candidateRevision"] == "candidate-fusion"
        events = runtime.store.events_after("project-fusion-event", 0)
        messages = [
            item for item in events
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]
        assert len(messages) == 1
        assert messages[0].payload["role"] == "system"
        assert "3 个来源知识包" in messages[0].payload["blocks"][0]["text"]
        assert "确认前不会改变当前版本" in messages[0].payload["blocks"][0]["text"]
        assert not any(item.kind == "message" and item.payload.get("role") == "user" for item in events)
        called = [call["tool"] for call in gateway.calls]
        assert called.count("compile_package_fusion_plan") == 1
        assert called.count("diff") == 1
        assert called.count("request_confirmation_preflight") == 1
        assert called.index("compile_package_fusion_plan") < called.index("diff")
        assert called.index("diff") < called.index("prepare_confirmation_groups")

        duplicate = await runtime.handle_fusion_review_event(
            project_id="project-fusion-event", token="token-2", event=event,
        )
        assert duplicate == {"accepted": True, "duplicate": True, "continued": False}
        assert len([
            item for item in runtime.store.events_after("project-fusion-event", 0)
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]) == 1
    finally:
        await runtime.shutdown()


async def test_full_read_tool_emits_real_checkpoint_progress(monkeypatch) -> None:
    class SlowFullReadGateway(FakeGateway):
        async def call(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["tool"] == "read_documents_fully":
                await asyncio.sleep(0.04)
                return {"coverage": [{"complete": True}]}
            if kwargs["tool"] == "get_material_analysis_progress":
                return {
                    "title": "正在全文精读《工艺规程.docx》",
                    "detail": "8/12 个分块完成 · 全文覆盖 67%",
                    "stage": "chunk_map",
                    "counters": {"progress": 67, "current": 8, "total": 12, "unit": "个分块"},
                }
            return {"ok": True}

    monkeypatch.setattr(tools_module, "FULL_READ_PROGRESS_INTERVAL_SECONDS", 0.01)
    gateway = SlowFullReadGateway()
    events = []
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "read_documents_fully")
    tool = CoreTool(spec, ToolContext(
        project_id="project_progress",
        run_id="run_progress",
        token="token_progress",
        gateway=gateway,
        emit=lambda kind, payload: events.append((kind, payload)),
    ))

    result = json.loads(await tool.invoke({"filenames": ["工艺规程.docx"]}))

    assert result["coverage"][0]["complete"] is True
    assert events[0][1]["detail"] == "正在建立完整阅读计划"
    checkpoint = next(payload for kind, payload in events if kind == "progress"
        and payload.get("counters", {}).get("progress") == 67)
    assert checkpoint["detail"] == "8/12 个分块完成 · 全文覆盖 67%"
    assert checkpoint["counters"]["current"] == 8
    assert any(call["tool"] == "get_material_analysis_progress" for call in gateway.calls)


async def test_free_input_while_waiting_resumes_as_interjection(settings) -> None:
    original_question = {
        "question": "请选择业务范围",
        "kind": "scope_confirmation",
        "options": [
            {"label": "范围 A", "description": "只覆盖 A", "value": "a"},
            {"label": "范围 B", "description": "覆盖 A 和 B", "value": "b"},
        ],
    }
    model = ScriptedModel([
        [ToolTurn("ask", original_question)],
        [ToolTurn("ask", original_question)],
    ], final="先解释范围切分原因。原问题仍需决定：范围 A 还是范围 B？下一步请选择。")
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_demo", token="token_1", message="开始")
        await wait_for_status(runtime, started["id"], {"waiting"})
        result = await runtime.steer(
            project_id="project_demo",
            token="token_2",
            text="为什么要这么切？",
        )
        assert result["mode"] == "interjection"
        waiting_again = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting_again["status"] == "waiting"
        repeated = runtime.store.pending_interaction("project_demo")
        assert repeated is not None
        assert repeated.payload["question"] == original_question["question"]
        events = runtime.store.events_after("project_demo", 0)
        assert any(event.kind == "message" and event.payload.get("role") == "user" for event in events)
    finally:
        await runtime.shutdown()


async def test_interjection_restores_question_when_model_only_answers(settings) -> None:
    original_question = {
        "question": "补货申请按哪条路径建模？",
        "kind": "scope_confirmation",
        "options": [
            {"label": "常规补货", "description": "按规程口径", "value": "standard"},
            {"label": "保留两条", "description": "记录证据冲突", "value": "both"},
        ],
    }
    model = ScriptedModel(
        [[ToolTurn("ask", original_question)], [], []],
        final="先说明两条路径的证据差异，原问题仍需用户决定。",
    )
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_demo", token="token_1", message="开始")
        await wait_for_status(runtime, started["id"], {"waiting"})
        original = runtime.store.pending_interaction("project_demo")
        assert original is not None

        await runtime.steer(project_id="project_demo", token="token_2", text="为什么有两条路径？")
        waiting_again = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting_again["status"] == "waiting"
        restored = runtime.store.pending_interaction("project_demo")
        assert restored is not None
        assert restored.id != original.id
        assert restored.payload["question"] == original_question["question"]
        assert restored.payload["restoredAfterInterjection"] is True
        original_after_interjection = runtime.store.get_interaction(original.id)
        assert original_after_interjection is not None
        assert original_after_interjection.status == "answered"
        assert original_after_interjection.answer == {"action": "interject", "text": "为什么有两条路径？"}
        pending_count = runtime.store._db.execute(  # noqa: SLF001 - single pending invariant
            "SELECT COUNT(*) AS n FROM agent_interactions WHERE project_id = ? AND status = 'pending'",
            ("project_demo",),
        ).fetchone()["n"]
        assert pending_count == 1

        events = runtime.store.events_after("project_demo", 0)
        answer_done = max(event.seq for event in events if event.kind == "message_done")
        original_closed = next(
            event.seq for event in events
            if event.kind == "interaction" and event.payload.get("id") == original.id
            and event.payload.get("status") == "answered"
        )
        restored_opened = next(
            event.seq for event in events
            if event.kind == "interaction" and event.payload.get("id") == restored.id
            and event.payload.get("status") == "pending"
        )
        assert answer_done < original_closed < restored_opened
        assert not any(
            event.kind == "message"
            and event.payload.get("blocks", [{}])[0].get("type") == "answer"
            and event.payload.get("blocks", [{}])[0].get("interactionId") == original.id
            for event in events
        )

        await runtime.answer_interaction(
            project_id="project_demo",
            interaction_id=restored.id,
            token="token_3",
            answer={"value": "both"},
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_demo") is None
    finally:
        await runtime.shutdown()


async def test_interjection_cannot_mutate_candidate_before_question_is_restored(settings) -> None:
    original_question = {
        "question": "请选择补货申请适用路径",
        "kind": "scope_confirmation",
        "options": [
            {"label": "路径 A", "description": "候选路径 A", "value": "a"},
            {"label": "路径 B", "description": "候选路径 B", "value": "b"},
        ],
    }
    model = ScriptedModel([
        [ToolTurn("ask", original_question)],
        [ToolTurn("delete_file", {
            "path": ".modeling/temporary.md",
            "change_group": "cleanup",
            "reason": "不应在插话回合执行",
        })],
        [],
    ], final="这次只解释两条路径的影响，不改变候选。")
    gateway = FakeGateway()
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_demo", token="token_1", message="开始")
        await wait_for_status(runtime, started["id"], {"waiting"})

        await runtime.steer(project_id="project_demo", token="token_2", text="为什么现在必须选？")
        waiting_again = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting_again["status"] == "waiting"
        assert not any(call["tool"] == "delete_file" for call in gateway.calls)
        restored = runtime.store.pending_interaction("project_demo")
        assert restored is not None
        assert restored.payload["question"] == original_question["question"]
    finally:
        await runtime.shutdown()


async def test_restored_confirmation_accepts_the_same_frozen_candidate(settings) -> None:
    groups = [{"id": "goal", "tier": "caliber", "title": "建模目标确认", "impact": []}]
    model = ScriptedModel(
        [[ToolTurn("request_confirmation", {"summary": "确认建模目标"})], [], []],
        final="目标已经确认，下一步继续建立业务流程。",
    )
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": groups},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate_goal",
            "validation_receipt": {
                "candidateHash": "sha256:goal",
                "validatorVersion": "package-contract-v2",
            },
            "auto_approved": False,
            "change_groups": groups,
            "focus": {"kind": "diff", "target": ".modeling/goal.md"},
        },
        "request_confirmation": {
            "approved": True,
            "candidateRevision": "candidate_goal",
            "revision": "revision_goal",
            "selectedGroups": ["goal"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_confirmation", token="token_1", message="开始")
        await wait_for_status(runtime, started["id"], {"waiting"})
        original = runtime.store.pending_interaction("project_confirmation")
        assert original is not None
        assert original.payload["candidateRevision"] == "candidate_goal"

        await runtime.steer(project_id="project_confirmation", token="token_2", text="为什么先确认目标？")
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting"
        restored = runtime.store.pending_interaction("project_confirmation")
        assert restored is not None
        assert restored.payload["candidateRevision"] == original.payload["candidateRevision"]

        await runtime.answer_interaction(
            project_id="project_confirmation",
            interaction_id=restored.id,
            token="token_3",
            answer={"approved": True, "review_mode": "full", "selected_groups": ["goal"], "sampled_groups": []},
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_confirmation") is None
        confirmations = [call for call in gateway.calls if call["tool"] == "request_confirmation"]
        assert len(confirmations) == 1
        assert confirmations[0]["arguments"]["expected_candidate_revision"] == "candidate_goal"
        assert confirmations[0]["arguments"]["expected_candidate_hash"] == "sha256:goal"
        assert confirmations[0]["arguments"]["expected_validator_version"] == "package-contract-v2"
        assert confirmations[0]["arguments"]["review"]["approved"] is True
        assert [call["tool"] for call in gateway.calls].count("prepare_confirmation_groups") == 1
        assert [call["tool"] for call in gateway.calls].count("request_confirmation_preflight") == 1
    finally:
        await runtime.shutdown()


async def test_legacy_confirmation_without_receipt_is_retired_and_revalidated_before_new_card(settings) -> None:
    groups = [{"id": "knowledge", "tier": "structural", "title": "确认知识修改", "impact": []}]
    model = ScriptedModel([
        [ToolTurn("request_confirmation", {"summary": "确认旧候选"})],
        [ToolTurn("request_confirmation", {"summary": "重新确认已校验候选"})],
    ])
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": groups},
        "request_confirmation": {
            "error": "CANDIDATE_REVALIDATION_REQUIRED",
            "reason": "待确认候选或校验规则已经变化，原确认不能继续提交",
            "retryable": False,
            "next": ["重新执行完整候选预检，并基于新的确认卡再次决定"],
        },
    })
    original_call = gateway.call
    preflight_count = 0

    async def changing_preflight(**kwargs):
        nonlocal preflight_count
        if kwargs["tool"] == "request_confirmation_preflight":
            gateway.calls.append({
                "projectId": kwargs["project_id"],
                "runId": kwargs["run_id"],
                "token": kwargs["token"],
                "tool": kwargs["tool"],
                "arguments": kwargs["arguments"],
            })
            preflight_count += 1
            return {
                "candidate_revision": "candidate-legacy",
                "auto_approved": False,
                "change_groups": groups,
                **({
                    "validation_receipt": {
                        "candidateHash": "sha256:revalidated",
                        "validatorVersion": "package-contract-v2",
                    },
                } if preflight_count > 1 else {}),
            }
        return await original_call(**kwargs)

    gateway.call = changing_preflight  # type: ignore[method-assign]
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_legacy_confirmation",
            token="token_1",
            message="继续处理旧候选",
        )
        await wait_for_status(runtime, started["id"], {"waiting"})
        old_interaction = runtime.store.pending_interaction("project_legacy_confirmation")
        assert old_interaction is not None
        assert old_interaction.payload.get("validationReceipt") is None

        await runtime.answer_interaction(
            project_id="project_legacy_confirmation",
            interaction_id=old_interaction.id,
            token="token_2",
            answer={"approved": True, "review_mode": "full", "selected_groups": ["knowledge"]},
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        assert runtime.store.get_interaction(old_interaction.id).status == "answered"
        new_interaction = runtime.store.pending_interaction("project_legacy_confirmation")
        assert new_interaction is not None and new_interaction.id != old_interaction.id
        assert new_interaction.payload["validationReceipt"] == {
            "candidateHash": "sha256:revalidated",
            "validatorVersion": "package-contract-v2",
        }
        assert preflight_count == 2
        system_text = "\n".join(
            block.get("text", "")
            for event in runtime.store.events_after("project_legacy_confirmation", 0)
            if event.kind == "message" and event.payload.get("role") == "system"
            for block in event.payload.get("blocks", [])
        )
        assert "接受操作未生效" in system_text
        assert "需要再次确认" in system_text
    finally:
        await runtime.shutdown()


async def test_normal_confirmation_applies_candidate_and_autonomously_reaches_next_question(settings) -> None:
    groups = [{"id": "knowledge", "tier": "structural", "title": "确认知识修改", "impact": []}]
    model = ScriptedModel(
        [
            [ToolTurn("request_confirmation", {"summary": "确认知识修改"})],
            [],
            [ToolTurn("ask", {
                "question": "下一步先核对哪个业务范围？",
                "kind": "business_decision",
                "options": [
                    {"label": "装置范围", "value": "unit_scope"},
                    {"label": "物料范围", "value": "material_scope"},
                ],
            })],
        ],
        final="修改已经确认，我会继续核对下一步。",
    )
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": groups},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-knowledge",
            "validation_receipt": {
                "candidateHash": "sha256:knowledge",
                "validatorVersion": "package-contract-v2",
            },
            "auto_approved": False,
            "change_groups": groups,
        },
        "request_confirmation": {
            "approved": True,
            "candidateRevision": "candidate-knowledge",
            "revision": "revision-knowledge",
            "selectedGroups": ["knowledge"],
            "display": {"text": "已确认 1 组修改"},
        },
    })
    candidate_status = "pending"
    original_call = gateway.call

    async def stateful_call(**kwargs):
        nonlocal candidate_status
        result = await original_call(**kwargs)
        if kwargs["tool"] == "request_confirmation" and not result.get("error"):
            candidate_status = "applied"
        return result

    gateway.call = stateful_call  # type: ignore[method-assign]
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_normal_confirmation",
            token="token_1",
            message="请整理知识修改并让我确认",
        )
        await wait_for_status(runtime, started["id"], {"waiting"})
        interaction = runtime.store.pending_interaction("project_normal_confirmation")
        assert interaction is not None

        await runtime.answer_interaction(
            project_id="project_normal_confirmation",
            interaction_id=interaction.id,
            token="token_2",
            answer={
                "approved": True,
                "review_mode": "full",
                "selected_groups": ["knowledge"],
                "sampled_groups": [],
            },
        )
        next_waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert next_waiting["status"] == "waiting", next_waiting["error"]
        assert candidate_status == "applied"
        confirmations = [call for call in gateway.calls if call["tool"] == "request_confirmation"]
        assert len(confirmations) == 1
        assert confirmations[0]["arguments"]["expected_candidate_revision"] == "candidate-knowledge"
        assert confirmations[0]["arguments"]["expected_candidate_hash"] == "sha256:knowledge"
        assert confirmations[0]["arguments"]["expected_validator_version"] == "package-contract-v2"
        assert confirmations[0]["arguments"]["review"]["approved"] is True
        next_interaction = runtime.store.pending_interaction("project_normal_confirmation")
        assert next_interaction is not None and next_interaction.kind == "business_decision"
        assert next_interaction.payload["question"] == "下一步先核对哪个业务范围？"
        assert model.calls == 3
    finally:
        await runtime.shutdown()


async def test_normal_confirmation_core_failure_keeps_interaction_pending(settings) -> None:
    groups = [{"id": "knowledge", "tier": "structural", "title": "确认知识修改", "impact": []}]
    model = ScriptedModel(
        [[ToolTurn("request_confirmation", {"summary": "确认知识修改"})], []],
        final="修改已经确认，我会继续核对下一步。",
    )
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": groups},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-knowledge",
            "auto_approved": False,
            "change_groups": groups,
        },
        "request_confirmation": {
            "error": "CANDIDATE_APPLY_FAILED",
            "reason": "候选写入失败",
            "next": ["保持候选并重试"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_confirmation_failure",
            token="token_1",
            message="请整理知识修改并让我确认",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting"})
        interaction = runtime.store.pending_interaction("project_confirmation_failure")
        assert interaction is not None

        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.answer_interaction(
                project_id="project_confirmation_failure",
                interaction_id=interaction.id,
                token="token_2",
                answer={
                    "approved": True,
                    "review_mode": "full",
                    "selected_groups": ["knowledge"],
                    "sampled_groups": [],
                },
            )

        assert raised.value.code == "CANDIDATE_APPLY_FAILED"
        assert runtime.store.require_run(started["id"]).status == waiting["status"] == "waiting"
        pending = runtime.store.pending_interaction("project_confirmation_failure")
        assert pending is not None and pending.id == interaction.id
        assert pending.payload["lastSubmissionError"] == "候选写入失败"
        assert model.calls == 1
        assert [call["tool"] for call in gateway.calls].count("request_confirmation") == 1
        assert not any(
            event.kind == "interaction" and event.payload.get("status") == "answered"
            for event in runtime.store.events_after("project_confirmation_failure", 0)
        )
        assert any(
            event.kind == "interaction"
            and event.payload.get("id") == interaction.id
            and event.payload.get("payload", {}).get("lastSubmissionError") == "候选写入失败"
            for event in runtime.store.events_after("project_confirmation_failure", 0)
        )
    finally:
        await runtime.shutdown()


async def test_failed_required_tool_counts_as_attempt_and_is_not_repeated(settings) -> None:
    model = ScriptedModel(
        [[ToolTurn("run_evaluation", {})], []],
        final="评测条件尚未满足；请先补齐阻断项。",
    )
    gateway = FakeGateway({
        "run_evaluation": {
            "error": "GATE_UNMET",
            "reason": "还有 2 项评测条件未满足",
            "next": ["补齐真实用例", "检查数据源"],
            "display": {"text": "评测条件尚未满足"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_evaluation_gate_failure",
            token="token_1",
            message="请立即运行评测",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert [call["tool"] for call in gateway.calls].count("run_evaluation") == 1
        tool_events = [
            event.payload for event in runtime.store.events_after("project_evaluation_gate_failure", 0)
            if event.kind == "tool" and event.payload.get("name") == "run_evaluation"
        ]
        assert [event["phase"] for event in tool_events] == ["started", "failed"]
        assert "没有完成这项明确要求" not in completed["result"]
    finally:
        await runtime.shutdown()


async def test_agent_reads_and_transitions_single_project_state(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("get_project_state")],
        [ToolTurn("transition_project_state", {
            "to_phase": "goal",
            "expected_revision": 2,
            "intent": "资料已就绪，开始确认目标",
            "summary": "已取得一份可读资料，正在形成目标候选。",
            "evidence": ["material:业务说明.md"],
            "open_questions": ["目标范围待确认"],
        })],
        [],
    ])
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_state", token="token_1", message="开始")
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        state = runtime.store.project_state("project_state")
        assert state.state == "goal:completed"
        assert state.summary == "已取得一份可读资料，正在形成目标候选。"
        assert state.open_questions == ["目标范围待确认"]
        assert any(call["tool"] == "gate_status" for call in gateway.calls)
    finally:
        await runtime.shutdown()


async def test_release_is_terminal_for_current_autonomous_run(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("transition_project_state", {
            "to_phase": "release",
            "expected_revision": 3,
            "intent": "全量评测已通过并完成正式发布",
            "summary": "知识包 1.0.0 已发布交付。",
            "evidence": ["evaluation:5/5"],
            "open_questions": ["后续迭代再处理 A1"],
        })],
        [],
    ], final="发布交付完成。")
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_release_terminal")
        runtime.store.transition_project_state(
            "project_release_terminal",
            expected_revision=initial.revision,
            next_state="evaluation:completed",
            cause="test_setup",
        )
        started = await runtime.start_run(
            project_id="project_release_terminal",
            token="token_1",
            message="确认发布 1.0.0",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.project_state("project_release_terminal").state == "release:completed"
        assert model.calls == 2
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize(
    ("message", "expected_phase"),
    [
        ("查看当前交付状态", "release"),
        ("启动 v1.1.0 增量迭代，处理开放问题", "knowledge"),
    ],
)
async def test_release_reentry_requires_explicit_user_iteration(settings, message, expected_phase) -> None:
    model = ScriptedModel([
        [ToolTurn("transition_project_state", {
            "to_phase": "knowledge",
            "expected_revision": 3,
            "intent": "开始下一版知识修订",
            "summary": "准备处理下一版知识变更。",
            "evidence": [],
            "open_questions": [],
        })],
        [],
    ])
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state(f"project_reentry_{expected_phase}")
        runtime.store.transition_project_state(
            f"project_reentry_{expected_phase}",
            expected_revision=initial.revision,
            next_state="release:completed",
            cause="test_setup",
        )
        started = await runtime.start_run(
            project_id=f"project_reentry_{expected_phase}",
            token="token_1",
            message=message,
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        phase = runtime.store.project_state(f"project_reentry_{expected_phase}").state.partition(":")[0]
        assert phase == expected_phase
    finally:
        await runtime.shutdown()


async def test_context_only_update_does_not_extend_autonomous_run(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("update_project_context", {
            "expected_revision": 2,
            "summary": "没有新的业务变更，只同步已有结论。",
            "evidence": [],
            "open_questions": [],
        })],
        [],
    ])
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_context_only",
            token="token_1",
            message="查看当前状态",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert model.calls == 2
        assert runtime.store.project_state("project_context_only").summary == "没有新的业务变更，只同步已有结论。"
    finally:
        await runtime.shutdown()


async def test_explicit_initial_work_request_reaches_question_without_continue_message(settings) -> None:
    model = ScriptedModel([
        [],
        [ToolTurn("ask", {
            "question": "下一步先确定哪个建模范围？",
            "kind": "business_decision",
            "options": [
                {"label": "装置范围", "value": "unit_scope"},
                {"label": "物料范围", "value": "material_scope"},
            ],
        })],
    ], final="资料已就绪，我正在梳理建模范围。")
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_initial_autonomous",
            token="token_1",
            message="请开始这个项目的建模",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_initial_autonomous")
        assert interaction is not None
        assert interaction.payload["question"] == "下一步先确定哪个建模范围？"
        assert model.calls == 2
    finally:
        await runtime.shutdown()


async def test_answering_business_question_autonomously_reaches_next_question(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("ask", {
            "question": "先采用哪个业务口径？",
            "kind": "business_decision",
            "options": [
                {"label": "生产口径", "value": "production"},
                {"label": "财务口径", "value": "finance"},
            ],
        })],
        [],
        [ToolTurn("ask", {
            "question": "接下来先核对哪个数据范围？",
            "kind": "business_decision",
            "options": [
                {"label": "当前数据", "value": "current"},
                {"label": "历史数据", "value": "history"},
            ],
        })],
    ], final="口径已记录，我会继续推进下一步。")
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_answer_autonomous",
            token="token_1",
            message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"waiting"})
        original = runtime.store.pending_interaction("project_answer_autonomous")
        assert original is not None

        await runtime.answer_interaction(
            project_id="project_answer_autonomous",
            interaction_id=original.id,
            token="token_2",
            answer={"value": "production"},
        )
        next_waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert next_waiting["status"] == "waiting", next_waiting["error"]
        next_interaction = runtime.store.pending_interaction("project_answer_autonomous")
        assert next_interaction is not None
        assert next_interaction.id != original.id
        assert next_interaction.payload["question"] == "接下来先核对哪个数据范围？"
        assert model.calls == 3
    finally:
        await runtime.shutdown()


async def test_agent_continues_after_progress_until_next_interaction(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("get_project_state")],
        [ToolTurn("transition_project_state", {
            "to_phase": "goal",
            "expected_revision": 2,
            "intent": "资料已就绪，开始目标建模",
            "summary": "已进入目标建模，继续形成候选。",
            "evidence": ["material:业务说明.md"],
            "open_questions": [],
        })],
        [],
        [ToolTurn("get_project_state")],
        [ToolTurn("write_file", {
            "path": ".modeling/goal.md",
            "content": "# 目标\n\n范围依据 material:doc_business#范围说明。",
            "change_group": "goal",
            "reason": "形成目标候选",
            "evidence": ["material:doc_business#范围说明"],
        })],
        [ToolTurn("ask", {
            "question": "目标范围是否包含历史数据？",
            "kind": "scope_confirmation",
            "options": [
                {"label": "包含", "description": "覆盖历史数据", "value": "yes"},
                {"label": "不包含", "description": "只覆盖当前数据", "value": "no"},
            ],
        })],
    ])
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(project_id="project_autonomous", token="token_1", message="开始建模")
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        assert any(call["tool"] == "write_file" for call in gateway.calls)
        interaction = runtime.store.pending_interaction("project_autonomous")
        assert interaction is not None
        assert interaction.payload["question"] == "目标范围是否包含历史数据？"
        assert model.calls >= 6
    finally:
        await runtime.shutdown()


async def test_discarded_rejected_changes_is_a_guarded_autonomous_progress_tool(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("discard_rejected_candidate_changes")],
        [],
        [ToolTurn("ask", {
            "question": "保持已确认版本后，下一步先处理哪项业务？",
            "kind": "business_decision",
            "options": [
                {"label": "继续当前阶段", "value": "continue"},
                {"label": "先补充资料", "value": "material"},
            ],
        })],
    ], final="已放弃退回的修改，继续当前业务阶段。")
    gateway = FakeGateway({
        "discard_rejected_candidate_changes": {
            "discarded": True,
            "discardedCount": 2,
            "discardedPaths": ["rules/obsolete.md", "objects/obsolete.md"],
            "display": {"text": "已放弃 2 项退回修改，当前已确认版本保持不变"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_discard_rejected_changes",
            token="token_1",
            message="这批退回修改不需要了，保持已确认版本",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        assert [call["tool"] for call in gateway.calls].count("discard_rejected_candidate_changes") == 1
        interaction = runtime.store.pending_interaction("project_discard_rejected_changes")
        assert interaction is not None
        assert interaction.payload["question"] == "保持已确认版本后，下一步先处理哪项业务？"
        spec = next(item for item in CORE_TOOL_SPECS if item.name == "discard_rejected_candidate_changes")
        assert spec.input_schema == {"type": "object", "properties": {}, "additionalProperties": False}
        assert spec.parallel_safe is False
        events = runtime.store.events_after("project_discard_rejected_changes", 0)
        completed = [
            event for event in events
            if event.kind == "tool"
            and event.payload.get("name") == "discard_rejected_candidate_changes"
            and event.payload.get("phase") == "completed"
        ]
        assert len(completed) == 1
        assert completed[0].payload["summary"] == "已放弃 2 项退回修改，当前已确认版本保持不变"
    finally:
        await runtime.shutdown()


async def test_explicit_evaluation_retry_cannot_be_satisfied_by_text_only(settings) -> None:
    model = ScriptedModel(
        [
            [],
            [ToolTurn("run_evaluation")],
            [],
        ],
        final="已重新运行评测，评测轮次 rd_secret-123 已完成，基于修订 588743e。",
    )
    gateway = FakeGateway({
        "run_evaluation": {
            "roundId": "rd_real",
            "status": "done",
            "passed": 5,
            "total": 5,
            "display": {"text": "评测完成：5/5 通过"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_evaluation_proof",
            token="token_1",
            message="平台已经修复，请立即重新运行原 5 条评测用例。",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert [call["tool"] for call in gateway.calls].count("run_evaluation") == 1
        events = runtime.store.events_after("project_evaluation_proof", 0)
        tool_started = next(
            event for event in events
            if event.kind == "tool"
            and event.payload.get("name") == "run_evaluation"
            and event.payload.get("phase") == "started"
        )
        visible_deltas = [event for event in events if event.kind == "message_delta"]
        assert visible_deltas
        assert all(event.seq > tool_started.seq for event in visible_deltas)
        visible_text = "".join(
            event.payload.get("delta", "") for event in visible_deltas
        )
        assert "已重新运行评测" in visible_text
        assert "rd_secret-123" not in visible_text
        assert "588743e" not in visible_text
        assert "本次评测" in visible_text
        assert "当前版本" in visible_text
        assert model.calls >= 3
    finally:
        await runtime.shutdown()


async def test_full_read_intent_requires_directive_and_verified_full_read_tools(settings) -> None:
    model = ScriptedModel(
        [
            [],
            [ToolTurn("set_material_directive", {
                "filename": "工艺规程.docx", "reading_mode": "full",
            })],
            [ToolTurn("read_documents_fully", {"filenames": ["工艺规程.docx"]})],
            [],
        ],
        final="已完整阅读《工艺规程》，20 万字符均已覆盖。",
    )
    gateway = FakeGateway({
        "set_material_directive": {"ok": True, "display": {"text": "已保存资料设置"}},
        "read_documents_fully": {
            "coverage": [{"filename": "工艺规程.docx", "coveredChars": 200_000, "totalChars": 200_000, "complete": True}],
            "display": {"text": "全文精读完成，完整度 100%"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        state = runtime.store.project_state("project_full_read")
        runtime.store.transition_project_state(
            "project_full_read",
            expected_revision=state.revision,
            next_state="release:idle",
            cause="test_released_baseline",
        )
        started = await runtime.start_run(
            project_id="project_full_read",
            token="token_1",
            message="请全文精读《工艺规程.docx》，完成后主动继续。",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert [call["tool"] for call in gateway.calls].count("set_material_directive") == 1
        assert [call["tool"] for call in gateway.calls].count("read_documents_fully") == 1
        assert runtime.project_state("project_full_read")["state"]["phase"] == "knowledge"
        visible = "".join(
            event.payload.get("delta", "")
            for event in runtime.store.events_after("project_full_read", 0)
            if event.kind == "message_delta"
        )
        assert "20 万字符均已覆盖" in visible
        assert model.calls >= 4
    finally:
        await runtime.shutdown()


async def test_fusion_continue_reads_and_reuses_existing_plan_without_forcing_create(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("list_package_fusion_plans", {})],
            [ToolTurn("list_package_sources", {})],
            [],
        ],
        final="已找到现有的 140/140 融合方案，将从这个方案继续。",
    )
    gateway = FakeGateway({
        "list_package_fusion_plans": {
            "plans": [{"id": "fusion-ready", "status": "ready", "unresolvedCount": 0}],
            "display": {"text": "已找到 1 个多来源融合计划"},
        },
        "list_package_sources": {
            "sources": [{"id": "source-a"}, {"id": "source-b"}],
            "display": {"text": "已找到 2 个只读来源包"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_fusion_resume",
            token="token_1",
            message="请继续多个来源知识包的一次性融合。",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        called = [call["tool"] for call in gateway.calls]
        assert called.count("list_package_fusion_plans") == 1
        assert called.count("list_package_sources") == 1
        assert "create_package_fusion_plan" not in called
    finally:
        await runtime.shutdown()


async def test_failed_full_read_tool_does_not_satisfy_completion_proof(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("set_material_directive", {
                "filename": "工艺规程.docx", "reading_mode": "full",
            })],
            [ToolTurn("read_documents_fully", {"filenames": ["工艺规程.docx"]})],
            [],
            [],
        ],
        final="已经读完。",
    )
    gateway = FakeGateway({
        "set_material_directive": {"ok": True, "display": {"text": "已保存资料设置"}},
        "read_documents_fully": {"error": "FULL_READ_INCOMPLETE", "reason": "仍有分段未完成"},
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_failed_full_read",
            token="token_1",
            message="请全文精读《工艺规程.docx》。",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "failed"
        assert [call["tool"] for call in gateway.calls].count("read_documents_fully") >= 1
        visible = "".join(
            event.payload.get("delta", "")
            for event in runtime.store.events_after("project_failed_full_read", 0)
            if event.kind == "message_delta"
        )
        assert "已经读完" not in visible
    finally:
        await runtime.shutdown()


async def test_confirmation_interjection_keeps_frozen_candidate() -> None:
    gateway = FakeGateway()
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = ConfirmationRail(context)
    tool_call = type("Call", (), {
        "arguments": '{"summary":"确认库存规则","change_groups":[{"id":"stock","tier":"caliber","title":"库存口径"}]}',
    })()

    decision = await rail.resolve_interrupt(
        None,  # type: ignore[arg-type]
        tool_call,
        {"action": "interject", "text": "为什么这样分组？"},
    )

    result = json.loads(decision.tool_result)
    assert result["interjection"] == "为什么这样分组？"
    assert "最终自然语言回答" in result["instruction"]
    assert "不要再次调用 request_confirmation" in result["instruction"]
    assert gateway.calls == []


async def test_confirmation_preflight_focuses_frozen_business_diff() -> None:
    events: list[tuple[str, dict]] = []
    gateway = FakeGateway({
        "prepare_confirmation_groups": {
            "change_groups": [{"id": "regroup_caliber_1", "tier": "caliber", "title": "建模目标确认"}],
        },
        "request_confirmation_preflight": {
            "candidate_revision": "candidate_goal",
            "validation_receipt": {
                "candidateHash": "sha256:goal",
                "validatorVersion": "package-contract-v2",
            },
            "auto_approved": False,
            "change_groups": [{"id": "regroup_caliber_1", "tier": "caliber", "title": "建模目标确认"}],
            "focus": {"kind": "diff", "target": ".modeling/goal.md"},
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    rail = ConfirmationRail(context)
    tool_call = type("Call", (), {
        "arguments": '{"summary":"确认目标"}',
    })()

    decision = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]

    assert [call["tool"] for call in gateway.calls] == ["prepare_confirmation_groups", "request_confirmation_preflight"]
    assert gateway.calls[1]["arguments"]["change_groups"][0]["id"] == "regroup_caliber_1"
    assert decision.request.metadata["candidateRevision"] == "candidate_goal"
    assert decision.request.metadata["validationReceipt"] == {
        "candidateHash": "sha256:goal",
        "validatorVersion": "package-contract-v2",
    }
    assert events == [
        ("focus", {"target": {"kind": "diff", "target": ".modeling/goal.md"}}),
    ]


async def test_confirmation_preflight_delivers_detailed_findings_to_agent_tool_result() -> None:
    finding = {
        "ruleId": "manifest.required.status",
        "path": "manifest.yaml",
        "message": "manifest 缺少必选字段 status",
        "suggestion": "以当前正式 manifest.yaml 为基线补回 status",
    }
    gateway = FakeGateway({
        "prepare_confirmation_groups": {
            "change_groups": [{"id": "indexes", "tier": "index", "title": "知识索引同步"}],
        },
        "request_confirmation_preflight": {
            "error": "CANDIDATE_FORMAT_INVALID",
            "reason": "候选格式校验未通过",
            "retryable": True,
            "validatorVersion": "package-format-v1",
            "findingCount": 1,
            "findings": [finding],
            "next": ["逐项修正后重新预检"],
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = ConfirmationRail(context)
    tool_call = type("Call", (), {"arguments": '{"summary":"确认知识索引"}'})()

    decision = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]

    delivered = json.loads(decision.tool_result)
    assert delivered["error"] == "CANDIDATE_FORMAT_INVALID"
    assert delivered["retryable"] is True
    assert delivered["validatorVersion"] == "package-format-v1"
    assert delivered["findingCount"] == 1
    assert delivered["findings"] == [finding]
    assert delivered["next"] == ["逐项修正后重新预检"]
    assert delivered["repairAttempt"] == 1
    assert delivered["repairStopped"] is False
    assert delivered["findingFingerprint"].startswith("sha256:")
    assert "完整处理 findings" in delivered["instruction"]

    context.validation_repair.begin_round("model-2")
    repeated = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]
    repeated_result = json.loads(repeated.tool_result)
    assert repeated_result["findings"] == [finding]
    assert repeated_result["repairAttempt"] == 2
    assert repeated_result["repairRoundId"] == "model-2"
    assert repeated_result["repairStopped"] is False

    context.validation_repair.begin_round("model-3")
    blocked = context.validation_repair.blocked_result()
    assert blocked["findings"] == [finding]
    assert blocked["repairAttempt"] == 2
    assert blocked["repeatedFinding"] is True
    assert blocked["repairStopped"] is True
    assert blocked["retryable"] is False
    assert "自动修正已停止" in blocked["instruction"]


async def test_text_choice_request_rejects_confirmation_and_opens_ask_card(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("request_confirmation", {"summary": "请确认目标范围"})],
            [ToolTurn("ask", {
                "question": "本次建模采用哪一种目标范围？",
                "kind": "business_decision",
                "options": [
                    {"label": "仅当前装置", "description": "先覆盖当前装置", "value": "current"},
                    {"label": "同类装置", "description": "同时覆盖同类装置", "value": "similar"},
                    {"label": "全厂范围", "description": "覆盖全厂流程", "value": "all"},
                ],
            })],
            [ToolTurn("request_confirmation", {"summary": "请确认已选定的目标范围"})],
        ],
        tool_content_by_call={
            0: "我整理成 A、B、C 三个互斥方案供您选择，请选择后继续。",
        },
    )
    gateway = FakeGateway({
        "prepare_confirmation_groups": {
            "change_groups": [{"id": "goal", "tier": "caliber", "title": "目标范围"}],
        },
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-goal",
            "auto_approved": False,
            "change_groups": [{"id": "goal", "tier": "caliber", "title": "目标范围"}],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_decision_confirmation_conflict",
            token="token_1",
            message="请继续整理建模目标",
            confirmation_required=True,
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_decision_confirmation_conflict")
        assert interaction is not None
        assert interaction.kind == "business_decision"
        assert interaction.payload["allowFreeText"] is True
        assert interaction.payload["overallConfirmationRequired"] is True
        assert interaction.payload["question"] == "本次建模采用哪一种目标范围？"
        assert not any(call["tool"] == "prepare_confirmation_groups" for call in gateway.calls)

        deltas = [
            event.payload["delta"]
            for event in runtime.store.events_after("project_decision_confirmation_conflict", 0)
            if event.kind == "message_delta"
        ]
        assert len(deltas) > 1
        assert "供您选择" in "".join(deltas)

        await runtime.answer_interaction(
            project_id="project_decision_confirmation_conflict",
            interaction_id=interaction.id,
            token="token_2",
            answer={"value": "current"},
        )
        resumed = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert resumed["status"] == "waiting", resumed["error"]
        confirmation = runtime.store.pending_interaction("project_decision_confirmation_conflict")
        assert confirmation is not None and confirmation.kind == "confirmation"
        assert [call["tool"] for call in gateway.calls].count("prepare_confirmation_groups") == 1
    finally:
        await runtime.shutdown()


async def test_text_only_choice_request_is_retried_until_ask_card(settings) -> None:
    model = ScriptedModel(
        [
            [],
            [ToolTurn("ask", {
                "question": "流程边界采用哪一种口径？",
                "kind": "business_decision",
                "options": [
                    {"label": "装置内", "value": "unit"},
                    {"label": "跨装置", "value": "cross_unit"},
                ],
            })],
        ],
        final="以下两个选项供您选择：装置内、跨装置。请选择一个。",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_text_only_decision",
            token="token_1",
            message="请确定流程边界",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_text_only_decision")
        assert interaction is not None and interaction.kind == "business_decision"
        assert model.calls == 2
    finally:
        await runtime.shutdown()


async def test_text_only_description_request_is_retried_until_input_card(settings) -> None:
    prompt = (
        "您选择了‘内容需要修改’。请告诉我具体哪些地方需要调整，"
        "我会据此修改后重新提交。您可以直接描述需要改的地方，我来修改。"
    )
    assert requests_user_input(prompt)
    assert not requests_user_input("内容完成后，可以在这里查看修改说明。")
    model = ScriptedModel(
        [
            [],
            [ToolTurn("ask", {
                "question": "请描述具体需要修改的地方",
                "kind": "clarification",
            })],
        ],
        final=prompt,
    )
    gateway = FakeGateway()
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_text_only_description",
            token="token_1",
            message="继续核对知识内容",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_text_only_description")
        assert interaction is not None and interaction.kind == "clarification"
        assert interaction.payload["question"] == "请描述具体需要修改的地方"
        assert model.calls == 2
        assert not any(call["tool"] == "transition_project_state" for call in gateway.calls)
    finally:
        await runtime.shutdown()


async def test_required_option_text_must_be_present_before_resume(settings) -> None:
    model = ScriptedModel(
        [[ToolTurn("ask", {
            "question": "知识内容是否需要调整？",
            "kind": "clarification",
            "options": [{
                "label": "内容需要修改",
                "description": "说明需要修改的位置和内容",
                "value": "content_fix",
                "requiresText": True,
                "textPrompt": "请描述具体需要修改的地方",
            }],
        })]],
        final="修改说明已收到。",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_required_option_text",
            token="token_1",
            message="核对知识内容",
        )
        await wait_for_status(runtime, started["id"], {"waiting"})
        interaction = runtime.store.pending_interaction("project_required_option_text")
        assert interaction is not None

        with pytest.raises(AgentRuntimeError) as captured:
            await runtime.answer_interaction(
                project_id="project_required_option_text",
                interaction_id=interaction.id,
                token="token_2",
                answer={"value": "content_fix", "selected": ["content_fix"]},
            )
        assert captured.value.code == "INTERACTION_TEXT_REQUIRED"
        assert runtime.store.pending_interaction("project_required_option_text").id == interaction.id
        assert runtime.store.require_run(started["id"]).status == "waiting"
        assert model.calls == 1

        await runtime.answer_interaction(
            project_id="project_required_option_text",
            interaction_id=interaction.id,
            token="token_2",
            answer={
                "value": "content_fix",
                "selected": ["content_fix"],
                "text": "将客户分类从三级调整为四级。",
            },
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_required_option_text") is None
    finally:
        await runtime.shutdown()


async def test_text_only_choice_request_stops_after_bounded_card_retries(settings) -> None:
    model = ScriptedModel([], final="请从方案 A 和方案 B 中选择一个。")
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_missing_decision_card",
            token="token_1",
            message="请确定业务方案",
        )
        failed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert failed["status"] == "failed"
        assert "可操作卡片" in failed["error"]
        assert runtime.store.pending_interaction("project_missing_decision_card") is None
        assert model.calls == 3
    finally:
        await runtime.shutdown()


async def test_optional_follow_up_suggestions_do_not_require_ask_card_during_bounded_progress(settings) -> None:
    model = ScriptedModel(
        [],
        final="后续可考虑补充历史样本或扩展同类装置；我会先继续核对已有资料。",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_optional_suggestions",
            token="token_1",
            message="先核对已有资料",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_optional_suggestions") is None
        assert model.calls == 2
    finally:
        await runtime.shutdown()


async def test_choice_text_with_ask_card_is_not_retried(settings) -> None:
    model = ScriptedModel(
        [[ToolTurn("ask", {
            "question": "请选择库存统计范围",
            "kind": "business_decision",
            "options": [
                {"label": "按仓库", "value": "warehouse"},
                {"label": "按全局", "value": "global"},
            ],
        })]],
        tool_content_by_call={0: "我会把互斥口径放在下面的问题卡中供您选择。"},
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_decision_card_present",
            token="token_1",
            message="确定库存统计范围",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        assert runtime.store.pending_interaction("project_decision_card_present") is not None
        assert model.calls == 1
    finally:
        await runtime.shutdown()


async def test_text_claimed_confirmation_card_is_retried_until_real_card(settings) -> None:
    assert claims_confirmation_card("流程图已在中栏展示，确认卡已生成，等待您确认。")
    assert not claims_confirmation_card("流程确认后我会继续知识建模。")
    model = ScriptedModel(
        [
            [],
            [ToolTurn("request_confirmation", {"summary": "确认产品问答核心流程"})],
        ],
        final="流程图已在中栏展示，确认卡已生成，等待您确认后继续。",
    )
    gateway = FakeGateway({
        "prepare_confirmation_groups": {
            "change_groups": [{"id": "process", "tier": "caliber", "title": "业务流程确认"}],
        },
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-process",
            "auto_approved": False,
            "change_groups": [{"id": "process", "tier": "caliber", "title": "业务流程确认"}],
            "focus": {"kind": "diff", "target": "diagrams/product_qa_flow.mmd"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_claimed_confirmation",
            token="token_1",
            message="继续完成产品问答流程建模",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})

        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_claimed_confirmation")
        assert interaction is not None and interaction.kind == "confirmation"
        assert interaction.payload["changeGroups"][0]["title"] == "业务流程确认"
        assert model.calls == 2
    finally:
        await runtime.shutdown()


async def test_confirmation_tool_only_exposes_business_summary_to_model() -> None:
    schema = RequestConfirmationTool.SPEC.input_schema
    assert schema["required"] == ["summary"]
    assert set(schema["properties"]) == {"summary"}


async def test_interrupt_rail_validation_errors_always_include_structured_findings() -> None:
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        confirmation_required=True,
    )
    ask_call = type("Call", (), {"arguments": '{"question":"还要补充吗"}'})()
    ask_result = json.loads((
        await AskRail(context).resolve_interrupt(None, ask_call, None)  # type: ignore[arg-type]
    ).tool_result)

    assert ask_result["error"] == "OVERALL_CONFIRMATION_REQUIRED"
    assert ask_result["validatorVersion"] == "interaction-boundary-v1"
    assert ask_result["repairScope"] == "interaction"
    assert ask_result["findingCount"] == 1
    assert ask_result["findings"][0]["ruleId"] == "interaction.overall_confirmation.required"

    context.confirmation_required = False
    confirmation_call = type("Call", (), {"arguments": '{"summary":"确认候选"}'})()
    confirmation_result = json.loads((
        await ConfirmationRail(context).resolve_interrupt(None, confirmation_call, None)  # type: ignore[arg-type]
    ).tool_result)

    assert confirmation_result["error"] == "CONFIRMATION_GROUPS_UNAVAILABLE"
    assert confirmation_result["validatorVersion"] == "confirmation-groups-v1"
    assert confirmation_result["findingCount"] == 1
    assert confirmation_result["findings"][0]["path"] == "candidate"


async def test_release_preflight_blocker_is_structured_and_not_auto_retried() -> None:
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway({
            "release_preflight": {
                "ready": False,
                "reason": "真实用例尚未覆盖发布范围",
            },
        }),
        emit=lambda _kind, _payload: None,
    )
    tool_call = type("Call", (), {"arguments": '{"summary":"确认正式发布"}'})()

    result = json.loads((
        await ReleaseConfirmationRail(context).resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]
    ).tool_result)

    assert result["error"] == "RELEASE_PREFLIGHT_BLOCKED"
    assert result["retryable"] is False
    assert result["tracePhase"] == "blocked"
    assert result["repairScope"] == "release"
    assert result["findings"][0]["ruleId"] == "release.preflight.blocked"


async def test_release_confirmation_uses_core_preflight_and_focuses_evaluation() -> None:
    events: list[tuple[str, dict]] = []
    gateway = FakeGateway({
        "release_preflight": {
            "ready": True,
            "version": "1.0.0",
            "testcase_count": 5,
            "questions": ["真实问题一", "真实问题二"],
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    rail = ReleaseConfirmationRail(context)
    tool_call = type("Call", (), {"arguments": '{"summary":"确认正式发布"}'})()

    decision = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]

    assert gateway.calls[0]["tool"] == "release_preflight"
    assert events == [("focus", {"target": {"kind": "evaluation", "target": "正式评测"}})]
    assert decision.request.metadata["kind"] == "release_confirmation"
    assert decision.request.metadata["version"] == "1.0.0"
    assert decision.request.metadata["testcaseCount"] == 5


async def test_declined_release_cannot_reopen_confirmation_in_same_run() -> None:
    gateway = FakeGateway({
        "release_preflight": {
            "ready": True,
            "version": "1.0.0",
            "testcase_count": 5,
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = ReleaseConfirmationRail(context)
    tool_call = type("Call", (), {"arguments": '{"summary":"确认正式发布"}'})()

    declined = await rail.resolve_interrupt(
        None,  # type: ignore[arg-type]
        tool_call,
        {"approved": False, "coverage_confirmed": False, "feedback": "继续补充用例"},
    )
    repeated = await rail.resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]

    assert json.loads(declined.tool_result)["approved"] is False
    repeated_result = json.loads(repeated.tool_result)
    assert repeated_result["release_declined"] is True
    assert "不得在本轮再次发起" in repeated_result["instruction"]
    assert gateway.calls == []


async def test_state_revision_conflict_is_returned_for_agent_retry() -> None:
    async def stale_state(_name: str, _arguments: dict) -> dict:
        raise AgentRuntimeError(
            "STATE_REVISION_CONFLICT",
            "项目 Agent 状态已经变化，请重新读取",
            status=409,
            details={"expectedRevision": 2, "currentRevision": 3},
        )

    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        state_action=stale_state,
    )
    spec = next(item for item in PROJECT_STATE_TOOL_SPECS if item.name == "transition_project_state")
    result = json.loads(await ProjectStateTool(spec, context).invoke({"expected_revision": 2}))

    assert result["code"] == "STATE_REVISION_CONFLICT"
    assert result["currentRevision"] == 3
    assert "get_project_state" in result["instruction"]
