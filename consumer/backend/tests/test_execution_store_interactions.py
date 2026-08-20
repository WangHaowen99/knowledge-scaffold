from concurrent.futures import ThreadPoolExecutor

import pytest

from persistence.execution_store import (
    INTERACTION_CANCELLED_MESSAGE,
    INTERACTION_CANCELLED_NOTICE,
    ExecutionStore,
    InteractionCheckpointError,
)


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
    assert (
        persisted_original["interaction_consumed_by_turn_id"]
        == persisted_answer["turn_id"]
    )
    assert persisted_answer["run_id"] == answer_turn["run_id"]
    assert persisted_answer["turn_id"] == answer_turn["turn_id"]
    answer_message = history["session_history"][-1]
    assert answer_message["role"] == "user"
    assert answer_message["run_id"] == answer_turn["run_id"]
    assert answer_message["turn_id"] == answer_turn["turn_id"]
    assert answer_message["interaction_response"] == response
    assert not any(
        message.get("role") == "tool"
        for message in history["session_history"]
    )


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
    assert (
        history["turns"][0]["interaction_consumed_by_turn_id"]
        == history["turns"][1]["turn_id"]
    )


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
    assert (
        history["turns"][0]["interaction_consumed_by_turn_id"]
        == answer_turn["turn_id"]
    )
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
