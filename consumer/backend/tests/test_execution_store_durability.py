# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from persistence.execution_store import (
    ExecutionStore,
    ExecutionStoreCorruptionError,
)
from internal_access import internal


def _seed_checkpoint(
    store: ExecutionStore,
    *,
    session_id: str = "session_durability",
) -> tuple[dict, dict]:
    turn, created = store.create_turn_for_run(
        session_id=session_id,
        user_request="Start a synthetic approval checkpoint",
        idempotency_key_hash="initial-key-hash",
        request_fingerprint="initial-fingerprint",
    )
    assert created is True
    interaction = {
        "id": "interaction_durability_approval",
        "type": "approval",
        "prompt": "Approve the synthetic operation?",
        "options": [
            {"id": "approve", "label": "Approve", "value": True},
            {"id": "reject", "label": "Reject", "value": False},
        ],
    }
    store.await_turn(
        session_id=session_id,
        turn_id=turn["turn_id"],
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    return turn, interaction


def _approval_response(interaction: dict) -> dict:
    return {
        "interaction_id": interaction["id"],
        "type": interaction["type"],
        "option_id": "approve",
        "value": True,
    }


def test_atomic_commit_uses_same_directory_temp_file_and_fsyncs_file_and_directory(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    turn, created = store.create_turn_for_run(
        session_id="session_atomic_success",
        user_request="Synthetic request",
        idempotency_key_hash="atomic-success-key-hash",
        request_fingerprint="atomic-success-fingerprint",
    )
    assert created is True

    real_replace = os.replace
    real_fsync = os.fsync
    replacements = []
    fsync_calls = []

    def recording_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    def recording_fsync(fd):
        fsync_calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", recording_replace)
    monkeypatch.setattr(os, "fsync", recording_fsync)

    store.complete_turn(
        "session_atomic_success",
        turn["turn_id"],
        {"answer": "synthetic result"},
        "Synthetic summary",
    )

    assert len(replacements) == 1
    source, destination = replacements[0]
    assert source.parent == destination.parent
    assert source.name.startswith(".execution.json.")
    assert source.suffix == ".tmp"
    assert destination.name == "execution.json"
    assert len(fsync_calls) == 2
    assert list(destination.parent.glob(".execution.json.*.tmp")) == []


@pytest.mark.parametrize(
    ("failpoint", "durable_status"),
    [
        ("file_fsync", "running"),
        ("replace", "running"),
        ("directory_fsync", "success"),
    ],
)
def test_failed_atomic_commit_never_publishes_uncommitted_cache(
    tmp_path,
    monkeypatch,
    failpoint,
    durable_status,
):
    storage_dir = tmp_path / "executions"
    session_id = "session_atomic_{}".format(failpoint)
    store = ExecutionStore(str(storage_dir))
    turn, created = store.create_turn_for_run(
        session_id=session_id,
        user_request="Synthetic request",
        idempotency_key_hash="{}-key-hash".format(failpoint),
        request_fingerprint="{}-fingerprint".format(failpoint),
    )
    assert created is True

    real_replace = os.replace
    real_fsync = os.fsync
    fsync_count = 0

    def injected_replace(source, destination):
        if failpoint == "replace":
            raise OSError("synthetic replace failure")
        return real_replace(source, destination)

    def injected_fsync(fd):
        nonlocal fsync_count
        fsync_count += 1
        if failpoint == "file_fsync" and fsync_count == 1:
            raise OSError("synthetic file fsync failure")
        if failpoint == "directory_fsync" and fsync_count == 2:
            raise OSError("synthetic directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", injected_replace)
    monkeypatch.setattr(os, "fsync", injected_fsync)

    with pytest.raises(OSError, match="synthetic"):
        store.complete_turn(
            session_id,
            turn["turn_id"],
            {"answer": "must not enter the cache after an error"},
            None,
        )

    assert session_id not in internal(store).cache
    session_path = storage_dir / session_id / "execution.json"
    durable = json.loads(session_path.read_text(encoding="utf-8"))
    assert durable["turns"][0]["status"] == durable_status
    assert list(session_path.parent.glob(".execution.json.*.tmp")) == []


def test_corrupt_json_is_quarantined_and_session_remains_fail_closed(tmp_path):
    storage_dir = tmp_path / "executions"
    session_dir = storage_dir / "session_corrupt"
    session_dir.mkdir(parents=True)
    execution_path = session_dir / "execution.json"
    corrupt_payload = "{synthetic invalid json"
    execution_path.write_text(corrupt_payload, encoding="utf-8")
    store = ExecutionStore(str(storage_dir))

    with pytest.raises(ExecutionStoreCorruptionError, match="isolated"):
        store.get_session_history("session_corrupt")

    quarantined = list(session_dir.glob("execution.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == corrupt_payload
    assert execution_path.exists() is False
    assert store.healthy is False
    assert store.corruption_count == 1

    with pytest.raises(ExecutionStoreCorruptionError, match="isolated"):
        store.get_session_history("session_corrupt")
    assert execution_path.exists() is False
    assert list(session_dir.glob("execution.json.corrupt.*")) == quarantined


def test_fresh_store_marks_queued_and_running_turns_service_restarted(tmp_path):
    storage_dir = tmp_path / "executions"
    session_id = "session_restart_inflight"
    store = ExecutionStore(str(storage_dir))
    store.start_turn(session_id, 1, "run_synthetic_running", "Synthetic running request")
    store.start_turn(session_id, 2, "run_synthetic_queued", "Synthetic queued request")

    execution_path = storage_dir / session_id / "execution.json"
    durable = json.loads(execution_path.read_text(encoding="utf-8"))
    durable["created_at"] = "2026-01-02T03:04:05"
    durable["updated_at"] = "2026-01-02T11:04:05+08:00"
    durable["turns"][0]["start_time"] = "2026-01-02T03:04:06"
    durable["turns"][0]["status"] = "running"
    durable["turns"][1]["status"] = "queued"
    execution_path.write_text(json.dumps(durable), encoding="utf-8")

    reloaded = ExecutionStore(str(storage_dir))
    history = reloaded.get_session_history(session_id)

    assert history is not None
    assert [turn["status"] for turn in history["turns"]] == ["failed", "failed"]
    assert [turn["error_code"] for turn in history["turns"]] == [
        "SERVICE_RESTARTED",
        "SERVICE_RESTARTED",
    ]
    assert all(turn["end_time"] for turn in history["turns"])
    assert history["created_at"] == "2026-01-02T03:04:05+00:00"
    assert history["updated_at"].endswith("+00:00")
    assert history["turns"][0]["start_time"] == "2026-01-02T03:04:06+00:00"
    assert all(datetime.fromisoformat(turn["end_time"]).tzinfo == timezone.utc for turn in history["turns"])
    assert all(turn["final_output"]["error_code"] == "SERVICE_RESTARTED" for turn in history["turns"])
    persisted = json.loads(execution_path.read_text(encoding="utf-8"))
    assert [turn["error_code"] for turn in persisted["turns"]] == [
        "SERVICE_RESTARTED",
        "SERVICE_RESTARTED",
    ]
    assert persisted["created_at"] == "2026-01-02T03:04:05+00:00"
    assert persisted["turns"][0]["start_time"] == "2026-01-02T03:04:06+00:00"


def test_awaiting_input_checkpoint_survives_restart_and_remains_consumable(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    original_turn, interaction = _seed_checkpoint(store)

    reloaded = ExecutionStore(str(storage_dir))
    before_consume = reloaded.get_turn(
        "session_durability",
        original_turn["turn_id"],
    )
    assert before_consume is not None
    assert before_consume["status"] == "awaiting_input"
    assert before_consume["pending_interaction"] == interaction
    assert "interaction_response" not in before_consume
    assert "interaction_consumed_at" not in before_consume

    response = _approval_response(interaction)
    answer_turn, created = reloaded.consume_interaction_and_create_turn(
        session_id="session_durability",
        interaction_id=interaction["id"],
        user_request="Approve the synthetic operation",
        interaction_response=response,
        idempotency_key_hash="answer-key-hash",
        request_fingerprint="answer-fingerprint",
    )

    assert created is True
    persisted_original = reloaded.get_turn(
        "session_durability",
        original_turn["turn_id"],
    )
    assert persisted_original is not None
    assert persisted_original["status"] == "awaiting_input"
    assert persisted_original["pending_interaction"] == interaction
    assert persisted_original["interaction_response"] == response
    assert persisted_original["interaction_consumed_by_turn_id"] == answer_turn["turn_id"]


def test_task_start_failure_preserves_consumed_checkpoint_and_retry_run(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    original_turn, interaction = _seed_checkpoint(store)
    response = _approval_response(interaction)
    answer_turn, created = store.consume_interaction_and_create_turn(
        session_id="session_durability",
        interaction_id=interaction["id"],
        user_request="Approve the synthetic operation",
        interaction_response=response,
        idempotency_key_hash="answer-key-hash",
        request_fingerprint="answer-fingerprint",
    )
    assert created is True

    store.mark_turn_task_start_failed(
        "session_durability",
        answer_turn["turn_id"],
    )

    retry_turn, retry_created = store.consume_interaction_and_create_turn(
        session_id="session_durability",
        interaction_id=interaction["id"],
        user_request="Approve the synthetic operation",
        interaction_response=response,
        idempotency_key_hash="retry-key-hash",
        request_fingerprint="answer-fingerprint",
    )

    assert retry_created is False
    assert retry_turn["run_id"] == answer_turn["run_id"]
    assert retry_turn["turn_id"] == answer_turn["turn_id"]
    assert retry_turn["status"] == "failed"
    assert retry_turn["error_code"] == "TASK_START_FAILED"
    persisted_original = store.get_turn(
        "session_durability",
        original_turn["turn_id"],
    )
    assert persisted_original is not None
    assert persisted_original["status"] == "awaiting_input"
    assert persisted_original["interaction_response"] == response
    assert persisted_original["interaction_consumed_by_turn_id"] == answer_turn["turn_id"]
