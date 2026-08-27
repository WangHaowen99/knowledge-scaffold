"""Transactional ontology mutation revision and outbox coordination."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ontology.change_events import (
    CONTRACT,
    OntologyChangedValidationError,
    build_ontology_changed_event,
    canonical_event_json,
    load_event_json,
)
from ontology.evidence.manager import revoke_active_pointer_snapshot
from ontology.file_lock import exclusive_file_lock


logger = logging.getLogger("ontology.mutations")


class OntologyMutationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OntologyMutationConflictError(OntologyMutationError):
    pass


@dataclass
class PreparedOntologyMutation:
    workspace_id: str
    ontology_id: str
    mutation_type: str
    before_version: str | None
    after_version: str | None
    before_enabled: bool
    after_enabled: bool
    commit: Callable[[], None]
    rollback: Callable[[], None]
    precondition: Callable[[], None] | None = None
    digest_hints: Mapping[str, Any] | None = None
    deleted_paths: Sequence[str] = field(default_factory=tuple)
    operator: Mapping[str, Any] | None = None
    request_id: str | None = None
    trigger: str | None = None
    finalize: Callable[[], None] | None = None
    base_mutation_revision: int | None = None


@dataclass(frozen=True)
class MutationResult:
    workspace_id: str
    ontology_id: str
    mutation_revision: int
    event_id: str
    event: dict[str, Any]


@dataclass(frozen=True)
class OutboxRecord:
    event_id: str
    workspace_id: str
    ontology_id: str
    mutation_revision: int
    payload_json: str
    payload_hash: str
    attempt_count: int


class MutationStore:
    """Workspace-local authoritative revision and durable event outbox."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def get_state(self, *, workspace_id: str, ontology_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mutation_state WHERE workspace_id=? AND ontology_id=?",
                (workspace_id, ontology_id),
            ).fetchone()
            return dict(row) if row is not None else None

    def ensure_state(
        self,
        *,
        workspace_id: str,
        ontology_id: str,
        active_version: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        """Adopt an existing ontology at revision zero without emitting a mutation."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO mutation_state (
                  workspace_id, ontology_id, active_version, enabled,
                  mutation_revision, last_event_id, updated_at
                ) VALUES (?, ?, ?, ?, 0, NULL, ?)
                """,
                (workspace_id, ontology_id, active_version, int(bool(enabled)), _utc_now()),
            )
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mutation_state WHERE workspace_id=? AND ontology_id=?",
                (workspace_id, ontology_id),
            ).fetchone()
            connection.commit()
        if row is None:
            raise OntologyMutationError("MUTATION_STATE_UNAVAILABLE", "failed to initialize mutation state")
        state = dict(row)
        if (
            state.get("active_version") != active_version
            or bool(state.get("enabled")) != bool(enabled)
        ):
            raise OntologyMutationConflictError(
                "MUTATION_STATE_CONFLICT",
                "authoritative mutation state does not match current ontology",
            )
        return state

    def commit_prepared(
        self,
        mutation: PreparedOntologyMutation,
        *,
        pointer_revoker: Callable[[int], None],
    ) -> MutationResult:
        """Commit one mutation without holding SQLite across filesystem I/O.

        The coordinator holds the workspace/ontology file lock across this
        method.  SQLite is used in two short transactions: first to read and
        validate the base revision, then to CAS the same base revision while
        persisting the new state and outbox record.  Pointer and ontology file
        operations happen between those transactions.
        """

        current_revision = 0
        next_revision = 0
        filesystem_commit_attempted = False
        committed = False
        try:
            current_revision = self._read_prepared_base(mutation)
            next_revision = current_revision + 1
            if mutation.precondition is not None:
                mutation.precondition()
            pointer_revoker(next_revision)

            digest_hints = dict(mutation.digest_hints) if mutation.digest_hints is not None else None
            if digest_hints is not None:
                digest_hints["scope_version"] = mutation.after_version
                digest_hints["base_mutation_revision"] = (
                    next_revision - 1 if digest_hints.get("completeness") == "partial" else None
                )

            event = build_ontology_changed_event(
                workspace_id=mutation.workspace_id,
                ontology_id=mutation.ontology_id,
                mutation_type=mutation.mutation_type,
                mutation_revision=next_revision,
                before_version=mutation.before_version,
                after_version=mutation.after_version,
                known_file_digests=digest_hints,
                deleted_paths=mutation.deleted_paths,
                operator=mutation.operator,
                request_id=mutation.request_id,
                trigger=mutation.trigger,
            )
            payload_json = canonical_event_json(event)
            payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            filesystem_commit_attempted = True
            mutation.commit()
            self._commit_prepared_record(
                mutation,
                expected_revision=current_revision,
                next_revision=next_revision,
                event=event,
                payload_json=payload_json,
                payload_hash=payload_hash,
            )
            committed = True
        except Exception as exc:
            rollback_failed = False
            if filesystem_commit_attempted and not committed:
                try:
                    mutation.rollback()
                except Exception:
                    rollback_failed = True
                    logger.exception(
                        "Ontology mutation rollback failed: workspace_id=%s ontology_id=%s",
                        mutation.workspace_id,
                        mutation.ontology_id,
                    )
            if not rollback_failed:
                self._cleanup_prepared_mutation(mutation)
            if rollback_failed:
                raise OntologyMutationError(
                    "MUTATION_COMMIT_STATE_UNCERTAIN",
                    "ontology files may not match the authoritative mutation state",
                ) from exc
            raise

        if mutation.finalize is not None:
            try:
                mutation.finalize()
            except Exception:
                logger.warning(
                    "Ontology mutation post-commit cleanup failed: workspace_id=%s ontology_id=%s",
                    mutation.workspace_id,
                    mutation.ontology_id,
                    exc_info=True,
                )
        return MutationResult(
            workspace_id=mutation.workspace_id,
            ontology_id=mutation.ontology_id,
            mutation_revision=next_revision,
            event_id=event["event_id"],
            event=event,
        )

    @staticmethod
    def _cleanup_prepared_mutation(mutation: PreparedOntologyMutation) -> None:
        """Remove staged artifacts after a pre-commit conflict or rolled-back commit."""

        if mutation.finalize is None:
            return
        try:
            mutation.finalize()
        except Exception:
            logger.warning(
                "Ontology mutation abort cleanup failed: workspace_id=%s ontology_id=%s",
                mutation.workspace_id,
                mutation.ontology_id,
                exc_info=True,
            )

    def _read_prepared_base(self, mutation: PreparedOntologyMutation) -> int:
        """Read a stable base revision in a short SQLite write transaction."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mutation_state WHERE workspace_id=? AND ontology_id=?",
                (mutation.workspace_id, mutation.ontology_id),
            ).fetchone()
            if row is None:
                current_revision = 0
                current_version = mutation.before_version
                current_enabled = bool(mutation.before_enabled)
            else:
                current_revision = int(row["mutation_revision"])
                current_version = row["active_version"]
                current_enabled = bool(row["enabled"])
            self._validate_prepared_base(
                mutation,
                current_revision=current_revision,
                current_version=current_version,
                current_enabled=current_enabled,
            )
            connection.commit()
            return current_revision

    def _commit_prepared_record(
        self,
        mutation: PreparedOntologyMutation,
        *,
        expected_revision: int,
        next_revision: int,
        event: Mapping[str, Any],
        payload_json: str,
        payload_hash: str,
    ) -> None:
        """CAS the base revision and atomically persist state plus outbox."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM mutation_state WHERE workspace_id=? AND ontology_id=?",
                (mutation.workspace_id, mutation.ontology_id),
            ).fetchone()
            if row is None:
                current_revision = 0
                current_version = mutation.before_version
                current_enabled = bool(mutation.before_enabled)
            else:
                current_revision = int(row["mutation_revision"])
                current_version = row["active_version"]
                current_enabled = bool(row["enabled"])
            self._validate_prepared_base(
                mutation,
                current_revision=current_revision,
                current_version=current_version,
                current_enabled=current_enabled,
                expected_revision=expected_revision,
            )

            now = _utc_now()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO mutation_state (
                      workspace_id, ontology_id, active_version, enabled,
                      mutation_revision, last_event_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mutation.workspace_id,
                        mutation.ontology_id,
                        mutation.after_version,
                        int(bool(mutation.after_enabled)),
                        next_revision,
                        event["event_id"],
                        now,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE mutation_state SET active_version=?, enabled=?, mutation_revision=?,
                      last_event_id=?, updated_at=?
                    WHERE workspace_id=? AND ontology_id=? AND mutation_revision=?
                      AND active_version IS ? AND enabled=?
                    """,
                    (
                        mutation.after_version,
                        int(bool(mutation.after_enabled)),
                        next_revision,
                        event["event_id"],
                        now,
                        mutation.workspace_id,
                        mutation.ontology_id,
                        expected_revision,
                        mutation.before_version,
                        int(bool(mutation.before_enabled)),
                    ),
                )
                if cursor.rowcount != 1:
                    raise OntologyMutationConflictError(
                        "MUTATION_REVISION_CONFLICT",
                        "ontology changed while the prepared mutation was committing",
                    )
            connection.execute(
                """
                INSERT INTO mutation_outbox (
                  event_id, workspace_id, ontology_id, mutation_revision, contract,
                  payload_json, payload_hash, status, attempt_count, next_attempt_at,
                  error_code, created_at, dispatched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL)
                """,
                (
                    event["event_id"],
                    mutation.workspace_id,
                    mutation.ontology_id,
                    next_revision,
                    CONTRACT,
                    payload_json,
                    payload_hash,
                    now,
                ),
            )
            connection.commit()

    @staticmethod
    def _validate_prepared_base(
        mutation: PreparedOntologyMutation,
        *,
        current_revision: int,
        current_version: str | None,
        current_enabled: bool,
        expected_revision: int | None = None,
    ) -> None:
        if current_version != mutation.before_version or current_enabled != bool(mutation.before_enabled):
            raise OntologyMutationConflictError(
                "MUTATION_STATE_CONFLICT",
                "authoritative mutation state does not match prepared before-state",
            )
        required_revision = (
            int(mutation.base_mutation_revision)
            if mutation.base_mutation_revision is not None
            else expected_revision
        )
        if required_revision is not None and current_revision != required_revision:
            raise OntologyMutationConflictError(
                "MUTATION_REVISION_CONFLICT",
                "ontology changed after the mutation was prepared",
            )

    def claim_pending(self, *, limit: int = 16, lease_seconds: int = 30) -> list[OutboxRecord]:
        now = _utc_now()
        lease_until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, lease_seconds))).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM mutation_outbox
                WHERE (
                  status='pending' AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                ) OR (
                  status='delivering' AND next_attempt_at IS NOT NULL AND next_attempt_at<=?
                )
                ORDER BY created_at, event_id LIMIT ?
                """,
                (now, now, max(1, min(int(limit), 100))),
            ).fetchall()
            records: list[OutboxRecord] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE mutation_outbox SET status='delivering', attempt_count=attempt_count+1,
                      next_attempt_at=?, error_code=NULL WHERE event_id=?
                    """,
                    (lease_until, row["event_id"]),
                )
                records.append(
                    OutboxRecord(
                        event_id=row["event_id"],
                        workspace_id=row["workspace_id"],
                        ontology_id=row["ontology_id"],
                        mutation_revision=int(row["mutation_revision"]),
                        payload_json=row["payload_json"],
                        payload_hash=row["payload_hash"],
                        attempt_count=int(row["attempt_count"]) + 1,
                    )
                )
            connection.commit()
            return records

    def mark_dispatched(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE mutation_outbox SET status='dispatched', next_attempt_at=NULL,
                  error_code=NULL, dispatched_at=COALESCE(dispatched_at, ?)
                WHERE event_id=? AND status='delivering'
                """,
                (_utc_now(), event_id),
            )
            connection.commit()

    def mark_failed(self, event_id: str, *, error_code: str, retry: bool, delay_seconds: int = 5) -> None:
        status = "pending" if retry else "dead_letter"
        next_attempt_at = (
            (datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))).isoformat()
            if retry
            else None
        )
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE mutation_outbox SET status=?, next_attempt_at=?, error_code=?
                WHERE event_id=? AND status='delivering'
                """,
                (status, next_attempt_at, str(error_code)[:64], event_id),
            )
            connection.commit()

    def list_outbox(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.row_factory = sqlite3.Row
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM mutation_outbox ORDER BY created_at, event_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM mutation_outbox WHERE status=? ORDER BY created_at, event_id",
                    (status,),
                ).fetchall()
            return [dict(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection


class OntologyMutationCoordinator:
    """Serialize one workspace/ontology mutation through revision + outbox."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root.resolve()
        self.store = MutationStore(self.runtime_root / "ontology-mutations.sqlite3")

    def commit(self, mutation: PreparedOntologyMutation) -> MutationResult:
        lock_key = hashlib.sha256(
            "{}\x1f{}".format(mutation.workspace_id, mutation.ontology_id).encode("utf-8")
        ).hexdigest()
        lock_path = self.runtime_root / "ontology-mutation-locks" / f"{lock_key}.lock"
        with exclusive_file_lock(lock_path):
            return self.store.commit_prepared(
                mutation,
                pointer_revoker=lambda revision: revoke_active_pointer_snapshot(
                    self.runtime_root,
                    workspace_id=mutation.workspace_id,
                    ontology_id=mutation.ontology_id,
                    mutation_revision=revision,
                    ontology_version=mutation.after_version,
                    enabled=mutation.after_enabled,
                ),
            )


