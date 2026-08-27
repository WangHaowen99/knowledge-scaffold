from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import AgentBundle, build_agent, build_model, list_skills
from .core_client import CoreGateway, HttpCoreGateway
from .errors import AgentRuntimeError
from .events import EventHub
from .schemas import (
    InteractionRecord,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_SUSPENDED,
    RUN_WAITING,
    RunRecord,
)
from .settings import Settings
from .store import RuntimeStore, token_fingerprint
from .tools import CandidateValidationRepairState, ToolContext
from .visible_text import VisibleReasoningSanitizer, VisibleTextSanitizer, sanitize_visible_text

logger = logging.getLogger("modeling_agent")
INTERJECTION_CONTINUATION_PREFIX = "interjection-continuation:"

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
RECOVERY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
AUTONOMOUS_PROGRESS_TOOLS = frozenset({
    "analyze_material_impact",
    "stage_tabular_dataset",
    "write_file",
    "delete_file",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "discard_rejected_candidate_changes",
    "regroup_changes",
    "create_package_fusion_plan",
    "update_package_fusion_plan",
    "compile_package_fusion_plan",
    "transition_project_state",
    "run_script_test",
    "run_evaluation",
})
# 评测返回业务门禁失败时已经完成了用户要求的“执行评测”；其余必需工具
# 会产生成果或完成证明，只有成功返回才算满足本轮要求。
FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL = frozenset({"run_evaluation"})
PHASE_ORDER = tuple(PHASE_LABELS)


@dataclass
class LiveRun:
    run_id: str
    project_id: str
    token: str
    steering: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    bundle: AgentBundle | None = None
    restore_interaction: InteractionRecord | None = None
    required_tools: set[str] = field(default_factory=set)
    attempted_tools: set[str] = field(default_factory=set)
    observed_tools: set[str] = field(default_factory=set)
    proof_retries: int = 0
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


