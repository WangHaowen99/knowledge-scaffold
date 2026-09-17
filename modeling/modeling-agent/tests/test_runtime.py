# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import json

import modeling_agent.tools as tools_module
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from modeling_agent.agent import _system_prompt, build_model, context_engine_config, output_budget_for_phase
from modeling_agent.rails import (
    AskRail,
    ConfirmationRail,
    PhaseTransitionRail,
    ReleaseConfirmationRail,
    claims_confirmation_card,
    requests_user_input,
    tool_trace_group_id,
)
from modeling_agent.errors import AgentRuntimeError
from modeling_agent.schemas import InteractionRecord, RUN_RUNNING, RUN_SUSPENDED, RunRecord
from modeling_agent.service import (
    AgentService,
    APPROVED_PLAN_REQUIRED_TOOLS,
    LiveRun,
    PURPOSE_REVIEW_ALLOWED_TOOLS,
    _absorb_plan_progress,
    _expert_answer_intent_text,
    _required_tools_for_user_intent,
    _unsatisfiable_required_tools,
)
from modeling_agent.tools import (
    CORE_TOOL_SPECS,
    PROJECT_STATE_TOOL_SPECS,
    CoreTool,
    ProjectStateTool,
    RequestConfirmationTool,
    CandidateValidationRepairState,
    EVALUATION_PENDING_MESSAGE,
    ToolContext,
    core_tool_specs_for_phase,
)

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


pytestmark = pytest.mark.asyncio


async def test_goal_section_schema_exposes_section_specific_data_contract() -> None:
    specs = {spec.name: spec for spec in CORE_TOOL_SPECS}
    data_schema = specs["put_goal_section"].properties["data"]
    variants = data_schema["oneOf"]
    assert all("summary" not in variant["properties"] for variant in variants)
    objective = next(variant for variant in variants if "success_criteria" in variant["properties"])
    assert objective["properties"]["success_criteria"] == tools_module.STRING
    assert objective["required"] == ["problem", "target_users", "success_criteria", "evidence"]
    assert objective["additionalProperties"] is False
    item_sections = [variant for variant in variants if set(variant["properties"]) == {"items", "evidence"}]
    assert item_sections
    assert all(variant["properties"]["items"]["items"] == tools_module.STRING for variant in item_sections)