class MutationOutboxDispatcher:
    """Small at-least-once dispatcher with durable retries and payload checks."""

    def __init__(self, store: MutationStore, *, max_attempts: int = 8) -> None:
        self.store = store
        self.max_attempts = max(1, int(max_attempts))

    def dispatch_once(self, handler: Callable[[dict[str, Any]], None], *, limit: int = 16) -> dict[str, int]:
        result = {"claimed": 0, "dispatched": 0, "retried": 0, "dead_letter": 0}
        for record in self.store.claim_pending(limit=limit):
            result["claimed"] += 1
            try:
                actual_hash = hashlib.sha256(record.payload_json.encode("utf-8")).hexdigest()
                if actual_hash != record.payload_hash:
                    raise OntologyMutationError("OUTBOX_PAYLOAD_CORRUPT", "outbox payload hash mismatch")
                event = load_event_json(record.payload_json)
                event_route = (
                    event["event_id"],
                    event["workspace_id"],
                    event["ontology_id"],
                    int(event["mutation_revision"]),
                )
                record_route = (
                    record.event_id,
                    record.workspace_id,
                    record.ontology_id,
                    record.mutation_revision,
                )
                if event_route != record_route:
                    raise OntologyMutationError("OUTBOX_ROUTE_MISMATCH", "outbox routing metadata mismatch")
                handler(event)
            except (OntologyChangedValidationError, OntologyMutationError) as exc:
                self.store.mark_failed(record.event_id, error_code=exc.code, retry=False)
                result["dead_letter"] += 1
            except Exception:
                retry = record.attempt_count < self.max_attempts
                self.store.mark_failed(
                    record.event_id,
                    error_code="DELIVERY_FAILED",
                    retry=retry,
                    delay_seconds=min(300, 2 ** min(record.attempt_count, 8)),
                )
                result["retried" if retry else "dead_letter"] += 1
            else:
                self.store.mark_dispatched(record.event_id)
                result["dispatched"] += 1
        return result


