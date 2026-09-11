from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from solver_coordination.job_store import (
    AttemptPayloadConflict,
    JobStoreConflict,
    LeaseConflict,
    OptimisticLockError,
    SolverJobStore,
    TerminalStateError,
)

WORKSPACE_ID = "workspace_synthetic"
OWNER_ID = "owner_synthetic"
BINDING_REVISION = "binding_revision_synthetic_1"
PROBLEM_DIGEST = "sha256:solver-coordination.v1:" + "a" * 64


def _store(tmp_path) -> SolverJobStore:
    store = SolverJobStore(tmp_path / "solver-jobs.sqlite3")
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        configuration={"backend": "local_process", "revision": "synthetic-1"},
    )
    return store


def _create_job(store: SolverJobStore, *, job_id: str = "job_synthetic_001"):
    return store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-01T00:00:00+00:00",
        job_id=job_id,
    )


def test_store_uses_wal_creates_all_durable_entities_and_survives_reopen(tmp_path):
    store = _store(tmp_path)
    job, created = _create_job(store)

    assert created is True
    assert job["workspace_id"] == WORKSPACE_ID
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "solver_binding_revisions",
        "solve_jobs",
        "solve_attempts",
        "job_subscriptions",
        "solve_artifacts",
        "verification_results",
        "run_solver_links",
        "outbox_events",
    }.issubset(tables)

    reopened = SolverJobStore(store.database_path)
    assert (
        reopened.get_job(job["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["logical_job_key"]
        == job["logical_job_key"]
    )


def test_job_reads_events_artifacts_and_verification_are_owner_scoped(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    event = store.append_event(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        event_type="solver.job.created",
        payload={"phase": "created"},
    )
    store.add_artifact(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        job_id=job["job_id"],
        attempt_id=None,
        artifact_type="problem_ir",
        artifact_digest=PROBLEM_DIGEST,
        artifact_ref="artifact://synthetic/problem",
    )
    store.add_verification_result(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        job_id=job["job_id"],
        attempt_id=None,
        verification_id="verification_synthetic_001",
        status="not_run",
        result={"status": "not_run"},
    )

    assert event["event_seq"] == 1
    assert store.get_job(job["job_id"], workspace_id=WORKSPACE_ID, owner_id="owner_other") is None
    assert (
        store.read_events(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id="owner_other",
            after_event_seq=0,
        )
        == ()
    )
    assert store.list_artifacts(job["job_id"], workspace_id=WORKSPACE_ID, owner_id="owner_other") == ()
    assert store.list_verification_results(job["job_id"], workspace_id=WORKSPACE_ID, owner_id="owner_other") == ()


def test_optimistic_updates_leases_and_fencing_reject_stale_workers(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    claimed = store.claim_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic_a",
        lease_seconds=30,
        expected_version=job["version"],
    )
    assert claimed["fencing_token"] == 1
    assert claimed["version"] == job["version"] + 1

    with pytest.raises(OptimisticLockError):
        store.update_job(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            expected_version=job["version"],
            status="submitting",
        )
    with pytest.raises(LeaseConflict):
        store.claim_job(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            lease_owner="worker_synthetic_b",
            lease_seconds=30,
        )
    with pytest.raises(LeaseConflict):
        store.update_job(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            expected_version=claimed["version"],
            fencing_token=0,
            phase="submit_job",
        )

    updated = store.update_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=claimed["version"],
        fencing_token=claimed["fencing_token"],
        status="submitting",
        phase="submit_job",
    )
    assert updated["status"] == "submitting"


def test_artifact_and_verification_writes_require_fence_when_job_is_claimed(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    claimed = store.claim_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic_fenced_writes",
        lease_seconds=30,
        expected_version=job["version"],
    )

    with pytest.raises(LeaseConflict):
        store.add_artifact(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=job["job_id"],
            attempt_id=None,
            artifact_type="problem_ir",
            artifact_digest="sha256:solver-coordination.v1:" + "f" * 64,
            artifact_ref="artifact://synthetic/problem-ir",
        )
    with pytest.raises(LeaseConflict):
        store.add_verification_result(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=job["job_id"],
            attempt_id=None,
            verification_id="verification_synthetic_fenced_write",
            status="not_run",
            result={"status": "not_run"},
        )

    artifact = store.add_artifact(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        job_id=job["job_id"],
        attempt_id=None,
        artifact_type="problem_ir",
        artifact_digest="sha256:solver-coordination.v1:" + "f" * 64,
        artifact_ref="artifact://synthetic/problem-ir",
        lease_owner="worker_synthetic_fenced_writes",
        fencing_token=claimed["fencing_token"],
    )
    verification = store.add_verification_result(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        job_id=job["job_id"],
        attempt_id=None,
        verification_id="verification_synthetic_fenced_write",
        status="not_run",
        result={"status": "not_run"},
        lease_owner="worker_synthetic_fenced_writes",
        fencing_token=claimed["fencing_token"],
    )
    assert artifact["artifact_type"] == "problem_ir"
    assert verification["status"] == "not_run"

    store.update_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=claimed["version"],
        status="failed",
        phase="solve",
        error_code="solver_unavailable",
        fencing_token=claimed["fencing_token"],
    )
    with pytest.raises(TerminalStateError):
        store.add_artifact(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=job["job_id"],
            attempt_id=None,
            artifact_type="solver_result",
            artifact_digest="sha256:solver-coordination.v1:" + "e" * 64,
            artifact_ref="artifact://synthetic/late-result",
            lease_owner="worker_synthetic_fenced_writes",
            fencing_token=claimed["fencing_token"],
        )
    with pytest.raises(TerminalStateError):
        store.add_verification_result(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=job["job_id"],
            attempt_id=None,
            verification_id="verification_synthetic_late_write",
            status="verified",
            result={"status": "verified"},
            lease_owner="worker_synthetic_fenced_writes",
            fencing_token=claimed["fencing_token"],
        )


def test_job_creation_can_atomically_reserve_and_complete_input_freeze(tmp_path):
    store = _store(tmp_path)
    job, created = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-01T00:00:00+00:00",
        job_id="job_synthetic_input_freeze",
        initial_lease_owner="input_freeze_synthetic",
        initial_lease_seconds=30,
    )

    assert created is True
    assert job["status"] == "created"
    assert job["phase"] == "freeze_inputs"
    assert job["lease_owner"] == "input_freeze_synthetic"
    assert job["lease_until"] is not None
    assert job["fencing_token"] == 1
    for index, artifact_type in enumerate(
        ("normalized_input", "fact_snapshot", "problem_ir"),
        start=1,
    ):
        store.add_artifact(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=job["job_id"],
            attempt_id=None,
            artifact_type=artifact_type,
            artifact_digest="sha256:solver-coordination.v1:{}".format(str(index) * 64),
            artifact_ref="artifact://synthetic/{}".format(artifact_type),
            lease_owner="input_freeze_synthetic",
            fencing_token=job["fencing_token"],
        )

    prepared = store.complete_input_preparation(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="input_freeze_synthetic",
        fencing_token=job["fencing_token"],
    )

    assert prepared["status"] == "created"
    assert prepared["phase"] == "created"
    assert prepared["lease_owner"] is None
    assert prepared["lease_until"] is None
    with pytest.raises(LeaseConflict):
        store.complete_input_preparation(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            lease_owner="input_freeze_synthetic",
            fencing_token=job["fencing_token"],
        )


