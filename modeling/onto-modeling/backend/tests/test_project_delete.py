"""删除项目：不看项目状态，硬杀会话（含排队）与评测，工作区连同对话记录永久清除（不进回收站）。

直接调用路由函数，并在同一事件循环里跑完响应之后的收尾任务；工作区用真实
WorkspaceManager，手写元数据文件代替建项目流程。
"""

import asyncio
import json
import os
import threading
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app import config
from app.routers import projects
from app.services import (
    agent_sessions,
    error_log,
    eval_data_sources,
    modeling_cleanup,
    modeling_eval,
    modeling_mcp,
    studio,
    workspace,
)
from app.services.agent_sessions import Session, SessionManager
from app.services.engines import jiuwen
from app.services.workspace import META_FILE, PURGE_MARKER, WorkspaceManager

PID = "p1"


class FakeEngine:
    def __init__(self, log: list):
        self.log = log

    async def disconnect(self):
        self.log.append(("engine", "disconnect"))

    async def kill(self):
        self.log.append(("engine", "kill"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    workspaces = WorkspaceManager(data, tmp_path / "assets")
    monkeypatch.setattr(projects, "workspaces", workspaces)
    # 全局 manager 的锁会绑定首次使用的事件循环，逐用例换新实例
    manager = SessionManager()
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(modeling_eval, "_running", set())
    monkeypatch.setattr(modeling_eval, "_stop_requested", set())
    monkeypatch.setattr(modeling_eval, "_live_tokens", {})

    root = workspaces.workspaces_dir / PID
    root.mkdir()
    (root / META_FILE).write_text(json.dumps({"id": PID, "name": "示例任务"}), encoding="utf-8")

    log = []  # 按发生顺序记录终止动作与发往 modeling 侧的请求

    def fake_http(method, path, token, body=None, **kwargs):
        log.append(("modeling", method, path, body, threading.get_ident()))
        return {}

    monkeypatch.setattr(modeling_eval, "_http", fake_http)
    return SimpleNamespace(
        data=data,
        root=root,
        workspaces=workspaces,
        manager=manager,
        log=log,
        request=SimpleNamespace(state=SimpleNamespace(user=None), headers={}),  # 鉴权关闭
    )


def _delete(env, before_cleanup=None) -> dict:
    """调删除接口，再像服务端那样在响应之后跑完收尾任务；before_cleanup 观察响应那一刻的状态。"""

    async def call():
        background = BackgroundTasks()
        result = await projects.delete_project(PID, env.request, background)
        if before_cleanup:
            before_cleanup()
        await background()
        return result

    return asyncio.run(call())


def test_delete_removes_workspace_permanently(env):
    staged = []

    result = _delete(env, lambda: staged.extend(env.workspaces.purge_dir().iterdir()))

    assert result == {"deleted": PID}
    assert len(staged) == 1 and not env.root.exists()  # 响应时项目已移出列表
    assert not staged[0].exists()  # 收尾时连同 git 历史一并清除
    assert not (env.data / "trash").exists()  # 不进回收站
    assert env.workspaces.list_projects() == []
    assert env.log == []  # 没有会话和评测，不打扰 modeling 侧


def test_delete_kills_running_session_before_moving_workspace(env, monkeypatch):
    """在跑的会话不等 CLI 优雅断开，直接硬杀；先杀后移（CLI 占着工作目录时 Windows 上移不走）。"""
    session = Session(id="s1", project_id=PID, engine=FakeEngine(env.log), state="running")
    env.manager._sessions[PID] = session
    stage_delete = env.workspaces.stage_delete

    def logged_stage_delete(project_id):
        env.log.append(("stage", project_id))
        return stage_delete(project_id)

    monkeypatch.setattr(env.workspaces, "stage_delete", logged_stage_delete)

    _delete(env)

    assert env.log == [("engine", "kill"), ("stage", PID)]  # 没有 disconnect：不走优雅断开
    assert session.discarded and session.engine is None
    assert session.state == "closed" and env.manager.get(PID) is None


def test_delete_dequeues_queued_session(env):
    session = Session(id="s1", project_id=PID, state="queued", pending_prompts=["排队中的消息"])
    env.manager._sessions[PID] = session
    env.manager._waiters.append(PID)

    _delete(env)

    assert env.manager.queue_position(PID) is None
    assert session.pending_prompts == [] and session.state == "closed"
    assert env.manager.get(PID) is None


def test_delete_purges_conversation_and_side_data(env):
    """对话记录、续接点、数据源配置（含连接凭据）、Studio 草稿与写锁、引擎工件区一并清除。"""
    history = agent_sessions._history_path(PID)
    history.write_text('{"type": "user_message", "text": "开始建模"}\n', encoding="utf-8")
    agent_sessions.save_resume_id(PID, "sdk-session-abc")
    sources = eval_data_sources._file(PID)
    sources.parent.mkdir(parents=True, exist_ok=True)
    sources.write_text("[]", encoding="utf-8")
    sources.with_suffix(".tmp").write_text("[]", encoding="utf-8")
    studio_files = [studio._state_file(PID, name) for name in ("leases", "drafts")]
    for path in studio_files:
        path.write_text("{}", encoding="utf-8")
    artifact = jiuwen.shadow_dir(PID) / "memory" / "notes.md"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("引擎笔记", encoding="utf-8")
    other_history = agent_sessions._history_path("p2")
    other_history.write_text("{}\n", encoding="utf-8")

    _delete(env)

    leftovers = [
        history,
        agent_sessions._meta_path(PID),
        sources,
        sources.with_suffix(".tmp"),
        *studio_files,
        jiuwen.shadow_dir(PID),
    ]
    assert [path for path in leftovers if path.exists()] == []
    assert other_history.is_file()  # 别的项目不受影响


@pytest.mark.parametrize(
    "status, in_process",
    [("running", True), ("interrupted", False), ("failed", False), ("stopped", False)],
)
def test_delete_cancels_eval_round_after_response(env, status, in_process):
    """评测进行中（本进程有编排）、已中断（重启后无编排），或失败/已停止但 modeling 侧
    可能仍在跑：响应前只置取消标记，modeling 侧轮次的取消请求放到响应之后在线程里发，
    不拖慢删除，也不堵事件循环。"""
    modeling_eval._write_state(env.root, status=status, modeling_project="mp-1", round_id="r1")
    if in_process:
        modeling_eval._running.add(PID)
    at_response = []

    _delete(env, lambda: at_response.extend(env.log))

    assert at_response == []
    [cancel] = env.log
    assert cancel[:4] == ("modeling", "POST", "/api/projects/mp-1/evaluate/cancel", {"roundId": "r1"})
    assert cancel[4] != threading.get_ident()
    assert (PID in modeling_eval._stop_requested) is in_process
    # 编排晚于删除才收尾时的状态写入，不得在原位置重建工作区
    modeling_eval._write_state(env.root, status="stopped")
    assert not env.root.exists()


def test_delete_skips_cancel_for_finished_round(env):
    modeling_eval._write_state(env.root, status="done", modeling_project="mp-1", round_id="r1")

    _delete(env)

    assert env.log == []


def test_retired_core_skips_legacy_round_cancel(env, monkeypatch):
    modeling_eval._write_state(env.root, status="interrupted", modeling_project="mp-1", round_id="r1")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CORE_RETIRED", True)
    _delete(env)
    assert env.log == []


def test_delete_cancels_eval_still_starting(env):
    """编排刚起步、状态还没写成 running：同样置取消标记，免得删除后它照常去 modeling 侧发起一轮。"""
    modeling_eval._running.add(PID)

    _delete(env)

    assert PID in modeling_eval._stop_requested
    assert env.log == []  # 还没有轮次，无需取消


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("modeling 不可达"), (False, "modeling 不可达")],
    ids=["raises", "unconfirmed"],
)
def test_cleanup_failure_is_logged_without_blocking_others(env, monkeypatch, outcome):
    """收尾各项互不拖累：评测轮次取消失败只记运维错误日志，工作区照样清除。"""
    modeling_eval._write_state(env.root, status="running", modeling_project="mp-1", round_id="r1")

    def cancel_round(mp_project, round_id, token):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(modeling_eval, "cancel_round", cancel_round)

    _delete(env)

    assert not env.root.exists() and not any(env.workspaces.purge_dir().iterdir())
    [entry] = error_log.read()
    assert (entry["project_id"], entry["source"]) == (PID, "project_delete")
    assert entry["raw"] == "评测轮次取消未清理完成：modeling 不可达"


