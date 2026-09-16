# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import threading

import pytest

import persistence.execution_store as execution_store_module
import persistence.session_summary_index as summary_index_module
import pytest
from persistence.chat_store import ChatStore
from persistence.deterministic_terminal import (
    persist_deterministic_terminal,
    recover_pending_deterministic_artifacts,
    recover_pending_deterministic_terminal,
)
from persistence.execution_store import (
    INTERACTION_CANCELLED_MESSAGE,
    INTERACTION_CANCELLED_NOTICE,
    ExecutionStore,
    InteractionCheckpointError,
    SolverCheckpointError,
    TurnOwnershipError,
)
from persistence.session_summary_index import (
    SessionSummary,
    SessionSummaryIndex,
    SessionSummaryIndexWriteError,
)
from internal_access import internal


def _create_reserved_protocol_turn(
    store: ExecutionStore,
    *,
    session_id: str,
    external_run_id: str,
):
    reservation_id = store.reserve_protocol_run_id("a2a", external_run_id)
    if reservation_id is None:
        return None, False
    return store.create_turn_for_reserved_protocol_run(
        protocol="a2a",
        external_run_id=external_run_id,
        reservation_id=reservation_id,
        session_id=session_id,
        user_request="synthetic request",
    )


def test_session_admission_reservation_is_shared_and_consumed_atomically(tmp_path):
    storage = tmp_path / "executions"
    first = ExecutionStore(str(storage))
    second = ExecutionStore(str(storage))
    token = first.reserve_session_admission("reserved-session")

    assert token
    assert second.reserve_session_admission("reserved-session") is None
    with second.session_prune_guard("reserved-session") as prune_allowed:
        assert prune_allowed is False

    turn, created = second.create_turn_for_run(
        session_id="reserved-session",
        user_request="synthetic request",
        admission_reservation_id=token,
    )

    assert created is True
    assert turn["turn_id"] == 1
    next_token = first.reserve_session_admission("reserved-session")
    assert next_token
    assert first.release_session_admission("reserved-session", next_token) is True


def _seed_session_summary(index: SessionSummaryIndex, session_id: str) -> None:
    index.upsert(
        SessionSummary.create(
            session_id=session_id,
            title="Synthetic session",
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T00:00:00+00:00",
            metadata={},
        )
    )


def _summary(index: SessionSummaryIndex, session_id: str):
    return next(item for item in index.list_page(limit=100).items if item.session_id == session_id)


def test_execution_store_projects_active_interaction_and_terminal_runtime(tmp_path):
    index = SessionSummaryIndex(tmp_path / "session-summary-index.json")
    _seed_session_summary(index, "summary-execution")
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)

    turn, created = store.create_turn_for_run(
        session_id="summary-execution",
        user_request="synthetic request",
        run_id="run-summary-1",
    )
    assert created is True
    running = _summary(index, "summary-execution")
    assert running.active_run_id == "run-summary-1"
    assert running.last_run_status == "running"
    assert running.pending_interaction is None
    assert running.turn_count == 1

    interaction = {
        "id": "interaction-summary",
        "type": "input",
        "prompt": "Synthetic input",
    }
    store.await_turn(
        "summary-execution",
        turn["turn_id"],
        final_output={"interaction": interaction},
        run_id="run-summary-1",
    )
    waiting = _summary(index, "summary-execution")
    assert waiting.active_run_id == "run-summary-1"
    assert waiting.last_run_status == "awaiting_input"
    assert dict(waiting.pending_interaction or {}) == interaction

    resumed, resumed_created = store.consume_interaction_and_create_turn(
        session_id="summary-execution",
        interaction_id="interaction-summary",
        user_request="synthetic answer",
        interaction_response={
            "interaction_id": "interaction-summary",
            "type": "input",
            "value": "approved",
        },
    )
    assert resumed_created is True
    resumed_summary = _summary(index, "summary-execution")
    assert resumed_summary.active_run_id == resumed["run_id"]
    assert resumed_summary.last_run_status == "running"
    assert resumed_summary.pending_interaction is None
    assert resumed_summary.turn_count == 2

    store.complete_turn(
        "summary-execution",
        resumed["turn_id"],
        {"answer": "synthetic"},
        "done",
        run_id=resumed["run_id"],
    )
    completed = _summary(index, "summary-execution")
    assert completed.active_run_id is None
    assert completed.last_run_status == "completed"
    assert completed.pending_interaction is None
    assert completed.turn_count == 2


def test_get_turn_count_reads_ready_summary_without_loading_execution_history(
    tmp_path,
    monkeypatch,
):
    index = SessionSummaryIndex(tmp_path / "session-summary-index.json")
    _seed_session_summary(index, "ready-count")
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)
    store.start_turn(
        "ready-count",
        1,
        "run-ready-count",
        "synthetic request",
    )
    monkeypatch.setattr(
        store,
        "_load_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ready summary count must avoid history loading")
        ),
    )

    assert store.get_turn_count("ready-count") == 1


