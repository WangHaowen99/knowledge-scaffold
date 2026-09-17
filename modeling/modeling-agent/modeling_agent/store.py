from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

from .errors import AgentRuntimeError
from .schemas import (
    ACTIVE_RUN_STATUSES,
    InteractionRecord,
    ProjectAgentState,
    RECOVERY_NONE,
    RECOVERY_STALE_RUN,
    RUN_CANCELLED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_STOPPING,
    RUN_SUSPENDED,
    RUN_WAITING,
    TERMINAL_RUN_STATUSES,
    RunRecord,
    RuntimeEvent,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL,
  prompt TEXT NOT NULL,
  token_fingerprint TEXT NOT NULL,
  tool_profile TEXT NOT NULL DEFAULT 'domain-v2',
  run_credential TEXT NOT NULL DEFAULT '',
  baseline_revision TEXT NOT NULL DEFAULT '',
  candidate_revision TEXT NOT NULL DEFAULT '',
  interaction_id TEXT NOT NULL DEFAULT '',
  latest_interaction_json TEXT NOT NULL DEFAULT '{}',
  result TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_project_status
  ON agent_runs(project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_interactions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL REFERENCES agent_runs(id),
  tool_call_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  answer_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  answered_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_agent_interactions_project_status
  ON agent_interactions(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS agent_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_events_project_seq
  ON agent_events(project_id, seq);

-- 外部项目事实的幂等收据。它只记录事件是否已进入消息流、是否已触发续跑，
-- 不保存业务阶段；project_agent_state 仍是唯一业务状态源。
CREATE TABLE IF NOT EXISTS external_project_events (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  continuation_status TEXT NOT NULL DEFAULT 'pending',
  presented_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  continued_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_external_project_events_project
  ON external_project_events(project_id, created_at);

-- origin 区分插话来源：expert 是专家亲口说的指令，需要逐条处置；system 是服务端
-- 组装的续跑/事件提示，投递即完结。disposition 是专家插话的生命周期：空串表示
-- 「已投递但未处置」——投递只说明进过对话，不代表办了，两者混同正是插话被
-- 静默丢弃的根源。task_status 只在 disposition='task' 时使用（open/done）。
CREATE TABLE IF NOT EXISTS steering_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  origin TEXT NOT NULL DEFAULT 'system',
  disposition TEXT NOT NULL DEFAULT '',
  disposition_json TEXT NOT NULL DEFAULT '{}',
  disposed_at TEXT NOT NULL DEFAULT '',
  task_status TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  delivered_at TEXT NOT NULL DEFAULT ''
);

-- 注意：steering_messages 的处置索引不放在这里。存量库的表已存在，
-- CREATE TABLE IF NOT EXISTS 不会补列，origin 等列要等迁移函数才有；
-- 在 executescript 阶段建引用新列的索引会在旧库上直接失败。
-- 索引在 _migrate_schema_locked 补列之后创建。

-- 专家立的长期规矩：活到被撤销或被新条款覆盖为止，每轮注入 system 段。
-- 与 turn_goal（一轮寿命）、待办（做完即死）、package_contract（代码定义）
-- 是四种不同寿命的容器，判据只有一个问题：这条指令什么时候失效。
CREATE TABLE IF NOT EXISTS project_constraints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  text TEXT NOT NULL,
  source_steering_id INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by INTEGER,
  revoke_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  ended_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_project_constraints_project_status
  ON project_constraints(project_id, status, created_at);

-- 每个项目唯一的当前状态。业务阶段与执行状态编码在同一个 state 字段中，
-- 例如 goal:waiting；其他表只保存事实记录、事件或恢复材料。
CREATE TABLE IF NOT EXISTS project_agent_state (
  project_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'intake:idle',
  revision INTEGER NOT NULL DEFAULT 0,
  round_mode TEXT NOT NULL DEFAULT 'initial',
  round_id TEXT NOT NULL DEFAULT '',
  baseline_revision TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  interaction_id TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  open_questions_json TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
  recovery_status TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_agent_state_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  from_state TEXT NOT NULL,
  to_state TEXT NOT NULL,
  from_revision INTEGER NOT NULL,
  to_revision INTEGER NOT NULL,
  cause TEXT NOT NULL,
  actor_run_id TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_project_agent_state_transitions
  ON project_agent_state_transitions(project_id, to_revision);

-- OpenJiuwen 可能在进程退出前已完成工具调用、但尚未来得及保存下一节点。
-- 以稳定 tool_call_id 保存完成结果，重放 checkpoint 时直接复用，避免重复提交。
CREATE TABLE IF NOT EXISTS agent_tool_receipts (
  run_id TEXT NOT NULL REFERENCES agent_runs(id),
  tool_call_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  result_json TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  PRIMARY KEY (run_id, tool_call_id)
);

-- 自动修复是候选级状态，而不是 Python 进程内的临时计数器。服务重启或 checkpoint
-- 恢复后必须继续沿用相同轮次、finding 指纹和停止条件。
CREATE TABLE IF NOT EXISTS agent_validation_repair_state (
  run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 受管文件被拒绝后的确定性工具转向也必须跨服务重启保留。
CREATE TABLE IF NOT EXISTS agent_technical_recoveries (
  run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  issue_id TEXT NOT NULL,
  state_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, issue_id)
);

-- SDK 内部重试对业务层不可见。这里记录应用层恢复是否已经使用、何时允许再次执行，
-- 防止服务重启或模型上下文切换后把同一个失败操作无限重放。
CREATE TABLE IF NOT EXISTS agent_retry_ledger (
  run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  operation_id TEXT NOT NULL,
  category TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  error_fingerprint TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'retrying',
  next_allowed_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_retry_ledger_run
  ON agent_retry_ledger(run_id, updated_at DESC);
"""


class RuntimeStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate_schema_locked()

    def _migrate_schema_locked(self) -> None:
        columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        if "run_credential" not in columns:
            self._db.execute(
                "ALTER TABLE agent_runs ADD COLUMN run_credential TEXT NOT NULL DEFAULT ''"
            )
        if "tool_profile" not in columns:
            # 数据库中已有的运行可能仍持有旧工具调用 checkpoint，不能强制切换 schema。
            self._db.execute(
                "ALTER TABLE agent_runs ADD COLUMN tool_profile TEXT NOT NULL DEFAULT 'legacy-v1'"
            )
        if "latest_interaction_json" not in columns:
            self._db.execute(
                "ALTER TABLE agent_runs ADD COLUMN latest_interaction_json TEXT NOT NULL DEFAULT '{}'"
            )
        state_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(project_agent_state)").fetchall()
        }
        if "recovery_status" not in state_columns:
            self._db.execute(
                "ALTER TABLE project_agent_state ADD COLUMN recovery_status TEXT NOT NULL DEFAULT ''"
            )
        if "round_mode" not in state_columns:
            self._db.execute(
                "ALTER TABLE project_agent_state ADD COLUMN round_mode TEXT NOT NULL DEFAULT 'initial'"
            )
        if "round_id" not in state_columns:
            self._db.execute(
                "ALTER TABLE project_agent_state ADD COLUMN round_id TEXT NOT NULL DEFAULT ''"
            )
        if "baseline_revision" not in state_columns:
            self._db.execute(
                "ALTER TABLE project_agent_state ADD COLUMN baseline_revision TEXT NOT NULL DEFAULT ''"
            )
        steering_columns = {
            str(row["name"])
            for row in self._db.execute("PRAGMA table_info(steering_messages)").fetchall()
        }
        # 历史插话行没有处置生命周期，origin 默认 system：它们早已随当时的对话
        # 处理完，补上 expert 语义会让陈年插话在升级后突然复活成本轮目标。
        for column, definition in (
            ("origin", "TEXT NOT NULL DEFAULT 'system'"),
            ("disposition", "TEXT NOT NULL DEFAULT ''"),
            ("disposition_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("disposed_at", "TEXT NOT NULL DEFAULT ''"),
            ("task_status", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in steering_columns:
                self._db.execute(
                    f"ALTER TABLE steering_messages ADD COLUMN {column} {definition}"
                )
        # 处置索引只能在补列之后建：存量库在 executescript 阶段还没有这些列。
        self._db.execute(
            """CREATE INDEX IF NOT EXISTS idx_steering_messages_project_disposition
            ON steering_messages(project_id, origin, status, disposition)"""
        )
        timestamp = now_iso()
        terminal = tuple(sorted(TERMINAL_RUN_STATUSES))
        placeholders = ",".join("?" for _ in terminal)
        self._db.execute(
            f"""UPDATE agent_interactions
            SET status = 'cancelled',
                answer_json = ?,
                answered_at = ?
            WHERE status = 'pending' AND run_id IN (
              SELECT id FROM agent_runs WHERE status IN ({placeholders})
            )""",
            (
                _json({"action": "run_closed", "reason": "所属运行已经结束"}),
                timestamp,
                *terminal,
            ),
        )
        self._db.execute(
            f"UPDATE agent_runs SET interaction_id = '' WHERE status IN ({placeholders})",
            terminal,
        )
        self._db.execute(
            """UPDATE project_agent_state SET interaction_id = ''
            WHERE interaction_id <> '' AND NOT EXISTS (
              SELECT 1 FROM agent_interactions interaction
              JOIN agent_runs run ON run.id = interaction.run_id
              WHERE interaction.id = project_agent_state.interaction_id
                AND interaction.status = 'pending' AND run.status = 'waiting'
            )""",
        )
        # A continuation claim is process-local work. After a restart the Core
        # outbox must be able to redeliver and claim any interrupted event.
        self._db.execute(
            """UPDATE external_project_events SET continuation_status = 'pending'
            WHERE continuation_status = 'continuing'""",
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def ensure_project_state(self, project_id: str) -> ProjectAgentState:
        with self._lock, self._db:
            return self._ensure_project_state_locked(project_id)

    def project_state(self, project_id: str) -> ProjectAgentState:
        return self.ensure_project_state(project_id)

    def transition_project_state(
        self,
        project_id: str,
        *,
        expected_revision: int,
        next_state: str,
        cause: str,
        actor_run_id: str = "",
        run_id: str | None = None,
        interaction_id: str | None = None,
        checkpoint_id: str | None = None,
        summary: str | None = None,
        open_questions: list[str] | None = None,
        error: str | None = None,
        round_mode: str | None = None,
        round_id: str | None = None,
        baseline_revision: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProjectAgentState:
        _validate_state(next_state)
        with self._lock, self._db:
            current = self._ensure_project_state_locked(project_id)
            if current.revision != expected_revision:
                raise AgentRuntimeError(
                    "STATE_REVISION_CONFLICT",
                    "项目 Agent 状态已经变化",
                    status=409,
                    details={"expectedRevision": expected_revision, "currentRevision": current.revision},
                )
            return self._transition_project_state_locked(
                current,
                next_state=next_state,
                cause=cause,
                actor_run_id=actor_run_id,
                run_id=run_id,
                interaction_id=interaction_id,
                checkpoint_id=checkpoint_id,
                summary=summary,
                open_questions=open_questions,
                error=error,
                round_mode=round_mode,
                round_id=round_id,
                baseline_revision=baseline_revision,
                metadata=metadata,
            )

    def start_increment_round(
        self,
        project_id: str,
        *,
        baseline_revision: str,
        package_version: str = "",
        actor_run_id: str = "",
    ) -> tuple[ProjectAgentState, bool]:
        baseline = baseline_revision.strip()
        if not baseline:
            raise AgentRuntimeError("INCREMENT_BASELINE_REQUIRED", "增量建模缺少已确认基线", status=400)
        with self._lock, self._db:
            current = self._ensure_project_state_locked(project_id)
            phase, _separator, execution = current.state.partition(":")
            is_same_increment_round = (
                phase == "increment"
                and current.round_mode == "incremental"
                and current.baseline_revision == baseline
            )
            if is_same_increment_round and execution in {*ACTIVE_RUN_STATUSES, "idle"}:
                return current, False
            if execution in ACTIVE_RUN_STATUSES and phase not in {"release", "goal", "increment"}:
                raise AgentRuntimeError(
                    "INCREMENT_ROUND_BLOCKED",
                    "当前建模任务正在其他阶段执行，请先停止或完成后再开始增量建模",
                    status=409,
                    details={"state": current.state, "runId": current.run_id},
                )
            if phase not in {
                "release", "goal", "process", "knowledge",
                "increment", "validation", "evaluation",
            }:
                raise AgentRuntimeError(
                    "INCREMENT_BASELINE_STATE_REQUIRED",
                    "只有已发布项目才能开始新的增量建模轮次",
                    status=409,
                    details={"state": current.state},
                )
            active = execution in ACTIVE_RUN_STATUSES
            next_execution = execution if active else "idle"
            round_id = f"increment_{uuid.uuid4().hex}"
            version = package_version.strip()
            summary = (
                f"已基于知识包 {version} 开始增量建模；正式基线保持不变。"
                if version else "已基于当前正式知识包开始增量建模；正式基线保持不变。"
            )
            updated = self._transition_project_state_locked(
                current,
                next_state=f"increment:{next_execution}",
                cause="increment_round_started",
                actor_run_id=actor_run_id or (current.run_id if active else ""),
                run_id=current.run_id if active else "",
                interaction_id=current.interaction_id if active else "",
                checkpoint_id=current.checkpoint_id if active else "",
                summary=summary,
                error="",
                round_mode="incremental",
                round_id=round_id,
                baseline_revision=baseline,
                metadata={
                    "roundId": round_id,
                    "baselineRevision": baseline,
                    "packageVersion": version,
                    "previousPhase": phase,
                },
            )
            return updated, True

    def _ensure_project_state_locked(self, project_id: str) -> ProjectAgentState:
        row = self._db.execute(
            "SELECT * FROM project_agent_state WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row:
            return _project_state_from_row(row)
        latest = self._db.execute(
            "SELECT * FROM agent_runs WHERE project_id = ? ORDER BY updated_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        execution = str(latest["status"]) if latest else "idle"
        if execution not in _EXECUTIONS:
            execution = "idle"
        pending = None
        if latest is not None and execution == RUN_WAITING:
            pending = self._db.execute(
                """SELECT interaction.id FROM agent_interactions interaction
                JOIN agent_runs run ON run.id = interaction.run_id
                WHERE interaction.project_id = ? AND interaction.run_id = ?
                  AND interaction.status = 'pending' AND run.status = 'waiting'
                ORDER BY interaction.created_at LIMIT 1""",
                (project_id, str(latest["id"])),
            ).fetchone()
        timestamp = now_iso()
        state = ProjectAgentState(
            project_id=project_id,
            state=f"intake:{execution}",
            run_id=str(latest["id"]) if latest else "",
            interaction_id=str(pending["id"]) if pending else "",
            checkpoint_id=str(latest["session_id"]) if latest else "",
            error=str(latest["error"]) if latest else "",
            updated_at=timestamp,
        )
        self._db.execute(
            """INSERT INTO project_agent_state (
            project_id, state, revision, round_mode, round_id, baseline_revision,
            run_id, interaction_id, checkpoint_id,
            summary, open_questions_json, error, recovery_status, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            _project_state_values(state),
        )
        return state

    def _transition_project_state_locked(
        self,
        current: ProjectAgentState,
        *,
        next_state: str,
        cause: str,
        actor_run_id: str = "",
        run_id: str | None = None,
        interaction_id: str | None = None,
        checkpoint_id: str | None = None,
        summary: str | None = None,
        open_questions: list[str] | None = None,
        error: str | None = None,
        recovery_status: str | None = None,
        round_mode: str | None = None,
        round_id: str | None = None,
        baseline_revision: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ProjectAgentState:
        _validate_state(next_state)
        updated = ProjectAgentState(
            project_id=current.project_id,
            state=next_state,
            revision=current.revision + 1,
            round_mode=current.round_mode if round_mode is None else round_mode,
            round_id=current.round_id if round_id is None else round_id,
            baseline_revision=(
                current.baseline_revision if baseline_revision is None else baseline_revision
            ),
            run_id=current.run_id if run_id is None else run_id,
            interaction_id=current.interaction_id if interaction_id is None else interaction_id,
            checkpoint_id=current.checkpoint_id if checkpoint_id is None else checkpoint_id,
            summary=current.summary if summary is None else summary[:8_000],
            open_questions=current.open_questions if open_questions is None else open_questions[:30],
            error=current.error if error is None else error[:8_000],
            recovery_status=(
                current.recovery_status if recovery_status is None else recovery_status
            ),
            updated_at=now_iso(),
        )
        changed = self._db.execute(
            """UPDATE project_agent_state SET
            state = ?, revision = ?, round_mode = ?, round_id = ?, baseline_revision = ?,
            run_id = ?, interaction_id = ?, checkpoint_id = ?,
            summary = ?, open_questions_json = ?, error = ?, recovery_status = ?, updated_at = ?
            WHERE project_id = ? AND revision = ?""",
            (
                updated.state,
                updated.revision,
                updated.round_mode,
                updated.round_id,
                updated.baseline_revision,
                updated.run_id,
                updated.interaction_id,
                updated.checkpoint_id,
                updated.summary,
                _json(updated.open_questions),
                updated.error,
                updated.recovery_status,
                updated.updated_at,
                current.project_id,
                current.revision,
            ),
        )
        if changed.rowcount != 1:
            raise AgentRuntimeError("STATE_REVISION_CONFLICT", "项目 Agent 状态已经变化", status=409)
        self._db.execute(
            """INSERT INTO project_agent_state_transitions (
            project_id, from_state, to_state, from_revision, to_revision, cause,
            actor_run_id, metadata_json, before_json, after_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                current.project_id,
                current.state,
                updated.state,
                current.revision,
                updated.revision,
                cause,
                actor_run_id,
                _json(metadata or {}),
                _json(current.public()),
                _json(updated.public()),
                updated.updated_at,
            ),
        )
        return updated

    def create_run(self, run: RunRecord) -> RunRecord:
        timestamp = now_iso()
        run.created_at = run.created_at or timestamp
        run.updated_at = run.updated_at or timestamp
        with self._lock, self._db:
            state = self._ensure_project_state_locked(run.project_id)
            phase, _separator, execution = state.state.partition(":")
            if execution in ACTIVE_RUN_STATUSES:
                existing = self.get_run(state.run_id) if state.run_id else None
                if existing is None:
                    # 运行记录已经丢失但状态仍被占用：进入可恢复异常态，
                    # 不再同时拒绝旧计划与新计划，也不允许静默创建新运行。
                    raise AgentRuntimeError(
                        "RUN_STALE_OCCUPIED",
                        "当前运行状态引用的运行记录已丢失，请先安全终止失效占用后重新规划",
                        status=409,
                        details={
                            "runId": state.run_id,
                            "state": state.state,
                            "recovery": {
                                "status": state.recovery_status or RECOVERY_STALE_RUN,
                                "missing": ["run"],
                            },
                            "nextSteps": ["安全终止失效计划后重新规划"],
                        },
                    )
                raise AgentRuntimeError(
                    "RUN_ACTIVE",
                    "该项目已有正在运行或等待处理的 Agent",
                    status=409,
                    details={"runId": state.run_id, "state": state.state},
                )
            run.baseline_revision = run.baseline_revision.strip() or state.baseline_revision
            self._db.execute(
                """INSERT INTO agent_runs
                (id, project_id, session_id, status, prompt, token_fingerprint,
                 tool_profile, run_credential, baseline_revision, candidate_revision, interaction_id,
                 latest_interaction_json, result, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _run_values(run),
            )
            self._transition_project_state_locked(
                state,
                next_state=f"{phase}:queued",
                cause="run_started",
                actor_run_id=run.id,
                run_id=run.id,
                interaction_id="",
                checkpoint_id=run.session_id,
                error="",
            )
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM agent_runs WHERE id = ?", (run_id,)).fetchone()
        return _run_from_row(row) if row else None

    def require_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run is None:
            raise AgentRuntimeError("RUN_NOT_FOUND", "找不到这次 Agent 运行", status=404)
        return run

    def active_run(self, project_id: str) -> RunRecord | None:
        state = self.project_state(project_id)
        _phase, _separator, execution = state.state.partition(":")
        return self.get_run(state.run_id) if state.run_id and execution in ACTIVE_RUN_STATUSES else None

    def update_run(self, run_id: str, **changes: Any) -> RunRecord:
        allowed = {
            "session_id",
            "status",
            "token_fingerprint",
            "tool_profile",
            "run_credential",
            "baseline_revision",
            "candidate_revision",
            "interaction_id",
            "latest_interaction",
            "result",
            "error",
        }
        clean = {key: value for key, value in changes.items() if key in allowed}
        if "latest_interaction" in clean and isinstance(clean["latest_interaction"], dict):
            clean["latest_interaction"] = _json(clean["latest_interaction"])
        if "latest_interaction" in clean:
            clean["latest_interaction_json"] = clean.pop("latest_interaction")
        if not clean:
            return self.require_run(run_id)
        clean["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in clean)
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE agent_runs SET {assignments} WHERE id = ?",
                (*clean.values(), run_id),
            )
            if cursor.rowcount != 1:
                raise AgentRuntimeError("RUN_NOT_FOUND", "找不到这次 Agent 运行", status=404)
            state = self._ensure_project_state_locked(self.require_run(run_id).project_id)
            if state.run_id == run_id and any(
                key in changes for key in ("session_id", "status", "interaction_id", "error")
            ):
                phase, _separator, execution = state.state.partition(":")
                next_execution = str(changes.get("status", execution))
                if next_execution not in _EXECUTIONS:
                    raise AgentRuntimeError("STATE_INVALID", f"无效的执行状态：{next_execution}", status=400)
                self._transition_project_state_locked(
                    state,
                    next_state=f"{phase}:{next_execution}",
                    cause=(
                        "execution_session_rotated"
                        if "session_id" in changes and "status" not in changes
                        else f"run_{next_execution}"
                    ),
                    actor_run_id=run_id,
                    interaction_id=str(changes["interaction_id"]) if "interaction_id" in changes else None,
                    checkpoint_id=str(changes["session_id"]) if "session_id" in changes else None,
                    error=str(changes["error"]) if "error" in changes else None,
                )
        return self.require_run(run_id)

    def settle_interactions_resolved_elsewhere(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> list[InteractionRecord]:
        """把已经通过别的途径解决掉的挂起交互一并结算。

        资料用途有两条确认路径：批量确认直接落库到核心侧，逐份卡是助手运行时的
        交互对象。只解开前者会让后者作为「仍未解决的原问题」被自动恢复，
        于是用户刚一次性确认完，又被推回同一张卡（见 #36）。
        """
        timestamp = now_iso()
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT * FROM agent_interactions WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            ).fetchall()
            if not rows:
                return []
            self._db.execute(
                """UPDATE agent_interactions
                SET status = 'cancelled', answer_json = ?, answered_at = ?
                WHERE run_id = ? AND status = 'pending'""",
                (_json({"action": "resolved_elsewhere", "reason": reason}), timestamp, run_id),
            )
            self._db.execute(
                "UPDATE agent_runs SET interaction_id = '', updated_at = ? WHERE id = ?",
                (timestamp, run_id),
            )
            return [_interaction_from_row(row) for row in rows]

    def begin_run_cancellation(
        self,
        run_id: str,
        *,
        reason: str = "用户已停止运行",
    ) -> tuple[RunRecord, list[InteractionRecord]]:
        timestamp = now_iso()
        with self._lock, self._db:
            run = self.require_run(run_id)
            rows = self._db.execute(
                "SELECT * FROM agent_interactions WHERE run_id = ? AND status = 'pending'",
                (run_id,),
            ).fetchall()
            answer = _json({"action": "run_cancelled", "reason": reason})
            self._db.execute(
                """UPDATE agent_interactions
                SET status = 'cancelled', answer_json = ?, answered_at = ?
                WHERE run_id = ? AND status = 'pending'""",
                (answer, timestamp, run_id),
            )
            self._db.execute(
                """UPDATE agent_runs SET status = ?, interaction_id = '', error = '', updated_at = ?
                WHERE id = ?""",
                (RUN_STOPPING, timestamp, run_id),
            )
            state = self._ensure_project_state_locked(run.project_id)
            if state.run_id == run_id:
                phase, _separator, _execution = state.state.partition(":")
                self._transition_project_state_locked(
                    state,
                    next_state=f"{phase}:{RUN_STOPPING}",
                    cause="run_stopping",
                    actor_run_id=run_id,
                    interaction_id="",
                    error="",
                )
            closed = [
                InteractionRecord(
                    id=str(row["id"]),
                    project_id=str(row["project_id"]),
                    run_id=str(row["run_id"]),
                    tool_call_id=str(row["tool_call_id"]),
                    kind=str(row["kind"]),
                    payload=_object(row["payload_json"]),
                    status="cancelled",
                    answer={"action": "run_cancelled", "reason": reason},
                    created_at=str(row["created_at"]),
                    answered_at=timestamp,
                )
                for row in rows
            ]
        return self.require_run(run_id), closed

    def retry_entry(self, run_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM agent_retry_ledger
                WHERE run_id = ? AND operation_id = ?""",
                (run_id, operation_id),
            ).fetchone()
        return _retry_entry_from_row(row) if row else None

    def save_technical_recovery(
        self,
        run_id: str,
        issue_id: str,
        recovery: dict[str, Any],
    ) -> None:
        timestamp = now_iso()
        with self._lock, self._db:
            self.require_run(run_id)
            self._db.execute(
                """INSERT INTO agent_technical_recoveries (
                run_id, issue_id, state_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(run_id, issue_id) DO UPDATE SET
                  state_json = excluded.state_json,
                  status = 'active',
                  updated_at = excluded.updated_at""",
                (run_id, issue_id, _json(recovery), timestamp, timestamp),
            )

    def technical_recoveries(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT issue_id, state_json FROM agent_technical_recoveries
                WHERE run_id = ? AND status = 'active' ORDER BY created_at, issue_id""",
                (run_id,),
            ).fetchall()
        return {str(row["issue_id"]): _object(row["state_json"]) for row in rows}

    def resolve_technical_recoveries(self, run_id: str, issue_ids: list[str]) -> None:
        normalized = [str(item).strip() for item in issue_ids if str(item).strip()]
        if not normalized:
            return
        placeholders = ",".join("?" for _item in normalized)
        with self._lock, self._db:
            self._db.execute(
                f"""UPDATE agent_technical_recoveries
                SET status = 'resolved', updated_at = ?
                WHERE run_id = ? AND issue_id IN ({placeholders})""",
                (now_iso(), run_id, *normalized),
            )

    def record_retry(
        self,
        run_id: str,
        operation_id: str,
        *,
        category: str,
        max_attempts: int,
        error_fingerprint: str,
        last_error: str,
        delay_seconds: float = 0,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        next_allowed_at = (
            datetime.now(UTC) + timedelta(seconds=max(0.0, delay_seconds))
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with self._lock, self._db:
            self.require_run(run_id)
            row = self._db.execute(
                """SELECT attempt FROM agent_retry_ledger
                WHERE run_id = ? AND operation_id = ?""",
                (run_id, operation_id),
            ).fetchone()
            attempt = int(row["attempt"] or 0) + 1 if row else 1
            if attempt > max_attempts:
                raise AgentRuntimeError(
                    "RETRY_EXHAUSTED",
                    "该操作已经达到自动恢复上限",
                    status=409,
                    details={"operationId": operation_id, "attempt": attempt - 1},
                )
            self._db.execute(
                """INSERT INTO agent_retry_ledger (
                run_id, operation_id, category, attempt, max_attempts,
                error_fingerprint, last_error, status, next_allowed_at,
                created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'retrying', ?, ?, ?)
                ON CONFLICT(run_id, operation_id) DO UPDATE SET
                  category = excluded.category,
                  attempt = excluded.attempt,
                  max_attempts = excluded.max_attempts,
                  error_fingerprint = excluded.error_fingerprint,
                  last_error = excluded.last_error,
                  status = 'retrying',
                  next_allowed_at = excluded.next_allowed_at,
                  updated_at = excluded.updated_at""",
                (
                    run_id,
                    operation_id,
                    category,
                    attempt,
                    max_attempts,
                    error_fingerprint,
                    str(last_error)[:8_000],
                    next_allowed_at,
                    timestamp,
                    timestamp,
                ),
            )
        return self.retry_entry(run_id, operation_id) or {}

    def settle_retry(
        self,
        run_id: str,
        operation_id: str,
        status: str,
        *,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in {"succeeded", "exhausted", "cancelled"}:
            raise AgentRuntimeError("RETRY_STATUS_INVALID", "无效的重试结论", status=400)
        updates = ["status = ?", "next_allowed_at = ''", "updated_at = ?"]
        values: list[Any] = [status, now_iso()]
        if last_error is not None:
            updates.append("last_error = ?")
            values.append(str(last_error)[:8_000])
        with self._lock, self._db:
            self._db.execute(
                f"""UPDATE agent_retry_ledger SET {', '.join(updates)}
                WHERE run_id = ? AND operation_id = ?""",
                (*values, run_id, operation_id),
            )
        return self.retry_entry(run_id, operation_id)

    def retry_ledger(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM agent_retry_ledger
                WHERE run_id = ? ORDER BY created_at, operation_id""",
                (run_id,),
            ).fetchall()
        return [_retry_entry_from_row(row) for row in rows]

    def recover_interrupted(self) -> list[RunRecord]:
        timestamp = now_iso()
        with self._lock, self._db:
            # 首次升级时先从旧 run 事实初始化项目状态；初始化后只认唯一状态行。
            legacy_projects = self._db.execute(
                "SELECT DISTINCT project_id FROM agent_runs WHERE status = ?", (RUN_RUNNING,)
            ).fetchall()
            for project in legacy_projects:
                self._ensure_project_state_locked(str(project["project_id"]))
            state_rows = self._db.execute(
                """SELECT * FROM project_agent_state
                WHERE state LIKE ? OR state LIKE ? OR state LIKE ? OR state LIKE ?""",
                (
                    f"%:{RUN_RUNNING}",
                    f"%:{RUN_STOPPING}",
                    f"%:{RUN_SUSPENDED}",
                    f"%:{RUN_QUEUED}",
                ),
            ).fetchall()
            recovered: list[RunRecord] = []
            for state_row in state_rows:
                state = _project_state_from_row(state_row)
                row = self._db.execute(
                    "SELECT * FROM agent_runs WHERE id = ?", (state.run_id,)
                ).fetchone()
                if row is None and state.run_id:
                    # 幽灵占用：状态引用的运行记录已经丢失，不能自动恢复，
                    # 保持可诊断标记，避免旧计划不可执行、新计划不可创建的流程死锁。
                    self._transition_project_state_locked(
                        state,
                        next_state=state.state,
                        cause="recovery_stale_run",
                        actor_run_id=state.run_id,
                        recovery_status=RECOVERY_STALE_RUN,
                        error="运行记录已丢失，占用已标记为失效，请安全终止后重新规划",
                        metadata={"missing": ["run"]},
                    )
                    continue
                if row and not state.interaction_id:
                    self._db.execute(
                        """UPDATE agent_runs SET status = ?, error = ?, updated_at = ?
                        WHERE id = ?""",
                        (
                            RUN_SUSPENDED,
                            "Agent 服务重启，已停在最近检查点",
                            timestamp,
                            state.run_id,
                        ),
                    )
                    recovered.append(_run_from_row(self._db.execute(
                        "SELECT * FROM agent_runs WHERE id = ?", (state.run_id,)
                    ).fetchone()))
                phase, _separator, execution = state.state.partition(":")
                if execution in {RUN_RUNNING, RUN_STOPPING, RUN_QUEUED}:
                    self._transition_project_state_locked(
                        state,
                        next_state=f"{phase}:{RUN_SUSPENDED}",
                        cause="service_restarted",
                        actor_run_id=str(row["id"]) if row else state.run_id,
                        error="Agent 服务重启，已停在最近检查点",
                    )
        return recovered

    def terminate_stale_occupancy(
        self,
        project_id: str,
        *,
        reason: str = "用户安全终止失效运行",
    ) -> ProjectAgentState:
        """终止仅剩状态占用的失效运行，保留回执、候选与审计后可重新规划。"""
        with self._lock, self._db:
            state = self._ensure_project_state_locked(project_id)
            phase, _separator, execution = state.state.partition(":")
            if execution not in ACTIVE_RUN_STATUSES:
                raise AgentRuntimeError("RUN_NOT_ACTIVE", "当前没有需要终止的运行占用", status=409)
            if state.run_id and self.get_run(state.run_id) is not None:
                raise AgentRuntimeError(
                    "RUN_ACTIVE",
                    "该运行仍然存在，请使用正常取消入口",
                    status=409,
                    details={"runId": state.run_id},
                )
            return self._transition_project_state_locked(
                state,
                next_state=f"{phase}:{RUN_CANCELLED}",
                cause="stale_occupancy_terminated",
                actor_run_id=state.run_id,
                run_id="",
                interaction_id="",
                checkpoint_id="",
                error="",
                recovery_status=RECOVERY_NONE,
                metadata={"reason": reason, "terminatedRunId": state.run_id},
            )

    def mark_recovery_status(
        self,
        project_id: str,
        status: str,
        *,
        error: str = "",
        cause: str = "recovery_diagnostic",
    ) -> ProjectAgentState:
        """在锁内将当前状态迁移为新的恢复诊断（状态文本不变，仅更新诊断）。"""
        with self._lock, self._db:
            state = self._ensure_project_state_locked(project_id)
            phase, _separator, execution = state.state.partition(":")
            return self._transition_project_state_locked(
                state,
                next_state=f"{phase}:{execution}",
                cause=cause,
                actor_run_id=state.run_id,
                recovery_status=status,
                error=error or state.error,
            )

    def clear_recovery_status(self, status: str, *, cause: str) -> int:
        """Clear a stale recovery status after the corresponding invariant is verified."""
        cleared = 0
        with self._lock, self._db:
            rows = self._db.execute(
                "SELECT * FROM project_agent_state WHERE recovery_status = ?",
                (status,),
            ).fetchall()
            for row in rows:
                state = _project_state_from_row(row)
                self._transition_project_state_locked(
                    state,
                    next_state=state.state,
                    cause=cause,
                    actor_run_id=state.run_id,
                    recovery_status=RECOVERY_NONE,
                    error="",
                )
                cleared += 1
        return cleared

    def completed_tool_names(self, run_id: str) -> set[str]:
        with self._lock:
            rows = self._db.execute(
                """SELECT payload_json FROM agent_events
                WHERE run_id = ? AND kind = 'tool' ORDER BY seq""",
                (run_id,),
            ).fetchall()
        names: set[str] = set()
        for row in rows:
            payload = _object(row["payload_json"])
            if str(payload.get("phase") or "") != "completed":
                continue
            name = str(payload.get("name") or "").strip()
            if name:
                names.add(name)
        return names

    def tool_receipt(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        arguments_hash = _arguments_hash(arguments)
        with self._lock:
            row = self._db.execute(
                """SELECT tool_name, arguments_hash, result_json
                FROM agent_tool_receipts WHERE run_id = ? AND tool_call_id = ?""",
                (run_id, tool_call_id),
            ).fetchone()
        if row is None:
            return None
        if row["tool_name"] != tool_name or row["arguments_hash"] != arguments_hash:
            return {
                "error": "TOOL_CALL_ID_COLLISION",
                "reason": "恢复时发现工具调用标识与原请求不一致，已停止重复提交",
                "display": {"text": "已阻止不一致的恢复操作"},
            }
        return _object(row["result_json"])

    def save_tool_receipt(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        if not tool_call_id:
            return
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO agent_tool_receipts
                (run_id, tool_call_id, tool_name, arguments_hash, result_json, completed_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    tool_call_id,
                    tool_name,
                    _arguments_hash(arguments),
                    _json(result),
                    now_iso(),
                ),
            )

    def validation_repair_state(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT state_json FROM agent_validation_repair_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _object(row["state_json"]) if row else {}

    def save_validation_repair_state(
        self,
        run_id: str,
        state: dict[str, Any],
    ) -> None:
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO agent_validation_repair_state(run_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  state_json = excluded.state_json,
                  updated_at = excluded.updated_at""",
                (run_id, _json(state), now_iso()),
            )

    def create_interaction(self, interaction: InteractionRecord) -> InteractionRecord:
        interaction.created_at = interaction.created_at or now_iso()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO agent_interactions
                (id, project_id, run_id, tool_call_id, kind, payload_json, status,
                 answer_json, created_at, answered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    interaction.id,
                    interaction.project_id,
                    interaction.run_id,
                    interaction.tool_call_id,
                    interaction.kind,
                    _json(interaction.payload),
                    interaction.status,
                    _json(interaction.answer),
                    interaction.created_at,
                    interaction.answered_at,
                ),
            )
            self._db.execute(
                """UPDATE agent_runs SET status = ?, interaction_id = ?, updated_at = ?
                WHERE id = ?""",
                ("waiting", interaction.id, interaction.created_at, interaction.run_id),
            )
            state = self._ensure_project_state_locked(interaction.project_id)
            if state.run_id != interaction.run_id:
                raise AgentRuntimeError("STATE_RUN_MISMATCH", "待答问题不属于当前项目运行", status=409)
            phase, _separator, _execution = state.state.partition(":")
            self._transition_project_state_locked(
                state,
                next_state=f"{phase}:waiting",
                cause="interaction_requested",
                actor_run_id=interaction.run_id,
                interaction_id=interaction.id,
                error="",
            )
        return interaction

    def get_interaction(self, interaction_id: str) -> InteractionRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agent_interactions WHERE id = ?", (interaction_id,)
            ).fetchone()
        return _interaction_from_row(row) if row else None

    def set_interaction_submission_error(
        self,
        interaction_id: str,
        message: str,
    ) -> InteractionRecord | None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT payload_json FROM agent_interactions WHERE id = ? AND status = 'pending'",
                (interaction_id,),
            ).fetchone()
            if row is None:
                return None
            payload = _object(row["payload_json"])
            payload["lastSubmissionError"] = str(message).strip()[:1000]
            self._db.execute(
                "UPDATE agent_interactions SET payload_json = ? WHERE id = ? AND status = 'pending'",
                (_json(payload), interaction_id),
            )
        return self.get_interaction(interaction_id)

    def pending_interaction(self, project_id: str) -> InteractionRecord | None:
        state = self.project_state(project_id)
        if not state.interaction_id:
            return None
        interaction = self.get_interaction(state.interaction_id)
        return interaction if interaction is not None and interaction.status == "pending" else None

    def answer_interaction(self, interaction_id: str, answer: dict[str, Any]) -> InteractionRecord:
        timestamp = now_iso()
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE agent_interactions
                SET status = 'answered', answer_json = ?, answered_at = ?
                WHERE id = ? AND status = 'pending'""",
                (_json(answer), timestamp, interaction_id),
            )
            if cursor.rowcount != 1:
                current = self.get_interaction(interaction_id)
                if current is None:
                    raise AgentRuntimeError("INTERACTION_NOT_FOUND", "待答问题不存在", status=404)
                raise AgentRuntimeError("INTERACTION_CLOSED", "这个问题已经处理过了", status=409)
        # 升级待决不是终态：专家已经裁决，插话回到未处置队列，
        # 下一轮以裁决结论为背景重新成为本轮目标并完成收尾。
        self.reopen_escalated_interjections(interaction_id)
        return self.get_interaction(interaction_id)  # type: ignore[return-value]

    def append_event(
        self,
        project_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> RuntimeEvent:
        timestamp = now_iso()
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT INTO agent_events(project_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (project_id, run_id, kind, _json(payload), timestamp),
            )
            seq = int(cursor.lastrowid)
        return RuntimeEvent(seq, project_id, run_id, kind, payload, timestamp)

    def record_external_event(
        self,
        event_id: str,
        project_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO external_project_events
                (id, project_id, kind, payload_json, continuation_status, presented_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', '', ?)""",
                (event_id, project_id, kind, _json(payload), now_iso()),
            )
        return cursor.rowcount == 1

    def append_external_event_once(
        self,
        event_id: str,
        project_id: str,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> RuntimeEvent | None:
        timestamp = now_iso()
        with self._lock, self._db:
            receipt = self._db.execute(
                """SELECT presented_at FROM external_project_events
                WHERE id = ? AND project_id = ?""",
                (event_id, project_id),
            ).fetchone()
            if receipt is None or receipt["presented_at"]:
                return None
            cursor = self._db.execute(
                """INSERT INTO agent_events(project_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (project_id, run_id, kind, _json(payload), timestamp),
            )
            self._db.execute(
                """UPDATE external_project_events SET presented_at = ?
                WHERE id = ? AND project_id = ? AND presented_at = ''""",
                (timestamp, event_id, project_id),
            )
            seq = int(cursor.lastrowid)
        return RuntimeEvent(seq, project_id, run_id, kind, payload, timestamp)

    def external_event_pending(self, event_id: str, project_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                """SELECT continuation_status FROM external_project_events
                WHERE id = ? AND project_id = ?""",
                (event_id, project_id),
            ).fetchone()
        return bool(row and row["continuation_status"] == "pending")

    def claim_external_event_continuation(self, event_id: str, project_id: str) -> str:
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE external_project_events
                SET continuation_status = 'continuing'
                WHERE id = ? AND project_id = ? AND continuation_status = 'pending'""",
                (event_id, project_id),
            )
            if cursor.rowcount == 1:
                return "claimed"
            row = self._db.execute(
                """SELECT continuation_status FROM external_project_events
                WHERE id = ? AND project_id = ?""",
                (event_id, project_id),
            ).fetchone()
        return str(row["continuation_status"]) if row else "missing"

    def release_external_event_continuation(self, event_id: str, project_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE external_project_events SET continuation_status = 'pending'
                WHERE id = ? AND project_id = ? AND continuation_status = 'continuing'""",
                (event_id, project_id),
            )

    def latest_presented_evaluation(
        self,
        project_id: str,
        *,
        exclude_event_id: str = "",
    ) -> dict[str, Any] | None:
        with self._lock:
            rows = self._db.execute(
                """SELECT payload_json FROM external_project_events
                WHERE project_id = ? AND kind = 'evaluation_terminal'
                  AND presented_at <> '' AND id <> ?""",
                (project_id, exclude_event_id),
            ).fetchall()
        evaluations = [_object(row["payload_json"]) for row in rows]
        return max(
            evaluations,
            key=lambda item: (
                str(item.get("finishedAt") or ""),
                str(item.get("roundId") or ""),
            ),
            default=None,
        )

    def mark_external_event_continued(self, event_id: str, project_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE external_project_events
                SET continuation_status = 'continued', continued_at = ?
                WHERE id = ? AND project_id = ?
                  AND continuation_status IN ('pending', 'continuing')""",
                (now_iso(), event_id, project_id),
            )

    def material_batch_events(self, project_id: str, batch_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT payload_json FROM external_project_events
                WHERE project_id = ? AND kind = 'material_parse' ORDER BY created_at, id""",
                (project_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = _object(row["payload_json"])
            event_batch_id = str(payload.get("uploadBatchId") or payload.get("documentId") or "")
            if event_batch_id == batch_id and payload.get("status") in {"parsed", "failed"}:
                events.append(payload)
        return events

    def mark_material_batch_continued(self, project_id: str, batch_id: str) -> None:
        timestamp = now_iso()
        with self._lock, self._db:
            rows = self._db.execute(
                """SELECT id, payload_json FROM external_project_events
                WHERE project_id = ? AND kind = 'material_parse'""",
                (project_id,),
            ).fetchall()
            ids = []
            for row in rows:
                payload = _object(row["payload_json"])
                event_batch_id = str(payload.get("uploadBatchId") or payload.get("documentId") or "")
                if event_batch_id == batch_id and payload.get("status") in {"parsed", "failed"}:
                    ids.append(str(row["id"]))
            self._db.executemany(
                """UPDATE external_project_events
                SET continuation_status = 'continued', continued_at = ?
                WHERE id = ? AND project_id = ? AND continuation_status = 'pending'""",
                [(timestamp, event_id, project_id) for event_id in ids],
            )

    def events_after(self, project_id: str, after: int, limit: int = 200) -> list[RuntimeEvent]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM agent_events
                WHERE project_id = ? AND seq > ? ORDER BY seq LIMIT ?""",
                (project_id, max(0, after), min(max(1, limit), 1000)),
            ).fetchall()
        return [
            RuntimeEvent(
                seq=int(row["seq"]),
                project_id=row["project_id"],
                run_id=row["run_id"],
                kind=row["kind"],
                payload=_object(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def latest_event(self, project_id: str, run_id: str, kind: str) -> RuntimeEvent | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM agent_events
                WHERE project_id = ? AND run_id = ? AND kind = ?
                ORDER BY seq DESC LIMIT 1""",
                (project_id, run_id, kind),
            ).fetchone()
        if row is None:
            return None
        return RuntimeEvent(
            seq=int(row["seq"]),
            project_id=row["project_id"],
            run_id=row["run_id"],
            kind=row["kind"],
            payload=_object(row["payload_json"]),
            created_at=row["created_at"],
        )

    def unfinished_tool_events(self, run_id: str) -> list[RuntimeEvent]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM agent_events
                WHERE run_id = ? AND kind = 'tool' ORDER BY seq""",
                (run_id,),
            ).fetchall()
        active: dict[str, RuntimeEvent] = {}
        for row in rows:
            event = RuntimeEvent(
                seq=int(row["seq"]),
                project_id=row["project_id"],
                run_id=row["run_id"],
                kind=row["kind"],
                payload=_object(row["payload_json"]),
                created_at=row["created_at"],
            )
            phase = str(event.payload.get("phase") or "")
            identity = _tool_event_identity(event)
            if phase in {"started", "repairing"}:
                active[identity] = event
            elif phase in {"completed", "failed", "blocked", "rejected", "waiting", "cancelled"}:
                active.pop(identity, None)
        return sorted(active.values(), key=lambda event: event.seq)

    def latest_event_seq(self, project_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_events WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["seq"] if row else 0)

    def queue_steering(
        self,
        project_id: str,
        run_id: str,
        text: str,
        *,
        origin: str = "system",
    ) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT INTO steering_messages(project_id, run_id, text, origin, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (project_id, run_id, text, origin, now_iso()),
            )
            return int(cursor.lastrowid)

    def mark_steering_delivered(self, message_id: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE steering_messages
                SET status = 'delivered', delivered_at = ? WHERE id = ?""",
                (now_iso(), message_id),
            )

    def pending_interjections(self, project_id: str) -> list[dict[str, Any]]:
        """已投递但未处置的专家插话，按时间先后排列。

        按项目而不是按运行查询：插话是对项目说的，本轮预算耗尽没处置完，
        下一次运行仍然要接着处置，不能随运行结束一起蒸发。
        """
        with self._lock:
            rows = self._db.execute(
                """SELECT id, run_id, text, created_at FROM steering_messages
                WHERE project_id = ? AND origin = 'expert'
                  AND status = 'delivered' AND disposition = ''
                ORDER BY id""",
                (project_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "runId": str(row["run_id"]),
                "text": str(row["text"]),
                "createdAt": str(row["created_at"]),
            }
            for row in rows
        ]

    def dispose_interjection(
        self,
        project_id: str,
        interjection_id: int,
        *,
        resolution: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE steering_messages
                SET disposition = ?, disposition_json = ?, disposed_at = ?,
                    task_status = ?
                WHERE id = ? AND project_id = ? AND origin = 'expert'
                  AND status = 'delivered' AND disposition = ''""",
                (
                    resolution,
                    _json(detail),
                    now_iso(),
                    "open" if resolution == "task" else "",
                    interjection_id,
                    project_id,
                ),
            )
            if cursor.rowcount != 1:
                row = self._db.execute(
                    "SELECT disposition FROM steering_messages WHERE id = ? AND project_id = ?",
                    (interjection_id, project_id),
                ).fetchone()
                if row is None:
                    raise AgentRuntimeError(
                        "INTERJECTION_NOT_FOUND", "这条插话不存在或不属于当前项目", status=404,
                    )
                raise AgentRuntimeError(
                    "INTERJECTION_ALREADY_DISPOSED",
                    f"这条插话已经处置过了（{row['disposition']}），不能重复处置",
                    status=409,
                )
        return {"id": interjection_id, "resolution": resolution, **detail}

    def open_interjection_tasks(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT id, text, disposition_json, disposed_at FROM steering_messages
                WHERE project_id = ? AND disposition = 'task' AND task_status = 'open'
                ORDER BY id""",
                (project_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "text": str(_object(row["disposition_json"]).get("text") or row["text"]),
                "disposedAt": str(row["disposed_at"]),
            }
            for row in rows
        ]

    def complete_interjection_task(
        self,
        project_id: str,
        interjection_id: int,
        *,
        evidence: str,
    ) -> dict[str, Any]:
        with self._lock, self._db:
            row = self._db.execute(
                """SELECT disposition_json FROM steering_messages
                WHERE id = ? AND project_id = ? AND disposition = 'task' AND task_status = 'open'""",
                (interjection_id, project_id),
            ).fetchone()
            if row is None:
                raise AgentRuntimeError(
                    "INTERJECTION_TASK_NOT_OPEN",
                    "没有这条待办，或它已经完成",
                    status=409,
                )
            detail = _object(row["disposition_json"])
            detail["evidence"] = evidence
            detail["completedAt"] = now_iso()
            self._db.execute(
                """UPDATE steering_messages
                SET task_status = 'done', disposition_json = ? WHERE id = ?""",
                (_json(detail), interjection_id),
            )
        return {"id": interjection_id, **detail}

    def escalate_interjections(self, project_id: str, interaction_id: str) -> list[int]:
        """把未处置插话标记为「已升级待决」，绑定裁决卡。

        升级不是终态：裁决卡被回答时 reopen 回未处置，插话重新成为本轮目标，
        由模型按专家裁决完成收尾。这样错误升级（无关的冲突卡恰好出现）最多
        延迟处置一次，不会静默吞掉插话。
        """
        with self._lock, self._db:
            rows = self._db.execute(
                """SELECT id FROM steering_messages
                WHERE project_id = ? AND origin = 'expert'
                  AND status = 'delivered' AND disposition = ''""",
                (project_id,),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                timestamp = now_iso()
                detail = _json({"interactionId": interaction_id})
                self._db.executemany(
                    """UPDATE steering_messages
                    SET disposition = 'escalated', disposition_json = ?, disposed_at = ?
                    WHERE id = ?""",
                    [(detail, timestamp, item) for item in ids],
                )
        return ids

    def reopen_escalated_interjections(self, interaction_id: str) -> list[int]:
        with self._lock, self._db:
            rows = self._db.execute(
                """SELECT id, disposition_json FROM steering_messages
                WHERE disposition = 'escalated'""",
            ).fetchall()
            ids = [
                int(row["id"])
                for row in rows
                if _object(row["disposition_json"]).get("interactionId") == interaction_id
            ]
            if ids:
                self._db.executemany(
                    """UPDATE steering_messages
                    SET disposition = '', disposition_json = '{}', disposed_at = ''
                    WHERE id = ?""",
                    [(item,) for item in ids],
                )
        return ids

    def register_constraint(
        self,
        project_id: str,
        text: str,
        *,
        source_steering_id: int | None = None,
        replaces_id: int | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        with self._lock, self._db:
            if replaces_id is not None:
                replaced = self._db.execute(
                    """SELECT id FROM project_constraints
                    WHERE id = ? AND project_id = ? AND status = 'active'""",
                    (replaces_id, project_id),
                ).fetchone()
                if replaced is None:
                    raise AgentRuntimeError(
                        "CONSTRAINT_NOT_ACTIVE", "要覆盖的约束不存在或已失效", status=409,
                    )
            cursor = self._db.execute(
                """INSERT INTO project_constraints(project_id, text, source_steering_id, created_at)
                VALUES (?, ?, ?, ?)""",
                (project_id, text, source_steering_id, timestamp),
            )
            constraint_id = int(cursor.lastrowid)
            if replaces_id is not None:
                self._db.execute(
                    """UPDATE project_constraints
                    SET status = 'superseded', superseded_by = ?, ended_at = ?
                    WHERE id = ?""",
                    (constraint_id, timestamp, replaces_id),
                )
        return {"id": constraint_id, "text": text, "replaces": replaces_id}

    def revoke_constraint(
        self,
        project_id: str,
        constraint_id: int,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE project_constraints
                SET status = 'revoked', revoke_reason = ?, ended_at = ?
                WHERE id = ? AND project_id = ? AND status = 'active'""",
                (reason, now_iso(), constraint_id, project_id),
            )
            if cursor.rowcount != 1:
                raise AgentRuntimeError(
                    "CONSTRAINT_NOT_ACTIVE", "这条约束不存在或已失效", status=409,
                )
        return {"id": constraint_id, "revoked": True, "reason": reason}

    def active_constraints(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT id, text, created_at FROM project_constraints
                WHERE project_id = ? AND status = 'active' ORDER BY id""",
                (project_id,),
            ).fetchall()
        return [
            {"id": int(row["id"]), "text": str(row["text"]), "createdAt": str(row["created_at"])}
            for row in rows
        ]

    def list_constraints(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM project_constraints
                WHERE project_id = ? ORDER BY id DESC""",
                (project_id,),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "text": str(row["text"]),
                "status": str(row["status"]),
                "sourceSteeringId": row["source_steering_id"],
                "supersededBy": row["superseded_by"],
                "revokeReason": str(row["revoke_reason"]),
                "createdAt": str(row["created_at"]),
                "endedAt": str(row["ended_at"]),
            }
            for row in rows
        ]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


class RunValues(NamedTuple):
    id: str
    project_id: str
    session_id: str
    status: str
    prompt: str
    token_fingerprint: str
    tool_profile: str
    run_credential: str
    baseline_revision: str
    candidate_revision: str
    interaction_id: str
    latest_interaction_json: str
    result: str
    error: str
    created_at: str
    updated_at: str


def _run_values(run: RunRecord) -> RunValues:
    return RunValues(
        id=run.id,
        project_id=run.project_id,
        session_id=run.session_id,
        status=run.status,
        prompt=run.prompt,
        token_fingerprint=run.token_fingerprint,
        tool_profile=run.tool_profile,
        run_credential=run.run_credential,
        baseline_revision=run.baseline_revision,
        candidate_revision=run.candidate_revision,
        interaction_id=run.interaction_id,
        latest_interaction_json=_json(run.latest_interaction),
        result=run.result,
        error=run.error,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        status=row["status"],
        prompt=row["prompt"],
        token_fingerprint=row["token_fingerprint"],
        tool_profile=row["tool_profile"],
        run_credential=row["run_credential"],
        baseline_revision=row["baseline_revision"],
        candidate_revision=row["candidate_revision"],
        interaction_id=row["interaction_id"],
        latest_interaction=_object(row["latest_interaction_json"]),
        result=row["result"],
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _arguments_hash(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_event_identity(event: RuntimeEvent) -> str:
    payload = event.payload
    tool_call_id = str(payload.get("toolCallId") or "").strip()
    if tool_call_id:
        return f"call:{tool_call_id}"
    trace_group_id = str(payload.get("traceGroupId") or "").strip()
    name = str(payload.get("name") or "").strip()
    if trace_group_id:
        return f"group:{trace_group_id}"
    return f"name:{name}"


def _retry_entry_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "runId": str(row["run_id"]),
        "operationId": str(row["operation_id"]),
        "category": str(row["category"]),
        "attempt": int(row["attempt"]),
        "maxAttempts": int(row["max_attempts"]),
        "errorFingerprint": str(row["error_fingerprint"]),
        "lastError": str(row["last_error"]),
        "status": str(row["status"]),
        "nextAllowedAt": str(row["next_allowed_at"]),
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def _interaction_from_row(row: sqlite3.Row) -> InteractionRecord:
    return InteractionRecord(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        tool_call_id=row["tool_call_id"],
        kind=row["kind"],
        payload=_object(row["payload_json"]),
        status=row["status"],
        answer=_object(row["answer_json"]),
        created_at=row["created_at"],
        answered_at=row["answered_at"],
    )


_PHASES = frozenset({
    "intake", "goal", "process", "knowledge", "increment", "validation", "evaluation", "release",
})
_EXECUTIONS = frozenset({"idle", "queued", "running", "stopping", "waiting", "suspended", "completed", "failed", "cancelled"})


def _validate_state(value: str) -> None:
    phase, separator, execution = value.partition(":")
    if separator != ":" or phase not in _PHASES or execution not in _EXECUTIONS:
        raise AgentRuntimeError("STATE_INVALID", f"无效的项目 Agent 状态：{value}", status=400)


class ProjectStateValues(NamedTuple):
    project_id: str
    state: str
    revision: int
    round_mode: str
    round_id: str
    baseline_revision: str
    run_id: str
    interaction_id: str
    checkpoint_id: str
    summary: str
    open_questions_json: str
    error: str
    recovery_status: str
    updated_at: str


def _project_state_values(state: ProjectAgentState) -> ProjectStateValues:
    return ProjectStateValues(
        project_id=state.project_id,
        state=state.state,
        revision=state.revision,
        round_mode=state.round_mode,
        round_id=state.round_id,
        baseline_revision=state.baseline_revision,
        run_id=state.run_id,
        interaction_id=state.interaction_id,
        checkpoint_id=state.checkpoint_id,
        summary=state.summary,
        open_questions_json=_json(state.open_questions),
        error=state.error,
        recovery_status=state.recovery_status,
        updated_at=state.updated_at,
    )


def _project_state_from_row(row: sqlite3.Row) -> ProjectAgentState:
    try:
        questions = json.loads(row["open_questions_json"])
    except (TypeError, json.JSONDecodeError):
        questions = []
    recovery_status = str(row["recovery_status"]) if "recovery_status" in row.keys() else ""
    return ProjectAgentState(
        project_id=row["project_id"],
        state=row["state"],
        revision=int(row["revision"]),
        round_mode=str(row["round_mode"] or "initial"),
        round_id=str(row["round_id"] or ""),
        baseline_revision=str(row["baseline_revision"] or ""),
        run_id=row["run_id"],
        interaction_id=row["interaction_id"],
        checkpoint_id=row["checkpoint_id"],
        summary=row["summary"],
        open_questions=[str(item) for item in questions] if isinstance(questions, list) else [],
        error=row["error"],
        recovery_status=recovery_status,
        updated_at=row["updated_at"],
    )
