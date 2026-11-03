"""F 第一批（V 修复审查第 1、2 条）：命令这一轮结束时锁还在，分清「等你回复」和「忘了收尾」。

- 第 1 条：一轮结束推给界面的建议条（turns.suggestions → view.build）也要主推「继续」，不只 FL-02；
- 第 2 条：命令停下来等你回复（锁切到 `confirm`）不是没收尾，不主推「继续」。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.api import deps
from app.services import auth
from app.services.flow import model, view
from app.services.session_host import exec_records, turns
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


def _session(project_id: str, root: Path) -> SimpleNamespace:
    return SimpleNamespace(project_id=project_id, conversation_id="default", cwd=root, pending_permissions={})


def _finish(project_id: str, record: dict, root: Path) -> dict:
    return exec_records.finish_turn(project_id, record["id"], root)


def test_turn_end_suggestions_push_continue_like_fl02(env):
    project_id, root = env
    record = _start(project_id, root, "extract-operation")
    _lock(root, "extract-operation", "operation")
    assert _finish(project_id, record, root)["state"] == "waiting"
    fl02 = view.build(root, conversation_id="default")["suggestions"]
    pushed = turns.suggestions(_session(project_id, root))
    assert fl02["primary"]["action"] == CONTINUE
    assert pushed["primary"]["action"] == CONTINUE


def test_waiting_for_reply_is_not_unfinished(env):
    project_id, root = env
    record = _start(project_id, root, "update")
    _lock(root, "update", "confirm")  # /ontology-update 第 3 步展示清单，等你回「确认」
    assert _finish(project_id, record, root)["state"] == "waiting"
    for got in (view.build(root, conversation_id="default")["suggestions"], turns.suggestions(_session(project_id, root))):
        assert (got.get("primary") or {}).get("action") != CONTINUE
        assert all(item.get("action") != CONTINUE for item in got.get("secondary") or [])


def test_waiting_phase_has_a_label(env):
    assert exec_records.phase_label("confirm") == "等你确认"
    assert exec_records.phase_label("analyze") == "影响分析"
