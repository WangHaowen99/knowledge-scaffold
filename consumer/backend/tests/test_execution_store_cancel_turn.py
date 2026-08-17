"""Tests for cancelled turn persistence in execution store."""

import threading

import persistence.execution_store as execution_store_module
from persistence.execution_store import ExecutionStore


def test_cancel_turn_marks_cancelled_and_persists_payload(tmp_path):
    store = ExecutionStore(str(tmp_path))
    store.start_turn(
        session_id="session_1",
        turn_id=1,
        run_id="run_1",
        user_request="demo request",
    )

    store.cancel_turn(
        session_id="session_1",
        turn_id=1,
        reason="user_stop",
        final_output={"cancel_reason": "user_stop", "partial_output": {"a": 1}},
        summary="cancelled after partial progress",
    )

    history = store.get_session_history("session_1")
    assert history is not None
    turn = next((t for t in history["turns"] if t["turn_id"] == 1), None)
    assert turn is not None
    assert turn["status"] == "cancelled"
    assert turn["summary"] == "cancelled after partial progress"
    assert turn["final_output"]["cancel_reason"] == "user_stop"
    assert turn["end_time"] is not None
    assert "nodes" not in turn


def test_fail_turn_does_not_overwrite_cancelled_terminal(tmp_path):
    store = ExecutionStore(str(tmp_path))
    store.start_turn(
        session_id="session_cancelled",
        turn_id=1,
        run_id="run_cancelled",
        user_request="demo request",
    )
    store.cancel_turn(
        session_id="session_cancelled",
        turn_id=1,
        reason="user_stop",
        final_output={"cancel_reason": "user_stop"},
    )

    store.fail_turn("session_cancelled", 1, "late failure")

    turn = store.get_turn("session_cancelled", 1)
    assert turn["status"] == "cancelled"
    assert turn["final_output"] == {"cancel_reason": "user_stop"}


def test_execution_store_atomic_replace_syncs_parent_directory(tmp_path, monkeypatch):
    synced_directories = []
    monkeypatch.setattr(
        execution_store_module,
        "_fsync_directory",
        lambda path: synced_directories.append(path),
    )
    store = ExecutionStore(str(tmp_path))

    store.start_turn(
        session_id="session_durable",
        turn_id=1,
        run_id="run_durable",
        user_request="demo request",
    )

    assert synced_directories == [tmp_path / "session_durable"]


def test_get_turn_returns_without_reentering_store_lock(tmp_path):
    store = ExecutionStore(str(tmp_path))
    store.start_turn(
        session_id="session_1",
        turn_id=1,
        run_id="run_1",
        user_request="demo request",
    )

    result = []

    def read_turn():
        result.append(store.get_turn("session_1", 1))

    thread = threading.Thread(target=read_turn, daemon=True)
    thread.start()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert result[0]["run_id"] == "run_1"


def test_get_recent_turns_returns_without_reentering_store_lock(tmp_path):
    store = ExecutionStore(str(tmp_path))
    store.start_turn(
        session_id="session_1",
        turn_id=1,
        run_id="run_1",
        user_request="first request",
    )

    result = []

    def read_turns():
        result.append(store.get_recent_turns("session_1"))

    thread = threading.Thread(target=read_turns, daemon=True)
    thread.start()
    thread.join(timeout=0.5)

    assert not thread.is_alive()
    assert [turn["run_id"] for turn in result[0]] == ["run_1"]


def test_reading_missing_execution_history_does_not_create_session_directory(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))

    assert store.get_session_history("missing-session") is None
    assert not (storage_dir / "missing-session").exists()
