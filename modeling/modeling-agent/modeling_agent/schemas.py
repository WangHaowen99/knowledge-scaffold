from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_STOPPING = "stopping"
RUN_WAITING = "waiting"
RUN_SUSPENDED = "suspended"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"

ACTIVE_RUN_STATUSES = frozenset({RUN_QUEUED, RUN_RUNNING, RUN_STOPPING, RUN_WAITING, RUN_SUSPENDED})
TERMINAL_RUN_STATUSES = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})

# 服务重启后的恢复一致性诊断。空串表示没有待处理的恢复事项。
RECOVERY_NONE = ""
RECOVERY_STALE_RUN = "stale_run"
RECOVERY_CHECKPOINT_MISSING = "checkpoint_missing"
RECOVERY_RESUME_FAILED = "resume_failed"
RECOVERY_PLAN_UNREADABLE = "plan_unreadable"
RECOVERY_VOLUME_MISMATCH = "volume_mismatch"


@dataclass
class RunRecord:
    id: str
    project_id: str
    session_id: str
    status: str
    prompt: str
    token_fingerprint: str
    # 新运行只暴露收敛后的领域工具；迁移前的 checkpoint 保留旧工具面。
    tool_profile: str = "domain-v2"
    # Core 签发的短期、run 级凭据，只用于服务重启后接续同一工作区。
    # 它不是业务状态，也绝不能进入 public()。
    run_credential: str = ""
    baseline_revision: str = ""
    candidate_revision: str = ""
    interaction_id: str = ""
    # 最近一次交互/续跑的业务事实快照，用于空正文紧凑摘要恢复时保留最新问题、
    # 答案、续跑提示和脚本契约摘要，避免恢复摘要回退到最初 run.prompt 或旧计划。
    latest_interaction: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    error: str = ""
    created_at: str = ""
    updated_at: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "sessionId": self.session_id,
            "status": self.status,
            "baselineRevision": self.baseline_revision or None,
            "candidateRevision": self.candidate_revision or None,
            "interactionId": self.interaction_id or None,
            "result": self.result or None,
            "error": self.error or None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }


@dataclass
class InteractionRecord:
    id: str
    project_id: str
    run_id: str
    tool_call_id: str
    kind: str
    payload: dict[str, Any]
    status: str = "pending"
    answer: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    answered_at: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "runId": self.run_id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status,
            "answer": self.answer or None,
            "createdAt": self.created_at,
            "answeredAt": self.answered_at or None,
        }


@dataclass
class ProjectAgentState:
    project_id: str
    state: str = "intake:idle"
    revision: int = 0
    round_mode: str = "initial"
    round_id: str = ""
    baseline_revision: str = ""
    run_id: str = ""
    interaction_id: str = ""
    checkpoint_id: str = ""
    summary: str = ""
    open_questions: list[str] = field(default_factory=list)
    error: str = ""
    recovery_status: str = RECOVERY_NONE
    updated_at: str = ""

    def public(self) -> dict[str, Any]:
        phase, _, execution = self.state.partition(":")
        public = {
            "projectId": self.project_id,
            "state": self.state,
            "phase": phase,
            "execution": execution,
            "revision": self.revision,
            "roundMode": self.round_mode,
            "roundId": self.round_id or None,
            "baselineRevision": self.baseline_revision or None,
            "runId": self.run_id or None,
            "interactionId": self.interaction_id or None,
            "checkpointId": self.checkpoint_id or None,
            "summary": self.summary,
            "openQuestions": self.open_questions,
            "error": self.error or None,
            "updatedAt": self.updated_at,
        }
        if self.recovery_status:
            public["recovery"] = {"status": self.recovery_status}
        return public


@dataclass(frozen=True)
class RuntimeEvent:
    seq: int
    project_id: str
    run_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "projectId": self.project_id,
            "runId": self.run_id or None,
            "kind": self.kind,
            **self.payload,
            "createdAt": self.created_at,
        }
