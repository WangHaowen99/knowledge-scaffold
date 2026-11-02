"""命令与执行记录接口（契约 3.11 S-15～S-20、EP-09、EP-10）：页面发命令返回可追踪的任务对象，
进度经项目流推送；执行记录列表、续跑、放弃。假引擎，不起真实 CLI；测试数据全部为合成内容。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.session_host import command_tasks, exec_records
from app.services.workspace import WorkspaceManager
from tests.test_session_host import EXEC, FakeEngine, lock, release, result

TASK = {"task_id", "origin", "title", "state", "percent", "step_label", "conversation_id", "message_id", "exec_id",
        "started_at", "finished_at", "result"}
COMMAND_ITEM = {"id", "label", "description", "command", "pack", "node_id", "args_schema", "available",
                "unavailable_reason"}


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)  # 启动时不补跑备份（打包包仓库要几十秒）
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"name": "页面命令", "goal": "合成场景：图书借阅"}).json()["id"]
        yield client, manager, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def wait(predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待超时")


def test_catalog_lists_assembled_commands_by_business_group(api):
    client, _, project_id, _ = api
    catalog = client.get(f"/api/v1/projects/{project_id}/commands/catalog").json()
    items = [item for group in catalog["groups"] for item in group["items"]]
    assert catalog["total"] == len(items) > 5
    assert all(set(item) == COMMAND_ITEM for item in items)
    by_command = {item["command"]: item for item in items}
    assert by_command["/ontology-extract-object"]["node_id"] == "core.model"
    assert by_command["/ontology-research"]["label"] and by_command["/ontology-research"]["pack"] == "core"
    assert catalog["groups"][0]["label"] == "建模"


def test_page_command_returns_task_and_tracks_exec_until_done(api):
    client, manager, project_id, _ = api
    FakeEngine.scripts = [[lock("validate", "start"),
                           {"type": "assistant_message", "blocks": [{"kind": "text", "text": "开始校验"}]},
                           release("validate"), result()]]
    response = client.post(f"/api/v1/projects/{project_id}/commands", json={
        "command": "/ontology-validate", "args": {"scope": "全部", "keys": ["s-01", "s-02"]}, "text": "只看规则",
        "origin": {"page": "reports", "label": "报告"}})
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"task", "conversation_id", "message_id"} and set(body["task"]) == TASK
    task_id = body["task"]["task_id"]
    assert wait(lambda: FakeEngine.sent)[0] == '/ontology-validate scope=全部 keys=["s-01", "s-02"]\n\n只看规则'
    done = wait(lambda: next((task for task in command_tasks.query(project_id, states=["done"])
                              if task["task_id"] == task_id), None))
    assert done["exec_id"] and done["result"]["summary"].startswith("「校验与走查」做完了")
    assert exec_records.get(project_id, done["exec_id"])["task_id"] == task_id
    assert client.get(f"/api/v1/projects/{project_id}/commands/tasks?origin_page=reports").json()["items"] == []
    finished = client.get(f"/api/v1/projects/{project_id}/commands/tasks?state=done").json()["items"]
    assert [task["task_id"] for task in finished] == [task_id]
    # 对话里的用户消息带来源页面（EC-10 origin）
    messages = client.get(f"/api/v1/projects/{project_id}/conversations/{body['conversation_id']}/messages").json()
    user = next(item for item in messages["items"] if item["type"] == "message.user")
    assert user["data"]["origin"] == {"kind": "page", "page": "reports", "label": "报告", "task_id": task_id}
    assert user["data"]["command"] == "/ontology-validate"
    assert user["data"]["message_id"] == body["message_id"]


@pytest.mark.parametrize("body,code", [
    ({"command": "/ontology-validate"}, "invalid_argument"),  # 缺 origin
    ({"origin": {"page": "reports", "label": "报告"}}, "invalid_argument"),  # action、command 都没给
    ({"command": "/ontology-not-installed", "origin": {"page": "x", "label": "x"}}, "pack_not_enabled"),
    ({"action": {"pack": "trace", "page": "scenarios", "action_id": "run"}, "origin": {"page": "x", "label": "x"}},
     "pack_not_enabled"),
])
def test_page_command_errors(api, body, code):
    client, _, project_id, _ = api
    response = client.post(f"/api/v1/projects/{project_id}/commands", json=body)
    assert response.json()["code"] == code


def test_exec_list_resume_and_abandon(api):
    client, manager, project_id, root = api
    FakeEngine.scripts = [[lock("extract-object", "source"),
                           {"type": "assistant_message", "blocks": [{"kind": "text", "text": "读资料"}]}, result()]]
    client.post(f"/api/v1/projects/{project_id}/conversations/default/messages", json={"text": "/ontology-extract-object"})
    record = wait(lambda: next(iter(exec_records.query(project_id, ["waiting"])), None))
    listing = client.get(f"/api/v1/projects/{project_id}/execs?state=waiting").json()
    assert [item["id"] for item in listing["items"]] == [record["id"]] and set(listing["items"][0]) == EXEC
    assert listing["next_cursor"] is None
    assert client.get(f"/api/v1/projects/{project_id}/execs?state=bogus").json()["code"] == "invalid_argument"

    # 还在等的执行不能续跑（没中断）
    not_yet = client.post(f"/api/v1/projects/{project_id}/execs/{record['id']}/resume", json={})
    assert not_yet.status_code == 409 and not_yet.json()["code"] == "exec_not_resumable"
    # 像会话被回收那样：等待中的执行记为中断
    interrupted = exec_records.interrupt(project_id, record["id"], "recycled", root=root)
    assert interrupted["state"] == "interrupted"
    manager.get(project_id).exec_id = None

    FakeEngine.scripts = [[release("extract-object"), result()]]
    resumed = client.post(f"/api/v1/projects/{project_id}/execs/{record['id']}/resume", json={})
    assert resumed.status_code == 202, resumed.text
    body = resumed.json()
    assert body["exec"]["state"] == "running" and body["conversation_id"] == "default" and body["message_id"]
    assert wait(lambda: len(FakeEngine.sent) == 2 and FakeEngine.sent)[-1].startswith(
        "/ontology-extract-object 继续上次没做完的「对象提取」")
    wait(lambda: (exec_records.get(project_id, record["id"]) or {}).get("state") == "done")

    gone = client.post(f"/api/v1/projects/{project_id}/execs/{record['id']}/abandon")
    assert gone.status_code == 409 and gone.json()["details"]["reason"] == "already_finished"
    assert client.post(f"/api/v1/projects/{project_id}/execs/ex-missing/abandon").json()["code"] == "exec_not_found"


def test_abandon_interrupted_exec_updates_resume_card(api):
    client, manager, project_id, root = api
    from app.services.flow import model
    from app.services.session_host import recovery

    record = exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                                command="/ontology-research", command_name="research")
    recovery.sweep_execs(deps.workspaces)  # 像重启后那样改判中断，放续跑卡
    response = client.post(f"/api/v1/projects/{project_id}/execs/{record['id']}/abandon")
    assert response.status_code == 200 and response.json()["exec"]["state"] == "abandoned"
    items = client.get(f"/api/v1/projects/{project_id}/conversations/default/messages").json()["items"]
    card = next(item for item in items if item["type"] == "card" and item["data"]["card_type"] == "resume")
    # S-06 给卡片的最新状态：已放弃、不能再续跑；对话里有一行系统提示
    assert card["data"]["status"]["label"] == "已放弃" and card["data"]["resumable"] is False
    notice = [item for item in items if item["type"] == "notice"][-1]["data"]
    assert notice["kind"] == "info" and "已放弃「资料分析」" in notice["text"] and notice["align"] == "center"


def test_conversation_changes_reach_project_stream(api):
    client, _, project_id, _ = api
    import asyncio

    channel = streams.project_channel(project_id)

    def updates():
        async def replay():
            subscriber, events, _ = channel.open(f"{streams.EPOCH}.0", asyncio.get_running_loop())
            channel.unsubscribe(subscriber)
            return events

        return [item["data"] for item in asyncio.run(replay()) if item["type"] == "conversation.updated"]

    created = client.post(f"/api/v1/projects/{project_id}/conversations", json={"title": "第二条"}).json()
    assert updates()[-1]["conversation"]["id"] == created["id"] and updates()[-1]["deleted"] is False
    renamed = client.patch(f"/api/v1/projects/{project_id}/conversations/{created['id']}", json={"title": "改名"})
    assert renamed.status_code == 200 and updates()[-1]["conversation"]["title"] == "改名"
    client.delete(f"/api/v1/projects/{project_id}/conversations/{created['id']}")
    assert updates()[-1] == {"conversation": {**updates()[-2]["conversation"]}, "deleted": True}


def test_catalog_maps_node_pack_entry_command_to_its_node(tmp_path, monkeypatch):
    """节点包只在节点上写 entry.command、没有 commands 映射（V 阶段：/naming-check 落在「其他」，pack 记成 core）。"""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    (tmp_path / ".claude" / "commands" / "naming-check.md").write_text("检查 $ARGUMENTS\n", encoding="utf-8")
    flow = {"nodes": [{"id": "naming.check", "title": "命名一致性检查", "pack": "naming", "section": "required",
                       "entry": {"command": "/naming-check"}, "actions": [{"label": "运行命名检查", "command": "/naming-check"}]}],
            "commands": {}}
    monkeypatch.setattr(session_routes.turns, "_flow", lambda root: flow)
    catalog = session_routes.command_catalog(tmp_path)
    item = catalog["groups"][0]["items"][0]
    assert catalog["groups"][0]["id"] == "required"
    assert (item["command"], item["node_id"], item["pack"], item["label"]) == ("/naming-check", "naming.check", "naming", "运行命名检查")
