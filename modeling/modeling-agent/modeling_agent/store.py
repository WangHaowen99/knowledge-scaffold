from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import AgentRuntimeError
from .schemas import (
    ACTIVE_RUN_STATUSES,
    InteractionRecord,
    ProjectAgentState,
    RUN_RUNNING,
    RUN_STOPPING,
    RUN_SUSPENDED,
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
  run_credential TEXT NOT NULL DEFAULT '',
  baseline_revision TEXT NOT NULL DEFAULT '',
  candidate_revision TEXT NOT NULL DEFAULT '',
  interaction_id TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS steering_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  created_at TEXT NOT NULL,
  delivered_at TEXT NOT NULL DEFAULT ''
);

-- 每个项目唯一的当前状态。业务阶段与执行状态编码在同一个 state 字段中，
-- 例如 goal:waiting；其他表只保存事实记录、事件或恢复材料。
CREATE TABLE IF NOT EXISTS project_agent_state (
  project_id TEXT PRIMARY KEY,
  state TEXT NOT NULL DEFAULT 'intake:idle',
  revision INTEGER NOT NULL DEFAULT 0,
  run_id TEXT NOT NULL DEFAULT '',
  interaction_id TEXT NOT NULL DEFAULT '',
  checkpoint_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  open_questions_json TEXT NOT NULL DEFAULT '[]',
  error TEXT NOT NULL DEFAULT '',
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
                metadata=metadata,
            )

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
        pending = self._db.execute(
            """SELECT id FROM agent_interactions
            WHERE project_id = ? AND status = 'pending' ORDER BY created_at LIMIT 1""",
            (project_id,),
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
            project_id, state, revision, run_id, interaction_id, checkpoint_id,
            summary, open_questions_json, error, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
        metadata: dict[str, Any] | None = None,
    ) -> ProjectAgentState:
        _validate_state(next_state)
        updated = ProjectAgentState(
            project_id=current.project_id,
            state=next_state,
            revision=current.revision + 1,
            run_id=current.run_id if run_id is None else run_id,
            interaction_id=current.interaction_id if interaction_id is None else interaction_id,
            checkpoint_id=current.checkpoint_id if checkpoint_id is None else checkpoint_id,
            summary=current.summary if summary is None else summary[:8_000],
            open_questions=current.open_questions if open_questions is None else open_questions[:30],
            error=current.error if error is None else error[:8_000],
            updated_at=now_iso(),
        )
        changed = self._db.execute(
            """UPDATE project_agent_state SET
            state = ?, revision = ?, run_id = ?, interaction_id = ?, checkpoint_id = ?,
            summary = ?, open_questions_json = ?, error = ?, updated_at = ?
            WHERE project_id = ? AND revision = ?""",
            (
                updated.state,
                updated.revision,
                updated.run_id,
                updated.interaction_id,
                updated.checkpoint_id,
                updated.summary,
                _json(updated.open_questions),
                updated.error,
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
                raise AgentRuntimeError(
                    "RUN_ACTIVE",
                    "该项目已有正在运行或等待处理的 Agent",
                    status=409,
                    details={"runId": state.run_id, "state": state.state},
                )
            self._db.execute(
                """INSERT INTO agent_runs
                (id, project_id, session_id, status, prompt, token_fingerprint,
                 run_credential, baseline_revision, candidate_revision, interaction_id,
                 result, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            "run_credential",
            "baseline_revision",
            "candidate_revision",
            "interaction_id",
            "result",
            "error",
        }
        clean = {key: value for key, value in changes.items() if key in allowed}
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

    def retry_entry(self, run_id: str, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM agent_retry_ledger
                WHERE run_id = ? AND operation_id = ?""",
                (run_id, operation_id),
            ).fetchone()
        return _retry_entry_from_row(row) if row else None

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
                WHERE state LIKE ? OR state LIKE ? OR state LIKE ?""",
                (f"%:{RUN_RUNNING}", f"%:{RUN_STOPPING}", f"%:{RUN_SUSPENDED}"),
            ).fetchall()
            recovered: list[RunRecord] = []
            for state_row in state_rows:
                state = _project_state_from_row(state_row)
                row = self._db.execute(
                    "SELECT * FROM agent_runs WHERE id = ?", (state.run_id,)
                ).fetchone()
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
                if execution in {RUN_RUNNING, RUN_STOPPING}:
                    self._transition_project_state_locked(
                        state,
                        next_state=f"{phase}:{RUN_SUSPENDED}",
                        cause="service_restarted",
                        actor_run_id=str(row["id"]) if row else state.run_id,
                        error="Agent 服务重启，已停在最近检查点",
                    )
        return recovered

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

    def mark_external_event_continued(self, event_id: str, project_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE external_project_events
                SET continuation_status = 'continued', continued_at = ?
                WHERE id = ? AND project_id = ? AND continuation_status = 'pending'""",
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

    def latest_event_seq(self, project_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS seq FROM agent_events WHERE project_id = ?",
                (project_id,),
            ).fetchone()
        return int(row["seq"] if row else 0)

    def queue_steering(self, project_id: str, run_id: str, text: str) -> int:
        with self._lock, self._db:
            cursor = self._db.execute(
                """INSERT INTO steering_messages(project_id, run_id, text, created_at)
                VALUES (?, ?, ?, ?)""",
                (project_id, run_id, text, now_iso()),
            )
            return int(cursor.lastrowid)

    def mark_steering_delivered(self, message_id: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                """UPDATE steering_messages
                SET status = 'delivered', delivered_at = ? WHERE id = ?""",
                (now_iso(), message_id),
            )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_values(run: RunRecord) -> tuple[Any, ...]:
    return (
        run.id,
        run.project_id,
        run.session_id,
        run.status,
        run.prompt,
        run.token_fingerprint,
        run.run_credential,
        run.baseline_revision,
        run.candidate_revision,
        run.interaction_id,
        run.result,
        run.error,
        run.created_at,
        run.updated_at,
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        status=row["status"],
        prompt=row["prompt"],
        token_fingerprint=row["token_fingerprint"],
        run_credential=row["run_credential"],
        baseline_revision=row["baseline_revision"],
        candidate_revision=row["candidate_revision"],
        interaction_id=row["interaction_id"],
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


def _project_state_values(state: ProjectAgentState) -> tuple[Any, ...]:
    return (
        state.project_id,
        state.state,
        state.revision,
        state.run_id,
        state.interaction_id,
        state.checkpoint_id,
        state.summary,
        _json(state.open_questions),
        state.error,
        state.updated_at,
    )


def _project_state_from_row(row: sqlite3.Row) -> ProjectAgentState:
    try:
        questions = json.loads(row["open_questions_json"])
    except (TypeError, json.JSONDecodeError):
        questions = []
    return ProjectAgentState(
        project_id=row["project_id"],
        state=row["state"],
        revision=int(row["revision"]),
        run_id=row["run_id"],
        interaction_id=row["interaction_id"],
        checkpoint_id=row["checkpoint_id"],
        summary=row["summary"],
        open_questions=[str(item) for item in questions] if isinstance(questions, list) else [],
        error=row["error"],
        updated_at=row["updated_at"],
    )
