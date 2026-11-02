"""I1：确认生效前双击这一轮的产物卡读到这一轮的版本（AR-03 `?changeset=`、产物卡 `changeset_id`；gaps「B5 合并」第 1 条）。"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.artifacts import cards
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager

FLOW = "diagram.flowchart"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    project_id = workspaces.create_project("产物合成", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        yield client, workspaces, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def _labels(artifact: dict) -> list[str]:
    return [node["label"] for node in artifact["content"]["nodes"]]


def test_card_double_click_reads_this_round(api):
    client, workspaces, pid, root = api
    base = f"/api/v1/projects/{pid}/artifacts/{FLOW}"
    created = client.post(base, json={"key": "borrow-flow", "content": {
        "process": {"name": "借阅流程", "description": "", "roles": [], "inputs": [], "exceptions": [], "handoffs": []},
        "nodes": [{"id": "n1", "type": "start", "label": "读者申请", "x": 120, "y": 60},
                  {"id": "n2", "type": "end", "label": "核对借阅证", "x": 120, "y": 180}],
        "edges": [{"id": "e1", "type": "seq", "from": "n1", "to": "n2", "label": ""}]}})
    assert created.status_code == 201
    rel = created.json()["artifact"]["path"]
    # 助手这一轮在副本里改了这张图、又新建了一张（还没确认）
    copy = changesets.copy_for_turn(workspaces, pid, root)
    text = (copy / rel).read_text(encoding="utf-8")
    (copy / rel).write_text(text.replace("核对借阅证", "核对读者证"), encoding="utf-8")
    shutil.copy(copy / rel, copy / rel.replace("borrow-flow", "renew-flow"))
    record = changesets.observe(workspaces, pid, running=False, conversation_id="default")
    assert record["state"] == "pending_confirm"

    assert _labels(client.get(f"{base}/borrow-flow").json()) == ["读者申请", "核对借阅证"]
    mine = client.get(f"{base}/borrow-flow", params={"changeset": record["id"]}).json()
    assert _labels(mine) == ["读者申请", "核对读者证"] and mine["from_changeset"] == record["id"]
    # 副本里新增的也读得到；只读跟着项目的写入范围锁（待确认时这一轮改到的都锁着）
    added = client.get(f"{base}/renew-flow", params={"changeset": record["id"]}).json()
    assert added["key"] == "renew-flow" and added["from_changeset"] == record["id"] and added["locked"] is True
    assert client.get(f"{base}/renew-flow").json()["code"] == "artifact_not_found"
    # 不存在的变更集：读当前版本
    assert client.get(f"{base}/borrow-flow", params={"changeset": "cs-999"}).json()["from_changeset"] is None

    # 撤回之后再点同一张卡：读当前版本；副本里新增的那张没有了
    assert client.post(f"/api/v1/projects/{pid}/changesets/{record['id']}/discard", json={}).status_code == 200
    after = client.get(f"{base}/borrow-flow", params={"changeset": record["id"]}).json()
    assert _labels(after) == ["读者申请", "核对借阅证"] and after["from_changeset"] is None
    gone = client.get(f"{base}/renew-flow", params={"changeset": record["id"]})
    assert gone.status_code == 404 and gone.json()["code"] == "artifact_not_found"


def test_artifact_card_carries_changeset_slot(api):
    client, workspaces, pid, root = api
    client.post(f"/api/v1/projects/{pid}/artifacts/{FLOW}", json={"key": "flow-a", "fields": {"title": "流程甲"}})
    rel = next(root.glob("ontology/diagrams/flow-a.drawio")).relative_to(root).as_posix()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    result = cards.round_cards(workspaces, pid, {"modified": [rel]}, seed=head)
    assert result and result[0]["changeset_id"] is None
