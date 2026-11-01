"""删除项目：不看项目状态，硬杀会话（含排队），工作区连同对话记录与发布留存永久清除（不进回收站）。

直接调用路由函数，并在同一事件循环里跑完响应之后的收尾任务；工作区用真实
WorkspaceManager，手写元数据文件代替建项目流程。
"""

import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

from app import config
from app.routers import projects
from app.services import agent_sessions, error_log, release_store, studio, workspace
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

    root = workspaces.workspaces_dir / PID
    root.mkdir()
    (root / META_FILE).write_text(json.dumps({"id": PID, "name": "示例任务"}), encoding="utf-8")

    log = []  # 按发生顺序记录终止动作
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
    assert env.log == []  # 没有会话，不用终止


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
    """对话记录、续接点、发布留存、Studio 草稿与写锁、引擎工件区一并清除。"""
    history = agent_sessions._history_path(PID)
    history.write_text('{"type": "user_message", "text": "开始建模"}\n', encoding="utf-8")
    agent_sessions.save_resume_id(PID, "sdk-session-abc")
    releases = release_store.root_dir(env.data) / PID
    releases.mkdir(parents=True)
    (releases / "0.1.2.zip").write_bytes(b"synthetic-package")
    other_releases = release_store.root_dir(env.data) / "p2"
    other_releases.mkdir()
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
        releases,
        *studio_files,
        jiuwen.shadow_dir(PID),
    ]
    assert [path for path in leftovers if path.exists()] == []
    assert other_history.is_file() and other_releases.is_dir()  # 别的项目不受影响


@pytest.mark.parametrize(
    "outcome",
    [(OSError("磁盘不可写"), "磁盘不可写"), (False, "仍有文件被占用")],
    ids=["raises", "unfinished"],
)
def test_cleanup_failure_is_logged_without_blocking_others(env, monkeypatch, outcome):
    """收尾各项互不拖累：发布留存删不净只记运维错误日志，工作区照样清除。"""
    error, message = outcome

    def purge_project(data_dir, project_id):
        if isinstance(error, Exception):
            raise error
        return error

    monkeypatch.setattr(release_store, "purge_project", purge_project)

    _delete(env)

    assert not env.root.exists() and not any(env.workspaces.purge_dir().iterdir())
    [entry] = error_log.read()
    assert (entry["project_id"], entry["source"]) == (PID, "project_delete")
    assert entry["raw"] == f"发布留存未清理完成：{message}"


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
