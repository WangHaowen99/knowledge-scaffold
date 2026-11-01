"""配置变更不切断正在跑的一轮（B1）与停机先标中断、并发回收（B9）。

用可控的假引擎驱动 SessionManager：测试自己投递 result 事件，决定一轮何时结束。
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from app import config
from app.services import agent_sessions
from app.services.agent_sessions import SessionManager, load_history


class FakeEngine:
    name = "claude"
    instances: list["FakeEngine"] = []
    disconnect_delay = 0.0
    hang_forever = False

    def __init__(self, ctx):
        self.ctx = ctx
        self.client = None
        self.resume_attempted = False
        self.sent: list[str] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.stale = False
        self.disconnected = False
        self.killed = False
        FakeEngine.instances.append(self)

    async def connect(self):
        self.client = object()
        return False

    async def send(self, text):
        self.sent.append(text)

    async def events(self):
        while True:
            event = await self.queue.get()
            yield event
            if event.get("type") == "_engine_end":
                return

    async def interrupt(self):
        pass

    async def disconnect(self):
        if self.hang_forever:
            await asyncio.Event().wait()
        await asyncio.sleep(self.disconnect_delay)
        self.disconnected = True
        self.client = None

    async def kill(self):
        if self.hang_forever:
            await asyncio.Event().wait()
        self.killed = True
        self.client = None

    def config_stale(self):
        return self.stale

    def context_limits(self):
        return None, None


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 4)
    monkeypatch.setattr(agent_sessions, "SDK_AVAILABLE", True)
    FakeEngine.instances = []
    FakeEngine.disconnect_delay = 0.0
    FakeEngine.hang_forever = False
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: FakeEngine)
    return manager, tmp_path


async def _settle(manager, project_id):
    session = manager.get(project_id)
    for _ in range(50):
        if session.connect_task:
            await session.connect_task
        await asyncio.sleep(0)
        if session.engine is not None:
            break
    return session


async def _drain(times: int = 20):
    for _ in range(times):
        await asyncio.sleep(0)


def test_config_change_while_running_waits_for_result(env):
    manager, tmp_path = env

    async def scenario():
        await manager.send("p1", tmp_path, "第一条")
        session = await _settle(manager, "p1")
        first = session.engine
        assert session.state == "running" and first.sent == ["第一条"]

        first.stale = True  # 管理员改了模型配置
        await manager.send("p1", tmp_path, "补充一句")
        # 运行中不切断：消息照常交给旧引擎，只记待重载
        assert session.engine is first and not first.disconnected
        assert first.sent == ["第一条", "补充一句"]
        assert session.reload_pending

        first.queue.put_nowait({"type": "result"})
        await _drain()
        # 引擎里还积压着第二条消息，不能断
        assert session.engine is first and not first.disconnected

        first.queue.put_nowait({"type": "result"})
        await _drain()
        assert first.disconnected
        assert session.engine is None and not session.reload_pending
        assert session.state == "idle"

        # 下一条消息按新配置重连
        await manager.send("p1", tmp_path, "第三条")
        session = await _settle(manager, "p1")
        assert len(FakeEngine.instances) == 2
        assert session.engine is FakeEngine.instances[1]
        assert session.engine.sent == ["第三条"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_config_change_while_idle_reconnects_immediately(env):
    manager, tmp_path = env

    async def scenario():
        await manager.send("p1", tmp_path, "第一条")
        session = await _settle(manager, "p1")
        first = session.engine
        first.queue.put_nowait({"type": "result"})
        await _drain()
        assert session.state == "idle"

        first.stale = True
        await manager.send("p1", tmp_path, "第二条")
        session = await _settle(manager, "p1")
        assert first.disconnected and first.sent == ["第一条"]
        assert session.engine is FakeEngine.instances[1]
        assert session.engine.sent == ["第二条"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_shutdown_marks_running_rounds_and_recycles_concurrently(env):
    manager, tmp_path = env
    FakeEngine.disconnect_delay = 0.3

    async def scenario():
        await manager.send("p1", tmp_path, "在跑")
        await _settle(manager, "p1")
        await manager.send("p2", tmp_path, "已结束")
        idle = await _settle(manager, "p2")
        idle.engine.queue.put_nowait({"type": "result"})
        await _drain()
        assert idle.state == "idle"

        started = time.monotonic()
        await manager.shutdown()
        elapsed = time.monotonic() - started

        assert elapsed < 0.55  # 两个引擎各 0.3 秒，并发回收
        assert all(engine.disconnected for engine in FakeEngine.instances)
        assert manager.get("p1") is None and manager.get("p2") is None

    asyncio.run(scenario())

    running = load_history("p1")
    kinds = [(e["type"], e.get("state"), e.get("reason")) for e in running]
    mark = kinds.index(("session_interrupted", None, "shutdown"))
    assert kinds[mark + 1] == ("session_state", "idle", None)
    assert kinds[-1] == ("session_state", "closed", None)
    assert [e["seq"] for e in running] == list(range(len(running)))
    assert not any(e["type"] == "session_interrupted" for e in load_history("p2"))

    # 重启后不重复补写
    restarted = SessionManager()
    assert not any(
        e.get("reason") == "stale" for e in restarted.get_history("p1")
    )


def test_shutdown_is_bounded_when_engines_hang(env, monkeypatch):
    manager, tmp_path = env
    FakeEngine.hang_forever = True
    monkeypatch.setattr(agent_sessions, "SHUTDOWN_BUDGET_SECONDS", 0.2)

    async def scenario():
        await manager.send("p1", tmp_path, "在跑")
        await _settle(manager, "p1")
        started = time.monotonic()
        await manager.shutdown()
        return time.monotonic() - started

    assert asyncio.run(scenario()) < 1.0
    assert any(e.get("reason") == "shutdown" for e in load_history("p1"))


def test_shutdown_budget_fits_docker_grace_period():
    # 单个引擎的断开 + 硬杀上限要落在总预算内；总预算加 uvicorn 的 3 秒在 25 秒内
    assert (
        agent_sessions.INTERRUPT_TIMEOUT_SECONDS + agent_sessions.KILL_TIMEOUT_SECONDS
        <= agent_sessions.SHUTDOWN_BUDGET_SECONDS
    )
    assert agent_sessions.SHUTDOWN_BUDGET_SECONDS + 3 <= 25


def test_connect_writes_engine_marker_for_each_instance(env):
    """state_helper 按这个标识认锁的持有者：每个引擎实例一个，连接前写好。"""
    manager, tmp_path = env
    marker_path = tmp_path / ".onto" / "run" / "engine.json"

    async def scenario():
        await manager.send("p1", tmp_path, "第一条")
        await _settle(manager, "p1")
        first = json.loads(marker_path.read_text(encoding="utf-8"))
        assert first["host_pid"] == os.getpid()
        assert first["engine"] == "claude" and first["conversation_id"] == "default"

        await manager.recycle_project("p1")
        await manager.send("p1", tmp_path, "第二条")
        await _settle(manager, "p1")
        second = json.loads(marker_path.read_text(encoding="utf-8"))
        assert second["engine_id"] != first["engine_id"]
        await manager.shutdown()

    asyncio.run(scenario())