def test_expired_freeze_fence_cannot_be_renewed_or_used_for_job_updates(tmp_path):
    current = [datetime(2026, 8, 17, tzinfo=timezone.utc)]
    store = SolverJobStore(tmp_path / "solver-jobs.sqlite3", clock=lambda: current[0])
    store.register_binding_revision(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        configuration={"backend": "synthetic"},
    )
    job, _ = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-01T00:00:00+00:00",
        initial_lease_owner="input_freeze_expiring",
        initial_lease_seconds=30,
    )
    current[0] += timedelta(seconds=31)

    with pytest.raises(LeaseConflict):
        store.renew_job_lease(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            lease_owner="input_freeze_expiring",
            fencing_token=job["fencing_token"],
            lease_seconds=30,
        )
    with pytest.raises(LeaseConflict):
        store.update_job(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            expected_version=job["version"],
            phase="created",
            fencing_token=job["fencing_token"],
        )
    claimed = store.claim_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="input_freeze_recovery",
        lease_seconds=30,
        expected_version=job["version"],
    )
    assert claimed["fencing_token"] > job["fencing_token"]


def test_attempt_updates_reject_a_stale_worker_fencing_token(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    attempt, _ = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_fenced",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-fenced-key",
    )
    first = store.claim_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic",
        lease_seconds=30,
    )
    second = store.claim_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic",
        lease_seconds=30,
    )
    with pytest.raises(LeaseConflict):
        store.update_attempt_status(
            attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            status="running",
            fencing_token=first["fencing_token"],
        )
    updated = store.update_attempt_status(
        attempt["attempt_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        status="running",
        fencing_token=second["fencing_token"],
    )
    assert updated["status"] == "running"


def test_binding_backend_job_rejects_terminal_attempt_and_stale_fencing(tmp_path):
    store = _store(tmp_path)
    first_job, _ = _create_job(store, job_id="job_synthetic_bind_terminal")
    terminal_attempt, _ = store.create_or_replay_attempt(
        first_job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_bind_terminal",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-bind-terminal-key",
    )
    store.update_attempt_status(
        terminal_attempt["attempt_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        status="failed",
        error_code="solver_unavailable",
    )
    with pytest.raises(TerminalStateError):
        store.bind_backend_job(
            terminal_attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            backend_job_id="backend_synthetic_terminal",
        )

    second_digest = "sha256:solver-coordination.v1:" + "f" * 64
    second_job, _ = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=second_digest,
        problem={"schema_version": "solver.problem.v1", "problem_id": second_digest},
        run_policy={"mode": "bounded"},
        deadline_at="2099-01-03T00:00:00+00:00",
        job_id="job_synthetic_bind_fenced",
    )
    fenced_attempt, _ = store.create_or_replay_attempt(
        second_job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_bind_fenced",
        request_payload={"problem_id": second_digest},
        backend_idempotency_key="opaque-bind-fenced-key",
    )
    first_claim = store.claim_job(
        second_job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic",
        lease_seconds=30,
    )
    second_claim = store.claim_job(
        second_job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        lease_owner="worker_synthetic",
        lease_seconds=30,
    )
    with pytest.raises(LeaseConflict):
        store.bind_backend_job(
            fenced_attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            backend_job_id="backend_synthetic_fenced",
            fencing_token=first_claim["fencing_token"],
        )
    bound = store.bind_backend_job(
        fenced_attempt["attempt_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        backend_job_id="backend_synthetic_fenced",
        fencing_token=second_claim["fencing_token"],
    )
    assert bound["backend_job_id"] == "backend_synthetic_fenced"


def test_terminal_jobs_are_immutable_and_late_attempt_results_are_rejected(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    attempt, _ = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_001",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-key",
    )
    terminal = store.update_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=job["version"],
        status="timed_out",
        phase="solve",
        error_code="timed_out_no_solution",
    )
    assert terminal["terminal_at"] is not None

    with pytest.raises(TerminalStateError):
        store.update_job(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            expected_version=terminal["version"],
            status="completed",
        )
    with pytest.raises(TerminalStateError):
        store.record_attempt_result(
            attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            result={"status": "optimal"},
        )
    with pytest.raises(TerminalStateError):
        store.update_attempt_status(
            attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            status="failed",
            error_code="solver_unavailable",
        )


def test_terminal_attempt_cannot_be_overwritten_while_job_is_nonterminal(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    attempt, _ = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_terminal",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-terminal-key",
    )
    store.update_attempt_status(
        attempt["attempt_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        status="timed_out",
        error_code="timed_out_no_solution",
    )

    with pytest.raises(TerminalStateError):
        store.record_attempt_result(
            attempt["attempt_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            result={"status": "optimal"},
        )


def test_attempt_replay_conflict_and_explicit_retry_ordinals(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    first, created = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_001",
        request_payload={"problem_id": PROBLEM_DIGEST, "limit": 10},
        backend_idempotency_key="opaque-synthetic-key-1",
    )
    replay, replay_created = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_001",
        request_payload={"limit": 10, "problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-key-1",
    )
    assert created is True
    assert replay_created is False
    assert replay["attempt_id"] == first["attempt_id"]

    with pytest.raises(AttemptPayloadConflict):
        store.create_or_replay_attempt(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_001",
            request_payload={"problem_id": PROBLEM_DIGEST, "limit": 11},
            backend_idempotency_key="opaque-synthetic-key-1",
        )

    retry, retry_created = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_002",
        request_payload={"problem_id": PROBLEM_DIGEST, "limit": 10},
        backend_idempotency_key="opaque-synthetic-key-2",
    )
    assert retry_created is True
    assert (first["attempt_number"], retry["attempt_number"]) == (1, 2)


def test_concurrent_initial_attempt_creation_converges_on_one_attempt(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)

    def create(index: int):
        return store.create_or_get_initial_attempt(
            job["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id="attempt_synthetic_{:03d}".format(index),
            request_payload={"problem_id": PROBLEM_DIGEST},
            backend_idempotency_key="opaque-synthetic-key-{:03d}".format(index),
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(create, range(8)))

    attempt_ids = {outcome[0]["attempt_id"] for outcome in outcomes}
    assert len(attempt_ids) == 1
    assert sum(1 for _, created in outcomes if created) == 1
    assert len(store.list_attempts(job["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)) == 1


def test_event_sequence_and_tool_result_write_are_atomic_and_idempotent(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    attempt, _ = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_001",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-key",
    )
    link, subscription, created = store.create_subscription_and_link(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        run_id="run_synthetic_001",
        turn_id="turn_synthetic_001",
        tool_call_id="tool_call_synthetic_001",
        job_id=job["job_id"],
        attempt_id=attempt["attempt_id"],
        subscription_id="subscription_synthetic_001",
        card_name="solve_synthetic_a1b2c3d4",
        contract_digest="sha256:solver-coordination.v1:" + "b" * 64,
    )
    assert created is True
    assert subscription["active"] == 1
    first = store.append_event(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id=attempt["attempt_id"],
        event_type="solver.progress",
        payload={"percent": 10},
    )
    second = store.append_event(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id=attempt["attempt_id"],
        event_type="solver.progress",
        payload={"percent": 20},
    )
    assert (first["event_seq"], second["event_seq"]) == (1, 2)
    advanced = store.advance_run_link_event_seq(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        run_id=link["run_id"],
        turn_id=link["turn_id"],
        tool_call_id=link["tool_call_id"],
        event_seq=2,
    )
    assert advanced["last_event_seq"] == 2
    with pytest.raises(JobStoreConflict):
        store.advance_run_link_event_seq(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id=link["run_id"],
            turn_id=link["turn_id"],
            tool_call_id=link["tool_call_id"],
            event_seq=1,
        )

    result = {"status": "completed", "job_id": job["job_id"]}
    with pytest.raises(TerminalStateError):
        store.write_tool_result(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id=link["run_id"],
            turn_id=link["turn_id"],
            tool_call_id=link["tool_call_id"],
            contract_digest=link["contract_digest"],
            result=result,
        )
    current_job = store.get_job(job["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)
    store.update_job(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=current_job["version"],
        status="completed",
        phase="publish_result",
    )
    written, result_created = store.write_tool_result(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        run_id=link["run_id"],
        turn_id=link["turn_id"],
        tool_call_id=link["tool_call_id"],
        contract_digest=link["contract_digest"],
        result=result,
    )
    replayed, replay_created = store.write_tool_result(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        run_id=link["run_id"],
        turn_id=link["turn_id"],
        tool_call_id=link["tool_call_id"],
        contract_digest=link["contract_digest"],
        result=result,
    )
    assert result_created is True
    assert replay_created is False
    assert replayed["tool_result_digest"] == written["tool_result_digest"]

    events = store.read_events(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        after_event_seq=0,
    )
    assert [event["event_seq"] for event in events] == [1, 2, 3]
    assert events[-1]["event_type"] == "solver.tool_result.ready"
    reopened = SolverJobStore(store.database_path)
    unpublished = reopened.list_unpublished_events(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID, limit=10)
    assert [event["event_seq"] for event in unpublished] == [1, 2, 3]
    reopened.mark_event_published(
        job["job_id"],
        event_seq=1,
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
    )
    assert [
        event["event_seq"]
        for event in reopened.list_unpublished_events(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID, limit=10)
    ] == [2, 3]
    with pytest.raises(JobStoreConflict):
        store.write_tool_result(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id=link["run_id"],
            turn_id=link["turn_id"],
            tool_call_id=link["tool_call_id"],
            contract_digest=link["contract_digest"],
            result={"status": "different"},
        )


def test_job_transition_and_outbox_event_are_committed_together(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    transitioned, event = store.transition_job_with_event(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        expected_version=job["version"],
        status="submitting",
        phase="submit_job",
        event_type="solver.job.submitting",
        event_payload={"phase": "submit_job", "status": "submitting"},
    )
    assert transitioned["status"] == "submitting"
    assert event["event_type"] == "solver.job.submitting"
    reopened = SolverJobStore(store.database_path)
    assert reopened.get_job(job["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "submitting"
    assert (
        reopened.list_unpublished_events(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID, limit=10)[-1]["event_type"]
        == "solver.job.submitting"
    )


def test_subscription_cancellation_is_local_and_reports_last_subscriber(tmp_path):
    store = _store(tmp_path)
    job, _ = _create_job(store)
    attempt, _ = store.create_or_replay_attempt(
        job["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_001",
        request_payload={"problem_id": PROBLEM_DIGEST},
        backend_idempotency_key="opaque-synthetic-key",
    )
    for suffix in ("a", "b"):
        store.create_subscription_and_link(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id="run_synthetic_{}".format(suffix),
            turn_id="turn_synthetic_{}".format(suffix),
            tool_call_id="tool_call_synthetic_{}".format(suffix),
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            subscription_id="subscription_synthetic_{}".format(suffix),
            card_name="solve_synthetic_a1b2c3d4",
            contract_digest="sha256:solver-coordination.v1:" + "b" * 64,
        )

    first = store.cancel_subscription(
        "subscription_synthetic_a",
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
    )
    second = store.cancel_subscription(
        "subscription_synthetic_b",
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
    )
    assert first["active_subscriber_count"] == 1
    assert first["last_subscriber"] is False
    assert first["cancel_reserved"] is False
    assert second["active_subscriber_count"] == 0
    assert second["last_subscriber"] is True
    assert second["cancel_reserved"] is True
    assert store.get_job(job["job_id"], workspace_id=WORKSPACE_ID, owner_id=OWNER_ID)["status"] == "cancelling"
    with pytest.raises(JobStoreConflict):
        store.create_subscription_and_link(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            run_id="run_synthetic_late",
            turn_id="turn_synthetic_late",
            tool_call_id="tool_call_synthetic_late",
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            subscription_id="subscription_synthetic_late",
            card_name="solve_synthetic_a1b2c3d4",
            contract_digest="sha256:solver-coordination.v1:" + "b" * 64,
        )


def test_attempt_lineage_is_enforced_for_events_artifacts_and_verification(tmp_path):
    store = _store(tmp_path)
    first, _ = _create_job(store, job_id="job_synthetic_lineage_a")
    second, _ = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest="sha256:solver-coordination.v1:" + "d" * 64,
        problem={
            "schema_version": "solver.problem.v1",
            "problem_id": "sha256:solver-coordination.v1:" + "d" * 64,
        },
        run_policy={"mode": "bounded"},
        deadline_at="2099-01-01T00:00:00+00:00",
        job_id="job_synthetic_lineage_b",
    )
    second_attempt, _ = store.create_or_replay_attempt(
        second["job_id"],
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        attempt_id="attempt_synthetic_other_job",
        request_payload={"problem_id": second["problem_digest"]},
        backend_idempotency_key="opaque-other-job-key",
    )

    with pytest.raises(JobStoreConflict):
        store.append_event(
            first["job_id"],
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            attempt_id=second_attempt["attempt_id"],
            event_type="solver.progress",
            payload={"phase": "solve"},
        )
    with pytest.raises(JobStoreConflict):
        store.add_artifact(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=first["job_id"],
            attempt_id=second_attempt["attempt_id"],
            artifact_type="solver_result",
            artifact_digest="sha256:solver-coordination.v1:" + "e" * 64,
            artifact_ref="artifact://synthetic/result",
        )
    with pytest.raises(JobStoreConflict):
        store.add_verification_result(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            job_id=first["job_id"],
            attempt_id=second_attempt["attempt_id"],
            verification_id="verification_synthetic_wrong_lineage",
            status="not_run",
            result={"status": "not_run"},
        )


def test_deadline_does_not_split_equivalent_logical_jobs(tmp_path):
    store = _store(tmp_path)
    first, _ = _create_job(store, job_id="job_synthetic_deadline_a")
    second, created = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-02T00:00:00+00:00",
        job_id="job_synthetic_deadline_b",
    )
    assert created is False
    assert second["job_id"] == first["job_id"]
    assert second["logical_job_key"] == first["logical_job_key"]
    assert second["deadline_at"] == "2099-01-01T00:00:00+00:00"


def test_different_runs_do_not_share_a_logical_job_for_same_problem_and_binding(tmp_path):
    store = _store(tmp_path)
    first, first_created = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-01T00:00:00+00:00",
        run_id="run-synthetic-a",
        job_id="job_synthetic_run_a",
    )
    second, second_created = store.create_or_get_job(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        binding_revision_id=BINDING_REVISION,
        problem_digest=PROBLEM_DIGEST,
        problem={"schema_version": "solver.problem.v1", "problem_id": PROBLEM_DIGEST},
        run_policy={"mode": "bounded", "max_attempts": 2},
        deadline_at="2099-01-01T00:00:00+00:00",
        run_id="run-synthetic-b",
        job_id="job_synthetic_run_b",
    )

    assert first_created is True
    assert second_created is True
    assert first["job_id"] != second["job_id"]
    assert first["logical_job_key"] != second["logical_job_key"]


def test_job_history_uses_scoped_keyset_pagination_without_private_bodies(tmp_path):
    store = _store(tmp_path)
    for index in range(3):
        digest = "sha256:solver-coordination.v1:" + str(index + 1) * 64
        store.create_or_get_job(
            workspace_id=WORKSPACE_ID,
            owner_id=OWNER_ID,
            binding_revision_id=BINDING_REVISION,
            problem_digest=digest,
            problem={"schema_version": "solver.problem.v1", "problem_id": digest},
            run_policy={"mode": "bounded", "max_attempts": 1},
            deadline_at="2099-01-0{}T00:00:00+00:00".format(index + 1),
            job_id="job_synthetic_history_{}".format(index + 1),
        )

    first_page = store.list_jobs(workspace_id=WORKSPACE_ID, owner_id=OWNER_ID, limit=2)
    second_page = store.list_jobs(
        workspace_id=WORKSPACE_ID,
        owner_id=OWNER_ID,
        limit=2,
        cursor=first_page["next_cursor"],
    )
    assert len(first_page["items"]) == 2
    assert len(second_page["items"]) == 1
    assert {item["job_id"] for item in first_page["items"] + second_page["items"]} == {
        "job_synthetic_history_1",
        "job_synthetic_history_2",
        "job_synthetic_history_3",
    }
    assert second_page["next_cursor"] is None
    forbidden = {"problem", "problem_json", "run_policy", "run_policy_json", "configuration"}
    assert all(not (forbidden & set(item)) for item in first_page["items"])
    assert store.list_jobs(workspace_id=WORKSPACE_ID, owner_id="owner_other", limit=10)["items"] == []
