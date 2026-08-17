"""Tests for run cancel metadata behavior."""

import pytest

from persistence.run_store import RunStatus, RunStore


def test_cancel_run_merges_metadata():
    store = RunStore()
    run = store.create_run(run_id="run_1", user_request="hello", metadata={"user_id": "u1"})
    assert run is not None

    cancelled = store.cancel_run("run_1", metadata={"cancel_reason": "user_stop"})
    assert cancelled is not None
    assert cancelled.metadata["user_id"] == "u1"
    assert cancelled.metadata["cancel_reason"] == "user_stop"


def test_cancelled_run_cannot_be_overwritten_by_late_terminal_result():
    store = RunStore()
    store.create_run(run_id="run_1", user_request="hello")
    store.cancel_run("run_1", metadata={"cancel_reason": "user_stop"})

    store.complete_run("run_1", {"answer": "late"})
    store.fail_run("run_1", "late failure")
    store.await_run("run_1", {"status": "late awaiting"})

    run = store.get_run("run_1")
    assert run.status == RunStatus.CANCELLED
    assert run.result is None
    assert run.error is None


def test_transactional_completion_leaves_run_running_when_commit_fails():
    store = RunStore()
    store.create_run(run_id="run_tx", user_request="hello", metadata={"user_id": "u1"})

    with pytest.raises(RuntimeError, match="persistence failed"):
        store.complete_run_transaction(
            "run_tx",
            {"answer": "late"},
            lambda: (_ for _ in ()).throw(RuntimeError("persistence failed")),
        )

    run = store.get_run("run_tx")
    assert run.status == RunStatus.RUNNING
    assert run.result is None
    assert store.get_active_run_count("u1") == 1


def test_transactional_completion_skips_commit_after_cancellation():
    store = RunStore()
    store.create_run(run_id="run_tx", user_request="hello")
    store.cancel_run("run_tx", metadata={"cancel_reason": "race"})
    committed = False

    def commit():
        nonlocal committed
        committed = True

    run = store.complete_run_transaction("run_tx", {"answer": "late"}, commit)

    assert committed is False
    assert run.status == RunStatus.CANCELLED
    assert run.result is None


def test_transactional_awaiting_commits_dependencies_before_final_state():
    store = RunStore()
    store.create_run(run_id="run_await", user_request="hello", metadata={"user_id": "u1"})
    committed = []

    run = store.await_run_transaction(
        "run_await",
        {"status": "awaiting_input"},
        {"execution_type": "deterministic"},
        lambda: committed.append("turn-and-events"),
    )
    store.cancel_run("run_await", metadata={"cancel_reason": "late"})

    assert committed == ["turn-and-events"]
    assert run.status == RunStatus.AWAITING
    assert run.result == {"status": "awaiting_input"}
    assert run.metadata["execution_type"] == "deterministic"
    assert store.get_active_run_count("u1") == 0


def test_transactional_failure_skips_commit_after_cancellation():
    store = RunStore()
    store.create_run(run_id="run_failed", user_request="hello")
    store.cancel_run("run_failed", metadata={"cancel_reason": "race"})
    committed = []

    run = store.fail_run_transaction(
        "run_failed",
        "late failure",
        {"error_code": "late"},
        lambda: committed.append("turn-and-events"),
        result={"status": "failed"},
    )

    assert committed == []
    assert run.status == RunStatus.CANCELLED
    assert run.error is None
    assert "error_code" not in run.metadata


def test_transactional_failure_persists_structured_result():
    store = RunStore()
    store.create_run(run_id="run_failed", user_request="hello")
    public_result = {
        "status": "failed",
        "error": {"code": "handler_failed", "message": "failed safely"},
    }

    run = store.fail_run_transaction(
        "run_failed",
        "handler_failed: failed safely",
        {"error_code": "handler_failed"},
        lambda: None,
        result=public_result,
    )

    assert run.status == RunStatus.FAILED
    assert run.result == public_result


def test_upsert_completed_run_increments_active_count_for_same_user():
    store = RunStore()
    store.create_run(run_id="run_1", user_request="hello", metadata={"user_id": "u1"})
    store.complete_run("run_1", {"answer": "done"})
    assert store.get_active_run_count("u1") == 0

    store.create_run(run_id="run_1", user_request="rerun", metadata={"user_id": "u1"})

    assert store.get_active_run_count("u1") == 1


def test_upsert_running_run_moves_active_count_when_user_changes():
    store = RunStore()
    store.create_run(run_id="run_1", user_request="hello", metadata={"user_id": "u1"})
    assert store.get_active_run_count("u1") == 1

    store.create_run(run_id="run_1", user_request="rerun", metadata={"user_id": "u2"})

    assert store.get_active_run_count("u1") == 0
    assert store.get_active_run_count("u2") == 1