def test_get_turn_count_treats_missing_session_in_ready_index_as_new(
    tmp_path,
    monkeypatch,
):
    index = SessionSummaryIndex(
        tmp_path / "session-summary-index.json",
        rebuild_loader=lambda: (),
    )
    index.rebuild()
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)
    monkeypatch.setattr(
        store,
        "_load_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("new session count must avoid history loading")),
    )

    assert store.get_turn_count("new-session") == 0


def test_get_turn_count_falls_back_to_execution_fact_when_summary_is_cold(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "executions"
    writer = ExecutionStore(str(storage_dir))
    writer.start_turn(
        "cold-count",
        1,
        "run-cold-count",
        "synthetic request",
    )
    index = SessionSummaryIndex(
        tmp_path / "missing-summary-index.json",
        rebuild_loader=lambda: (),
    )
    reader = ExecutionStore(str(storage_dir), summary_index=index)
    monkeypatch.setattr(
        index,
        "get_summary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cold index must not be read as ready")),
        raising=False,
    )

    assert reader.get_turn_count("cold-count") == 1


def test_execution_store_summary_failure_does_not_rollback_turn(tmp_path):
    class FailingIndex:
        def __init__(self):
            self.degraded_reason = None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def update_runtime(self, _session_id, **_fields):
            raise OSError("synthetic index failure")

        def mark_degraded(self, reason):
            self.degraded_reason = reason

    index = FailingIndex()
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)

    turn, created = store.create_turn_for_run(
        session_id="summary-write-survives",
        user_request="synthetic request",
        run_id="run-write-survives",
    )

    assert created is True
    assert store.get_turn("summary-write-survives", turn["turn_id"]) is not None
    assert index.degraded_reason == "execution_projection_failed"


def test_execution_projection_crash_marker_repairs_runtime_after_restart(tmp_path, monkeypatch):
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(path)
    _seed_session_summary(index, "runtime-after-crash")
    base_summary = _summary(index, "runtime-after-crash")
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)
    monkeypatch.setattr(
        index,
        "update_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic process exit before runtime projection")
        ),
    )

    store.create_turn_for_run(
        session_id="runtime-after-crash",
        user_request="synthetic request",
        run_id="run-after-crash",
    )

    restarted = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: [
            replace(
                base_summary,
                **store.try_get_session_summary_runtime("runtime-after-crash"),
            )
        ],
    )
    repaired = _summary(restarted, "runtime-after-crash")
    assert repaired.active_run_id == "run-after-crash"
    assert repaired.last_run_status == "running"


def test_execution_projection_failure_rebuilds_runtime_in_same_process(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(path)
    _seed_session_summary(index, "runtime-same-process")
    base_summary = _summary(index, "runtime-same-process")
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)

    def rebuild_summaries():
        return [
            replace(
                base_summary,
                **store.try_get_session_summary_runtime("runtime-same-process"),
            )
        ]

    internal(index).rebuild_loader = rebuild_summaries
    monkeypatch.setattr(
        index,
        "update_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic runtime projection failure")),
    )

    store.create_turn_for_run(
        session_id="runtime-same-process",
        user_request="synthetic request",
        run_id="run-same-process",
    )

    repaired = _summary(index, "runtime-same-process")
    assert repaired.active_run_id == "run-same-process"
    assert repaired.last_run_status == "running"
    assert internal(index).dirty_path.exists() is False


def test_execution_source_failure_abandons_projection_for_rebuild(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(path)
    _seed_session_summary(index, "runtime-write-failure")
    base_summary = _summary(index, "runtime-write-failure")

    def rebuild_summaries():
        return [base_summary]

    internal(index).rebuild_loader = rebuild_summaries
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)
    original_replace = execution_store_module.os.replace

    def fail_execution_replace(source, target):
        if str(target).endswith("execution.json"):
            raise OSError("synthetic execution source failure")
        return original_replace(source, target)

    monkeypatch.setattr(execution_store_module.os, "replace", fail_execution_replace)

    with pytest.raises(OSError, match="source failure"):
        store.create_turn_for_run(
            session_id="runtime-write-failure",
            user_request="synthetic request",
            run_id="run-write-failure",
        )

    assert _summary(index, "runtime-write-failure").last_run_status == "idle"
    assert internal(index).dirty_path.exists() is False


def test_execution_marker_failure_does_not_abort_durable_turn(tmp_path, monkeypatch):
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(path)
    _seed_session_summary(index, "execution-marker-failure")
    base_summary = _summary(index, "execution-marker-failure")
    store = ExecutionStore(str(tmp_path / "executions"), summary_index=index)
    original_write = internal(summary_index_module).write_atomic_json

    def fail_dirty_marker(target, document):
        if str(target).endswith(".dirty"):
            raise SessionSummaryIndexWriteError(
                "synthetic execution marker failure",
                committed=False,
            )
        return original_write(target, document)

    monkeypatch.setattr(summary_index_module, "_write_atomic_json", fail_dirty_marker)

    turn, created = store.create_turn_for_run(
        session_id="execution-marker-failure",
        user_request="must commit",
        run_id="run-must-commit",
    )

    assert created is True
    assert store.get_turn("execution-marker-failure", turn["turn_id"]) is not None
    execution_path = tmp_path / "executions" / "execution-marker-failure" / "execution.json"
    assert execution_path.exists() is True

    restarted = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: [
            replace(
                base_summary,
                **store.get_session_summary_runtime("execution-marker-failure"),
            )
        ],
    )
    repaired = _summary(restarted, "execution-marker-failure")
    assert repaired.active_run_id == "run-must-commit"
    assert repaired.turn_count == 1


