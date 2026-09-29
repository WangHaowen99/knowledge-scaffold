"""删除项目的忙碌守卫：进行中的项目返回 409 PROJECT_BUSY，空闲才真正删除。

直接调路由函数，用替身顶掉 workspaces 与会话管理器，避开建工作区（需要 symlink 权限）。
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import config
from app.routers import projects
from app.services import agent_sessions, modeling_eval
from app.services.agent_sessions import Session, SessionManager


@pytest.fixture()
def guard_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    deleted: list[str] = []

    def delete_project(pid):
        deleted.append(pid)
        return {"deleted": pid}

    monkeypatch.setattr(
        projects,
        "workspaces",
        SimpleNamespace(
            project_root=lambda pid: tmp_path / pid,
            delete_project=delete_project,
            # 无 modeling_project_id：不触发 modeling-mcp 级联删除，本用例只管忙碌守卫
            get_project=lambda pid: {"id": pid},
        ),
    )
    # 全局 manager 的锁会绑在首次使用的事件循环上，逐用例换新实例
    manager = SessionManager()
    monkeypatch.setattr(agent_sessions, "manager", manager)
    (tmp_path / "p1").mkdir(parents=True, exist_ok=True)
    request = SimpleNamespace(state=SimpleNamespace(user=None))  # 鉴权关闭：走 owner 校验的放行分支
    return SimpleNamespace(request=request, deleted=deleted, manager=manager, root=tmp_path / "p1")


def _delete(request):
    return asyncio.run(projects.delete_project("p1", request))


def _busy_body(response):
    assert response.status_code == 409
    return json.loads(response.body)


def test_delete_refuses_while_agent_session_running(guard_env):
    guard_env.manager._sessions["p1"] = Session(id="s1", project_id="p1", state="running")

    body = _busy_body(_delete(guard_env.request))
    assert body["code"] == "PROJECT_BUSY"
    assert body["operation"] == "agent-session"
    assert body["error"] == "项目正在执行任务，请等待完成后再删除"
    assert guard_env.deleted == []  # 忙碌时不得落盘删除


def test_delete_refuses_while_agent_session_queued(guard_env):
    """排队等槽位也算进行中：提示语要说清是在排队，别让人以为自己的任务在跑。"""
    guard_env.manager._sessions["p1"] = Session(id="s1", project_id="p1", state="queued")

    body = _busy_body(_delete(guard_env.request))
    assert body["code"] == "PROJECT_BUSY"
    assert body["operation"] == "agent-session-queued"
    assert body["error"] == "项目正在排队等待会话槽位，请等待开始并完成后再删除"
    assert guard_env.deleted == []


def test_delete_refuses_while_eval_running(guard_env):
    modeling_eval._write_state(guard_env.root, status="running")  # ts 取当前时间，非孤儿

    body = _busy_body(_delete(guard_env.request))
    assert body["operation"] == "modeling-eval"
    assert guard_env.deleted == []


def test_delete_proceeds_when_idle(guard_env):
    assert _delete(guard_env.request) == {"deleted": "p1"}
    assert guard_env.deleted == ["p1"]


def test_idle_session_does_not_block_delete(guard_env):
    guard_env.manager._sessions["p1"] = Session(id="s1", project_id="p1", state="idle")

    assert _delete(guard_env.request) == {"deleted": "p1"}


def test_stale_eval_running_does_not_block_delete(guard_env):
    """进程重启遗留的 running 不能把项目锁成永远删不掉：status_view 自愈为 interrupted。"""
    state_path = guard_env.root / modeling_eval.STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"status": "running", "ts": int(time.time()) - 100_000}), encoding="utf-8"
    )
    assert "p1" not in modeling_eval._running  # 内存里已无编排

    assert _delete(guard_env.request) == {"deleted": "p1"}


def test_unreadable_eval_state_does_not_block_delete(guard_env):
    """状态文件损坏只该让忙碌判定失效，不该让项目删不掉。"""
    state_path = guard_env.root / modeling_eval.STATE_REL
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{ 不是 JSON", encoding="utf-8")

    assert _delete(guard_env.request) == {"deleted": "p1"}
