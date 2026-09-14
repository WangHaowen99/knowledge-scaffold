# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Durable evidence rebuild jobs and management-operation idempotency."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


class EvidenceOperationsError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceOperationsStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def create_or_get_build_job(
        self,
        *,
        job_id: str,
        workspace_id: str,
        ontology_id: str,
        ontology_epoch: str,
        target_generation: str,
        mutation_revision: int,
        semantic: str,
        reason: str,
        requested_by: str,
        idempotency_key: str,
        request_hash: str,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        now = _utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=max(1, ttl_hours))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            _delete_expired_idempotency_keys(connection, now)
            existing = connection.execute(
                """
                SELECT * FROM idempotency_keys
                WHERE workspace_id=? AND operation='rebuild' AND idempotency_key=?
                """,
                (workspace_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise EvidenceOperationsError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 Idempotency-Key 不能用于不同的重建请求。",
                    )
                row = connection.execute(
                    "SELECT * FROM build_jobs WHERE job_id=?",
                    (existing["result_ref"],),
                ).fetchone()
                connection.commit()
                if row is None:
                    raise EvidenceOperationsError(
                        "EVIDENCE_STORAGE_INCONSISTENT",
                        "幂等记录指向的重建任务不存在。",
                    )
                return _job_contract(dict(row), duplicate=True)

            connection.execute(
                """
                INSERT INTO build_jobs (
                  job_id, workspace_id, ontology_id, ontology_epoch,
                  target_generation, mutation_revision, generation_kind,
                  semantic, status, trigger, requested_by, reason,
                  idempotency_key, attempt, created_at, started_at,
                  completed_at, error_code, error_summary
                ) VALUES (?, ?, ?, ?, ?, ?, 'lexical', ?, 'queued', 'manual', ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL)
                """,
                (
                    job_id,
                    workspace_id,
                    ontology_id,
                    ontology_epoch,
                    target_generation,
                    int(mutation_revision),
                    semantic,
                    requested_by,
                    reason,
                    idempotency_key,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                  workspace_id, operation, idempotency_key, request_hash,
                  result_ref, result_json, created_at, expires_at
                ) VALUES (?, 'rebuild', ?, ?, ?, NULL, ?, ?)
                """,
                (workspace_id, idempotency_key, request_hash, job_id, now, expires_at),
            )
            row = connection.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
            connection.commit()
        if row is None:
            raise EvidenceOperationsError("EVIDENCE_STORAGE_INCONSISTENT", "重建任务写入失败。")
        return _job_contract(dict(row), duplicate=False)

    def claim_build_jobs(self, *, limit: int = 2, stale_after_seconds: int = 600) -> list[dict[str, Any]]:
        stale_before = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, stale_after_seconds))
        ).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM build_jobs
                WHERE status='queued' OR (status='building' AND started_at<=?)
                ORDER BY created_at, job_id LIMIT ?
                """,
                (stale_before, max(1, min(int(limit), 8))),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            now = _utc_now()
            for row in rows:
                attempt = int(row["attempt"] or 0) + 1
                if attempt > 3:
                    connection.execute(
                        """
                        UPDATE build_jobs SET status='failed', completed_at=?,
                          error_code='BUILD_RETRY_EXHAUSTED', error_summary='重建任务重试次数已耗尽。'
                        WHERE job_id=?
                        """,
                        (now, row["job_id"]),
                    )
                    continue
                connection.execute(
                    """
                    UPDATE build_jobs SET status='building', attempt=?, started_at=?,
                      completed_at=NULL, error_code=NULL, error_summary=NULL
                    WHERE job_id=?
                    """,
                    (attempt, now, row["job_id"]),
                )
                payload = dict(row)
                payload.update({"status": "building", "attempt": attempt, "started_at": now})
                claimed.append(payload)
            connection.commit()
            return claimed

    def mark_build_succeeded(self, job_id: str) -> None:
        self._finish_build(job_id, status="succeeded", error_code=None, error_summary=None)

    def mark_build_failed(self, job_id: str, *, error_code: str, error_summary: str) -> None:
        self._finish_build(
            job_id,
            status="failed",
            error_code=str(error_code or "INDEX_BUILD_FAILED")[:64],
            error_summary=str(error_summary or "证据索引构建失败。")[:300],
        )

    def mark_build_cancelled(self, job_id: str, *, reason: str = "目标知识包已变化。") -> None:
        self._finish_build(
            job_id,
            status="cancelled",
            error_code="INDEX_STALE",
            error_summary=str(reason)[:300],
        )

    def get_build_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM build_jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(row) if row is not None else None

    def latest_build_job(self, *, workspace_id: str, ontology_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM build_jobs WHERE workspace_id=? AND ontology_id=?
                ORDER BY created_at DESC, job_id DESC LIMIT 1
                """,
                (workspace_id, ontology_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def reserve_result(
        self,
        *,
        workspace_id: str,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        result_ref: str,
        ttl_hours: int = 24,
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        now = _utc_now()
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=max(1, ttl_hours))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            _delete_expired_idempotency_keys(connection, now)
            row = connection.execute(
                """
                SELECT * FROM idempotency_keys
                WHERE workspace_id=? AND operation=? AND idempotency_key=?
                """,
                (workspace_id, operation, idempotency_key),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise EvidenceOperationsError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 Idempotency-Key 不能用于不同的清理请求。",
                    )
                connection.commit()
                value = _loads_object(row["result_json"])
                return False, value
            connection.execute(
                """
                INSERT INTO idempotency_keys (
                  workspace_id, operation, idempotency_key, request_hash,
                  result_ref, result_json, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    workspace_id,
                    operation,
                    idempotency_key,
                    request_hash,
                    result_ref,
                    now,
                    expires_at,
                ),
            )
            connection.commit()
            return True, None

    def complete_result(
        self,
        *,
        workspace_id: str,
        operation: str,
        idempotency_key: str,
        result: dict[str, Any],
    ) -> None:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE idempotency_keys SET result_json=?
                WHERE workspace_id=? AND operation=? AND idempotency_key=?
                """,
                (payload, workspace_id, operation, idempotency_key),
            )
            connection.commit()

    def release_result(
        self,
        *,
        workspace_id: str,
        operation: str,
        idempotency_key: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM idempotency_keys
                WHERE workspace_id=? AND operation=? AND idempotency_key=? AND result_json IS NULL
                """,
                (workspace_id, operation, idempotency_key),
            )
            connection.commit()

    def _finish_build(
        self,
        job_id: str,
        *,
        status: str,
        error_code: Optional[str],
        error_summary: Optional[str],
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE build_jobs SET status=?, completed_at=?, error_code=?, error_summary=?
                WHERE job_id=? AND status='building'
                """,
                (status, _utc_now(), error_code, error_summary, job_id),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


def read_latest_build_job_snapshot(
    path: Path,
    *,
    workspace_id: str,
    ontology_id: str,
) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=1.0)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT * FROM build_jobs WHERE workspace_id=? AND ontology_id=?
            ORDER BY created_at DESC, job_id DESC LIMIT 1
            """,
            (workspace_id, ontology_id),
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def build_job_contract(row: dict[str, Any], *, duplicate: bool = False) -> dict[str, Any]:
    return _job_contract(row, duplicate=duplicate)


def _job_contract(row: dict[str, Any], *, duplicate: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract": "OntologyEvidenceIndexJob/v1",
        "job_id": row["job_id"],
        "ontology_id": row["ontology_id"],
        "ontology_epoch": row["ontology_epoch"],
        "mutation_revision": int(row["mutation_revision"]),
        "target_generation": row["target_generation"],
        "status": row["status"],
        "duplicate": bool(duplicate),
        "created_at": row["created_at"],
    }
    if row.get("started_at"):
        payload["started_at"] = row["started_at"]
    if row.get("completed_at"):
        payload["completed_at"] = row["completed_at"]
    if row.get("error_code"):
        payload["error"] = {
            "code": row["error_code"],
            "message": row.get("error_summary") or "证据索引构建失败。",
        }
    return payload


def _loads_object(value: Any) -> Optional[dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _delete_expired_idempotency_keys(
    connection: sqlite3.Connection,
    now: str,
) -> None:
    connection.execute(
        "DELETE FROM idempotency_keys WHERE expires_at<=?",
        (now,),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS build_jobs (
  job_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  ontology_id TEXT NOT NULL,
  ontology_epoch TEXT NOT NULL,
  target_generation TEXT NOT NULL,
  mutation_revision INTEGER NOT NULL CHECK(mutation_revision >= 0),
  generation_kind TEXT NOT NULL CHECK(generation_kind IN ('lexical','semantic')),
  semantic TEXT NOT NULL CHECK(semantic IN ('inherit','enable','disable')),
  status TEXT NOT NULL CHECK(status IN ('queued','building','succeeded','failed','cancelled')),
  trigger TEXT NOT NULL,
  requested_by TEXT,
  reason TEXT,
  idempotency_key TEXT,
  attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  error_code TEXT,
  error_summary TEXT
);
CREATE INDEX IF NOT EXISTS idx_build_jobs_ontology_status
  ON build_jobs(workspace_id, ontology_id, status);
CREATE INDEX IF NOT EXISTS idx_build_jobs_created_at ON build_jobs(created_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  workspace_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('rebuild','static_cleanup','runtime_cleanup')),
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  result_ref TEXT NOT NULL,
  result_json TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id, operation, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expires_at ON idempotency_keys(expires_at);
"""
