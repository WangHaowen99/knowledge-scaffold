"""Durable state for solver jobs, attempts, subscriptions, and outbox events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from .canonical import canonical_json_bytes, sha256_digest


TERMINAL_JOB_STATUSES = frozenset({"completed", "timed_out", "failed", "cancelled"})
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"completed", "timed_out", "failed", "cancelled", "attempt_lost"}
)
RETRYABLE_SOLVER_ERROR_CODES = frozenset(
    {"solver_unavailable", "connector_capacity", "attempt_lost"}
)
_UNSET = object()


class JobStoreError(ValueError):
    """Base error for invalid or inaccessible durable solver state."""


class JobStoreConflict(JobStoreError):
    """Raised when an immutable durable identity is reused inconsistently."""


class JobNotFound(JobStoreError):
    pass


class OptimisticLockError(JobStoreConflict):
    pass


class LeaseConflict(JobStoreConflict):
    pass


class TerminalStateError(JobStoreConflict):
    pass


class AttemptPayloadConflict(JobStoreConflict):
    pass


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise JobStoreError("{} is required".format(name))
    normalized = value.strip()
    if not normalized:
        raise JobStoreError("{} is required".format(name))
    if len(normalized) > 512:
        raise JobStoreError("{} is too long".format(name))
    return normalized


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _json_text(value: Any, name: str) -> str:
    try:
        return canonical_json_bytes(value).decode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JobStoreError("{} must be canonical JSON data".format(name)) from exc


def _json_value(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise JobStoreError("{} must be a timezone-aware datetime".format(name))
    return value.astimezone(timezone.utc)


def _deadline_text(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return _as_utc(value, "deadline_at").isoformat()
    text = _required_text(value, "deadline_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobStoreError("deadline_at must be an ISO timestamp") from exc
    return _as_utc(parsed, "deadline_at").isoformat()


class SolverJobStore:
    """SQLite/WAL source of truth for durable solver coordination.

    Every public lookup requires both workspace and owner scope. Mutations use
    short ``BEGIN IMMEDIATE`` transactions so event sequencing, idempotent
    writes, and optimistic versions remain correct across processes.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or _utc_now
        self._initialize()

    def _now(self) -> datetime:
        return _as_utc(self._clock(), "clock value")

    def _now_text(self) -> str:
        return self._now().isoformat()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=30.0,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS solver_binding_revisions (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    binding_revision_id TEXT NOT NULL,
                    configuration_json TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, binding_revision_id)
                );

                CREATE TABLE IF NOT EXISTS solve_jobs (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    logical_job_key TEXT NOT NULL,
                    binding_revision_id TEXT NOT NULL,
                    problem_digest TEXT NOT NULL,
                    problem_json TEXT NOT NULL,
                    run_policy_json TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until TEXT,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    terminal_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, job_id),
                    UNIQUE(workspace_id, owner_id, logical_job_key),
                    FOREIGN KEY(workspace_id, owner_id, binding_revision_id)
                        REFERENCES solver_binding_revisions(
                            workspace_id, owner_id, binding_revision_id
                        )
                );
                CREATE INDEX IF NOT EXISTS idx_solve_jobs_recovery
                    ON solve_jobs(workspace_id, owner_id, status, updated_at);

                CREATE TABLE IF NOT EXISTS solve_attempts (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    backend_idempotency_key TEXT NOT NULL,
                    backend_job_id TEXT,
                    status TEXT NOT NULL,
                    result_digest TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    terminal_at TEXT,
                    PRIMARY KEY(workspace_id, owner_id, attempt_id),
                    UNIQUE(workspace_id, owner_id, job_id, attempt_number),
                    UNIQUE(workspace_id, owner_id, job_id, attempt_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_solve_attempts_job
                    ON solve_attempts(workspace_id, owner_id, job_id, attempt_number);

                CREATE TABLE IF NOT EXISTS job_subscriptions (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    cancelled_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, subscription_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id),
                    FOREIGN KEY(workspace_id, owner_id, attempt_id)
                        REFERENCES solve_attempts(workspace_id, owner_id, attempt_id)
                );
                CREATE INDEX IF NOT EXISTS idx_job_subscriptions_active
                    ON job_subscriptions(workspace_id, owner_id, job_id, active);

                CREATE TABLE IF NOT EXISTS solve_artifacts (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT,
                    artifact_type TEXT NOT NULL,
                    artifact_digest TEXT NOT NULL,
                    artifact_ref TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, artifact_id),
                    UNIQUE(workspace_id, owner_id, job_id, artifact_type, artifact_digest),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id, attempt_id)
                        REFERENCES solve_attempts(
                            workspace_id, owner_id, job_id, attempt_id
                        )
                );

                CREATE TABLE IF NOT EXISTS verification_results (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    verification_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT,
                    status TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, verification_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id, attempt_id)
                        REFERENCES solve_attempts(
                            workspace_id, owner_id, job_id, attempt_id
                        )
                );

                CREATE TABLE IF NOT EXISTS run_solver_links (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    card_name TEXT NOT NULL,
                    contract_digest TEXT NOT NULL,
                    last_event_seq INTEGER NOT NULL DEFAULT 0,
                    tool_result_digest TEXT,
                    tool_result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(workspace_id, owner_id, run_id, turn_id, tool_call_id),
                    UNIQUE(workspace_id, owner_id, subscription_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id),
                    FOREIGN KEY(workspace_id, owner_id, attempt_id)
                        REFERENCES solve_attempts(workspace_id, owner_id, attempt_id),
                    FOREIGN KEY(workspace_id, owner_id, subscription_id)
                        REFERENCES job_subscriptions(workspace_id, owner_id, subscription_id)
                );

                CREATE TABLE IF NOT EXISTS outbox_events (
                    workspace_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    event_seq INTEGER NOT NULL,
                    attempt_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_at TEXT,
                    PRIMARY KEY(workspace_id, owner_id, job_id, event_seq),
                    FOREIGN KEY(workspace_id, owner_id, job_id)
                        REFERENCES solve_jobs(workspace_id, owner_id, job_id),
                    FOREIGN KEY(workspace_id, owner_id, job_id, attempt_id)
                        REFERENCES solve_attempts(
                            workspace_id, owner_id, job_id, attempt_id
                        )
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
                    ON outbox_events(workspace_id, owner_id, published_at, created_at);
                """
            )

    @staticmethod
    def derive_logical_job_key(
        *,
        workspace_id: str,
        binding_revision_id: str,
        problem_digest: str,
        run_policy: Mapping[str, Any],
        deadline_at: datetime | str,
        run_id: str | None = None,
    ) -> str:
        _deadline_text(deadline_at)
        identity = {
            "workspace_id": _required_text(workspace_id, "workspace_id"),
            "binding_revision_id": _required_text(
                binding_revision_id, "binding_revision_id"
            ),
            "problem_digest": _required_text(problem_digest, "problem_digest"),
            "run_policy": dict(run_policy),
        }
        # The optional argument keeps direct pre-federation JobStore callers
        # readable during migration.  Every federated orchestrator call passes
        # the immutable run identity, so equivalent requests in different runs
        # cannot be coalesced by the database unique key.
        if run_id is not None:
            identity["run_id"] = _required_text(run_id, "run_id")
        return sha256_digest(identity)

    @staticmethod
    def _decode(row: sqlite3.Row | None, kind: str) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        json_fields = {
            "binding": (("configuration_json", "configuration"),),
            "job": (("problem_json", "problem"), ("run_policy_json", "run_policy")),
            "attempt": (("request_json", "request_payload"), ("result_json", "result")),
            "verification": (("result_json", "result"),),
            "link": (("tool_result_json", "tool_result"),),
            "event": (("payload_json", "payload"),),
        }
        for source, target in json_fields.get(kind, ()):
            result[target] = _json_value(result.pop(source))
        return result

    @staticmethod
    def _scope(workspace_id: str, owner_id: str) -> tuple[str, str]:
        return (
            _required_text(workspace_id, "workspace_id"),
            _required_text(owner_id, "owner_id"),
        )

    def register_binding_revision(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str,
        configuration: Mapping[str, Any],
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        revision = _required_text(binding_revision_id, "binding_revision_id")
        configuration_json = _json_text(dict(configuration), "configuration")
        digest = sha256_digest(dict(configuration))
        now = self._now_text()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM solver_binding_revisions
                WHERE workspace_id = ? AND owner_id = ? AND binding_revision_id = ?
                """,
                (workspace, owner, revision),
            ).fetchone()
            if existing is not None:
                current = self._decode(existing, "binding") or {}
                if current["configuration_digest"] != digest:
                    raise JobStoreConflict(
                        "binding revision is already bound to different configuration"
                    )
                return current
            connection.execute(
                """
                INSERT INTO solver_binding_revisions (
                    workspace_id, owner_id, binding_revision_id,
                    configuration_json, configuration_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (workspace, owner, revision, configuration_json, digest, now),
            )
            row = connection.execute(
                """
                SELECT * FROM solver_binding_revisions
                WHERE workspace_id = ? AND owner_id = ? AND binding_revision_id = ?
                """,
                (workspace, owner, revision),
            ).fetchone()
        return self._decode(row, "binding") or {}

    def register_connector_binding_revision(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str,
        configuration: Mapping[str, Any],
        endpoint_policy: Any | None = None,
    ) -> dict[str, Any]:
        """Register a strict Consumer Connector binding for new solver runs.

        The lower-level persistence primitive remains available for internal
        fixtures. Production selection must use this method so arbitrary
        backend mappings cannot start new runs.
        """

        from .bindings import SolverBindingRevision

        binding = SolverBindingRevision.from_configuration(
            revision_id=binding_revision_id,
            workspace_id=workspace_id,
            configuration=configuration,
            endpoint_policy=endpoint_policy,
        )
        return self.register_binding_revision(
            workspace_id=workspace_id,
            owner_id=owner_id,
            binding_revision_id=binding.revision_id,
            configuration=binding.to_configuration(),
        )

    def get_binding_revision(
        self,
        binding_revision_id: str,
        *,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        workspace, owner = self._scope(workspace_id, owner_id)
        revision = _required_text(binding_revision_id, "binding_revision_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM solver_binding_revisions
                WHERE workspace_id = ? AND owner_id = ? AND binding_revision_id = ?
                """,
                (workspace, owner, revision),
            ).fetchone()
        return self._decode(row, "binding")

    def list_binding_revisions(
        self,
        *,
        workspace_id: str,
        owner_id: str,
    ) -> list[dict[str, Any]]:
        """List immutable binding revisions in one administrator scope.

        Resolution is deliberately performed by the binding resolver rather
        than by SQL.  Returning the complete, deterministically ordered set
        keeps compatibility checks (family/version and enabled state) in one
        place and prevents a database ordering from becoming an implicit
        solver preference.
        """

        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM solver_binding_revisions
                WHERE workspace_id = ? AND owner_id = ?
                ORDER BY binding_revision_id ASC
                """,
                (workspace, owner),
            ).fetchall()
        decoded_rows = []
        for row in rows:
            decoded = self._decode(row, "binding")
            if decoded is not None:
                decoded_rows.append(decoded)
        return decoded_rows

    def create_or_get_job(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str,
        problem_digest: str,
        problem: Mapping[str, Any],
        run_policy: Mapping[str, Any],
        deadline_at: datetime | str,
        run_id: str | None = None,
        job_id: str | None = None,
        initial_lease_owner: str | None = None,
        initial_lease_seconds: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        workspace, owner = self._scope(workspace_id, owner_id)
        revision = _required_text(binding_revision_id, "binding_revision_id")
        digest = _required_text(problem_digest, "problem_digest")
        problem_payload = dict(problem)
        if problem_payload.get("problem_id") not in (None, digest):
            raise JobStoreConflict("problem digest does not match problem payload")
        policy_payload = dict(run_policy)
        problem_json = _json_text(problem_payload, "problem")
        policy_json = _json_text(policy_payload, "run_policy")
        deadline = _deadline_text(deadline_at)
        logical_key = self.derive_logical_job_key(
            workspace_id=workspace,
            binding_revision_id=revision,
            problem_digest=digest,
            run_policy=policy_payload,
            deadline_at=deadline,
            run_id=run_id,
        )
        selected_job_id = _required_text(
            job_id or "job_{}".format(uuid.uuid4().hex), "job_id"
        )
        now_value = self._now()
        initial_owner = _optional_text(initial_lease_owner, "initial_lease_owner")
        if initial_owner is None:
            if initial_lease_seconds is not None:
                raise JobStoreError(
                    "initial_lease_seconds requires initial_lease_owner"
                )
            initial_until = None
            initial_phase = "created"
            initial_fencing_token = 0
        else:
            if (
                isinstance(initial_lease_seconds, bool)
                or not isinstance(initial_lease_seconds, int)
                or initial_lease_seconds < 1
            ):
                raise JobStoreError(
                    "initial_lease_seconds must be a positive integer"
                )
            initial_until = (
                now_value + timedelta(seconds=initial_lease_seconds)
            ).isoformat()
            initial_phase = "freeze_inputs"
            initial_fencing_token = 1
        now = now_value.isoformat()
        with self._transaction() as connection:
            binding = connection.execute(
                """
                SELECT 1 FROM solver_binding_revisions
                WHERE workspace_id = ? AND owner_id = ? AND binding_revision_id = ?
                """,
                (workspace, owner, revision),
            ).fetchone()
            if binding is None:
                raise JobStoreConflict("solver binding revision is not registered")
            existing = connection.execute(
                """
                SELECT * FROM solve_jobs
                WHERE workspace_id = ? AND owner_id = ? AND logical_job_key = ?
                """,
                (workspace, owner, logical_key),
            ).fetchone()
            if existing is not None:
                current = self._decode(existing, "job") or {}
                immutable = (
                    current["binding_revision_id"],
                    current["problem_digest"],
                    _json_text(current["problem"], "problem"),
                    _json_text(current["run_policy"], "run_policy"),
                )
                requested = (revision, digest, problem_json, policy_json)
                if immutable != requested:
                    raise JobStoreConflict(
                        "logical job key is already bound to different content"
                    )
                return current, False
            collision = connection.execute(
                """
                SELECT 1 FROM solve_jobs
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, selected_job_id),
            ).fetchone()
            if collision is not None:
                raise JobStoreConflict("job_id is already in use")
            connection.execute(
                """
                INSERT INTO solve_jobs (
                    workspace_id, owner_id, job_id, logical_job_key,
                    binding_revision_id, problem_digest, problem_json,
                    run_policy_json, deadline_at, phase, status, error_code,
                    version, lease_owner, lease_until, fencing_token,
                    terminal_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', NULL,
                          0, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    workspace,
                    owner,
                    selected_job_id,
                    logical_key,
                    revision,
                    digest,
                    problem_json,
                    policy_json,
                    deadline,
                    initial_phase,
                    initial_owner,
                    initial_until,
                    initial_fencing_token,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM solve_jobs
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, selected_job_id),
            ).fetchone()
        return self._decode(row, "job") or {}, True

    def complete_input_preparation(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        lease_owner: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        """Publish the durable marker that all frozen input artifacts exist."""

        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        worker = _required_text(lease_owner, "lease_owner")
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
            raise JobStoreError("fencing_token must be an integer")
        with self._transaction() as connection:
            row = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            if row["lease_owner"] != worker or row["fencing_token"] != fencing_token:
                raise LeaseConflict("solver input preparation lease is stale")
            lease_until = row["lease_until"]
            if lease_until is None or datetime.fromisoformat(lease_until) <= self._now():
                raise LeaseConflict("solver input preparation lease has expired")
            if row["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs cannot finish input preparation")
            if row["phase"] != "freeze_inputs":
                raise JobStoreConflict("solver job is not freezing inputs")
            artifact_rows = connection.execute(
                """
                SELECT artifact_type, COUNT(*) AS artifact_count
                FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                  AND artifact_type IN ('normalized_input', 'fact_snapshot', 'problem_ir')
                GROUP BY artifact_type
                """,
                (workspace, owner, identifier),
            ).fetchall()
            artifact_counts = {
                str(item["artifact_type"]): int(item["artifact_count"])
                for item in artifact_rows
            }
            if any(
                artifact_counts.get(kind) != 1
                for kind in ("normalized_input", "fact_snapshot", "problem_ir")
            ):
                raise JobStoreConflict("solver frozen input artifacts are incomplete")
            connection.execute(
                """
                UPDATE solve_jobs
                SET phase = 'created', lease_owner = NULL, lease_until = NULL,
                    fencing_token = fencing_token + 1,
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (self._now_text(), workspace, owner, identifier),
            )
            updated = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
        return self._decode(updated, "job") or {}

    def get_job(
        self, job_id: str, *, workspace_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM solve_jobs
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "job")

    @staticmethod
    def _history_cursor(created_at: str, job_id: str) -> str:
        payload = canonical_json_bytes(
            {"created_at": created_at, "job_id": job_id}
        )
        return base64.urlsafe_b64encode(payload).decode("ascii")

    @staticmethod
    def _parse_history_cursor(cursor: str) -> tuple[str, str]:
        value = _required_text(cursor, "cursor")
        try:
            payload = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        except ValueError as exc:
            raise JobStoreError("history cursor is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {"created_at", "job_id"}:
            raise JobStoreError("history cursor is invalid")
        return (
            _required_text(payload["created_at"], "cursor.created_at"),
            _required_text(payload["job_id"], "cursor.job_id"),
        )

    def list_jobs(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise JobStoreError("limit must be an integer from 1 to 100")
        clauses = ["workspace_id = ?", "owner_id = ?"]
        values: list[Any] = [workspace, owner]
        if cursor is not None:
            created_at, job_id = self._parse_history_cursor(cursor)
            clauses.append("(created_at < ? OR (created_at = ? AND job_id < ?))")
            values.extend((created_at, created_at, job_id))
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT workspace_id, owner_id, job_id, logical_job_key,
                       binding_revision_id, problem_digest, deadline_at,
                       phase, status, error_code, version, terminal_at,
                       created_at, updated_at
                FROM solve_jobs
                WHERE {}
                ORDER BY created_at DESC, job_id DESC
                LIMIT ?
                """.format(" AND ".join(clauses)),
                values,
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [dict(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            next_cursor = self._history_cursor(
                selected[-1]["created_at"], selected[-1]["job_id"]
            )
        return {"items": items, "next_cursor": next_cursor}

    def _job_for_update(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM solve_jobs
            WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
            """,
            (workspace_id, owner_id, job_id),
        ).fetchone()
        if row is None:
            raise JobNotFound("solver job was not found")
        return row

    @staticmethod
    def _assert_attempt_lineage(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
    ) -> None:
        if attempt_id is None:
            return
        row = connection.execute(
            """
            SELECT 1 FROM solve_attempts
            WHERE workspace_id = ? AND owner_id = ?
              AND job_id = ? AND attempt_id = ?
            """,
            (workspace_id, owner_id, job_id, attempt_id),
        ).fetchone()
        if row is None:
            raise JobStoreConflict("attempt does not belong to solver job")

    def _append_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
        event_type: str,
        payload_json: str,
        now: str,
    ) -> sqlite3.Row:
        self._assert_attempt_lineage(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job_id,
            attempt_id=attempt_id,
        )
        event_seq = connection.execute(
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1 FROM outbox_events
            WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
            """,
            (workspace_id, owner_id, job_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO outbox_events (
                workspace_id, owner_id, job_id, event_seq, attempt_id,
                event_type, payload_json, created_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                workspace_id,
                owner_id,
                job_id,
                event_seq,
                attempt_id,
                event_type,
                payload_json,
                now,
            ),
        )
        return connection.execute(
            """
            SELECT * FROM outbox_events
            WHERE workspace_id = ? AND owner_id = ? AND job_id = ? AND event_seq = ?
            """,
            (workspace_id, owner_id, job_id, event_seq),
        ).fetchone()

    def _update_job_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        expected_version: int,
        requested_status: str | None,
        requested_phase: str | None,
        requested_error: str | None | object,
        fencing_token: int | None,
    ) -> tuple[sqlite3.Row, bool]:
        row = self._job_for_update(
            connection,
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job_id,
        )
        if row["version"] != expected_version:
            raise OptimisticLockError("solver job version is stale")
        if fencing_token is not None and row["fencing_token"] != fencing_token:
            raise LeaseConflict("solver job fencing token is stale")
        if fencing_token is not None:
            lease_until = row["lease_until"]
            lease_active = (
                row["lease_owner"] is not None
                and lease_until is not None
                and datetime.fromisoformat(lease_until) > self._now()
            )
            cancellation_completion = (
                row["status"] == "cancelling"
                and requested_status == "cancelled"
                and requested_phase == "cancel"
            )
            if not lease_active and not cancellation_completion:
                raise LeaseConflict("solver job lease has expired")
        unchanged = (
            (requested_status is None or requested_status == row["status"])
            and (requested_phase is None or requested_phase == row["phase"])
            and (requested_error is _UNSET or requested_error == row["error_code"])
        )
        if row["status"] in TERMINAL_JOB_STATUSES:
            if unchanged:
                return row, False
            raise TerminalStateError("terminal solver jobs are immutable")
        if unchanged:
            return row, False
        next_status = requested_status or row["status"]
        next_phase = requested_phase or row["phase"]
        next_error = row["error_code"] if requested_error is _UNSET else requested_error
        now = self._now_text()
        terminal_at = now if next_status in TERMINAL_JOB_STATUSES else None
        connection.execute(
            """
            UPDATE solve_jobs
            SET status = ?, phase = ?, error_code = ?, version = version + 1,
                terminal_at = ?, updated_at = ?
            WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
            """,
            (
                next_status,
                next_phase,
                next_error,
                terminal_at,
                now,
                workspace_id,
                owner_id,
                job_id,
            ),
        )
        return (
            self._job_for_update(
                connection,
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
            ),
            True,
        )

    def update_job(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        expected_version: int,
        status: str | None = None,
        phase: str | None = None,
        error_code: str | None | object = _UNSET,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise JobStoreError("expected_version must be an integer")
        requested_status = _required_text(status, "status") if status is not None else None
        requested_phase = _required_text(phase, "phase") if phase is not None else None
        requested_error = (
            _optional_text(error_code, "error_code") if error_code is not _UNSET else _UNSET
        )
        with self._transaction() as connection:
            updated, _ = self._update_job_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                expected_version=expected_version,
                requested_status=requested_status,
                requested_phase=requested_phase,
                requested_error=requested_error,
                fencing_token=fencing_token,
            )
        return self._decode(updated, "job") or {}

    def transition_job_with_event(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        expected_version: int,
        status: str,
        phase: str,
        event_type: str,
        event_payload: Mapping[str, Any],
        attempt_id: str | None = None,
        error_code: str | None = None,
        fencing_token: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        next_status = _required_text(status, "status")
        next_phase = _required_text(phase, "phase")
        kind = _required_text(event_type, "event_type")
        attempt = _optional_text(attempt_id, "attempt_id")
        payload_json = _json_text(dict(event_payload), "event payload")
        with self._transaction() as connection:
            updated, changed = self._update_job_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                expected_version=expected_version,
                requested_status=next_status,
                requested_phase=next_phase,
                requested_error=_optional_text(error_code, "error_code"),
                fencing_token=fencing_token,
            )
            if not changed:
                raise JobStoreConflict("job transition was already applied")
            event = self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                attempt_id=attempt,
                event_type=kind,
                payload_json=payload_json,
                now=updated["updated_at"],
            )
        return self._decode(updated, "job") or {}, self._decode(event, "event") or {}

    def complete_job_with_authoritative_events(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        expected_version: int,
        fencing_token: int,
        attempt_id: str,
        verification_payload: Mapping[str, Any],
        authoritative_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Atomically publish the only authoritative completion for a Job."""

        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        attempt = _required_text(attempt_id, "attempt_id")
        verification_json = _json_text(
            dict(verification_payload), "verification event payload"
        )
        authoritative_json = _json_text(
            dict(authoritative_payload), "authoritative event payload"
        )
        with self._transaction() as connection:
            updated, changed = self._update_job_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                expected_version=expected_version,
                requested_status="completed",
                requested_phase="publish",
                requested_error=None,
                fencing_token=fencing_token,
            )
            if not changed:
                raise JobStoreConflict("authoritative completion was already applied")
            verification_event = self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                attempt_id=attempt,
                event_type="solver.verification.completed",
                payload_json=verification_json,
                now=updated["updated_at"],
            )
            authoritative_event = self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                attempt_id=attempt,
                event_type="solver.result.authoritative",
                payload_json=authoritative_json,
                now=updated["updated_at"],
            )
            connection.execute(
                """
                UPDATE solve_jobs
                SET lease_owner = NULL, lease_until = NULL
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, identifier),
            )
            updated = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
        return (
            self._decode(updated, "job") or {},
            self._decode(verification_event, "event") or {},
            self._decode(authoritative_event, "event") or {},
        )

    def fail_job_and_attempt_with_event(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        expected_version: int,
        fencing_token: int,
        lease_owner: str,
        phase: str,
        error_code: str,
        event_type: str,
        event_payload: Mapping[str, Any],
        attempt_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
        """Atomically terminalize recovery state while the caller still owns its fence."""

        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        worker = _required_text(lease_owner, "lease_owner")
        next_phase = _required_text(phase, "phase")
        error = _required_text(error_code, "error_code")
        kind = _required_text(event_type, "event_type")
        attempt_identifier = _optional_text(attempt_id, "attempt_id")
        payload_json = _json_text(dict(event_payload), "event payload")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise JobStoreError("expected_version must be an integer")
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
            raise JobStoreError("fencing_token must be an integer")

        with self._transaction() as connection:
            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            if job["version"] != expected_version:
                raise OptimisticLockError("solver job version is stale")
            if (
                job["fencing_token"] != fencing_token
                or job["lease_owner"] != worker
            ):
                raise LeaseConflict("solver recovery lease or fencing token is stale")
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs are immutable")

            attempt_row: sqlite3.Row | None = None
            if attempt_identifier is not None:
                attempt_row = connection.execute(
                    """
                    SELECT * FROM solve_attempts
                    WHERE workspace_id = ? AND owner_id = ?
                      AND job_id = ? AND attempt_id = ?
                    """,
                    (workspace, owner, identifier, attempt_identifier),
                ).fetchone()
                if attempt_row is None:
                    raise JobStoreConflict("attempt does not belong to solver job")
                if attempt_row["status"] not in TERMINAL_ATTEMPT_STATUSES:
                    now = self._now_text()
                    connection.execute(
                        """
                        UPDATE solve_attempts
                        SET status = 'failed', error_code = ?, terminal_at = ?,
                            version = version + 1, updated_at = ?
                        WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                        """,
                        (
                            error,
                            now,
                            now,
                            workspace,
                            owner,
                            attempt_identifier,
                        ),
                    )

            updated, changed = self._update_job_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                expected_version=expected_version,
                requested_status="failed",
                requested_phase=next_phase,
                requested_error=error,
                fencing_token=fencing_token,
            )
            if not changed:
                raise JobStoreConflict("job failure transition was already applied")
            event = self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                attempt_id=attempt_identifier,
                event_type=kind,
                payload_json=payload_json,
                now=updated["updated_at"],
            )
            if attempt_identifier is not None:
                attempt_row = connection.execute(
                    """
                    SELECT * FROM solve_attempts
                    WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                    """,
                    (workspace, owner, attempt_identifier),
                ).fetchone()

        return (
            self._decode(updated, "job") or {},
            self._decode(attempt_row, "attempt"),
            self._decode(event, "event") or {},
        )

    def claim_job(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        lease_owner: str,
        lease_seconds: int,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        worker = _required_text(lease_owner, "lease_owner")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise JobStoreError("lease_seconds must be a positive integer")
        with self._transaction() as connection:
            row = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            if row["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs cannot be claimed")
            if expected_version is not None and row["version"] != expected_version:
                raise OptimisticLockError("solver job version is stale")
            now = self._now()
            lease_until = row["lease_until"]
            if lease_until is not None:
                current_until = datetime.fromisoformat(lease_until)
                if current_until > now and row["lease_owner"] != worker:
                    raise LeaseConflict("solver job already has an active lease")
            next_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE solve_jobs
                SET lease_owner = ?, lease_until = ?,
                    fencing_token = fencing_token + 1,
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (worker, next_until, now.isoformat(), workspace, owner, identifier),
            )
            updated = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
        return self._decode(updated, "job") or {}

    def renew_job_lease(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        lease_owner: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> dict[str, Any]:
        """Extend an existing lease without changing its fence or version."""

        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        worker = _required_text(lease_owner, "lease_owner")
        if isinstance(fencing_token, bool) or not isinstance(fencing_token, int):
            raise JobStoreError("fencing_token must be an integer")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise JobStoreError("lease_seconds must be a positive integer")
        with self._transaction() as connection:
            row = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            if row["lease_owner"] != worker or row["fencing_token"] != fencing_token:
                raise LeaseConflict("solver verification lease is stale")
            if row["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs cannot renew a lease")
            now = self._now()
            current_until = row["lease_until"]
            if (
                current_until is None
                or datetime.fromisoformat(current_until) <= now
            ):
                raise LeaseConflict("solver verification lease has expired")
            lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
            connection.execute(
                """
                UPDATE solve_jobs
                SET lease_until = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (lease_until, now.isoformat(), workspace, owner, identifier),
            )
            updated = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
        return self._decode(updated, "job") or {}

    def release_job_lease(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        lease_owner: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        worker = _required_text(lease_owner, "lease_owner")
        with self._transaction() as connection:
            row = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            if row["lease_owner"] != worker or row["fencing_token"] != fencing_token:
                raise LeaseConflict("solver job lease is stale")
            connection.execute(
                """
                UPDATE solve_jobs
                SET lease_owner = NULL, lease_until = NULL,
                    fencing_token = fencing_token + 1,
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (self._now_text(), workspace, owner, identifier),
            )
            updated = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
        return self._decode(updated, "job") or {}

    def create_or_replay_attempt(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        attempt_id: str,
        request_payload: Mapping[str, Any],
        backend_idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        workspace, owner = self._scope(workspace_id, owner_id)
        job_identifier = _required_text(job_id, "job_id")
        identifier = _required_text(attempt_id, "attempt_id")
        idempotency_key = _required_text(
            backend_idempotency_key, "backend_idempotency_key"
        )
        request = dict(request_payload)
        request_json = _json_text(request, "request_payload")
        request_digest = sha256_digest(request)
        now = self._now_text()
        with self._transaction() as connection:
            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
            )
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs cannot create attempts")
            existing = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if existing is not None:
                current = self._decode(existing, "attempt") or {}
                if (
                    current["job_id"] != job_identifier
                    or current["request_digest"] != request_digest
                    or current["backend_idempotency_key"] != idempotency_key
                ):
                    raise AttemptPayloadConflict(
                        "attempt_id is already bound to a different request"
                    )
                return current, False
            attempt_number = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, job_identifier),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO solve_attempts (
                    workspace_id, owner_id, attempt_id, job_id, attempt_number,
                    request_digest, request_json, backend_idempotency_key,
                    backend_job_id, status, result_digest, result_json,
                    error_code, version, created_at, updated_at, terminal_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'created', NULL, NULL,
                          NULL, 0, ?, ?, NULL)
                """,
                (
                    workspace,
                    owner,
                    identifier,
                    job_identifier,
                    attempt_number,
                    request_digest,
                    request_json,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "attempt") or {}, True

    def create_or_get_initial_attempt(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        attempt_id: str,
        request_payload: Mapping[str, Any],
        backend_idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create the first attempt or join the one already created."""

        workspace, owner = self._scope(workspace_id, owner_id)
        job_identifier = _required_text(job_id, "job_id")
        identifier = _required_text(attempt_id, "attempt_id")
        idempotency_key = _required_text(
            backend_idempotency_key, "backend_idempotency_key"
        )
        request = dict(request_payload)
        request_json = _json_text(request, "request_payload")
        request_digest = sha256_digest(request)
        now = self._now_text()
        with self._transaction() as connection:
            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
            )
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs cannot create attempts")
            existing = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                ORDER BY attempt_number ASC LIMIT 1
                """,
                (workspace, owner, job_identifier),
            ).fetchone()
            if existing is not None:
                current = self._decode(existing, "attempt") or {}
                if current["request_digest"] != request_digest:
                    raise AttemptPayloadConflict(
                        "logical job already has an initial attempt for different content"
                    )
                return current, False
            collision = connection.execute(
                """
                SELECT 1 FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if collision is not None:
                raise AttemptPayloadConflict("attempt_id is already in use")
            connection.execute(
                """
                INSERT INTO solve_attempts (
                    workspace_id, owner_id, attempt_id, job_id, attempt_number,
                    request_digest, request_json, backend_idempotency_key,
                    backend_job_id, status, result_digest, result_json,
                    error_code, version, created_at, updated_at, terminal_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, NULL, 'created', NULL, NULL,
                          NULL, 0, ?, ?, NULL)
                """,
                (
                    workspace,
                    owner,
                    identifier,
                    job_identifier,
                    request_digest,
                    request_json,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "attempt") or {}, True

    def prepare_retry_attempt(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        attempt_id: str,
        request_payload: Mapping[str, Any],
        backend_idempotency_key: str,
    ) -> tuple[dict[str, Any], bool, str | None]:
        """Reserve one policy-approved retry and supersede any stale active attempt."""

        workspace, owner = self._scope(workspace_id, owner_id)
        job_identifier = _required_text(job_id, "job_id")
        identifier = _required_text(attempt_id, "attempt_id")
        idempotency_key = _required_text(
            backend_idempotency_key, "backend_idempotency_key"
        )
        request = dict(request_payload)
        request_json = _json_text(request, "request_payload")
        request_digest = sha256_digest(request)
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if existing is not None:
                current = self._decode(existing, "attempt") or {}
                if current["job_id"] != job_identifier:
                    raise AttemptPayloadConflict(
                        "retry attempt_id is already bound to different content"
                    )
                if current["attempt_number"] < 2:
                    raise AttemptPayloadConflict(
                        "retry attempt_id is already bound to different content"
                    )
                if current["request_digest"] != request_digest:
                    raise AttemptPayloadConflict(
                        "retry attempt_id is already bound to different content"
                    )
                if current["backend_idempotency_key"] != idempotency_key:
                    raise AttemptPayloadConflict(
                        "retry attempt_id is already bound to different content"
                    )
                return current, False, None

            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
            )
            if job["status"] not in {"failed", "timed_out"}:
                raise JobStoreConflict("only terminal solver failures may be retried")
            retryable = job["error_code"] in RETRYABLE_SOLVER_ERROR_CODES
            if job["status"] == "timed_out" and job["error_code"] == "timed_out_no_solution":
                retryable = bool(_json_value(job["run_policy_json"]).get("retry_on_timeout"))
            if not retryable:
                raise JobStoreConflict("solver job error is not retryable")
            run_policy = _json_value(job["run_policy_json"])
            max_attempts = run_policy.get("max_attempts", 1)
            if (
                isinstance(max_attempts, bool)
                or not isinstance(max_attempts, int)
                or max_attempts < 1
            ):
                raise JobStoreConflict("solver retry policy max_attempts is invalid")
            attempts = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                ORDER BY attempt_number ASC
                """,
                (workspace, owner, job_identifier),
            ).fetchall()
            if len(attempts) >= max_attempts:
                raise JobStoreConflict("solver retry policy reached max_attempts")
            superseded_backend_job_id: str | None = None
            if attempts:
                latest = attempts[-1]
                if latest["status"] not in TERMINAL_ATTEMPT_STATUSES:
                    superseded_backend_job_id = latest["backend_job_id"]
                    now = self._now_text()
                    connection.execute(
                        """
                        UPDATE solve_attempts
                        SET status = 'failed', error_code = 'superseded_by_retry',
                            terminal_at = ?, version = version + 1, updated_at = ?
                        WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                        """,
                        (now, now, workspace, owner, latest["attempt_id"]),
                    )
            attempt_number = len(attempts) + 1
            now = self._now_text()
            connection.execute(
                """
                INSERT INTO solve_attempts (
                    workspace_id, owner_id, attempt_id, job_id, attempt_number,
                    request_digest, request_json, backend_idempotency_key,
                    backend_job_id, status, result_digest, result_json,
                    error_code, version, created_at, updated_at, terminal_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'created', NULL, NULL,
                          NULL, 0, ?, ?, NULL)
                """,
                (
                    workspace,
                    owner,
                    identifier,
                    job_identifier,
                    attempt_number,
                    request_digest,
                    request_json,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE job_subscriptions
                SET attempt_id = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ?
                  AND job_id = ? AND active = 1
                """,
                (identifier, now, workspace, owner, job_identifier),
            )
            connection.execute(
                """
                UPDATE run_solver_links
                SET attempt_id = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                  AND tool_result_digest IS NULL
                  AND subscription_id IN (
                      SELECT subscription_id FROM job_subscriptions
                      WHERE workspace_id = ? AND owner_id = ?
                        AND job_id = ? AND active = 1
                  )
                """,
                (
                    identifier,
                    now,
                    workspace,
                    owner,
                    job_identifier,
                    workspace,
                    owner,
                    job_identifier,
                ),
            )
            connection.execute(
                """
                UPDATE solve_jobs
                SET status = 'created', phase = 'retry', error_code = NULL,
                    terminal_at = NULL, lease_owner = NULL, lease_until = NULL,
                    fencing_token = fencing_token + 1,
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (now, workspace, owner, job_identifier),
            )
            self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
                attempt_id=identifier,
                event_type="solver.job.retrying",
                payload_json=_json_text(
                    {
                        "attempt_number": attempt_number,
                        "phase": "retry",
                        "status": "created",
                    },
                    "retry event",
                ),
                now=now,
            )
            row = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "attempt") or {}, True, superseded_backend_job_id

    def get_attempt(
        self, attempt_id: str, *, workspace_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(attempt_id, "attempt_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "attempt")

    def list_attempts(
        self, job_id: str, *, workspace_id: str, owner_id: str
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                ORDER BY attempt_number ASC
                """,
                (workspace, owner, identifier),
            ).fetchall()
        return tuple(self._decode(row, "attempt") or {} for row in rows)

    def latest_attempt(
        self, job_id: str, *, workspace_id: str, owner_id: str
    ) -> dict[str, Any] | None:
        attempts = self.list_attempts(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        return attempts[-1] if attempts else None

    def bind_backend_job(
        self,
        attempt_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        backend_job_id: str,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(attempt_id, "attempt_id")
        backend_identifier = _required_text(backend_job_id, "backend_job_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, j.status AS job_status,
                       j.fencing_token AS job_fencing_token,
                       j.lease_owner AS job_lease_owner,
                       j.lease_until AS job_lease_until
                FROM solve_attempts AS a
                JOIN solve_jobs AS j
                  ON j.workspace_id = a.workspace_id AND j.owner_id = a.owner_id
                 AND j.job_id = a.job_id
                WHERE a.workspace_id = ? AND a.owner_id = ? AND a.attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver attempt was not found")
            if row["job_status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs reject backend submissions")
            if row["status"] in TERMINAL_ATTEMPT_STATUSES:
                raise TerminalStateError("terminal solver attempts reject backend binding")
            if fencing_token is not None and row["job_fencing_token"] != fencing_token:
                raise LeaseConflict("solver backend binding fencing token is stale")
            if fencing_token is not None:
                if row["job_lease_owner"] is None or row["job_lease_until"] is None:
                    raise LeaseConflict("solver backend binding lease has expired")
                if datetime.fromisoformat(row["job_lease_until"]) <= self._now():
                    raise LeaseConflict("solver backend binding lease has expired")
            if row["job_lease_owner"] is not None and fencing_token is None:
                raise LeaseConflict("solver backend binding requires the active fencing token")
            if row["backend_job_id"] is not None:
                if row["backend_job_id"] != backend_identifier:
                    raise JobStoreConflict(
                        "attempt is already bound to a different backend job"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM solve_attempts
                    WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                    """,
                    (workspace, owner, identifier),
                ).fetchone()
                return self._decode(existing, "attempt") or {}
            connection.execute(
                """
                UPDATE solve_attempts
                SET backend_job_id = ?, status = 'submitted',
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (backend_identifier, self._now_text(), workspace, owner, identifier),
            )
            updated = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(updated, "attempt") or {}

    def update_attempt_status(
        self,
        attempt_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        status: str,
        error_code: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(attempt_id, "attempt_id")
        next_status = _required_text(status, "status")
        error = _optional_text(error_code, "error_code")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, j.status AS job_status,
                       j.fencing_token AS job_fencing_token,
                       j.lease_owner AS job_lease_owner,
                       j.lease_until AS job_lease_until
                FROM solve_attempts AS a
                JOIN solve_jobs AS j
                  ON j.workspace_id = a.workspace_id AND j.owner_id = a.owner_id
                 AND j.job_id = a.job_id
                WHERE a.workspace_id = ? AND a.owner_id = ? AND a.attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver attempt was not found")
            if row["job_status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs reject attempt updates")
            if fencing_token is not None and row["job_fencing_token"] != fencing_token:
                raise LeaseConflict("solver attempt fencing token is stale")
            if fencing_token is not None:
                lease_active = (
                    row["job_lease_owner"] is not None
                    and row["job_lease_until"] is not None
                    and datetime.fromisoformat(row["job_lease_until"]) > self._now()
                )
                cancellation_completion = (
                    row["job_status"] == "cancelling" and next_status == "cancelled"
                )
                if not lease_active and not cancellation_completion:
                    raise LeaseConflict("solver attempt lease has expired")
            if row["status"] in TERMINAL_ATTEMPT_STATUSES:
                if row["status"] == next_status and row["error_code"] == error:
                    existing = connection.execute(
                        """
                        SELECT * FROM solve_attempts
                        WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                        """,
                        (workspace, owner, identifier),
                    ).fetchone()
                    return self._decode(existing, "attempt") or {}
                raise TerminalStateError("terminal solver attempts are immutable")
            now = self._now_text()
            terminal_at = now if next_status in TERMINAL_ATTEMPT_STATUSES else None
            connection.execute(
                """
                UPDATE solve_attempts
                SET status = ?, error_code = ?, terminal_at = ?,
                    version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (next_status, error, terminal_at, now, workspace, owner, identifier),
            )
            updated = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(updated, "attempt") or {}

    def record_attempt_result(
        self,
        attempt_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        result: Mapping[str, Any],
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(attempt_id, "attempt_id")
        payload = dict(result)
        result_json = _json_text(payload, "result")
        result_digest = sha256_digest(payload)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT a.*, j.status AS job_status,
                       j.fencing_token AS job_fencing_token,
                       j.lease_owner AS job_lease_owner,
                       j.lease_until AS job_lease_until
                FROM solve_attempts AS a
                JOIN solve_jobs AS j
                  ON j.workspace_id = a.workspace_id AND j.owner_id = a.owner_id
                 AND j.job_id = a.job_id
                WHERE a.workspace_id = ? AND a.owner_id = ? AND a.attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver attempt was not found")
            if row["job_status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("late backend results cannot change a terminal job")
            if fencing_token is not None and row["job_fencing_token"] != fencing_token:
                raise LeaseConflict("solver attempt fencing token is stale")
            if fencing_token is not None:
                if row["job_lease_owner"] is None or row["job_lease_until"] is None:
                    raise LeaseConflict("solver attempt lease has expired")
                if datetime.fromisoformat(row["job_lease_until"]) <= self._now():
                    raise LeaseConflict("solver attempt lease has expired")
            if row["status"] in TERMINAL_ATTEMPT_STATUSES:
                if row["status"] == "completed" and row["result_digest"] == result_digest:
                    existing = connection.execute(
                        """
                        SELECT * FROM solve_attempts
                        WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                        """,
                        (workspace, owner, identifier),
                    ).fetchone()
                    return self._decode(existing, "attempt") or {}
                raise TerminalStateError("terminal solver attempts reject late results")
            if row["result_digest"] is not None:
                if row["result_digest"] != result_digest:
                    raise JobStoreConflict(
                        "attempt result is already bound to different content"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM solve_attempts
                    WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                    """,
                    (workspace, owner, identifier),
                ).fetchone()
                return self._decode(existing, "attempt") or {}
            now = self._now_text()
            connection.execute(
                """
                UPDATE solve_attempts
                SET status = 'completed', result_digest = ?, result_json = ?,
                    terminal_at = ?, version = version + 1, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (result_digest, result_json, now, now, workspace, owner, identifier),
            )
            updated = connection.execute(
                """
                SELECT * FROM solve_attempts
                WHERE workspace_id = ? AND owner_id = ? AND attempt_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(updated, "attempt") or {}

    def create_subscription_and_link(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        job_id: str,
        attempt_id: str,
        subscription_id: str,
        card_name: str,
        contract_digest: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        workspace, owner = self._scope(workspace_id, owner_id)
        run = _required_text(run_id, "run_id")
        turn = _required_text(turn_id, "turn_id")
        tool_call = _required_text(tool_call_id, "tool_call_id")
        job_identifier = _required_text(job_id, "job_id")
        attempt_identifier = _required_text(attempt_id, "attempt_id")
        subscription_identifier = _required_text(subscription_id, "subscription_id")
        card = _required_text(card_name, "card_name")
        contract = _required_text(contract_digest, "contract_digest")
        now = self._now_text()
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM run_solver_links
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
            if existing is not None:
                link = self._decode(existing, "link") or {}
                immutable = (
                    link["job_id"],
                    link["attempt_id"],
                    link["card_name"],
                    link["contract_digest"],
                )
                if immutable != (job_identifier, attempt_identifier, card, contract):
                    raise JobStoreConflict(
                        "tool call is already linked to a different solver contract"
                    )
                subscription = connection.execute(
                    """
                    SELECT * FROM job_subscriptions
                    WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                    """,
                    (workspace, owner, link["subscription_id"]),
                ).fetchone()
                return link, dict(subscription), False
            attempt = connection.execute(
                """
                SELECT attempt.attempt_id, job.status AS job_status
                FROM solve_attempts AS attempt
                JOIN solve_jobs AS job
                  ON job.workspace_id = attempt.workspace_id
                 AND job.owner_id = attempt.owner_id
                 AND job.job_id = attempt.job_id
                WHERE attempt.workspace_id = ? AND attempt.owner_id = ?
                  AND attempt.attempt_id = ? AND attempt.job_id = ?
                """,
                (workspace, owner, attempt_identifier, job_identifier),
            ).fetchone()
            if attempt is None:
                raise JobStoreConflict("attempt does not belong to solver job")
            if attempt["job_status"] == "cancelling":
                raise JobStoreConflict("solver job no longer accepts subscriptions")
            subscription_collision = connection.execute(
                """
                SELECT 1 FROM job_subscriptions
                WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                """,
                (workspace, owner, subscription_identifier),
            ).fetchone()
            if subscription_collision is not None:
                raise JobStoreConflict("subscription_id is already in use")
            connection.execute(
                """
                INSERT INTO job_subscriptions (
                    workspace_id, owner_id, subscription_id, job_id, attempt_id,
                    run_id, turn_id, tool_call_id, active, cancelled_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, ?)
                """,
                (
                    workspace,
                    owner,
                    subscription_identifier,
                    job_identifier,
                    attempt_identifier,
                    run,
                    turn,
                    tool_call,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_solver_links (
                    workspace_id, owner_id, run_id, turn_id, tool_call_id,
                    job_id, attempt_id, subscription_id, card_name,
                    contract_digest, last_event_seq, tool_result_digest,
                    tool_result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
                """,
                (
                    workspace,
                    owner,
                    run,
                    turn,
                    tool_call,
                    job_identifier,
                    attempt_identifier,
                    subscription_identifier,
                    card,
                    contract,
                    now,
                    now,
                ),
            )
            link_row = connection.execute(
                """
                SELECT link.*,
                       subscription.active AS subscription_active,
                       subscription.cancelled_at AS subscription_cancelled_at
                FROM run_solver_links AS link
                JOIN job_subscriptions AS subscription
                  ON subscription.workspace_id = link.workspace_id
                 AND subscription.owner_id = link.owner_id
                 AND subscription.subscription_id = link.subscription_id
                WHERE link.workspace_id = ? AND link.owner_id = ?
                  AND link.run_id = ? AND link.turn_id = ? AND link.tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
            subscription_row = connection.execute(
                """
                SELECT * FROM job_subscriptions
                WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                """,
                (workspace, owner, subscription_identifier),
            ).fetchone()
        return (
            self._decode(link_row, "link") or {},
            dict(subscription_row),
            True,
        )

    def get_run_link(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
    ) -> dict[str, Any] | None:
        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT link.*,
                       subscription.active AS subscription_active,
                       subscription.cancelled_at AS subscription_cancelled_at
                FROM run_solver_links AS link
                JOIN job_subscriptions AS subscription
                  ON subscription.workspace_id = link.workspace_id
                 AND subscription.owner_id = link.owner_id
                 AND subscription.subscription_id = link.subscription_id
                WHERE link.workspace_id = ? AND link.owner_id = ?
                  AND link.run_id = ? AND link.turn_id = ? AND link.tool_call_id = ?
                """,
                (
                    workspace,
                    owner,
                    _required_text(run_id, "run_id"),
                    _required_text(turn_id, "turn_id"),
                    _required_text(tool_call_id, "tool_call_id"),
                ),
            ).fetchone()
        return self._decode(row, "link")

    def list_job_run_links(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT link.*,
                       subscription.active AS subscription_active,
                       subscription.cancelled_at AS subscription_cancelled_at
                FROM run_solver_links AS link
                JOIN job_subscriptions AS subscription
                  ON subscription.workspace_id = link.workspace_id
                 AND subscription.owner_id = link.owner_id
                 AND subscription.subscription_id = link.subscription_id
                WHERE link.workspace_id = ? AND link.owner_id = ?
                  AND link.job_id = ?
                ORDER BY
                  CASE WHEN link.tool_result_json IS NULL THEN 0 ELSE 1 END ASC,
                  subscription.active DESC,
                  link.created_at ASC,
                  link.run_id ASC,
                  link.turn_id ASC,
                  link.tool_call_id ASC
                """,
                (workspace, owner, _required_text(job_id, "job_id")),
            ).fetchall()
        return tuple(self._decode(row, "link") or {} for row in rows)

    def list_retryable_verification_links(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        card_name: str,
        contract_digest: str,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT link.*,
                       subscription.active AS subscription_active,
                       subscription.cancelled_at AS subscription_cancelled_at
                FROM run_solver_links AS link
                JOIN solve_jobs AS job
                  ON job.workspace_id = link.workspace_id
                 AND job.owner_id = link.owner_id
                 AND job.job_id = link.job_id
                JOIN job_subscriptions AS subscription
                  ON subscription.workspace_id = link.workspace_id
                 AND subscription.owner_id = link.owner_id
                 AND subscription.subscription_id = link.subscription_id
                WHERE link.workspace_id = ? AND link.owner_id = ?
                  AND link.run_id = ? AND link.card_name = ?
                  AND link.contract_digest = ?
                  AND link.tool_result_json IS NOT NULL
                  AND job.status = 'solver_completed'
                ORDER BY link.updated_at DESC, link.tool_call_id DESC
                """,
                (
                    workspace,
                    owner,
                    _required_text(run_id, "run_id"),
                    _required_text(card_name, "card_name"),
                    _required_text(contract_digest, "contract_digest"),
                ),
            ).fetchall()
        decoded = []
        for row in rows:
            link = self._decode(row, "link") or {}
            result = link.get("tool_result")
            if not isinstance(result, Mapping):
                continue
            if result.get("status") != "error":
                continue
            if result.get("code") not in {
                "verification_runtime_failed",
                "artifact_persist_failed",
            }:
                continue
            if result.get("retryable") is not True:
                continue
            if result.get("authoritative") is not False:
                continue
            decoded.append(link)
        return tuple(decoded)

    def advance_run_link_event_seq(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        event_seq: int,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        run = _required_text(run_id, "run_id")
        turn = _required_text(turn_id, "turn_id")
        tool_call = _required_text(tool_call_id, "tool_call_id")
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < 0:
            raise JobStoreError("event_seq must be a non-negative integer")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM run_solver_links
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver run link was not found")
            if event_seq < row["last_event_seq"]:
                raise JobStoreConflict("run link event sequence cannot move backwards")
            if event_seq == row["last_event_seq"]:
                return self._decode(row, "link") or {}
            latest_event_seq = connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) FROM outbox_events
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, row["job_id"]),
            ).fetchone()[0]
            if event_seq > latest_event_seq:
                raise JobStoreConflict("run link cannot advance past persisted events")
            connection.execute(
                """
                UPDATE run_solver_links
                SET last_event_seq = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (
                    event_seq,
                    self._now_text(),
                    workspace,
                    owner,
                    run,
                    turn,
                    tool_call,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM run_solver_links
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
        return self._decode(updated, "link") or {}

    def cancel_subscription(
        self,
        subscription_id: str,
        *,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(subscription_id, "subscription_id")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM job_subscriptions
                WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver subscription was not found")
            was_active = bool(row["active"])
            if was_active:
                now = self._now_text()
                connection.execute(
                    """
                    UPDATE job_subscriptions
                    SET active = 0, cancelled_at = ?, updated_at = ?
                    WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                    """,
                    (now, now, workspace, owner, identifier),
                )
            active_count = connection.execute(
                """
                SELECT COUNT(*) FROM job_subscriptions
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ? AND active = 1
                """,
                (workspace, owner, row["job_id"]),
            ).fetchone()[0]
            cancel_reserved = False
            reserved_job: sqlite3.Row | None = None
            if was_active and active_count == 0:
                job = self._job_for_update(
                    connection,
                    workspace_id=workspace,
                    owner_id=owner,
                    job_id=row["job_id"],
                )
                if (
                    job["status"] not in TERMINAL_JOB_STATUSES
                    and job["status"] != "cancelling"
                ):
                    now = self._now_text()
                    submit_lease_active = (
                        job["phase"] == "submit_job"
                        and job["lease_owner"] is not None
                        and job["lease_until"] is not None
                        and datetime.fromisoformat(job["lease_until"]) > self._now()
                    )
                    if submit_lease_active:
                        # Let an in-flight submit finish and observe cancellation with
                        # the same fence; replaying here would duplicate the backend call.
                        connection.execute(
                            """
                            UPDATE solve_jobs
                            SET status = 'cancelling', version = version + 1,
                                updated_at = ?
                            WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                            """,
                            (now, workspace, owner, row["job_id"]),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE solve_jobs
                            SET status = 'cancelling',
                                version = version + 1, fencing_token = fencing_token + 1,
                                lease_owner = NULL, lease_until = NULL, updated_at = ?
                            WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                            """,
                            (now, workspace, owner, row["job_id"]),
                        )
                    reserved_job = self._job_for_update(
                        connection,
                        workspace_id=workspace,
                        owner_id=owner,
                        job_id=row["job_id"],
                    )
                    self._append_event_in_transaction(
                        connection,
                        workspace_id=workspace,
                        owner_id=owner,
                        job_id=row["job_id"],
                        attempt_id=row["attempt_id"],
                        event_type="solver.cancellation.requested",
                        payload_json=_json_text(
                            {
                                "phase": "cancel",
                                "status": "cancelling",
                                "submission_in_flight": submit_lease_active,
                            },
                            "cancellation event",
                        ),
                        now=now,
                    )
                    cancel_reserved = True
            updated = connection.execute(
                """
                SELECT * FROM job_subscriptions
                WHERE workspace_id = ? AND owner_id = ? AND subscription_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        result = dict(updated)
        result["active_subscriber_count"] = active_count
        result["last_subscriber"] = bool(was_active and active_count == 0)
        result["cancel_reserved"] = cancel_reserved
        if reserved_job is not None:
            result["job_version"] = reserved_job["version"]
            result["fencing_token"] = reserved_job["fencing_token"]
        return result

    def append_event(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        kind = _required_text(event_type, "event_type")
        attempt = _optional_text(attempt_id, "attempt_id")
        payload_json = _json_text(dict(payload), "event payload")
        with self._transaction() as connection:
            self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
            )
            row = self._append_event_in_transaction(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=identifier,
                attempt_id=attempt,
                event_type=kind,
                payload_json=payload_json,
                now=self._now_text(),
            )
        return self._decode(row, "event") or {}

    def read_events(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        after_event_seq: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        if isinstance(after_event_seq, bool) or not isinstance(after_event_seq, int):
            raise JobStoreError("after_event_seq must be an integer")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                  AND event_seq > ?
                ORDER BY event_seq ASC
                """,
                (workspace, owner, identifier, after_event_seq),
            ).fetchall()
        return tuple(self._decode(row, "event") or {} for row in rows)

    def list_unpublished_events(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise JobStoreError("limit must be an integer from 1 to 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE workspace_id = ? AND owner_id = ? AND published_at IS NULL
                ORDER BY created_at ASC, job_id ASC, event_seq ASC
                LIMIT ?
                """,
                (workspace, owner, limit),
            ).fetchall()
        return tuple(self._decode(row, "event") or {} for row in rows)

    def mark_event_published(
        self,
        job_id: str,
        *,
        event_seq: int,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        identifier = _required_text(job_id, "job_id")
        if isinstance(event_seq, bool) or not isinstance(event_seq, int) or event_seq < 1:
            raise JobStoreError("event_seq must be a positive integer")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM outbox_events
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ? AND event_seq = ?
                """,
                (workspace, owner, identifier, event_seq),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver outbox event was not found")
            if row["published_at"] is None:
                connection.execute(
                    """
                    UPDATE outbox_events SET published_at = ?
                    WHERE workspace_id = ? AND owner_id = ?
                      AND job_id = ? AND event_seq = ? AND published_at IS NULL
                    """,
                    (self._now_text(), workspace, owner, identifier, event_seq),
                )
                row = connection.execute(
                    """
                    SELECT * FROM outbox_events
                    WHERE workspace_id = ? AND owner_id = ?
                      AND job_id = ? AND event_seq = ?
                    """,
                    (workspace, owner, identifier, event_seq),
                ).fetchone()
        return self._decode(row, "event") or {}

    def write_tool_result(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        result: Mapping[str, Any],
        fencing_token: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        workspace, owner = self._scope(workspace_id, owner_id)
        run = _required_text(run_id, "run_id")
        turn = _required_text(turn_id, "turn_id")
        tool_call = _required_text(tool_call_id, "tool_call_id")
        contract = _required_text(contract_digest, "contract_digest")
        result_payload = dict(result)
        result_json = _json_text(result_payload, "tool result")
        result_digest = sha256_digest(result_payload)
        if fencing_token is not None and (
            isinstance(fencing_token, bool) or not isinstance(fencing_token, int)
        ):
            raise JobStoreError("fencing_token must be an integer")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT link.*, job.status AS job_status,
                       job.fencing_token AS job_fencing_token,
                       subscription.active AS subscription_active
                FROM run_solver_links AS link
                JOIN solve_jobs AS job
                  ON job.workspace_id = link.workspace_id
                 AND job.owner_id = link.owner_id
                 AND job.job_id = link.job_id
                JOIN job_subscriptions AS subscription
                  ON subscription.workspace_id = link.workspace_id
                 AND subscription.owner_id = link.owner_id
                 AND subscription.subscription_id = link.subscription_id
                WHERE link.workspace_id = ? AND link.owner_id = ?
                  AND link.run_id = ? AND link.turn_id = ? AND link.tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
            if row is None:
                raise JobNotFound("solver run link was not found")
            if fencing_token is not None and row["job_fencing_token"] != fencing_token:
                raise LeaseConflict("solver tool result fence is stale")
            if row["contract_digest"] != contract:
                raise JobStoreConflict("solver contract drifted before tool result write")
            subscriber_cancelled = (
                not bool(row["subscription_active"])
                and result_payload.get("status") == "cancelled"
                and result_payload.get("code") == "cancelled"
                and result_payload.get("phase") == "cancel"
                and result_payload.get("retryable") is False
                and result_payload.get("authoritative") is False
            )
            if not bool(row["subscription_active"]) and not subscriber_cancelled:
                raise JobStoreConflict(
                    "cancelled solver subscriptions cannot receive a business result"
                )
            retryable_verification_failure = (
                row["job_status"] == "solver_completed"
                and result_payload.get("status") == "error"
                and result_payload.get("code")
                in {"verification_runtime_failed", "artifact_persist_failed"}
                and result_payload.get("phase") in {"verify", "publish"}
                and result_payload.get("retryable") is True
                and result_payload.get("authoritative") is False
            )
            if (
                row["job_status"] not in TERMINAL_JOB_STATUSES
                and not subscriber_cancelled
                and not retryable_verification_failure
            ):
                raise TerminalStateError(
                    "tool result cannot be written before the solver job is terminal"
                )
            if row["tool_result_digest"] is not None:
                if row["tool_result_digest"] != result_digest:
                    raise JobStoreConflict(
                        "tool result is already bound to different content"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM run_solver_links
                    WHERE workspace_id = ? AND owner_id = ?
                      AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                    """,
                    (workspace, owner, run, turn, tool_call),
                ).fetchone()
                return self._decode(existing, "link") or {}, False
            event_seq = connection.execute(
                """
                SELECT COALESCE(MAX(event_seq), 0) + 1 FROM outbox_events
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                """,
                (workspace, owner, row["job_id"]),
            ).fetchone()[0]
            now = self._now_text()
            event_payload = {
                "run_id": run,
                "tool_call_id": tool_call,
                "tool_result_digest": result_digest,
            }
            connection.execute(
                """
                INSERT INTO outbox_events (
                    workspace_id, owner_id, job_id, event_seq, attempt_id,
                    event_type, payload_json, created_at, published_at
                ) VALUES (?, ?, ?, ?, ?, 'solver.tool_result.ready', ?, ?, NULL)
                """,
                (
                    workspace,
                    owner,
                    row["job_id"],
                    event_seq,
                    row["attempt_id"],
                    _json_text(event_payload, "tool result event"),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE run_solver_links
                SET tool_result_digest = ?, tool_result_json = ?,
                    last_event_seq = ?, updated_at = ?
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (
                    result_digest,
                    result_json,
                    event_seq,
                    now,
                    workspace,
                    owner,
                    run,
                    turn,
                    tool_call,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM run_solver_links
                WHERE workspace_id = ? AND owner_id = ?
                  AND run_id = ? AND turn_id = ? AND tool_call_id = ?
                """,
                (workspace, owner, run, turn, tool_call),
            ).fetchone()
        return self._decode(updated, "link") or {}, True

    def add_artifact(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
        artifact_type: str,
        artifact_digest: str,
        artifact_ref: str,
        artifact_id: str | None = None,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        job_identifier = _required_text(job_id, "job_id")
        attempt = _optional_text(attempt_id, "attempt_id")
        kind = _required_text(artifact_type, "artifact_type")
        digest = _required_text(artifact_digest, "artifact_digest")
        ref = _required_text(artifact_ref, "artifact_ref")
        worker = _optional_text(lease_owner, "lease_owner")
        if (worker is None) != (fencing_token is None):
            raise JobStoreError("lease_owner and fencing_token must be provided together")
        if fencing_token is not None and (
            isinstance(fencing_token, bool) or not isinstance(fencing_token, int)
        ):
            raise JobStoreError("fencing_token must be an integer")
        identifier = _required_text(
            artifact_id or "artifact_{}".format(uuid.uuid4().hex), "artifact_id"
        )
        with self._transaction() as connection:
            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
            )
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError("terminal solver jobs reject artifact writes")
            if worker is None and job["lease_owner"] is not None:
                raise LeaseConflict(
                    "solver artifact write requires the active fencing token"
                )
            if worker is not None:
                if job["lease_owner"] != worker or job["fencing_token"] != fencing_token:
                    raise LeaseConflict("solver artifact lease or fencing token is stale")
                lease_until = job["lease_until"]
                if lease_until is None or datetime.fromisoformat(lease_until) <= self._now():
                    raise LeaseConflict("solver artifact lease has expired")
            self._assert_attempt_lineage(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
                attempt_id=attempt,
            )
            existing = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                  AND artifact_type = ? AND artifact_digest = ?
                """,
                (workspace, owner, job_identifier, kind, digest),
            ).fetchone()
            if existing is not None:
                if existing["artifact_ref"] != ref or existing["attempt_id"] != attempt:
                    raise JobStoreConflict(
                        "artifact digest is already bound to different metadata"
                    )
                return dict(existing)
            connection.execute(
                """
                INSERT INTO solve_artifacts (
                    workspace_id, owner_id, artifact_id, job_id, attempt_id,
                    artifact_type, artifact_digest, artifact_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace,
                    owner,
                    identifier,
                    job_identifier,
                    attempt,
                    kind,
                    digest,
                    ref,
                    self._now_text(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND artifact_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return dict(row)

    def list_artifacts(
        self, job_id: str, *, workspace_id: str, owner_id: str
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM solve_artifacts
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                (workspace, owner, _required_text(job_id, "job_id")),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def add_verification_result(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
        verification_id: str,
        status: str,
        result: Mapping[str, Any],
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        workspace, owner = self._scope(workspace_id, owner_id)
        job_identifier = _required_text(job_id, "job_id")
        attempt = _optional_text(attempt_id, "attempt_id")
        identifier = _required_text(verification_id, "verification_id")
        result_payload = dict(result)
        result_json = _json_text(result_payload, "verification result")
        result_digest = sha256_digest(result_payload)
        worker = _optional_text(lease_owner, "lease_owner")
        if (worker is None) != (fencing_token is None):
            raise JobStoreError("lease_owner and fencing_token must be provided together")
        if fencing_token is not None and (
            isinstance(fencing_token, bool) or not isinstance(fencing_token, int)
        ):
            raise JobStoreError("fencing_token must be an integer")
        with self._transaction() as connection:
            job = self._job_for_update(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
            )
            if job["status"] in TERMINAL_JOB_STATUSES:
                raise TerminalStateError(
                    "terminal solver jobs reject verification writes"
                )
            if worker is None and job["lease_owner"] is not None:
                raise LeaseConflict(
                    "solver verification write requires the active fencing token"
                )
            if worker is not None:
                lease_until = job["lease_until"]
                if (
                    job["lease_owner"] != worker
                    or job["fencing_token"] != fencing_token
                ):
                    raise LeaseConflict("solver verification lease is stale")
                if lease_until is None:
                    raise LeaseConflict("solver verification lease is stale")
                if datetime.fromisoformat(lease_until) <= self._now():
                    raise LeaseConflict("solver verification lease is stale")
            self._assert_attempt_lineage(
                connection,
                workspace_id=workspace,
                owner_id=owner,
                job_id=job_identifier,
                attempt_id=attempt,
            )
            existing = connection.execute(
                """
                SELECT * FROM verification_results
                WHERE workspace_id = ? AND owner_id = ? AND verification_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
            if existing is not None:
                if (
                    existing["job_id"] != job_identifier
                    or existing["attempt_id"] != attempt
                    or existing["result_digest"] != result_digest
                ):
                    raise JobStoreConflict(
                        "verification identity is already bound to different content"
                    )
                return self._decode(existing, "verification") or {}
            connection.execute(
                """
                INSERT INTO verification_results (
                    workspace_id, owner_id, verification_id, job_id, attempt_id,
                    status, result_digest, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace,
                    owner,
                    identifier,
                    job_identifier,
                    attempt,
                    _required_text(status, "status"),
                    result_digest,
                    result_json,
                    self._now_text(),
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM verification_results
                WHERE workspace_id = ? AND owner_id = ? AND verification_id = ?
                """,
                (workspace, owner, identifier),
            ).fetchone()
        return self._decode(row, "verification") or {}

    def list_verification_results(
        self, job_id: str, *, workspace_id: str, owner_id: str
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM verification_results
                WHERE workspace_id = ? AND owner_id = ? AND job_id = ?
                ORDER BY created_at ASC, verification_id ASC
                """,
                (workspace, owner, _required_text(job_id, "job_id")),
            ).fetchall()
        return tuple(self._decode(row, "verification") or {} for row in rows)

    def list_recoverable_jobs(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        binding_clause = ""
        binding_values: tuple[Any, ...] = ()
        if binding_revision_id is not None:
            binding_revision = _required_text(
                binding_revision_id, "binding_revision_id"
            )
            binding_clause = "AND binding_revision_id = ?"
            binding_values = (binding_revision,)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM solve_jobs
                WHERE workspace_id = ? AND owner_id = ?
                  {}
                  AND status NOT IN ({})
                ORDER BY created_at ASC, job_id ASC
                """.format(binding_clause, placeholders),
                (
                    workspace,
                    owner,
                    *binding_values,
                    *sorted(TERMINAL_JOB_STATUSES),
                ),
            ).fetchall()
        return tuple(self._decode(row, "job") or {} for row in rows)

    def list_service_recovery_scopes(
        self,
        *,
        binding_revision_id: str | None = None,
    ) -> tuple[tuple[str, str], ...]:
        # Kept as an optional compatibility argument for older callers.  A
        # recovery scan must include stale bindings so the service can emit a
        # durable solver_contract_drift ToolResult instead of orphaning them.
        del binding_revision_id
        placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT job.workspace_id, job.owner_id
                FROM solve_jobs AS job
                WHERE (
                    job.status NOT IN ({})
                    OR EXISTS (
                        SELECT 1
                        FROM run_solver_links AS link
                        WHERE link.workspace_id = job.workspace_id
                          AND link.owner_id = job.owner_id
                          AND link.job_id = job.job_id
                          AND link.tool_result_json IS NULL
                    )
                  )
                ORDER BY job.workspace_id ASC, job.owner_id ASC
                """.format(placeholders),
                tuple(sorted(TERMINAL_JOB_STATUSES)),
            ).fetchall()
        return tuple((str(row["workspace_id"]), str(row["owner_id"])) for row in rows)

    def list_service_recovery_jobs(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        binding_revision_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        workspace, owner = self._scope(workspace_id, owner_id)
        del binding_revision_id
        placeholders = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job.*
                FROM solve_jobs AS job
                WHERE job.workspace_id = ? AND job.owner_id = ?
                  AND (
                    job.status NOT IN ({})
                    OR EXISTS (
                        SELECT 1
                        FROM run_solver_links AS link
                        WHERE link.workspace_id = job.workspace_id
                          AND link.owner_id = job.owner_id
                          AND link.job_id = job.job_id
                          AND link.tool_result_json IS NULL
                    )
                  )
                ORDER BY job.updated_at ASC, job.job_id ASC
                """.format(placeholders),
                (
                    workspace,
                    owner,
                    *sorted(TERMINAL_JOB_STATUSES),
                ),
            ).fetchall()
        return tuple(self._decode(row, "job") or {} for row in rows)
