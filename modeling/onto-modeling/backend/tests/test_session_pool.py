"""会话池的调度语义测试：上限、驱逐、排队、resume 续接与空闲回收。

用假客户端替换 ClaudeEngine._create_client，不拉起真实 CLI 子进程。
"""

import json
import asyncio
import time
from pathlib import Path

import pytest

from app import config
from app.services import agent_sessions
from app.services.agent_sessions import SessionManager, load_resume_id, save_resume_id
from app.services.engines.claude import ClaudeEngine


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.connected = False
        self.queries = []
        self.interrupted = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def query(self, text):
        self.queries.append(text)

    async def interrupt(self):
        self.interrupted = True

    async def receive_messages(self):
        # 长驻不产消息，模拟等待模型的会话；测试结束由 cancel 收尾
        if False:  # pragma: no cover
            yield
        await asyncio.Event().wait()


@pytest.fixture()
def pool_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 1)
    monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", 600)
    monkeypatch.setattr(agent_sessions, "SDK_AVAILABLE", True)

    created = []

    def create_client(self, options):
        client = FakeClient(options)
        created.append(client)
        return client

    monkeypatch.setattr(ClaudeEngine, "_create_client", create_client)
    manager = SessionManager()
    return manager, created, tmp_path


async def _settle(manager: SessionManager, project_id: str):
    """等连接后台任务跑完。"""
    session = manager.get(project_id)
    for _ in range(20):
        if session and session.connect_task:
            await session.connect_task
        await asyncio.sleep(0)
        if session and (session.client is not None or session.state in ("queued", "error")):
            return session
    return session


