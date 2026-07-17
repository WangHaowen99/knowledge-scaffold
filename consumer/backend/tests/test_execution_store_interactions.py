import pytest

from persistence.execution_store import ExecutionStore, InteractionCheckpointError


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
