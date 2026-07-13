"""Tests for user memory management routes."""

from flask import Flask

from agent.memory.filesystem_manager import FileSystemMemoryManager
from api.routes import memories as memories_module
from api.routes.memories import memories_bp
from api.auth.auth import AuthManager


def _build_test_client(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "admin123")
    assert user is not None
    if user.password_change_required:
        user = auth_manager.change_password("admin", "admin123", "RootStrong123!")
    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(memories_bp)

    manager = FileSystemMemoryManager(
        user_id="admin",
        workspace_dir=tmp_path / "memory" / "admin",
    )

    class _FakeOrchestrator:
        def _get_memory_manager(self, user_id):
            assert user_id == "admin"
            return manager

    monkeypatch.setattr(memories_module, "_get_orchestrator", lambda: _FakeOrchestrator())

    client = app.test_client()
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}, manager


def test_list_memories_returns_current_user_memory(tmp_path, monkeypatch):
    client, headers, manager = _build_test_client(tmp_path, monkeypatch)
    manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )

    response = client.get("/agentic/memories", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["memory_enabled"] is True
    assert body["count"] == 1
    assert body["memories"][0]["id"]
    assert "source_uri" not in body["memories"][0]
    assert body["memories"][0]["memory_type"] == "preference"
    assert "中文回答" in body["memories"][0]["content"]


def test_list_memories_filters_by_memory_type(tmp_path, monkeypatch):
    client, headers, manager = _build_test_client(tmp_path, monkeypatch)
    manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    manager.store_memory(
        content="用户画像：用户是质量工程师，关注风险说明。",
        memory_type="profile",
        scope="user",
    )

    response = client.get("/agentic/memories?memory_type=profile", headers=headers)

    assert response.status_code == 200
    body = response.get_json()
    assert body["count"] == 1
    assert body["memories"][0]["memory_type"] == "profile"
    assert "质量工程师" in body["memories"][0]["content"]


def test_memories_require_auth(tmp_path, monkeypatch):
    client, _headers, _manager = _build_test_client(tmp_path, monkeypatch)

    response = client.get("/agentic/memories")

    assert response.status_code == 401


def test_create_memory_uses_user_memory_store(tmp_path, monkeypatch):
    client, headers, manager = _build_test_client(tmp_path, monkeypatch)

    def _fake_store_user_memory(content, *, memory_manager, session_id=None):
        result = memory_manager.store_memory(
            content=content,
            memory_type="preference",
            scope="user",
            app_session_id=session_id,
            source="manual",
        )
        return result.to_dict()

    monkeypatch.setattr(memories_module, "store_user_memory", _fake_store_user_memory)

    response = client.post(
        "/agentic/memories",
        json={"content": "用户偏好：以后回答接口问题时先给结论。"},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.get_json()["memory"]["stored"] is True
    hits = manager.list_memories(limit=10)
    assert len(hits) == 1
    assert "先给结论" in hits[0].content


def test_update_memory_replaces_active_content(tmp_path, monkeypatch):
    client, headers, manager = _build_test_client(tmp_path, monkeypatch)
    manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    memory_id = client.get("/agentic/memories", headers=headers).get_json()["memories"][0]["id"]

    response = client.put(
        f"/agentic/memories/{memory_id}",
        json={"content": "用户偏好：回答接口问题时先给结论。"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["memory"]["id"]
    active_hits = manager.list_memories(limit=10)
    archived_hits = manager.list_memories(include_archived=True, limit=10)
    assert len(active_hits) == 1
    assert "先给结论" in active_hits[0].content
    assert any(hit.archived and "默认使用中文" in hit.content for hit in archived_hits)


def test_delete_memory_archives_active_item(tmp_path, monkeypatch):
    client, headers, manager = _build_test_client(tmp_path, monkeypatch)
    manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    memory_id = client.get("/agentic/memories", headers=headers).get_json()["memories"][0]["id"]

    response = client.delete(f"/agentic/memories/{memory_id}", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "id": memory_id}
    assert manager.list_memories(limit=10) == []
    archived_hits = manager.list_memories(include_archived=True, limit=10)
    assert len(archived_hits) == 1
    assert archived_hits[0].archived is True
