"""Tests for run cancel metadata behavior."""

from persistence.run_store import RunStore


def test_cancel_run_merges_metadata():
    store = RunStore()
    run = store.create_run(run_id="run_1", user_request="hello", metadata={"user_id": "u1"})
    assert run is not None

    cancelled = store.cancel_run("run_1", metadata={"cancel_reason": "user_stop"})
    assert cancelled is not None
    assert cancelled.metadata["user_id"] == "u1"
    assert cancelled.metadata["cancel_reason"] == "user_stop"


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
