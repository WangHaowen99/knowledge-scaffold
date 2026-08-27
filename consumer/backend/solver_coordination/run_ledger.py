"""Durable run and artifact lineage for solver coordination.

The coordination endpoints deliberately keep the candidate and validation
payloads as immutable JSON artifacts.  This small SQLite ledger is the
authoritative index for those artifacts: it records lifecycle state and the
lineage between a validation run and the solve run that produced its input.
SQLite is used from the standard library so the slice has no new runtime
dependency.  Each operation opens a short-lived connection, enables WAL, and
uses parameterized statements; a process restart therefore does not lose the
index or leave an in-memory lock behind.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.identifiers import validate_session_id
from workspace.workspaces import get_workspace_paths


class RunLedgerError(ValueError):
    """Raised when a run or artifact cannot be recorded safely."""


class RunLedgerConflict(RunLedgerError):
    """Raised when an immutable identity is reused with different content."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RunLedgerError("{} is required".format(name))
    normalized = value.strip()
    if not normalized:
        raise RunLedgerError("{} is required".format(name))
    if len(normalized) > 512:
        raise RunLedgerError("{} is too long".format(name))
    return normalized


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RunLedgerError("{} must be a string".format(name))
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 512:
        raise RunLedgerError("{} is too long".format(name))
    return normalized


