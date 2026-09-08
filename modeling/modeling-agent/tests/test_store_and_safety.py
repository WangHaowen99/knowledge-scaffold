from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import modeling_agent.service as service_module
from modeling_agent.errors import AgentRuntimeError
from modeling_agent.events import EventHub
from modeling_agent.rails import ActivityRail, ToolBoundaryRail, WriteSafetyRail, _phase_write_problem, check_write_safety
from modeling_agent.schemas import (
    InteractionRecord,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_WAITING,
    RunRecord,
)
from modeling_agent.store import RuntimeStore
from modeling_agent.tools import (
    CORE_TOOL_SPECS,
    CandidateValidationRepairState,
    CoreTool,
    EVALUATION_PENDING_MESSAGE,
    ToolContext,
)
from conftest import FakeGateway, ScriptedModel, service

BACKGROUND_TASK_START_TIMEOUT_SECONDS = 10


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
        assert store.require_run("run_1").tool_profile == "domain-v2"
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


def test_external_event_continuation_claim_is_atomic_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    event = {
        "eventId": "evaluation:round_claim:terminal",
        "roundId": "round_claim",
        "status": "done",
    }
    try:
        assert store.record_external_event(
            event["eventId"], "project_claim", "evaluation_terminal", event,
        ) is True
        assert store.claim_external_event_continuation(
            event["eventId"], "project_claim",
        ) == "claimed"
        assert store.claim_external_event_continuation(
            event["eventId"], "project_claim",
        ) == "continuing"
    finally:
        store.close()

    recovered = RuntimeStore(path)
    try:
        assert recovered.external_event_pending(event["eventId"], "project_claim") is True
        assert recovered.claim_external_event_continuation(
            event["eventId"], "project_claim",
        ) == "claimed"
        recovered.mark_external_event_continued(event["eventId"], "project_claim")
        assert recovered.claim_external_event_continuation(
            event["eventId"], "project_claim",
        ) == "continued"
    finally:
        recovered.close()


