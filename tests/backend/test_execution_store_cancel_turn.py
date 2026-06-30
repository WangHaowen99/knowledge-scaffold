"""Tests for cancelled turn persistence in execution store."""

import threading

from agentic.stores.execution_store import ExecutionStore


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