async def test_explicit_resume_plan_id_is_claimed_and_injected_into_runtime_prompt(settings) -> None:
    resumed_plan = {
        "id": "plan_resume_demo",
        "goal": "完成尚未提取的规则",
        "status": "active",
        "approval_status": "approved",
        "version": 4,
        "remaining_count": 2,
        "tasks": [
            {"id": "task-a", "title": "信号匹配规则", "status": "pending", "required": True},
            {"id": "task-b", "title": "客户分层规则", "status": "pending", "required": True},
        ],
    }
    model = ScriptedModel(
        [[ToolTurn("get_change_plan", {"plan_id": "plan_resume_demo"})], []],
        final="已读取原计划，等待继续执行。",
    )
    gateway = FakeGateway({
        "claim_candidate_workspace": {
            "resumedPlan": resumed_plan,
            "packageContract": {
                "validatorVersion": "package-format-test-v1",
                "manifest": {"requiredFields": [], "platformManagedFields": []},
            },
        },
        "get_change_plan": {
            "plan": resumed_plan,
            "display": {"text": "建模计划还有 2 项必选任务待完成"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_resume_plan",
            token="token_1",
            message="继续页面选择的计划",
            resume_plan_id="plan_resume_demo",
        )
        settled = await wait_for_status(runtime, started["id"], {"waiting"})

        claim = next(call for call in gateway.calls if call["tool"] == "claim_candidate_workspace")
        assert claim["arguments"] == {"resume_plan_id": "plan_resume_demo"}
        runtime_prompt = str(model.stream_inputs[0])
        assert "页面显式恢复的暂停计划" in runtime_prompt
        assert "plan_resume_demo" in runtime_prompt
        assert "信号匹配规则" in runtime_prompt
        assert "计划完成前不得" in runtime_prompt
        assert any(call["tool"] == "get_change_plan" for call in gateway.calls)
        assert settled["status"] == "waiting"
        interaction = runtime.store.pending_interaction("project_resume_plan")
        assert interaction is not None
        assert set(interaction.payload["missingRequiredTools"]) == APPROVED_PLAN_REQUIRED_TOOLS
    finally:
        await runtime.shutdown()


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


class _CapturingScriptedModel(ScriptedModel):
    def __init__(self, script, **kwargs):
        super().__init__(script, **kwargs)
        self.seen_messages = []

    async def stream(self, *args, **kwargs):
        if args:
            self.seen_messages.append(args[0])
        async for chunk in super().stream(*args, **kwargs):
            yield chunk


async def test_core_tool_surface_is_filtered_by_business_phase() -> None:
    knowledge = {spec.name for spec in core_tool_specs_for_phase("knowledge")}
    validation = {spec.name for spec in core_tool_specs_for_phase("validation")}
    increment = {spec.name for spec in core_tool_specs_for_phase("increment")}

    assert "suggest_object_relations" not in knowledge | validation | increment
    assert "review_object_relations" in knowledge
    assert {
        "list_domain_resources", "stage_change_set", "sync_schema_objects",
        "patch_object_source_binding",
    } <= knowledge
    assert not {"write_file", "delete_file", "patch_object", "create_metric", "upsert_object"} & knowledge
    assert "update_work_notes" in knowledge and "update_plan" not in knowledge
    assert "run_evaluation" not in knowledge
    assert "create_package_fusion_plan" not in knowledge
    assert "patch_rule" not in validation
    assert {
        "list_domain_resources", "get_domain_resource", "define_script_contract",
        "get_product_ledger", "get_schema_semantics",
        "analyze_deterministic_capabilities", "confirm_deterministic_scope",
        "get_script_edit_context", "stage_script_patch", "run_script_test",
        "run_evaluation", "get_evaluation_results", "cancel_evaluation",
    } <= validation
    assert not {"analyze_deterministic_capabilities", "confirm_deterministic_scope"} & knowledge
    assert "stage_script_source" not in validation
    assert {
        "stage_change_set", "sync_schema_objects", "patch_object_source_binding",
        "analyze_material_impact", "compile_package_fusion_plan",
    } <= increment
    assert not {"patch_operation", "upsert_operation", "bind_object_source"} & increment
    legacy: set[str] = set()
    for spec in core_tool_specs_for_phase("knowledge", tool_profile="legacy-v1"):
        legacy.add(spec.name)
    assert {"write_file", "patch_object", "create_metric", "upsert_object", "bind_object_source"} <= legacy
    assert "update_plan" in legacy
    intake_recovery: set[str] = set()
    legacy_validation = {
        spec.name for spec in core_tool_specs_for_phase("validation", tool_profile="legacy-v1")
    }
    assert "stage_script_source" in legacy_validation
    required_tools = frozenset({"run_evaluation", "compile_package_fusion_plan"})
    for spec in core_tool_specs_for_phase("intake", required_tools):
        intake_recovery.add(spec.name)
    assert {"run_evaluation", "compile_package_fusion_plan"} <= intake_recovery


async def test_script_tool_schema_preserves_boolean_action_and_forbids_test_bypasses() -> None:
    specs = {spec.name: spec for spec in CORE_TOOL_SPECS}
    parameter_schema = specs["define_script_contract"].properties["parameters"]["items"]
    assert parameter_schema["properties"]["action"]["enum"] == ["store_true", "store_false"]

    case_schema = specs["run_script_test"].properties["cases"]["items"]
    patch_schema = specs["stage_script_patch"].properties
    assert "source_code" not in patch_schema
    assert patch_schema["edits"]["items"]["required"] == [
        "kind", "symbol", "expected_node_hash", "source",
    ]

    assert case_schema["required"] == ["input", "expected"]
    assert case_schema["additionalProperties"] is False
    assert "argv" not in case_schema["properties"]
    assert "function" not in case_schema["properties"]


async def test_schema_semantic_tool_uses_dedicated_field_update_contract() -> None:
    specs = {spec.name: spec for spec in CORE_TOOL_SPECS}
    actions = specs["stage_schema_semantic_changes"].properties[
        "objects"
    ]["items"]["properties"]["actions"]
    assert actions["maxItems"] == 500

    variants = actions["items"]["oneOf"]
    update = next(
        variant for variant in variants
        if variant["properties"]["op"]["enum"] == ["update_field"]
    )
    # #32：字段级乐观锁改为可选——补空白语义时逐字段带锁会因为一次成功写入
    # 让剩余字段哈希整批作废，批量被迫退化为逐个提交。对象级
    # expected_resource_hash 仍然必填，丢失更新的防护没有削弱。
    assert update["required"] == ["op", "field_key", "changes"]
    assert set(update["properties"]) == {
        "op", "field_key", "expected_field_hash", "changes",
    }
    assert update["additionalProperties"] is False
    assert update["properties"]["changes"] == {
        "type": "object",
        "properties": {
            "cn_name": tools_module.STRING,
            "note": tools_module.STRING,
            "alias": tools_module.STRING,
            "required": {"type": "boolean"},
        },
        "minProperties": 1,
        "additionalProperties": False,
    }
    assert "deferred_warnings" not in specs["confirm_schema_semantics"].properties


async def test_phase_transition_forces_model_boundary_before_using_stale_tools() -> None:
    context = ToolContext(
        project_id="project_phase_refresh",
        run_id="run_phase_refresh",
        token="token",
        gateway=FakeGateway({}),
        emit=lambda *_args: None,
        phase="process",
    )
    rail = PhaseTransitionRail(context)

    class Callback:
        def __init__(self, result: dict) -> None:
            self.inputs = SimpleNamespace(
                tool_name="transition_project_state",
                tool_result=json.dumps(result),
            )
            self.finish = None

        def request_force_finish(self, result: dict) -> None:
            self.finish = result

    callback = Callback({"phase": "knowledge", "revision": 6})
    await rail.after_tool_call(callback)  # type: ignore[arg-type]

    assert context.phase == "knowledge"
    assert callback.finish == {
        "result_type": "answer",
        "output": "",
        "phase_refresh": {"from": "process", "to": "knowledge"},
    }

    failed = Callback({"error": "STATE_GATE_BLOCKED", "phase": "validation"})
    await rail.after_tool_call(failed)  # type: ignore[arg-type]
    assert failed.finish is None
    assert context.phase == "knowledge"


async def test_evaluation_cancel_requires_explicit_user_intent() -> None:
    assert "cancel_evaluation" in _required_tools_for_user_intent("请立即停止当前评测")
    assert "run_evaluation" not in _required_tools_for_user_intent("请立即停止当前评测")
    assert "cancel_evaluation" not in _required_tools_for_user_intent("现在是否具备停止评测的能力？")

    spec = next(item for item in CORE_TOOL_SPECS if item.name == "cancel_evaluation")
    blocked_gateway = FakeGateway({"cancel_evaluation": {"cancelled": True}})
    blocked = json.loads(await CoreTool(spec, ToolContext(
        project_id="project_cancel_guard",
        run_id="run_cancel_guard",
        token="token_1",
        gateway=blocked_gateway,
        emit=lambda _kind, _payload: None,
    )).invoke({"round_id": "round_demo"}))
    assert blocked["error"] == "EVALUATION_CANCEL_NOT_REQUESTED"
    assert blocked_gateway.calls == []

    allowed_gateway = FakeGateway({"cancel_evaluation": {
        "cancelled": True, "roundId": "round_demo", "status": "failed",
    }})
    allowed = json.loads(await CoreTool(spec, ToolContext(
        project_id="project_cancel_guard",
        run_id="run_cancel_guard",
        token="token_1",
        gateway=allowed_gateway,
        emit=lambda _kind, _payload: None,
        evaluation_cancel_authorized=True,
    )).invoke({"round_id": "round_demo"}))
    assert allowed["cancelled"] is True
    assert [call["tool"] for call in allowed_gateway.calls] == ["cancel_evaluation"]


async def test_named_document_rule_request_requires_read_impact_and_plan() -> None:
    required = _required_tools_for_user_intent(
        "请读取《安全管理办法.docx》，根据内容新增一条业务规则",
    )
    assert {"read_document", "create_change_plan"} <= required
    assert "analyze_material_impact" not in required
    assert _required_tools_for_user_intent("请新增一条业务规则") == set()
    assert _required_tools_for_user_intent(
        "是否可以读取《安全管理办法.docx》并新增规则？",
    ) == set()


async def test_upsert_object_relation_name_is_model_generated_chinese_text() -> None:
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "upsert_object")
    relation = spec.input_schema["properties"]["relations"]["items"]["properties"]["relation"]

    assert relation["type"] == "string"
    assert "enum" not in relation
    assert "模型根据材料证据生成" in relation["description"]
    assert "中文谓词" in relation["description"]
    assert "1:N" in relation["description"]
    assert "不得包含源对象或目标对象" in relation["description"]
    assert "连读" in relation["description"]


async def test_model_uses_stream_specific_timeouts(settings) -> None:
    model = build_model(settings)

    assert model.model_client_config.stream_first_chunk_timeout == 120.0
    assert model.model_client_config.stream_idle_timeout == 60.0

    context = context_engine_config(settings)
    assert context.max_context_message_num == 64
    assert context.default_window_message_num == 48
    assert context.default_window_round_num == 10
    assert context.enable_reload is False


async def test_model_output_budget_is_phase_specific(settings) -> None:
    assert output_budget_for_phase(settings, "goal") == 8_192
    assert output_budget_for_phase(settings, "process") == 8_192
    assert settings.analyst_output_tokens == 8_192
    assert output_budget_for_phase(settings, "knowledge") == 8_192
    assert output_budget_for_phase(settings, "validation") == 8_192
    model = build_model(settings, output_tokens=1_024)
    assert model.model_config.max_tokens == 1_024


async def test_model_passes_thinking_request_config(settings) -> None:
    runtime_settings = settings.with_runtime_llm({
        "baseUrl": "https://agent.example.com/v1",
        "model": "reasoning-model",
        "apiKey": "synthetic-agent-key",
        "maxTokens": 4096,
        "thinkingMode": "enabled",
        "reasoningEffort": "high",
        "extraBodyJson": '{"parallel_tool_calls": false}',
    })
    model = build_model(runtime_settings)

    assert model.model_config.model_name == "reasoning-model"
    assert model.model_config.model_extra["reasoning_effort"] == "high"
    assert model.model_config.model_extra["extra_body"] == {
        "parallel_tool_calls": False,
        "thinking": {"type": "enabled"},
    }


async def test_model_timeout_uses_one_persisted_recovery_and_new_execution_brief(settings) -> None:
    class TimeoutThenSuccessModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([[], []], final="已从保存的计划继续完成。")
            self.failures = 1

        async def stream(self, *args, **kwargs):  # type: ignore[override]
            if self.failures:
                self.failures -= 1
                self.calls += 1
                raise TimeoutError("stream first chunk timeout")
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    model = TimeoutThenSuccessModel()
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_model_timeout_recovery",
            token="token_1",
            message="继续核对当前建模计划",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        ledger = runtime.store.retry_ledger(started["id"])
        assert len(ledger) == 1
        assert ledger[0]["category"] == "model_timeout"
        assert ledger[0]["attempt"] == 1
        assert ledger[0]["status"] == "succeeded"
        assert completed["sessionId"] != started["sessionId"]
        recovery = []
        for event in runtime.store.events_after("project_model_timeout_recovery", 0):
            if event.kind == "recovery":
                recovery.append(event.payload)
        assert [event["status"] for event in recovery] == ["retrying", "succeeded"]
    finally:
        await runtime.shutdown()


async def test_second_model_timeout_exhausts_application_recovery(settings) -> None:
    class AlwaysTimeoutModel(ScriptedModel):
        async def stream(self, *_args, **_kwargs):  # type: ignore[override]
            self.calls += 1
            if False:
                yield None
            raise TimeoutError("stream idle timeout")

    model = AlwaysTimeoutModel([[]])
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_model_timeout_exhausted",
            token="token_1",
            message="继续完成当前工作",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "failed"
        ledger = runtime.store.retry_ledger(started["id"])
        assert len(ledger) == 1
        assert ledger[0]["attempt"] == 1
        assert ledger[0]["status"] == "exhausted"
        assert model.calls == 2
        recovery = []
        for event in runtime.store.events_after("project_model_timeout_exhausted", 0):
            if event.kind == "recovery" and event.payload.get("status") == "retrying":
                recovery.append(event)
        assert len(recovery) == 1
    finally:
        await runtime.shutdown()


async def test_context_limit_is_recovered_in_a_fresh_bounded_session(settings) -> None:
    class ContextLimitThenSuccessModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__([[], []], final="已根据有界摘要继续。")
            self.failures = 1

        async def stream(self, *args, **kwargs):  # type: ignore[override]
            if self.failures:
                self.failures -= 1
                self.calls += 1
                raise ValueError("maximum context length exceeded")
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    runtime = service(settings, ContextLimitThenSuccessModel())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_context_limit_recovery",
            token="token_1",
            message="继续处理长资料中的剩余任务",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        ledger = runtime.store.retry_ledger(started["id"])
        assert len(ledger) == 1
        assert ledger[0]["category"] == "context_limit"
        assert ledger[0]["status"] == "succeeded"
        transitions = runtime.store._db.execute(  # noqa: SLF001 - checkpoint audit invariant
            """SELECT cause FROM project_agent_state_transitions
            WHERE project_id = ? ORDER BY id""",
            ("project_context_limit_recovery",),
        ).fetchall()
        assert any(row["cause"] == "execution_session_rotated" for row in transitions)
        assert runtime.store.project_state(
            "project_context_limit_recovery",
        ).checkpoint_id == completed["sessionId"]
    finally:
        await runtime.shutdown()


async def test_interrupted_stream_marks_partial_message_incomplete(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    run = runtime.store.create_run(RunRecord(
        id="run_partial_timeout",
        project_id="project_partial_timeout",
        session_id="session_partial_timeout",
        status=RUN_RUNNING,
        prompt="核对当前计划",
        token_fingerprint="fingerprint",
    ))
    live = LiveRun(run_id=run.id, project_id=run.project_id, token="token_1")
    live.bundle = SimpleNamespace(agent=object())

    class PartialTimeoutRunner:
        @staticmethod
        async def run_agent_streaming(*_args, **_kwargs):
            yield SimpleNamespace(type="llm_output", payload="这是尚未完成的半段回复")
            raise TimeoutError("stream idle timeout")

    with pytest.raises(TimeoutError):
        await runtime._run_streaming(  # noqa: SLF001
            live, run, PartialTimeoutRunner, "核对当前计划",
        )
    events = runtime.store.events_after(run.project_id, 0)
    done = [event for event in events if event.kind == "message_done"]
    assert len(done) == 1
    assert done[0].payload["incomplete"] is True


async def test_full_document_read_declares_long_running_timeout() -> None:
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "read_documents_fully")
    tool = CoreTool(spec, ToolContext(
        project_id="project_timeout",
        run_id="run_timeout",
        token="token",
        gateway=FakeGateway({}),
        emit=lambda *_args: None,
    ))

    assert tool.card.properties["resilience"]["timeout_s"] == 1830.0
    assert AbilityManager._resolve_call_timeout(tool.card) == 1830.0


async def test_change_set_tools_explain_one_task_per_resource_contract() -> None:
    create_plan = next(item for item in CORE_TOOL_SPECS if item.name == "create_change_plan")
    stage_change_set = next(item for item in CORE_TOOL_SPECS if item.name == "stage_change_set")

    assert "每项任务只能对应一个具体目标资源" in create_plan.description
    assert "一个且仅一个目标资源" in create_plan.properties["tasks"]["items"]["properties"]["target_key"]["description"]
    assert "每个 action 只对应一个计划任务和一个目标资源" in stage_change_set.description
    assert stage_change_set.properties["actions"]["maxItems"] == 4
    variants = stage_change_set.properties["actions"]["items"]["oneOf"]
    by_tool = {variant["properties"]["tool"]["enum"][0]: variant for variant in variants}
    assert set(by_tool) == {
        "create_object", "create_rule", "create_operation", "create_metric",
        "patch_object", "patch_rule", "patch_operation", "patch_metric",
    }
    object_fields = by_tool["create_object"]["properties"]["arguments"]["properties"]["fields"]
    assert "cn_name" in object_fields["items"]["properties"]
    assert "name" not in object_fields["items"]["properties"]
    patch_actions = by_tool["patch_object"]["properties"]["arguments"]["properties"]["actions"]
    assert "update_field" in patch_actions["items"]["properties"]["op"]["enum"]
    assert "changes" in patch_actions["items"]["properties"]


async def test_paged_trace_groups_ignore_offsets_but_keep_resource_identity() -> None:
    first = tool_trace_group_id("read_document", {"filename": "a.docx", "offset": 0, "limit": 20_000})
    second = tool_trace_group_id("read_document", {"filename": "a.docx", "offset": 20_000, "limit": 20_000})
    other = tool_trace_group_id("read_document", {"filename": "b.docx", "offset": 0, "limit": 20_000})

    assert first == second
    assert first != other
    assert tool_trace_group_id("read_file", {"path": "rules/example.md"}) == ""
    latest_results = tool_trace_group_id("get_evaluation_results", {})
    specific_results = tool_trace_group_id("get_evaluation_results", {"round_id": "round-2"})
    assert latest_results.startswith("snapshot:get_evaluation_results:")
    assert latest_results != specific_results


async def test_query_eval_db_description_is_mysql_mirror_diagnostic() -> None:
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "query_eval_db")
    prompt = _system_prompt("", {})

    assert "MySQL 评测库镜像" in spec.description
    assert "项目数据源配置" in spec.description
    assert "Neo4j" in spec.description
    assert "NL2SQL 服务内部管理" in spec.description
    assert "query_eval_db 只用于诊断" in prompt
    assert "get_project_state 返回的 dataSource 摘要" in prompt
    assert "provider 表示项目真实评测数据源的技术类型" in prompt
    assert "default_provider_alias/provider_alias 只是知识包内部逻辑数据源别名" in prompt


async def test_project_state_includes_core_data_source_summary(settings) -> None:
    gateway = FakeGateway({
        "project_data_source_status": {
            "configured": True,
            "mode": "connection",
            "provider": "neo4j",
            "providerMeaning": "project_evaluation_data_source_type",
            "workspaceLinked": True,
            "ontologyLinked": True,
            "neo4j": {
                "workspaceIsolationConfigured": True,
                "ontologyIsolationConfigured": True,
                "graphDatabaseConfigured": True,
            },
        },
        "gate_status": {"pass": True, "blockers": []},
    })
    runtime = service(settings, ScriptedModel([]), gateway)
    await runtime.startup()
    try:
        state = await runtime._state_for_agent(LiveRun(  # noqa: SLF001
            run_id="run_data_source_state",
            project_id="project_data_source_state",
            token="token_1",
        ))

        assert state["dataSource"]["provider"] == "neo4j"
        assert state["dataSource"]["providerMeaning"] == "project_evaluation_data_source_type"
        assert state["dataSource"]["neo4j"]["workspaceIsolationConfigured"] is True
        assert any(call["tool"] == "project_data_source_status" for call in gateway.calls)
    finally:
        await runtime.shutdown()


async def test_repeated_completed_evaluation_read_calls_core_once_and_emits_one_trace(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("get_evaluation_results", {})],
            [ToolTurn("get_evaluation_results", {})],
            [],
        ],
        final="已根据本轮结果完成归因。",
    )
    gateway = FakeGateway({
        "get_evaluation_results": {
            "round": {"id": "round-1", "status": "done", "total": 7, "passed": 4},
            "counts": {"pass": 4, "fail": 3},
            "results": [{"testcaseId": "case-1", "verdict": "fail"}],
            "display": {"text": "已读取 7 条真实评测结果"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_evaluation_result_cache",
            token="token_1",
            message="归因本轮评测失败",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        calls = [call for call in gateway.calls if call["tool"] == "get_evaluation_results"]
        assert len(calls) == 1
        trace = []
        for event in runtime.store.events_after("project_evaluation_result_cache", 0):
            if event.kind != "tool":
                continue
            if event.payload.get("name") != "get_evaluation_results":
                continue
            trace.append(event.payload)
        assert [event["phase"] for event in trace] == ["started", "completed"]
    finally:
        await runtime.shutdown()


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


async def test_scope_confirmation_empty_response_without_artifact_keeps_latest_interaction_and_fails(settings) -> None:
    question = {
        "question": "是否保留 optional_route 为可选输入？专家调整：缺失时不得补零或推断，应返回输入不足、值为空并给出可追溯原因。",
        "kind": "scope_confirmation",
        "options": [
            {"label": "保留可选输入", "description": "按专家意见调整", "value": "adjust"},
            {"label": "本轮不生成", "description": "不生成脚本", "value": "skip"},
        ],
        "operation": "demo_operation_batch_parse",
    }
    model = ScriptedModel(
        [[ToolTurn("ask", question)], []],
        final="",
    )
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_scope_empty_response",
            token="token_1",
            message="开始确定性脚本建模",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting"})
        interaction = runtime.store.get_interaction(waiting["interactionId"])
        assert interaction is not None

        await runtime.answer_interaction(
            project_id="project_scope_empty_response",
            interaction_id=interaction.id,
            token="token_2",
            answer={"value": "adjust", "text": "保留 optional_route 为可选输入"},
        )
        finished = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert finished["status"] == "failed"

        snapshot = runtime.store.require_run(started["id"]).latest_interaction
        assert snapshot["interactionId"] == interaction.id
        assert snapshot["kind"] == "scope_confirmation"
        assert "optional_route" in snapshot["question"]
        assert snapshot["answer"]["value"] == "adjust"
        assert snapshot["actionKey"] == "adjust"
        assert snapshot["requiresArtifact"] is True
        assert not any(
            event.kind == "status" and event.payload.get("status") == "completed"
            for event in runtime.store.events_after("project_scope_empty_response", 0)
        )
    finally:
        await runtime.shutdown()


async def test_compact_summary_carries_latest_interaction_facts_without_old_plan(settings) -> None:
    model = _CapturingScriptedModel(
        [[]],
        final="",
        content_by_call={0: "已保留专家调整意见，尚未形成新的脚本契约或脚本候选，请从当前交互继续。"},
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_compact_snapshot",
            project_id="project_compact_snapshot",
            session_id="project:project_compact_snapshot:run:run_compact_snapshot",
            status=RUN_RUNNING,
            prompt="开始确定性脚本建模",
            token_fingerprint="fingerprint",
        ))
        runtime.store.update_run(run.id, latest_interaction={
            "interactionId": "interaction_latest",
            "kind": "scope_confirmation",
            "question": "是否保留 optional_route 为可选输入？",
            "answer": {"value": "adjust", "text": "保留 optional_route 为可选输入"},
            "actionKey": "adjust",
            "continuation": "请基于最新范围调整生成脚本候选",
            "contractSummary": "demo_operation_batch_parse 脚本契约摘要",
            "operation": "demo_operation_batch_parse",
            "requiresArtifact": True,
            "updatedAt": "2026-09-02T00:00:00+08:00",
        })
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token="token_1",
            required_tools=set(),
        )
        live.bundle = SimpleNamespace(model=model)
        output = await runtime._run_compact_summary(  # noqa: SLF001
            live,
            runtime.store.require_run(run.id),
            "empty_content",
        )
        assert output
        assert model.seen_messages
        content = "".join(
            str(message.get("content") or "")
            for message in model.seen_messages[-1]
        )
        assert "optional_route" in content
        assert "保留" in content
        assert "demo_operation_batch_parse" in content
        assert "5 项" not in content
        assert "plan_id" not in content
        assert "approval_status" not in content
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
    assert "必须先调用 analyze_deterministic_capabilities" in prompt
    assert "只有收到专家答案后才能调用 confirm_deterministic_scope" in prompt
    assert "deterministic_confirmed 未通过时不得转换到评测阶段" in prompt


async def test_waiting_run_restores_persisted_credential_before_claiming_workspace(settings) -> None:
    class RestartedGateway(FakeGateway):
        async def call(self, **kwargs):  # type: ignore[override]
            if (
                kwargs["tool"] == "claim_candidate_workspace"
                and not kwargs["token"]
                and not self.run_credential(kwargs["run_id"])
            ):
                return {"error": "AUTH_REQUIRED", "reason": "运行凭据没有恢复"}
            return await super().call(**kwargs)

    gateway = RestartedGateway()
    runtime = service(settings, ScriptedModel([], final="已完成。"), gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_waiting_credential_restore",
            token="initial_token",
            message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed"})
        persisted = runtime.store.require_run(started["id"]).run_credential
        assert persisted

        gateway.credentials.clear()
        claimed = await runtime._claim_workspace(  # noqa: SLF001
            project_id="project_waiting_credential_restore",
            run_id=started["id"],
            token="",
        )

        assert claimed["packageContract"]["validatorVersion"]
        assert gateway.run_credential(started["id"]) == persisted
    finally:
        await runtime.shutdown()


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


async def test_candidate_validation_repair_gives_distinct_findings_separate_budgets() -> None:
    state = CandidateValidationRepairState()

    issue_ids = set()
    for round_number in range(1, 4):
        state.begin_round(f"model-{round_number}")
        result = state.record({
            "error": "CANDIDATE_FORMAT_INVALID",
            "validatorVersion": "package-v1",
            "findings": [{"ruleId": f"rule-{round_number}", "path": f"file-{round_number}.md"}],
        })
        assert result["repairAttempt"] == 1
        assert result["repairStopped"] is False
        issue_ids.add(result["issueId"])

    state.begin_round("model-4")
    assert state.attempts == 1
    assert state.repeated_findings is False
    assert state.stopped is False
    assert len(issue_ids) == 3


async def test_candidate_validation_repair_remembers_issue_across_alternating_findings() -> None:
    state = CandidateValidationRepairState()
    report_a = {
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findingFingerprint": "sha256:alternating-a",
    }
    report_b = {
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findingFingerprint": "sha256:alternating-b",
    }

    state.begin_round("model-1")
    state.record(report_a)
    state.begin_round("model-2")
    state.record(report_b)
    state.begin_round("model-3")
    repeated_a = state.record(report_a)

    assert repeated_a["repairAttempt"] == 2
    assert repeated_a["repairStopped"] is False

    state.begin_round("model-4")
    assert state.issue_attempts == {
        "sha256:alternating-a": 2,
        "sha256:alternating-b": 1,
    }
    assert state.stopped is True
    blocked = state.blocked_result("sha256:alternating-a")
    assert blocked["repairStopped"] is True
    assert blocked["tracePhase"] == "waiting"
    assert blocked["problemVisibility"] == "recovery"
    assert [action["id"] for action in blocked["recoveryActions"]] == [
        "review_validation_findings", "keep_current_progress",
    ]

    unrelated = state.record({
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findingFingerprint": "sha256:alternating-c",
    })
    assert unrelated["repairAttempt"] == 1
    assert unrelated["repairStopped"] is False


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


async def test_successful_candidate_write_does_not_reset_validation_episode() -> None:
    state = CandidateValidationRepairState()
    state.begin_round("model-1")
    state.record({
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findingFingerprint": "sha256:first",
        "validationReportId": "report-1",
        "findingCount": 12,
        "findingsTruncated": True,
    })
    state.begin_round("model-2")
    assert state.attempts == 1

    state.record_success("write_file")
    state.begin_round("model-3")

    assert state.attempts == 1
    assert state.last_fingerprint == "sha256:first"
    assert state.current_round_id == "model-3"
    assert state.stopped is False


async def test_successful_full_validation_resolves_all_open_issue_ids() -> None:
    state = CandidateValidationRepairState()
    state.begin_round("model-1")
    first = state.record({
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findings": [{"ruleId": "rule-a", "path": "objects/a.md"}],
    })
    second = state.record({
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findings": [{"ruleId": "rule-b", "path": "rules/b.md"}],
    })

    assert state.record_success("write_file") == []
    assert state.record_success("validate_change_set") == [
        first["issueId"], second["issueId"],
    ]
    assert state.open_issue_ids == []


async def test_compact_validation_report_without_inline_findings_enters_repair_state() -> None:
    gateway = FakeGateway({
        "write_file": {
            "error": "CANDIDATE_FORMAT_INVALID",
            "reason": "候选格式校验未通过",
            "retryable": True,
            "validatorVersion": "package-v1",
            "findingFingerprint": "sha256:compact",
            "validationReportId": "report-compact",
            "findingCount": 24,
            "findingsTruncated": True,
            "findingSummary": {
                "paths": [{"path": "objects/index.md", "count": 24}],
            },
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

    result = json.loads(await CoreTool(spec, context).invoke({
        "path": "objects/index.md",
        "content": "# index",
        "change_group": "objects",
        "evidence": [],
    }))

    assert result["validationReportId"] == "report-compact"
    assert result["findingFingerprint"] == "sha256:compact"
    assert result["findingCount"] == 24
    assert "findings" not in result
    assert result["repairAttempt"] == 1
    assert "get_validation_findings" in result["instruction"]


async def test_managed_file_failure_requires_domain_tool_and_resolves_same_issue() -> None:
    issue_id = "agent-issue-synthetic-binding"
    gateway = FakeGateway({
        "write_file": {
            "error": "MANAGED_FILE_REQUIRES_DOMAIN_TOOL",
            "reason": "对象数据映射是受管文件，不能直接写入",
            "requiredAction": "redirect_tool",
            "requiredTool": "patch_object_source_binding",
            "targetKind": "object_binding",
            "issueId": issue_id,
            "retryable": False,
        },
        "patch_object_source_binding": {
            "ok": True,
            "display": {"text": "对象与数据表绑定完成"},
        },
    })
    required: list[tuple[str, dict]] = []
    recoveries: dict[str, dict] = {}
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
        require_tool=lambda name, recovery: required.append((name, recovery)),
        technical_recoveries=recoveries,
    )
    write_spec = next(item for item in CORE_TOOL_SPECS if item.name == "write_file")
    rejected = json.loads(await CoreTool(write_spec, context).invoke({
        "path": "mappings/object-data-mappings.json",
        "content": "{}",
        "change_group": "synthetic_binding",
        "evidence": [],
    }))

    assert rejected["issueId"] == issue_id
    assert rejected["issueStatus"] == "repairing"
    assert rejected["tracePhase"] == "repairing"
    assert required[0][0] == "patch_object_source_binding"
    assert recoveries[issue_id]["targetKind"] == "object_binding"

    binding_spec = next(
        item for item in CORE_TOOL_SPECS if item.name == "patch_object_source_binding"
    )
    repaired = json.loads(await CoreTool(binding_spec, context).invoke({}))
    assert repaired["issueStatus"] == "resolved"
    assert repaired["resolvedIssueIds"] == [issue_id]
    assert recoveries == {}


async def test_revision_conflict_auto_recovers_then_offers_customer_actions() -> None:
    gateway = FakeGateway({
        "stage_change_set": {
            "error": "REVISION_CONFLICT",
            "reason": "知识包内容已经变化，请重新读取计划目标后生成新的 Change Set",
            "retryable": False,
            "recovery": {
                "owner": "agent",
                "strategy": "refresh_change_set",
                "requiredTool": "stage_change_set",
                "maxAttempts": 2,
            },
        },
    })
    required: list[str] = []
    released: list[str] = []
    context = ToolContext(
        project_id="project_revision_recovery",
        run_id="run_revision_recovery",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
        require_tool=lambda name, _recovery: required.append(name),
        release_required_tool=lambda name: released.append(name),
    )
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "stage_change_set")
    arguments = {
        "plan_id": "plan_revision_recovery",
        "expected_revision": "sha256:old",
        "mutation_id": "revision-recovery-fixture",
        "actions": [],
    }

    first = json.loads(await CoreTool(spec, context).invoke(arguments))
    second = json.loads(await CoreTool(spec, context).invoke(arguments))
    exhausted = json.loads(await CoreTool(spec, context).invoke(arguments))

    assert first["tracePhase"] == "repairing"
    assert first["problemVisibility"] == "activity"
    assert first["repairAttempt"] == 1
    assert second["tracePhase"] == "repairing"
    assert second["repairAttempt"] == 2
    assert exhausted["tracePhase"] == "waiting"
    assert exhausted["issueStatus"] == "awaiting_user"
    assert exhausted["problemVisibility"] == "recovery"
    assert exhausted["repairStopped"] is True
    assert [action["id"] for action in exhausted["recoveryActions"]] == [
        "retry_from_latest", "keep_current_progress",
    ]
    assert required == ["stage_change_set"] * 3
    assert released == ["stage_change_set"]
    recovery = context.technical_recoveries[exhausted["issueId"]]
    assert recovery["attempt"] == 2
    assert recovery["exhausted"] is True


async def test_revision_recovery_exhaustion_suspends_run_without_error_message(settings) -> None:
    action = {
        "task_id": "task_revision_recovery",
        "tool": "create_object",
        "arguments": {
            "key": "revision_recovery_object",
            "title": "版本恢复对象",
            "description": "用于验证版本冲突恢复",
            "fields": [{"key": "id", "type": "string", "required": True}],
        },
    }
    model = ScriptedModel([
        [ToolTurn("stage_change_set", {
            "plan_id": "plan_revision_recovery",
            "expected_revision": "sha256:old",
            "mutation_id": f"revision-recovery-{index}",
            "actions": [action],
        })]
        for index in range(12)
    ])
    gateway = FakeGateway({
        "stage_change_set": {
            "error": "REVISION_CONFLICT",
            "reason": "知识包内容已经变化，请重新读取计划目标后生成新的 Change Set",
            "retryable": False,
            "recovery": {
                "owner": "agent",
                "strategy": "refresh_change_set",
                "requiredTool": "stage_change_set",
                "maxAttempts": 2,
            },
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        project_id = "project_revision_recovery_suspend"
        state = runtime.store.project_state(project_id)
        runtime.store.transition_project_state(
            project_id,
            expected_revision=state.revision,
            next_state="knowledge:completed",
            cause="test_fixture",
            actor_run_id="",
        )
        started = await runtime.start_run(
            project_id=project_id,
            token="token_revision_recovery",
            message="继续完成当前建模计划",
        )
        settled = await wait_for_status(
            runtime, started["id"], {"suspended", "failed", "completed"}, timeout=10,
        )

        events = runtime.store.events_after(project_id, 0)
        tool_events = [
            event.payload for event in events
            if event.kind == "tool" and event.payload.get("name") == "stage_change_set"
        ]
        assert settled["status"] == "suspended", {
            "settled": settled,
            "modelCalls": model.calls,
            "toolEvents": tool_events,
            "statuses": [event.payload for event in events if event.kind == "status"],
        }
        assert [
            event["phase"] for event in tool_events if event["phase"] != "started"
        ] == [
            "repairing", "repairing", "waiting",
        ]
        assert not any(
            event.kind == "message"
            and any(block.get("type") == "error" for block in event.payload.get("blocks", []))
            for event in events
        )
    finally:
        await runtime.shutdown()


async def test_ask_rejects_technical_question_while_recovery_tool_is_available() -> None:
    issue_id = "agent-issue-synthetic-binding"
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        technical_recoveries={
            issue_id: {
                "issueId": issue_id,
                "requiredTool": "patch_object_source_binding",
                "targetKind": "object_binding",
            },
        },
    )
    tool_call = type("Call", (), {
        "arguments": json.dumps({
            "question": "当前环境无法补写，请让管理员处理吗？",
            "kind": "clarification",
            "evidence": [],
        }, ensure_ascii=False),
    })()

    decision = await AskRail(context).resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]
    result = json.loads(decision.tool_result)
    assert result["error"] == "TECHNICAL_RECOVERY_REQUIRED"
    assert result["issueId"] == issue_id
    assert result["issueStatus"] == "repairing"
    assert result["findings"][0]["ruleId"] == "interaction.technical_recovery.required"


async def test_ask_rejects_platform_capability_question_without_recovery_record() -> None:
    context = ToolContext(
        project_id="project_capability_question",
        run_id="run_capability_question",
        token="token_demo",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    tool_call = type("Call", (), {
        "arguments": json.dumps({
            "question": "本会话工具集中没有该读取能力，请问平台侧能力是否已开放？",
            "kind": "clarification",
            "evidence": [],
        }, ensure_ascii=False),
    })()

    decision = await AskRail(context).resolve_interrupt(None, tool_call, None)  # type: ignore[arg-type]
    result = json.loads(decision.tool_result)
    assert result["error"] == "TECHNICAL_RECOVERY_REQUIRED"
    assert result["findings"][0]["ruleId"] == "interaction.technical_recovery.required"
    assert context.technical_capability_claimed is True


async def test_option_value_is_saved_as_latest_interaction_action_key(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_option_action_key",
            project_id="project_option_action_key",
            session_id="session_option_action_key",
            status=RUN_RUNNING,
            prompt="确认脚本范围",
            token_fingerprint="fingerprint",
        ))
        interaction = InteractionRecord(
            id="interaction_option_action_key",
            project_id=run.project_id,
            run_id=run.id,
            tool_call_id="call_option_action_key",
            kind="scope_confirmation",
            payload={"question": "是否调整脚本范围？"},
        )
        runtime._persist_latest_interaction(  # noqa: SLF001
            run,
            interaction=interaction,
            answer={"value": "adjust", "text": "保留可选输入"},
        )
        snapshot = runtime.store.require_run(run.id).latest_interaction
        assert snapshot["actionKey"] == "adjust"
    finally:
        await runtime.shutdown()


async def test_structured_issue_action_resumes_terminal_run_without_fake_user_message(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("patch_object_source_binding", {})],
        [],
    ], final="对象与来源表已经重新绑定。")
    runtime = service(settings, model, FakeGateway({
        "patch_object_source_binding": {
            "ok": True,
            "display": {"text": "对象与来源表绑定完成"},
        },
    }))
    await runtime.startup()
    try:
        old = runtime.store.create_run(RunRecord(
            id="run_old_issue",
            project_id="project_issue_resume",
            session_id="old-session",
            status="queued",
            prompt="历史建模",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(old.id, status="completed")
        state = runtime.store.project_state(old.project_id)
        runtime.store.transition_project_state(
            old.project_id,
            expected_revision=state.revision,
            next_state="knowledge:completed",
            cause="test_fixture",
            actor_run_id=old.id,
        )
        issue_id = "agent-issue-synthetic-resume"
        runtime.store.save_technical_recovery(old.id, issue_id, {
            "issueId": issue_id,
            "requiredTool": "patch_object_source_binding",
            "targetKind": "object_binding",
            "actionId": "resume_object_binding",
        })

        resumed = await runtime.resume_issue(
            project_id=old.project_id,
            token="token_resume",
            issue_id=issue_id,
            action_id="resume_object_binding",
        )
        new_run_id = resumed["run"]["id"]
        settled = await wait_for_status(
            runtime, new_run_id, {"completed", "suspended", "failed"}, timeout=10,
        )

        assert resumed["mode"] == "new_run"
        assert settled["status"] == "completed"
        events = runtime.store.events_after(old.project_id, 0)
        assert not any(
            event.run_id == new_run_id and event.kind == "message"
            and event.payload.get("role") == "user"
            for event in events
        )
        assert any(
            event.run_id == new_run_id and event.kind == "tool"
            and issue_id in event.payload.get("resolvedIssueIds", [])
            for event in events
        )
        assert runtime.store.technical_recoveries(old.id) == {}
    finally:
        await runtime.shutdown()


async def test_structured_issue_can_keep_progress_and_end_suspended_run(settings) -> None:
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_keep_recovery_progress",
            project_id="project_keep_recovery_progress",
            session_id="keep-recovery-session",
            status="queued",
            prompt="历史建模",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(run.id, status="suspended")
        issue_id = "agent-recovery-keep-progress"
        runtime.store.save_technical_recovery(run.id, issue_id, {
            "issueId": issue_id,
            "requiredTool": "stage_change_set",
            "strategy": "refresh_change_set",
            "actionId": "retry_from_latest",
            "recoveryActions": [
                {"id": "retry_from_latest"},
                {"id": "keep_current_progress"},
            ],
            "attempt": 2,
            "maxAttempts": 2,
            "exhausted": True,
        })

        kept = await runtime.resume_issue(
            project_id=run.project_id,
            token="token_keep_progress",
            issue_id=issue_id,
            action_id="keep_current_progress",
        )

        assert kept["mode"] == "kept"
        assert kept["run"]["status"] == "cancelled"
        assert kept["resolvedIssueIds"] == [issue_id]
        assert runtime.store.technical_recoveries(run.id) == {}
        events = runtime.store.events_after(run.project_id, 0)
        assert any(
            event.kind == "tool"
            and event.payload.get("resolvedIssueIds") == [issue_id]
            for event in events
        )
    finally:
        await runtime.shutdown()


async def test_required_tool_recovery_card_answer_enters_issue_recovery_path(settings) -> None:
    """技术恢复卡虽复用 clarification UI，也必须走 resume_issue。"""
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_required_tool_card",
            project_id="project_required_tool_card",
            session_id="required-tool-card-session",
            status="queued",
            prompt="建立结构化产品台账",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(run.id, status="suspended")
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token="token_required_tool_card",
            required_tools={"begin_product_ledger", "append_product_ledger_batch"},
        )
        runtime._suspend_required_tool_review(  # noqa: SLF001
            live,
            run,
            {"begin_product_ledger", "append_product_ledger_batch"},
        )
        interaction = runtime.store.pending_interaction(run.project_id)
        assert interaction is not None
        assert interaction.payload["recovery"]["requiredTools"] == [
            "append_product_ledger_batch", "begin_product_ledger",
        ]

        kept = await runtime.answer_interaction(
            project_id=run.project_id,
            interaction_id=interaction.id,
            token="token_required_tool_card",
            answer={"value": "keep_current_progress", "selected": ["keep_current_progress"]},
        )
        assert kept["mode"] == "kept"
        assert kept["run"]["status"] == "cancelled"
    finally:
        await runtime.shutdown()


async def test_required_tool_recovery_retry_restores_all_missing_tools(settings) -> None:
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    captured: list[set[str]] = []

    async def continue_stub(run, token, prompt, **kwargs):
        captured.append(set(kwargs.get("required_tools") or ()))
        return run.public()

    runtime._continue = continue_stub  # type: ignore[method-assign]
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_required_tool_retry",
            project_id="project_required_tool_retry",
            session_id="required-tool-retry-session",
            status="queued",
            prompt="建立结构化产品台账",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(run.id, status="suspended")
        issue_id = "required-tool-retry"
        runtime.store.save_technical_recovery(run.id, issue_id, {
            "issueId": issue_id,
            "requiredTool": "begin_product_ledger",
            "requiredTools": ["begin_product_ledger", "append_product_ledger_batch"],
            "actionId": "retry_required_tools",
            "recoveryActions": [{"id": "retry_required_tools"}],
        })
        resumed = await runtime.resume_issue(
            project_id=run.project_id,
            token="token_required_tool_retry",
            issue_id=issue_id,
            action_id="retry_required_tools",
        )
        assert resumed["mode"] == "checkpoint_resume"
        assert captured == [{"begin_product_ledger", "append_product_ledger_batch"}]
    finally:
        await runtime.shutdown()


async def test_confirmation_answer_intent_ignores_assistant_card_text() -> None:
    """卡片文案出自助手，只有专家答复内容才能触发必需工具（台账误判回归）。"""
    card_question = (
        "已完成建模目标草稿：覆盖从原油输入到终产品的物料平衡与收率计算，"
        "并将 10 项已识别的口径歧义整理为待确认项。"
    )
    approval_answer = {
        "approved": True,
        "review_mode": "full",
        "displayText": "从当前进度继续",
    }
    intent_text = _expert_answer_intent_text(approval_answer)
    assert card_question not in intent_text
    assert "displayText" not in intent_text
    assert not _required_tools_for_user_intent(intent_text) & {
        "require_product_ledger", "begin_product_ledger",
    }
    # 旧逻辑把问题文案一并判定，会将助手行文误判成产品台账诉求。
    assert "require_product_ledger" in _required_tools_for_user_intent(
        f"{card_question}\n{intent_text}",
    )
    # 专家自己明确要求逐项对账时，意图判定仍然生效。
    explicit = _expert_answer_intent_text(
        {"feedback": "请把产品清单逐项去重后完整对账"},
    )
    assert "require_product_ledger" in _required_tools_for_user_intent(explicit)


async def test_unsatisfiable_required_tools_waived_only_after_real_attempts() -> None:
    live = LiveRun(
        run_id="run_waiver", project_id="project_waiver", token="token_waiver",
    )
    missing = {"begin_product_ledger", "append_product_ledger_batch"}
    live.required_tools = set(missing)
    # 完全没调用过：是回避，不是不可满足，不得撤销。
    assert _unsatisfiable_required_tools(live, missing) == set()
    # 只调用过一部分：仍按回避处理。
    live.attempted_tools = {"begin_product_ledger"}
    assert _unsatisfiable_required_tools(live, missing) == set()
    # 全部真实调用过且始终未完成：判定为不可满足，整组撤销。
    live.attempted_tools = set(missing)
    assert _unsatisfiable_required_tools(live, missing) == missing


async def test_cancel_required_tools_resumes_run_without_requirement(settings) -> None:
    """恢复卡的「取消该要求」应撤销必需工具并继续本轮，而不是结束本轮。"""
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    captured: list[dict] = []

    async def continue_stub(run, token, prompt, **kwargs):
        captured.append({
            "prompt": prompt,
            "requiredTools": set(kwargs.get("required_tools") or ()),
        })
        return run.public()

    runtime._continue = continue_stub  # type: ignore[method-assign]
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_cancel_required_tools",
            project_id="project_cancel_required_tools",
            session_id="cancel-required-tools-session",
            status="queued",
            prompt="建立结构化产品台账",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(run.id, status="suspended")
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token="token_cancel_required_tools",
            required_tools={"begin_product_ledger", "append_product_ledger_batch"},
        )
        live.attempted_tools = {"begin_product_ledger", "append_product_ledger_batch"}
        runtime._suspend_required_tool_review(  # noqa: SLF001
            live,
            run,
            {"begin_product_ledger", "append_product_ledger_batch"},
        )
        interaction = runtime.store.pending_interaction(run.project_id)
        assert interaction is not None
        option_values = [item["value"] for item in interaction.payload["options"]]
        assert "cancel_required_tools" in option_values
        assert "cancel_required_tools" in interaction.payload["recovery"]["allowedActions"]
        # 工具全部真实调用过仍被拒绝时，卡片应标注出来供专家知情决策。
        assert interaction.payload["attemptedButRejected"] == [
            "append_product_ledger_batch", "begin_product_ledger",
        ]
        assert "均被当前项目状态拒绝" in interaction.payload["failureReason"]

        cancelled = await runtime.answer_interaction(
            project_id=run.project_id,
            interaction_id=interaction.id,
            token="token_cancel_required_tools",
            answer={
                "value": "cancel_required_tools",
                "selected": ["cancel_required_tools"],
            },
        )
        assert cancelled["mode"] == "checkpoint_resume"
        assert captured and captured[0]["requiredTools"] == set()
        assert runtime.store.technical_recoveries(run.id) == {}
    finally:
        await runtime.shutdown()


async def test_validation_issue_resume_preserves_report_locator_and_has_fallback(settings) -> None:
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    captured: list[dict] = []

    async def continue_stub(run, token, prompt, **kwargs):
        captured.append({
            "run": run,
            "token": token,
            "prompt": prompt,
            "requiredTools": kwargs.get("required_tools"),
        })
        return run.public()

    runtime._continue = continue_stub  # type: ignore[method-assign]
    fixtures = [
        ("report-backed", "report-validation-resume", {"get_validation_summary"}),
        ("inline-findings", "", {"validate_change_set"}),
    ]
    for suffix, report_id, expected_tools in fixtures:
        run = runtime.store.create_run(RunRecord(
            id=f"run_validation_resume_{suffix}",
            project_id=f"project_validation_resume_{suffix}",
            session_id=f"validation-resume-{suffix}",
            status="queued",
            prompt="历史建模",
            token_fingerprint="000000000000",
        ))
        runtime.store.update_run(run.id, status="suspended")
        issue_id = f"validation-issue-{suffix}"
        runtime.store.save_validation_repair_state(run.id, {
            "openIssueIds": [issue_id],
            "lastReport": {
                "issueId": issue_id,
                "validationReportId": report_id,
                "findingFingerprint": f"sha256:{suffix}",
            },
        })

        resumed = await runtime.resume_issue(
            project_id=run.project_id,
            token=f"token-{suffix}",
            issue_id=issue_id,
            action_id="review_validation_findings",
        )

        assert resumed["mode"] == "checkpoint_resume"
        assert captured[-1]["requiredTools"] == expected_tools
        if report_id:
            assert report_id in captured[-1]["prompt"]
        else:
            assert "重新执行一次完整 Change Set 校验" in captured[-1]["prompt"]
        assert runtime.store.validation_repair_state(run.id).get("openIssueIds") == []


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
                "stopOnRepeatedFindingFingerprint": False,  # NOSONAR - public option name
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
        upload_results_after_retry = 0
        for item in after_retry:
            if item.kind != "message":
                continue
            if item.payload.get("blocks", [{}])[0].get("type") != "uploadResult":
                continue
            upload_results_after_retry += 1
        assert upload_results_after_retry == 1
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
        results = []
        for event in events:
            if event.kind != "message":
                continue
            if event.payload.get("blocks", [{}])[0].get("type") != "uploadResult":
                continue
            results.append(event.payload["blocks"][0])
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


async def test_material_purpose_review_stops_before_impact_or_full_read(settings) -> None:
    model = ScriptedModel([
        [
            ToolTurn("set_material_directive", {
                "filename": "业务说明.docx",
                "purpose": "evidence",
                "reading_mode": "on_demand",
            }),
            ToolTurn("set_material_directive", {
                "filename": "对象数据.xlsx",
                "purpose": "schema",
                "reading_mode": "full",
                "sheet_purposes": [{
                    "sheet": "客户",
                    "semantic_role": "schema",
                    "import_disposition": "object",
                }],
            }),
        ],
        [
            ToolTurn("analyze_material_impact", {"source": "source-doc"}),
            ToolTurn("read_documents_fully", {"filenames": ["对象数据.xlsx"]}),
        ],
        [],
    ])
    gateway = FakeGateway({
        "set_material_directive": {
            "ok": True,
            "requiresUserConfirmation": True,
            "display": {"text": "资料用途建议等待用户确认"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        common = {
            "uploadBatchId": "batch-purpose-review",
            "uploadBatchSize": 2,
            "purpose": "auto",
            "readingMode": "on_demand",
            "autoContinue": True,
            "revisionNo": 1,
            "purposeReviewRequired": True,
            "semanticChanged": True,
        }
        first = await runtime.handle_material_event(
            project_id="project_purpose_review",
            token="token_1",
            event={
                **common,
                "eventId": "material:doc-purpose:terminal",
                "status": "parsed",
                "documentId": "doc-purpose",
                "sourceKey": "source-doc",
                "filename": "业务说明.docx",
            },
        )
        assert first["waitingFor"] == 1
        second = await runtime.handle_material_event(
            project_id="project_purpose_review",
            token="token_2",
            event={
                **common,
                "eventId": "material:sheet-purpose:terminal",
                "status": "parsed",
                "documentId": "sheet-purpose",
                "sourceKey": "source-sheet",
                "filename": "对象数据.xlsx",
                "readingMode": "full",
                "sheetCount": 1,
            },
        )
        active = runtime.store.active_run("project_purpose_review")
        assert active is not None
        completed = await wait_for_status(runtime, active.id, {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert second["continued"] is True
        calls = [call["tool"] for call in gateway.calls]
        assert calls.count("set_material_directive") == 2
        assert "analyze_material_impact" not in calls
        assert "read_documents_fully" not in calls
        status_texts = [
            str(event.payload.get("text") or "")
            for event in runtime.store.events_after("project_purpose_review", 0)
            if event.kind == "status"
        ]
        assert "助手正在执行下一步" not in status_texts
    finally:
        await runtime.shutdown()


async def test_completed_purpose_review_is_deferred_until_restricted_run_finishes(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    await runtime.startup()
    try:
        run = runtime.store.create_run(RunRecord(
            id="run_deferred_purpose_review",
            project_id="project_deferred_purpose_review",
            session_id="session_deferred_purpose_review",
            status=RUN_RUNNING,
            prompt="预判资料用途",
            token_fingerprint="fingerprint",
        ))
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token="token_1",
            required_tools={"set_material_directive"},
            required_material_directives={"对象数据.xlsx"},
            observed_tools={"set_material_directive"},
            observed_material_directives={"对象数据.xlsx"},
            allowed_tools=PURPOSE_REVIEW_ALLOWED_TOOLS,
        )
        runtime._live[run.id] = live  # noqa: SLF001 - active purpose-review boundary

        result = await runtime.steer(
            project_id=run.project_id,
            token="token_2",
            text="全部资料用途已经确认，请继续全文精读并建模。",
            purpose_review_completed=True,
        )

        assert result["mode"] == "after_purpose_review"
        assert live.deferred_purpose_continuation is not None
        assert live.steering.empty()
        assert "read_documents_fully" not in live.required_tools
        runtime._live.pop(run.id, None)  # noqa: SLF001 - avoid synthetic live run during shutdown
    finally:
        await runtime.shutdown()


async def test_material_purpose_review_suggestion_does_not_change_project_phase(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    initial = runtime.store.project_state("project_pending_purpose_phase")
    released = runtime.store.transition_project_state(
        "project_pending_purpose_phase",
        expected_revision=initial.revision,
        next_state="release:idle",
        cause="test_released_baseline",
    )
    live = LiveRun(
        run_id="run_pending_purpose_phase",
        project_id="project_pending_purpose_phase",
        token="token_1",
    )

    await runtime._after_core_tool(  # noqa: SLF001 - pending-review state invariant
        live,
        "set_material_directive",
        {"filename": "对象数据.xlsx", "purpose": "schema"},
        {
            "document": {"filename": "对象数据.xlsx", "purpose": "schema"},
            "requiresUserConfirmation": True,
        },
    )

    current = runtime.store.project_state("project_pending_purpose_phase")
    assert current.state == released.state
    assert current.revision == released.revision
    assert live.observed_material_directives == {"对象数据.xlsx"}
    await runtime.shutdown()


async def test_material_purpose_review_requires_a_directive_for_every_document(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("set_material_directive", {
            "filename": "业务说明.docx",
            "purpose": "evidence",
            "reading_mode": "on_demand",
        })],
        [],
        [],
    ])
    gateway = FakeGateway({
        "set_material_directive": {
            "ok": True,
            "requiresUserConfirmation": True,
            "display": {"text": "资料用途建议等待用户确认"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        common = {
            "uploadBatchId": "batch-incomplete-purpose-review",
            "uploadBatchSize": 2,
            "purpose": "auto",
            "readingMode": "on_demand",
            "autoContinue": True,
            "revisionNo": 1,
            "purposeReviewRequired": True,
            "semanticChanged": True,
        }
        await runtime.handle_material_event(
            project_id="project_incomplete_purpose_review",
            token="token_1",
            event={
                **common,
                "eventId": "material:doc-incomplete-purpose:terminal",
                "status": "parsed",
                "documentId": "doc-incomplete-purpose",
                "sourceKey": "source-doc",
                "filename": "业务说明.docx",
            },
        )
        await runtime.handle_material_event(
            project_id="project_incomplete_purpose_review",
            token="token_2",
            event={
                **common,
                "eventId": "material:sheet-incomplete-purpose:terminal",
                "status": "parsed",
                "documentId": "sheet-incomplete-purpose",
                "sourceKey": "source-sheet",
                "filename": "对象数据.xlsx",
                "sheetCount": 1,
            },
        )
        active = runtime.store.active_run("project_incomplete_purpose_review")
        assert active is not None
        settled = await wait_for_status(runtime, active.id, {"waiting"})

        assert settled["status"] == "waiting"
        directives = [
            call["arguments"]["filename"] for call in gateway.calls
            if call["tool"] == "set_material_directive"
        ]
        assert directives == ["业务说明.docx"]
        interaction = runtime.store.pending_interaction("project_incomplete_purpose_review")
        assert interaction is not None
        assert interaction.payload["missingRequiredTools"] == ["set_material_directive"]
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
    checkpoint = None
    for kind, payload in events:
        if kind != "progress":
            continue
        if payload.get("counters", {}).get("progress") != 67:
            continue
        checkpoint = payload
        break
    assert checkpoint is not None
    assert checkpoint["detail"] == "8/12 个分块完成 · 全文覆盖 67%"
    assert checkpoint["counters"]["current"] == 8
    assert any(call["tool"] == "get_material_analysis_progress" for call in gateway.calls)


async def test_source_tools_bound_leader_context_without_losing_schema_fields() -> None:
    long_sample = "样" * 500
    gateway = FakeGateway({
        "read_document": {
            "filename": "产品手册.docx",
            "content": "正文" * 3_000,
            "offset": 0,
            "endOffset": 6_000,
            "nextOffset": 6_000,
            "totalChars": 90_000,
            "eof": False,
        },
        "describe_table": {
            "table": {
                "name": "客户表",
                "documentId": "doc-schema",
                "rowCount": 100,
                "fields": [{
                    "name": "customer_id",
                    "inferredType": "string",
                    "distinctRatio": 1,
                    "sampleValues": [long_sample, "C002", "C003", "C004"],
                    "largeUnusedProfile": "x" * 20_000,
                }],
            },
        },
        "read_table_rows": {
            "table": "客户表",
            "rows": [[f"C{index:03d}"] for index in range(5)],
            "nextOffset": 5,
            "totalRows": 100,
        },
    })
    context = ToolContext(
        project_id="project_context_budget",
        run_id="run_context_budget",
        token="token_context_budget",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )

    read_document = CoreTool(next(spec for spec in CORE_TOOL_SPECS if spec.name == "read_document"), context)
    describe_table = CoreTool(next(spec for spec in CORE_TOOL_SPECS if spec.name == "describe_table"), context)
    read_rows = CoreTool(next(spec for spec in CORE_TOOL_SPECS if spec.name == "read_table_rows"), context)

    document = json.loads(await read_document.invoke({"filename": "产品手册.docx", "limit": 20_000}))
    described = json.loads(await describe_table.invoke({"document_id": "doc-schema", "table": "客户表"}))
    rows = json.loads(await read_rows.invoke({
        "document_id": "doc-schema", "table": "客户表", "offset": 0, "limit": 50,
    }))

    assert document["nextOffset"] == 6_000
    assert described["table"]["fieldCount"] == 1
    assert described["table"]["fields"][0]["name"] == "customer_id"
    assert described["table"]["fields"][0]["distinctRatio"] == 1
    assert len(described["table"]["fields"][0]["sampleValues"]) == 3
    assert len(described["table"]["fields"][0]["sampleValues"][0]) <= 123
    assert "largeUnusedProfile" not in described["table"]["fields"][0]
    assert len(json.dumps(described, ensure_ascii=False)) < 2_000
    assert len(rows["rows"]) == 5
    calls = {call["tool"]: call["arguments"] for call in gateway.calls}
    assert calls["read_document"]["limit"] == 6_000
    assert calls["read_table_rows"]["limit"] == 20

    await read_document.invoke({"filename": "产品手册.docx", "limit": "invalid"})
    assert gateway.calls[-1]["arguments"]["limit"] == 6_000


async def test_schema_overview_tool_is_available_to_read_only_material_agents() -> None:
    names = {spec.name for spec in CORE_TOOL_SPECS}
    assert "get_schema_overview" in names
    assert "read_table_rows" in names


async def test_repeated_read_table_rows_uses_run_cache(settings) -> None:
    gateway = FakeGateway({
        "read_table_rows": {
            "table": "客户表",
            "rows": [["C001"]],
            "nextOffset": None,
            "totalRows": 1,
        },
    })
    context = ToolContext(
        project_id="project_read_cache",
        run_id="run_read_cache",
        token="token_read_cache",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    read_rows = CoreTool(next(spec for spec in CORE_TOOL_SPECS if spec.name == "read_table_rows"), context)

    first = json.loads(await read_rows.invoke({"table": "客户表", "offset": 0, "limit": 50}))
    second = json.loads(await read_rows.invoke({"table": "客户表", "offset": 0, "limit": 50}))

    assert first == second
    assert len([call for call in gateway.calls if call["tool"] == "read_table_rows"]) == 1


async def test_free_input_while_waiting_resumes_as_interjection(settings) -> None:
    original_question = {
        "question": "请选择业务范围",
        "kind": "business_decision",
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
        "kind": "business_decision",
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
        original_closed = None
        restored_opened = None
        for event in events:
            if event.kind != "interaction":
                continue
            if event.payload.get("id") == original.id and event.payload.get("status") == "answered":
                original_closed = event.seq
            if event.payload.get("id") == restored.id and event.payload.get("status") == "pending":
                restored_opened = event.seq
        assert original_closed is not None
        assert restored_opened is not None
        assert answer_done < original_closed < restored_opened
        original_answer_messages = 0
        for event in events:
            if event.kind != "message":
                continue
            if event.payload.get("blocks", [{}])[0].get("type") != "answer":
                continue
            if event.payload.get("blocks", [{}])[0].get("interactionId") != original.id:
                continue
            original_answer_messages += 1
        assert original_answer_messages == 0

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
        "kind": "business_decision",
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


@pytest.mark.parametrize(
    ("error_code", "reason"),
    [
        (
            "CANDIDATE_REVALIDATION_REQUIRED",
            "待确认候选或校验规则已经变化，原确认不能继续提交",
        ),
        (
            "STALE_CONFIRMATION",
            "待确认内容已经变化，不能把旧卡片的决定用于新候选",
        ),
    ],
)
async def test_stale_confirmation_is_retired_and_revalidated_before_new_card(
    settings,
    error_code: str,
    reason: str,
) -> None:
    groups = [{"id": "knowledge", "tier": "structural", "title": "确认知识修改", "impact": []}]
    model = ScriptedModel([
        [ToolTurn("request_confirmation", {"summary": "确认旧候选"})],
        [ToolTurn("request_confirmation", {"summary": "重新确认已校验候选"})],
    ])
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": groups},
        "request_confirmation": {
            "error": error_code,
            "reason": reason,
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
        system_blocks = []
        for event in runtime.store.events_after("project_legacy_confirmation", 0):
            if event.kind != "message" or event.payload.get("role") != "system":
                continue
            system_blocks.extend(
                block.get("text", "") for block in event.payload.get("blocks", [])
            )
        system_text = "\n".join(system_blocks)
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


async def test_cancel_waits_for_inflight_evaluation_and_prevents_late_retry(settings) -> None:
    class BlockingEvaluationGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.evaluation_started = threading.Event()
            self.evaluation_cancelled = threading.Event()

        async def call(self, **kwargs):
            tool = kwargs["tool"]
            if tool == "run_evaluation":
                self.calls.append(kwargs)
                self.evaluation_started.set()
                if not await asyncio.to_thread(self.evaluation_cancelled.wait, 5):
                    raise TimeoutError("evaluation cancellation was not forwarded")
                return {
                    "error": "EVALUATION_RUN_FAILED",
                    "reason": "评测运行失败，本轮已停止",
                }
            if tool == "cancel_evaluation":
                self.calls.append(kwargs)
                self.evaluation_cancelled.set()
                return {"cancelled": True, "roundId": "round-1", "status": "failed"}
            return await super().call(**kwargs)

    gateway = BlockingEvaluationGateway()
    model = ScriptedModel(
        [[ToolTurn("run_evaluation", {})], [ToolTurn("run_evaluation", {})]],
        final="继续重试评测。",
    )
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_cancel_evaluation",
            token="token_1",
            message="运行评测",
        )
        deadline = asyncio.get_running_loop().time() + 5
        while not gateway.evaluation_started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("evaluation did not start before cancellation")
            await asyncio.sleep(0.01)

        cancelled = await runtime.cancel(project_id="project_cancel_evaluation")
        await asyncio.sleep(0.05)

        assert cancelled["status"] == "cancelled"
        assert runtime.store.require_run(started["id"]).status == "cancelled"
        assert [call["tool"] for call in gateway.calls].count("cancel_evaluation") == 1
        assert [call["tool"] for call in gateway.calls].count("run_evaluation") == 1
    finally:
        await runtime.shutdown()


async def test_cancel_run_publishes_pending_interaction_closure(settings) -> None:
    model = ScriptedModel([[ToolTurn("ask", {
        "question": "是否继续当前方案？",
        "kind": "business_decision",
        "options": [
            {"label": "继续", "description": "继续当前方案", "value": "continue"},
            {"label": "停止", "description": "停止当前方案", "value": "stop"},
        ],
    })]])
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_cancel_interaction",
            token="token_1",
            message="请确认是否继续",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting"
        interaction = runtime.store.pending_interaction("project_cancel_interaction")
        assert interaction is not None

        cancelled = await runtime.cancel(project_id="project_cancel_interaction")

        assert cancelled["status"] == "cancelled"
        assert runtime.store.get_interaction(interaction.id).status == "cancelled"
        assert runtime.store.pending_interaction("project_cancel_interaction") is None
        closure_events = []
        for event in runtime.store.events_after("project_cancel_interaction", 0):
            if event.kind != "interaction":
                continue
            if event.payload.get("id") != interaction.id:
                continue
            if event.payload.get("status") == "cancelled":
                closure_events.append(event)
        assert len(closure_events) == 1
    finally:
        await runtime.shutdown()


async def test_cancel_run_settles_unfinished_tool_activity(settings) -> None:
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    run = runtime.store.create_run(RunRecord(
        id="run_cancel_change_set",
        project_id="project_cancel_change_set",
        session_id="session_cancel_change_set",
        status=RUN_RUNNING,
        prompt="继续知识建模",
        token_fingerprint="000000000000",
    ))
    runtime.store.update_run(run.id, status=RUN_RUNNING)
    runtime._live[run.id] = LiveRun(  # noqa: SLF001 - cancellation reconciliation invariant
        run_id=run.id,
        project_id=run.project_id,
        token="token_cancel_change_set",
    )
    runtime.events.publish(run.project_id, run.id, "progress", {
        "text": "正在原子生成待确认修改",
        "detail": "正在处理当前批次",
        "counters": {},
    })
    runtime.events.publish(run.project_id, run.id, "tool", {
        "name": "stage_change_set",
        "phase": "started",
        "summary": "正在原子生成待确认修改",
        "toolCallId": "call_cancel_change_set",
    })
    try:
        cancelled = await runtime.cancel(project_id=run.project_id)

        events = runtime.store.events_after(run.project_id, 0)
        tool_events = []
        for event in events:
            if event.kind != "tool":
                continue
            if event.payload.get("toolCallId") != "call_cancel_change_set":
                continue
            tool_events.append(event)
        assert cancelled["status"] == "cancelled"
        assert [event.payload.get("phase") for event in tool_events] == ["started", "cancelled"]
        assert tool_events[-1].payload["summary"] == "运行已停止，当前操作已取消"
        assert runtime.store.unfinished_tool_events(run.id) == []
        assert runtime.project_state(run.project_id)["activity"] is None
        cancelled_status = next(
            event for event in events
            if event.kind == "status" and event.payload.get("status") == "cancelled"
        )
        assert tool_events[-1].seq < cancelled_status.seq
    finally:
        await runtime.shutdown()


async def test_failed_run_settles_unfinished_tool_activity(settings) -> None:
    runtime = service(settings, ScriptedModel([]), FakeGateway())
    run = runtime.store.create_run(RunRecord(
        id="run_failed_change_set",
        project_id="project_failed_change_set",
        session_id="session_failed_change_set",
        status=RUN_RUNNING,
        prompt="继续知识建模",
        token_fingerprint="000000000000",
    ))
    runtime.store.update_run(run.id, status=RUN_RUNNING)
    runtime.events.publish(run.project_id, run.id, "tool", {
        "name": "stage_change_set",
        "phase": "started",
        "summary": "正在原子生成待确认修改",
        "toolCallId": "call_failed_change_set",
    })
    try:
        runtime._settle_failed_tool_events(  # noqa: SLF001 - failure settlement invariant
            run.project_id,
            run.id,
            "这次处理没有完成，之前已经确认的内容和候选修改都已保留。",
        )

        events = runtime.store.events_after(run.project_id, 0)
        tool_events = [
            event for event in events
            if event.kind == "tool"
            and event.payload.get("toolCallId") == "call_failed_change_set"
        ]
        assert [event.payload.get("phase") for event in tool_events] == ["started", "failed"]
        assert tool_events[-1].payload["summary"] == "这次处理没有完成，之前已经确认的内容和候选修改都已保留。"
        assert runtime.store.unfinished_tool_events(run.id) == []
    finally:
        await runtime.shutdown()


async def test_cancel_run_enters_suspended_when_core_receipt_is_unknown(settings) -> None:
    class UnknownCancellationGateway(FakeGateway):
        async def cancel_run(self, **_kwargs):
            return {"quiescent": False, "error": "CORE_CANCELLATION_UNAVAILABLE"}

    runtime = service(settings, ScriptedModel([]), UnknownCancellationGateway())
    run = runtime.store.create_run(RunRecord(
        id="run_cancel_unknown_receipt",
        project_id="project_cancel_unknown_receipt",
        session_id="session_cancel_unknown_receipt",
        status=RUN_RUNNING,
        prompt="继续知识建模",
        token_fingerprint="000000000000",
    ))
    runtime.store.update_run(run.id, status=RUN_RUNNING)
    runtime._live[run.id] = LiveRun(
        run_id=run.id,
        project_id=run.project_id,
        token="token_cancel_unknown_receipt",
    )
    runtime.events.publish(run.project_id, run.id, "tool", {
        "name": "stage_change_set",
        "phase": "started",
        "summary": "正在原子生成待确认修改",
        "toolCallId": "call_cancel_unknown_receipt",
    })
    try:
        suspended = await runtime.cancel(project_id=run.project_id)
        assert suspended["status"] == "suspended"
        events = runtime.store.events_after(run.project_id, 0)
        tool_events = [
            event.payload for event in events
            if event.kind == "tool"
            and event.payload.get("toolCallId") == "call_cancel_unknown_receipt"
        ]
        assert tool_events[-1]["phase"] == "cancel_pending"
        assert not any(
            event.kind == "status" and event.payload.get("status") == "cancelled"
            for event in events
        )
    finally:
        await runtime.shutdown()


async def test_repeated_cancel_restores_credential_and_uses_latest_user_token(settings) -> None:
    class RecoveringCancellationGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.cancel_calls = []

        async def cancel_run(self, **kwargs):
            self.cancel_calls.append({
                "token": kwargs.get("token"),
                "credential": self.run_credential(str(kwargs.get("run_id") or "")),
            })
            return {"quiescent": len(self.cancel_calls) > 1}

    gateway = RecoveringCancellationGateway()
    runtime = service(settings, ScriptedModel([]), gateway)
    run = runtime.store.create_run(RunRecord(
        id="run_repeated_cancel",
        project_id="project_repeated_cancel",
        session_id="session_repeated_cancel",
        status=RUN_RUNNING,
        prompt="继续知识建模",
        token_fingerprint="000000000000",
        run_credential="arc_persisted_test_credential",
    ))
    runtime.store.update_run(run.id, status=RUN_RUNNING)
    try:
        result = await runtime.cancel(project_id=run.project_id, token="latest_user_token")
        assert result["status"] == "cancelled"
        assert gateway.cancel_calls
        assert all(call["token"] == "latest_user_token" for call in gateway.cancel_calls)
        assert all(call["credential"] == "arc_persisted_test_credential" for call in gateway.cancel_calls)
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


async def test_pending_evaluation_stops_agent_without_autonomous_polling(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("run_evaluation", {})],
            [ToolTurn("get_project_state", {})],
        ],
        final="不应继续轮询评测。",
    )
    gateway = FakeGateway({
        "run_evaluation": {
            "roundId": "round-pending",
            "status": "running",
            "pending": True,
            "terminal": False,
            "display": {"text": "正式评测任务已创建，可在评测界面查看实时进度"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_evaluation_pending",
            token="token_1",
            message="请立即运行评测",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert completed["result"] == EVALUATION_PENDING_MESSAGE
        assert model.calls == 1
        core_calls = [call for call in gateway.calls if call["tool"] != "claim_candidate_workspace"]
        assert [call["tool"] for call in core_calls] == ["run_evaluation"]
        events = runtime.store.events_after("project_evaluation_pending", 0)
        focus_events = [event.payload for event in events if event.kind == "focus"]
        assert focus_events == [{"target": {
            "kind": "evaluation",
            "target": "正式评测",
            "view": "runs",
            "roundId": "round-pending",
        }}]
        visible_text = "".join(
            str(event.payload.get("delta") or "")
            for event in events
            if event.kind == "message_delta"
        )
        assert visible_text == EVALUATION_PENDING_MESSAGE
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


async def test_checkpoint_db_points_to_openjiuwen_physical_database(settings) -> None:
    assert settings.checkpoint_db.name == "checkpoints.sqlite.db"


async def test_global_checkpoint_failure_does_not_mark_idle_project_as_recovery(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]), FakeGateway())
    runtime._storage_diag = {"healthy": False, "checkpointDbExists": False}
    try:
        state = runtime.project_state("project_new")
        assert "recovery" not in state["state"]
    finally:
        await runtime.shutdown()


async def test_checkpoint_failure_is_reported_for_recoverable_project(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]), FakeGateway())
    run = RunRecord(
        id="run_checkpoint_missing",
        project_id="project_checkpoint_missing",
        session_id="session_checkpoint_missing",
        status=RUN_RUNNING,
        prompt="继续建模",
        token_fingerprint="fingerprint",
    )
    runtime.store.create_run(run)
    runtime.store.update_run(run.id, status=RUN_RUNNING)
    runtime._storage_diag = {"healthy": False, "checkpointDbExists": False}
    try:
        state = runtime.project_state(run.project_id)
        assert state["state"]["recovery"] == {
            "status": "volume_mismatch",
            "missing": ["checkpoint"],
            "nextSteps": ["核对建模服务数据卷与 checkpoints.sqlite.db 后重启"],
        }
    finally:
        await runtime.shutdown()


async def test_project_state_uses_authoritative_terminal_evaluation_and_rejects_stale_summary(settings) -> None:
    gateway = FakeGateway({"evaluation_status": {
        "available": True,
        "roundId": "round_terminal",
        "status": "failed",
        "terminal": True,
        "done": 2,
        "total": 5,
        "passed": 1,
        "finishedAt": "2026-08-31T08:00:00.000Z",
    }})
    runtime = service(settings, ScriptedModel([[]]), gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_terminal_state")
        runtime.store.transition_project_state(
            "project_terminal_state",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测仍在异步运行中，等待本轮结束。",
            open_questions=["等待评测完成"],
        )
        run = RunRecord(
            id="run_terminal_state",
            project_id="project_terminal_state",
            session_id="session_terminal_state",
            status="queued",
            prompt="读取状态",
            token_fingerprint="fingerprint",
        )
        runtime.store.create_run(run)
        runtime.store.update_run(run.id, status=RUN_RUNNING)
        live = LiveRun(run_id=run.id, project_id=run.project_id, token="token_1")

        state = await runtime._state_for_agent(live)
        assert state["evaluation"]["status"] == "failed"
        assert state["evaluation"]["terminal"] is True
        assert "运行中" not in state["summary"]
        assert state["openQuestions"] == []

        current = runtime.store.project_state(run.project_id)
        with pytest.raises(AgentRuntimeError) as raised:
            await runtime._state_action(live, "update_project_context", {
                "expected_revision": current.revision,
                "summary": "评测仍在后台运行中。",
                "open_questions": ["等待本轮评测结束"],
                "evidence": [],
            })
        assert raised.value.code == "STATE_EVALUATION_STATUS_CONFLICT"
        assert raised.value.details["evaluation"]["status"] == "failed"
        assert runtime.store.project_state(run.project_id).summary == "评测仍在异步运行中，等待本轮结束。"
    finally:
        await runtime.shutdown()


async def test_evaluation_terminal_event_reconciles_state_once_and_invalidates_cache(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("get_evaluation_results", {"round_id": "round_event"})],
        [],
        [],
    ], final="已读取本轮失败结果。")
    gateway = FakeGateway({
        "get_evaluation_results": {
            "round": {"id": "round_event", "status": "done"},
            "counts": {"pass": 3, "fail": 1},
            "results": [{"testcaseId": "case-failed", "verdict": "fail"}],
            "display": {"text": "已读取 4 条真实评测结果"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_evaluation_event")
        runtime.store.transition_project_state(
            "project_evaluation_event",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测正在运行中。",
            open_questions=["等待评测完成", "保留另一个业务问题"],
        )
        live = LiveRun(
            run_id="run_evaluation_event",
            project_id="project_evaluation_event",
            token="token_1",
            stable_read_cache={"get_evaluation_results:latest": {"round": {"status": "running"}}},
        )
        runtime._live[live.run_id] = live
        event = {
            "eventId": "evaluation:round_event:terminal",
            "roundId": "round_event",
            "status": "done",
            "done": 4,
            "total": 4,
            "passed": 3,
            "finishedAt": "2026-08-31T09:00:00.000Z",
        }

        first = await runtime.handle_evaluation_event(
            project_id="project_evaluation_event", token="token_1", event=event,
        )
        continuation_run_id = runtime.store.project_state("project_evaluation_event").run_id
        second = await runtime.handle_evaluation_event(
            project_id="project_evaluation_event", token="token_1", event=event,
        )
        completed = await wait_for_status(
            runtime, continuation_run_id, {"completed", "failed"},
        )

        assert first["reconciled"] is True
        assert first["continued"] is True
        assert second == {
            "accepted": True, "duplicate": True, "reconciled": False,
        }
        assert completed["status"] == "completed", completed["error"]
        state = runtime.store.project_state("project_evaluation_event")
        assert "已完成" in state.summary
        assert "运行中" not in state.summary
        assert state.open_questions == ["保留另一个业务问题"]
        assert live.stable_read_cache == {}
        messages = [
            item for item in runtime.store.events_after("project_evaluation_event", 0)
            if item.kind == "message" and item.payload.get("externalEventId") == event["eventId"]
        ]
        assert len(messages) == 1
        result_calls = [
            call for call in gateway.calls if call["tool"] == "get_evaluation_results"
        ]
        assert len(result_calls) == 1
        assert result_calls[0]["arguments"] == {"round_id": "round_event"}
    finally:
        runtime._live.pop("run_evaluation_event", None)
        await runtime.shutdown()


async def test_concurrent_evaluation_terminal_event_starts_one_continuation(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("get_evaluation_results", {"round_id": "round_concurrent"})],
        [],
        [],
    ], final="已读取本轮失败结果。")
    gateway = FakeGateway({
        "get_evaluation_results": {
            "round": {"id": "round_concurrent", "status": "done"},
            "counts": {"pass": 2, "fail": 1},
            "results": [{"testcaseId": "case-failed", "verdict": "fail"}],
            "display": {"text": "已读取 3 条合成评测结果"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    release_continuation = asyncio.Event()
    continuation_started = asyncio.Event()
    continuation_calls = 0
    original_continue = runtime._continue_for_external_event

    async def blocked_continue(**kwargs) -> None:
        nonlocal continuation_calls
        continuation_calls += 1
        continuation_started.set()
        await release_continuation.wait()
        await original_continue(**kwargs)

    runtime._continue_for_external_event = blocked_continue  # type: ignore[method-assign]
    first_task: asyncio.Task | None = None
    try:
        initial = runtime.store.project_state("project_evaluation_concurrent")
        runtime.store.transition_project_state(
            "project_evaluation_concurrent",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测正在运行中。",
            open_questions=["等待评测完成"],
        )
        event = {
            "eventId": "evaluation:round_concurrent:terminal",
            "roundId": "round_concurrent",
            "status": "done",
            "done": 3,
            "total": 3,
            "passed": 2,
            "finishedAt": "2026-08-31T11:00:00.000Z",
        }

        first_task = asyncio.create_task(runtime.handle_evaluation_event(
            project_id="project_evaluation_concurrent", token="token_1", event=event,
        ))
        await asyncio.wait_for(continuation_started.wait(), timeout=5)

        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.handle_evaluation_event(
                project_id="project_evaluation_concurrent", token="token_1", event=event,
            )
        assert raised.value.code == "EVALUATION_EVENT_IN_PROGRESS"
        assert raised.value.status == 409

        release_continuation.set()
        first = await first_task
        continuation_run_id = runtime.store.project_state(
            "project_evaluation_concurrent",
        ).run_id
        completed = await wait_for_status(
            runtime, continuation_run_id, {"completed", "failed"},
        )
        duplicate = await runtime.handle_evaluation_event(
            project_id="project_evaluation_concurrent", token="token_1", event=event,
        )

        assert first["continued"] is True
        assert completed["status"] == "completed", completed["error"]
        assert duplicate == {
            "accepted": True, "duplicate": True, "reconciled": False,
        }
        assert continuation_calls == 1
        result_calls = [
            call for call in gateway.calls if call["tool"] == "get_evaluation_results"
        ]
        assert len(result_calls) == 1
        assert result_calls[0]["arguments"] == {"round_id": "round_concurrent"}
    finally:
        release_continuation.set()
        if first_task is not None and not first_task.done():
            await asyncio.gather(first_task, return_exceptions=True)
        await runtime.shutdown()


async def test_passed_evaluation_terminal_event_opens_release_confirmation_once(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("request_release", {"summary": "确认真实用例覆盖并发布"})],
    ])
    gateway = FakeGateway({
        "release_preflight": {
            "ready": True,
            "version": "1.0.0",
            "testcase_count": 4,
            "questions": ["合成评测问题"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_evaluation_passed")
        runtime.store.transition_project_state(
            "project_evaluation_passed",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测正在运行中。",
            open_questions=["等待评测完成"],
        )
        event = {
            "eventId": "evaluation:round_passed:terminal",
            "roundId": "round_passed",
            "status": "done",
            "done": 4,
            "total": 4,
            "passed": 4,
            "finishedAt": "2026-08-31T10:00:00.000Z",
        }

        first = await runtime.handle_evaluation_event(
            project_id="project_evaluation_passed", token="token_1", event=event,
        )
        continuation_run_id = runtime.store.project_state("project_evaluation_passed").run_id
        second = await runtime.handle_evaluation_event(
            project_id="project_evaluation_passed", token="token_1", event=event,
        )
        waiting = await wait_for_status(
            runtime, continuation_run_id, {"waiting", "failed"},
        )

        assert first["continued"] is True
        assert second == {
            "accepted": True, "duplicate": True, "reconciled": False,
        }
        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_evaluation_passed")
        assert interaction is not None and interaction.kind == "release_confirmation"
        assert [call["tool"] for call in gateway.calls].count("release_preflight") == 1
        assert not [call for call in gateway.calls if call["tool"] == "get_evaluation_results"]
    finally:
        await runtime.shutdown()


async def test_cancelled_evaluation_terminal_event_does_not_continue_agent(settings) -> None:
    model = ScriptedModel([[]])
    gateway = FakeGateway()
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_evaluation_cancelled")
        runtime.store.transition_project_state(
            "project_evaluation_cancelled",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测正在运行中。",
            open_questions=["等待评测完成"],
        )
        event = {
            "eventId": "evaluation:round_cancelled:terminal",
            "roundId": "round_cancelled",
            "status": "cancelled",
            "done": 1,
            "total": 4,
            "passed": 1,
            "finishedAt": "2026-08-31T10:00:00.000Z",
        }

        first = await runtime.handle_evaluation_event(
            project_id="project_evaluation_cancelled", token="token_1", event=event,
        )
        second = await runtime.handle_evaluation_event(
            project_id="project_evaluation_cancelled", token="token_1", event=event,
        )

        assert first["continued"] is False
        assert second == {
            "accepted": True, "duplicate": True, "reconciled": False,
        }
        assert runtime.store.active_run("project_evaluation_cancelled") is None
        assert model.calls == 0
        assert gateway.calls == []
    finally:
        await runtime.shutdown()


async def test_evaluation_terminal_event_does_not_present_or_apply_older_round(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]), FakeGateway())
    await runtime.startup()
    try:
        initial = runtime.store.project_state("project_evaluation_order")
        runtime.store.transition_project_state(
            "project_evaluation_order",
            expected_revision=initial.revision,
            next_state="evaluation:idle",
            cause="test_setup",
            summary="评测正在运行中。",
            open_questions=["等待评测完成"],
        )
        latest = {
            "eventId": "evaluation:round_latest:terminal",
            "roundId": "round_latest",
            "status": "done",
            "done": 5,
            "total": 5,
            "passed": 5,
            "finishedAt": "2026-08-31T10:00:00.000Z",
        }
        older = {
            "eventId": "evaluation:round_older:terminal",
            "roundId": "round_older",
            "status": "done",
            "done": 4,
            "total": 4,
            "passed": 3,
            "finishedAt": "2026-08-31T09:00:00.000Z",
        }

        applied = await runtime.handle_evaluation_event(
            project_id="project_evaluation_order", token="token_1", event=latest,
        )
        state_after_latest = runtime.store.project_state("project_evaluation_order")
        ignored = await runtime.handle_evaluation_event(
            project_id="project_evaluation_order", token="token_1", event=older,
        )

        assert applied["reconciled"] is True
        assert ignored["stale"] is True
        state_after_older = runtime.store.project_state("project_evaluation_order")
        assert state_after_older.revision == state_after_latest.revision
        assert state_after_older.summary == state_after_latest.summary
        assert "5/5" in state_after_older.summary
        messages = [
            item.payload for item in runtime.store.events_after("project_evaluation_order", 0)
            if item.kind == "message" and item.payload.get("externalEventId")
        ]
        assert [item["externalEventId"] for item in messages] == [latest["eventId"]]
        stale_receipt = runtime.store._db.execute(  # noqa: SLF001 - persistence invariant test
            "SELECT continuation_status, presented_at FROM external_project_events WHERE id = ?",
            (older["eventId"],),
        ).fetchone()
        assert stale_receipt["continuation_status"] == "continued"
        assert stale_receipt["presented_at"] == ""

        current = runtime.store.project_state("project_evaluation_order")
        runtime.store.transition_project_state(
            "project_evaluation_order",
            expected_revision=current.revision,
            next_state=current.state,
            cause="test_stale_projection",
            summary="最近一轮正式评测已完成：已执行 4/4 条，通过 3 条。",
        )
        assert "4/4" in runtime.store.project_state("project_evaluation_order").summary
        assert "5/5" in runtime.project_state("project_evaluation_order")["state"]["summary"]
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
            "kind": "business_decision",
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
        completed = []
        for event in events:
            if event.kind != "tool":
                continue
            if event.payload.get("name") != "discard_rejected_candidate_changes":
                continue
            if event.payload.get("phase") != "completed":
                continue
            completed.append(event)
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
        tool_started = None
        for event in events:
            if event.kind != "tool":
                continue
            if event.payload.get("name") != "run_evaluation":
                continue
            if event.payload.get("phase") != "started":
                continue
            tool_started = event
            break
        assert tool_started is not None
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
                "filename": "工艺规程.docx", "purpose": "evidence", "reading_mode": "full",
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
        # Reading a document in a released project is read-only; it must not
        # rewind the published phase into full knowledge modeling.
        assert runtime.project_state("project_full_read")["state"]["phase"] == "release"
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
                "filename": "工艺规程.docx", "purpose": "evidence", "reading_mode": "full",
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
        settled = await wait_for_status(runtime, started["id"], {"waiting"})

        assert settled["status"] == "waiting"
        assert [call["tool"] for call in gateway.calls].count("read_documents_fully") >= 1
        interaction = runtime.store.pending_interaction("project_failed_full_read")
        assert interaction is not None
        assert interaction.payload["missingRequiredTools"] == ["read_documents_fully"]
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
    context.validation_repair.attempts = 2
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
    assert context.validation_repair.candidate_revision == "candidate_goal"
    assert context.validation_repair.attempts == 0
    assert events == [
        ("focus", {"target": {"kind": "diff", "target": ".modeling/goal.md"}}),
    ]

    settled = await rail.resolve_interrupt(  # type: ignore[arg-type]
        None,
        tool_call,
        {"action": "settled_confirmation", "result": {"ok": True, "accepted": True}},
    )
    assert json.loads(settled.tool_result)["accepted"] is True
    assert context.validation_repair.candidate_revision == ""
    assert context.validation_repair.attempts == 0


async def test_process_confirmation_does_not_require_schema_mapping_plan() -> None:
    group = {
        "id": "process",
        "tier": "caliber",
        "title": "确认合成业务流程",
        "impact": ["diagrams/synthetic-flow.mmd"],
    }
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": [group]},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-process",
            "validation_receipt": {
                "candidateHash": "sha256:process",
                "validatorVersion": "package-contract-v2",
            },
            "auto_approved": False,
            "change_groups": [group],
        },
        "get_schema_mapping_plan": {
            "complete": False,
            "unresolvedObjectSheetCount": 3,
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    decision = await ConfirmationRail(context).resolve_interrupt(  # type: ignore[arg-type]
        None,
        type("Call", (), {"arguments": '{"summary":"确认合成业务流程"}'})(),
        None,
    )

    assert decision.request.metadata["candidateRevision"] == "candidate-process"
    assert [call["tool"] for call in gateway.calls] == [
        "prepare_confirmation_groups",
        "request_confirmation_preflight",
    ]


async def test_knowledge_confirmation_routes_incomplete_schema_mapping_back_to_agent() -> None:
    group = {
        "id": "objects",
        "tier": "structural",
        "title": "确认合成对象",
        "impact": ["objects/synthetic-record.md"],
    }
    problem = {
        "code": "SCHEMA_SHEET_OBJECT_MAPPING_MISSING",
        "documentId": "document_synthetic",
        "sheet": "synthetic_records",
        "message": "实体 Sheet 尚未完成对象来源绑定",
    }
    mapping_plan = {
        "complete": False,
        "objectSheetCount": 1,
        "mappedObjectSheetCount": 0,
        "unresolvedObjectSheetCount": 1,
        "sheets": [{
            "documentId": "document_synthetic",
            "sheet": "synthetic_records",
            "importDisposition": "object",
            "mappedObjects": [],
            "mappingStatus": "unmapped",
        }],
        "problems": [problem],
    }
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": [group]},
        "get_schema_mapping_plan": mapping_plan,
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    decision = await ConfirmationRail(context).resolve_interrupt(  # type: ignore[arg-type]
        None,
        type("Call", (), {"arguments": '{"summary":"确认合成对象"}'})(),
        None,
    )

    delivered = json.loads(decision.tool_result)
    assert delivered["error"] == "SCHEMA_SHEET_MAPPING_INCOMPLETE"
    assert delivered["retryable"] is True
    assert delivered["repairScope"] == "mapping-plan"
    assert delivered["display"]["text"] == "正在补齐 1 张实体数据表的对象映射"
    assert delivered["mappingPlan"] == mapping_plan
    assert delivered["findings"][0]["actual"]["sheet"] == "synthetic_records"
    assert [call["tool"] for call in gateway.calls] == [
        "prepare_confirmation_groups",
        "get_schema_mapping_plan",
    ]


async def test_knowledge_confirmation_continues_after_schema_mapping_is_complete() -> None:
    group = {
        "id": "objects",
        "tier": "structural",
        "title": "确认合成对象",
        "impact": ["objects/synthetic-record.md"],
    }
    gateway = FakeGateway({
        "prepare_confirmation_groups": {"change_groups": [group]},
        "get_schema_mapping_plan": {
            "complete": True,
            "unresolvedObjectSheetCount": 0,
            "problems": [],
        },
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-knowledge",
            "validation_receipt": {
                "candidateHash": "sha256:knowledge",
                "validatorVersion": "package-contract-v2",
            },
            "auto_approved": False,
            "change_groups": [group],
        },
    })
    context = ToolContext(
        project_id="project_demo",
        run_id="run_demo",
        token="token_demo",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    decision = await ConfirmationRail(context).resolve_interrupt(  # type: ignore[arg-type]
        None,
        type("Call", (), {"arguments": '{"summary":"确认合成对象"}'})(),
        None,
    )

    assert decision.request.metadata["candidateRevision"] == "candidate-knowledge"
    assert [call["tool"] for call in gateway.calls] == [
        "prepare_confirmation_groups",
        "get_schema_mapping_plan",
        "request_confirmation_preflight",
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
    assert "自动修正已达到上限" in blocked["instruction"]


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


async def test_missing_confirmation_card_suspends_with_recovery_actions(settings) -> None:
    model = ScriptedModel(
        [[], [], []],
        final="确认卡已生成，等待您确认。",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_confirmation_review_recovery",
            token="token_1",
            message="继续复核当前候选",
        )
        suspended = await wait_for_status(runtime, started["id"], {"suspended", "failed"})

        assert suspended["status"] == RUN_SUSPENDED
        recoveries = runtime.store.technical_recoveries(started["id"])
        assert len(recoveries) == 1
        issue_id, recovery = next(iter(recoveries.items()))
        assert issue_id == f"confirmation-review-{started['id']}"
        assert recovery["strategy"] == "confirmation_review"
        assert [item["id"] for item in recovery["recoveryActions"]] == [
            "retry_from_latest", "keep_current_progress",
        ]
        events = runtime.store.events_after("project_confirmation_review_recovery", 0)
        waiting = next(
            event.payload for event in events
            if event.kind == "tool" and event.payload.get("issueId") == issue_id
        )
        assert waiting["phase"] == "waiting"
        assert waiting["code"] == "CONFIRMATION_CARD_NOT_CREATED"
        assert waiting["projectPhase"] == "intake"
        assert waiting["toolProfile"] == "domain-v2"
        assert not any(
            event.kind == "message"
            and "助手未能形成整体确认" in str(event.payload)
            for event in events
        )
    finally:
        await runtime.shutdown()


async def test_confirmation_review_retries_from_latest_and_recreates_card(settings) -> None:
    group = {"id": "script", "tier": "caliber", "title": "脚本范围"}
    model = ScriptedModel(
        [
            [], [], [],
            [ToolTurn("list_domain_resources", {"kind": "operation"})],
            [ToolTurn("request_confirmation", {"summary": "请确认脚本范围"})],
        ],
        final="确认卡已生成，等待您确认。",
    )
    gateway = FakeGateway({
        "list_domain_resources": {
            "working_revision": "working-revision",
            "kind": "operation",
            "items": [],
            "total": 0,
        },
        "prepare_confirmation_groups": {"change_groups": [group]},
        "request_confirmation_preflight": {
            "candidate_revision": "candidate-script",
            "auto_approved": False,
            "change_groups": [group],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_confirmation_review_resume",
            token="token_1",
            message="继续复核当前候选",
        )
        suspended = await wait_for_status(runtime, started["id"], {"suspended", "failed"})
        assert suspended["status"] == RUN_SUSPENDED
        issue_id = next(iter(runtime.store.technical_recoveries(started["id"])))

        resumed = await runtime.resume_issue(
            project_id="project_confirmation_review_resume",
            token="token_2",
            issue_id=issue_id,
            action_id="retry_from_latest",
        )
        assert resumed["mode"] == "checkpoint_resume"
        assert resumed["run"]["id"] == started["id"]

        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting"
        interaction = runtime.store.pending_interaction("project_confirmation_review_resume")
        assert interaction is not None and interaction.kind == "confirmation"
        assert [call["tool"] for call in gateway.calls].count("list_domain_resources") == 1
        assert runtime.store.technical_recoveries(started["id"]) == {}
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


async def test_release_confirmation_executes_core_release_once_after_approval(settings) -> None:
    model = ScriptedModel(
        [
            [ToolTurn("request_release", {"summary": "确认 19 条真实用例并发布"})],
            [],
        ],
        final="1.0.0 已正式发布。",
    )
    gateway = FakeGateway({
        "release_preflight": {
            "ready": True,
            "version": "1.0.0",
            "testcase_count": 19,
            "questions": ["月度排产安全校验"],
        },
        "request_release": {
            "released": True,
            "version": "1.0.0",
            "revision": "stable-revision",
            "display": {"text": "1.0.0 已正式发布"},
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_release",
            token="token_1",
            message="发布 1.0.0",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_release")
        assert interaction is not None and interaction.kind == "release_confirmation"
        assert runtime.store.tool_receipt(
            started["id"],
            interaction.tool_call_id,
            "request_release",
            {"summary": "确认 19 条真实用例并发布"},
        ) is None
        completed_release_events = []
        for event in runtime.store.events_after("project_release", 0):
            if event.kind != "tool":
                continue
            if event.payload.get("name") != "request_release":
                continue
            if event.payload.get("phase") == "completed":
                completed_release_events.append(event)
        assert not completed_release_events

        await runtime.answer_interaction(
            project_id="project_release",
            interaction_id=interaction.id,
            token="token_2",
            answer={
                "approved": True,
                "coverage_confirmed": True,
                "version": "1.0.0",
            },
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_release") is None
        release_calls = [call for call in gateway.calls if call["tool"] == "request_release"]
        assert len(release_calls) == 1
        assert release_calls[0]["arguments"]["version"] == "1.0.0"
        assert release_calls[0]["arguments"]["review"]["coverage_confirmed"] is True
        receipt = runtime.store.tool_receipt(
            started["id"],
            interaction.tool_call_id,
            "request_release",
            {"summary": "确认 19 条真实用例并发布"},
        )
        assert receipt is not None and receipt["released"] is True
        completed_release_events = []
        for event in runtime.store.events_after("project_release", 0):
            if event.kind != "tool":
                continue
            if event.payload.get("name") != "request_release":
                continue
            if event.payload.get("phase") == "completed":
                completed_release_events.append(event)
        assert len(completed_release_events) == 1
    finally:
        await runtime.shutdown()


async def test_release_failure_keeps_confirmation_pending(settings) -> None:
    model = ScriptedModel([
        [ToolTurn("request_release", {"summary": "确认 19 条真实用例并发布"})],
    ])
    gateway = FakeGateway({
        "release_preflight": {
            "ready": True,
            "version": "1.0.0",
            "testcase_count": 19,
        },
        "request_release": {
            "error": "RELEASE_WRITE_FAILED",
            "reason": "稳定版本写入失败",
            "next": ["保持候选版本并重试"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_release_failure",
            token="token_1",
            message="发布 1.0.0",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_release_failure")
        assert interaction is not None

        with pytest.raises(AgentRuntimeError) as raised:
            await runtime.answer_interaction(
                project_id="project_release_failure",
                interaction_id=interaction.id,
                token="token_2",
                answer={
                    "approved": True,
                    "coverage_confirmed": True,
                    "version": "1.0.0",
                },
            )

        assert raised.value.code == "RELEASE_WRITE_FAILED"
        assert runtime.store.require_run(started["id"]).status == "waiting"
        pending = runtime.store.pending_interaction("project_release_failure")
        assert pending is not None and pending.id == interaction.id
        assert pending.payload["lastSubmissionError"] == "稳定版本写入失败"
        assert [call["tool"] for call in gateway.calls].count("request_release") == 1
        assert runtime.store.tool_receipt(
            started["id"],
            interaction.tool_call_id,
            "request_release",
            {"summary": "确认 19 条真实用例并发布"},
        ) is None
    finally:
        await runtime.shutdown()


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


def test_plan_progress_absorbed_from_core_result() -> None:
    live = LiveRun(run_id="run_plan", project_id="project_plan", token="token")
    _absorb_plan_progress(live, {"plan": {
        "remaining_count": 3,
        "tasks": [
            {"title": "同步 Schema 对象", "required": True, "status": "accepted"},
            {"title": "政府采购订单融资规则", "required": True, "status": "pending"},
            {"title": "公务卡免息还款期", "required": True, "status": "pending"},
        ],
    }})
    assert live.situation.plan_remaining == 3
    assert live.situation.plan_next_task == "政府采购订单融资规则"

    # 计划做完后必须清零，否则轮次永远收不了尾。
    _absorb_plan_progress(live, {"plan": {"remaining_count": 0, "tasks": []}})
    assert live.situation.plan_remaining == 0
    assert live.situation.plan_next_task == ""

    # 不带计划快照的结果不能改动已记录的进度。
    live.situation.plan_remaining = 2
    _absorb_plan_progress(live, {"ok": True})
    assert live.situation.plan_remaining == 2


def test_autonomous_continuation_prompt_names_next_plan_task(settings) -> None:
    runtime = AgentService(settings)
    runtime.store.create_run(RunRecord(
        id="run_plan_prompt",
        project_id="project_plan_prompt",
        session_id="session_plan_prompt",
        status=RUN_RUNNING,
        prompt="继续执行建模计划",
        token_fingerprint="fingerprint",
    ))
    live = LiveRun(run_id="run_plan_prompt", project_id="project_plan_prompt", token="token")
    live.situation.plan_remaining = 4
    live.situation.plan_next_task = "政府采购订单融资规则"

    prompt = runtime._autonomous_continuation_prompt(  # noqa: SLF001
        "project_plan_prompt", live, stalled=2,
    )
    assert "4 项必选任务" in prompt
    assert "政府采购订单融资规则" in prompt
    assert "连续 2 轮" in prompt

    # 没有待办时不应凭空出现计划段落。
    live.situation.plan_remaining = 0
    live.situation.plan_next_task = ""
    clean = runtime._autonomous_continuation_prompt(  # noqa: SLF001
        "project_plan_prompt", live, stalled=0,
    )
    assert "受控建模计划待办" not in clean


def test_full_read_intent_ignores_assistant_question_text() -> None:
    """#1：助手问题文案复述「需要全文精读」时，不得强制拉起全文精读。"""
    from modeling_agent.service import _required_tools_for_user_intent

    assistant_question = "已将《资料.docx》设为通用业务依据，需要全文精读的资料继续按已保存设置执行"
    answer_text = '{"choice": "confirm_all"}'

    required = _required_tools_for_user_intent(
        f"{assistant_question}\n{answer_text}",
        full_read_text=answer_text,
    )
    assert "read_documents_fully" not in required

    # 专家自己要求精读时仍然必须拉起
    user_answer = '{"note": "请对这份文档做全文精读"}'
    required_by_user = _required_tools_for_user_intent(
        f"{assistant_question}\n{user_answer}",
        full_read_text=user_answer,
    )
    assert "read_documents_fully" in required_by_user
    assert "set_material_directive" in required_by_user

    # 不传 full_read_text 时保持原行为，其它调用点不受影响
    assert "read_documents_fully" in _required_tools_for_user_intent("请全文精读这份文档")


def test_ambiguous_tokens_survive_english_prose() -> None:
    """#16：普通英文单词不得被术语替换改成病句，中文语境下仍要替换。"""
    from modeling_agent.visible_text import VisibleTextSanitizer

    def clean(text: str) -> str:
        sanitizer = VisibleTextSanitizer()
        return sanitizer.feed(text) + sanitizer.finish()

    english = "I will ask the user to confirm; the revision is stable and the diff is small"
    assert clean(english) == english

    chinese = clean("调用 ask 生成问题卡，再看 revision 和 diff")
    assert "业务问题" in chinese and "版本" in chinese and "查看变更对照" in chinese

    # 明确的内部工具名无论中英文语境都必须脱敏
    assert "全文精读资料" in clean("calling read_documents_fully now")


def test_reasoning_segments_are_separated() -> None:
    """#6：多轮推理拼进同一条消息时要补断句，不能粘成一句话。"""
    from modeling_agent.service import _segment_separator

    assert _segment_separator("执行全文精读。") == "\n\n"
    assert _segment_separator("已经换行了\n") == ""
    assert _segment_separator("") == ""


def test_language_reminder_appended_once() -> None:
    """#4：每轮 query 末尾就近提醒输出语言，且不重复追加。"""
    from modeling_agent.service import _with_language_reminder

    once = _with_language_reminder("请继续推进建模计划。")
    assert "【输出语言】" in once
    assert once.count("【输出语言】") == 1
    # 已带提醒的 query 不再叠加
    assert _with_language_reminder(once) == once
    # 非字符串 query 原样返回
    payload = {"query": "x"}
    assert _with_language_reminder(payload) is payload
    assert _with_language_reminder("") == ""


def test_schema_semantic_field_hash_is_optional() -> None:
    """#32：字段级乐观锁必须可选，否则一次成功写入会让剩余封条整批作废。"""
    from modeling_agent.tools import SCHEMA_SEMANTIC_ACTIONS

    update_field = next(
        item for item in SCHEMA_SEMANTIC_ACTIONS["items"]["oneOf"]
        if "update_field" in item["properties"].get("op", {}).get("enum", [])
    )
    assert "expected_field_hash" not in update_field["required"]
    # 仍然允许传，只是不强制
    assert "expected_field_hash" in update_field["properties"]
    assert update_field["required"] == ["op", "field_key", "changes"]


def test_schema_semantic_batch_guidance_is_actionable() -> None:
    """#31：工具描述要给模型真能产出的批量规模，而不是只写硬上限。"""
    from modeling_agent.tools import CORE_TOOL_SPECS

    spec = next(item for item in CORE_TOOL_SPECS if item.name == "stage_schema_semantic_changes")
    assert "建议单批不超过 3 个对象、60 个字段" in spec.description
    assert "截断" in spec.description
    assert "expected_field_hash 可以省略" in spec.description


def test_full_read_requirement_follows_saved_reading_mode() -> None:
    """#1：没有任何资料设为全文精读时，文本里的「全文精读」不得强推该工具。"""
    from modeling_agent.service import (
        LiveRun, _absorb_reading_modes, _required_tools_for_user_intent,
    )

    live = LiveRun(run_id="run_r", project_id="project_r", token="token")
    assert live.situation.materials_observed is False
    text = "请全文精读这份文档"

    # 还没观察到资料设置时保持原状，避免吞掉用户真实诉求
    notes: list[str] = []
    assert "read_documents_fully" in _required_tools_for_user_intent(
        text, situation=live.situation, notes=notes,
    )
    assert notes == []

    # 观察到全部为 on_demand → 撤掉强制精读，并留下可执行的说明
    _absorb_reading_modes(live, {"documents": [
        {"filename": "导则.md", "readingMode": "on_demand"},
        {"filename": "规则.md", "readingMode": "on_demand"},
    ]})
    assert live.situation.materials_observed is True
    assert live.situation.full_read_materials == ()
    notes = []
    required = _required_tools_for_user_intent(text, situation=live.situation, notes=notes)
    assert "read_documents_fully" not in required
    assert notes and "set_material_directive" in notes[0]

    # 确有 full 资料时仍然强制
    _absorb_reading_modes(live, {"documents": [
        {"filename": "导则.md", "readingMode": "full"},
        {"filename": "规则.md", "readingMode": "on_demand"},
    ]})
    assert live.situation.full_read_materials == ("导则.md",)
    required = _required_tools_for_user_intent(
        text, situation=live.situation,
    )
    assert "read_documents_fully" in required
    assert "set_material_directive" not in required

    _absorb_reading_modes(live, {
        "documents": [{"filename": "导则.md", "id": "doc-1", "readingMode": "full"}],
        "coverage": [{"filename": "导则.md", "documentId": "doc-1", "complete": True}],
    })
    assert live.situation.has_completed_full_read() is True
    assert "read_documents_fully" not in _required_tools_for_user_intent(
        text, situation=live.situation,
    )


def test_full_read_prohibition_and_completed_result_override_positive_wording() -> None:
    from modeling_agent.service import _required_tools_for_user_intent

    prohibited = _required_tools_for_user_intent(
        "本轮需要产品台账，但禁止再次全文精读，复用已经完成的全文分析结果；不要修改知识包。",
    )
    assert "read_documents_fully" not in prohibited
    assert "set_material_directive" not in prohibited
    satisfied = _required_tools_for_user_intent(
        "全文精读已经完成，请复用已完成结果继续整理产品台账。",
    )
    assert "read_documents_fully" not in satisfied


def test_full_read_completion_is_invalidated_when_document_revision_changes() -> None:
    from modeling_agent.service import LiveRun, _absorb_reading_modes, _required_tools_for_user_intent

    live = LiveRun(run_id="run_revision", project_id="project_revision", token="token")
    _absorb_reading_modes(live, {
        "documents": [{
            "id": "doc-1", "filename": "synthetic.md", "readingMode": "full",
            "revisionFingerprint": "doc-1:1:hash-a",
        }],
        "coverage": [{
            "documentId": "doc-1", "filename": "synthetic.md",
            "revisionFingerprint": "doc-1:1:hash-a", "complete": True,
        }],
    })
    assert "read_documents_fully" not in _required_tools_for_user_intent(
        "请全文精读 synthetic.md", situation=live.situation,
    )

    _absorb_reading_modes(live, {"documents": [{
        "id": "doc-1", "filename": "synthetic.md", "readingMode": "full",
        "revisionFingerprint": "doc-1:2:hash-b",
    }]})
    assert live.situation.has_completed_full_read() is False
    assert "read_documents_fully" in _required_tools_for_user_intent(
        "请全文精读 synthetic.md", situation=live.situation,
    )


def test_required_text_receipt_reports_missing_exact_constraints() -> None:
    from modeling_agent.service import _required_text_constraint_status

    constraint = {"text": "保留 198 条与 3 组归并，不修改知识包"}
    acknowledged, missing = _required_text_constraint_status(
        "已按补充说明处理，保留 198 条与 3 组归并，知识包保持不变。", constraint,
    )
    assert acknowledged is True
    assert missing == []
    acknowledged, missing = _required_text_constraint_status("已按要求处理。", constraint)
    assert acknowledged is False
    assert "count:198" in missing and "count:3" in missing
    assert "preserve_knowledge_package" in missing


def test_purpose_review_continuation_avoids_full_read_wording() -> None:
    """#1：前端代发的续跑文案不得命中全文精读意图。"""
    import re
    from pathlib import Path
    from modeling_agent.service import _FULL_READ_INTENT

    store = Path(__file__).resolve().parents[2] / "frontend/src/workbench/lib/appStore.js"
    text = store.read_text(encoding="utf-8")
    match = re.search(r"const PURPOSE_REVIEW_CONTINUATION = '([^']+)'", text)
    assert match, "没有找到续跑文案常量"
    assert not _FULL_READ_INTENT.search(match.group(1))


def test_stage_transition_counts_as_progress() -> None:
    """#35：切换阶段是前进；刚进新阶段不能被当成无事可做。"""
    from modeling_agent.service import (
        AUTONOMOUS_PROGRESS_TOOLS, LiveRun, _absorb_stage_delivery,
    )

    assert "transition_project_state" in AUTONOMOUS_PROGRESS_TOOLS

    live = LiveRun(run_id="run_s", project_id="project_s", token="token")
    # 默认不给特殊待遇，不改变既有收尾节奏
    assert live.situation.stage_started_without_delivery is False

    # 切到新阶段 → 新阶段尚无产出，放宽停滞上限
    _absorb_stage_delivery(live, "transition_project_state", {})
    assert live.situation.stage_started_without_delivery is True

    # 只读工具不算产出，仍然保持放宽
    _absorb_stage_delivery(live, "gate_status", {})
    assert live.situation.stage_started_without_delivery is True

    # 新阶段有写入 → 回到常规节奏
    _absorb_stage_delivery(live, "write_file", {})
    assert live.situation.stage_started_without_delivery is False


def test_loop_detection_distinguishes_stuck_from_quiet() -> None:
    """打转和没产出是两回事：反复同参调用、反复同样输出才是卡住。"""
    from modeling_agent.service import (
        LiveRun, _detect_loop, _record_tool_call, _record_turn_output,
    )

    live = LiveRun(run_id="run_l", project_id="project_l", token="token")
    assert _detect_loop(live) == ""

    # 不同参数的连续只读调用不算打转——那是正常取证
    for key in ("a", "b", "c", "d"):
        _record_tool_call(live, "search_documents", {"q": key})
    assert _detect_loop(live) == ""

    # 同工具同参数连续三次才算
    for _ in range(3):
        _record_tool_call(live, "gate_status", {})
    assert "相同参数调用同一工具" in _detect_loop(live)

    # 输出侧：同一段内容反复出现
    other = LiveRun(run_id="run_o", project_id="project_o", token="token")
    for _ in range(3):
        _record_turn_output(other, "让我重新理解用户的意思。")
    assert "重复同样的内容" in _detect_loop(other)

    # 空输出不参与判定
    blank = LiveRun(run_id="run_b", project_id="project_b", token="token")
    for _ in range(5):
        _record_turn_output(blank, "   ")
    assert _detect_loop(blank) == ""


def test_intent_rule_precondition_is_three_valued() -> None:
    """#1 根治：文本只提供线索，状态拥有否决权，拿不准时按规则声明的默认走。"""
    from modeling_agent.service import RunSituation, Verdict, _FULL_READ_RULE

    unknown = RunSituation()
    assert unknown.has_full_read_materials() is Verdict.UNKNOWN
    # 精读规则拿不准时放行：宁可多读一次，不能吞掉用户诉求
    assert _FULL_READ_RULE.unknown_default is Verdict.ALLOW
    assert _FULL_READ_RULE.verdict(unknown) is Verdict.ALLOW

    observed = RunSituation(materials_observed=True)
    assert observed.has_full_read_materials() is Verdict.DENY
    assert _FULL_READ_RULE.verdict(observed) is Verdict.DENY

    has_full = RunSituation(materials_observed=True, full_read_materials=("a.md",))
    assert _FULL_READ_RULE.verdict(has_full) is Verdict.ALLOW


def test_denied_intent_note_reaches_the_prompt() -> None:
    """否决必须留痕：说明要并进下一次 query，并且只消费一次。"""
    from modeling_agent.service import LiveRun, _with_intent_notes

    live = LiveRun(run_id="run_n", project_id="project_n", token="token")
    assert _with_intent_notes(live, "继续") == "继续"

    live.intent_notes.append("当前没有资料设为全文精读。")
    merged = _with_intent_notes(live, "继续")
    assert "【状态核对】" in merged and "全文精读" in merged
    # 消费一次即清空，不会每轮重复叠加
    assert live.intent_notes == []
    assert _with_intent_notes(live, "继续") == "继续"


def test_batch_purpose_confirmation_settles_pending_cards(tmp_path) -> None:
    """#36：批量确认成交后，助手挂起的逐份用途卡必须一并结算。

    只解开数据侧、留着交互侧，那张卡会被当成「仍未解决的原问题」自动恢复，
    用户刚一次性确认完又被推回同一张卡。
    """
    from modeling_agent.schemas import RUN_QUEUED, InteractionRecord, RunRecord
    from modeling_agent.store import RuntimeStore

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    run = store.create_run(RunRecord(
        id="run_purpose",
        project_id="project_p",
        session_id="project:project_p:run:run_purpose",
        status=RUN_QUEUED,
        prompt="上传资料",
        token_fingerprint="111111111111",
    ))
    interaction = store.create_interaction(InteractionRecord(
        id="ix_purpose_1",
        run_id=run.id,
        project_id="project_p",
        tool_call_id="call_1",
        kind="ask",
        payload={"question": "第一份资料的用途是？"},
    ))
    assert store.pending_interaction("project_p") is not None

    settled = store.settle_interactions_resolved_elsewhere(
        run.id, reason="资料用途已通过批量确认完成",
    )
    assert [item.id for item in settled] == [interaction.id]
    # 结算后不再是待处理，不会被自动恢复
    assert store.pending_interaction("project_p") is None
    assert store.get_interaction(interaction.id).status == "cancelled"

    # 幂等：没有挂起项时返回空，不报错
    assert store.settle_interactions_resolved_elsewhere(run.id, reason="再来一次") == []


def test_loop_detection_catches_paraphrased_repeats() -> None:
    """#39：真实的打转是「换个说法再说一遍」，只比对逐字相同会全部漏掉。"""
    from modeling_agent.service import LiveRun, _detect_loop, _record_turn_output

    live = LiveRun(run_id="run_p", project_id="project_p", token="token")
    _record_turn_output(live, "请您前往评测界面完成以下两步：录入真实场景用例、配置数据源。")
    _record_turn_output(live, "请您在评测界面完成以下两项：录入真实场景用例，以及配置数据源。")
    _record_turn_output(live, "请您到评测界面完成这两步：录入真实场景用例、配置数据源即可。")
    assert "重复同样的内容" in _detect_loop(live)

    # 内容真的不同就不该判为打转
    other = LiveRun(run_id="run_q", project_id="project_q", token="token")
    _record_turn_output(other, "已同步 9 个物理对象，接下来补全字段业务语义。")
    _record_turn_output(other, "字段语义补全完成，现在开始创建营销决策规则。")
    _record_turn_output(other, "规则创建完成，接下来生成业务操作和确定性脚本。")
    assert _detect_loop(other) == ""


def test_awaiting_user_action_settles_the_turn() -> None:
    """#39：剩余事项只能由用户在别的界面完成时，应结束本轮而不是反复宣告。"""
    from modeling_agent.service import LiveRun, _absorb_awaiting_user

    live = LiveRun(run_id="run_w", project_id="project_w", token="token")
    assert live.situation.awaiting_user_action is False

    _absorb_awaiting_user(live, {
        "gate": "ready_for_eval",
        "awaitingUser": True,
        "blockers": ["先在评测界面录入至少 1 条用户真实场景用例", "先在评测界面完成数据源配置"],
    })
    assert live.situation.awaiting_user_action is True
    assert "录入" in live.situation.awaiting_user_reason

    # 阻塞项被清掉后恢复正常轮转
    _absorb_awaiting_user(live, {"gate": "ready_for_eval", "awaitingUser": False, "blockers": []})
    assert live.situation.awaiting_user_action is False
    assert live.situation.awaiting_user_reason == ""

    # 不带该字段的结果不改变状态
    live.situation.awaiting_user_action = True
    _absorb_awaiting_user(live, {"gate": "releasable", "blockers": []})
    assert live.situation.awaiting_user_action is True


def test_purpose_batch_confirmation_is_not_treated_as_interjection() -> None:
    """#36：批量确认的续跑消息走的是插话分支，会把逐份卡标记为待恢复。

    它不是插话，而是同一个问题的答案：必须给出真实答案让在途的 ask 返回，
    同时不设置 restore_interaction，否则卡片会被自动恢复推回给用户。
    """
    import inspect
    from modeling_agent.service import AgentService

    source = inspect.getsource(AgentService.steer)
    assert "resolved_elsewhere = purpose_review_completed" in source
    assert '"action": "resolved_elsewhere" if resolved_elsewhere else "interject"' in source
    assert "restore_interaction=None if resolved_elsewhere else interaction" in source


def test_repeated_tool_failure_counts_as_loop() -> None:
    """#40：同一工具反复失败，参数每次都变，「同参数重复」判据完全失效。

    现场是 put_goal_section 连续 22 次 400——报错只说哪个字段不对、
    不说这一段允许什么，助手只能一次次猜。
    """
    from modeling_agent.service import (
        LOOP_TOOL_FAILURE_THRESHOLD, LiveRun, _detect_loop, _record_tool_outcome,
    )

    live = LiveRun(run_id="run_f", project_id="project_f", token="token")
    for _ in range(LOOP_TOOL_FAILURE_THRESHOLD - 1):
        _record_tool_outcome(live, "put_goal_section", True)
    assert _detect_loop(live) == ""

    _record_tool_outcome(live, "put_goal_section", True)
    assert "put_goal_section 连续失败" in _detect_loop(live)

    # 成功一次就清零，正常的自我纠错不受影响
    _record_tool_outcome(live, "put_goal_section", False)
    assert _detect_loop(live) == ""

    # 换了工具重新计数，不会把不同工具的失败累加
    for _ in range(LOOP_TOOL_FAILURE_THRESHOLD - 1):
        _record_tool_outcome(live, "put_goal_section", True)
    _record_tool_outcome(live, "create_rule", True)
    assert _detect_loop(live) == ""


async def test_approved_plan_execution_intent_requires_plan_chain() -> None:
    chain = {"stage_change_set", "validate_change_set", "request_confirmation"}
    approval_messages = [
        "我已在页面批准当前建模计划。请继续执行已批准的建模计划：完成候选预检，候选修订写入后执行完整校验，校验通过后生成统一确认卡；不要重复已经完成的任务。",
        "我已在页面批准当前建模计划。请从计划第一项继续，先核对当前状态和写入回执，不要重复已经完成的任务。",
        "请按已批准的 5 项修复计划继续执行，完成候选预检、完整校验并生成确认卡",
        "批准这份计划，开始执行",
    ]
    for message in approval_messages:
        assert chain <= _required_tools_for_user_intent(message), message
    rejected = _required_tools_for_user_intent(
        "我已在页面拒绝当前建模计划。请读取计划的最终状态并停止这份计划，不要产生任何候选修改"
    )
    assert chain.isdisjoint(rejected)
    non_intent = [
        "不要按已批准计划继续执行",
        "不是根据已批准计划推进",
        "确认这份计划已完成",
        "检查这份计划是否已经完成",
    ]
    for message in non_intent:
        assert chain.isdisjoint(_required_tools_for_user_intent(message)), message


async def test_progress_only_final_text_detection() -> None:
    from modeling_agent.service import _progress_only_final_text

    assert _progress_only_final_text("正在执行候选预检，下一步展示统一确认卡")
    assert _progress_only_final_text("下一步将展示统一确认卡")
    assert not _progress_only_final_text("确认卡已生成，等待您确认。")


async def test_approved_plan_progress_text_is_not_settled_completed(settings) -> None:
    model = ScriptedModel(
        [[]],
        final="正在执行候选预检，下一步展示统一确认卡",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_approved_plan_progress",
            token="token_approved_plan_progress",
            message="请按已批准的 5 项修复计划继续执行，完成候选预检、完整校验并生成确认卡",
        )
        settled = await wait_for_status(runtime, started["id"], {"waiting"}, timeout=15)
        assert settled["status"] == "waiting", settled
        interaction = runtime.store.pending_interaction("project_approved_plan_progress")
        assert interaction is not None
        assert set(interaction.payload["missingRequiredTools"]) == APPROVED_PLAN_REQUIRED_TOOLS
        events = runtime.store.events_after("project_approved_plan_progress", 0)
        assert not any(
            event.kind == "status" and event.payload.get("text") == "本轮完成"
            for event in events
        )
    finally:
        await runtime.shutdown()


async def test_progress_only_text_without_approved_plan_settles_normally(settings) -> None:
    model = ScriptedModel(
        [[]],
        final="正在执行候选预检，下一步展示统一确认卡",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_plain_progress",
            token="token_plain_progress",
            message="继续处理当前内容",
        )
        settled = await wait_for_status(
            runtime, started["id"], {"completed", "failed", "suspended"}, timeout=15,
        )
        assert settled["status"] == "completed", settled
    finally:
        await runtime.shutdown()


def test_completed_tool_names_restores_observed_tools(tmp_path) -> None:
    from modeling_agent.store import RuntimeStore

    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.append_event(
        "project_restore_tools", "run_restore_tools", "tool",
        {"name": "analyze_deterministic_capabilities", "phase": "completed"},
    )
    store.append_event(
        "project_restore_tools", "run_restore_tools", "tool",
        {"name": "read_document", "phase": "failed"},
    )
    assert store.completed_tool_names("run_restore_tools") == {"analyze_deterministic_capabilities"}


async def test_approved_plan_plain_text_is_not_settled_completed(settings) -> None:
    model = ScriptedModel(
        [[]],
        final="修改计划已处理完毕，本轮结束。",
    )
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_approved_plan_plain_text",
            token="token_approved_plan_plain_text",
            message=("我已在页面批准当前建模计划。请从计划第一项继续，先核对当前状态和写入回执，不要重复已经完成的任务。"),
        )
        settled = await wait_for_status(runtime, started["id"], {"waiting"}, timeout=15)
        assert settled["status"] == "waiting", settled
        interaction = runtime.store.pending_interaction("project_approved_plan_plain_text")
        assert interaction is not None
        assert set(interaction.payload["missingRequiredTools"]) == APPROVED_PLAN_REQUIRED_TOOLS
    finally:
        await runtime.shutdown()


async def test_rejected_confirmation_requires_completion_chain(settings) -> None:
    groups = [{"id": "goal", "tier": "caliber", "title": "建模目标确认", "impact": []}]
    model = ScriptedModel(
        [[ToolTurn("request_confirmation", {"summary": "确认建模目标"})], []],
        final="已按反馈调整完毕。",
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
        "request_confirmation_rejected": {
            "rejected": True,
            "candidateRevision": "candidate_goal",
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_rejected_confirmation_chain",
            token="token_confirm_1",
            message="开始",
        )
        await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        interaction = runtime.store.pending_interaction("project_rejected_confirmation_chain")
        assert interaction is not None
        await runtime.answer_interaction(
            project_id="project_rejected_confirmation_chain",
            interaction_id=interaction.id,
            token="token_confirm_2",
            answer={"approved": False, "feedback": "补充来源依据后重新生成候选"},
        )
        settled = await wait_for_status(
            runtime, started["id"], {"completed", "failed", "suspended", "waiting"}, timeout=15,
        )
        assert settled["status"] != "completed", settled
    finally:
        await runtime.shutdown()


async def test_scope_confirmation_without_analysis_is_rejected(settings) -> None:
    question = {
        "question": "4 个操作是否需要生成自动化脚本？",
        "kind": "scope_confirmation",
        "options": [
            {"label": "本轮不生成脚本", "description": "操作作为业务规范文档使用", "value": "no_scripts"},
            {
                "label": "调整范围",
                "description": "指定需要生成脚本的操作名称",
                "value": "custom",
                "requiresText": True,
                "textPrompt": "请列出操作名称",
            },
        ],
    }
    model = ScriptedModel([[ToolTurn("ask", question)]], final="本轮未形成脚本范围决定，已停止。")
    runtime = service(settings, model)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_scope_gate_blocked",
            token="token_scope_gate",
            message="进入验证阶段，确认脚本范围",
        )
        completed = await wait_for_status(runtime, started["id"], {"completed", "failed"})
        assert completed["status"] == "completed", completed["error"]
        assert runtime.store.pending_interaction("project_scope_gate_blocked") is None
        events = runtime.store.events_after("project_scope_gate_blocked", 0)
        assert not any(event.kind == "interaction" for event in events)
    finally:
        await runtime.shutdown()


async def test_scope_confirmation_allowed_after_analysis(settings) -> None:
    question = {
        "question": "4 个操作是否需要生成自动化脚本？",
        "kind": "scope_confirmation",
        "options": [
            {"label": "为 2 个操作生成脚本", "description": "基于分析结论的推荐范围", "value": "recommended"},
            {"label": "本轮不生成脚本", "description": "操作作为业务规范文档使用", "value": "no_scripts"},
            {
                "label": "调整范围",
                "description": "指定需要生成脚本的操作名称",
                "value": "custom",
                "requiresText": True,
                "textPrompt": "请列出操作名称",
            },
        ],
    }
    model = ScriptedModel([
        [ToolTurn("analyze_deterministic_capabilities")],
        [ToolTurn("ask", question)],
    ], final="等待专家决定。")
    runtime = service(settings, model)
    await runtime.startup()
    try:
        state = runtime.store.project_state("project_scope_gate_allowed")
        runtime.store.transition_project_state(
            "project_scope_gate_allowed",
            expected_revision=state.revision,
            next_state="validation:completed",
            cause="test_fixture",
            actor_run_id="",
        )
        started = await runtime.start_run(
            project_id="project_scope_gate_allowed",
            token="token_scope_gate",
            message="进入验证阶段，确认脚本范围",
        )
        waiting = await wait_for_status(runtime, started["id"], {"waiting", "failed"})
        assert waiting["status"] == "waiting", waiting["error"]
        interaction = runtime.store.pending_interaction("project_scope_gate_allowed")
        assert interaction is not None
        assert interaction.kind == "scope_confirmation"
        assert interaction.payload["question"] == question["question"]
    finally:
        await runtime.shutdown()


async def test_repeated_core_tool_failure_aborts_during_tool_callback() -> None:
    from modeling_agent.service import AgentService, LOOP_TOOL_FAILURE_THRESHOLD, LiveRun

    live = LiveRun(run_id="run_f", project_id="project_f", token="token")
    for index in range(LOOP_TOOL_FAILURE_THRESHOLD - 1):
        await AgentService._after_core_tool(
            None, live, "put_goal_section", {"request_id": f"request_{index}"}, {"error": "INVALID_INPUT"},
        )
    with pytest.raises(AgentRuntimeError) as caught:
        await AgentService._after_core_tool(
            None, live, "put_goal_section", {"request_id": "request_final"}, {"error": "INVALID_INPUT"},
        )
    assert caught.value.code == "AGENT_TOOL_LOOP_DETECTED"
    assert "put_goal_section 连续失败" in caught.value.message