def test_send_connects_and_flushes_prompt(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        session = await manager.send("p1", tmp_path / "w1", "第一条")
        session = await _settle(manager, "p1")
        assert session.client is not None
        assert session.client.queries == ["第一条"]
        assert session.state == "running"
        assert manager.active_count() == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_pool_evicts_oldest_idle_session(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "你好")
        first = await _settle(manager, "p1")
        first.state = "idle"  # 模拟上一轮已结束

        await manager.send("p2", tmp_path / "w2", "开始")
        second = await _settle(manager, "p2")

        assert manager.get("p1") is None  # 空闲会话被驱逐让位
        assert first.client is None
        assert second.client is not None and second.client.queries == ["开始"]
        assert manager.active_count() == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_pool_queues_when_all_running_then_drains(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务一")
        first = await _settle(manager, "p1")
        assert first.state == "running"  # 在跑，不可驱逐

        await manager.send("p2", tmp_path / "w2", "任务二")
        second = manager.get("p2")
        assert second.state == "queued"
        assert manager.queue_position("p2") == 1

        # 第一个会话结束并被关闭 → 排队者自动续接
        first.state = "idle"
        await manager.close("p1")
        second = await _settle(manager, "p2")
        assert second.client is not None
        assert second.client.queries == ["任务二"]
        assert manager.queue_position("p2") is None
        await manager.shutdown()

    asyncio.run(scenario())


def test_interrupt_cancels_queue(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务一")
        first = await _settle(manager, "p1")
        first.state = "running"

        await manager.send("p2", tmp_path / "w2", "任务二")
        assert manager.queue_position("p2") == 1

        await manager.interrupt("p2")
        assert manager.queue_position("p2") is None
        assert manager.get("p2").pending_prompts == []
        assert manager.get("p2").state == "idle"
        await manager.shutdown()

    asyncio.run(scenario())


def test_resume_id_passed_to_options(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        save_resume_id("p1", "sdk-session-abc")
        await manager.send("p1", tmp_path / "w1", "继续")
        session = await _settle(manager, "p1")
        assert session.client.options.resume == "sdk-session-abc"
        assert session.resume_attempted is True
        await manager.shutdown()

    asyncio.run(scenario())


def test_close_clears_resume_id(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        save_resume_id("p1", "sdk-session-abc")
        await manager.send("p1", tmp_path / "w1", "你好")
        await _settle(manager, "p1")
        await manager.close("p1")
        assert load_resume_id("p1") is None
        await manager.shutdown()

    asyncio.run(scenario())


def test_reap_idle_recycles_but_keeps_resume_id(pool_env, tmp_path, monkeypatch):
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "你好")
        session = await _settle(manager, "p1")
        session.state = "idle"
        session.sdk_session_id = "sdk-session-xyz"
        save_resume_id("p1", "sdk-session-xyz")
        session.last_activity = time.time() - 10_000

        reaped = await manager.reap_idle()
        assert reaped == 1
        assert manager.get("p1") is None
        assert load_resume_id("p1") == "sdk-session-xyz"  # 续接点保留
        await manager.shutdown()

    asyncio.run(scenario())


def test_running_session_not_reaped(pool_env, tmp_path):
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "你好")
        session = await _settle(manager, "p1")
        assert session.state == "running"
        session.last_activity = time.time() - 10_000

        reaped = await manager.reap_idle()
        assert reaped == 0
        assert manager.get("p1") is session
        await manager.shutdown()

    asyncio.run(scenario())


def test_tool_result_excerpt_and_git_changes(tmp_path):
    import subprocess

    from app.services.agent_sessions import _git_changes, _git_head, _tool_result_excerpt

    assert _tool_result_excerpt("hello") == "hello"
    assert _tool_result_excerpt([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert _tool_result_excerpt("x" * 500).endswith("…")

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "base"], cwd=tmp_path, check=True)
    base = _git_head(tmp_path)
    (tmp_path / "a.md").write_text("1", encoding="utf-8")
    (tmp_path / "订单目标.md").write_text("2", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)
    head = _git_head(tmp_path)
    changes = _git_changes(tmp_path, base, head)
    # 中文路径必须原样返回（core.quotepath 关闭），不得出现 \347 转义或引号
    assert sorted(changes["added"]) == ["a.md", "订单目标.md"]
    assert changes["modified"] == []


def test_interrupt_force_kills_hung_engine(pool_env, tmp_path, monkeypatch):
    """CLI 悬死时打断必须在超时后硬停：引擎释放、状态回 idle、事件到位。"""
    manager, created, _ = pool_env
    monkeypatch.setattr(agent_sessions, "INTERRUPT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(agent_sessions, "KILL_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务")
        session = await _settle(manager, "p1")
        assert session.state == "running"

        async def hang():
            await asyncio.Event().wait()

        session.engine.client.interrupt = hang  # 模拟 CLI 对 interrupt 无应答
        await manager.interrupt("p1")
        assert session.engine is None  # 引擎已硬终止释放
        assert session.state == "idle"
        assert any(e.get("type") == "session_interrupted" for e in session.events)
        # 续接点保留：下一条消息可重连
        await manager.send("p1", tmp_path / "w1", "继续")
        again = await _settle(manager, "p1")
        assert again.client is not None
        await manager.shutdown()

    asyncio.run(scenario())


def test_stale_running_history_reconciles(pool_env):
    """重启杀掉运行中的轮后，历史尾部停在 running：读取或打断都要自愈为终态，
    而不是让前端永远显示"执行中"、打断 404（幽灵运行态修复）。"""
    manager, _, _ = pool_env
    path = agent_sessions._history_path("ghost")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "user_message", "text": "跑任务", "seq": 0, "ts": 1.0}) + "\n")
        f.write(json.dumps({"type": "session_state", "state": "running", "seq": 1, "ts": 2.0}) + "\n")

    history = manager.get_history("ghost")
    assert history[-1]["type"] == "session_state" and history[-1]["state"] == "idle"
    assert any(e["type"] == "session_interrupted" for e in history)
    assert [e["seq"] for e in history] == [0, 1, 2, 3]  # seq 连续

    # 幂等：已是终态则不再追加
    assert len(manager.get_history("ghost")) == 4

    # 打断入口：无活跃会话不再 404；终态历史返回未和解
    assert asyncio.run(manager.interrupt("ghost")) is False


def test_interrupt_ghost_running_reconciles(pool_env):
    manager, _, _ = pool_env
    path = agent_sessions._history_path("ghost2")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "session_state", "state": "running", "seq": 0, "ts": 1.0}) + "\n")

    assert asyncio.run(manager.interrupt("ghost2")) is True
    tail = manager.get_history("ghost2")
    assert tail[-1] == {"type": "session_state", "state": "idle", "ts": tail[-1]["ts"], "seq": 2}
    # 不存在历史的项目：打断是无害幂等操作
    assert asyncio.run(manager.interrupt("nonexistent")) is False