def read_mutation_state_snapshot(
    runtime_root: Path,
    *,
    workspace_id: str,
    ontology_id: str,
) -> dict[str, Any] | None:
    """Read mutation state without creating SQLite files on disabled paths."""
    path = runtime_root.resolve() / "ontology-mutations.sqlite3"
    if not path.is_file():
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM mutation_state WHERE workspace_id=? AND ontology_id=?",
            (workspace_id, ontology_id),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mutation_state (
  workspace_id TEXT NOT NULL,
  ontology_id TEXT NOT NULL,
  active_version TEXT,
  enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
  mutation_revision INTEGER NOT NULL CHECK(mutation_revision >= 0),
  last_event_id TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(workspace_id, ontology_id)
);
CREATE INDEX IF NOT EXISTS idx_mutation_state_updated_at ON mutation_state(updated_at);

CREATE TABLE IF NOT EXISTS mutation_outbox (
  event_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  ontology_id TEXT NOT NULL,
  mutation_revision INTEGER NOT NULL,
  contract TEXT NOT NULL CHECK(contract='OntologyChanged/v1'),
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending', 'delivering', 'dispatched', 'dead_letter')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  next_attempt_at TEXT,
  error_code TEXT,
  created_at TEXT NOT NULL,
  dispatched_at TEXT,
  UNIQUE(workspace_id, ontology_id, mutation_revision)
);
CREATE INDEX IF NOT EXISTS idx_mutation_outbox_dispatch
  ON mutation_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_mutation_outbox_created_at
  ON mutation_outbox(created_at);
"""
