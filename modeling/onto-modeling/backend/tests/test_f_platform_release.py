"""F 第一批第 5 条（命令锁的收尾从根上修）：命令在流程里声明了等回复的阶段（lock_waits，遵守约定的 core）时，一轮正常结束、这次保存过、副本里
  没有没提交的改动、锁不在等回复的阶段，平台代为释放锁，执行记录记完成；旧 core 没有声明的照旧等待。
没保存过（可能被迭代上限截断）、有没提交的改动、一轮出错都不代为释放，留给「继续」。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app import config
from app.api import deps
from app.services import auth
from app.services.flow import model, view
from app.services.session_host import exec_records
from app.services.workspace import WorkspaceManager

CONTINUE = {"type": "send", "text": "继续"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    project_id = workspaces.create_project("收尾", owner="employee_0001")["id"]
    return project_id, workspaces.project_root(project_id)


def _start(project_id: str, root: Path, command: str) -> dict:
    return exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                              command=f"/ontology-{command}", command_name=command)


def _lock(root: Path, command: str, phase: str) -> Path:
    state = root / ".ontology-state"
    state.mkdir(exist_ok=True)
    path = state / f"lock-{command}.json"
    path.write_text(json.dumps({"command": command, "phase": phase, "owner": "engine:e1"}), encoding="utf-8")
    return path


def _save(root: Path, name: str) -> None:
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "add", "ontology"], cwd=root, check=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "commit", "-q", "-m", f"extract: {name} objects"], cwd=root, check=True)


def _finish(project_id: str, record: dict, root: Path) -> dict:
    return exec_records.finish_turn(project_id, record["id"], root, flow=model.load_for(root), wrap_up=True)


def test_platform_releases_lock_when_saved_and_clean(env):
    project_id, root = env
    record = _start(project_id, root, "extract-object")
    lock = _lock(root, "extract-object", "objects")
    _save(root, "门店")
    finished = _finish(project_id, record, root)
    assert finished["state"] == "done" and finished.get("released_by") == "platform"
    assert not lock.exists()
    assert (view.build(root, conversation_id="default")["suggestions"].get("primary") or {}).get("action") != CONTINUE


def test_platform_keeps_lock_with_unsaved_changes(env):
    project_id, root = env
    record = _start(project_id, root, "extract-object")
    lock = _lock(root, "extract-object", "objects")
    _save(root, "门店")
    (root / "ontology" / "object" / "商品.md").write_text("# 商品\n", encoding="utf-8")
    assert _finish(project_id, record, root)["state"] == "waiting"
    assert lock.exists()


def test_platform_keeps_lock_when_nothing_saved_or_waiting_for_reply(env):
    project_id, root = env
    record = _start(project_id, root, "extract-object")
    lock = _lock(root, "extract-object", "objects")
    assert _finish(project_id, record, root)["state"] == "waiting"  # 还没保存过：可能是被迭代上限截断
    _save(root, "门店")
    _lock(root, "extract-object", "confirm")
    assert _finish(project_id, record, root)["state"] == "waiting"
    assert lock.exists()


def test_platform_keeps_lock_for_commands_without_declaration(env):
    project_id, root = env
    record = _start(project_id, root, "extract-object")
    lock = _lock(root, "extract-object", "objects")
    _save(root, "门店")
    flow = model.load_for(root)
    flow["commands"]["extract-object"] = {key: value for key, value in flow["commands"]["extract-object"].items()
                                          if key != "lock_waits"}
    assert exec_records.finish_turn(project_id, record["id"], root, flow=flow, wrap_up=True)["state"] == "waiting"
    assert lock.exists()


def test_platform_keeps_lock_when_turn_failed(env):
    project_id, root = env
    record = _start(project_id, root, "extract-object")
    lock = _lock(root, "extract-object", "objects")
    _save(root, "门店")
    assert exec_records.finish_turn(project_id, record["id"], root, flow=model.load_for(root))["state"] == "waiting"
    assert lock.exists()
