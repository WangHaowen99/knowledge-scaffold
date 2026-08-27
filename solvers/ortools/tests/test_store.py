from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from solvers.ortools.store import AttemptStore, IdempotencyConflict


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_attempt_store_reuses_same_key_and_same_canonical_request(tmp_path):
    store = AttemptStore(tmp_path / "attempts.sqlite3", ttl_seconds=3600, clock=_now)

    first, created = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    second, reused = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )

    assert created is True
    assert reused is False
    assert second.job_id == first.job_id


def test_attempt_store_rejects_same_key_with_different_request(tmp_path):
    store = AttemptStore(tmp_path / "attempts.sqlite3", ttl_seconds=3600, clock=_now)
    store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )

    with pytest.raises(IdempotencyConflict):
        store.create_or_get(
            idempotency_key="opaque-key-00000001",
            request_digest="sha256:" + "2" * 64,
            request_id="req_SYNTHETIC0123456789AB",
            problem_type="binary_ordered_route",
            problem_version="1.0",
        )


def test_reopening_store_marks_unfinished_attempt_as_lost(tmp_path):
    path = tmp_path / "attempts.sqlite3"
    first = AttemptStore(path, ttl_seconds=3600, clock=_now)
    job, _ = first.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    first.update_status(job.job_id, "running")
    first.close()

    recovered = AttemptStore(path, ttl_seconds=3600, clock=_now)

    assert recovered.get(job.job_id).status == "attempt_lost"


def test_attempt_store_removes_expired_mappings(tmp_path):
    current = [_now()]
    store = AttemptStore(
        tmp_path / "attempts.sqlite3",
        ttl_seconds=10,
        clock=lambda: current[0],
    )
    job, _ = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    current[0] += timedelta(seconds=11)

    store.purge_expired()

    assert store.get(job.job_id) is None


def test_expired_mapping_is_not_readable_without_explicit_purge(tmp_path):
    current = [_now()]
    store = AttemptStore(
        tmp_path / "attempts.sqlite3",
        ttl_seconds=10,
        clock=lambda: current[0],
    )
    job, _ = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    current[0] += timedelta(seconds=11)

    assert store.get(job.job_id) is None
    assert store.get_by_idempotency_key("opaque-key-00000001") is None


@pytest.mark.parametrize("terminal", ("failed", "cancelled", "attempt_lost"))
def test_late_result_cannot_overwrite_a_terminal_job(tmp_path, terminal):
    store = AttemptStore(tmp_path / "attempts.sqlite3", ttl_seconds=3600, clock=_now)
    job, _ = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    store.update_status(job.job_id, terminal)

    record = store.complete(job.job_id, {"schema_version": "solver.result.v1"})

    assert record.status == terminal
    assert record.result is None


@pytest.mark.parametrize("terminal", ("failed", "cancelled", "attempt_lost"))
def test_terminal_job_cannot_regress_to_running(tmp_path, terminal):
    store = AttemptStore(tmp_path / "attempts.sqlite3", ttl_seconds=3600, clock=_now)
    job, _ = store.create_or_get(
        idempotency_key="opaque-key-00000001",
        request_digest="sha256:" + "1" * 64,
        request_id="req_SYNTHETIC0123456789AB",
        problem_type="binary_ordered_route",
        problem_version="1.0",
    )
    store.update_status(job.job_id, terminal)

    record = store.update_status(job.job_id, "running")

    assert record.status == terminal
