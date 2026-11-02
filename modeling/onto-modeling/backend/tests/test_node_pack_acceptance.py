"""详设第 13 章 P3 第一条验收标准：导入一个节点包后，进度条出现新节点，下一条消息生效。

端到端走接口：管理员上传合成节点包 → 静态检查通过 → 发布到 stable → 项目里启用（插件变更集）→ 流程提示
「下一条消息生效」、生效中的会话不受影响 → 发下一条消息：重新装配并重连，进度条上出现新节点、排在质量检查之后。
假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_node_pack
from tests.test_packs_api import upload, zipped
from tests.test_session_host import FakeEngine, settle


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        project_id = client.post("/api/v1/projects", json={"name": "验收", "goal": "合成场景：图书借阅建模"}).json()["id"]
        yield client, workspaces, manager, project_id, tmp_path
    streams.reset_all()
    builtin.clear_cache()


def track(flow: dict) -> list[str]:
    """进度条上的节点顺序（与前端 state/flow.trackNodes 同一规则：主线、串入、关卡，按连线排；连线分叉时按节点顺序）。"""
    spine = [node for node in flow["nodes"] if node["placement"] in ("spine", "step", "gate")
             and not node["flags"].get("disabled")]
    ids = {node["id"] for node in spine}
    nxt, has_in = {}, set()
    for edge in flow["edges"]:
        if edge["from"] in ids and edge["to"] in ids:
            if nxt.get(edge["from"], edge["to"]) != edge["to"]:
                return [node["id"] for node in spine]  # 同一锚点挂了两个主线节点：按后端给的顺序
            nxt[edge["from"]] = edge["to"]
            has_in.add(edge["to"])
    order = []
    current = next((node["id"] for node in spine if node["id"] not in has_in), None)
    while current and current not in order:
        order.append(current)
        current = nxt.get(current)
    return order + [node["id"] for node in spine if node["id"] not in order]


def test_imported_node_pack_appears_on_the_track_from_the_next_message(env):
    client, workspaces, manager, project_id, tmp_path = env
    root = workspaces.project_root(project_id)

    async def send(text: str):
        await manager.send(project_id, root, text, username="local")
        return await settle(manager, project_id)

    first = asyncio.run(send("你好"))
    engine_before = first.engine

    # 导入：上传、静态检查、发布
    accepted = upload(client, zipped(make_node_pack(tmp_path / "src")))
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"/api/v1/admin/packs/uploads/{accepted.json()['id']}").json()
    assert detail["state"] == "passed", detail["checks"]
    assert {item["id"]: item["result"] for item in detail["checks"]}["node_pack"] == "pass"
    published = client.post(f"/api/v1/admin/packs/uploads/{accepted.json()['id']}/publish", json={"channel": "stable"})
    assert published.status_code == 200, published.text
    card = next(item for item in client.get(f"/api/v1/packs?project_id={project_id}").json()["items"]
                if item["name"] == "naming")
    assert card["category"] == "capability" and card["project_status"] == "available"

    # 启用：作为插件变更集合入；下一条消息之前流程不变，只多一条提示
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"})
    assert enabled.status_code == 201 and enabled.json()["effective"] == "next_message"
    assert enabled.json()["changeset_id"]
    before = client.get(f"/api/v1/projects/{project_id}/flow").json()
    assert "naming.check" not in {node["id"] for node in before["nodes"]}
    assert [notice["kind"] for notice in before["notices"]] == ["assembly_pending"]

    # 下一条消息：重新装配、重连，进度条上出现新节点
    second = asyncio.run(send("继续"))
    assert second.engine is not engine_before
    reconnected = [event for event in second.events if event.get("type") == "session_reconnected"]
    assert reconnected and "assembly" in reconnected[-1]["changes"]
    after = client.get(f"/api/v1/projects/{project_id}/flow").json()
    order = track(after)
    assert "naming.check" in order and order.index("naming.check") == order.index("core.validate") + 1
    node = next(item for item in after["nodes"] if item["id"] == "naming.check")
    assert node["plugin"]["name"] == "naming" and node["state"] == "not_ready"
    assert not after["notices"]
    assert (root / ".claude" / "commands" / "naming-check.md").is_file()
    asyncio.run(manager.shutdown())
