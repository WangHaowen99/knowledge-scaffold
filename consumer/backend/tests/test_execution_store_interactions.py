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
    store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-1",
        user_request="start",
    )
    store.await_turn(
        session_id=session_id,
        turn_id=1,
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    return interaction


def _seed_input_checkpoint(store: ExecutionStore, session_id: str = "sess-input") -> dict:
    interaction = {
        "id": "interaction_call_input",
        "type": "input",
        "prompt": "请输入值",
    }
    store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-input",
        user_request="start",
    )
    store.await_turn(
        session_id=session_id,
        turn_id=1,
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    return interaction


@pytest.mark.parametrize("value", [5, 5.25, True, {"region": "north"}, [1, 2]])
def test_input_interaction_preserves_standard_json_value_types(tmp_path, value):
    store = ExecutionStore(str(tmp_path / "executions"))
    interaction = _seed_input_checkpoint(store)
    response = {
        "interaction_id": interaction["id"],
        "type": "input",
        "value": value,
    }

    canonical = store.validate_interaction_response("sess-input", response)

    assert canonical == response
    assert type(canonical["value"]) is type(value)


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
    canonical = reloaded_store.validate_interaction_response("sess-choice", response)
    assert canonical == response

    reloaded_store.start_turn(
        session_id="sess-choice",
        turn_id=2,
        run_id="run-2",
        user_request="方案 A",
        interaction_response=response,
    )

    history = reloaded_store.get_session_history("sess-choice")
    assert history["turns"][0]["interaction_response"] == response
    assert history["turns"][0]["interaction_consumed_at"]
    assert history["turns"][1]["status"] == "running"

    with pytest.raises(InteractionCheckpointError, match="no pending interaction"):
        reloaded_store.validate_interaction_response("sess-choice", response)


def test_deterministic_terminal_journal_restores_cache_when_atomic_save_fails(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "executions"
    session_id = "sess-terminal-save-failure"
    store = ExecutionStore(str(storage_dir))
    store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-terminal-save-failure",
        user_request="query",
    )
    events = [
        {
            "event": "agent.v2",
            "data": {
                "event_id": "event-terminal-save-failure",
                "type": "turn.finished",
            },
        }
    ]
    original_save = store._save_session

    def fail_save(_session_id):
        raise OSError("simulated atomic save failure")

    monkeypatch.setattr(store, "_save_session", fail_save)
    with pytest.raises(OSError, match="atomic save failure"):
        store.prepare_deterministic_terminal(
            session_id,
            1,
            status="success",
            final_output={"status": "success"},
            summary=None,
            llm_context=[{"role": "assistant", "content": "ok"}],
            events=events,
        )
    assert store.get_pending_deterministic_terminal(session_id) is None
    assert ExecutionStore(str(storage_dir)).get_pending_deterministic_terminal(
        session_id
    ) is None

    monkeypatch.setattr(store, "_save_session", original_save)
    store.prepare_deterministic_terminal(
        session_id,
        1,
        status="success",
        final_output={"status": "success"},
        summary=None,
        llm_context=[{"role": "assistant", "content": "ok"}],
        events=events,
    )
    monkeypatch.setattr(store, "_save_session", fail_save)
    with pytest.raises(OSError, match="atomic save failure"):
        store.commit_deterministic_terminal(session_id, 1)

    assert store.get_turn(session_id, 1)["status"] == "running"
    assert store.get_pending_deterministic_terminal(session_id) is not None
    assert store.get_llm_context(session_id) == []
    reloaded = ExecutionStore(str(storage_dir))
    assert reloaded.get_turn(session_id, 1)["status"] == "running"
    assert reloaded.get_pending_deterministic_terminal(session_id) is not None


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
        store.start_turn(
            session_id="sess-choice",
            turn_id=2,
            run_id="run-2",
            user_request="ignore checkpoint",
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
    assert history["turns"][0]["status"] == "cancelled"
    assert history["turns"][0]["interaction_response"] == cancellation
    assert history["turns"][0]["interaction_cancelled_at"]
    assert "interaction_context_consumed_at" not in history["turns"][0]
    assert store.get_llm_context("sess-choice") == prior_context

    # A plain message claims the cancellation for one-time model injection.
    reloaded = ExecutionStore(str(storage_dir))
    assert reloaded.validate_interaction_response("sess-choice", None) is None
    resumed_turn = reloaded.start_turn(
        session_id="sess-choice",
        turn_id=2,
        run_id="run-2",
        user_request="继续分析其他问题",
    )
    assert resumed_turn["prior_interaction_cancellation"] == cancellation
    cancelled_turn = reloaded.get_turn("sess-choice", 1)
    assert cancelled_turn["interaction_context_consumed_at"]
    assert cancelled_turn["interaction_context_consumed_by_turn_id"] == 2
    assert reloaded.get_llm_context("sess-choice") == prior_context

    # Retrying stays idempotent and later turns cannot claim the event again.
    assert reloaded.cancel_pending_interaction("sess-choice", interaction["id"]) == cancellation
    reloaded.complete_turn("sess-choice", 2, {"answer": "done"}, None)
    later_turn = reloaded.start_turn(
        session_id="sess-choice",
        turn_id=3,
        run_id="run-3",
        user_request="再次提问",
    )
    assert "prior_interaction_cancellation" not in later_turn
    assert reloaded.get_llm_context("sess-choice") == prior_context


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
