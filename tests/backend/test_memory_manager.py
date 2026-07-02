"""Tests for OpenViking memory manager behavior."""

from pathlib import Path

from agentic.memory.openviking_manager import OpenVikingMemoryManager


class _FakeOpenVikingClient:
    last_instance = None

    def __init__(self, path: str) -> None:
        self.path = path
        self.created_sessions = 0
        self.files = {}
        self.search_payload = {"resources": []}
        _FakeOpenVikingClient.last_instance = self

    def initialize(self) -> None:
        return

    def session_exists(self, session_id: str) -> bool:
        return session_id == "existing-session"

    def create_session(self):
        self.created_sessions += 1
        return {"session_id": "ov-session-{}".format(self.created_sessions)}

    def mkdir(self, uri: str):
        return {"uri": uri}

    def add_message(self, session_id: str, role: str, content: str):
        return {"session_id": session_id, "role": role, "content": content}

    def commit_session(self, session_id: str):
        return {"session_id": session_id}

    def add_resource(self, path: str, to: str, **kwargs):
        del kwargs
        file_name = Path(path).name
        self.files["{}/{}".format(to, file_name)] = Path(path).read_text(encoding="utf-8")
        return {"uri": "{}/{}".format(to, file_name)}

    def search(self, **kwargs):
        del kwargs
        return self.search_payload

    def read(self, uri: str):
        return self.files.get(uri, "")


def test_store_memory_routes_preference_to_expected_directory(tmp_path):
    manager = OpenVikingMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "ov",
        client_cls=_FakeOpenVikingClient,
    )

    result = manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
        app_session_id="sess-1",
    )

    assert result.stored is True
    assert result.scope == "user"
    assert "/preferences/" in result.source_uri
    assert result.created_at
    assert result.updated_at
    assert result.to_dict()["created_at"] == result.created_at
    stored_note = _FakeOpenVikingClient.last_instance.files[result.source_uri]
    assert "stored_at:" in stored_note
    assert "created_at:" in stored_note
    assert "updated_at:" in stored_note
    assert "tags" not in stored_note
    assert "confidence" not in stored_note
    assert "用户偏好：默认使用中文回答。" in stored_note


def test_recall_memory_reads_frontmatter_and_returns_classification(tmp_path):
    manager = OpenVikingMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "ov",
        client_cls=_FakeOpenVikingClient,
    )
    store_result = manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
        app_session_id="sess-1",
    )

    _FakeOpenVikingClient.last_instance.search_payload = {
        "resources": [
            {
                "uri": store_result.source_uri,
                "score": 0.91,
                "overview": "默认使用中文回答",
                "abstract": "中文回答偏好",
            }
        ]
    }

    hits = manager.recall_memory(
        query="中文回答",
        scope="user",
        k=5,
        ov_session_id="ov-session-1",
    )

    assert len(hits) == 1
    assert hits[0].memory_type == "preference"
    assert hits[0].scope == "user"
    assert "中文回答" in hits[0].content
    assert hits[0].created_at == store_result.created_at
    assert hits[0].updated_at == store_result.updated_at


def test_archive_memory_marks_openviking_note_archived(tmp_path):
    manager = OpenVikingMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "ov",
        client_cls=_FakeOpenVikingClient,
    )
    store_result = manager.store_memory(
        content="以后使用英文回答计算处理成本的问题。",
        memory_type="preference",
        scope="user",
        app_session_id="sess-1",
    )

    assert manager.archive_memory(source_uri=store_result.source_uri, reason="updated preference") is True
    stored_note = _FakeOpenVikingClient.last_instance.files[store_result.source_uri]
    assert 'archived: true' in stored_note
    assert "以后使用英文回答计算处理成本的问题。" in stored_note
