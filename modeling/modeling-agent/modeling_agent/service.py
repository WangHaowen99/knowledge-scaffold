from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .agent import AgentBundle, build_agent, build_model, list_skills
from .core_client import CoreGateway, HttpCoreGateway
from .errors import AgentRuntimeError
from .events import EventHub
from .schemas import (
    ACTIVE_RUN_STATUSES,
    InteractionRecord,
    RECOVERY_CHECKPOINT_MISSING,
    RECOVERY_NONE,
    RECOVERY_RESUME_FAILED,
    RECOVERY_STALE_RUN,
    RECOVERY_VOLUME_MISMATCH,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_STOPPING,
    RUN_SUSPENDED,
    RUN_WAITING,
    RunRecord,
)
from .settings import Settings
from .store import RuntimeStore, token_fingerprint
from .project_transfer import (
    delete_project_transfer,
    export_project_transfer,
    import_project_transfer,
)
from .tools import CandidateValidationRepairState, ToolContext
from .visible_text import VisibleReasoningSanitizer, VisibleTextSanitizer, sanitize_visible_text
from .workflows import (
    build_execution_brief_workflow,
    build_tool_redirect_workflow,
    execution_brief_prompt,
    tool_redirect_prompt,
)

logger = logging.getLogger("modeling_agent")
INTERJECTION_CONTINUATION_PREFIX = "interjection-continuation:"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


PHASE_LABELS = {
    "intake": "资料与目标准备",
    "goal": "目标确认",
    "process": "流程建模",
    "knowledge": "知识建模",
    "increment": "增量建模",
    "validation": "确定性验证",
    "evaluation": "评测",
    "release": "发布交付",
}
PHASE_EDGES = {
    "intake": ("goal",),
    "goal": ("process",),
    "process": ("goal", "knowledge"),
    "knowledge": ("goal", "process", "validation"),
    "increment": ("goal", "process", "knowledge", "validation", "evaluation"),
    "validation": ("goal", "process", "knowledge", "evaluation"),
    "evaluation": ("goal", "process", "knowledge", "validation", "release"),
    "release": ("goal", "process", "knowledge"),
}
PHASE_GATES = {
    "goal": "materials_ready",
    "process": "goal_confirmed",
    "knowledge": "process_confirmed",
    "validation": "knowledge_confirmed",
    "evaluation": "ready_for_eval",
    "release": "releasable",
}

MAX_AUTONOMOUS_CONTINUATIONS = 8
MAX_STALLED_CONTINUATIONS = 2
MAX_DECISION_CARD_RETRIES = 2
MAX_VISIBLE_REASONING_CHARS = 12_000
REASONING_EVENT_BATCH_CHARS = 160
COMPACT_SUMMARY_MAX_TOKENS = 4_096
COMPACT_SUMMARY_TIMEOUT_SECONDS = 60.0
MAX_TOOL_SUMMARIES = 60
MAX_MODEL_RECOVERY_ATTEMPTS = 1
MODEL_RECOVERY_DELAY_SECONDS = 0.5
RECOVERY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
AUTONOMOUS_PROGRESS_TOOLS = frozenset({
    "create_change_plan",
    "revise_change_plan",
    "stage_change_set",
    "validate_change_set",
    "discard_change_set",
    "analyze_material_impact",
    "stage_tabular_dataset",
    "update_work_notes",
    "write_file",
    "upsert_object",
    "delete_file",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "create_object",
    "create_rule",
    "create_operation",
    "create_metric",
    "patch_object",
    "patch_rule",
    "patch_operation",
    "patch_metric",
    "sync_schema_objects",
    "patch_object_source_binding",
    "delete_domain_resource",
    "confirm_deterministic_scope",
    "define_script_contract",
    "stage_script_source",
    "bind_object_sources",
    "discard_rejected_candidate_changes",
    "regroup_changes",
    "create_package_fusion_plan",
    "update_package_fusion_plan",
    "compile_package_fusion_plan",
    "transition_project_state",
    "run_script_test",
    "run_evaluation",
    "cancel_evaluation",
})

# 脚本契约/确定性建模交互要求本轮形成实际产物：新契约、脚本候选、脚本测试或
# 范围确认结果。空正文紧凑摘要恢复时，若这些工具没有任何完成记录且没有生成
# 下一张确认卡，不得把 Run 结算为成功完成，否则会形成“只有摘要没有产物”的假完成。
SCRIPT_ARTIFACT_TOOLS = frozenset({
    "define_script_contract",
    "stage_script_source",
    "confirm_deterministic_scope",
    "run_script_test",
    "bind_object_sources",
})
# 评测返回业务门禁失败时已经完成了用户要求的“执行评测”；其余必需工具
# 会产生成果或完成证明，只有成功返回才算满足本轮要求。
FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL = frozenset({"run_evaluation"})
PURPOSE_REVIEW_ALLOWED_TOOLS = frozenset({
    "get_project_state", "list_documents", "read_document", "list_tables", "describe_table",
    "set_material_directive", "read_file", "list_skill", "focus", "notify",
})
PHASE_ORDER = tuple(PHASE_LABELS)


@dataclass
class LiveRun:
    run_id: str
    project_id: str
    token: str
    tool_profile: str = "domain-v2"
    steering: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    bundle: AgentBundle | None = None
    restore_interaction: InteractionRecord | None = None
    required_tools: set[str] = field(default_factory=set)
    attempted_tools: set[str] = field(default_factory=set)
    observed_tools: set[str] = field(default_factory=set)
    required_material_directives: set[str] = field(default_factory=set)
    observed_material_directives: set[str] = field(default_factory=set)
    allowed_tools: frozenset[str] | None = None
    proof_retries: dict[str, int] = field(default_factory=dict)
    release_reentry_authorized: bool = False
    release_declined: bool = False
    confirmation_required: bool = False
    autonomous_seed: bool = False
    tool_summaries: list[str] = field(default_factory=list)
    stable_read_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasoning_chars_published: int = 0
    reasoning_truncated: bool = False
    package_contract: dict[str, Any] = field(default_factory=dict)
    validation_repair: CandidateValidationRepairState = field(default_factory=CandidateValidationRepairState)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    inflight_core_calls: set[asyncio.Task[Any]] = field(default_factory=set)
    plan_approval_required: bool = False
    technical_recoveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    deferred_purpose_continuation: tuple[int, str, str] | None = None


def _missing_required_tools(live: LiveRun) -> set[str]:
    terminal_attempts = live.required_tools & FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL
    successful_results = live.required_tools - terminal_attempts
    missing = (terminal_attempts - live.attempted_tools) | (successful_results - live.observed_tools)
    if live.required_material_directives - live.observed_material_directives:
        missing.add("set_material_directive")
    return missing


def _missing_material_directives(live: LiveRun) -> set[str]:
    return live.required_material_directives - live.observed_material_directives


def _interaction_requires_artifact(interaction: InteractionRecord) -> bool:
    """判断交互是否要求本轮形成实际脚本产物（新契约/候选/确认卡）。

    只对脚本契约与确定性范围确认类交互开启：这类交互回答后如果模型返回空正文，
    必须确认本轮真的形成了新契约或候选，否则不得把 Run 结算为成功完成。
    """
    kind = str(interaction.kind or "")
    payload = interaction.payload or {}
    if kind == "scope_confirmation":
        return True
    if kind in {"confirmation", "release_confirmation"}:
        return False
    question = str(payload.get("question") or "")
    contract_summary = str(payload.get("contractSummary") or "")
    summary = str(payload.get("summary") or "")
    return (
        "脚本契约" in question
        or "script contract" in question.lower()
        or "脚本契约" in contract_summary
        or "script contract" in contract_summary.lower()
        or "脚本契约" in summary
        or "script contract" in summary.lower()
    )


_SCRIPT_SKIP_ARTIFACT_INTENT = re.compile(
    r"(?:本轮)?不生成|不调整|跳过|无需生成|skip",
    re.IGNORECASE,
)


def _answer_requires_script_artifact(
    interaction: InteractionRecord,
    answer: dict[str, Any],
) -> bool:
    """按专家答案判断本轮是否必须形成脚本产物。

    交互本身要求脚本产物时，如果专家明确选择“本轮不生成/不调整/跳过”，
    空正文恢复不应再强制要求新契约或候选，否则会把合法的“不生成”误判为失败。
    """
    if not _interaction_requires_artifact(interaction):
        return False
    text = json.dumps(answer, ensure_ascii=False)
    return not bool(_SCRIPT_SKIP_ARTIFACT_INTENT.search(text))


def _purpose_review_suggestions_complete(live: LiveRun) -> bool:
    return (
        bool(live.required_material_directives)
        and live.allowed_tools == PURPOSE_REVIEW_ALLOWED_TOOLS
        and not _missing_required_tools(live)
    )


class _ReasoningEmitter:
    def __init__(self, events: EventHub, live: LiveRun, run: RunRecord) -> None:
        self._events = events
        self._live = live
        self._run = run
        self._sanitizer = VisibleReasoningSanitizer()
        self._pending = ""
        self._emitted = False
        self._observed = False

    @property
    def seen(self) -> bool:
        return self._observed

    def feed(self, raw_text: str) -> None:
        if not raw_text or self._live.reasoning_truncated:
            return
        self._observed = True
        self._pending += self._sanitizer.feed(raw_text)
        if len(self._pending) >= REASONING_EVENT_BATCH_CHARS or "\n" in self._pending:
            self._flush()

    def finish(self) -> None:
        if not self._live.reasoning_truncated:
            self._pending += self._sanitizer.finish()
        self._flush()
        if self._emitted:
            self._events.publish(self._run.project_id, self._run.id, "reasoning_done", {
                "messageId": f"reasoning_{self._run.id}",
                "role": "assistant",
            })

    def _flush(self) -> None:
        if not self._pending:
            return
        remaining = MAX_VISIBLE_REASONING_CHARS - self._live.reasoning_chars_published
        if remaining <= 0:
            self._pending = ""
            self._publish_truncated()
            return
        delta = self._pending[:remaining]
        self._pending = self._pending[remaining:]
        if delta:
            self._emitted = True
            self._live.reasoning_chars_published += len(delta)
            self._events.publish(self._run.project_id, self._run.id, "reasoning_delta", {
                "messageId": f"reasoning_{self._run.id}",
                "role": "assistant",
                "delta": delta,
            })
        if self._pending or self._live.reasoning_chars_published >= MAX_VISIBLE_REASONING_CHARS:
            self._pending = ""
            self._publish_truncated()

    def _publish_truncated(self) -> None:
        if self._live.reasoning_truncated:
            return
        self._live.reasoning_truncated = True
        self._emitted = True
        self._events.publish(self._run.project_id, self._run.id, "reasoning_delta", {
            "messageId": f"reasoning_{self._run.id}",
            "role": "assistant",
            "delta": "\n\n（思考过程较长，后续内容已收起）",
            "truncated": True,
        })


class AgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: RuntimeStore | None = None,
        gateway: CoreGateway | None = None,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or RuntimeStore(settings.runtime_db)
        self.events = EventHub(self.store)
        self._owns_gateway = gateway is None
        self._gateway = gateway or HttpCoreGateway(
            settings.core_base_url,
            service_token=settings.service_token,
        )
        self._model_factory = model_factory
        self._live: dict[str, LiveRun] = {}
        self._started = False
        self._shutting_down = False
        self._storage_diag: dict[str, Any] = {}
        self._recovery_summary: dict[str, Any] = {}

    async def startup(self) -> None:
        if self._started:
            return
        self._shutting_down = False
        from openjiuwen.core.runner.runner import Runner
        from openjiuwen.core.runner.runner_config import (
            DistributedConfig,
            MessageQueueConfig,
            MessageQueueType,
            RunnerConfig,
        )
        from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig

        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        Runner.set_config(
            RunnerConfig(
                distributed_mode=False,
                distributed_config=DistributedConfig(
                    message_queue_config=MessageQueueConfig(type=MessageQueueType.FAKE),
                ),
                checkpointer_config=CheckpointerConfig(
                    type="persistence",
                    conf={"db_type": "sqlite", "db_path": str(self.settings.checkpoint_db)},
                ),
            )
        )
        await Runner.start()
        self._started = True
        self._storage_diag = self._storage_diagnostic()
        recovered_runs = self.store.recover_interrupted()
        if recovered_runs:
            if not self._storage_diag.get("healthy", True):
                # 数据卷自检失败时不允许自动续跑，避免半恢复继续污染状态。
                for recovered in recovered_runs:
                    self._mark_resume_failed(
                        recovered,
                        RECOVERY_VOLUME_MISMATCH,
                        "建模服务持久化自检未通过，已暂停自动恢复，请核对数据卷",
                    )
                return
            await asyncio.gather(*(
                self._auto_resume_recovered(recovered) for recovered in recovered_runs
            ))

    async def _auto_resume_recovered(self, run: RunRecord) -> None:
        self._publish_state(run.project_id, run.id)
        if not run.run_credential or not run.session_id:
            self.events.publish(
                run.project_id,
                run.id,
                "status",
                {"status": RUN_SUSPENDED, "text": "已保留最近进展，重新打开项目后会继续"},
            )
            return
        if not self._checkpoint_available():
            self._mark_resume_failed(
                run,
                RECOVERY_CHECKPOINT_MISSING,
                "检查点数据库缺失或不可读，已暂停自动恢复，请核对数据卷",
            )
            return
        restore = getattr(self._gateway, "restore_run_credential", None)
        if not callable(restore):
            self.events.publish(
                run.project_id,
                run.id,
                "status",
                {"status": RUN_SUSPENDED, "text": "已保留最近进展，等待安全接续"},
            )
            return
        restore(run.id, run.run_credential)
        for attempt in range(len(RECOVERY_RETRY_DELAYS) + 1):
            try:
                await self._resume(
                    run,
                    "",
                    {},
                    preserve_token_fingerprint=True,
                )
                break
            except AgentRuntimeError as error:
                transient = error.code == "CORE_UNAVAILABLE"
                if not transient or attempt >= len(RECOVERY_RETRY_DELAYS):
                    logger.warning("Could not automatically resume run %s: %s", run.id, error)
                    self._mark_resume_failed(run, RECOVERY_RESUME_FAILED, "自动恢复失败，已安全停在最近进展")
                    return
                await asyncio.sleep(RECOVERY_RETRY_DELAYS[attempt])
            except Exception as error:  # noqa: BLE001 - one failed recovery must not block other projects
                logger.warning("Could not automatically resume run %s: %s", run.id, error)
                self._mark_resume_failed(run, RECOVERY_RESUME_FAILED, "自动恢复失败，已安全停在最近进展")
                return
        self.store.mark_recovery_status(
            run.project_id,
            RECOVERY_NONE,
            error="",
            cause="recovery_resumed",
        )
        self.events.publish(
            run.project_id,
            run.id,
            "status",
            {"status": RUN_QUEUED, "text": "服务恢复完成，助手正在从原进度继续"},
        )

    def _mark_resume_failed(self, run: RunRecord, status: str, message: str) -> None:
        self.store.mark_recovery_status(
            run.project_id,
            status,
            error=message,
            cause="recovery_failed",
        )
        self._publish_state(run.project_id, run.id)
        self.events.publish(
            run.project_id,
            run.id,
            "status",
            {"status": RUN_SUSPENDED, "text": message},
        )

    def _checkpoint_available(self) -> bool:
        checkpoint_db = self.settings.checkpoint_db
        if not checkpoint_db.exists():
            return False
        try:
            database = sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)
            database.close()
            return True
        except sqlite3.Error:
            return False

    def _storage_diagnostic(self) -> dict[str, Any]:
        runtime_db = self.settings.runtime_db
        checkpoint_db = self.settings.checkpoint_db
        same_directory = runtime_db.parent.resolve() == checkpoint_db.parent.resolve()
        diagnostic: dict[str, Any] = {
            "healthy": True,
            "sameDirectory": same_directory,
            "runtimeDbExists": runtime_db.exists(),
            "checkpointDbExists": checkpoint_db.exists(),
            "volumeChanged": False,
            "runCount": 0,
            "checkpointCount": 0,
            "checkedAt": now_iso(),
        }
        identity_path = self.settings.data_dir / ".modeling_agent_identity"
        volume_id = ""
        try:
            if identity_path.exists():
                identity = _object(identity_path.read_text(encoding="utf-8"))
                volume_id = str(identity.get("volumeId") or "")
            if not volume_id:
                volume_id = f"vol_{uuid.uuid4().hex}"
                identity_path.write_text(
                    _json({"volumeId": volume_id, "createdAt": now_iso()}),
                    encoding="utf-8",
                )
                diagnostic["volumeChanged"] = not runtime_db.exists() or not checkpoint_db.exists()
        except OSError:
            diagnostic["volumeChanged"] = True
        diagnostic["volumeId"] = volume_id
        try:
            if runtime_db.exists():
                database = sqlite3.connect(f"file:{runtime_db}?mode=ro", uri=True)
                try:
                    diagnostic["runCount"] = int(database.execute(
                        "SELECT COUNT(*) FROM agent_runs"
                    ).fetchone()[0])
                finally:
                    database.close()
        except sqlite3.Error:
            diagnostic["healthy"] = False
            diagnostic["reason"] = "runtime.sqlite 无法读取"
        if (
            not same_directory
            or not runtime_db.exists()
            or (not checkpoint_db.exists() and diagnostic["runCount"] > 0)
        ):
            diagnostic["healthy"] = False
            diagnostic["reason"] = "runtime.sqlite 与 checkpoints.sqlite 不在同一持久卷，或存在运行记录但检查点数据库缺失"
        try:
            if checkpoint_db.exists():
                database = sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)
                try:
                    total = 0
                    for table in database.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall():
                        try:
                            total += int(database.execute(
                                f"SELECT COUNT(*) FROM {table[0]}"
                            ).fetchone()[0])
                        except sqlite3.Error:
                            continue
                    diagnostic["checkpointCount"] = total
                finally:
                    database.close()
        except sqlite3.Error:
            diagnostic["healthy"] = False
            diagnostic["reason"] = "checkpoints.sqlite 无法读取"
        self._recovery_summary = {
            "status": "healthy" if diagnostic["healthy"] else "storage_unhealthy",
            "volumeId": volume_id,
            "runCount": diagnostic["runCount"],
            "checkpointCount": diagnostic["checkpointCount"],
            "checkedAt": diagnostic["checkedAt"],
        }
        return diagnostic

    async def shutdown(self) -> None:
        self._shutting_down = True
        for live in list(self._live.values()):
            if live.task and not live.task.done():
                live.task.cancel()
        tasks = [live.task for live in self._live.values() if live.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_gateway:
            await self._gateway.aclose()
        if self._started:
            from openjiuwen.core.runner.runner import Runner

            await Runner.stop()
            self._started = False
        self.store.close()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "modeling-agent",
            "runtime": "openJiuwen",
            "config": self.settings.public_config(),
            "skills": list_skills(self.settings.skills_dir),
            "storage": self._storage_diag,
            "recovery": self._recovery_summary,
        }

    def project_state(self, project_id: str) -> dict[str, Any]:
        state = self.store.project_state(project_id)
        run = self.store.get_run(state.run_id) if state.run_id else None
        interaction = self.store.pending_interaction(project_id)
        activity_event = (
            self.store.latest_event(project_id, run.id, "progress")
            if run and run.status in {"queued", "running"}
            else None
        )
        focus_event = self.store.latest_event(project_id, run.id, "focus") if run else None
        restored_focus = focus_event.payload.get("target") if focus_event else None
        public_state = state.public()
        if (
            public_state.get("phase") == "evaluation"
            and _claims_terminal_evaluation(str(public_state.get("summary") or ""))
        ):
            latest_evaluation = self.store.latest_presented_evaluation(project_id)
            if latest_evaluation is not None:
                public_state["summary"] = _evaluation_terminal_summary(latest_evaluation)
        recovery = self._recovery_diagnostic(project_id, state)
        if recovery:
            public_state["recovery"] = recovery
        public_state["phaseLabel"] = PHASE_LABELS.get(public_state["phase"], public_state["phase"])
        return {
            "state": public_state,
            "run": self._run_public(run, state) if run else None,
            "interaction": interaction.public() if interaction else None,
            "activity": {
                **activity_event.payload,
                "createdAt": activity_event.created_at,
            } if activity_event else None,
            "focus": restored_focus if isinstance(restored_focus, dict) else None,
            "lastSeq": self.store.latest_event_seq(project_id),
        }

    def _recovery_diagnostic(
        self,
        project_id: str,
        state: Any,
    ) -> dict[str, Any] | None:
        missing: list[str] = []
        if state.run_id and self.store.get_run(state.run_id) is None:
            missing.append("run")
        storage = self._storage_diag or {}
        if not storage.get("healthy", True) and not storage.get("checkpointDbExists", False):
            missing.append("checkpoint")
        status = state.recovery_status or ""
        if not status and missing:
            status = RECOVERY_STALE_RUN if "run" in missing else RECOVERY_VOLUME_MISMATCH
        if not status:
            return None
        next_steps: list[str] = []
        if "run" in missing:
            next_steps.append("安全终止失效计划后重新规划")
        if "checkpoint" in missing:
            next_steps.append("核对建模服务数据卷与 checkpoints.sqlite 后重启")
        if status == RECOVERY_RESUME_FAILED:
            next_steps.append("从最近检查点手动恢复，或取消当前运行后重新规划")
        diagnostic: dict[str, Any] = {"status": status}
        if missing:
            diagnostic["missing"] = missing
        if next_steps:
            diagnostic["nextSteps"] = next_steps
        return diagnostic

    def export_project_transfer(self, project_id: str) -> dict[str, Any]:
        return export_project_transfer(self.store, self.settings.checkpoint_db, project_id)

    def import_project_transfer(self, project_id: str, payload: dict[str, Any]) -> dict[str, str]:
        return import_project_transfer(self.store, self.settings.checkpoint_db, project_id, payload)

    def delete_project_transfer(self, project_id: str) -> None:
        delete_project_transfer(self.store, self.settings.checkpoint_db, project_id)

    async def start_run(
        self,
        *,
        project_id: str,
        token: str,
        message: str,
        baseline_revision: str = "",
        publish_user_message: bool = True,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_reentry_authorized: bool | None = None,
        confirmation_required: bool = False,
        technical_recoveries: dict[str, dict[str, Any]] | None = None,
        validation_repair_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        prompt = message.strip()
        if not prompt:
            raise AgentRuntimeError("INVALID_MESSAGE", "请输入希望助手处理的内容")
        run_id = f"run_{uuid.uuid4().hex}"
        run = self.store.create_run(
            RunRecord(
                id=run_id,
                project_id=project_id,
                session_id=f"project:{project_id}:run:{run_id}",
                status=RUN_QUEUED,
                prompt=prompt,
                token_fingerprint=token_fingerprint(token),
                baseline_revision=baseline_revision,
            )
        )
        for issue_id, recovery in (technical_recoveries or {}).items():
            self.store.save_technical_recovery(run_id, issue_id, recovery)
        if validation_repair_snapshot:
            self.store.save_validation_repair_state(
                run_id, validation_repair_snapshot,
            )
        self._publish_state(project_id, run_id)
        try:
            claimed = await self._claim_workspace(project_id=project_id, run_id=run_id, token=token)
        except Exception as error:
            self.store.update_run(run_id, status=RUN_FAILED, error=f"{type(error).__name__}: {error}")
            self._publish_state(project_id, run_id)
            raise
        if publish_user_message:
            self.events.publish(project_id, run_id, "message", {"role": "user", "blocks": [{"type": "text", "text": prompt}]})
        self.events.publish(project_id, run_id, "status", {"status": RUN_QUEUED, "text": "助手已接手，正在准备"})
        intent_required_tools = (
            set(required_tools)
            if required_tools is not None
            else _required_tools_for_user_intent(prompt)
        )
        live = LiveRun(
            run_id=run_id,
            project_id=project_id,
            token=token,
            tool_profile=run.tool_profile,
            required_tools=intent_required_tools,
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=(
                release_reentry_authorized
                if release_reentry_authorized is not None
                else _release_reentry_requested(prompt)
            ),
            confirmation_required=confirmation_required,
            package_contract=dict(claimed.get("packageContract") or {}),
            autonomous_seed=(
                confirmation_required
                or bool(intent_required_tools)
                or _autonomous_work_requested(prompt)
            ),
            validation_repair=self._validation_repair_state(run_id),
            technical_recoveries=self.store.technical_recoveries(run_id),
        )
        self._live[run_id] = live
        state_snapshot = json.dumps(self.project_state(project_id)["state"], ensure_ascii=False)[:12_000]
        runtime_prompt = (
            f"{prompt}\n\n"
            "# 项目唯一 Agent 状态快照（服务端在本轮启动时注入）\n"
            f"{state_snapshot}\n"
            "执行前仍应调用 get_project_state 获取最新版本；状态转换只能调用 transition_project_state。"
        )
        live.task = asyncio.create_task(self._execute(live, runtime_prompt))
        return self._run_public(run, self.store.project_state(project_id))

    async def handle_material_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        status = str(event.get("status") or "").strip()
        if not event_id or status not in {"parsing", "parsed", "failed"}:
            raise AgentRuntimeError("INVALID_MATERIAL_EVENT", "资料事件不完整", status=400)
        inserted = self.store.record_external_event(event_id, project_id, "material_parse", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        if status == "parsing":
            self.events.publish_external_once(event_id, project_id, run_id, "message", {
                "role": "system",
                "externalEventId": event_id,
                "blocks": [self._material_event_block(event)],
            })
            self.store.mark_external_event_continued(event_id, project_id)
            return {"accepted": True, "duplicate": not inserted, "continued": False}

        batch_id = str(event.get("uploadBatchId") or event.get("documentId") or event_id)
        terminal_events = self.store.material_batch_events(project_id, batch_id)
        batch_size = max(
            [max(1, int(item.get("uploadBatchSize") or 1)) for item in terminal_events],
            default=1,
        )
        if len(terminal_events) < batch_size:
            return {
                "accepted": True,
                "duplicate": not inserted,
                "continued": False,
                "waitingFor": batch_size - len(terminal_events),
            }

        aggregate_id = f"material-batch:{batch_id}:terminal"
        aggregate = self._material_batch_event(aggregate_id, batch_id, terminal_events)
        self.store.record_external_event(aggregate_id, project_id, "material_batch", aggregate)
        self.events.publish_external_once(aggregate_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": aggregate_id,
            "blocks": [self._material_batch_block(terminal_events)],
        })
        self._rewind_for_material_events(project_id, terminal_events)
        if aggregate.get("autoContinue") is False:
            self.store.mark_material_batch_continued(project_id, batch_id)
            self.store.mark_external_event_continued(aggregate_id, project_id)
            return {"accepted": True, "duplicate": not inserted, "continued": False}
        if not self.store.external_event_pending(aggregate_id, project_id):
            self.store.mark_material_batch_continued(project_id, batch_id)
            return {"accepted": True, "duplicate": True, "continued": False}

        parsed_events = [item for item in terminal_events if item.get("status") == "parsed"]
        purpose_review_names = {
            str(item.get("filename")) for item in parsed_events
            if item.get("purposeReviewRequired") is True and str(item.get("filename") or "").strip()
        }
        prompt = self._material_batch_prompt(terminal_events)
        if purpose_review_names:
            required_tools = {"set_material_directive"}
            allowed_tools = PURPOSE_REVIEW_ALLOWED_TOOLS
        else:
            required_tools = {"analyze_material_impact"} if parsed_events else set()
            if any(item.get("readingMode") == "full" for item in parsed_events):
                required_tools.add("read_documents_fully")
            allowed_tools = None
        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=prompt,
            required_tools=required_tools,
            required_material_directives=purpose_review_names,
            allowed_tools=allowed_tools,
            event=aggregate,
        )
        self.store.mark_material_batch_continued(project_id, batch_id)
        self.store.mark_external_event_continued(aggregate_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    async def handle_package_import_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        baseline_revision = str(event.get("baselineRevision") or "").strip()
        package_name = str(event.get("packageName") or "").strip()
        package_version = str(event.get("packageVersion") or "").strip()
        if not event_id or not baseline_revision or not package_name or not package_version:
            raise AgentRuntimeError("INVALID_PACKAGE_IMPORT_EVENT", "导入基线事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "package_import", event)
        state = self.store.project_state(project_id)
        if state.revision != 0 or state.state != "intake:idle":
            self.store.mark_external_event_continued(event_id, project_id)
            return {
                "accepted": True,
                "duplicate": not inserted,
                "initialized": False,
                "preserved": True,
                "state": state.public(),
            }

        self.events.publish_external_once(event_id, project_id, "", "message", {
            "role": "assistant",
            "externalEventId": event_id,
            "blocks": [{
                "type": "text",
                "text": (
                    f"已把“{package_name}” {package_version} 作为当前增量基线。"
                    "你现在可以直接补充或替换资料；解析完成后，我会自动展示保持有效、"
                    "需要处理的内容和证据链，再形成增量候选。确认前，当前基线保持不变。"
                ),
            }],
        })
        state = self.store.transition_project_state(
            project_id,
            expected_revision=state.revision,
            next_state="increment:idle",
            cause="package_imported",
            summary=(
                f"已导入知识包“{package_name}” {package_version} 作为增量基线；"
                "尚未运行本项目的真实业务评测。"
            ),
            metadata={
                "eventId": event_id,
                "baselineRevision": baseline_revision,
                "packageName": package_name,
                "packageVersion": package_version,
                "sourceFilename": str(event.get("sourceFilename") or ""),
                "fileCount": int(event.get("fileCount") or 0),
            },
        )
        public = state.public()
        public["phaseLabel"] = PHASE_LABELS["increment"]
        self.events.publish(project_id, "", "state", public)
        self.store.mark_external_event_continued(event_id, project_id)
        return {
            "accepted": True,
            "duplicate": not inserted,
            "initialized": True,
            "preserved": False,
            "state": state.public(),
        }

    async def handle_evaluation_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        round_id = str(event.get("roundId") or "").strip()
        status = str(event.get("status") or "").strip()
        if not event_id or not round_id or status not in {"done", "failed", "cancelled"}:
            raise AgentRuntimeError("INVALID_EVALUATION_EVENT", "评测终态事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "evaluation_terminal", event)
        if not self.store.external_event_pending(event_id, project_id):
            return {"accepted": True, "duplicate": True, "reconciled": False}

        latest = self.store.latest_presented_evaluation(
            project_id,
            exclude_event_id=event_id,
        )
        if latest is not None and _evaluation_event_is_stale(event, latest):
            self.store.mark_external_event_continued(event_id, project_id)
            return {
                "accepted": True,
                "duplicate": not inserted,
                "reconciled": False,
                "stale": True,
                "status": status,
                "roundId": round_id,
            }

        for live in self._live.values():
            if live.project_id == project_id:
                live.stable_read_cache.clear()

        summary = _evaluation_terminal_summary(event)
        active = self.store.active_run(project_id)
        run_id = active.id if active else self.store.project_state(project_id).run_id
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": summary}],
        })

        current = self.store.project_state(project_id)
        phase, _separator, _execution = current.state.partition(":")
        reconciled = False
        if phase == "evaluation":
            updated = self.store.transition_project_state(
                project_id,
                expected_revision=current.revision,
                next_state=current.state,
                cause="evaluation_terminal",
                actor_run_id=run_id,
                summary=summary,
                open_questions=[
                    item for item in current.open_questions
                    if not _claims_active_evaluation(item)
                ],
                metadata={
                    "eventId": event_id,
                    "roundId": round_id,
                    "status": status,
                    "done": max(0, int(event.get("done") or 0)),
                    "total": max(0, int(event.get("total") or 0)),
                    "passed": max(0, int(event.get("passed") or 0)),
                    "finishedAt": str(event.get("finishedAt") or ""),
                },
            )
            public = updated.public()
            public["phaseLabel"] = PHASE_LABELS.get(phase, phase)
            self.events.publish(project_id, run_id, "state", public)
            reconciled = True

        self.store.mark_external_event_continued(event_id, project_id)
        return {
            "accepted": True,
            "duplicate": not inserted,
            "reconciled": reconciled,
            "status": status,
            "roundId": round_id,
        }

    async def handle_studio_review_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        client_mutation_id = str(event.get("clientMutationId") or "").strip()
        request_hash = str(event.get("requestHash") or "").strip()
        if not event_id or not client_mutation_id or len(request_hash) != 64:
            raise AgentRuntimeError("INVALID_STUDIO_EVENT", "知识复核事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "studio_review", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": self._studio_event_message(event)}],
        })
        if not self.store.external_event_pending(event_id, project_id):
            return {"accepted": True, "duplicate": True, "continued": False}
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None:
            raise AgentRuntimeError(
                "STUDIO_EVENT_WAITING_CONFIRMATION",
                "当前还有一项决定等待处理，新的知识修改会在这项决定处理后继续核对",
                status=409,
            )

        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=self._studio_event_prompt(event),
            required_tools={"diff"},
            event=event,
            confirmation_required=True,
        )
        self.store.mark_external_event_continued(event_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    async def handle_fusion_review_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        plan_id = str(event.get("planId") or "").strip()
        candidate_id = str(event.get("candidateId") or "").strip()
        if not event_id or not plan_id or not candidate_id:
            raise AgentRuntimeError("INVALID_FUSION_EVENT", "融合复核事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "fusion_review", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": self._fusion_event_message(event)}],
        })
        if not self.store.external_event_pending(event_id, project_id):
            return {"accepted": True, "duplicate": True, "continued": False}
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None:
            raise AgentRuntimeError(
                "FUSION_EVENT_WAITING_CONFIRMATION",
                "当前还有一项决定等待处理，融合候选会在这项决定处理后继续核对",
                status=409,
            )

        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=self._fusion_event_prompt(event),
            required_tools={"compile_package_fusion_plan", "diff"},
            event=event,
            confirmation_required=True,
        )
        self.store.mark_external_event_continued(event_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    @staticmethod
    def _studio_event_message(event: dict[str, Any]) -> str:
        count = len(event.get("elementIds") or [])
        scope = f"，涉及 {count} 个知识条目" if count else ""
        return (
            f"知识包 Studio 的修改已交给助手复核{scope}。"
            "助手会先核对整批变更，再请你整体确认；确认前不会改变当前版本。"
        )

    @staticmethod
    def _fusion_event_message(event: dict[str, Any]) -> str:
        source_count = max(2, int(event.get("sourceCount") or 2))
        output_name = str(event.get("outputName") or "融合知识包")
        return (
            f"“{output_name}”的融合候选已交给助手整体复核，包含 {source_count} 个来源知识包。"
            "助手会核对完整差异和质量结果，再请你一次确认；确认前不会改变当前版本。"
        )

    @staticmethod
    def _studio_event_prompt(event: dict[str, Any]) -> str:
        summary = str(event.get("summary") or "知识包 Studio 已形成待确认修改")
        element_ids = [str(value) for value in event.get("elementIds") or []]
        return (
            "服务端已持久化一项知识包 Studio 整体复核请求。"
            "这不是用户对话消息，不得伪造用户发言，也不要重复编辑这些内容。"
            "先读取项目唯一状态，再调用 diff 并令 rev_b='candidate' 核对整批真实差异；"
            "把整体差异定位到中栏，然后立即调用 request_confirmation 展示一个整体确认卡。"
            "只能展示候选，绝不能替用户批准或直接提交。不要向用户复述工具名、参数或内部标识。\n"
            f"业务摘要：{summary}\n"
            f"涉及的知识条目：{json.dumps(element_ids, ensure_ascii=False)}"
        )

    @staticmethod
    def _fusion_event_prompt(event: dict[str, Any]) -> str:
        summary = str(event.get("summary") or "多来源融合已形成整体候选")
        plan_id = str(event.get("planId") or "")
        candidate_id = str(event.get("candidateId") or "")
        return (
            "服务端已持久化一项多来源融合整体复核请求。"
            "这不是用户对话消息，不得伪造用户发言，也不要重新建立或改写融合方案。"
            "先读取项目唯一状态并接续候选上下文，调用 compile_package_fusion_plan 挂接已生成的候选；"
            "再调用 diff 并令 rev_b='candidate' 核对整批真实差异，随后定位整体差异并立即调用 "
            "request_confirmation 展示一个整体确认卡。只能展示候选，绝不能替用户批准或直接提交。"
            "不要向用户复述工具名、参数、计划编号、候选编号或其他内部标识。\n"
            f"业务摘要：{summary}\n"
            f"融合计划：{plan_id}\n"
            f"候选校验值：{candidate_id}"
        )

    def _rewind_for_material_events(self, project_id: str, events: list[dict[str, Any]]) -> None:
        changed = [item for item in events if item.get("status") == "parsed" and item.get("semanticChanged") is True]
        if not changed:
            return
        target = "goal" if any(item.get("purpose") == "goal" for item in changed) else "knowledge"
        current = self.store.project_state(project_id)
        phase, _separator, execution = current.state.partition(":")
        if phase not in PHASE_ORDER or PHASE_ORDER.index(phase) <= PHASE_ORDER.index(target):
            return
        updated = self.store.transition_project_state(
            project_id,
            expected_revision=current.revision,
            next_state=f"{target}:{execution}",
            cause="material_parse_completed",
            actor_run_id=current.run_id,
            metadata={
                "eventIds": [item.get("eventId") for item in changed],
                "documentIds": [item.get("documentId") for item in changed],
                "purposes": [item.get("purpose") for item in changed],
                "semanticChanged": True,
            },
        )
        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS[target]
        self.events.publish(project_id, current.run_id, "state", public)

    @staticmethod
    def _material_event_block(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "upload",
            "files": [{
                "id": event.get("documentId"),
                "documentId": event.get("documentId"),
                "name": event.get("filename"),
                "status": "parsing",
                "purpose": event.get("purpose") or "auto",
                "readingMode": event.get("readingMode") or "on_demand",
            }],
        }

    @staticmethod
    def _material_batch_block(events: list[dict[str, Any]]) -> dict[str, Any]:
        details = []
        for event in events:
            detail = {
                "documentId": event.get("documentId"),
                "name": event.get("filename"),
                "status": event.get("status"),
            }
            if event.get("parseError"):
                detail["error"] = event["parseError"]
            details.append(detail)
        parsed = len([item for item in events if item.get("status") == "parsed"])
        return {"type": "uploadResult", "parsed": parsed, "failed": len(events) - parsed, "details": details}

    @staticmethod
    def _material_batch_event(
        event_id: str,
        batch_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        parsed = [item for item in events if item.get("status") == "parsed"]
        return {
            "eventId": event_id,
            "status": "parsed" if parsed else "failed",
            "uploadBatchId": batch_id,
            "autoContinue": any(item.get("autoContinue") is not False for item in events),
            "filename": "、".join(str(item.get("filename") or "新资料") for item in events),
            "filenames": [item.get("filename") for item in parsed],
            "parsed": len(parsed),
            "failed": len(events) - len(parsed),
            "events": events,
        }

    @staticmethod
    def _material_batch_prompt(events: list[dict[str, Any]]) -> str:
        parsed = [item for item in events if item.get("status") == "parsed"]
        failed = [item for item in events if item.get("status") == "failed"]
        impact_calls = "；".join(
            f"《{item.get('filename')}》使用 source='{item.get('sourceKey') or item.get('documentId')}'"
            for item in parsed
        )
        full_read_names = [str(item.get("filename")) for item in parsed if item.get("readingMode") == "full"]
        purpose_review_names = [
            str(item.get("filename")) for item in parsed if item.get("purposeReviewRequired") is True
        ]
        if purpose_review_names:
            return (
                f"服务端资料批次已经全部结束：解析成功 {len(parsed)}/{len(events)}，失败 {len(failed)} 份。"
                f"以下资料尚未确认用途：{purpose_review_names!r}。这是独立的用途预判轮。"
                "逐份轻量预判用途：先使用资料清单中的文件名、类型和目录；普通文档只有判断依据不足时才按需读取开头片段，"
                "表格只读取全部 Sheet 结构而不读取全量数据行。逐份提交明确的文件级用途，不能保留为“由助手决定”；"
                "表格同时提交全部 Sheet 建议。完成所有建议后定位第一份待确认资料，立即结束本轮并等待用户逐份确认。"
                "本轮禁止执行影响分析、全文精读、候选写入、目标确认或阶段推进；全文精读设置留到全部用途确认后执行。"
                + ("说明失败资料的补救方式，且不要假装已经读到正文。" if failed else "")
                + "内部返回字段、参数名和调用步骤只供运行时使用，绝不能向用户复述。"
            )
        return (
            f"服务端资料批次已经全部结束：解析成功 {len(parsed)}/{len(events)}，失败 {len(failed)} 份。"
            f"逐份调用 analyze_material_impact 核对成功资料：{impact_calls or '没有可分析的成功资料'}。"
            "如果首次分析表明影响范围尚未界定，只能先展示正在界定范围；读完资料和相关知识后，"
            "再按工具契约逐份提交直接影响起点。两份以上资料都界定完成后，必须按同一批资料再取得一次"
            "核心去重的合并影响视图并原样显示在中栏，不得自行合并或估算数量。"
            "语义未变化就说明无需候选，有影响则从唯一项目状态主动继续。"
            + (f"必须调用 read_documents_fully 一次并传入 filenames={full_read_names!r} 完成全文覆盖。" if full_read_names else "")
            + ("说明失败资料的补救方式，且不要假装已经读到正文。" if failed else "")
            + "内部返回字段、参数名和调用步骤只供运行时使用，绝不能向用户复述。"
        )

    async def _continue_for_external_event(
        self,
        *,
        project_id: str,
        token: str,
        prompt: str,
        required_tools: set[str],
        event: dict[str, Any],
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        run = self.store.active_run(project_id)
        if run is None:
            await self.start_run(
                project_id=project_id,
                token=token,
                message=prompt,
                publish_user_message=False,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        execution = self._execution(project_id, run.id)
        interaction = self.store.pending_interaction(project_id)
        if interaction and interaction.run_id == run.id:
            if interaction.kind == "material_request" and event.get("status") == "parsed":
                answer = {
                    "action": "uploaded",
                    "files": event.get("filenames") or [],
                    "parsed": event.get("parsed") or 0,
                    "failed": event.get("failed") or 0,
                }
                self.store.answer_interaction(interaction.id, answer)
                self.events.publish(project_id, run.id, "interaction", {
                    "id": interaction.id, "status": "answered", "answer": answer,
                })
                self._persist_latest_interaction(
                    run,
                    interaction=interaction,
                    answer=answer,
                    continuation=prompt,
                    action_key="uploaded",
                    requires_artifact=_answer_requires_script_artifact(interaction, answer),
                )
                await self._resume(
                    run, token, {interaction.tool_call_id: answer},
                    required_tools=required_tools,
                    required_material_directives=required_material_directives,
                    allowed_tools=allowed_tools,
                    release_reentry_authorized=True,
                    confirmation_required=confirmation_required,
                )
                return
            answer = {"action": "interject", "text": prompt}
            self.store.answer_interaction(interaction.id, answer)
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key="interject",
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            await self._resume(
                run, token, {interaction.tool_call_id: answer},
                restore_interaction=interaction,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_SUSPENDED:
            await self._continue(
                run, token, prompt,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_WAITING:
            await self._continue(
                run, token, prompt,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        live = self._live.get(run.id)
        if live is None or execution not in {RUN_QUEUED, RUN_RUNNING}:
            raise AgentRuntimeError("RUN_NOT_STEERABLE", "当前运行暂时不能接收资料事件", status=409)
        steering_id = self.store.queue_steering(project_id, run.id, prompt)
        live.token = token
        live.stable_read_cache.clear()
        live.required_tools.update(required_tools)
        live.required_material_directives.update(required_material_directives or ())
        if allowed_tools is not None:
            live.allowed_tools = allowed_tools
        live.release_reentry_authorized = True
        live.confirmation_required = live.confirmation_required or confirmation_required
        await live.steering.put(prompt)
        self.store.mark_steering_delivered(steering_id)

    async def answer_interaction(
        self,
        *,
        project_id: str,
        interaction_id: str,
        token: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        if not token:
            raise AgentRuntimeError("TOKEN_REQUIRED", "需要登录后才能回答", status=401)
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None:
            raise AgentRuntimeError("INTERACTION_NOT_FOUND", "待答问题不存在", status=404)
        if interaction.project_id != project_id:
            raise AgentRuntimeError("INTERACTION_NOT_FOUND", "待答问题不存在", status=404)
        required_text_option = _required_text_option(interaction.payload, answer)
        if required_text_option is not None and not str(answer.get("text") or "").strip():
            raise AgentRuntimeError(
                "INTERACTION_TEXT_REQUIRED",
                str(required_text_option.get("textPrompt") or "请补充具体说明后再提交"),
                status=400,
            )
        direct_confirmation = (
            interaction.kind == "confirmation"
            and answer.get("action") != "interject"
        )
        direct_release = (
            interaction.kind == "release_confirmation"
            and answer.get("action") != "interject"
            and answer.get("approved") is True
            and answer.get("coverage_confirmed") is True
        )
        restored_confirmation = (
            direct_confirmation
            and interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX)
        )
        confirmation_result: dict[str, Any] | None = None
        release_result: dict[str, Any] | None = None
        if direct_confirmation:
            approved = answer.get("approved") is True
            validation_receipt = interaction.payload.get("validationReceipt")
            if not isinstance(validation_receipt, dict):
                validation_receipt = {}
            confirmation_result = await self._gateway.call(
                project_id=project_id,
                run_id=interaction.run_id,
                token=token,
                tool="request_confirmation" if approved else "request_confirmation_rejected",
                arguments={
                    "summary": str(interaction.payload.get("summary") or interaction.payload.get("question") or "确认待修改内容"),
                    "change_groups": interaction.payload.get("changeGroups") or [],
                    "expected_candidate_revision": str(interaction.payload.get("candidateRevision") or ""),
                    "expected_candidate_hash": str(validation_receipt.get("candidateHash") or ""),
                    "expected_validator_version": str(validation_receipt.get("validatorVersion") or ""),
                    "review": dict(answer),
                },
            )
            if confirmation_result.get("error"):
                reason = str(confirmation_result.get("reason") or "待修改内容没有成功结算")
                error_code = str(confirmation_result.get("error") or "CONFIRMATION_FAILED")
                if error_code in {"CANDIDATE_REVALIDATION_REQUIRED", "STALE_CONFIRMATION"}:
                    decision = "接受" if approved else "退回"
                    superseded_answer = {
                        "approved": False,
                        "action": "superseded",
                        "reason": reason,
                    }
                    self.store.answer_interaction(interaction.id, superseded_answer)
                    run = self.store.require_run(interaction.run_id)
                    self.events.publish(run.project_id, run.id, "interaction", {
                        "id": interaction.id,
                        "status": "answered",
                        "answer": superseded_answer,
                    })
                    self.events.publish(run.project_id, run.id, "message", {
                        "role": "system",
                        "blocks": [{
                            "type": "text",
                            "text": f"刚才的待确认内容已变化，{decision}操作未生效；助手正在重新校验，新的内容需要再次确认。",
                        }],
                    })
                    self._persist_latest_interaction(
                        run,
                        interaction=interaction,
                        answer=answer,
                        continuation="确认卡已经失效，用户没有应用任何候选；继续执行本次预期。",
                        action_key=str(answer.get("action") or "superseded"),
                        requires_artifact=_answer_requires_script_artifact(interaction, answer),
                    )
                    return await self._resume(
                        run,
                        token,
                        {interaction.tool_call_id: {
                            "action": "settled_confirmation",
                            "result": confirmation_result,
                            "continuation": (
                                f"旧确认卡已经作废，用户的{decision}没有应用到任何候选。"
                                "立即重新执行完整预检；若有 findings，自动修正后重新预检。"
                                "只有通过后才能展示新的确认卡，绝不能沿用刚才的决定。"
                            ),
                        }},
                        autonomous_seed=True,
                    )
                failed_interaction = self.store.set_interaction_submission_error(interaction.id, reason)
                if failed_interaction is not None:
                    self.events.publish(
                        interaction.project_id,
                        interaction.run_id,
                        "interaction",
                        failed_interaction.public(),
                    )
                raise AgentRuntimeError(
                    error_code,
                    reason,
                    status=409,
                    details={"next": confirmation_result.get("next") or []},
                )
        if direct_release:
            release_result = await self._gateway.call(
                project_id=project_id,
                run_id=interaction.run_id,
                token=token,
                tool="request_release",
                arguments={
                    "summary": str(
                        interaction.payload.get("summary")
                        or interaction.payload.get("question")
                        or "确认正式发布"
                    ),
                    "version": str(
                        answer.get("version")
                        or interaction.payload.get("version")
                        or ""
                    ),
                    "review": dict(answer),
                },
            )
            if release_result.get("error"):
                reason = str(release_result.get("reason") or "正式发布没有成功完成")
                error_code = str(release_result.get("error") or "RELEASE_FAILED")
                failed_interaction = self.store.set_interaction_submission_error(interaction.id, reason)
                if failed_interaction is not None:
                    self.events.publish(
                        interaction.project_id,
                        interaction.run_id,
                        "interaction",
                        failed_interaction.public(),
                    )
                raise AgentRuntimeError(
                    error_code,
                    reason,
                    status=409,
                    details={"next": release_result.get("next") or []},
                )
        self.store.answer_interaction(interaction_id, answer)
        run = self.store.require_run(interaction.run_id)
        self.events.publish(run.project_id, run.id, "interaction", {"id": interaction.id, "status": "answered", "answer": answer})
        self.events.publish(run.project_id, run.id, "message", {"role": "user", "blocks": [{"type": "answer", "interactionId": interaction.id, "answer": answer}]})
        required_tools = _required_tools_for_user_intent(
            f"{interaction.payload.get('question') or ''}\n{json.dumps(answer, ensure_ascii=False)}"
        )
        if direct_confirmation:
            approved = answer.get("approved") is True
            prompt = (
                "用户已经正式接受了候选修改，服务端已将该卡片绑定的候选写入当前修订。"
                if approved else
                "用户已经正式退回了候选修改，原候选已关闭。"
            )
            prompt += (
                f"\n用户反馈：{str(answer.get('feedback') or '').strip()}\n"
                f"确认结果：{json.dumps(confirmation_result or {}, ensure_ascii=False)}\n"
                "从当前项目继续主动完成下一步；不要再次询问已经结算的同一项确认。"
            )
            if restored_confirmation:
                self._persist_latest_interaction(
                    run,
                    interaction=interaction,
                    answer=answer,
                    continuation=prompt,
                    action_key=str(answer.get("action") or "confirm"),
                    requires_artifact=_answer_requires_script_artifact(interaction, answer),
                )
                return await self._continue(
                    run,
                    token,
                    prompt,
                    required_tools=required_tools,
                    autonomous_seed=True,
                )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "confirm"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._resume(
                run,
                token,
                {interaction.tool_call_id: {
                    "action": "settled_confirmation",
                    "result": confirmation_result or {},
                    "continuation": prompt,
                }},
                required_tools=required_tools,
                autonomous_seed=True,
            )
        if direct_release and release_result is not None:
            version = str(release_result.get("version") or answer.get("version") or "")
            prompt = (
                f"用户已确认真实用例覆盖，服务端已完成稳定版本 {version} 的正式发布。\n"
                f"发布结果：{json.dumps(release_result, ensure_ascii=False)}\n"
                "只依据该发布结果说明已完成的版本和交付状态；不要再次发起发布确认。"
            )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "release"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._resume(
                run,
                token,
                {interaction.tool_call_id: {
                    "action": "settled_release",
                    "result": release_result,
                    "continuation": prompt,
                }},
                required_tools=required_tools,
                autonomous_seed=True,
            )
        if interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX):
            prompt = (
                "这是用户对先前待确认问题的正式答复。\n"
                f"原问题：{interaction.payload.get('question') or '待确认问题'}\n"
                f"用户答复：{json.dumps(answer, ensure_ascii=False)}\n"
                "请把答复作为明确约束，从当前项目和同一会话继续主动建模。"
                "不要重复询问已经回答的问题；下一次确实需要用户决定时再调用 ask 或 request_confirmation。"
            )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "interject"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._continue(
                run,
                token,
                prompt,
                required_tools=required_tools,
                autonomous_seed=True,
            )
        self._persist_latest_interaction(
            run,
            interaction=interaction,
            answer=answer,
            continuation="",
            action_key=str(answer.get("action") or ""),
            requires_artifact=_answer_requires_script_artifact(interaction, answer),
        )
        return await self._resume(
            run,
            token,
            {interaction.tool_call_id: answer},
            required_tools=required_tools,
            confirmation_required=bool(interaction.payload.get("overallConfirmationRequired")),
            release_declined=(
                interaction.kind == "release_confirmation"
                and answer.get("action") != "interject"
                and (answer.get("approved") is not True or answer.get("coverage_confirmed") is not True)
            ),
            autonomous_seed=True,
        )

    async def steer(
        self,
        *,
        project_id: str,
        token: str,
        text: str,
        purpose_review_completed: bool = False,
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        message = text.strip()
        if not message:
            raise AgentRuntimeError("INVALID_MESSAGE", "请输入要补充或追问的内容")
        run = self.store.active_run(project_id)
        if run is None:
            return {"mode": "new_run", "run": await self.start_run(project_id=project_id, token=token, message=message)}
        execution = self._execution(project_id, run.id)

        interaction = self.store.pending_interaction(project_id)
        if interaction and interaction.run_id == run.id:
            answer = {"action": "interject", "text": message}
            self.store.answer_interaction(interaction.id, answer)
            self.events.publish(
                project_id,
                run.id,
                "message",
                {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "interaction_boundary"},
            )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation="",
                action_key="interject",
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            resumed = await self._resume(
                run,
                token,
                {interaction.tool_call_id: answer},
                restore_interaction=interaction,
            )
            return {"mode": "interjection", "run": resumed}

        if execution == RUN_SUSPENDED:
            steering_id = self.store.queue_steering(project_id, run.id, message)
            resumed = await self._continue(
                run,
                token,
                message,
                required_tools=_required_tools_for_user_intent(message),
                autonomous_seed=True,
            )
            self.store.mark_steering_delivered(steering_id)
            self.events.publish(
                project_id,
                run.id,
                "message",
                {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "checkpoint_resume"},
            )
            return {"mode": "checkpoint_resume", "run": resumed}

        live = self._live.get(run.id)
        if live is None or execution not in {RUN_QUEUED, RUN_RUNNING}:
            raise AgentRuntimeError("RUN_NOT_STEERABLE", "当前运行暂时不能接收插话", status=409)
        if purpose_review_completed and live.allowed_tools == PURPOSE_REVIEW_ALLOWED_TOOLS:
            if live.deferred_purpose_continuation is None:
                steering_id = self.store.queue_steering(project_id, run.id, message)
                live.deferred_purpose_continuation = (steering_id, token, message)
                self.events.publish(project_id, run.id, "message", {
                    "role": "user",
                    "blocks": [{"type": "text", "text": message}],
                    "delivery": "after_purpose_review",
                })
            return {
                "mode": "after_purpose_review",
                "runId": run.id,
                "delivery": "after_purpose_review",
            }
        steering_id = self.store.queue_steering(project_id, run.id, message)
        live.token = token
        live.stable_read_cache.clear()
        live.required_tools.update(_required_tools_for_user_intent(message))
        live.release_reentry_authorized = (
            live.release_reentry_authorized or _release_reentry_requested(message)
        )
        await live.steering.put(message)
        self.store.mark_steering_delivered(steering_id)
        self.events.publish(project_id, run.id, "message", {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "next_tool_boundary"})
        return {"mode": "steering", "runId": run.id, "delivery": "next_tool_boundary"}

    async def resume_issue(
        self,
        *,
        project_id: str,
        token: str,
        issue_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        normalized_issue = str(issue_id or "").strip()
        normalized_action = str(action_id or "").strip()
        if not normalized_issue or not normalized_action:
            raise AgentRuntimeError(
                "ISSUE_ACTION_INVALID", "恢复动作缺少问题身份或动作标识", status=400,
            )
        state = self.store.project_state(project_id)
        run = self.store.active_run(project_id) or (
            self.store.get_run(state.run_id) if state.run_id else None
        )
        if run is None or run.project_id != project_id:
            raise AgentRuntimeError("ISSUE_NOT_FOUND", "当前没有可恢复的问题", status=404)

        technical = self.store.technical_recoveries(run.id).get(normalized_issue)
        repair_snapshot = self.store.validation_repair_state(run.id) or {}
        repair_issue_ids = {
            str(item) for item in repair_snapshot.get("openIssueIds", []) if str(item).strip()
        }
        last_report = repair_snapshot.get("lastReport")
        if isinstance(last_report, dict) and last_report.get("issueId"):
            repair_issue_ids.add(str(last_report["issueId"]))
        is_validation_issue = normalized_issue in repair_issue_ids
        execution = self._execution(project_id, run.id)
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None and interaction.run_id == run.id:
            raise AgentRuntimeError(
                "ISSUE_WAITING_INTERACTION",
                "当前还有一项业务决定等待处理，请先完成该决定",
                status=409,
            )

        if normalized_action == "keep_current_progress":
            if technical is None and not is_validation_issue:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "这个问题已经解决或被后续状态替代", status=409,
                )
            if technical is not None:
                self.store.resolve_technical_recoveries(run.id, [normalized_issue])
            if is_validation_issue:
                self._validation_repair_state(run.id).reset()
            live = self._live.get(run.id)
            if live is not None:
                live.technical_recoveries.pop(normalized_issue, None)
                if technical is not None:
                    live.required_tools.discard(str(technical.get("requiredTool") or ""))
            if execution in {RUN_QUEUED, RUN_RUNNING, RUN_STOPPING}:
                settled_run = await self.cancel(project_id=project_id)
            else:
                updated = self.store.update_run(
                    run.id,
                    status=RUN_CANCELLED,
                    result="已保留当前成果并结束本轮",
                    error="",
                )
                self._publish_state(project_id, run.id)
                self.events.publish(project_id, run.id, "status", {
                    "status": RUN_CANCELLED,
                    "text": "已结束本轮，已完成的候选内容保留",
                })
                settled_run = self._run_public(updated, self.store.project_state(project_id))
            self.events.publish(project_id, run.id, "tool", {
                "name": "issue_recovery",
                "phase": "completed",
                "summary": "已保留当前成果并结束本轮",
                "issueStatus": "resolved",
                "resolvedIssueIds": [normalized_issue],
            })
            return {
                "mode": "kept",
                "run": settled_run,
                "issueId": normalized_issue,
                "resolvedIssueIds": [normalized_issue],
            }

        if technical is not None:
            available_actions = {
                str(item.get("id") or "").strip()
                for item in technical.get("recoveryActions") or []
                if isinstance(item, dict)
            }
            available_actions.add(str(technical.get("actionId") or "resume_required_tool"))
            available_actions.discard("")
            if normalized_action not in available_actions:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "当前问题的恢复动作已经变化，请刷新后重试", status=409,
                )
            technical = dict(technical)
            if normalized_action == "retry_from_latest":
                technical.update({"attempt": 0, "exhausted": False})
                self.store.save_technical_recovery(run.id, normalized_issue, technical)
            required_tools = {str(technical.get("requiredTool") or "").strip()}
            required_tools.discard("")
            recovery_prompt = (
                "页面请求基于最新内容继续一个版本同步问题。不要把它当成用户业务发言；"
                "先清除旧参数，重新读取当前建模计划和涉及的资源快照，"
                "再用新的 mutation_id 生成 Change Set。"
                "只处理尚未完成的任务，不得原样重放旧参数。"
                if technical.get("strategy") == "refresh_change_set"
                else (
                    "页面请求继续一个仍然有效的系统恢复问题。不要把它当成用户业务发言；"
                    "立即按已持久化的 required tool 恢复，成功后重新校验候选。"
                )
            )
            initial_recoveries = {normalized_issue: technical}
            initial_repair = None
        elif is_validation_issue:
            if normalized_action != "review_validation_findings":
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "当前问题的恢复动作已经变化，请刷新后重试", status=409,
                )
            report_id = str(
                last_report.get("validationReportId")
                if isinstance(last_report, dict) else ""
            ).strip()
            finding_fingerprint = str(
                last_report.get("findingFingerprint")
                if isinstance(last_report, dict) else ""
            ).strip()
            self._validation_repair_state(run.id).reset()
            repair_snapshot = self.store.validation_repair_state(run.id) or {}
            if report_id:
                required_tools = {"get_validation_summary"}
                recovery_prompt = (
                    "页面请求继续当前校验问题。不要重做已经完成的修改；"
                    f"先读取校验报告 {report_id} 的最新摘要和分页明细，"
                    "只处理仍未解决的 finding，随后执行完整候选校验。"
                )
            else:
                required_tools = {"validate_change_set"}
                recovery_prompt = (
                    "页面请求继续当前校验问题。不要重做已经完成的修改；"
                    "先对当前候选重新执行一次完整 Change Set 校验，再只处理仍未解决的 finding。"
                    f"上一轮问题指纹为 {finding_fingerprint}。"
                )
            initial_recoveries = None
            initial_repair = repair_snapshot
        else:
            raise AgentRuntimeError(
                "ISSUE_ACTION_STALE", "这个问题已经解决或被后续状态替代", status=409,
            )

        self.events.publish(project_id, run.id, "tool", {
            "name": "issue_recovery",
            "phase": "repairing",
            "summary": "已按你的选择从当前进展继续处理",
            "code": "RECOVERY_RESUMED",
            "issueId": normalized_issue,
            "issueStatus": "repairing",
            "problemVisibility": "activity",
        })
        if execution in {RUN_SUSPENDED, RUN_WAITING}:
            resumed = await self._continue(
                run,
                token,
                recovery_prompt,
                required_tools=required_tools,
                autonomous_seed=True,
            )
            return {"mode": "checkpoint_resume", "run": resumed, "issueId": normalized_issue}
        live = self._live.get(run.id)
        if live is not None and execution in {RUN_QUEUED, RUN_RUNNING}:
            live.token = token
            live.required_tools.update(required_tools)
            if technical is not None:
                live.technical_recoveries[normalized_issue] = technical
            live.stable_read_cache.clear()
            await live.steering.put(recovery_prompt)
            return {
                "mode": "steering",
                "runId": run.id,
                "issueId": normalized_issue,
                "delivery": "next_tool_boundary",
            }

        started = await self.start_run(
            project_id=project_id,
            token=token,
            message=recovery_prompt,
            publish_user_message=False,
            required_tools=required_tools,
            release_reentry_authorized=True,
            technical_recoveries=initial_recoveries,
            validation_repair_snapshot=initial_repair,
        )
        if technical is not None:
            self.store.resolve_technical_recoveries(run.id, [normalized_issue])
        return {"mode": "new_run", "run": started, "issueId": normalized_issue}

    async def _state_action(
        self,
        live: LiveRun,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "get_project_state":
            return await self._state_for_agent(live)

        current = self.store.project_state(live.project_id)
        expected = arguments.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != current.revision:
            raise AgentRuntimeError(
                "STATE_REVISION_CONFLICT",
                "项目 Agent 状态已经变化，请重新读取",
                status=409,
                details={"expectedRevision": expected, "currentRevision": current.revision},
            )
        phase, _separator, execution = current.state.partition(":")
        if current.run_id != live.run_id or execution != RUN_RUNNING:
            raise AgentRuntimeError(
                "STATE_TRANSITION_INVALID",
                "只有当前运行中的 Agent 可以更新项目状态",
                status=409,
                details={"state": current.state, "runId": current.run_id},
            )
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            raise AgentRuntimeError("STATE_SUMMARY_REQUIRED", "更新状态时必须提供精简摘要", status=400)
        questions = _string_list(arguments.get("open_questions"), 30)
        evidence = _string_list(arguments.get("evidence"), 50)
        evaluation = await self._evaluation_status_for_agent(live)
        claims_running = (
            _claims_active_evaluation(summary)
            or any(_claims_active_evaluation(item) for item in questions)
        )
        if claims_running and evaluation.get("available") is not True:
            raise AgentRuntimeError(
                "STATE_EVALUATION_STATUS_UNAVAILABLE",
                "尚未取得权威评测状态，不能把评测记录为仍在运行",
                status=409,
                details={
                    "evaluation": evaluation,
                    "next": ["重新调用 get_project_state，取得可用的 evaluation 快照后再更新"],
                },
            )
        if evaluation.get("terminal") is True and claims_running:
            raise AgentRuntimeError(
                "STATE_EVALUATION_STATUS_CONFLICT",
                "最近一轮评测已经结束，不能把它记录为仍在运行",
                status=409,
                details={
                    "evaluation": evaluation,
                    "next": ["以 get_project_state 返回的 evaluation 终态更新摘要和开放问题"],
                },
            )

        if name == "update_project_context":
            updated = self.store.transition_project_state(
                live.project_id,
                expected_revision=current.revision,
                next_state=current.state,
                cause="context_updated",
                actor_run_id=live.run_id,
                summary=summary,
                open_questions=questions,
                metadata={"evidence": evidence},
            )
        elif name == "transition_project_state":
            target = str(arguments.get("to_phase") or "")
            allowed = PHASE_EDGES.get(phase, ())
            if target not in allowed:
                raise AgentRuntimeError(
                    "STATE_TRANSITION_INVALID",
                    f"不能从“{PHASE_LABELS.get(phase, phase)}”直接进入“{PHASE_LABELS.get(target, target)}”",
                    status=409,
                    details={"allowed": list(allowed)},
                )
            if phase == "release" and target in {"goal", "process", "knowledge"} and not live.release_reentry_authorized:
                raise AgentRuntimeError(
                    "STATE_RELEASE_REENTRY_REQUIRED",
                    "已发布项目只有在用户明确发起新迭代后才能回到知识建模",
                    status=409,
                    details={"next": ["请用户明确说明要增量补资料、融合来源包或修改已发布知识"]},
                )
            gate = PHASE_GATES.get(target)
            if gate:
                result = await self._gateway.call(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                    tool="gate_status",
                    arguments={"gate": gate},
                )
                if result.get("error"):
                    raise AgentRuntimeError(
                        str(result.get("error")),
                        str(result.get("reason") or "状态门禁检查失败"),
                        status=409,
                        details={"next": result.get("next") or []},
                    )
                if not result.get("pass"):
                    raise AgentRuntimeError(
                        "STATE_GATE_BLOCKED",
                        f"还不能进入“{PHASE_LABELS[target]}”",
                        status=409,
                        details={"gate": gate, "blockers": result.get("blockers") or []},
                    )
            intent = str(arguments.get("intent") or "").strip()
            if not intent:
                raise AgentRuntimeError("STATE_INTENT_REQUIRED", "状态转换必须说明意图", status=400)
            updated = self.store.transition_project_state(
                live.project_id,
                expected_revision=current.revision,
                next_state=f"{target}:{execution}",
                cause="phase_transition",
                actor_run_id=live.run_id,
                summary=summary,
                open_questions=questions,
                metadata={"intent": intent[:2_000], "evidence": evidence, "gate": gate},
            )
        else:
            raise AgentRuntimeError("STATE_TOOL_NOT_FOUND", f"未知项目状态工具：{name}", status=404)

        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS.get(public["phase"], public["phase"])
        self.events.publish(live.project_id, live.run_id, "state", public)
        return {**public, "display": {"text": f"项目状态已更新为“{public['phaseLabel']}”"}}

    async def _after_core_tool(
        self,
        live: LiveRun,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if name == "create_change_plan":
            consume = getattr(self._gateway, "acceptance_fault", None)
            fault: dict[str, Any] = {}
            if callable(consume):
                try:
                    value = await consume(
                        project_id=live.project_id,
                        run_id=live.run_id,
                        token=live.token,
                        fault_id="F16",
                    )
                    fault = value if isinstance(value, dict) else {}
                except Exception:  # noqa: BLE001 - acceptance controls never affect production
                    fault = {}
            if fault.get("active") is True:
                plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
                if plan:
                    self.events.publish(live.project_id, live.run_id, "change_plan", {"plan": plan})
                self.events.publish(live.project_id, live.run_id, "progress", {
                    "activityId": f"acceptance-batch-wait:{live.run_id}",
                    "text": "当前业务批次已暂停，等待继续或停止",
                    "detail": "建模计划已经保存，尚未开始第一批写入",
                    "phase": "waiting_checkpoint",
                    "counters": {},
                })
                await live.cancel_event.wait()
                raise asyncio.CancelledError
            plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
            if plan.get("approval_status") == "pending":
                live.plan_approval_required = True
            return
        if name == "set_material_directive":
            document = result.get("document") if isinstance(result.get("document"), dict) else {}
            filename = str(document.get("filename") or arguments.get("filename") or "").strip()
            if filename:
                live.observed_material_directives.add(filename)
            if result.get("requiresUserConfirmation") is True:
                return
        if name not in {"set_material_directive", "read_documents_fully"}:
            return
        purposes: list[str] = []
        filenames: list[str] = []
        if name == "set_material_directive":
            document = result.get("document") if isinstance(result.get("document"), dict) else {}
            purpose = str(document.get("purpose") or arguments.get("purpose") or "auto")
            purposes.append(purpose)
            filenames.append(str(document.get("filename") or arguments.get("filename") or ""))
            # 只改变阅读方式时，等全文读取成功后再回退阶段。
            if purpose == "auto":
                return
        else:
            documents = result.get("documents") if isinstance(result.get("documents"), list) else []
            purposes.extend(str(item.get("purpose") or "auto") for item in documents if isinstance(item, dict))
            filenames.extend(str(item.get("filename") or "") for item in documents if isinstance(item, dict))
        target = "goal" if "goal" in purposes else "knowledge"
        current = self.store.project_state(live.project_id)
        phase, _separator, execution = current.state.partition(":")
        if phase not in PHASE_ORDER or PHASE_ORDER.index(phase) <= PHASE_ORDER.index(target):
            return
        if current.run_id != live.run_id or execution != RUN_RUNNING:
            raise AgentRuntimeError(
                "STATE_TRANSITION_INVALID",
                "资料设置完成，但项目阶段已经由另一轮处理更新",
                status=409,
            )
        updated = self.store.transition_project_state(
            live.project_id,
            expected_revision=current.revision,
            next_state=f"{target}:{execution}",
            cause="material_directive_applied" if name == "set_material_directive" else "material_full_read_completed",
            actor_run_id=live.run_id,
            metadata={
                "filenames": [item for item in filenames if item][:50],
                "purposes": purposes[:50],
            },
        )
        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS.get(target, target)
        self.events.publish(live.project_id, live.run_id, "state", public)

    async def _state_for_agent(self, live: LiveRun) -> dict[str, Any]:
        state = self.store.project_state(live.project_id)
        public = state.public()
        evaluation = await self._evaluation_status_for_agent(live)
        if evaluation.get("terminal") is True:
            if (
                _claims_active_evaluation(str(public.get("summary") or ""))
                or _claims_terminal_evaluation(str(public.get("summary") or ""))
            ):
                public["summary"] = _evaluation_terminal_summary(evaluation)
            public["openQuestions"] = [
                item for item in public.get("openQuestions") or []
                if not _claims_active_evaluation(str(item))
            ]
        phase = str(public["phase"])
        data_source = await self._gateway.call(
            project_id=live.project_id,
            run_id=live.run_id,
            token=live.token,
            tool="project_data_source_status",
            arguments={},
        )
        if data_source.get("error"):
            data_source = {
                "configured": False,
                "mode": "unknown",
                "error": str(data_source.get("reason") or "数据源配置读取失败"),
            }
        transitions: list[dict[str, Any]] = []
        for target in PHASE_EDGES.get(phase, ()):
            gate = PHASE_GATES.get(target)
            ready = True
            blockers: list[str] = []
            if gate:
                result = await self._gateway.call(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                    tool="gate_status",
                    arguments={"gate": gate},
                )
                ready = not result.get("error") and bool(result.get("pass"))
                blockers = _string_list(result.get("blockers") or result.get("next"), 30)
                if result.get("error") and not blockers:
                    blockers = [str(result.get("reason") or "门禁检查失败")]
            if phase == "release" and target in {"goal", "process", "knowledge"} and not live.release_reentry_authorized:
                ready = False
                blockers = ["本轮没有用户明确发起新迭代"]
            transitions.append({
                "toPhase": target,
                "label": PHASE_LABELS[target],
                "gate": gate,
                "ready": ready,
                "blockers": blockers,
            })
        public["phaseLabel"] = PHASE_LABELS.get(phase, phase)
        return {
            **public,
            "evaluation": evaluation,
            "dataSource": data_source,
            "allowedTransitions": transitions,
            "display": {"text": f"当前阶段：{public['phaseLabel']}"},
        }

    async def _evaluation_status_for_agent(self, live: LiveRun) -> dict[str, Any]:
        try:
            result = await self._gateway.call(
                project_id=live.project_id,
                run_id=live.run_id,
                token=live.token,
                tool="evaluation_status",
                arguments={},
            )
        except Exception as error:  # noqa: BLE001 - project state remains readable during Core outage
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": f"暂时无法读取评测状态：{type(error).__name__}",
            }
        if result.get("error") == "EVALUATION_NOT_STARTED":
            return {"available": False, "status": "not_started", "terminal": False}
        if result.get("error"):
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": str(result.get("reason") or "暂时无法读取评测状态"),
            }
        status = str(result.get("status") or "unknown")
        if status not in {"running", "done", "failed", "cancelled"}:
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": "Core 没有返回可识别的评测状态",
            }
        return {
            "available": True,
            "roundId": str(result.get("roundId") or ""),
            "status": status,
            "terminal": status in {"done", "failed", "cancelled"},
            "done": max(0, int(result.get("done") or 0)),
            "total": max(0, int(result.get("total") or 0)),
            "passed": max(0, int(result.get("passed") or 0)),
            "createdAt": result.get("createdAt"),
            "finishedAt": result.get("finishedAt"),
        }

    async def cancel(self, *, project_id: str) -> dict[str, Any]:
        run = self.store.active_run(project_id)
        if run is None:
            state = self.store.project_state(project_id)
            _phase, _separator, execution = state.state.partition(":")
            if (
                execution in ACTIVE_RUN_STATUSES
                and state.run_id
                and self.store.get_run(state.run_id) is None
            ):
                return await self.safe_terminate_stale(project_id=project_id)
            raise AgentRuntimeError("RUN_NOT_ACTIVE", "当前没有运行中的 Agent", status=409)
        live = self._live.get(run.id)
        if live:
            live.cancel_event.set()
        _stopping, closed_interactions = self.store.begin_run_cancellation(run.id)
        for interaction in closed_interactions:
            self.events.publish(project_id, run.id, "interaction", interaction.public())
        self.events.publish(project_id, run.id, "status", {
            "status": RUN_STOPPING,
            "text": "正在停止并核对已经发出的修改",
        })
        self._publish_state(project_id, run.id)
        try:
            await asyncio.wait_for(
                self._gateway.call(
                    project_id=project_id,
                    run_id=run.id,
                    token=live.token if live else "",
                    tool="cancel_evaluation",
                    arguments={},
                ),
                timeout=30,
            )
        except Exception as error:  # noqa: BLE001 - local cancellation must still complete
            logger.warning("Could not cancel core evaluation for run %s: %s", run.id, error)
        tasks: list[asyncio.Task[Any]] = []
        if live:
            tasks.extend(live.inflight_core_calls)
            if live.task and not live.task.done():
                tasks.append(live.task)
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        updated = self.store.update_run(run.id, status=RUN_CANCELLED, error="用户已停止运行")
        self._publish_state(project_id, run.id)
        self.events.publish(project_id, run.id, "status", {"status": RUN_CANCELLED, "text": "已停止，已完成的候选内容保留"})
        return self._run_public(updated, self.store.project_state(project_id))

    async def safe_terminate_stale(self, *, project_id: str) -> dict[str, Any]:
        state = self.store.project_state(project_id)
        terminated_run_id = state.run_id
        terminated = self.store.terminate_stale_occupancy(project_id)
        self._publish_state(project_id, terminated_run_id)
        self.events.publish(
            project_id,
            terminated_run_id,
            "status",
            {"status": RUN_CANCELLED, "text": "已安全终止失效运行，候选内容与审计记录保留，可以重新规划"},
        )
        return {
            "run": None,
            "state": terminated.public(),
            "terminatedStale": True,
            "terminatedRunId": terminated_run_id,
        }

    def _persist_latest_interaction(
        self,
        run: RunRecord,
        *,
        interaction: InteractionRecord,
        answer: dict[str, Any],
        continuation: str = "",
        action_key: str = "",
        requires_artifact: bool | None = None,
    ) -> None:
        """持久化最近一次交互的业务事实，供空正文紧凑摘要恢复使用。

        快照包含最新交互问题、答案、操作 key、续跑提示与脚本契约摘要；紧凑摘要
        只能基于这些最新事实生成，避免恢复摘要回退到最初 run.prompt 或旧计划。
        """
        payload = interaction.payload or {}
        question = str(payload.get("question") or "")[:4_000]
        contract_summary = str(
            payload.get("contractSummary")
            or payload.get("summary")
            or payload.get("question")
            or ""
        )[:4_000]
        answer_snapshot = {
            key: (value[:4_000] if isinstance(value, str) else value)
            for key, value in dict(answer).items()
        }
        snapshot = {
            "interactionId": interaction.id,
            "kind": interaction.kind,
            "question": question,
            "answer": answer_snapshot,
            "actionKey": (action_key or str(answer.get("action") or ""))[:200],
            "continuation": str(continuation or "")[:2_000],
            "contractSummary": contract_summary,
            "operation": str(payload.get("operation") or "")[:500],
            "requiresArtifact": (
                _interaction_requires_artifact(interaction)
                if requires_artifact is None
                else bool(requires_artifact)
            ),
            "updatedAt": now_iso(),
        }
        self.store.update_run(run.id, latest_interaction=snapshot)

    def _script_artifact_produced(self, live: LiveRun, run: RunRecord) -> bool:
        """本轮是否已经形成脚本产物或下一张确认卡。"""
        if live.observed_tools & SCRIPT_ARTIFACT_TOOLS:
            return True
        pending = self.store.pending_interaction(run.project_id)
        return pending is not None and pending.run_id == run.id

    async def _resume(
        self,
        run: RunRecord,
        token: str,
        decisions: dict[str, Any],
        *,
        steering: str = "",
        restore_interaction: InteractionRecord | None = None,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_declined: bool = False,
        release_reentry_authorized: bool = False,
        confirmation_required: bool = False,
        preserve_token_fingerprint: bool = False,
        autonomous_seed: bool = False,
    ) -> dict[str, Any]:
        if self._execution(run.project_id, run.id) not in {RUN_WAITING, RUN_SUSPENDED}:
            raise AgentRuntimeError("RUN_NOT_WAITING", "当前运行不在等待恢复状态", status=409)
        claimed = await self._claim_workspace(project_id=run.project_id, run_id=run.id, token=token)
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        resume = InteractiveInput()
        for tool_call_id, value in decisions.items():
            resume.update(tool_call_id, value)
        updates: dict[str, Any] = {
            "status": RUN_QUEUED,
            "interaction_id": "",
            "error": "",
        }
        if not preserve_token_fingerprint:
            updates["token_fingerprint"] = token_fingerprint(token)
        self.store.update_run(run.id, **updates)
        self._publish_state(run.project_id, run.id)
        technical_recoveries = self.store.technical_recoveries(run.id)
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            tool_profile=run.tool_profile,
            restore_interaction=restore_interaction,
            required_tools=set(required_tools or ()) | {
                str(item.get("requiredTool") or "")
                for item in technical_recoveries.values()
                if str(item.get("requiredTool") or "").strip()
            },
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=release_reentry_authorized,
            release_declined=release_declined,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            validation_repair=self._validation_repair_state(run.id),
            technical_recoveries=technical_recoveries,
        )
        if steering:
            await live.steering.put(steering)
        self._live[run.id] = live
        live.task = asyncio.create_task(self._execute(live, resume))
        updated = self.store.require_run(run.id)
        return self._run_public(updated, self.store.project_state(run.project_id))

    async def _continue(
        self,
        run: RunRecord,
        token: str,
        prompt: str,
        *,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_reentry_authorized: bool = False,
        confirmation_required: bool = False,
        autonomous_seed: bool = False,
    ) -> dict[str, Any]:
        if self._execution(run.project_id, run.id) not in {RUN_WAITING, RUN_SUSPENDED}:
            raise AgentRuntimeError("RUN_NOT_WAITING", "当前运行不在等待恢复状态", status=409)
        claimed = await self._claim_workspace(project_id=run.project_id, run_id=run.id, token=token)
        self.store.update_run(
            run.id,
            status=RUN_QUEUED,
            interaction_id="",
            token_fingerprint=token_fingerprint(token),
            error="",
        )
        self._publish_state(run.project_id, run.id)
        technical_recoveries = self.store.technical_recoveries(run.id)
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            tool_profile=run.tool_profile,
            required_tools=set(required_tools or ()) | {
                str(item.get("requiredTool") or "")
                for item in technical_recoveries.values()
                if str(item.get("requiredTool") or "").strip()
            },
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=release_reentry_authorized,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            validation_repair=self._validation_repair_state(run.id),
            technical_recoveries=technical_recoveries,
        )
        self._live[run.id] = live
        live.task = asyncio.create_task(self._execute(live, prompt))
        updated = self.store.require_run(run.id)
        return self._run_public(updated, self.store.project_state(run.project_id))

    async def _claim_workspace(self, *, project_id: str, run_id: str, token: str) -> dict[str, Any]:
        current_credential = getattr(self._gateway, "run_credential", None)
        restore_credential = getattr(self._gateway, "restore_run_credential", None)
        if callable(current_credential) and callable(restore_credential) and not current_credential(run_id):
            persisted = self.store.require_run(run_id).run_credential
            if persisted:
                restore_credential(run_id, persisted)
        claimed = await self._gateway.call(
            project_id=project_id,
            run_id=run_id,
            token=token,
            tool="claim_candidate_workspace",
            arguments={},
        )
        if claimed.get("error"):
            raise AgentRuntimeError(
                str(claimed.get("error")),
                str(claimed.get("reason") or "候选工作区接续失败"),
                status=409,
                details={"next": claimed.get("next") or []},
            )
        package_contract = claimed.get("packageContract")
        if not isinstance(package_contract, dict) or not str(package_contract.get("validatorVersion") or ""):
            raise AgentRuntimeError(
                "PACKAGE_CONTRACT_UNAVAILABLE",
                "核心服务没有提供带版本的知识包生成契约，Agent 已停止生成候选",
                status=503,
                details={"next": ["同步部署核心服务与 Agent 后从当前进度重试"]},
            )
        credential = getattr(self._gateway, "run_credential", None)
        if callable(credential):
            issued = str(credential(run_id) or "")
            if issued:
                self.store.update_run(run_id, run_credential=issued)
        return claimed

    def _validation_repair_state(self, run_id: str) -> CandidateValidationRepairState:
        return CandidateValidationRepairState.from_snapshot(
            self.store.validation_repair_state(run_id),
            on_change=lambda snapshot: self.store.save_validation_repair_state(
                run_id, snapshot,
            ),
        )

    async def _execute(self, live: LiveRun, query: Any) -> None:
        from openjiuwen.core.runner.runner import Runner

        run = self.store.require_run(live.run_id)
        try:
            if self._model_factory is not None:
                model = self._model_factory()
            elif len(self.settings.service_token) < 32:
                model = build_model(self.settings)
            else:
                runtime_llm = await self._gateway.model_config(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                )
                if runtime_llm.get("acceptanceDriver") is True:
                    from .acceptance_model import AcceptanceModel
                    model = AcceptanceModel(str(runtime_llm.get("acceptanceScenario") or ""))
                else:
                    model = build_model(self.settings.with_runtime_llm(runtime_llm))
            self.store.update_run(run.id, status=RUN_RUNNING)
            self._publish_state(run.project_id, run.id)
            self.events.publish(run.project_id, run.id, "status", {"status": RUN_RUNNING, "text": "助手正在建模"})
            autonomous = live.autonomous_seed
            stalled = 0
            continuations = 0
            decision_card_retries = 0
            confirmation_card_retries = 0
            model_operation_seq = 0
            current_query = query
            while True:
                def emit(kind: str, payload: dict[str, Any]) -> None:
                    if kind == "tool":
                        phase = str(payload.get("phase") or "")
                        name = str(payload.get("name") or "")
                        if phase in {"completed", "failed", "repairing", "blocked", "rejected"}:
                            live.attempted_tools.add(name)
                            summary = sanitize_visible_text(str(payload.get("summary") or "")).strip()
                            if summary:
                                live.tool_summaries.append(
                                    f"{name} / {phase}: {summary[:300]}"
                                )
                                del live.tool_summaries[:-MAX_TOOL_SUMMARIES]
                        if phase == "completed":
                            live.observed_tools.add(name)
                    self.events.publish(live.project_id, live.run_id, kind, payload)

                def require_tool(name: str, recovery: dict[str, Any]) -> None:
                    normalized = str(name or "").strip()
                    if not normalized:
                        return
                    live.required_tools.add(normalized)
                    issue_id = str(recovery.get("issueId") or "").strip()
                    if issue_id:
                        live.technical_recoveries[issue_id] = dict(recovery)
                        self.store.save_technical_recovery(
                            live.run_id, issue_id, dict(recovery),
                        )

                context = ToolContext(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                    gateway=self._gateway,
                    emit=emit,
                    phase=self.store.project_state(run.project_id).state.partition(":")[0],
                    tool_profile=live.tool_profile,
                    required_tools=frozenset(live.required_tools),
                    require_tool=require_tool,
                    release_required_tool=lambda name: live.required_tools.discard(
                        str(name or "").strip(),
                    ),
                    resolve_issues=lambda issue_ids: self.store.resolve_technical_recoveries(
                        live.run_id, issue_ids,
                    ),
                    technical_recoveries=live.technical_recoveries,
                    state_action=lambda name, arguments: self._state_action(live, name, arguments),
                    after_core_tool=lambda name, arguments, result: self._after_core_tool(
                        live, name, arguments, result,
                    ),
                    confirmation_guard=lambda: (
                        not live.confirmation_required
                        or not _missing_required_tools(live)
                    ),
                    tool_receipt_lookup=lambda name, call_id, arguments: self.store.tool_receipt(
                        live.run_id, call_id, name, arguments,
                    ),
                    tool_receipt_save=lambda name, call_id, arguments, result: self.store.save_tool_receipt(
                        live.run_id, call_id, name, arguments, result,
                    ),
                    stable_read_cache=live.stable_read_cache,
                    evaluation_cancel_authorized="cancel_evaluation" in live.required_tools,
                    allowed_tools=live.allowed_tools,
                    confirmation_required=live.confirmation_required,
                    release_declined=live.release_declined,
                    validation_repair=live.validation_repair,
                    package_contract=live.package_contract,
                    cancel_event=live.cancel_event,
                    inflight_core_calls=live.inflight_core_calls,
                )
                live.bundle = build_agent(
                    self.settings,
                    context,
                    model=model,
                    package_contract=live.package_contract,
                )
                boundary = self.events.publish(
                    run.project_id,
                    run.id,
                    "status",
                    {"status": RUN_RUNNING, "text": "助手正在执行下一步" if continuations else "助手正在建模"},
                )
                model_operation_seq += 1
                result = await self._run_model_operation(
                    live,
                    run,
                    Runner,
                    current_query,
                    operation_id=f"model-stream-{model_operation_seq:04d}",
                )
                if context.recovery_waiting or result.get("recovery_wait") is True:
                    self.store.update_run(
                        run.id,
                        status=RUN_SUSPENDED,
                        result="",
                        error="",
                    )
                    self._publish_state(run.project_id, run.id)
                    self.events.publish(run.project_id, run.id, "checkpoint", {
                        "id": run.session_id,
                        "revision": run.candidate_revision or run.baseline_revision,
                    })
                    self.events.publish(run.project_id, run.id, "status", {
                        "status": RUN_SUSPENDED,
                        "text": "已保留当前成果，等待你选择后续处理",
                    })
                    return
                if live.plan_approval_required:
                    output = str(result.get("output") or "").strip()
                    self.store.update_run(
                        run.id,
                        status=RUN_SUSPENDED,
                        result=output,
                        error="",
                    )
                    self._publish_state(run.project_id, run.id)
                    self.events.publish(run.project_id, run.id, "checkpoint", {
                        "id": run.session_id,
                        "revision": run.candidate_revision or run.baseline_revision,
                    })
                    self.events.publish(run.project_id, run.id, "status", {
                        "status": RUN_SUSPENDED,
                        "text": "等待你批准当前建模计划",
                    })
                    return
                missing_tools = _missing_required_tools(live)
                if result.get("result_type") == "success" and missing_tools:
                    proof_key = "|".join(sorted(missing_tools))
                    used_proof_retries = live.proof_retries.get(proof_key, 0)
                    if used_proof_retries < 2:
                        live.proof_retries[proof_key] = used_proof_retries + 1
                        current_query = await self._required_tool_prompt(
                            live, run, Runner, missing_tools,
                        )
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手没有完成这项明确要求，本轮已停止；已经确认的内容保持不变。",
                        "streamed": False,
                    }
                if result.get("result_type") == "success" and _purpose_review_suggestions_complete(live):
                    result = await self._ensure_visible_result(live, run, result)
                    self._settle(live, run, result)
                    return
                if (
                    result.get("result_type") == "success"
                    and context.confirmation_claimed_in_text
                    and not context.interjection_reply_required
                    and live.restore_interaction is None
                ):
                    live.confirmation_required = True
                if (
                    result.get("result_type") == "success"
                    and context.user_input_request_in_text
                    and not context.decision_card_requested
                    and not context.interjection_reply_required
                    and live.restore_interaction is None
                ):
                    if decision_card_retries < MAX_DECISION_CARD_RETRIES:
                        decision_card_retries += 1
                        current_query = (
                            "你刚才在普通文字里要求用户从业务选项中选择，但尚未生成可操作的问题卡。"
                            "立即调用 ask，把同一问题整理成少量互斥选项并保留自由输入；"
                            "不要改用 request_confirmation，也不要再次只输出文字选项。"
                            if context.decision_request_in_text else
                            "你刚才在普通文字里要求用户补充描述，但尚未生成可输入的问题卡。"
                            "立即调用 ask，把同一补充请求放入问题卡；可以不提供选项，并保留自由输入。"
                            "不要继续执行项目、转换阶段，也不要再次只用普通文字索要输入。"
                        )
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手没能把待用户补充的问题整理成可操作卡片，本轮已停止；已有内容保持不变。",
                        "streamed": False,
                    }
                if result.get("result_type") == "success" and live.confirmation_required:
                    if confirmation_card_retries < 2:
                        confirmation_card_retries += 1
                        current_query = (
                            "整体差异已经核对，但页面上尚未形成需要专家处理的真实确认卡。"
                            "立即发起整体确认；不得替专家批准，也不得只用文字询问。"
                        )
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手未能形成整体确认，待确认修改仍然保留，请稍后重试复核。",
                        "streamed": False,
                    }
                if result.get("result_type") == "interrupt" or live.restore_interaction:
                    self._settle(live, run, result)
                    return

                phase = self.store.project_state(run.project_id).state.partition(":")[0]
                if phase == "release":
                    result = await self._ensure_visible_result(
                        live,
                        run,
                        result,
                    )
                    self._settle(live, run, result)
                    return

                progressed = self._made_autonomous_progress(run.project_id, boundary.seq)
                if progressed:
                    autonomous = True
                    stalled = 0
                elif autonomous:
                    stalled += 1

                if (
                    autonomous
                    and continuations < MAX_AUTONOMOUS_CONTINUATIONS
                    and stalled < MAX_STALLED_CONTINUATIONS
                ):
                    continuations += 1
                    current_query = self._autonomous_continuation_prompt(run.project_id)
                    continue

                result = await self._ensure_visible_result(
                    live,
                    run,
                    result,
                )
                self._settle(live, run, result)
                return
        except asyncio.CancelledError:
            execution = self._execution(run.project_id, run.id)
            if self._shutting_down and execution != RUN_CANCELLED:
                self.store.update_run(
                    run.id,
                    status=RUN_SUSPENDED,
                    error="Agent 服务重启，已停在最近检查点",
                )
                self.events.publish(
                    run.project_id,
                    run.id,
                    "status",
                    {"status": RUN_SUSPENDED, "text": "服务正在重启，已保留当前进展"},
                )
                self._publish_state(run.project_id, run.id)
            elif execution not in {RUN_CANCELLED, RUN_STOPPING}:
                self.store.update_run(run.id, status=RUN_CANCELLED, error="运行被停止")
                self._publish_state(run.project_id, run.id)
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("Agent run %s failed", run.id)
            self.store.update_run(run.id, status=RUN_FAILED, error=f"{type(error).__name__}: {error}")
            self._publish_state(run.project_id, run.id)
            visible_error = (
                error.message
                if isinstance(error, AgentRuntimeError) and error.code == "LLM_NOT_CONFIGURED"
                else (
                    "模型上下文超过本轮可用范围；之前已经完成的处理和候选修改均已保留，请从当前进展重试。"
                    if _is_context_limit_exception(error)
                    else "模型响应等待超时；之前已经完成的处理和候选修改均已保留，请从当前进展重试。"
                    if _is_timeout_exception(error)
                    else "这次处理没有完成，之前已经确认的内容和候选修改都已保留。"
                )
            )
            self.events.publish(run.project_id, run.id, "message", {"role": "assistant", "blocks": [{"type": "error", "text": visible_error, "actions": [{"id": "retry", "label": "从当前进展重试"}]}]})
            if live.restore_interaction:
                self._restore_interaction(run, live.restore_interaction)
            else:
                self.events.publish(run.project_id, run.id, "status", {"status": RUN_FAILED, "text": "本次处理未完成", "error": visible_error})
        finally:
            self._live.pop(run.id, None)
            deferred = live.deferred_purpose_continuation
            run_cancelled = self.store.require_run(run.id).status == RUN_CANCELLED
            if deferred is not None and not self._shutting_down and not run_cancelled:
                steering_id, token, message = deferred
                try:
                    await self.start_run(
                        project_id=run.project_id,
                        token=token,
                        message=message,
                        publish_user_message=False,
                    )
                    self.store.mark_steering_delivered(steering_id)
                except Exception as error:  # noqa: BLE001
                    logger.exception(
                        "Could not start post-purpose-review run for project %s",
                        run.project_id,
                    )
                    self.events.publish(run.project_id, run.id, "message", {
                        "role": "assistant",
                        "blocks": [{
                            "type": "error",
                            "text": "资料用途已经确认，但后续建模轮次未能启动，请从当前进展重试。",
                        }],
                    })

    def _made_autonomous_progress(self, project_id: str, after: int) -> bool:
        return any(
            event.kind == "tool"
            and event.payload.get("phase") == "completed"
            and event.payload.get("name") in AUTONOMOUS_PROGRESS_TOOLS
            for event in self.store.events_after(project_id, after, limit=1_000)
        )

    async def _run_model_operation(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        query: Any,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        acceptance_fault = await self._consume_acceptance_model_fault(run, live.token)
        acceptance_attempt = 0
        while True:
            try:
                if acceptance_fault == "F01":
                    acceptance_attempt += 1
                    raise TimeoutError("acceptance first visible chunk timeout")
                if acceptance_fault == "F02":
                    acceptance_attempt += 1
                    if acceptance_attempt == 1:
                        self._publish_acceptance_partial_message(run)
                    raise TimeoutError("acceptance stream idle timeout")
                result = await self._run_streaming(live, run, runner, query)
            except asyncio.CancelledError:
                entry = self.store.retry_entry(run.id, operation_id)
                if entry and entry.get("status") == "retrying":
                    self.store.settle_retry(run.id, operation_id, "cancelled")
                raise
            except Exception as error:  # noqa: BLE001 - only classified failures are recovered
                category = _recoverable_model_failure(error)
                if not category or live.cancel_event.is_set():
                    raise
                current = self.store.retry_entry(run.id, operation_id)
                used = int(current.get("attempt") or 0) if current else 0
                if used >= MAX_MODEL_RECOVERY_ATTEMPTS:
                    self.store.settle_retry(
                        run.id,
                        operation_id,
                        "exhausted",
                        last_error=f"{type(error).__name__}: {error}",
                    )
                    raise
                fingerprint = _error_fingerprint(error)
                entry = self.store.record_retry(
                    run.id,
                    operation_id,
                    category=category,
                    max_attempts=MAX_MODEL_RECOVERY_ATTEMPTS,
                    error_fingerprint=fingerprint,
                    last_error=f"{type(error).__name__}: {error}",
                    delay_seconds=MODEL_RECOVERY_DELAY_SECONDS,
                )
                attempt = int(entry.get("attempt") or 1)
                recovery_session = f"project:{run.project_id}:run:{run.id}:recovery:{attempt}:{uuid.uuid4().hex[:8]}"
                self.store.update_run(run.id, session_id=recovery_session)
                run.session_id = recovery_session
                live.stable_read_cache.clear()
                query = await self._model_recovery_prompt(
                    live, run, runner, category, operation_id,
                )
                label = "上下文已收拢" if category == "context_limit" else "模型响应超时"
                self.events.publish(run.project_id, run.id, "recovery", {
                    "operationId": operation_id,
                    "category": category,
                    "attempt": attempt,
                    "maxAttempts": MAX_MODEL_RECOVERY_ATTEMPTS,
                    "status": "retrying",
                    "text": f"{label}，正在从已保存进展恢复（{attempt}/{MAX_MODEL_RECOVERY_ATTEMPTS}）",
                    "errorFingerprint": fingerprint,
                })
                self.events.publish(run.project_id, run.id, "progress", {
                    "activityId": f"model-recovery:{run.id}:{operation_id}",
                    "text": "正在从已保存进展恢复",
                    "detail": f"{label}，已保留完成项并核对写入回执",
                    "phase": "recovery",
                    "counters": {
                        "current": attempt,
                        "total": MAX_MODEL_RECOVERY_ATTEMPTS,
                    },
                })
                self.events.publish(run.project_id, run.id, "status", {
                    "status": RUN_RUNNING,
                    "text": f"{label}，正在从已保存进展恢复（{attempt}/{MAX_MODEL_RECOVERY_ATTEMPTS}）",
                })
                await asyncio.sleep(MODEL_RECOVERY_DELAY_SECONDS)
                continue

            entry = self.store.retry_entry(run.id, operation_id)
            if entry and entry.get("status") == "retrying":
                settled = self.store.settle_retry(run.id, operation_id, "succeeded") or entry
                self.events.publish(run.project_id, run.id, "recovery", {
                    "operationId": operation_id,
                    "category": settled.get("category"),
                    "attempt": settled.get("attempt"),
                    "maxAttempts": settled.get("maxAttempts"),
                    "status": "succeeded",
                    "text": "已从保存的进展继续",
                })
            return result

    async def _consume_acceptance_model_fault(
        self,
        run: RunRecord,
        token: str,
    ) -> str:
        consume = getattr(self._gateway, "acceptance_fault", None)
        if not callable(consume):
            return ""
        for fault_id in ("F01", "F02"):
            try:
                result = await consume(
                    project_id=run.project_id,
                    run_id=run.id,
                    token=token,
                    fault_id=fault_id,
                )
            except Exception:  # noqa: BLE001 - acceptance controls never affect production
                return ""
            if isinstance(result, dict) and result.get("active") is True:
                return fault_id
        return ""

    def _publish_acceptance_partial_message(self, run: RunRecord) -> None:
        message_id = f"stream_{run.id}_acceptance_partial"
        self.events.publish(run.project_id, run.id, "message_delta", {
            "messageId": message_id,
            "role": "assistant",
            "delta": "已定位合成资源，正在准备后续修改",
        })
        self.events.publish(run.project_id, run.id, "message_done", {
            "messageId": message_id,
            "role": "assistant",
            "incomplete": True,
        })

    async def _model_recovery_prompt(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        category: str,
        operation_id: str,
    ) -> str:
        state = self.store.project_state(run.project_id).public()
        completed_facts = [
            sanitize_visible_text(item)[:300]
            for item in live.tool_summaries[-20:]
            if sanitize_visible_text(item).strip()
        ]
        brief = {
            "briefVersion": "execution-brief-v1",
            "recoveryReason": category,
            "operation": operation_id,
            "userGoal": sanitize_visible_text(run.prompt)[:2_000],
            "projectState": {
                "phase": state.get("phase"),
                "execution": state.get("execution"),
                "revision": state.get("revision"),
                "summary": sanitize_visible_text(str(state.get("summary") or ""))[:4_000],
                "openQuestions": [
                    sanitize_visible_text(str(item))[:300]
                    for item in list(state.get("openQuestions") or [])[:10]
                ],
            },
            "candidateRevision": run.candidate_revision or None,
            "requiredTools": sorted(live.required_tools),
            "completedRequiredTools": sorted(live.observed_tools & live.required_tools),
            "recentCompletedFacts": completed_facts,
        }
        output = await runner.run_workflow(
            build_execution_brief_workflow(),
            {"payload_json": json.dumps(brief, ensure_ascii=False, sort_keys=True)},
            session=f"{run.session_id}:execution-brief",
        )
        prompt = execution_brief_prompt(output)
        if not prompt:
            raise AgentRuntimeError(
                "EXECUTION_BRIEF_WORKFLOW_INVALID",
                "恢复摘要没有形成有效结果",
                status=500,
            )
        return prompt

    async def _required_tool_prompt(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        missing_tools: set[str],
    ) -> str:
        matching = [
            recovery for recovery in live.technical_recoveries.values()
            if str(recovery.get("requiredTool") or "") in missing_tools
        ]
        if not matching:
            required = "、".join(sorted(missing_tools))
            return (
                f"服务端尚未观察到本轮必须执行的工具：{required}。"
                "不要声称已经运行、重试、完成或生成轮次；立即调用该工具，以真实返回为准继续。"
            )
        recovery = matching[0]
        state = self.store.project_state(run.project_id).public()
        payload = {
            "issueId": recovery.get("issueId"),
            "requiredTool": recovery.get("requiredTool"),
            "targetKind": recovery.get("targetKind"),
            "sourceTool": recovery.get("sourceTool"),
            "projectPhase": state.get("phase"),
            "candidateRevision": run.candidate_revision or None,
            "requiredTools": sorted(missing_tools),
        }
        output = await runner.run_workflow(
            build_tool_redirect_workflow(),
            {"payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            session=f"{run.session_id}:tool-redirect:{payload['issueId']}",
        )
        prompt = tool_redirect_prompt(output)
        if not prompt:
            raise AgentRuntimeError(
                "TOOL_REDIRECT_WORKFLOW_INVALID",
                "领域工具恢复流程没有形成有效步骤",
                status=500,
            )
        return prompt

    def _autonomous_continuation_prompt(self, project_id: str) -> str:
        state = json.dumps(self.project_state(project_id)["state"], ensure_ascii=False)[:12_000]
        return (
            "服务端检测到项目已经取得实质进展，但尚未到达需要专家决定、失败或发布交付的边界。"
            "不要请求用户指示，也不要只描述下一步；立即继续执行能自行完成的下一步，直到调用 ask、"
            "request_confirmation，或确实没有可执行动作。\n\n"
            "# 项目唯一 Agent 状态快照\n"
            f"{state}\n"
            "先调用 get_project_state 获取最新版本，状态转换只能调用 transition_project_state。"
        )

    async def _ensure_visible_result(
        self,
        live: LiveRun,
        run: RunRecord,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if result.get("result_type") != "success":
            return result
        output = str(result.get("output") or "").strip()
        if output:
            if result.get("streamed"):
                return result
            self._publish_stream_text(run, output)
            return {**result, "output": output, "streamed": True}

        empty_kind = str(result.get("emptyKind") or "empty_stream")
        self.events.publish(run.project_id, run.id, "progress", {
            "activityId": f"compact-summary:{run.id}",
            "text": "正在重新整理工作摘要",
            "detail": "原模型调用没有返回可读正文，正在使用已完成的处理事实重新生成",
            "phase": "summary_retry",
            "counters": {},
        })
        snapshot = run.latest_interaction or {}
        if snapshot.get("requiresArtifact") and not self._script_artifact_produced(live, run):
            # 脚本契约调整类交互要求本轮形成新契约/候选/下一张确认卡；空正文恢复
            # 若没有这些产物，不允许把 Run 结算为成功完成，否则会形成假完成状态。
            failure = (
                "本轮脚本契约调整没有形成新的契约、脚本候选或下一张确认卡；"
                "专家调整意见已保留，请从当前交互继续。"
            )
            self._publish_stream_text(run, failure)
            return {
                "result_type": "error",
                "output": failure,
                "streamed": True,
                "errorCode": "EMPTY_MODEL_RESPONSE_WITHOUT_ARTIFACT",
                "emptyKind": empty_kind,
                "retryable": True,
            }
        summary = await self._run_compact_summary(live, run, empty_kind)
        if summary:
            return {
                "result_type": "success",
                "output": summary,
                "streamed": True,
                "recoveredFrom": empty_kind,
            }

        failure = "模型没有返回可读摘要；已经完成的处理和候选修改均已保留，请从当前进展重试。"
        self._publish_stream_text(run, failure)
        return {
            "result_type": "error",
            "output": failure,
            "streamed": True,
            "errorCode": "EMPTY_MODEL_RESPONSE",
            "emptyKind": empty_kind,
        }

    async def _run_compact_summary(
        self,
        live: LiveRun,
        run: RunRecord,
        empty_kind: str,
    ) -> str:
        if live.bundle is None:
            return ""
        state = self.store.project_state(run.project_id).public()
        facts = "\n".join(f"- {item}" for item in live.tool_summaries[-MAX_TOOL_SUMMARIES:])
        snapshot = run.latest_interaction or {}
        if snapshot:
            snapshot_lines = [
                f"- 最新交互问题：{sanitize_visible_text(str(snapshot.get('question') or ''))[:1_200]}"
                if str(snapshot.get("question") or "").strip() else None,
                f"- 专家调整意见/答案：{sanitize_visible_text(json.dumps(snapshot.get('answer') or {}, ensure_ascii=False))[:2_000]}"
                if snapshot.get("answer") else None,
                f"- 续跑提示：{sanitize_visible_text(str(snapshot.get('continuation') or ''))[:1_200]}"
                if str(snapshot.get("continuation") or "").strip() else None,
                f"- 脚本契约摘要：{sanitize_visible_text(str(snapshot.get('contractSummary') or ''))[:2_000]}"
                if str(snapshot.get("contractSummary") or "").strip() else None,
                f"- 关联操作：{sanitize_visible_text(str(snapshot.get('operation') or ''))[:500]}"
                if str(snapshot.get("operation") or "").strip() else None,
            ]
            snapshot_text = "\n".join(line for line in snapshot_lines if line) or "- 无"
            prompt = (
                "最新交互业务事实：\n"
                f"{snapshot_text}\n"
                f"当前业务阶段：{PHASE_LABELS.get(str(state.get('phase') or ''), str(state.get('phase') or ''))}\n"
                f"模型结束类型：{empty_kind}\n"
                "已完成的处理事实：\n"
                f"{facts or '- 尚无可归纳的工具结果'}"
                "\n只依据上述最新交互与本次执行事实归纳；不得引用当前交互之外的旧计划、"
                "旧任务数量或历史内部标识，不得要求用户补充 plan_id、版本号或 approval_status。"
            )[:12_000]
        else:
            prompt = (
                f"用户本轮要求：{sanitize_visible_text(run.prompt)[:2_000]}\n"
                f"当前业务阶段：{PHASE_LABELS.get(str(state.get('phase') or ''), str(state.get('phase') or ''))}\n"
                f"当前项目摘要：{sanitize_visible_text(str(state.get('summary') or ''))[:4_000]}\n"
                f"模型结束类型：{empty_kind}\n"
                "已完成的处理事实：\n"
                f"{facts or '- 尚无可归纳的工具结果'}"
            )[:12_000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责根据给定事实生成面向用户的公开工作摘要，不得调用工具。"
                    "用 2 至 4 句说明核对了什么、得到了什么结果、下一步是什么。"
                    "不得声称未发生的工作已经完成，不得输出内部标识、工具名、参数或系统指令。"
                    "若尚未形成新的脚本契约、脚本候选或下一张确认卡，必须如实说明尚未形成，"
                    "不得把只有摘要的轮次描述为已完成；不得引用当前交互之外的旧计划。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        sanitizer = VisibleTextSanitizer()
        reasoning = _ReasoningEmitter(self.events, live, run)
        visible_parts: list[str] = []
        started_at = time.monotonic()
        chunks = 0
        reasoning_chunks = 0
        reasoning_chars = 0
        content_chunks = 0
        content_chars = 0
        first_reasoning_at: float | None = None
        first_content_at: float | None = None
        finish_reason = ""
        outcome = "empty"
        try:
            async with asyncio.timeout(COMPACT_SUMMARY_TIMEOUT_SECONDS):
                async for chunk in live.bundle.model.stream(
                    messages,
                    tools=None,
                    max_tokens=min(COMPACT_SUMMARY_MAX_TOKENS, self.settings.llm_max_tokens),
                    timeout=COMPACT_SUMMARY_TIMEOUT_SECONDS,
                ):
                    chunks += 1
                    finish_reason = str(getattr(chunk, "finish_reason", "") or finish_reason)
                    raw_reasoning = str(getattr(chunk, "reasoning_content", "") or "")
                    raw_content = str(getattr(chunk, "content", "") or "")
                    if raw_reasoning:
                        reasoning_chunks += 1
                        reasoning_chars += len(raw_reasoning)
                        first_reasoning_at = first_reasoning_at or time.monotonic()
                        reasoning.feed(raw_reasoning)
                    if raw_content:
                        content_chunks += 1
                        content_chars += len(raw_content)
                        first_content_at = first_content_at or time.monotonic()
                        delta = sanitizer.feed(raw_content)
                        if delta:
                            visible_parts.append(delta)
                            self.events.publish(run.project_id, run.id, "message_delta", {
                                "messageId": message_id,
                                "role": "assistant",
                                "delta": delta,
                            })
            tail = sanitizer.finish()
            if tail:
                visible_parts.append(tail)
                self.events.publish(run.project_id, run.id, "message_delta", {
                    "messageId": message_id,
                    "role": "assistant",
                    "delta": tail,
                })
            output = "".join(visible_parts).strip()
            if output:
                outcome = "content"
            elif reasoning.seen:
                outcome = "reasoning_only"
            elif chunks:
                outcome = "empty_content"
            else:
                outcome = "zero_chunk"
            if output:
                self.events.publish(run.project_id, run.id, "message_done", {
                    "messageId": message_id,
                    "role": "assistant",
                })
            return output
        except TimeoutError:
            outcome = "timeout"
            return ""
        except Exception as error:  # noqa: BLE001
            outcome = "error"
            logger.warning("Compact summary failed for run %s: %s", run.id, type(error).__name__)
            return ""
        finally:
            reasoning.finish()

            def elapsed(value: float | None) -> int | None:
                return round((value - started_at) * 1000) if value else None

            self.events.publish(run.project_id, run.id, "model_call", {
                "callId": f"compact_{uuid.uuid4().hex[:12]}",
                "actor": "compact_summary",
                "phase": "failed" if outcome != "content" else "completed",
                "outcome": outcome,
                "durationMs": round((time.monotonic() - started_at) * 1000),
                "firstReasoningMs": elapsed(first_reasoning_at),
                "firstContentMs": elapsed(first_content_at),
                "chunks": chunks,
                "reasoningChunks": reasoning_chunks,
                "reasoningChars": reasoning_chars,
                "contentChunks": content_chunks,
                "contentChars": content_chars,
                "finishReason": finish_reason,
                "inputMessages": len(messages),
                "inputChars": sum(len(str(item["content"])) for item in messages),
                "toolDefinitions": 0,
            })

    def _publish_stream_text(self, run: RunRecord, text: str) -> None:
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        self.events.publish(run.project_id, run.id, "message_delta", {
            "messageId": message_id,
            "role": "assistant",
            "delta": text,
        })
        self.events.publish(run.project_id, run.id, "message_done", {
            "messageId": message_id,
            "role": "assistant",
        })

    async def _run_streaming(self, live: LiveRun, run: RunRecord, runner: Any, query: Any) -> dict[str, Any]:
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        output_parts: list[str] = []
        answer_parts: list[str] = []
        visible_parts: list[str] = []
        sanitizer = VisibleTextSanitizer()
        reasoning = _ReasoningEmitter(self.events, live, run)
        interaction: InteractionRecord | None = None
        emitted = False
        held_parts: list[str] = []
        model_events = 0
        stream_completed = False

        def publish_visible(raw_text: str) -> None:
            nonlocal emitted
            delta = sanitizer.feed(raw_text)
            if not delta:
                return
            visible_parts.append(delta)
            emitted = True
            self.events.publish(run.project_id, run.id, "message_delta", {
                "messageId": message_id,
                "role": "assistant",
                "delta": delta,
            })

        try:
            async for chunk in runner.run_agent_streaming(
                live.bundle.agent,
                {"query": query, "_steering_queue": live.steering},
                session=run.session_id,
            ):
                chunk_type = str(getattr(chunk, "type", "") or "")
                if chunk_type == "llm_reasoning":
                    model_events += 1
                    reasoning.feed(_chunk_text(chunk))
                elif chunk_type == "llm_output":
                    model_events += 1
                    delta = _chunk_text(chunk)
                    if not delta:
                        continue
                    output_parts.append(delta)
                    if _missing_required_tools(live):
                        held_parts.append(delta)
                        continue
                    if held_parts:
                        delta = "".join(held_parts) + delta
                        held_parts.clear()
                    publish_visible(delta)
                elif chunk_type == "answer":
                    model_events += 1
                    framework_error = _stream_error_text(chunk)
                    if framework_error:
                        raise RuntimeError(framework_error)
                    answer = _chunk_text(chunk)
                    if answer:
                        answer_parts.append(answer)
                elif chunk_type == "__interaction__":
                    interaction = _interaction_from_stream(run, getattr(chunk, "payload", None))
            stream_completed = True
        finally:
            reasoning.finish()
            if not _missing_required_tools(live):
                if held_parts:
                    publish_visible("".join(held_parts))
                    held_parts.clear()
                tail = sanitizer.finish()
                if tail:
                    visible_parts.append(tail)
                    emitted = True
                    self.events.publish(run.project_id, run.id, "message_delta", {
                        "messageId": message_id,
                        "role": "assistant",
                        "delta": tail,
                    })
            if emitted:
                self.events.publish(run.project_id, run.id, "message_done", {
                    "messageId": message_id,
                    "role": "assistant",
                    "incomplete": not stream_completed,
                })

        if interaction is not None:
            return {"result_type": "interrupt", "interaction": interaction, "streamed": emitted}

        raw_output = "".join(output_parts) or "".join(answer_parts)
        output = "".join(visible_parts) or sanitize_visible_text(raw_output)
        if output and not emitted and not _missing_required_tools(live):
            self.events.publish(run.project_id, run.id, "message_delta", {
                "messageId": message_id,
                "role": "assistant",
                "delta": output,
            })
            self.events.publish(run.project_id, run.id, "message_done", {
                "messageId": message_id,
                "role": "assistant",
            })
            emitted = True
        if output:
            empty_kind = ""
        elif reasoning.seen:
            empty_kind = "reasoning_only"
        elif model_events:
            empty_kind = "empty_content"
        else:
            empty_kind = "zero_chunk"
        return {
            "result_type": "success",
            "output": output,
            "streamed": emitted,
            "emptyKind": empty_kind,
        }

    def _settle(self, live: LiveRun, run: RunRecord, result: dict[str, Any]) -> None:
        if result.get("result_type") == "interrupt":
            if live.restore_interaction:
                self.events.publish(
                    run.project_id,
                    run.id,
                    "interaction",
                    {"id": live.restore_interaction.id, "status": "answered", "answer": {"action": "interject"}},
                )
            interaction = result.get("interaction")
            if not isinstance(interaction, InteractionRecord):
                interaction = _interaction_from_result(run, result)
            self.store.create_interaction(interaction)
            self._publish_state(run.project_id, run.id)
            self.events.publish(run.project_id, run.id, "checkpoint", {"id": run.session_id, "revision": run.candidate_revision or run.baseline_revision})
            self.events.publish(run.project_id, run.id, "interaction", interaction.public())
            self.events.publish(run.project_id, run.id, "status", {"status": RUN_WAITING, "text": "等待你的决定；输入框仍可继续提问"})
            return

        output = str(result.get("output") or "").strip()
        if result.get("result_type") == "success" and (not output or not result.get("streamed")):
            output = "模型没有返回可读结果；已经完成的处理和候选修改均已保留，请从当前进展重试。"
            self._publish_stream_text(run, output)
            result = {
                "result_type": "error",
                "output": output,
                "streamed": True,
                "errorCode": "INVISIBLE_SUCCESS",
            }
        status = RUN_FAILED if result.get("result_type") == "error" else RUN_COMPLETED
        self.store.update_run(run.id, status=status, result=output, error="" if status == RUN_COMPLETED else output)
        self._publish_state(run.project_id, run.id)
        if output and not result.get("streamed"):
            self.events.publish(run.project_id, run.id, "message", {"role": "assistant", "blocks": [{"type": "text", "text": output}]})
        if live.restore_interaction:
            self._restore_interaction(run, live.restore_interaction)
            return
        self.events.publish(run.project_id, run.id, "status", {"status": status, "text": "本轮完成" if status == RUN_COMPLETED else "本轮未完成"})

    def _restore_interaction(self, run: RunRecord, original: InteractionRecord) -> None:
        self.events.publish(
            run.project_id,
            run.id,
            "interaction",
            {"id": original.id, "status": "answered", "answer": {"action": "interject"}},
        )
        restored = InteractionRecord(
            id=f"interaction_{uuid.uuid4().hex}",
            project_id=original.project_id,
            run_id=original.run_id,
            tool_call_id=f"{INTERJECTION_CONTINUATION_PREFIX}{original.tool_call_id}",
            kind=original.kind,
            payload={**original.payload, "restoredAfterInterjection": True},
        )
        self.store.create_interaction(restored)
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "checkpoint", {"id": run.session_id, "revision": run.candidate_revision or run.baseline_revision})
        self.events.publish(run.project_id, run.id, "interaction", restored.public())
        self.events.publish(run.project_id, run.id, "status", {"status": RUN_WAITING, "text": "已回答插话，回到原问题；输入框仍可继续提问"})

    def _execution(self, project_id: str, run_id: str) -> str:
        state = self.store.project_state(project_id)
        if state.run_id != run_id:
            return ""
        return str(state.public()["execution"])

    def _publish_state(self, project_id: str, run_id: str) -> None:
        state = self.store.project_state(project_id)
        public = state.public()
        public["phaseLabel"] = PHASE_LABELS.get(public["phase"], public["phase"])
        self.events.publish(project_id, run_id, "state", public)

    @staticmethod
    def _run_public(run: RunRecord, state: Any) -> dict[str, Any]:
        public = run.public()
        if state.run_id == run.id:
            public["status"] = state.public()["execution"]
            public["interactionId"] = state.interaction_id or None
            public["error"] = state.error or None
        return public


def _interaction_from_result(run: RunRecord, result: dict[str, Any]) -> InteractionRecord:
    interrupt_ids = [str(value) for value in result.get("interrupt_ids") or []]
    described = _describe_interrupts(result)
    info = described[0] if described else {}
    tool_call_id = str(info.get("id") or (interrupt_ids[0] if interrupt_ids else ""))
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    kind = str(metadata.get("kind") or "clarification")
    payload = {
        "question": info.get("message") or metadata.get("question") or "需要你补充一个决定",
        "options": info.get("uiOptions") or [],
        "schema": info.get("payloadSchema") or {},
        **metadata,
    }
    return InteractionRecord(
        id=f"interaction_{uuid.uuid4().hex}",
        project_id=run.project_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        kind=kind,
        payload=payload,
    )


def _required_text_option(
    payload: dict[str, Any],
    answer: dict[str, Any],
) -> dict[str, Any] | None:
    raw_selected = answer.get("selected")
    selected = {
        str(value)
        for value in raw_selected
        if str(value).strip()
    } if isinstance(raw_selected, (list, tuple, set)) else set()
    value = str(answer.get("value") or "").strip()
    if value:
        selected.add(value)
    for option in payload.get("options") or []:
        if not isinstance(option, dict) or option.get("requiresText") is not True:
            continue
        option_value = str(option.get("value") or option.get("label") or "").strip()
        if option_value and option_value in selected:
            return option
    return None


def _interaction_from_stream(run: RunRecord, payload: Any) -> InteractionRecord:
    tool_call_id = str(_field(payload, "interaction_id") or _field(payload, "id") or "")
    value = _field(payload, "value") or payload
    nested = _field(value, "value")
    if nested is not None and not _field(value, "message"):
        value = nested
    tool_call_id = str(tool_call_id or _field(value, "id") or "")
    metadata = _field(value, "metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    kind = str(metadata.get("kind") or "clarification")
    interaction_payload = {
        "question": _field(value, "message") or metadata.get("question") or "需要你补充一个决定",
        "options": _field(value, "ui_options") or _field(value, "uiOptions") or [],
        "schema": _field(value, "payload_schema") or _field(value, "payloadSchema") or {},
        **metadata,
    }
    return InteractionRecord(
        id=f"interaction_{uuid.uuid4().hex}",
        project_id=run.project_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        kind=kind,
        payload=interaction_payload,
    )


def _chunk_text(chunk: Any) -> str:
    payload = getattr(chunk, "payload", None)
    if isinstance(payload, dict):
        value = payload.get("content") or payload.get("output") or ""
    else:
        value = payload if isinstance(payload, str) else ""
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def _stream_error_text(chunk: Any) -> str:
    payload = getattr(chunk, "payload", None)
    if not isinstance(payload, dict):
        return ""
    candidates = [payload, payload.get("output")]
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("result_type") != "error":
            continue
        return str(candidate.get("output") or candidate.get("error") or "模型执行失败").strip()
    return ""


def _is_timeout_exception(error: BaseException) -> bool:
    current: BaseException | None = error
    for _depth in range(4):
        if current is None:
            break
        name = type(current).__name__.lower()
        message = str(current).lower()
        if "timeout" in name or "timed out" in message or "timeout" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_context_limit_exception(error: BaseException) -> bool:
    current: BaseException | None = error
    markers = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "上下文过长",
        "上下文长度",
    )
    for _depth in range(4):
        if current is None:
            break
        message = str(current).lower()
        if any(marker in message for marker in markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def _recoverable_model_failure(error: BaseException) -> str:
    if _is_context_limit_exception(error):
        return "context_limit"
    if _is_timeout_exception(error):
        return "model_timeout"
    return ""


def _error_fingerprint(error: BaseException) -> str:
    value = f"{type(error).__name__}:{str(error).strip()}".encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _describe_interrupts(result: dict[str, Any]) -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    for schema in result.get("state") or []:
        payload = getattr(schema, "payload", None)
        value = getattr(payload, "value", None) if payload is not None else None
        described.append({
            "id": _field(payload, "id"),
            "message": _field(value, "message"),
            "toolName": _field(value, "tool_name"),
            "payloadSchema": _field(value, "payload_schema") or {},
            "uiOptions": _field(value, "ui_options") or [],
            "metadata": _field(value, "metadata") or {},
        })
    return described


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


def _require_project_and_token(project_id: str, token: str) -> None:
    if not project_id.strip():
        raise AgentRuntimeError("PROJECT_REQUIRED", "缺少项目标识")
    if not token:
        raise AgentRuntimeError("TOKEN_REQUIRED", "需要登录后才能使用建模助手", status=401)


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


_EVALUATION_EXECUTION_INTENT = re.compile(
    r"(?:重试|重新运行|再次运行|立即运行|运行|开始|启动|执行).{0,12}评测|"
    r"评测.{0,12}(?:重试|重新运行|再次运行|立即运行|运行|开始|启动|执行)",
)

_EVALUATION_RESULT_INTENT = re.compile(
    r"(?:归因|分析|查看|读取|诊断|排查).{0,16}(?:评测|测试).{0,16}(?:失败|结果|错误)|"
    r"(?:评测|测试).{0,16}(?:失败|结果|错误).{0,16}(?:归因|分析|查看|读取|诊断|排查)",
)

_EVALUATION_CANCEL_INTENT = re.compile(
    r"(?:停止|取消|终止|中止|停掉).{0,12}(?:正式)?(?:评测|测评|测试)|"
    r"(?:正式)?(?:评测|测评|测试).{0,12}(?:停止|取消|终止|中止|停掉)",
)

_EVALUATION_CAPABILITY_QUESTION = re.compile(
    r"(?:能力|能否|是否|有没有|可不可以|可以吗|怎么|如何).{0,20}(?:评测|测评|测试)|"
    r"(?:评测|测评|测试).{0,20}(?:能力|能否|是否|有没有|可不可以|可以吗|怎么|如何)",
)

_FULL_READ_INTENT = re.compile(
    r"全文.{0,6}(?:精读|阅读|通读)|(?:精读|通读).{0,12}(?:文档|文件|资料|材料)",
)

_MATERIAL_DIRECTIVE_INTENT = re.compile(
    r"(?:作为|用作|设为).{0,12}(?:建模目标|目标依据|数据\s*Schema|数据模式|业务依据)|"
    r"(?:建模目标|目标依据|数据\s*Schema|数据模式|业务依据).{0,12}(?:资料|材料|文档|文件)",
    re.IGNORECASE,
)

_PACKAGE_FUSION_INTENT = re.compile(
    r"(?:一次性|批量|多个|多份|全部)?.{0,8}(?:融合|合并).{0,12}(?:知识包|来源包)|"
    r"(?:知识包|来源包).{0,12}(?:批量融合|一次性融合|合并到一起)",
    re.IGNORECASE,
)

_RELEASE_REENTRY_INTENT = re.compile(
    r"增量|迭代|新版本|补充.{0,8}资料|替换.{0,8}资料|导入.{0,8}(?:知识包|来源包)|"
    r"融合|修改.{0,8}(?:知识|规则|流程|对象|指标|脚本)|修复.{0,8}(?:知识|规则|流程|对象|指标|脚本)|"
    r"处理.{0,8}开放问题|全文.{0,6}(?:精读|阅读)|精读.{0,8}(?:文档|文件|资料|材料)|启动\s*v?\d",
    re.IGNORECASE,
)

_AUTONOMOUS_WORK_INTENT = re.compile(
    r"(?:开始|继续|接着|推进|完成|生成|创建|构建|执行|运行|重试|修复|优化|融合|导入|发布|更新|修改|核对|分析|精读|全文阅读)"
    r"|(?:请|请你|帮我).{0,32}(?:建模|整理|提取|处理)"
    r"|\b(?:start|continue|proceed|build|create|run|retry|fix|optimize|merge|import|release|update)\b",
    re.IGNORECASE,
)


def _release_reentry_requested(text: str) -> bool:
    return bool(_RELEASE_REENTRY_INTENT.search(str(text or "")))


def _autonomous_work_requested(text: str) -> bool:
    return bool(_AUTONOMOUS_WORK_INTENT.search(str(text or "")))


def _required_tools_for_user_intent(text: str) -> set[str]:
    value = str(text or "")
    required: set[str] = set()
    cancel_requested = bool(
        _EVALUATION_CANCEL_INTENT.search(value)
        and not _EVALUATION_CAPABILITY_QUESTION.search(value)
    )
    if cancel_requested:
        required.add("cancel_evaluation")
    elif _EVALUATION_EXECUTION_INTENT.search(value):
        required.add("run_evaluation")
    if _EVALUATION_RESULT_INTENT.search(value):
        required.add("get_evaluation_results")
    if _FULL_READ_INTENT.search(value):
        required.update({"set_material_directive", "read_documents_fully"})
    elif _MATERIAL_DIRECTIVE_INTENT.search(value):
        required.add("set_material_directive")
    if _PACKAGE_FUSION_INTENT.search(value):
        required.update({"list_package_sources", "list_package_fusion_plans"})
    return required


def _claims_active_evaluation(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    patterns = (
        r"(?:评测|测评|测试).{0,12}(?:仍|还|正在|当前|目前).{0,8}(?:运行|执行|处理中)",
        r"(?:评测|测评|测试).{0,12}(?:尚未|还未|没有)(?:结束|完成)",
        r"等待.{0,12}(?:评测|测评|测试|本轮).{0,12}(?:结束|完成)",
        r"(?:评测|测评|测试).{0,8}(?:异步|后台)(?:运行|执行)中",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _claims_terminal_evaluation(value: str) -> bool:
    return bool(re.search(
        r"(?:最近一轮)?正式评测(?:已完成|已停止|已结束)",
        str(value or ""),
    ))


def _evaluation_terminal_summary(evaluation: dict[str, Any]) -> str:
    status = str(evaluation.get("status") or "failed")
    done = max(0, int(evaluation.get("done") or 0))
    total = max(0, int(evaluation.get("total") or 0))
    passed = max(0, int(evaluation.get("passed") or 0))
    if status == "done":
        return f"正式评测已完成：已执行 {done}/{total} 条，通过 {passed} 条。"
    if status == "cancelled":
        return f"正式评测已停止：已执行 {done}/{total} 条，通过 {passed} 条。"
    return f"正式评测已结束，运行失败：已执行 {done}/{total} 条，通过 {passed} 条。"


def _evaluation_event_is_stale(
    incoming: dict[str, Any],
    latest: dict[str, Any],
) -> bool:
    incoming_at = _evaluation_finished_at(incoming)
    latest_at = _evaluation_finished_at(latest)
    if latest_at is None:
        return False
    if incoming_at is None:
        return True
    return incoming_at <= latest_at


def _evaluation_finished_at(evaluation: dict[str, Any]) -> datetime | None:
    value = str(evaluation.get("finishedAt") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