def _seed_choice_checkpoint(store: ExecutionStore, session_id: str = "sess-choice") -> dict:
    interaction = {
        "id": "interaction_call_choice",
        "type": "single_choice",
        "prompt": "请选择方案",
        "options": [
            {"id": "a", "label": "方案 A", "value": "A"},
            {"id": "b", "label": "方案 B", "value": "B"},
        ],
    }
    original_turn, created = store.create_turn_for_run(
        session_id=session_id,
        user_request="start",
        idempotency_key_hash="checkpoint-request-key-hash",
        request_fingerprint="checkpoint-request-fingerprint",
    )
    assert created is True
    store.await_turn(
        session_id=session_id,
        turn_id=original_turn["turn_id"],
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    return interaction


def test_deterministic_terminal_journal_recovers_failed_event_projection(
    tmp_path,
    monkeypatch,
):
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="session_terminal_recovery")
    execution_store.start_turn(
        "session_terminal_recovery",
        1,
        "run_terminal_recovery",
        "synthetic request",
    )
    event = {
        "event": "agent.v2",
        "data": {
            "protocol": "agent-stream.v2",
            "event_id": "run_terminal_recovery:1",
            "seq": 1,
            "type": "turn.finished",
            "run_id": "run_terminal_recovery",
            "session_id": "session_terminal_recovery",
            "turn_id": 1,
            "payload": {"status": "completed"},
        },
    }
    original_append = chat_store.append_events_once
    monkeypatch.setattr(
        chat_store,
        "append_events_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic projection failure")),
    )

    with pytest.raises(RuntimeError, match="synthetic projection failure"):
        persist_deterministic_terminal(
            execution_store,
            chat_store,
            session_id="session_terminal_recovery",
            turn_id=1,
            status="success",
            final_output={
                "status": "success",
                "text": "synthetic answer",
                "execution": {"type": "deterministic"},
            },
            llm_context=[],
            events=[event],
        )

    assert execution_store.get_pending_deterministic_terminal("session_terminal_recovery") is not None
    monkeypatch.setattr(chat_store, "append_events_once", original_append)

    assert (
        recover_pending_deterministic_terminal(
            execution_store,
            chat_store,
            "session_terminal_recovery",
        )
        is True
    )
    assert execution_store.get_pending_deterministic_terminal("session_terminal_recovery") is None
    assert execution_store.get_turn("session_terminal_recovery", 1)["status"] == "success"
    assert any(
        (item.get("data") or {}).get("event_id") == "run_terminal_recovery:1"
        for item in chat_store.get_session("session_terminal_recovery").events
    )


def test_deterministic_artifact_journal_removes_failed_run_output(tmp_path):
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="session_artifact_recovery")
    execution_store.start_turn(
        "session_artifact_recovery",
        1,
        "run_artifact_recovery",
        "synthetic request",
    )
    output_ref = "a" * 32
    execution_store.reserve_deterministic_artifacts(
        "session_artifact_recovery",
        1,
        "run_artifact_recovery",
        [output_ref],
    )
    stored = chat_store.store_tool_output(
        "session_artifact_recovery",
        "run_artifact_recovery",
        "deterministic:result.txt",
        "synthetic artifact",
        output_ref=output_ref,
    )
    assert stored is not None
    execution_store.fail_turn(
        "session_artifact_recovery",
        1,
        "synthetic failure",
        run_id="run_artifact_recovery",
    )

    assert (
        recover_pending_deterministic_artifacts(
            execution_store,
            chat_store,
            "session_artifact_recovery",
            run_is_active=lambda _run_id: False,
        )
        is True
    )
    assert execution_store.get_pending_deterministic_artifacts("session_artifact_recovery") == []
    assert (
        chat_store.get_tool_output(
            "session_artifact_recovery",
            "run_artifact_recovery",
            output_ref,
        )
        is None
    )


def test_deterministic_artifact_recovery_preserves_running_turn(tmp_path):
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="session_running_artifact")
    execution_store.start_turn(
        "session_running_artifact",
        1,
        "run_running_artifact",
        "synthetic request",
    )
    output_ref = "b" * 32
    execution_store.reserve_deterministic_artifacts(
        "session_running_artifact",
        1,
        "run_running_artifact",
        [output_ref],
    )
    chat_store.store_tool_output(
        "session_running_artifact",
        "run_running_artifact",
        "deterministic:result.txt",
        "synthetic artifact",
        output_ref=output_ref,
    )

    assert (
        recover_pending_deterministic_artifacts(
            execution_store,
            chat_store,
            "session_running_artifact",
            run_is_active=lambda _run_id: False,
        )
        is False
    )
    assert execution_store.get_pending_deterministic_artifacts("session_running_artifact") == [
        {
            "turn_id": 1,
            "run_id": "run_running_artifact",
            "output_refs": [output_ref],
        }
    ]
    assert (
        chat_store.get_tool_output(
            "session_running_artifact",
            "run_running_artifact",
            output_ref,
        )
        is not None
    )


