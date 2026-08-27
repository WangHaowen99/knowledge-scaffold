"""Finite-TTL durable job mapping for the OR-Tools Solver Service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Callable, Mapping
import uuid


TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "attempt_lost"}
)


class IdempotencyConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class AttemptRecord:
    job_id: str
    idempotency_key: str
    request_digest: str
    request_id: str
    problem_type: str
    problem_version: str
    status: str
    result: Mapping[str, Any] | None
    error_code: str | None
    created_at: str
    updated_at: str
    expires_at: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AttemptStore:
    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._lock = RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_digest TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    problem_type TEXT NOT NULL,
                    problem_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            # A process restart cannot silently resume an in-memory CP-SAT child.
            now = self._now_text()
            self._connection.execute(
                """
                UPDATE attempts
                   SET status = 'attempt_lost', error_code = 'ATTEMPT_LOST', updated_at = ?
                 WHERE status IN ('accepted', 'running')
                """,
                (now,),
            )
        self.purge_expired()

    def reopen_after_restart(self) -> None:
        """Reopen SQLite and fence any in-flight attempts as ``attempt_lost``."""

        with self._lock:
            self._connection.close()
            self._connection = sqlite3.connect(
                str(self.path), check_same_thread=False, isolation_level=None
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            now = self._now_text()
            self._connection.execute(
                """
                UPDATE attempts
                   SET status = 'attempt_lost', error_code = 'ATTEMPT_LOST', updated_at = ?
                 WHERE status IN ('accepted', 'running')
                """,
                (now,),
            )
            self.purge_expired()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _record(row: sqlite3.Row | None) -> AttemptRecord | None:
        if row is None:
            return None
        raw_result = row["result_json"]
        result = json.loads(raw_result) if raw_result is not None else None
        return AttemptRecord(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            request_digest=row["request_digest"],
            request_id=row["request_id"],
            problem_type=row["problem_type"],
            problem_version=row["problem_version"],
            status=row["status"],
            result=result,
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
        )

    def create_or_get(
        self,
        *,
        idempotency_key: str,
        request_digest: str,
        request_id: str,
        problem_type: str,
        problem_version: str,
    ) -> tuple[AttemptRecord, bool]:
        self.purge_expired()
        with self._lock:
            existing = self._connection.execute(
                "SELECT * FROM attempts WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                record = self._record(existing)
                if record is None:
                    raise RuntimeError("attempt store failed to decode an existing row")
                if record.request_digest != request_digest:
                    raise IdempotencyConflict("idempotency key payload conflict")
                return record, False
            now = self._now()
            job_id = "job_" + uuid.uuid4().hex
            created_at = now.isoformat()
            expires_at = (now + timedelta(seconds=self.ttl_seconds)).isoformat()
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT INTO attempts (
                        job_id, idempotency_key, request_digest, request_id,
                        problem_type, problem_version, status, result_json,
                        error_code, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'accepted', NULL, NULL, ?, ?, ?)
                    """,
                    (
                        job_id,
                        idempotency_key,
                        request_digest,
                        request_id,
                        problem_type,
                        problem_version,
                        created_at,
                        created_at,
                        expires_at,
                    ),
                )
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            record = self.get(job_id)
            if record is None:
                raise RuntimeError("attempt store failed to read a created row")
            return record, True

    def get(self, job_id: str) -> AttemptRecord | None:
        # Expiration is an access boundary as well as a maintenance concern:
        # polling an old id must not keep a result readable indefinitely.
        self.purge_expired()
        with self._lock:
            return self._record(
                self._connection.execute(
                    "SELECT * FROM attempts WHERE job_id = ?", (job_id,)
                ).fetchone()
            )

    def get_by_idempotency_key(self, key: str) -> AttemptRecord | None:
        self.purge_expired()
        with self._lock:
            return self._record(
                self._connection.execute(
                    "SELECT * FROM attempts WHERE idempotency_key = ?", (key,)
                ).fetchone()
            )

    def update_status(
        self, job_id: str, status: str, *, error_code: str | None = None
    ) -> AttemptRecord:
        if status not in {
            "accepted",
            "running",
            "completed",
            "failed",
            "cancelled",
            "attempt_lost",
        }:
            raise ValueError("invalid solver job status")
        with self._lock:
            current = self.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if current.status in TERMINAL_STATUSES:
                return current
            self._connection.execute(
                "UPDATE attempts SET status = ?, error_code = ?, updated_at = ? WHERE job_id = ?",
                (status, error_code, self._now_text(), job_id),
            )
            record = self.get(job_id)
            if record is None:
                raise RuntimeError("attempt store failed to read an updated row")
            return record

    def complete(self, job_id: str, result: Mapping[str, Any]) -> AttemptRecord:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._lock:
            current = self.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if current.status in TERMINAL_STATUSES:
                return current
            self._connection.execute(
                """
                UPDATE attempts
                   SET status = 'completed', result_json = ?, error_code = NULL, updated_at = ?
                 WHERE job_id = ?
                """,
                (encoded, self._now_text(), job_id),
            )
            completed = self.get(job_id)
            if completed is None:
                raise RuntimeError("attempt store failed to read a completed row")
            return completed

    def cancel(self, job_id: str) -> AttemptRecord | None:
        with self._lock:
            current = self.get(job_id)
            if current is None or current.status in TERMINAL_STATUSES:
                return current
            return self.update_status(job_id, "cancelled")

    def purge_expired(self) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM attempts WHERE expires_at <= ?", (self._now_text(),)
            )
            return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


__all__ = ["AttemptRecord", "AttemptStore", "IdempotencyConflict"]