@pytest.mark.parametrize(
    "error, enqueued",
    [
        (modeling_mcp.ModelingMcpError("modeling 侧项目不存在", 404), []),
        (modeling_mcp.ModelingMcpError("modeling 侧删除失败", 502), ["project_delete"]),
        (RuntimeError("连接被重置"), ["project_delete"]),
    ],
    ids=["already-gone", "modeling-error", "network-error"],
)
def test_delete_cascades_to_modeling_project_after_response(env, monkeypatch, error, enqueued):
    """modeling 侧关联项目在响应之后级联删除；除"那边已不在"外，失败都转入持久化重试队列。"""
    meta = {"id": PID, "name": "示例任务", "modeling_project_id": "mp-1"}
    (env.root / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    cascaded, queued, at_response = [], [], []

    def fail_delete(meta, request, operation):
        cascaded.append((meta, operation))
        raise error

    monkeypatch.setattr(modeling_mcp, "delete_project", fail_delete)
    monkeypatch.setattr(
        modeling_cleanup, "enqueue", lambda meta, reason, operation: queued.append(operation)
    )

    _delete(env, lambda: at_response.extend(cascaded))

    assert at_response == []  # 级联删除要等网络，不在响应前做
    assert cascaded == [(meta, "project_delete")]  # 元数据在移走工作区前读出
    assert queued == enqueued
    assert error_log.read() == []  # 已有重试队列兜底，不算未清理完成


def test_retired_core_does_not_block_onto_project_delete(env, monkeypatch):
    meta = {"id": PID, "name": "示例任务", "modeling_project_id": "mp-1"}
    (env.root / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(modeling_mcp.config, "MODELING_CORE_RETIRED", True)
    monkeypatch.setattr(modeling_mcp, "delete_project", lambda *_args, **_kwargs: pytest.fail("retired Core called"))
    assert _delete(env) == {"deleted": PID}


def test_delete_falls_back_to_in_place_purge_when_move_fails(env, monkeypatch):
    """目录移不走（Windows 上仍有进程开着其中的文件）：原地换上清除标记，对外同样立即消失。"""
    monkeypatch.setattr(workspace, "_DELETE_RETRY_SECONDS", 0)
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if os.path.normcase(os.fspath(src)) == os.path.normcase(os.fspath(env.root)):
            raise PermissionError(13, "目录被占用")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(workspace.os, "replace", replace)
    at_response = []

    _delete(
        env,
        lambda: at_response.extend(
            [(env.root / PURGE_MARKER).is_file(), env.workspaces.list_projects()]
        ),
    )

    assert at_response == [True, []]
    assert not env.root.exists()  # 收尾时原地清除


def test_delete_does_not_follow_links_out_of_workspace(env, tmp_path):
    """工作区里的 .claude 等链接指向所有项目共用的资产目录：只删链接，不能跟进去删资产。"""
    shared = tmp_path / "assets" / "shared"
    shared.mkdir(parents=True)
    (shared / "rules.md").write_text("共享规则", encoding="utf-8")
    try:
        (env.root / ".claude").symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境无权创建符号链接")

    _delete(env)

    assert not any(env.workspaces.purge_dir().iterdir())
    assert (shared / "rules.md").read_text(encoding="utf-8") == "共享规则"


def test_startup_sweep_purges_leftovers(env):
    """上次没删净的残留（移出后没删完、原地清除删到一半停机）由启动清扫收尾；正常项目不动。"""
    staged = env.workspaces.purge_dir() / "p2-0000abcd"
    (staged / "ontology").mkdir(parents=True)
    marked = env.workspaces.workspaces_dir / "p3"
    marked.mkdir()
    (marked / PURGE_MARKER).touch()

    assert env.workspaces.purge_leftovers() == 2

    assert not staged.exists() and not marked.exists()
    assert (env.root / META_FILE).is_file()
    with pytest.raises(ValueError):
        env.workspaces.purge_staged(env.data / "sessions")  # 只清工作区内的路径
