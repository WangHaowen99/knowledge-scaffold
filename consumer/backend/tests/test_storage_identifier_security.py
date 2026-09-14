import pytest
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from utils.identifiers import safe_user_storage_key


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "a\\b", "..", " session "])
def test_chat_store_rejects_unsafe_session_ids(tmp_path, session_id):
    store = ChatStore(str(tmp_path / "chats"))

    with pytest.raises(ValueError, match="session_id"):
        store.create_session(session_id=session_id)

    assert not (tmp_path / "escape").exists()


def test_execution_store_rejects_traversal_before_creating_files(tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))

    with pytest.raises(ValueError, match="session_id"):
        store.get_turn_count("../escape")

    assert not (tmp_path / "escape").exists()


def test_session_storage_rejects_symlink_escape(tmp_path):
    storage_dir = tmp_path / "chats"
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    store = ChatStore(str(storage_dir))
    (storage_dir / "linked").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="outside the storage directory"):
        store.get_session("linked")


def test_user_storage_keys_do_not_collapse_distinct_usernames():
    assert safe_user_storage_key("alice") == "alice"
    assert safe_user_storage_key("a/b") != safe_user_storage_key("a?b")
    assert safe_user_storage_key("Alice") != safe_user_storage_key("alice")
    assert "/" not in safe_user_storage_key("a/b")
