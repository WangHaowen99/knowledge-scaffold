from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import modeling_agent.service as service_module
from modeling_agent.errors import AgentRuntimeError
from modeling_agent.events import EventHub
from modeling_agent.rails import ActivityRail, ToolBoundaryRail, WriteSafetyRail, _phase_write_problem, check_write_safety
from modeling_agent.schemas import InteractionRecord, RUN_QUEUED, RUN_RUNNING, RunRecord
from modeling_agent.store import RuntimeStore
from modeling_agent.tools import ToolContext
from conftest import FakeGateway, ScriptedModel, service


def run(run_id: str, project_id: str = "project_demo") -> RunRecord:
    return RunRecord(
        id=run_id,
        project_id=project_id,
        session_id=f"project:{project_id}",
        status=RUN_QUEUED,
        prompt="开始建模",
        token_fingerprint="000000000000",
    )


def test_store_enforces_one_active_run_and_recovers_checkpoint(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        store.create_run(run("run_1"))
        queued = store.project_state("project_demo")
        assert queued.state == "intake:queued"
        assert queued.run_id == "run_1"
        with pytest.raises(AgentRuntimeError) as caught:
            store.create_run(run("run_2"))
        assert caught.value.code == "RUN_ACTIVE"

        store.update_run("run_1", status=RUN_RUNNING)
        assert store.project_state("project_demo").state == "intake:running"
        recovered = store.recover_interrupted()
        assert [item.id for item in recovered] == ["run_1"]
        assert store.require_run("run_1").status == "suspended"
        state = store.project_state("project_demo")
        assert state.state == "intake:suspended"
        assert state.revision == 3
        transitions = store._db.execute(  # noqa: SLF001 - persistence invariant test
            "SELECT from_state, to_state FROM project_agent_state_transitions ORDER BY id"
        ).fetchall()
        assert [(row["from_state"], row["to_state"]) for row in transitions] == [
            ("intake:idle", "intake:queued"),
            ("intake:queued", "intake:running"),
            ("intake:running", "intake:suspended"),
        ]
    finally:
        store.close()


def test_completed_tool_receipt_survives_restart_and_replays_once(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_1"))
    result = {"ok": True, "revision": "candidate-1", "display": {"text": "候选已保存"}}
    store.save_tool_receipt(
        "run_1",
        "call_write_1",
        "write_file",
        {"path": "objects/unit.md", "content": "# Unit"},
        result,
    )
    store.close()

    reopened = RuntimeStore(path)
    try:
        assert reopened.tool_receipt(
            "run_1",
            "call_write_1",
            "write_file",
            {"path": "objects/unit.md", "content": "# Unit"},
        ) == result
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_tool_boundary_rail_returns_receipt_instead_of_calling_tool_again(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.create_run(run("run_1"))
    arguments = {"path": "objects/unit.md", "content": "# Unit"}
    result = {"ok": True, "display": {"text": "候选已保存"}}
    store.save_tool_receipt(
        "run_1", "call_goal_evidence", "write_file", arguments, result,
    )
    context = ToolContext(
        project_id="project_demo",
        run_id="run_1",
        token="",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        tool_receipt_lookup=lambda name, call_id, args: store.tool_receipt(
            "run_1", call_id, name, args,
        ),
    )
    rail = ToolBoundaryRail(context)
    call = _write_call_context(arguments)
    try:
        await rail.before_tool_call(call)  # type: ignore[arg-type]
        assert json.loads(call.inputs.tool_result) == result
        assert call.extra["_tool_receipt_replayed"] is True
    finally:
        store.close()


def test_pending_interaction_is_reflected_only_by_project_state(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        store.create_run(run("run_1"))
        store.update_run("run_1", status=RUN_RUNNING)
        interaction = InteractionRecord(
            id="interaction_1",
            project_id="project_demo",
            run_id="run_1",
            tool_call_id="call_1",
            kind="scope_confirmation",
            payload={"question": "范围是否准确？"},
        )
        store.create_interaction(interaction)
        waiting = store.project_state("project_demo")
        assert waiting.state == "intake:waiting"
        assert waiting.interaction_id == interaction.id
        pending = store.pending_interaction("project_demo")
        assert pending is not None and pending.id == interaction.id

        store.answer_interaction(interaction.id, {"value": "yes"})
        store.update_run("run_1", status=RUN_QUEUED, interaction_id="")
        resumed = store.project_state("project_demo")
        assert resumed.state == "intake:queued"
        assert resumed.interaction_id == ""
        assert store.pending_interaction("project_demo") is None
    finally:
        store.close()


def test_latest_activity_can_be_restored_for_running_agent(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]))
    try:
        runtime.store.create_run(run("run_progress", "project_progress"))
        runtime.store.update_run("run_progress", status=RUN_RUNNING)
        runtime.store.append_event("project_progress", "run_progress", "progress", {
            "activityId": "full-read-progress",
            "text": "正在全文精读《工艺规程.docx》",
            "detail": "8/12 个处理检查点完成",
            "counters": {"progress": 67, "current": 8, "total": 12},
        })
        runtime.store.append_event("project_progress", "run_progress", "focus", {
            "target": {
                "kind": "flow",
                "target": "diagrams/process.mmd",
                "node": "D3",
            },
        })

        state = runtime.project_state("project_progress")

        assert state["activity"]["activityId"] == "full-read-progress"
        assert state["activity"]["counters"]["progress"] == 67
        assert state["activity"]["createdAt"]
        assert state["focus"] == {
            "kind": "flow",
            "target": "diagrams/process.mmd",
            "node": "D3",
        }
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_event_hub_replays_before_following_live_events(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    hub = EventHub(store)
    try:
        first = hub.publish("project_demo", "run_1", "focus", {"target": {"kind": "flow", "target": "diagrams/demo.mmd"}})
        stream = hub.stream("project_demo", 0)
        replayed = await anext(stream)
        assert replayed is not None and replayed.seq == first.seq

        hub.publish("project_demo", "run_1", "progress", {"text": "正在读取资料", "counters": {}})
        live = await asyncio.wait_for(anext(stream), 1)
        assert live is not None and live.kind == "progress"
        await stream.aclose()
    finally:
        store.close()


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_file", {"path": "../outside.md"}),
        ("write_file", {"path": "/absolute.md"}),
        ("write_file", {"path": "rules//broken.md"}),
        ("write_file", {"key": "bad/key"}),
        ("import_zip", {"entries": ["objects/../secret.md"]}),
        ("write_file", {"path": "scripts/demo.md", "entrypoint": "../demo.sh"}),
    ],
)
def test_write_safety_rejects_irreversible_inputs(tool: str, arguments: dict) -> None:
    assert check_write_safety(tool, arguments)


def test_write_safety_allows_normal_candidate_path() -> None:
    assert check_write_safety("write_file", {"path": "rules/safety-stock.md", "key": "safety_stock"}) == []


def _write_call_context(arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name="write_file",
            tool_args=arguments,
            tool_call=SimpleNamespace(id="call_goal_evidence"),
            tool_result=None,
            tool_msg=None,
        ),
        extra={},
    )