def _missing_required_tools(live: LiveRun) -> set[str]:
    terminal_attempts = live.required_tools & FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL
    successful_results = live.required_tools - terminal_attempts
    return (terminal_attempts - live.attempted_tools) | (successful_results - live.observed_tools)


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
        recovered_runs = self.store.recover_interrupted()
        if recovered_runs:
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
                    self.events.publish(
                        run.project_id,
                        run.id,
                        "status",
                        {"status": RUN_SUSPENDED, "text": "已安全停在最近进展，重新打开项目后会继续"},
                    )
                    return
                await asyncio.sleep(RECOVERY_RETRY_DELAYS[attempt])
            except Exception as error:  # noqa: BLE001 - one failed recovery must not block other projects
                logger.warning("Could not automatically resume run %s: %s", run.id, error)
                self.events.publish(
                    run.project_id,
                    run.id,
                    "status",
                    {"status": RUN_SUSPENDED, "text": "已安全停在最近进展，重新打开项目后会继续"},
                )
                return
        self.events.publish(
            run.project_id,
            run.id,
            "status",
            {"status": RUN_QUEUED, "text": "服务恢复完成，助手正在从原进度继续"},
        )

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

    async def start_run(
        self,
        *,
        project_id: str,
        token: str,
        message: str,
        baseline_revision: str = "",
        publish_user_message: bool = True,
        required_tools: set[str] | None = None,
        release_reentry_authorized: bool | None = None,
        confirmation_required: bool = False,
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
            required_tools=intent_required_tools,
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
        prompt = self._material_batch_prompt(terminal_events)
        required_tools = {"analyze_material_impact"} if parsed_events else set()
        if any(item.get("readingMode") == "full" for item in parsed_events):
            required_tools.add("read_documents_fully")
        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=prompt,
            required_tools=required_tools,
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
                await self._resume(
                    run, token, {interaction.tool_call_id: answer},
                    required_tools=required_tools,
                    release_reentry_authorized=True,
                    confirmation_required=confirmation_required,
                )
                return
            answer = {"action": "interject", "text": prompt}
            self.store.answer_interaction(interaction.id, answer)
            await self._resume(
                run, token, {interaction.tool_call_id: answer},
                restore_interaction=interaction,
                required_tools=required_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_SUSPENDED:
            await self._resume(
                run, token, {}, steering=prompt,
                required_tools=required_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_WAITING:
            await self._continue(
                run, token, prompt,
                required_tools=required_tools,
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
        restored_confirmation = (
            direct_confirmation
            and interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX)
        )
        confirmation_result: dict[str, Any] | None = None
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
                return await self._continue(
                    run,
                    token,
                    prompt,
                    required_tools=required_tools,
                    autonomous_seed=True,
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
        if interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX):
            prompt = (
                "这是用户对先前待确认问题的正式答复。\n"
                f"原问题：{interaction.payload.get('question') or '待确认问题'}\n"
                f"用户答复：{json.dumps(answer, ensure_ascii=False)}\n"
                "请把答复作为明确约束，从当前项目和同一会话继续主动建模。"
                "不要重复询问已经回答的问题；下一次确实需要用户决定时再调用 ask 或 request_confirmation。"
            )
            return await self._continue(
                run,
                token,
                prompt,
                required_tools=required_tools,
                autonomous_seed=True,
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

    async def steer(self, *, project_id: str, token: str, text: str) -> dict[str, Any]:
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
            resumed = await self._resume(
                run,
                token,
                {interaction.tool_call_id: answer},
                restore_interaction=interaction,
            )
            return {"mode": "interjection", "run": resumed}

        if execution == RUN_SUSPENDED:
            steering_id = self.store.queue_steering(project_id, run.id, message)
            resumed = await self._resume(
                run,
                token,
                {},
                steering=message,
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
        phase = str(public["phase"])
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
            "allowedTransitions": transitions,
            "display": {"text": f"当前阶段：{public['phaseLabel']}"},
        }

    async def cancel(self, *, project_id: str) -> dict[str, Any]:
        run = self.store.active_run(project_id)
        if run is None:
            raise AgentRuntimeError("RUN_NOT_ACTIVE", "当前没有运行中的 Agent", status=409)
        live = self._live.get(run.id)
        if live:
            live.cancel_event.set()
        updated = self.store.update_run(run.id, status=RUN_CANCELLED, error="用户已停止运行")
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
        self._publish_state(project_id, run.id)
        self.events.publish(project_id, run.id, "status", {"status": RUN_CANCELLED, "text": "已停止，已完成的候选内容保留"})
        return self._run_public(updated, self.store.project_state(project_id))

    async def _resume(
        self,
        run: RunRecord,
        token: str,
        decisions: dict[str, Any],
        *,
        steering: str = "",
        restore_interaction: InteractionRecord | None = None,
        required_tools: set[str] | None = None,
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
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            restore_interaction=restore_interaction,
            required_tools=set(required_tools or ()),
            release_reentry_authorized=release_reentry_authorized,
            release_declined=release_declined,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            validation_repair=self._validation_repair_state(run.id),
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
        release_reentry_authorized: bool = False,
        confirmation_required: bool = False,
        autonomous_seed: bool = False,
    ) -> dict[str, Any]:
        if self._execution(run.project_id, run.id) != RUN_WAITING:
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
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            required_tools=set(required_tools or ()),
            release_reentry_authorized=release_reentry_authorized,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            validation_repair=self._validation_repair_state(run.id),
        )
        self._live[run.id] = live
        live.task = asyncio.create_task(self._execute(live, prompt))
        updated = self.store.require_run(run.id)
        return self._run_public(updated, self.store.project_state(run.project_id))

    async def _claim_workspace(self, *, project_id: str, run_id: str, token: str) -> dict[str, Any]:
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
                model = build_model(self.settings.with_runtime_llm(runtime_llm))
            self.store.update_run(run.id, status=RUN_RUNNING)
            self._publish_state(run.project_id, run.id)
            self.events.publish(run.project_id, run.id, "status", {"status": RUN_RUNNING, "text": "助手正在建模"})
            autonomous = live.autonomous_seed
            stalled = 0
            continuations = 0
            decision_card_retries = 0
            confirmation_card_retries = 0
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

                context = ToolContext(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                    gateway=self._gateway,
                    emit=emit,
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
                result = await self._run_streaming(live, run, Runner, current_query)
                missing_tools = _missing_required_tools(live)
                if result.get("result_type") == "success" and missing_tools:
                    if live.proof_retries < 2:
                        live.proof_retries += 1
                        required = "、".join(sorted(missing_tools))
                        current_query = (
                            f"服务端尚未观察到本轮必须执行的工具：{required}。"
                            "不要声称已经运行、重试、完成或生成轮次；立即调用该工具，以真实返回为准继续。"
                        )
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手没有完成这项明确要求，本轮已停止；已经确认的内容保持不变。",
                        "streamed": False,
                    }
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
            elif execution != RUN_CANCELLED:
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
                    "模型响应等待超时；之前已经完成的处理和候选修改均已保留，请从当前进展重试。"
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

    def _made_autonomous_progress(self, project_id: str, after: int) -> bool:
        return any(
            event.kind == "tool"
            and event.payload.get("phase") == "completed"
            and event.payload.get("name") in AUTONOMOUS_PROGRESS_TOOLS
            for event in self.store.events_after(project_id, after, limit=1_000)
        )

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
                    answer = _chunk_text(chunk)
                    if answer:
                        answer_parts.append(answer)
                elif chunk_type == "__interaction__":
                    interaction = _interaction_from_stream(run, getattr(chunk, "payload", None))
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
    r"(?:重试|重新运行|再次运行|立即运行|开始|启动|执行).{0,12}评测|"
    r"评测.{0,12}(?:重试|重新运行|再次运行|立即运行|开始|启动|执行)",
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
    if _EVALUATION_EXECUTION_INTENT.search(value):
        required.add("run_evaluation")
    if _FULL_READ_INTENT.search(value):
        required.update({"set_material_directive", "read_documents_fully"})
    elif _MATERIAL_DIRECTIVE_INTENT.search(value):
        required.add("set_material_directive")
    if _PACKAGE_FUSION_INTENT.search(value):
        required.update({"list_package_sources", "list_package_fusion_plans"})
    return required
