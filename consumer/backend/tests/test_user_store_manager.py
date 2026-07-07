import json

from agentic.stores import UserStoreManager
from agentic.workspaces import create_workspace


def _set_session_updated_at(storage_dir, session_id, updated_at):
    snapshot_path = storage_dir / session_id / "snapshot.json"
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["updated_at"] = updated_at
    snapshot_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_user_store_manager_prunes_old_sessions_and_execution_records(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    manager = UserStoreManager()

    chat_store = manager.get_chat_store("alice", "demo")
    execution_store = manager.get_execution_store("alice", "demo")

    for index in range(3):
        session_id = "session-{}".format(index)
        chat_store.create_session(session_id=session_id, title=session_id)
        execution_store.start_turn(
            session_id=session_id,
            turn_id=1,
            run_id="run-{}".format(index),
            user_request="hello",
        )
        _set_session_updated_at(
            tmp_path
            / ".onto-runtime"
            / "workspaces"
            / "demo"
            / "runtime"
            / "users"
            / "alice"
            / "chats",
            session_id,
            "2026-05-{:02d}T00:00:00+00:00".format(index + 1),
        )

    deleted = manager.prune_sessions("alice", "demo", max_sessions=2)

    assert deleted == ["session-0"]
    assert [session.id for session in chat_store.list_sessions()] == ["session-2", "session-1"]
    assert execution_store.get_recent_turns("session-0") == []
    assert execution_store.get_recent_turns("session-1")
