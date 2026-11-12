"""I1：偏好（A-08）、通知列表与标为已读（A-13、A-14）、用户流 notification（EU-01）与几处通知来源。"""

import asyncio
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import packs as pack_routes
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, notifications, streams
from app.services.agent_sessions import SessionManager
from app.services.workspace import WorkspaceManager
from tests.test_event_streams import subscription
from tests.test_session_host import FakeEngine, result, settle


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    streams.reset_all()
    with TestClient(app) as client:
        yield client, workspaces
    streams.reset_all()


def user_events(username: str, event_type: str) -> list[dict]:
    channel = streams.user_channel(username)

    async def scenario():
        with subscription(channel, f"{streams.EPOCH}.0") as (_subscriber, replay, _reset):
            return replay

    return [item for item in asyncio.run(scenario()) if item["type"] == event_type]


def test_preferences_roundtrip_and_validation(api):
    client, _ = api
    prefs = client.get("/api/v1/me/preferences").json()
    assert prefs == notifications.DEFAULT_PREFERENCES
    body = {"theme": "dark", "notifications": {**prefs["notifications"], "l4_new_suspension": True, "extra": 1}}
    saved = client.put("/api/v1/me/preferences", json=body).json()
    assert saved["theme"] == "dark" and saved["notifications"]["l4_new_suspension"] is True
    assert "extra" not in saved["notifications"] and client.get("/api/v1/me/preferences").json() == saved
    # 整体替换：没给的回到默认
    assert client.put("/api/v1/me/preferences", json={"theme": "light"}).json() == notifications.DEFAULT_PREFERENCES
    bad = client.put("/api/v1/me/preferences", json={"theme": "pink"})
    assert bad.status_code == 400 and bad.json()["details"]["fields"] == {"theme": "enum"}
    bad = client.put("/api/v1/me/preferences", json={"notifications": {"system": "yes"}})
    assert bad.status_code == 400 and "notifications.system" in bad.json()["details"]["fields"]


def test_notifications_list_page_and_read(api):
    client, _ = api
    assert client.get("/api/v1/me/notifications").json() == {"items": [], "next_cursor": None, "unread_count": 0}
    for index in range(25):
        notifications.notify("", "publish_done", f"发布完成 {index}", "合成项目")
    page = client.get("/api/v1/me/notifications").json()
    assert len(page["items"]) == 20 and page["next_cursor"] and page["unread_count"] == 25
    first = page["items"][0]
    assert first["title"] == "发布完成 24" and first["read"] is False and first["created_at"].endswith("Z")
    assert set(first) == {"id", "kind", "title", "body", "project_id", "project_name", "action", "created_at", "read"}
    rest = client.get("/api/v1/me/notifications", params={"cursor": page["next_cursor"]}).json()
    assert len(rest["items"]) == 5 and rest["next_cursor"] is None
    assert client.post("/api/v1/me/notifications/read", json={"ids": [first["id"]]}).json() == {"unread_count": 24}
    assert len(client.get("/api/v1/me/notifications", params={"unread_only": True, "limit": 50}).json()["items"]) == 24
    assert client.post("/api/v1/me/notifications/read", json={"all": True}).json() == {"unread_count": 0}
    assert client.post("/api/v1/me/notifications/read", json={}).status_code == 400
    # 用户流同步推（本地模式是空用户名那个频道）
    assert user_events("", "notification")[-1]["data"]["title"] == "发布完成 24"


def test_retention_and_grouping(api, monkeypatch):
    notifications.notify("employee_0001", "step_done", "一步跑完", "甲", group="p:c")
    notifications.notify("employee_0001", "step_done", "一步跑完", "乙", group="p:c")
    items = notifications.list_notifications("employee_0001")
    assert [item["body"] for item in items] == ["乙"]  # 同一对话还没读的同类只留最新一条
    later = notifications.retention_seconds() + 10
    real = notifications.time.time
    monkeypatch.setattr(notifications.time, "time", lambda: real() + later)
    assert notifications.list_notifications("employee_0001") == []


def test_turn_end_question_and_error_notify_the_owner(api, monkeypatch):
    client, workspaces = api
    project_id = workspaces.create_project("通知合成", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [[result()]], []

    async def run():
        await manager.send(project_id, root, "合成的一句话", username="employee_0001")
        await settle(manager, project_id)
        session = manager.get(project_id)
        session.broadcast({"type": "session_error", "error": "合成错误", "error_id": "e-1", "retries": 0})
        await manager.shutdown()

    asyncio.run(run())
    items = notifications.list_notifications("employee_0001")
    kinds = [item["kind"] for item in items]
    assert kinds == ["session_error", "step_done"]
    assert items[1]["project_id"] == project_id and items[1]["project_name"] == "通知合成"
    assert items[1]["action"]["page"] == "wb-l2"
    assert [event["data"]["kind"] for event in user_events("employee_0001", "notification")] == ["step_done", "session_error"]


def test_rejected_upload_notifies_the_uploader(api, monkeypatch):
    client, _ = api
    record = {"id": "up-1", "name": "sticky-notes", "version": "1.0.0", "uploaded_by": "employee_0002"}
    monkeypatch.setattr(pack_routes, "_uploads", lambda: types.SimpleNamespace(reject=lambda *_a, **_k: record))
    monkeypatch.setattr(pack_routes, "_upload_view", lambda item: item)
    assert client.post("/api/v1/admin/packs/uploads/up-1/reject", json={"reason": "缺少说明"}).status_code == 200
    item = notifications.list_notifications("employee_0002")[0]
    assert item["kind"] == "upload_rejected" and item["body"] == "sticky-notes 1.0.0：缺少说明"
    assert item["action"] == {"label": "查看", "page": "plugin-upload", "params": {"upload": "up-1"}}
    # 自己退回自己的不通知
    record["uploaded_by"] = "local"
    client.post("/api/v1/admin/packs/uploads/up-1/reject", json={"reason": "再退一次"})
    assert notifications.list_notifications("") == []