def _seed_one_run_checkpoint(
    store: ExecutionStore,
    session_id: str = "session_hitl_contract",
) -> tuple[dict, dict]:
    original_turn, created = store.create_turn_for_run(
        session_id=session_id,
        user_request="Start a synthetic interaction",
        idempotency_key_hash="initial-request-key-hash",
        request_fingerprint="initial-request-fingerprint",
    )
    assert created is True

    interaction = {
        "id": "interaction_contract_choice",
        "type": "single_choice",
        "prompt": "Choose a synthetic option",
        "options": [
            {"id": "option_a", "label": "Option A", "value": "value_a"},
            {"id": "option_b", "label": "Option B", "value": "value_b"},
        ],
    }
    store.await_turn(
        session_id=session_id,
        turn_id=original_turn["turn_id"],
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    return original_turn, interaction


def _answer_choice(interaction: dict, option_id: str, value: str) -> dict:
    return {
        "interaction_id": interaction["id"],
        "type": interaction["type"],
        "option_id": option_id,
        "value": value,
    }


def test_hitl_answer_creates_new_run_and_turn_without_reopening_original(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    original_turn, interaction = _seed_one_run_checkpoint(store)
    response = _answer_choice(interaction, "option_a", "value_a")

    answer_turn, created = store.consume_interaction_and_create_turn(
        session_id="session_hitl_contract",
        interaction_id=interaction["id"],
        user_request="Choose option A",
        interaction_response=response,
        idempotency_key_hash="answer-request-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )

    assert created is True
    assert answer_turn["run_id"] != original_turn["run_id"]
    assert answer_turn["turn_id"] == original_turn["turn_id"] + 1
    assert answer_turn["status"] == "running"

    history = store.get_session_history("session_hitl_contract")
    assert len(history["turns"]) == 2
    persisted_original, persisted_answer = history["turns"]
    assert persisted_original["run_id"] == original_turn["run_id"]
    assert persisted_original["status"] == "awaiting_input"
    assert persisted_original["pending_interaction"] == interaction
    assert persisted_original["interaction_response"] == response
    assert persisted_original["interaction_consumed_at"]
    assert persisted_original["interaction_consumed_by_turn_id"] == persisted_answer["turn_id"]
    assert persisted_answer["run_id"] == answer_turn["run_id"]
    assert persisted_answer["turn_id"] == answer_turn["turn_id"]
    answer_message = history["session_history"][-1]
    assert answer_message["role"] == "user"
    assert answer_message["run_id"] == answer_turn["run_id"]
    assert answer_message["turn_id"] == answer_turn["turn_id"]
    assert answer_message["interaction_response"] == response
    assert not any(message.get("role") == "tool" for message in history["session_history"])


def test_same_interaction_retry_returns_existing_admission_without_second_execution(
    tmp_path,
):
    store = ExecutionStore(str(tmp_path / "executions"))
    _original_turn, interaction = _seed_one_run_checkpoint(store)
    response = _answer_choice(interaction, "option_a", "value_a")
    registered_run_ids = []

    first_turn, first_created = store.consume_interaction_and_create_turn(
        session_id="session_hitl_contract",
        interaction_id=interaction["id"],
        user_request="Choose option A",
        interaction_response=response,
        idempotency_key_hash="answer-request-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )
    if first_created:
        registered_run_ids.append(first_turn["run_id"])

    retry_turn, retry_created = store.consume_interaction_and_create_turn(
        session_id="session_hitl_contract",
        interaction_id=interaction["id"],
        user_request="Choose option A",
        interaction_response=response,
        idempotency_key_hash="answer-retry-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )
    if retry_created:
        registered_run_ids.append(retry_turn["run_id"])

    assert first_created is True
    assert retry_created is False
    assert retry_turn == first_turn
    assert registered_run_ids == [first_turn["run_id"]]
    history = store.get_session_history("session_hitl_contract")
    assert len(history["turns"]) == 2


def test_concurrent_interaction_retries_create_one_run_and_turn(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    _original_turn, interaction = _seed_one_run_checkpoint(store)
    response = _answer_choice(interaction, "option_a", "value_a")

    def submit(index: int):
        return store.consume_interaction_and_create_turn(
            session_id="session_hitl_contract",
            interaction_id=interaction["id"],
            user_request="Choose option A",
            interaction_response=response,
            idempotency_key_hash="answer-retry-key-hash-{}".format(index),
            request_fingerprint="answer-request-fingerprint",
        )

    with ThreadPoolExecutor(max_workers=20) as executor:
        admissions = list(executor.map(submit, range(100)))

    turns = [turn for turn, _created in admissions]
    created_flags = [created for _turn, created in admissions]
    assert sum(created_flags) == 1
    assert {turn["run_id"] for turn in turns} == {turns[0]["run_id"]}
    assert {turn["turn_id"] for turn in turns} == {turns[0]["turn_id"]}
    history = store.get_session_history("session_hitl_contract")
    assert len(history["turns"]) == 2
    assert history["turns"][0]["status"] == "awaiting_input"
    assert history["turns"][0]["interaction_consumed_by_turn_id"] == history["turns"][1]["turn_id"]


def test_different_answer_for_consumed_interaction_reports_existing_run(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    _original_turn, interaction = _seed_one_run_checkpoint(store)
    first_response = _answer_choice(interaction, "option_a", "value_a")
    first_turn, created = store.consume_interaction_and_create_turn(
        session_id="session_hitl_contract",
        interaction_id=interaction["id"],
        user_request="Choose option A",
        interaction_response=first_response,
        idempotency_key_hash="answer-request-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )
    assert created is True

    conflicting_response = _answer_choice(interaction, "option_b", "value_b")
    with pytest.raises(InteractionCheckpointError) as exc_info:
        store.consume_interaction_and_create_turn(
            session_id="session_hitl_contract",
            interaction_id=interaction["id"],
            user_request="Choose option B",
            interaction_response=conflicting_response,
            idempotency_key_hash="conflicting-answer-key-hash",
            request_fingerprint="conflicting-answer-fingerprint",
        )

    assert exc_info.value.code == "INTERACTION_ALREADY_CONSUMED"
    assert exc_info.value.existing_run_id == first_turn["run_id"]
    history = store.get_session_history("session_hitl_contract")
    assert len(history["turns"]) == 2


def test_interaction_response_survives_backend_process_reload_and_is_consumed(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    interaction = _seed_choice_checkpoint(store)
    response = {
        "interaction_id": interaction["id"],
        "type": "single_choice",
        "option_id": "a",
        "value": "A",
    }

    # The ContextEngine cutover does not change ExecutionStore's on-disk
    # contract. A fresh instance models the old-backend process reopening the
    # same current runtime after a code-only rollback.
    reloaded_store = ExecutionStore(str(storage_dir))
    answer_turn, created = reloaded_store.consume_interaction_and_create_turn(
        session_id="sess-choice",
        user_request="方案 A",
        interaction_id=interaction["id"],
        interaction_response=response,
        idempotency_key_hash="answer-request-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )
    assert created is True

    history = reloaded_store.get_session_history("sess-choice")
    assert history["turns"][0]["interaction_response"] == response
    assert history["turns"][0]["interaction_consumed_at"]
    assert history["turns"][0]["interaction_consumed_by_turn_id"] == answer_turn["turn_id"]
    assert history["turns"][1]["status"] == "running"

    retry_turn, retry_created = reloaded_store.consume_interaction_and_create_turn(
        session_id="sess-choice",
        user_request="方案 A",
        interaction_id=interaction["id"],
        interaction_response=response,
        idempotency_key_hash="answer-retry-key-hash",
        request_fingerprint="answer-request-fingerprint",
    )
    assert retry_created is False
    assert retry_turn == answer_turn


@pytest.mark.parametrize(
    "response,error",
    [
        ({"interaction_id": "wrong", "option_id": "a", "value": "A"}, "interaction_id"),
        (
            {
                "interaction_id": "interaction_call_choice",
                "type": "approval",
                "option_id": "a",
                "value": "A",
            },
            "interaction type",
        ),
        (
            {"interaction_id": "interaction_call_choice", "option_id": "missing", "value": "A"},
            "selected option",
        ),
        (
            {"interaction_id": "interaction_call_choice", "option_id": "a", "value": "B"},
            "different options",
        ),
    ],
)
def test_interaction_response_must_match_durable_checkpoint(tmp_path, response, error):
    store = ExecutionStore(str(tmp_path / "executions"))
    _seed_choice_checkpoint(store)

    with pytest.raises(InteractionCheckpointError, match=error):
        store.validate_interaction_response("sess-choice", response)


def test_pending_interaction_cannot_be_bypassed_with_plain_message(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    _seed_choice_checkpoint(store)

    with pytest.raises(InteractionCheckpointError, match="awaiting an interaction response"):
        store.create_turn_for_run(
            session_id="sess-choice",
            user_request="ignore checkpoint",
            idempotency_key_hash="bypass-request-key-hash",
            request_fingerprint="bypass-request-fingerprint",
        )


def test_pending_interaction_can_be_cancelled_without_losing_context(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    interaction = _seed_choice_checkpoint(store)
    prior_context = [
        {"role": "system", "content": "system prompt"},
        {"role": "assistant", "content": "请选择方案"},
    ]
    store.set_llm_context("sess-choice", list(prior_context))

    cancellation = store.cancel_pending_interaction(
        "sess-choice",
        interaction["id"],
    )

    assert cancellation["action"] == "cancel"
    assert cancellation["cancelled"] is True
    assert cancellation["cancelled_by"] == "user"
    assert cancellation["message"] == INTERACTION_CANCELLED_MESSAGE
    assert cancellation["display_message"] == INTERACTION_CANCELLED_NOTICE
    history = store.get_session_history("sess-choice")
    assert history["turns"][0]["status"] == "awaiting_input"
    assert history["turns"][0]["interaction_response"] == cancellation
    assert history["turns"][0]["interaction_consumed_at"]
    assert history["turns"][0]["interaction_cancelled_at"]
    assert datetime.fromisoformat(cancellation["cancelled_at"]).tzinfo == timezone.utc
    assert datetime.fromisoformat(history["turns"][0]["interaction_consumed_at"]).tzinfo == timezone.utc
    assert "interaction_consumed_by_turn_id" not in history["turns"][0]
    assert store.get_llm_context("sess-choice") == prior_context

    # Cancellation consumes the checkpoint without reopening the old Run.
    reloaded = ExecutionStore(str(storage_dir))
    assert reloaded.validate_interaction_response("sess-choice", None) is None
    next_turn, created = reloaded.create_turn_for_run(
        session_id="sess-choice",
        user_request="继续分析其他问题",
        idempotency_key_hash="post-cancel-request-key-hash",
        request_fingerprint="post-cancel-request-fingerprint",
    )
    assert created is True
    cancelled_turn = reloaded.get_turn("sess-choice", 1)
    assert cancelled_turn["status"] == "awaiting_input"
    assert "interaction_consumed_by_turn_id" not in cancelled_turn
    assert "prior_interaction_cancellation" not in next_turn
    context_after_cancel = reloaded.get_llm_context("sess-choice")
    assert context_after_cancel[: len(prior_context)] == prior_context
    assert context_after_cancel[-1]["role"] == "user"
    assert context_after_cancel[-1]["content"] == "继续分析其他问题"
    assert "interaction_response" not in context_after_cancel[-1]

    # Retrying the cancellation stays idempotent and creates no Run or Turn.
    assert reloaded.cancel_pending_interaction("sess-choice", interaction["id"]) == cancellation
    assert len(reloaded.get_session_history("sess-choice")["turns"]) == 2
    reloaded.complete_turn(
        "sess-choice",
        next_turn["turn_id"],
        {"answer": "done"},
        None,
    )
    later_turn, later_created = reloaded.create_turn_for_run(
        session_id="sess-choice",
        user_request="再次提问",
        idempotency_key_hash="later-request-key-hash",
        request_fingerprint="later-request-fingerprint",
    )
    assert later_created is True
    assert later_turn["turn_id"] == next_turn["turn_id"] + 1
    final_context = reloaded.get_llm_context("sess-choice")
    assert final_context[: len(prior_context)] == prior_context
    assert [entry["content"] for entry in final_context[-2:]] == [
        "继续分析其他问题",
        "再次提问",
    ]


def test_pending_interaction_cancel_rejects_stale_id(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    _seed_choice_checkpoint(store)

    with pytest.raises(InteractionCheckpointError, match="interaction_id"):
        store.cancel_pending_interaction("sess-choice", "stale-interaction")


def test_turn_feedback_is_durable_and_queryable_without_evolution_store(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    store.start_turn("sess-feedback", 1, "run-1", "first")
    store.complete_turn("sess-feedback", 1, {"answer": "ok"}, "done")
    store.start_turn("sess-feedback", 2, "run-2", "second")
    store.fail_turn("sess-feedback", 2, "boom")

    assert store.set_turn_feedback("sess-feedback", 2, "down") is True

    reloaded = ExecutionStore(str(storage_dir))
    failed = reloaded.list_turns_by_status("sess-feedback", "failed")
    assert [turn["turn_id"] for turn in failed] == [2]
    assert failed[0]["feedback"]["vote"] == "down"

    assert reloaded.set_turn_feedback("sess-feedback", 2, "none") is True
    assert "feedback" not in reloaded.get_turn("sess-feedback", 2)


def test_waiting_solver_checkpoint_and_tool_inbox_survive_process_reload(tmp_path):
    storage_dir = tmp_path / "executions"
    store = ExecutionStore(str(storage_dir))
    store.start_turn(
        session_id="sess-solver",
        turn_id=1,
        run_id="run-solver",
        user_request="solve synthetic plan",
    )
    checkpoint = {
        "schema_version": "agent.solver-checkpoint.v1",
        "stage": "waiting_solver",
        "tool_call": {
            "id": "tool-call-solver",
            "name": "solve_synthetic_aaaaaaaa",
            "arguments": {"item_count": 2},
        },
        "state": {
            "run_id": "run-solver",
            "session_id": "sess-solver",
            "current_turn_id": 1,
            "workspace_id": "workspace-synthetic",
            "user_id": "owner-synthetic",
            "user_request": "solve synthetic plan",
            "user_context": {
                "selected_ontology_id": "package-synthetic",
                "selected_ontology_version": "revision-synthetic-1",
            },
            "messages": [
                {"role": "user", "content": "solve synthetic plan"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tool-call-solver",
                            "type": "function",
                            "function": {
                                "name": "solve_synthetic_aaaaaaaa",
                                "arguments": '{"item_count":2}',
                            },
                        }
                    ],
                },
            ],
            "read_documents": {"by_path": {}, "by_resource": {}},
            "results": {},
            "completed_ids": [],
            "failed_ids": [],
            "errors": [],
            "evolution_recall_records": [],
            "debug": False,
            "final_output": None,
        },
    }

    saved = store.save_waiting_solver_checkpoint(
        "sess-solver",
        run_id="run-solver",
        turn_id=1,
        checkpoint=checkpoint,
    )
    assert saved == checkpoint

    result = {
        "status": "completed",
        "authoritative": True,
        "run_id": "run-solver",
        "job_id": "job-solver",
    }
    first = store.put_solver_tool_result(
        "sess-solver",
        run_id="run-solver",
        turn_id=1,
        tool_call_id="tool-call-solver",
        result_digest="sha256:" + "a" * 64,
        result=result,
    )
    replay = store.put_solver_tool_result(
        "sess-solver",
        run_id="run-solver",
        turn_id=1,
        tool_call_id="tool-call-solver",
        result_digest="sha256:" + "a" * 64,
        result=result,
    )
    assert first == replay

    reloaded = ExecutionStore(str(storage_dir))
    durable = reloaded.get_waiting_solver_checkpoint(
        "sess-solver",
        run_id="run-solver",
    )
    assert durable["checkpoint"] == checkpoint
    assert durable["tool_result_inbox"]["result"] == result
    assert durable["tool_result_inbox"]["result_digest"] == "sha256:" + "a" * 64

    with pytest.raises(SolverCheckpointError, match="tool result"):
        reloaded.put_solver_tool_result(
            "sess-solver",
            run_id="run-solver",
            turn_id=1,
            tool_call_id="tool-call-solver",
            result_digest="sha256:" + "b" * 64,
            result={"status": "different"},
        )

    next_checkpoint = dict(checkpoint)
    next_checkpoint["tool_call"] = {
        **checkpoint["tool_call"],
        "id": "tool-call-solver-next",
        "name": "solve_synthetic_bbbbbbbb",
    }
    next_checkpoint["state"] = {
        **checkpoint["state"],
        "messages": [
            {
                **checkpoint["state"]["messages"][1],
                "tool_calls": [
                    {
                        **checkpoint["state"]["messages"][1]["tool_calls"][0],
                        "id": "tool-call-solver-next",
                        "function": {
                            "name": "solve_synthetic_bbbbbbbb",
                            "arguments": '{"item_count":3}',
                        },
                    }
                ],
            }
        ],
    }
    store.save_waiting_solver_checkpoint(
        "sess-solver",
        run_id="run-solver",
        turn_id=1,
        checkpoint=next_checkpoint,
    )
    switched = store.get_waiting_solver_checkpoint(
        "sess-solver",
        run_id="run-solver",
    )
    assert switched["checkpoint"]["tool_call"]["id"] == "tool-call-solver-next"
    assert switched["tool_result_inbox"] is None


def test_start_next_turn_allocates_unique_ids_atomically(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))

    def start(index):
        turn, response = store.start_next_turn(
            session_id="sess-concurrent",
            run_id="run-{}".format(index),
            user_request="synthetic request {}".format(index),
        )
        assert response is None
        return turn

    with ThreadPoolExecutor(max_workers=8) as executor:
        turns = list(executor.map(start, range(16)))

    assert sorted(turn["turn_id"] for turn in turns) == list(range(1, 17))
    assert len({turn["run_id"] for turn in turns}) == 16


@pytest.mark.parametrize(
    "session_ids",
    [
        ("protocol-session", "protocol-session"),
        ("protocol-session-a", "protocol-session-b"),
    ],
)
def test_protocol_run_id_is_atomic_across_concurrent_sessions(
    tmp_path,
    session_ids,
):
    store = ExecutionStore(str(tmp_path / "executions"))
    ready = threading.Barrier(2)

    def submit(session_id):
        ready.wait(timeout=5)
        return _create_reserved_protocol_turn(
            store,
            session_id=session_id,
            external_run_id="external-task-shared",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = list(executor.map(submit, session_ids))

    assert sum(int(created) for _turn, created in admissions) == 1
    turns = store.list_turns_by_protocol("a2a")
    assert len(turns) == 1
    assert turns[0]["protocol_metadata"]["external_run_id"] == "external-task-shared"


def test_protocol_turns_sort_and_deduplicate_mixed_timezone_timestamps(tmp_path):
    storage_dir = tmp_path / "executions"
    records = (
        ("session-later", "run-later", "external-shared", "2026-01-02T10:00:00+00:00"),
        ("session-earlier", "run-earlier", "external-shared", "2026-01-02T11:00:00+02:00"),
        ("session-legacy", "run-legacy", "external-legacy", "2026-01-02T08:30:00"),
    )
    for session_id, run_id, external_id, start_time in records:
        session_dir = storage_dir / session_id
        session_dir.mkdir(parents=True)
        payload = {
            "session_id": session_id,
            "created_at": "2026-01-02T08:00:00",
            "updated_at": "2026-01-02T08:00:00",
            "turns": [
                {
                    "turn_id": 1,
                    "session_id": session_id,
                    "run_id": run_id,
                    "start_time": start_time,
                    "end_time": "2026-01-02T12:00:00+00:00",
                    "status": "success",
                    "protocol_metadata": {
                        "protocol": "a2a",
                        "external_run_id": external_id,
                    },
                }
            ],
        }
        (session_dir / "execution.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

    turns = ExecutionStore(str(storage_dir)).list_turns_by_protocol("a2a")

    assert [turn["run_id"] for turn in turns] == ["run-later", "run-legacy"]
    assert turns[1]["start_time"] == "2026-01-02T08:30:00+00:00"


def test_protocol_run_id_reservation_recognizes_durable_turn_after_reload(tmp_path):
    storage_dir = tmp_path / "executions"
    first = ExecutionStore(str(storage_dir))
    turn, created = _create_reserved_protocol_turn(
        first,
        session_id="protocol-session-a",
        external_run_id="external-task-reload",
    )
    assert created is True

    reloaded = ExecutionStore(str(storage_dir))

    assert reloaded.reserve_protocol_run_id("a2a", "external-task-reload") is None
    session_id, persisted = reloaded.find_turn_by_protocol_id(
        "a2a",
        "external-task-reload",
    )
    assert session_id == "protocol-session-a"
    assert persisted["run_id"] == turn["run_id"]


def test_protocol_run_id_is_atomic_across_store_instances(tmp_path):
    storage_dir = tmp_path / "executions"
    stores = (ExecutionStore(str(storage_dir)), ExecutionStore(str(storage_dir)))
    ready = threading.Barrier(2)
    reservations_ready = threading.Barrier(2)

    def submit(index):
        ready.wait(timeout=5)
        reservation_id = stores[index].reserve_protocol_run_id(
            "a2a",
            "external-task-shared-store",
        )
        reservations_ready.wait(timeout=5)
        if reservation_id is None:
            return None, False
        return stores[index].create_turn_for_reserved_protocol_run(
            protocol="a2a",
            external_run_id="external-task-shared-store",
            reservation_id=reservation_id,
            session_id=f"protocol-store-session-{index}",
            user_request="synthetic request",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        admissions = list(executor.map(submit, range(2)))

    assert sum(int(created) for _turn, created in admissions) == 1
    reloaded = ExecutionStore(str(storage_dir))
    assert len(reloaded.list_turns_by_protocol("a2a")) == 1


def test_protocol_run_id_durable_scan_ignores_stale_store_cache(tmp_path):
    storage_dir = tmp_path / "executions"
    writer = ExecutionStore(str(storage_dir))
    stale_reader = ExecutionStore(str(storage_dir))
    stale_reader.create_turn_for_run(
        session_id="protocol-stale-session",
        user_request="unrelated request",
    )

    _turn, created = _create_reserved_protocol_turn(
        writer,
        session_id="protocol-stale-session",
        external_run_id="external-task-stale-cache",
    )
    assert created is True

    assert stale_reader.reserve_protocol_run_id("a2a", "external-task-stale-cache") is None


def test_protocol_run_id_reservation_is_released_when_durable_write_fails(
    tmp_path,
    monkeypatch,
):
    store = ExecutionStore(str(tmp_path / "executions"))
    reservation_id = store.reserve_protocol_run_id("a2a", "external-task-retry")
    assert reservation_id is not None
    save_session = internal(store).save_session

    def fail_save(*_args, **_kwargs):
        raise OSError("synthetic durable write failure")

    monkeypatch.setattr(store, "_save_session", fail_save)
    with pytest.raises(OSError, match="synthetic durable write failure"):
        store.create_turn_for_reserved_protocol_run(
            protocol="a2a",
            external_run_id="external-task-retry",
            reservation_id=reservation_id,
            session_id="protocol-session-a",
            user_request="synthetic request",
        )

    monkeypatch.setattr(store, "_save_session", save_session)
    retried, retried_created = _create_reserved_protocol_turn(
        store,
        session_id="protocol-session-b",
        external_run_id="external-task-retry",
    )
    assert retried_created is True
    assert retried["session_id"] == "protocol-session-b"


@pytest.mark.parametrize(
    "finish",
    [
        lambda store: store.complete_turn(
            "sess-owner",
            1,
            {"answer": "unexpected"},
            None,
            run_id="run-other",
        ),
        lambda store: store.fail_turn(
            "sess-owner",
            1,
            "unexpected",
            run_id="run-other",
        ),
        lambda store: store.cancel_turn(
            "sess-owner",
            1,
            "unexpected",
            run_id="run-other",
        ),
        lambda store: store.await_turn(
            "sess-owner",
            1,
            run_id="run-other",
        ),
    ],
)
def test_turn_finalization_rejects_another_run(tmp_path, finish):
    store = ExecutionStore(str(tmp_path / "executions"))
    store.start_turn("sess-owner", 1, "run-owner", "synthetic request")

    with pytest.raises(TurnOwnershipError, match="does not belong"):
        finish(store)

    turn = store.get_turn("sess-owner", 1)
    assert turn["run_id"] == "run-owner"
    assert turn["status"] == "running"


def test_run_scoped_turn_lookup_distinguishes_missing_from_wrong_owner(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    store.start_turn("sess-owner", 1, "run-owner", "synthetic request")

    assert store.get_turn("sess-owner", 2, run_id="run-owner") is None
    with pytest.raises(TurnOwnershipError, match="does not belong"):
        store.get_turn("sess-owner", 1, run_id="run-other")