def _safe_run_id(value: object) -> str:
    try:
        return validate_session_id(value)
    except ValueError as exc:
        raise RunLedgerError("run_id is invalid") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SolverRunLedger:
    """Workspace/owner-scoped SQLite WAL ledger.

    The owner is part of every lookup key.  A caller who knows another
    owner's run id or artifact digest receives ``None`` rather than an
    existence oracle.  The database file itself lives below the workspace
    runtime directory returned by the existing workspace path resolver.
    """

    def __init__(self, workspace_id: str, *, database_path: str | Path | None = None) -> None:
        self.workspace_id = _required_text(workspace_id, "workspace_id")
        root = get_workspace_paths(self.workspace_id).runtime_dir / "solver_coordination"
        root.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(database_path) if database_path is not None else root / "run_ledger.sqlite3"
        self.database_path = self.database_path.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        # WAL is set for every connection because SQLite stores this pragma
        # at database level and a database may be created by another process.
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS solve_runs (
                    run_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    run_type TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parent_run_id TEXT,
                    problem_digest TEXT,
                    candidate_set_digest TEXT,
                    candidate_digest TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_solve_runs_scope
                    ON solve_runs(workspace_id, owner_id, updated_at);
                CREATE TABLE IF NOT EXISTS solve_artifacts (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    artifact_ref TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    source_run_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, artifact_digest)
                );
                CREATE INDEX IF NOT EXISTS idx_solve_artifacts_scope
                    ON solve_artifacts(workspace_id, owner_id, artifact_ref);
                """
            )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_run(
        self,
        *,
        run_id: str,
        owner_id: str,
        run_type: str,
        phase: str,
        status: str,
        parent_run_id: str | None = None,
        problem_digest: str | None = None,
        candidate_set_digest: str | None = None,
        candidate_digest: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        safe_run_id = _safe_run_id(run_id)
        owner = _required_text(owner_id, "owner_id")
        run_kind = _required_text(run_type, "run_type")
        run_phase = _required_text(phase, "phase")
        run_status = _required_text(status, "status")
        parent = _optional_text(parent_run_id, "parent_run_id")
        problem = _optional_text(problem_digest, "problem_digest")
        candidate_set = _optional_text(candidate_set_digest, "candidate_set_digest")
        candidate = _optional_text(candidate_digest, "candidate_digest")
        error = _optional_text(error_code, "error_code")
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM solve_runs WHERE run_id = ? AND workspace_id = ? AND owner_id = ?",
                (safe_run_id, self.workspace_id, owner),
            ).fetchone()
            if existing is not None:
                current = self._row_to_dict(existing) or {}
                immutable = (
                    current.get("run_type"),
                    current.get("parent_run_id"),
                    current.get("problem_digest"),
                    current.get("candidate_set_digest"),
                    current.get("candidate_digest"),
                )
                requested = (run_kind, parent, problem, candidate_set, candidate)
                if immutable != requested:
                    raise RunLedgerConflict("run_id is already bound to different lineage")
                return current
            # A collision belonging to another owner in this workspace is
            # intentionally reported as a conflict without exposing that
            # owner's row.  Other workspaces have independent namespaces.
            collision = connection.execute(
                "SELECT run_id FROM solve_runs WHERE run_id = ? AND workspace_id = ?",
                (safe_run_id, self.workspace_id),
            ).fetchone()
            if collision is not None:
                raise RunLedgerConflict("run_id is already bound to another owner")
            connection.execute(
                """
                INSERT INTO solve_runs (
                    run_id, workspace_id, owner_id, run_type, phase, status,
                    parent_run_id, problem_digest, candidate_set_digest,
                    candidate_digest, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_run_id,
                    self.workspace_id,
                    owner,
                    run_kind,
                    run_phase,
                    run_status,
                    parent,
                    problem,
                    candidate_set,
                    candidate,
                    error,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM solve_runs WHERE run_id = ?",
                (safe_run_id,),
            ).fetchone()
        return self._row_to_dict(row) or {}

    def update_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        phase: str | None = None,
        status: str | None = None,
        error_code: str | None = None,
        candidate_set_digest: str | None = None,
        candidate_digest: str | None = None,
    ) -> dict[str, Any] | None:
        safe_run_id = _safe_run_id(run_id)
        owner = _required_text(owner_id, "owner_id")
        updates: dict[str, str | None] = {}
        if phase is not None:
            updates["phase"] = _required_text(phase, "phase")
        if status is not None:
            updates["status"] = _required_text(status, "status")
        if error_code is not None:
            updates["error_code"] = _optional_text(error_code, "error_code")
        if candidate_set_digest is not None:
            updates["candidate_set_digest"] = _optional_text(candidate_set_digest, "candidate_set_digest")
        if candidate_digest is not None:
            updates["candidate_digest"] = _optional_text(candidate_digest, "candidate_digest")
        if not updates:
            return self.get_run(safe_run_id, owner_id=owner)
        assignments = ", ".join("{} = ?".format(key) for key in updates)
        values = list(updates.values()) + [_utc_now(), safe_run_id, self.workspace_id, owner]
        with self._connect() as connection:
            connection.execute(
                "UPDATE solve_runs SET {}, updated_at = ? WHERE run_id = ? AND workspace_id = ? AND owner_id = ?".format(
                    assignments
                ),
                values,
            )
            row = connection.execute(
                "SELECT * FROM solve_runs WHERE run_id = ? AND workspace_id = ? AND owner_id = ?",
                (safe_run_id, self.workspace_id, owner),
            ).fetchone()
        return self._row_to_dict(row)

    def get_run(self, run_id: str, *, owner_id: str) -> dict[str, Any] | None:
        safe_run_id = _safe_run_id(run_id)
        owner = _required_text(owner_id, "owner_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM solve_runs WHERE run_id = ? AND workspace_id = ? AND owner_id = ?",
                (safe_run_id, self.workspace_id, owner),
            ).fetchone()
        return self._row_to_dict(row)

    def register_artifact(
        self,
        *,
        artifact_digest: str,
        artifact_type: str,
        artifact_ref: str,
        artifact_path: str,
        owner_id: str,
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        digest = _required_text(artifact_digest, "artifact_digest")
        kind = _required_text(artifact_type, "artifact_type")
        ref = _required_text(artifact_ref, "artifact_ref")
        path = _required_text(artifact_path, "artifact_path")
        path_value = Path(path)
        if path_value.is_absolute() or ".." in path_value.parts:
            raise RunLedgerError("artifact_path must be workspace-relative")
        owner = _required_text(owner_id, "owner_id")
        source = _optional_text(source_run_id, "source_run_id")
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND artifact_digest = ?
                """,
                (self.workspace_id, owner, digest),
            ).fetchone()
            if existing is not None:
                current = self._row_to_dict(existing) or {}
                immutable = (current.get("artifact_type"), current.get("artifact_ref"), current.get("artifact_path"))
                if immutable != (kind, ref, path):
                    raise RunLedgerConflict("artifact digest is already bound to different content")
                return current
            connection.execute(
                """
                INSERT INTO solve_artifacts (
                    workspace_id, owner_id, artifact_digest, artifact_type,
                    artifact_ref, artifact_path, source_run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (self.workspace_id, owner, digest, kind, ref, path, source, now),
            )
            row = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND artifact_digest = ?
                """,
                (self.workspace_id, owner, digest),
            ).fetchone()
        return self._row_to_dict(row) or {}

    def get_artifact(self, artifact_digest: str, *, owner_id: str) -> dict[str, Any] | None:
        digest = _required_text(artifact_digest, "artifact_digest")
        owner = _required_text(owner_id, "owner_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND artifact_digest = ?
                """,
                (self.workspace_id, owner, digest),
            ).fetchone()
        return self._row_to_dict(row)

    def find_artifacts(
        self,
        *,
        owner_id: str,
        source_run_id: str,
        artifact_type: str,
    ) -> tuple[dict[str, Any], ...]:
        """List artifacts only within the complete workspace/owner lineage scope."""
        owner = _required_text(owner_id, "owner_id")
        source = _required_text(source_run_id, "source_run_id")
        kind = _required_text(artifact_type, "artifact_type")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ?
                  AND source_run_id = ? AND artifact_type = ?
                ORDER BY created_at, artifact_digest
                """,
                (self.workspace_id, owner, source, kind),
            ).fetchall()
        return tuple(self._row_to_dict(row) or {} for row in rows)
