from agent.core.orchestrator import AgenticOrchestrator
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from persistence.session_summary_index import SessionSummaryIndex


def test_protocol_preflight_uses_summary_metadata_and_turn_count_without_history_replay(
    tmp_path,
    monkeypatch,
):
    summary_index = SessionSummaryIndex(tmp_path / "session-summary-index.json")
    chat_store = ChatStore(
        str(tmp_path / "chats"),
        summary_index=summary_index,
    )
    execution_store = ExecutionStore(
        str(tmp_path / "executions"),
        summary_index=summary_index,
    )
    chat_store.create_session(
        session_id="session-preflight",
        metadata={
            "selected_ontology_id": "ontology-demo",
            "selected_ontology_version": "v1",
            "model_profile_id": "profile-demo",
        },
    )
    execution_store.start_turn(
        "session-preflight",
        1,
        "run-existing",
        "synthetic existing turn",
    )
    orchestrator = AgenticOrchestrator(
        chat_store=chat_store,
        execution_store=execution_store,
    )
    monkeypatch.setattr(
        chat_store,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("protocol preflight must not replay chat history")
        ),
    )
    monkeypatch.setattr(
        execution_store,
        "get_session_history",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("protocol preflight must not copy execution history")
        ),
    )
    monkeypatch.setattr(
        execution_store,
        "_load_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("turn count must come from the ready summary index")
        ),
    )

    request, ontology_id, version, profile_id = (
        orchestrator._build_protocol_preflight_request(
            user_request="run operation",
            user_context={},
            arguments={},
            session_id="session-preflight",
            user_id="operator-a",
            workspace_id="workspace-a",
        )
    )

    assert request is not None
    assert request.runtime_context.turn_id == 2
    assert ontology_id == "ontology-demo"
    assert version == "v1"
    assert profile_id == "profile-demo"
