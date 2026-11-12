"""V 阶段真实模型发现：助手做完操作提取、正文说「全部完成」，却没释放命令锁。

这一轮结束时执行记录记成「等待」，
节点一直显示进行中，确认生效之后建议条是空的，用户不知道要说「继续」让它收尾。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.flow import model
from app.services.session_host import exec_records
from app.services.workspace import WorkspaceManager


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    project_id = workspaces.create_project("没收尾", owner="employee_0001")["id"]
    with TestClient(app) as client:
        yield client, project_id, workspaces.project_root(project_id)


def _hold_lock(root: Path, command: str) -> None:
    state = root / ".ontology-state"
    state.mkdir(exist_ok=True)
    (state / f"lock-{command}.json").write_text(json.dumps({"command": command, "owner": "engine:e1"}), encoding="utf-8")
    run = root / ".onto" / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "engine.json").write_text(json.dumps({"engine_id": "e1"}), encoding="utf-8")


def test_waiting_step_with_lock_still_held_suggests_continue(env):
    client, project_id, root = env
    record = exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                                command="/ontology-extract-operation", command_name="extract-operation")
    _hold_lock(root, "extract-operation")
    assert exec_records.finish_turn(project_id, record["id"], root)["state"] == "waiting"

    got = client.get(f"/api/v1/projects/{project_id}/flow/suggestions", params={"conversation_id": "default"}).json()
    assert got["primary"] is not None
    assert got["primary"]["action"] == {"type": "send", "text": "继续"}
    # 别的对话不受影响
    other = client.get(f"/api/v1/projects/{project_id}/flow/suggestions", params={"conversation_id": "c2"}).json()
    assert (other["primary"] or {}).get("action") != {"type": "send", "text": "继续"}


def test_step_waiting_for_a_typed_reply_does_not_push_other_commands(env):
    """端到端验证发现：校验停下来让用户打字回复（锁在 confirm），建议条却主推「刷新『操作』」，点了会把校验丢在半路。"""
    client, project_id, root = env
    record = exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                                command="/ontology-validate", command_name="validate")
    _hold_lock(root, "validate")
    lock = root / ".ontology-state" / "lock-validate.json"
    lock.write_text(json.dumps({**json.loads(lock.read_text(encoding="utf-8")), "phase": "confirm"}), encoding="utf-8")
    assert exec_records.finish_turn(project_id, record["id"], root)["state"] == "waiting"

    got = client.get(f"/api/v1/projects/{project_id}/flow/suggestions", params={"conversation_id": "default"}).json()
    assert got["primary"] is None and got["secondary"] == []
    assert got["note"].endswith("在等你回复上面的问题")
    # 别的对话照常给下一步
    other = client.get(f"/api/v1/projects/{project_id}/flow/suggestions", params={"conversation_id": "c2"}).json()
    assert other["note"] is None or "在等你回复" not in other["note"]