def test_increment_round_persists_baseline_and_is_idempotent_until_terminal(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        initial = store.project_state("project_increment")
        released = store.transition_project_state(
            "project_increment",
            expected_revision=initial.revision,
            next_state="release:completed",
            cause="test_released",
        )

        started, created = store.start_increment_round(
            "project_increment",
            baseline_revision="sha256:released-1",
            package_version="1.2.0",
        )
        assert created is True
        assert started.state == "increment:idle"
        assert started.round_mode == "incremental"
        assert started.round_id.startswith("increment_")
        assert started.baseline_revision == "sha256:released-1"
        assert started.revision == released.revision + 1

        duplicate, created = store.start_increment_round(
            "project_increment",
            baseline_revision="sha256:released-1",
            package_version="1.2.0",
        )
        assert created is False
        assert duplicate.round_id == started.round_id
        assert duplicate.revision == started.revision

        created_run = store.create_run(run("run_increment", "project_increment"))
        assert created_run.baseline_revision == "sha256:released-1"
        store.update_run("run_increment", status="completed")
        next_round, created = store.start_increment_round(
            "project_increment",
            baseline_revision="sha256:released-1",
            package_version="1.2.0",
        )
        assert created is True
        assert next_round.state == "increment:idle"
        assert next_round.round_id != started.round_id
    finally:
        store.close()


@pytest.mark.parametrize("stale_phase", ["goal", "knowledge"])
def test_increment_round_recovers_released_project_from_stale_phase(
    tmp_path: Path,
    stale_phase: str,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        initial = store.project_state("project_stale_goal")
        released = store.transition_project_state(
            "project_stale_goal",
            expected_revision=initial.revision,
            next_state="release:completed",
            cause="test_released",
        )
        stale = store.transition_project_state(
            "project_stale_goal",
            expected_revision=released.revision,
            next_state=f"{stale_phase}:completed",
            cause="test_legacy_read_rewind",
        )

        increment, created = store.start_increment_round(
            "project_stale_goal",
            baseline_revision="sha256:released-stale",
        )

        assert created is True
        assert increment.state == "increment:idle"
        assert increment.baseline_revision == "sha256:released-stale"
        assert increment.revision == stale.revision + 1
    finally:
        store.close()


def test_increment_round_does_not_replace_active_full_modeling_run(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        initial = store.project_state("project_active_knowledge")
        store.transition_project_state(
            "project_active_knowledge",
            expected_revision=initial.revision,
            next_state="knowledge:idle",
            cause="test_active_full_modeling",
        )
        store.create_run(run("run_active_knowledge", "project_active_knowledge"))

        with pytest.raises(AgentRuntimeError) as caught:
            store.start_increment_round(
                "project_active_knowledge",
                baseline_revision="sha256:released-active",
            )

        assert caught.value.code == "INCREMENT_ROUND_BLOCKED"
        assert store.project_state("project_active_knowledge").state == "knowledge:queued"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_released_material_event_starts_increment_round_without_auto_run(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    initial = runtime.store.project_state("project_released_material")
    runtime.store.transition_project_state(
        "project_released_material",
        expected_revision=initial.revision,
        next_state="release:completed",
        cause="test_released",
    )
    try:
        accepted = await runtime.handle_material_event(
            project_id="project_released_material",
            token="token_material",
            event={
                "eventId": "material:increment-doc:terminal",
                "status": "parsed",
                "documentId": "increment-doc",
                "sourceKey": "source-increment-doc",
                "filename": "新增规则.docx",
                "revisionNo": 1,
                "purpose": "goal",
                "readingMode": "on_demand",
                "autoContinue": False,
                "semanticChanged": True,
                "packageRevision": "sha256:released-material",
                "packageVersion": "1.2.0",
                "packageStatus": "released",
            },
        )

        state = runtime.store.project_state("project_released_material")
        assert accepted["continued"] is False
        assert state.state == "increment:idle"
        assert state.round_mode == "incremental"
        assert state.baseline_revision == "sha256:released-material"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_full_material_read_never_rewinds_released_phase(settings) -> None:
    runtime = service(settings, ScriptedModel([]))
    initial = runtime.store.project_state("project_read_only")
    released = runtime.store.transition_project_state(
        "project_read_only",
        expected_revision=initial.revision,
        next_state="release:completed",
        cause="test_released",
    )
    live = service_module.LiveRun(
        run_id="run_read_only",
        project_id="project_read_only",
        token="token_read_only",
    )
    try:
        await runtime._after_core_tool(  # noqa: SLF001 - read-only state invariant
            live,
            "read_documents_fully",
            {"filenames": ["目标说明.docx"]},
            {"documents": [{"filename": "目标说明.docx", "purpose": "goal"}]},
        )

        current = runtime.store.project_state("project_read_only")
        assert current.state == released.state
        assert current.revision == released.revision
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_increment_phase_rejects_full_generation_plan() -> None:
    gateway = FakeGateway()
    context = ToolContext(
        project_id="project_increment_plan",
        run_id="run_increment_plan",
        token="token_increment_plan",
        gateway=gateway,
        emit=lambda _kind, _payload: None,
        phase="increment",
    )
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "create_change_plan")

    result = json.loads(await CoreTool(spec, context).invoke({
        "mode": "generation",
        "goal": "重新生成全部知识包",
        "tasks": [],
    }))

    assert result["error"] == "INCREMENT_PLAN_MODE_REQUIRED"
    assert result["retryable"] is False
    assert gateway.calls == []


def test_store_migrates_existing_checkpoints_to_legacy_tool_profile(tmp_path: Path) -> None:
    path = tmp_path / "legacy-runtime.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE agent_runs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, session_id TEXT NOT NULL,
      status TEXT NOT NULL, prompt TEXT NOT NULL, token_fingerprint TEXT NOT NULL,
      baseline_revision TEXT NOT NULL DEFAULT '', candidate_revision TEXT NOT NULL DEFAULT '',
      interaction_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.execute(
        "INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "run_legacy", "project_legacy", "legacy-session", "suspended", "继续建模",
            "000000000000", "", "", "", "", "", "2026-08-30T00:00:00Z",
            "2026-08-30T00:00:00Z",
        ),
    )
    db.commit()
    db.close()

    store = RuntimeStore(path)
    try:
        assert store.require_run("run_legacy").tool_profile == "legacy-v1"
    finally:
        store.close()


def test_store_migrates_legacy_project_state_round_columns(tmp_path: Path) -> None:
    path = tmp_path / "legacy-state.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE project_agent_state (
      project_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'intake:idle',
      revision INTEGER NOT NULL DEFAULT 0, run_id TEXT NOT NULL DEFAULT '',
      interaction_id TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL DEFAULT '',
      summary TEXT NOT NULL DEFAULT '', open_questions_json TEXT NOT NULL DEFAULT '[]',
      error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    )""")
    db.execute(
        "INSERT INTO project_agent_state VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "project_legacy_state", "release:completed", 7, "", "", "",
            "已发布", "[]", "", "2026-08-31T00:00:00Z",
        ),
    )
    db.commit()
    db.close()

    store = RuntimeStore(path)
    try:
        state = store.project_state("project_legacy_state")
        assert state.state == "release:completed"
        assert state.revision == 7
        assert state.round_mode == "initial"
        assert state.round_id == ""
        assert state.baseline_revision == ""
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


def test_validation_repair_state_survives_runtime_store_restart(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_repair"))
    state = CandidateValidationRepairState.from_snapshot(
        store.validation_repair_state("run_repair"),
        on_change=lambda snapshot: store.save_validation_repair_state(
            "run_repair", snapshot,
        ),
    )
    state.begin_round("model-1")
    state.record({
        "error": "CANDIDATE_FORMAT_INVALID",
        "validatorVersion": "package-v1",
        "findingFingerprint": "sha256:persisted",
        "validationReportId": "report-persisted",
        "findingCount": 18,
        "findingsTruncated": True,
    })
    state.begin_round("model-2")
    store.close()

    reopened = RuntimeStore(path)
    try:
        restored = CandidateValidationRepairState.from_snapshot(
            reopened.validation_repair_state("run_repair"),
        )
        assert restored.attempts == 1
        assert restored.last_fingerprint == "sha256:persisted"
        assert restored.current_round_id == "model-2"
        assert restored.last_report["validationReportIds"] == ["report-persisted"]
        assert restored.last_report["findingCount"] == 18
    finally:
        reopened.close()


def test_technical_tool_redirect_survives_restart_until_resolved(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_redirect"))
    recovery = {
        "issueId": "agent-issue-synthetic",
        "requiredTool": "patch_object_source_binding",
        "targetKind": "object_binding",
    }
    store.save_technical_recovery(
        "run_redirect", "agent-issue-synthetic", recovery,
    )
    store.close()

    reopened = RuntimeStore(path)
    try:
        assert reopened.technical_recoveries("run_redirect") == {
            "agent-issue-synthetic": recovery,
        }
        reopened.resolve_technical_recoveries(
            "run_redirect", ["agent-issue-synthetic"],
        )
        assert reopened.technical_recoveries("run_redirect") == {}
    finally:
        reopened.close()


def test_retry_ledger_survives_restart_and_enforces_attempt_limit(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_retry"))
    first = store.record_retry(
        "run_retry",
        "model-stream-0001",
        category="model_timeout",
        max_attempts=1,
        error_fingerprint="sha256:first",
        last_error="TimeoutError: first chunk timeout",
        delay_seconds=0.5,
    )
    assert first["attempt"] == 1
    assert first["status"] == "retrying"
    assert first["nextAllowedAt"]
    store.close()

    reopened = RuntimeStore(path)
    try:
        restored = reopened.retry_entry("run_retry", "model-stream-0001")
        assert restored is not None
        assert restored["attempt"] == 1
        assert restored["errorFingerprint"] == "sha256:first"
        with pytest.raises(AgentRuntimeError) as caught:
            reopened.record_retry(
                "run_retry",
                "model-stream-0001",
                category="model_timeout",
                max_attempts=1,
                error_fingerprint="sha256:second",
                last_error="TimeoutError: idle timeout",
            )
        assert caught.value.code == "RETRY_EXHAUSTED"
        settled = reopened.settle_retry(
            "run_retry", "model-stream-0001", "exhausted", last_error="still timed out",
        )
        assert settled is not None
        assert settled["status"] == "exhausted"
        assert settled["nextAllowedAt"] == ""
    finally:
        reopened.close()


def test_validation_repair_state_is_scoped_to_candidate_revision() -> None:
    state = CandidateValidationRepairState()
    state.scope_candidate_revision("candidate-a")
    state.begin_round("model-a1")
    state.record({"error": "INVALID", "findings": [{"ruleId": "a", "path": "a"}]})
    state.begin_round("model-a2")
    assert state.attempts == 1

    state.scope_candidate_revision("candidate-b")

    assert state.candidate_revision == "candidate-b"
    assert state.attempts == 0
    assert state.last_fingerprint == ""


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


@pytest.mark.asyncio
async def test_script_test_never_replays_receipt_across_candidate_source_changes(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.create_run(run("run_script"))
    arguments = {"script": "scripts/current.py", "cases": [{"input": {}, "expected": {"ok": True}}]}
    store.save_tool_receipt(
        "run_script", "call_script_test", "run_script_test", arguments,
        {"passed": 1, "failed": 0},
    )
    context = ToolContext(
        project_id="project_script",
        run_id="run_script",
        token="",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        tool_receipt_lookup=lambda name, call_id, args: store.tool_receipt(
            "run_script", call_id, name, args,
        ),
    )
    rail = ToolBoundaryRail(context)
    call = _tool_call_context("run_script_test", arguments, "call_script_test")
    try:
        await rail.before_tool_call(call)  # type: ignore[arg-type]
        assert call.inputs.tool_result is None
        assert "_tool_receipt_replayed" not in call.extra
    finally:
        store.close()


@pytest.mark.asyncio
async def test_tool_boundary_does_not_save_empty_interrupted_result() -> None:
    saved: list[tuple[str, str, dict, dict]] = []
    context = ToolContext(
        project_id="project_release",
        run_id="run_release",
        token="",
        gateway=FakeGateway(),
        emit=lambda _kind, _payload: None,
        tool_receipt_save=lambda name, call_id, arguments, result: saved.append(
            (name, call_id, arguments, result)
        ),
    )
    rail = ToolBoundaryRail(context)
    interrupted = _tool_call_context(
        "request_release",
        {"summary": "确认正式发布"},
        "call_release_1",
        {},
    )

    await rail.after_tool_call(interrupted)  # type: ignore[arg-type]

    assert saved == []


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


def test_begin_run_cancellation_atomically_closes_pending_interaction(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        store.create_run(run("run_cancel_pending"))
        store.update_run("run_cancel_pending", status=RUN_RUNNING)
        interaction = InteractionRecord(
            id="interaction_cancel_pending",
            project_id="project_demo",
            run_id="run_cancel_pending",
            tool_call_id="call_cancel_pending",
            kind="business_decision",
            payload={"question": "是否继续当前方案？"},
        )
        store.create_interaction(interaction)

        stopping, closed = store.begin_run_cancellation("run_cancel_pending")

        assert stopping.status == "stopping"
        assert stopping.interaction_id == ""
        assert [item.id for item in closed] == [interaction.id]
        assert closed[0].status == "cancelled"
        assert store.get_interaction(interaction.id).status == "cancelled"
        assert store.pending_interaction("project_demo") is None
        state = store.project_state("project_demo")
        assert state.state == "intake:stopping"
        assert state.interaction_id == ""
    finally:
        store.close()


def test_unfinished_tool_events_track_each_tool_call_terminal_state(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        current = store.create_run(run("run_tool_activity"))
        for payload in (
            {"name": "stage_change_set", "phase": "started", "toolCallId": "call_done"},
            {"name": "stage_change_set", "phase": "completed", "toolCallId": "call_done"},
            {"name": "stage_change_set", "phase": "started", "toolCallId": "call_repairing"},
            {"name": "stage_change_set", "phase": "repairing", "toolCallId": "call_repairing"},
            {"name": "validate_change_set", "phase": "started", "toolCallId": "call_active"},
        ):
            store.append_event(current.project_id, current.id, "tool", payload)

        unfinished = store.unfinished_tool_events(current.id)

        assert [event.payload["toolCallId"] for event in unfinished] == [
            "call_repairing", "call_active",
        ]
        store.append_event(current.project_id, current.id, "tool", {
            "name": "stage_change_set",
            "phase": "cancelled",
            "toolCallId": "call_repairing",
        })
        assert [
            event.payload["toolCallId"] for event in store.unfinished_tool_events(current.id)
        ] == ["call_active"]
    finally:
        store.close()


def test_store_startup_closes_legacy_pending_interaction_from_terminal_run(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_legacy_terminal"))
    store.update_run("run_legacy_terminal", status=RUN_RUNNING)
    interaction = InteractionRecord(
        id="interaction_legacy_terminal",
        project_id="project_demo",
        run_id="run_legacy_terminal",
        tool_call_id="call_legacy_terminal",
        kind="business_decision",
        payload={"question": "历史问题"},
    )
    store.create_interaction(interaction)
    store.update_run("run_legacy_terminal", status="cancelled")
    store.close()

    reopened = RuntimeStore(path)
    try:
        assert reopened.get_interaction(interaction.id).status == "cancelled"
        assert reopened.require_run("run_legacy_terminal").interaction_id == ""
        assert reopened.project_state("project_demo").interaction_id == ""
        assert reopened.pending_interaction("project_demo") is None
    finally:
        reopened.close()


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
async def test_tool_boundary_finishes_agent_round_after_background_evaluation_starts() -> None:
    events: list[tuple[str, dict]] = []
    context = ToolContext(
        project_id="project_evaluation_pending",
        run_id="run_evaluation_pending",
        token="",
        gateway=FakeGateway(),
        emit=lambda kind, payload: events.append((kind, payload)),
    )
    rail = ToolBoundaryRail(context)

    class Callback:
        def __init__(self) -> None:
            self.inputs = SimpleNamespace(
                tool_name="run_evaluation",
                tool_args={},
                tool_call=SimpleNamespace(id="call-evaluation-pending"),
                tool_result=json.dumps({
                    "roundId": "round-pending",
                    "status": "running",
                    "pending": True,
                    "terminal": False,
                }),
            )
            self.extra: dict = {}
            self.finish: dict | None = None

        def request_force_finish(self, result: dict) -> None:
            self.finish = result

    callback = Callback()
    await rail.after_tool_call(callback)  # type: ignore[arg-type]

    assert context.pending_evaluation == {"roundId": "round-pending"}
    assert events == [("focus", {"target": {
        "kind": "evaluation",
        "target": "正式评测",
        "view": "runs",
        "roundId": "round-pending",
    }})]
    assert callback.finish == {
        "result_type": "answer",
        "output": EVALUATION_PENDING_MESSAGE,
        "pending_evaluation": True,
    }


@pytest.mark.asyncio
async def test_optional_evaluation_database_unavailable_stays_in_activity_trace() -> None:
    events: list[tuple[str, dict]] = []
    call = _tool_call_context("query_eval_db", {"sql": "SELECT 1"}, "call_eval_db", {
        "error": "EVAL_DB_TOOL_UNAVAILABLE",
        "reason": "评测库尚未配置，未执行查询",
        "retryable": False,
        "tracePhase": "rejected",
        "problemVisibility": "activity",
        "customerSummary": "可选评测库未配置，已跳过本次查询",
    })

    await ActivityRail(lambda kind, payload: events.append((kind, payload))).after_tool_call(call)  # type: ignore[arg-type]

    tool_event = next(payload for kind, payload in events if kind == "tool")
    assert tool_event["phase"] == "rejected"
    assert tool_event["code"] == "EVAL_DB_TOOL_UNAVAILABLE"
    assert tool_event["problemVisibility"] == "activity"
    assert tool_event["summary"] == "可选评测库未配置，已跳过本次查询"


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
        "issueId": result["issueId"],
        "issueStatus": "repairing",
        "problemVisibility": "",
        "resolvedIssueIds": [],
    }), ("progress", {
        "activityId": "after-tool:call_goal_evidence",
        "text": "建模目标中的资料依据还不能打开原文",
        "detail": "正在核对失败原因和可恢复的下一步",
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
async def test_repair_stop_does_not_freeze_unrelated_dependency_failures() -> None:
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

    assert json.loads(first.inputs.tool_result)["error"] == "WRITE_PHASE_MISMATCH"
    assert json.loads(second.inputs.tool_result)["error"] == "WRITE_PHASE_MISMATCH"
    assert context.validation_repair.stopped is True
    assert context.validation_repair.attempts == 2
    assert len(events) == 4
    assert [event[1]["phase"] for event in events if event[0] == "tool"] == ["waiting", "waiting"]

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
        ("upsert_object", "", "process", False),
        ("upsert_object", "", "knowledge", True),
        ("bind_object_sources", "", "increment", True),
        ("bind_object_source", "", "validation", False),
    ],
)
def test_candidate_paths_are_limited_to_their_business_phase(
    tool: str, path: str, phase: str, allowed: bool,
) -> None:
    problem = _phase_write_problem(tool, {"path": path}, phase)
    assert (problem == "") is allowed


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
    await asyncio.wait_for(
        entered.wait(), timeout=BACKGROUND_TASK_START_TIMEOUT_SECONDS,
    )
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


def test_stale_run_record_is_diagnosed_and_does_not_block_new_plan_after_termination(
    tmp_path: Path,
) -> None:
    """run 记录丢失但状态仍被占用时，恢复应标记 stale 而不是静默续跑或阻塞新建。"""
    path = tmp_path / "runtime.sqlite"
    store = RuntimeStore(path)
    store.create_run(run("run_stale"))
    store.update_run("run_stale", status=RUN_RUNNING)
    store.save_tool_receipt(
        "run_stale",
        "call_stale_done",
        "write_file",
        {"path": "objects/unit.md", "content": "# Unit"},
        {"ok": True, "revision": "candidate-1"},
    )
    # 模拟半恢复：runtime.sqlite 保留了状态与回执，但 agent_runs 行丢失。
    database = sqlite3.connect(path)
    database.execute("DELETE FROM agent_runs WHERE id = 'run_stale'")
    database.commit()
    database.close()
    try:
        assert store.recover_interrupted() == []
        state = store.project_state("project_demo")
        assert state.recovery_status == "stale_run"
        assert state.public()["recovery"] == {"status": "stale_run"}
        with pytest.raises(AgentRuntimeError) as caught:
            store.create_run(run("run_new"))
        assert caught.value.code == "RUN_STALE_OCCUPIED"
        assert caught.value.details["recovery"]["missing"] == ["run"]

        terminated = store.terminate_stale_occupancy("project_demo")
        assert terminated.state == "intake:cancelled"
        assert terminated.run_id == ""
        assert terminated.recovery_status == ""
        store.create_run(run("run_new"))
        assert store.active_run("project_demo").id == "run_new"
        transitions = store._db.execute(  # noqa: SLF001 - persistence invariant test
            "SELECT cause FROM project_agent_state_transitions ORDER BY id"
        ).fetchall()
        assert [row["cause"] for row in transitions][-2:] == [
            "stale_occupancy_terminated",
            "run_started",
        ]
    finally:
        store.close()


# def test_clear_recovery_status_only_clears_requested_status(tmp_path: Path) -> None:
#     store = RuntimeStore(tmp_path / "runtime.sqlite")
#     try:
#         state = store.project_state("project_recovery_status")
#         store.transition_project_state(
#             "project_recovery_status",
#             expected_revision=state.revision,
#             next_state="evaluation:completed",
#             cause="test_setup",
#             recovery_status="volume_mismatch",
#         )
#         assert store.clear_recovery_status(
#             "volume_mismatch",
#             cause="storage_verified_after_checkpoint_path_fix",
#         ) == 1
#         assert store.project_state("project_recovery_status").recovery_status == ""
#     finally:
#         store.close()


def test_recover_interrupted_keeps_waiting_interaction_pending(tmp_path: Path) -> None:
    """等待用户回答的交互在重启后保留，不转入自动恢复队列。"""
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    store.create_run(run("run_waiting"))
    store.update_run("run_waiting", status=RUN_WAITING)
    store.create_interaction(
        InteractionRecord(
            id="interaction_waiting",
            project_id="project_demo",
            run_id="run_waiting",
            tool_call_id="call_waiting",
            kind="question",
            payload={"question": "请确认业务口径"},
        )
    )
    try:
        assert store.recover_interrupted() == []
        state = store.project_state("project_demo")
        assert state.state == "intake:waiting"
        assert state.interaction_id == "interaction_waiting"
        assert state.recovery_status == ""
    finally:
        store.close()
