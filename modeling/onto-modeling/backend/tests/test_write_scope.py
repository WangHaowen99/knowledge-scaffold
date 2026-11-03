"""助手运行中写入范围只读（详设 4.3、14#13；方案 C10、D11；契约 2.4 write_scope_locked、CS-06、EP-03）。

- 范围 = 正在执行的节点声明的 produces/writes ∪ 副本里已改的实体 ∪ 决策卡改动预览列出的条目，
  持续到确认生效或撤回；
- 所有写入方都经写入事务，统一 423（details 是 ScopeLock）；MCP 回 PROJECT_EDITING_BUSY 并附实体列表；
  回退在有没确认的变更集时一律 423；Studio 租约与整项目忙的判断取消；
- 插件产物、Studio、文件读接口都标出锁；
- 守卫一侧：state_helper acquire 把节点的写入范围写进 `.onto/run/active.json`，release 时去掉。

测试数据全部为合成内容。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.artifacts import service as artifact_service
from app.kernel.changesets import service
from app.main import app
from app.services import auth, streams
from app.services.onto_mcp import workspace as mcp
from app.services.onto_mcp.errors import McpError
from app.services.workspace import WorkspaceError, WorkspaceManager
from tests.test_changesets import agent_write, main_write, obj
from tests.test_event_streams import buffered


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(service, "_activity_provider", None)
        project_id = workspaces.create_project("写入范围", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
        main_write(root, "ontology/object/客户.md", obj("客户", "合成客户"))
        yield client, workspaces, project_id, root
    streams.reset_all()


def pending_change(workspaces, project_id, root, rel="ontology/object/订单.md", text=None):
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, rel, text or obj("订单", "助手改了说明"), commit="agent: 改订单")
    return service.observe(workspaces, project_id, running=False, conversation_id="default")


def running(node_id: str | None = None, affected=None):
    record = {"id": "ex-1", "node": {"id": node_id, "version": "core@0.2.0", "title": "对象提取"},
              "started_at": 1.0} if node_id else None
    return lambda pid: {"running": True, "conversation_id": "default", "exec": record, "affected": affected or []}


def test_pending_changeset_locks_changed_entities_until_closed(env):
    client, workspaces, project_id, root = env
    record = pending_change(workspaces, project_id, root)
    with pytest.raises(WorkspaceError) as exc:
        workspaces.write_file(project_id, "ontology/object/订单.md", obj("订单", "你在网页上改"), author="employee_0001")
    assert exc.value.status == 423 and exc.value.code == "write_scope_locked"
    lock = exc.value.details
    assert lock["state"] == "pending_confirm" and lock["changeset_id"] == record["id"]
    assert [ref["id"] for ref in lock["entries"]] == ["object:订单"] and lock["reason_text"]
    # 文件内容没动
    assert "合成订单" in (root / "ontology/object/订单.md").read_text(encoding="utf-8")
    # 范围外照常保存，进主线
    saved = workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "范围外照常改"),
                                  author="employee_0001")
    assert saved["commit"]
    # 接口：整份锁、按条目问、423 的错误体
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["changeset_id"] == record["id"]
    response = client.put(f"/api/v1/projects/{project_id}/files/content",
                          json={"path": "ontology/object/订单.md", "content": "x", "base_revision": ""})
    assert response.status_code == 423 and response.json()["code"] == "write_scope_locked"
    assert response.json()["details"]["changeset_id"] == record["id"]
    # 撤回之后解锁
    service.discard(workspaces, project_id, record["id"], actor="employee_0001")
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["state"] == "none"
    workspaces.write_file(project_id, "ontology/object/订单.md", obj("订单", "撤回后能改了"), author="employee_0001")


def test_running_node_locks_its_declared_scope(env, monkeypatch):
    _client, workspaces, project_id, root = env
    monkeypatch.setattr(service, "_activity_provider", running("core.model"))
    lock = service.scope_lock(workspaces, project_id)
    assert lock["state"] == "assistant_running" and lock["node"]["id"] == "core.model"
    assert "ontology/object/*" in lock["paths"] and "对象提取" in lock["reason_text"]
    with pytest.raises(WorkspaceError) as exc:
        workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "x"), author="employee_0001")
    assert exc.value.status == 423
    # 节点范围外（研究报告）照常
    workspaces.write_file(project_id, "notes/备忘.md", "合成备忘\n", author="employee_0001")
    # 助手没在执行有声明的节点（自由对话）、也没改动：什么都不锁
    monkeypatch.setattr(service, "_activity_provider", running(None))
    workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "自由对话时能改"), author="employee_0001")


def test_decision_preview_entries_join_the_scope(env, monkeypatch):
    _client, workspaces, project_id, _root = env
    affected = [{"kind": "object", "id": "object:客户", "key": "客户", "name": "客户"}]
    monkeypatch.setattr(service, "_activity_provider", running(None, affected))
    lock = service.scope_lock(workspaces, project_id)
    assert lock["entries"] == affected
    with pytest.raises(WorkspaceError):
        workspaces.write_file(project_id, "ontology/object/客户.md", obj("客户", "x"), author="employee_0001")


def test_revert_is_refused_while_a_changeset_is_open(env):
    _client, workspaces, project_id, root = env
    first = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=root, capture_output=True,
                           text=True).stdout.strip()
    pending_change(workspaces, project_id, root)
    with pytest.raises(WorkspaceError) as exc:
        workspaces.revert_to(project_id, first, author="employee_0001")
    assert exc.value.code == "write_scope_locked"


def test_merge_itself_is_not_blocked_by_its_own_scope(env):
    _client, workspaces, project_id, root = env
    record = pending_change(workspaces, project_id, root)
    assert service.merge(workspaces, project_id, record["id"], actor="employee_0001")["state"] == "merged"


def test_mcp_busy_lists_entities_and_no_longer_blocks_the_whole_project(env, monkeypatch):
    _client, workspaces, project_id, root = env
    pending_change(workspaces, project_id, root)
    revision = mcp.revision(root)
    with pytest.raises(McpError) as exc:
        mcp.execute(workspaces, project_id, "update_ontology_object",
                    {"object_id": "object:订单", "expected_revision": revision,
                     "metadata": {"description": "MCP 改"}}, "employee_0001")
    assert exc.value.code in ("PROJECT_EDITING_BUSY", "SUBMISSION_BLOCKED")
    issues = exc.value.details.get("issues") or [exc.value.details]
    busy = next(item for item in issues if item.get("code", "PROJECT_EDITING_BUSY") == "PROJECT_EDITING_BUSY")
    assert [ref["id"] for ref in busy["entities"]] == ["object:订单"]
    # 范围外的条目：助手在跑也照常写（整项目忙的判断取消）
    monkeypatch.setattr(service, "_activity_provider", running(None))
    out = mcp.execute(workspaces, project_id, "update_ontology_object",
                      {"object_id": "object:客户", "expected_revision": mcp.revision(root),
                       "metadata": {"description": "MCP 改客户"}}, "employee_0001")
    assert out["changed_paths"] == ["ontology/object/客户.md"]


def test_studio_and_files_show_the_lock_and_artifacts_use_it(env):
    client, workspaces, project_id, root = env
    pending_change(workspaces, project_id, root)
    base = f"/api/v1/projects/{project_id}"
    detail = client.get(f"{base}/studio/entries/object:订单").json()
    assert detail["lock"]["state"] == "pending_confirm"
    assert client.get(f"{base}/studio/entries/object:客户").json()["lock"] is None
    rows = {row["id"]: row["locked"] for row in client.get(f"{base}/studio/entries").json()["items"]}
    assert rows == {"object:订单": True, "object:客户": False}
    nodes = {node["id"]: node["locked"] for node in client.get(f"{base}/studio/graph").json()["nodes"]}
    assert nodes["object:订单"] is True and nodes["object:客户"] is False
    content = client.get(f"{base}/files/content", params={"path": "ontology/object/订单.md"}).json()
    assert content["lock"]["state"] == "pending_confirm"
    # 插件产物按同一份锁（B4c 留下的登记点）
    assert artifact_service._scope_lock(root)["state"] == "pending_confirm"
    # Studio 草稿提交到范围内的条目：423
    client.put(f"{base}/studio/entries/object:订单/draft", json={"content": obj("订单", "Studio 改")})
    submitted = client.post(f"{base}/studio/entries/object:订单/submit", json={})
    assert submitted.status_code == 423 and submitted.json()["code"] == "write_scope_locked"


def test_scope_updates_are_pushed_on_the_project_stream(env):
    _client, workspaces, project_id, root = env
    record = pending_change(workspaces, project_id, root)
    scopes = [event["data"] for event in buffered(project_id, "scope.updated")]
    assert scopes and scopes[-1]["state"] == "pending_confirm" and scopes[-1]["changeset_id"] == record["id"]
    changes = [event["data"] for event in buffered(project_id, "changeset.updated")]
    assert changes[-1]["state"] == "pending_confirm"


def test_state_helper_records_the_running_nodes_scope(env):
    _client, workspaces, project_id, root = env
    copy = service.copy_for_turn(workspaces, project_id, root)
    helper = [ "python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    run = lambda *args: subprocess.run([*helper, *args], cwd=copy, capture_output=True, text=True, check=True)
    run("acquire", "extract-object", "objects")
    active = json.loads((copy / ".onto" / "run" / "active.json").read_text(encoding="utf-8"))
    assert active["commands"]["extract-object"]["node"] == "core.model"
    assert active["commands"]["extract-object"]["allow"] == ["ontology/object/*", "ontology/operation/*", "ontology/rule/*",
                                                             "ontology/logic/*", "ontology/metric/*"]
    assert "protect" not in active["commands"]["extract-object"]  # 全量模式不按段收窄
    run("acquire", "overview", "start")  # 不属于任何节点的命令：守卫不收窄
    assert json.loads((copy / ".onto/run/active.json").read_text())["commands"]["overview"]["node"] is None
    run("release", "overview")
    run("release", "extract-object")
    assert not (copy / ".onto" / "run" / "active.json").exists()