def _tool_call_context(
    name: str,
    arguments: dict,
    call_id: str,
    result: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=name,
            tool_args=arguments,
            tool_call=SimpleNamespace(id=call_id),
            tool_result=json.dumps(result, ensure_ascii=False) if result is not None else None,
            tool_msg=None,
        ),
        extra={},
    )


@pytest.mark.asyncio
async def test_tool_boundary_reuses_completed_evaluation_read_until_new_round() -> None:
    context = ToolContext(
        project_id="project_evaluation",
        run_id="run_evaluation",
        token="",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = ToolBoundaryRail(context)
    result = {
        "round": {"id": "round-1", "status": "done"},
        "counts": {"pass": 4, "fail": 3},
        "results": [{"testcaseId": "case-1", "verdict": "fail"}],
        "display": {"text": "已读取 7 条真实评测结果"},
    }
    first = _tool_call_context("get_evaluation_results", {}, "call-results-1", result)
    await rail.after_tool_call(first)  # type: ignore[arg-type]

    repeated = _tool_call_context("get_evaluation_results", {}, "call-results-2")
    await rail.before_tool_call(repeated)  # type: ignore[arg-type]
    replay = json.loads(repeated.inputs.tool_result)
    assert replay["duplicateRead"] is True
    assert replay["round"]["id"] == "round-1"
    assert replay["suppressTrace"] is True

    invalidator = _tool_call_context("run_evaluation", {}, "call-evaluation-2")
    await rail.before_tool_call(invalidator)  # type: ignore[arg-type]
    after_new_round = _tool_call_context("get_evaluation_results", {}, "call-results-3")
    await rail.before_tool_call(after_new_round)  # type: ignore[arg-type]
    assert after_new_round.inputs.tool_result is None


@pytest.mark.asyncio
async def test_goal_write_rejects_material_evidence_missing_from_body() -> None:
    events: list[tuple[str, dict]] = []
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=FakeGateway(),
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\n依据《工艺规程.docx》确定范围。",
        "evidence": ["material:工艺规程.docx#第3章"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "GOAL_EVIDENCE_LINK_REQUIRED"
    assert result["display"]["text"] == "建模目标中的资料依据还不能打开原文"
    assert result["retryable"] is True
    assert result["tracePhase"] == "repairing"
    assert result["validatorVersion"] == "goal-evidence-v2"
    assert result["findings"][0]["ruleId"] == "goal.evidence.missing_from_body"
    assert "material:<资料 id>#<原文锚点>" in result["instruction"]
    assert call.extra["_skip_tool"] is True
    assert events == []

    await ActivityRail(context.emit).after_tool_call(call)  # type: ignore[arg-type]

    assert events == [("tool", {
        "name": "write_file",
        "phase": "repairing",
        "summary": "建模目标中的资料依据还不能打开原文",
        "toolCallId": "call_goal_evidence",
        "code": "GOAL_EVIDENCE_LINK_REQUIRED",
        "retryable": True,
        "repairStopped": False,
        "repairAttempt": 1,
        "repairRoundId": "unscoped",
        "findingCount": 1,
    }), ("progress", {
        "activityId": "after-tool:call_goal_evidence",
        "text": "正在根据处理结果判断下一步",
        "detail": "当前操作已经结束，正在规划下一项可执行工作",
        "phase": "planning",
        "counters": {},
    })]


@pytest.mark.asyncio
async def test_goal_write_reports_body_material_token_missing_from_evidence() -> None:
    gateway = FakeGateway({
        "list_documents": {
            "documents": [{"id": "doc_1", "filename": "工艺规程.docx"}],
        },
    })
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    declared = "material:doc_1#第1章"
    undeclared = "material:doc_1#第2章"
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": f"# 建模目标\n\n依据 {declared} 与 {undeclared} 确定范围。",
        "evidence": [declared],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    finding = next(item for item in result["findings"] if item["ruleId"] == "goal.evidence.not_declared")
    assert finding["path"] == ".modeling/goal.md"
    assert finding["actual"] == undeclared
    assert "加入 write_file.evidence" in finding["suggestion"]


@pytest.mark.asyncio
async def test_repair_stop_emits_one_blocker_and_keeps_recovery_tools_available() -> None:
    events: list[tuple[str, dict]] = []

    async def state_action(_name: str, _arguments: dict) -> dict:
        return {"phase": "goal"}

    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=FakeGateway(),
        emit=lambda kind, payload: events.append((kind, payload)),
        state_action=state_action,
    )
    report = {
        "error": "CANDIDATE_FORMAT_INVALID",
        "retryable": True,
        "validatorVersion": "package-v1",
        "findings": [{
            "ruleId": "object.required_section",
            "path": "objects/customer.md",
            "message": "缺少章节",
            "suggestion": "补齐章节",
        }],
    }
    context.validation_repair.begin_round("model-1")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-2")
    context.validation_repair.record(report)
    context.validation_repair.begin_round("model-3")
    assert context.validation_repair.stopped is True

    rail = WriteSafetyRail(context)
    activity = ActivityRail(context.emit)
    first = _write_call_context({"path": "objects/customer.md"})
    second = _write_call_context({"path": "objects/product.md"})
    second.inputs.tool_call.id = "call_second_blocked"

    await rail.before_tool_call(first)  # type: ignore[arg-type]
    await activity.after_tool_call(first)  # type: ignore[arg-type]
    await rail.before_tool_call(second)  # type: ignore[arg-type]
    await activity.after_tool_call(second)  # type: ignore[arg-type]

    assert json.loads(first.inputs.tool_result)["error"] == "CANDIDATE_REPAIR_STOPPED"
    assert json.loads(second.inputs.tool_result)["suppressTrace"] is True
    assert len(events) == 2
    assert events[0][1]["phase"] == "blocked"
    assert events[1][0] == "progress"
    assert events[1][1]["detail"] == "正在核对失败原因和可恢复的下一步"

    for tool_name in ("delete_file", "discard_rejected_candidate_changes"):
        recovery = SimpleNamespace(
            inputs=SimpleNamespace(
                tool_name=tool_name,
                tool_args={"path": "objects/customer.md"} if tool_name == "delete_file" else {},
                tool_call=SimpleNamespace(id=f"call_{tool_name}"),
                tool_result=None,
                tool_msg=None,
            ),
            extra={},
        )
        await rail.before_tool_call(recovery)  # type: ignore[arg-type]
        assert recovery.extra == {}
        assert recovery.inputs.tool_result is None


@pytest.mark.asyncio
async def test_goal_write_rejects_empty_material_anchor() -> None:
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\nmaterial:工艺规程.docx#",
        "evidence": ["material:工艺规程.docx#"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "GOAL_EVIDENCE_LINK_REQUIRED"
    assert "缺少可定位的原文锚点" in result["reason"]


@pytest.mark.asyncio
async def test_goal_write_allows_complete_material_token_in_body() -> None:
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    token = "material:工艺规程.docx#第3章"
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": f"# 建模目标\n\n目标范围依据 {token} 确定。",
        "evidence": [token],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    assert call.extra == {}
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_goal_write_without_material_evidence_remains_allowed() -> None:
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\n由专家口述确认范围。",
        "evidence": ["user-decision:目标范围"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    assert call.extra == {}


@pytest.mark.asyncio
async def test_goal_write_with_project_materials_requires_locatable_evidence() -> None:
    gateway = FakeGateway({
        "list_documents": {
            "documents": [{"id": "doc_1", "filename": "工艺规程.docx"}],
        },
    })
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\n依据工艺规程梳理装置。",
        "evidence": [],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "GOAL_EVIDENCE_LINK_REQUIRED"
    assert "至少需要一处" in result["reason"]
    assert gateway.calls[-1]["tool"] == "list_documents"


@pytest.mark.asyncio
async def test_goal_write_requires_evidence_for_each_named_material() -> None:
    gateway = FakeGateway({
        "list_documents": {
            "documents": [
                {"id": "doc_1", "filename": "规程甲.docx"},
                {"id": "doc_2", "filename": "规程乙.docx"},
            ],
        },
    })
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\n规程甲.docx：material:doc_1#第1章\n\n规程乙.docx：待定位",
        "evidence": ["material:doc_1#第1章"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "GOAL_EVIDENCE_LINK_REQUIRED"
    assert "《规程乙.docx》" in result["reason"]


@pytest.mark.asyncio
async def test_goal_write_allows_each_named_material_with_body_evidence() -> None:
    gateway = FakeGateway({
        "list_documents": {
            "documents": [
                {"id": "doc_1", "filename": "规程甲.docx"},
                {"id": "doc_2", "filename": "规程乙.docx"},
            ],
        },
    })
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": (
            "# 建模目标\n\n规程甲.docx：material:doc_1#第1章\n\n"
            "规程乙.docx：material:doc_2#第2章"
        ),
        "evidence": ["material:doc_1#第1章", "material:doc_2#第2章"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    assert call.extra == {}
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_goal_write_rejects_visible_internal_document_id() -> None:
    gateway = FakeGateway({
        "list_documents": {
            "documents": [{"id": "doc_1", "filename": "工艺规程.docx"}],
        },
    })
    context = ToolContext(
        project_id="project_goal",
        run_id="run_goal",
        token="token_goal",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    call = _write_call_context({
        "path": ".modeling/goal.md",
        "content": "# 建模目标\n\n**doc_1 | 工艺规程.docx**\n\n依据 material:doc_1#第1章。",
        "evidence": ["material:doc_1#第1章"],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "GOAL_EVIDENCE_LINK_REQUIRED"
    assert "内部资料编号" in result["reason"]


@pytest.mark.asyncio
async def test_multi_section_diagram_rejects_one_shared_source_position() -> None:
    context = ToolContext(
        project_id="project_process",
        run_id="run_process",
        token="token_process",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    token = "material:doc_process#offset=0"
    call = _write_call_context({
        "path": "diagrams/process.mmd",
        "content": (
            "flowchart TD\n"
            f"subgraph Feed\n%% evidence: {token}\nA[原料]\nend\n"
            f"subgraph Reaction\n%% evidence: {token}\nB[反应]\nend\n"
            f"subgraph Product\n%% evidence: {token}\nC[产品]\nend\nA --> B\nB --> C"
        ),
        "evidence": [token],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "DIAGRAM_EVIDENCE_SPREAD_REQUIRED"
    assert "同一个原文位置" in result["reason"]


@pytest.mark.asyncio
async def test_multi_section_diagram_allows_distinct_source_positions() -> None:
    context = ToolContext(
        project_id="project_process",
        run_id="run_process",
        token="token_process",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    first = "material:doc_process#offset=1200"
    second = "material:doc_process#offset=9600"
    call = _write_call_context({
        "path": "diagrams/process.mmd",
        "content": (
            "flowchart TD\n"
            f"subgraph Feed\n%% evidence: {first}\nA[原料]\nend\n"
            f"subgraph Reaction\n%% evidence: {second}\nB[反应]\nend\n"
            f"subgraph Product\n%% evidence: {second}\nC[产品]\nend\nA --> B\nB --> C"
        ),
        "evidence": [first, second],
    })

    await rail.before_tool_call(call)  # type: ignore[arg-type]

    assert call.extra == {}
    assert call.inputs.tool_result is None


@pytest.mark.asyncio
async def test_mermaid_fence_enters_candidate_auto_repair_and_repeated_finding_stops() -> None:
    context = ToolContext(
        project_id="project_process",
        run_id="run_process",
        token="token_process",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    rail = WriteSafetyRail(context)
    arguments = {
        "path": "diagrams/process.mmd",
        "content": "```mermaid\nflowchart TD\nA --> B\n```\n",
        "evidence": ["user-decision:confirmed-process"],
    }

    context.validation_repair.begin_round("model-1")
    first = _write_call_context(arguments)
    await rail.before_tool_call(first)  # type: ignore[arg-type]
    first_result = json.loads(first.inputs.tool_result)
    assert first_result["error"] == "MERMAID_CONTENT_INVALID"
    assert first_result["retryable"] is True
    assert first_result["validatorVersion"] == "mermaid-source-v1"
    assert first_result["repairAttempt"] == 1
    assert first_result["repairStopped"] is False
    assert [finding["ruleId"] for finding in first_result["findings"]] == [
        "mermaid.no-markdown-fence",
        "mermaid.flowchart-declaration",
    ]

    context.validation_repair.begin_round("model-2")
    repeated = _write_call_context(arguments)
    await rail.before_tool_call(repeated)  # type: ignore[arg-type]
    repeated_result = json.loads(repeated.inputs.tool_result)
    assert repeated_result["repairAttempt"] == 2
    assert repeated_result["repeatedFinding"] is False
    assert repeated_result["repairStopped"] is False

    context.validation_repair.begin_round("model-3")
    blocked = context.validation_repair.blocked_result()
    assert blocked["repairAttempt"] == 2
    assert blocked["repeatedFinding"] is True
    assert blocked["repairStopped"] is True
    assert blocked["retryable"] is False


@pytest.mark.asyncio
async def test_mermaid_raw_source_passes_write_rail() -> None:
    context = ToolContext(
        project_id="project_process",
        run_id="run_process",
        token="token_process",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
    )
    evidence = "material:doc_process#offset=1200"
    call = _write_call_context({
        "path": "diagrams/process.mmd",
        "content": f"%% provenance: generated\n%% evidence: {evidence}\nflowchart LR\nA --> B\n",
        "evidence": [evidence],
    })

    await WriteSafetyRail(context).before_tool_call(call)  # type: ignore[arg-type]

    assert call.extra == {}
    assert call.inputs.tool_result is None


@pytest.mark.parametrize(
    ("tool", "path", "phase", "allowed"),
    [
        ("write_file", ".modeling/goal.md", "goal", True),
        ("write_file", "diagrams/process.mmd", "goal", False),
        ("write_file", "diagrams/process.mmd", "process", True),
        ("delete_file", "objects/unit.md", "process", False),
        ("write_file", "objects/unit.md", "knowledge", True),
        ("write_file", "mappings/object-data-mappings.json", "knowledge", True),
        ("write_file", "manifest.yaml", "goal", False),
        ("write_file", "scripts/calculate.py", "knowledge", False),
        ("write_file", "scripts/calculate.py", "validation", True),
        ("write_file", "other/bypass.md", "knowledge", False),
        ("stage_package_merge", "", "process", False),
        ("stage_package_merge", "", "knowledge", True),
        ("compile_package_fusion_plan", "", "intake", True),
        ("compile_package_fusion_plan", "", "knowledge", True),
    ],
)
def test_candidate_paths_are_limited_to_their_business_phase(
    tool: str, path: str, phase: str, allowed: bool,
) -> None:
    problem = _phase_write_problem(tool, {"path": path}, phase)
    assert (problem == "") is allowed


@pytest.mark.asyncio
async def test_restart_automatically_resumes_same_run_without_replaying_user_message(settings) -> None:
    gateway = FakeGateway()
    runtime = service(settings, ScriptedModel([[]]), gateway)
    runtime.store.create_run(run("run_restarting"))
    runtime.store.update_run(
        "run_restarting",
        status=RUN_RUNNING,
        run_credential="arc_run_restarting_secret",
    )
    runtime.events.publish(
        "project_demo",
        "run_restarting",
        "message",
        {"role": "user", "blocks": [{"type": "text", "text": "开始建模"}]},
    )
    resumed = asyncio.Event()
    captured: dict[str, object] = {}

    async def execute(live, query) -> None:
        captured["run_id"] = live.run_id
        captured["query"] = query
        resumed.set()

    runtime._execute = execute  # type: ignore[method-assign]  # noqa: SLF001
    await runtime.startup()
    try:
        await asyncio.wait_for(resumed.wait(), timeout=2)
        assert captured["run_id"] == "run_restarting"
        assert type(captured["query"]).__name__ == "InteractiveInput"
        assert runtime.store.require_run("run_restarting").status == RUN_QUEUED
        assert gateway.credentials["run_restarting"] == "arc_run_restarting_secret"
        claims = [call for call in gateway.calls if call["tool"] == "claim_candidate_workspace"]
        assert len(claims) == 1
        assert claims[0]["token"] == ""
        messages = [
            event for event in runtime.store.events_after("project_demo", 0)
            if event.kind == "message" and event.payload.get("role") == "user"
        ]
        assert len(messages) == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_restart_retries_transient_core_unavailability_before_resuming(
    settings, monkeypatch,
) -> None:
    class StartingGateway(FakeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.claim_attempts = 0

        async def call(self, **kwargs):
            if kwargs["tool"] == "claim_candidate_workspace":
                self.claim_attempts += 1
                if self.claim_attempts < 3:
                    raise AgentRuntimeError(
                        "CORE_UNAVAILABLE",
                        "建模核心暂时不可用，Agent 已停止继续写入",
                        status=503,
                    )
            return await super().call(**kwargs)

    monkeypatch.setattr(service_module, "RECOVERY_RETRY_DELAYS", (0, 0))
    gateway = StartingGateway()
    runtime = service(settings, ScriptedModel([[]]), gateway)
    runtime.store.create_run(run("run_starting_core"))
    runtime.store.update_run(
        "run_starting_core",
        status=RUN_RUNNING,
        run_credential="arc_run_starting_core_secret",
    )
    resumed = asyncio.Event()

    async def execute(_live, _query) -> None:
        resumed.set()

    runtime._execute = execute  # type: ignore[method-assign]  # noqa: SLF001
    await runtime.startup()
    try:
        await asyncio.wait_for(resumed.wait(), timeout=2)
        assert gateway.claim_attempts == 3
        assert runtime.store.require_run("run_starting_core").status == RUN_QUEUED
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_graceful_service_shutdown_preserves_running_checkpoint_for_next_process(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]), FakeGateway())
    await runtime.startup()
    entered = asyncio.Event()
    never_finishes = asyncio.Event()

    async def block_streaming(*_args, **_kwargs):
        entered.set()
        await never_finishes.wait()
        return {"result_type": "success", "output": "", "streamed": False}

    runtime._run_streaming = block_streaming  # type: ignore[method-assign]  # noqa: SLF001
    started = await runtime.start_run(
        project_id="project_demo",
        token="token_before_restart",
        message="开始建模",
    )
    run_id = started["id"]
    await asyncio.wait_for(entered.wait(), timeout=2)
    await runtime.shutdown()

    reopened = RuntimeStore(settings.runtime_db)
    try:
        stopped = reopened.require_run(run_id)
        assert stopped.status == "suspended"
        assert stopped.run_credential == f"arc_{run_id}_secret"
        assert reopened.project_state("project_demo").state == "intake:suspended"
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_legacy_suspended_run_without_scoped_credential_waits_for_user(settings) -> None:
    runtime = service(settings, ScriptedModel([[]]))
    runtime.store.create_run(run("run_suspended"))
    runtime.store.update_run("run_suspended", status="suspended")
    await runtime.startup()
    try:
        result = await runtime.steer(
            project_id="project_demo",
            token="token_after_restart",
            text="从最近检查点继续",
        )
        assert result["mode"] == "checkpoint_resume"
    finally:
        await runtime.shutdown()
