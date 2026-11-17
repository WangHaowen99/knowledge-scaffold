"""会话池的调度语义测试：上限、驱逐、排队、resume 续接与空闲回收。

用假客户端替换 ClaudeEngine._create_client，不拉起真实 CLI 子进程。
"""

import json
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from app import config
from app.services import agent_sessions
from app.services.agent_sessions import (
    SessionError,
    SessionManager,
    _is_visible_chat_event,
    load_resume_id,
    save_resume_id,
    visible_history_start,
)
from app.services.engines.claude import ClaudeEngine


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.connected = False
        self.queries = []
        self.interrupted = False
        self.terminated = False
        # kill() 走 client._transport._process.terminate()，自任进程以便断言硬终止
        self._transport = SimpleNamespace(_process=self)

    def terminate(self):
        self.terminated = True

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


def test_duplicate_codegen_scan_is_rejected_while_running(pool_env, tmp_path):
    manager, _created, _ = pool_env
    command = "/ontology-codegen-scan"

    async def scenario():
        results = await asyncio.gather(
            manager.send("p1", tmp_path / "w1", command),
            manager.send("p1", tmp_path / "w1", command),
            return_exceptions=True,
        )
        rejected = [result for result in results if isinstance(result, SessionError)]
        assert len(rejected) == 1
        assert rejected[0].status == 409
        session = await _settle(manager, "p1")
        assert session.client.queries == [command]
        assert [event["text"] for event in session.events if event["type"] == "user_message"] == [command]

        await manager.send("p1", tmp_path / "w1", "补充说明")
        assert session.client.queries == [command, "补充说明"]
        session.set_state("idle")
        await manager.send("p1", tmp_path / "w1", command)
        assert session.client.queries == [command, "补充说明", command]
        await manager.shutdown()

    asyncio.run(scenario())


def test_duplicate_codegen_scan_is_rejected_while_queued(pool_env, tmp_path):
    manager, _created, _ = pool_env
    command = "/ontology-codegen-scan"

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "长任务")
        await _settle(manager, "p1")
        await manager.send("p2", tmp_path / "w2", command)
        queued = manager.get("p2")
        assert queued.state == "queued"
        with pytest.raises(SessionError, match="正在运行") as error:
            await manager.send("p2", tmp_path / "w2", command)
        assert error.value.status == 409
        assert queued.pending_prompts == [command]
        await manager.shutdown()

    asyncio.run(scenario())


def test_codegen_scan_waits_for_prior_turn_in_same_session(scripted_env, tmp_path):
    manager, created, _ = scripted_env
    command = "/ontology-codegen-scan"

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "长任务")
        session = await _settle(manager, "p1")
        with pytest.raises(SessionError, match="当前任务正在运行") as error:
            await manager.send("p1", tmp_path / "w1", command)
        assert error.value.status == 409
        assert session.client.queries == ["长任务"]
        assert not session.codegen_scan_active

        await _feed(created[0], _turn_result())
        assert session.state == "idle"
        await manager.send("p1", tmp_path / "w1", command)
        assert session.client.queries == ["长任务", command]
        with pytest.raises(SessionError, match="扫描候选正在运行"):
            await manager.send("p1", tmp_path / "w1", command)
        await manager.shutdown()

    asyncio.run(scenario())


def test_codegen_scan_rejects_prior_turn_queued_for_pool_slot(pool_env, tmp_path):
    manager, _created, _ = pool_env
    command = "/ontology-codegen-scan"

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "占用会话池")
        await _settle(manager, "p1")
        await manager.send("p2", tmp_path / "w2", "排队任务")
        queued = manager.get("p2")
        assert queued.state == "queued"
        with pytest.raises(SessionError, match="当前任务正在运行") as error:
            await manager.send("p2", tmp_path / "w2", command)
        assert error.value.status == 409
        assert queued.pending_prompts == ["排队任务"]
        assert not queued.codegen_scan_active
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


def test_resume_id_scoped_to_engine(pool_env, tmp_path):
    manager, _, _ = pool_env

    async def scenario():
        # 切到 jiuwen 引擎后写下的 token，Claude 引擎不得拿去 resume
        save_resume_id("p1", "jiuwen:onto-p1", engine="jiuwen")
        assert load_resume_id("p1", engine="jiuwen") == "jiuwen:onto-p1"
        assert load_resume_id("p1") is None
        await manager.send("p1", tmp_path / "w1", "继续")
        session = await _settle(manager, "p1")
        assert session.client.options.resume is None
        assert session.resume_attempted is False
        await manager.shutdown()

    asyncio.run(scenario())

    # 旧格式 meta 无 engine 字段：由 Claude 引擎写入，只归 Claude
    meta = Path(config.DATA_DIR) / "sessions" / "p2.meta.json"
    meta.write_text(json.dumps({"sdk_session_id": "sdk-session-old"}), encoding="utf-8")
    assert load_resume_id("p2") == "sdk-session-old"
    assert load_resume_id("p2", engine="jiuwen") is None


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


def test_visible_history_window_ignores_runtime_noise():
    history = [
        {"type": "system", "subtype": "thinking_tokens", "seq": 0},
        {"type": "session_state", "state": "running", "seq": 1},
        {"type": "user_message", "text": "开始", "seq": 2},
        {"type": "tool_results", "blocks": [], "seq": 3},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "完成"}], "seq": 4},
        {"type": "result", "seq": 5},
    ]

    assert _is_visible_chat_event(history[0]) is False
    assert _is_visible_chat_event(history[2]) is True
    assert visible_history_start(history, 400) == 2
    assert visible_history_start(history, 1) == 4


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


def test_close_force_kills_hung_engine(pool_env, tmp_path, monkeypatch):
    """CLI 对 disconnect 无应答时 close() 必须超时硬停后返回。

    无超时兜底时 _recycle 会在 _lock 里永久 await disconnect()，删除项目的接口
    因此永不返回，前端表现为"点删除没反应"。此处若回退到无超时实现，wait_for 超时报错。
    """
    manager, created, _ = pool_env
    monkeypatch.setattr(agent_sessions, "INTERRUPT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(agent_sessions, "KILL_TIMEOUT_SECONDS", 0.2)

    async def scenario():
        save_resume_id("p1", "sdk-session-abc")
        await manager.send("p1", tmp_path / "w1", "任务")
        session = await _settle(manager, "p1")
        assert session.client is not None

        async def hang():
            await asyncio.Event().wait()

        client = session.engine.client
        client.disconnect = hang  # 模拟 CLI 对 disconnect 无应答
        await asyncio.wait_for(manager.close("p1"), timeout=2)

        assert client.terminated is True  # disconnect 超时后回落到 kill()
        assert session.engine is None
        assert manager.get("p1") is None
        assert load_resume_id("p1") is None  # close 语义不变：续接点清除
        # _lock 没被拖住：后续调用仍能进来
        await asyncio.wait_for(manager.close("p1"), timeout=2)
        await manager.shutdown()

    asyncio.run(scenario())


def test_close_during_connect_stops_late_engine(pool_env, tmp_path, monkeypatch):
    """连接中途被关闭（如删除项目）：晚到的引擎要当场断开，不能留成没人管的游离子进程。"""
    manager, created, _ = pool_env

    async def scenario():
        gate = asyncio.Event()

        async def slow_connect(self):
            await gate.wait()
            self.connected = True

        monkeypatch.setattr(FakeClient, "connect", slow_connect)
        session = await manager.send("p1", tmp_path / "w1", "任务")
        connect_task = session.connect_task  # _connect 收尾时会清空该字段，先留住
        assert connect_task is not None
        await asyncio.sleep(0)
        await manager.close("p1")
        gate.set()
        await connect_task

        assert created[0].connected is False  # 连上即断开
        assert session.engine is None and session.reader_task is None
        assert session.state == "closed"
        assert manager.get("p1") is None
        assert manager.active_count() == 0
        await manager.shutdown()

    asyncio.run(scenario())


def test_discard_kills_without_waiting_for_hung_engine(pool_env, tmp_path):
    """删除项目走 discard：不等 CLI 优雅断开，立即硬杀；对话历史与续接点一并清除。

    close() 遇到悬死的 CLI 要等断开超时（10 秒）才硬杀，删除接口因此变慢；discard 不能等。
    """
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务")
        session = await _settle(manager, "p1")
        save_resume_id("p1", "sdk-session-abc")

        async def hang():
            await asyncio.Event().wait()

        client = session.engine.client
        client.disconnect = hang  # 模拟 CLI 对 disconnect 无应答
        await asyncio.wait_for(manager.discard("p1"), timeout=1)

        assert client.terminated is True
        assert session.discarded and session.engine is None
        assert session.state == "closed"
        assert manager.get("p1") is None
        assert not agent_sessions._history_path("p1").exists()
        assert load_resume_id("p1") is None
        # 晚到的事件（如读循环收尾）不能把已清除的历史文件重建出来
        session.broadcast({"type": "assistant_text", "text": "晚到的事件"})
        assert not agent_sessions._history_path("p1").exists()
        await manager.shutdown()

    asyncio.run(scenario())


def test_discard_during_connect_kills_late_engine(pool_env, tmp_path, monkeypatch):
    """连接中途项目被删除：晚到的引擎当场硬杀，积压的消息不再发给它。"""
    manager, created, _ = pool_env

    async def scenario():
        gate = asyncio.Event()

        async def slow_connect(self):
            await gate.wait()
            self.connected = True

        monkeypatch.setattr(FakeClient, "connect", slow_connect)
        session = await manager.send("p1", tmp_path / "w1", "任务")
        connect_task = session.connect_task  # _connect 收尾时会清空该字段，先留住
        await asyncio.sleep(0)
        await manager.discard("p1")
        gate.set()
        await connect_task

        assert created[0].terminated is True
        assert created[0].queries == []
        assert session.engine is None and session.state == "closed"
        assert manager.active_count() == 0
        assert not agent_sessions._history_path("p1").exists()
        await manager.shutdown()

    asyncio.run(scenario())


def test_discard_hands_slot_to_waiter(pool_env, tmp_path):
    """删除在跑的项目，让出的槽位交给排队者。"""
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务一")
        first_client = (await _settle(manager, "p1")).client
        await manager.send("p2", tmp_path / "w2", "任务二")
        assert manager.queue_position("p2") == 1

        await manager.discard("p1")
        await asyncio.gather(*manager._tasks)  # 续接排队者在后台进行，删除不等它
        second = await _settle(manager, "p2")

        assert first_client.terminated is True
        assert second.client is not None and second.client.queries == ["任务二"]
        assert manager.queue_position("p2") is None
        await manager.shutdown()

    asyncio.run(scenario())


def test_discard_dequeues_and_denies_pending_permission(pool_env, tmp_path):
    """排队中的项目删除即出队、不波及在跑的项目；待确认卡按拒绝了结，权限回调不会一直挂着。"""
    manager, created, _ = pool_env

    async def scenario():
        await manager.send("p1", tmp_path / "w1", "任务一")
        first = await _settle(manager, "p1")
        await manager.send("p2", tmp_path / "w2", "任务二")
        queued = manager.get("p2")

        await manager.discard("p2")
        assert manager.queue_position("p2") is None
        assert queued.pending_prompts == [] and queued.state == "closed"
        assert manager.get("p2") is None
        assert first.client is not None and first.client.terminated is False

        permission = asyncio.create_task(
            manager._request_permission(first, "Write", {"file_path": "a.md"}, None, None)
        )
        await asyncio.sleep(0)
        assert first.pending_permissions
        await manager.discard("p1")
        decision = await asyncio.wait_for(permission, timeout=1)
        assert decision == {"behavior": "deny", "message": "项目已删除"}
        assert not agent_sessions._history_path("p1").exists()
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


def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_open_stream_receives_first_message_events_immediately(pool_env, tmp_path):
    """事件流先于会话建立（打开项目后发第一条消息）：新会话一建立就推送，不等心跳超时。"""
    manager, created, _ = pool_env

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        manager.watch("p1", queue)
        await manager.send("p1", tmp_path / "w1", "第一条")
        await _settle(manager, "p1")
        events = _drain(queue)
        assert events[0]["type"] == "user_message"
        assert "session_started" in [e["type"] for e in events]
        assert "running" in [e.get("state") for e in events if e["type"] == "session_state"]
        manager.unwatch("p1", queue)
        assert queue not in manager.get("p1").subscribers
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("limits", [(200000, 167000), (None, None)])
def test_session_started_carries_context_limits_known_at_connect(
    pool_env, tmp_path, monkeypatch, limits
):
    """连接时已知窗口与触发点就随 session_started 下发：切换引擎后的第一轮进行中，用量圈即按新值显示。"""
    manager, _, _ = pool_env
    monkeypatch.setattr(ClaudeEngine, "context_limits", lambda self: limits)

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        manager.watch("p1", queue)
        await manager.send("p1", tmp_path / "w1", "第一条")
        await _settle(manager, "p1")
        started = next(e for e in _drain(queue) if e["type"] == "session_started")
        manager.unwatch("p1", queue)
        await manager.shutdown()
        return started

    started = asyncio.run(scenario())
    if limits[0]:
        assert (started["window"], started["compact_at"]) == limits
    else:
        assert "window" not in started and "compact_at" not in started


def test_open_stream_follows_session_recreated_after_reap(pool_env, tmp_path):
    """空闲回收后再发消息会新建会话：已打开的事件流自动挂上，过程照常即时推送。"""
    manager, created, _ = pool_env

    async def scenario():
        queue: asyncio.Queue = asyncio.Queue()
        manager.watch("p1", queue)
        await manager.send("p1", tmp_path / "w1", "第一条")
        first = await _settle(manager, "p1")
        first.state = "idle"
        first.last_activity = time.time() - 10_000
        assert await manager.reap_idle() == 1
        _drain(queue)

        await manager.send("p1", tmp_path / "w1", "第二条")
        second = await _settle(manager, "p1")
        assert second is not first
        assert queue in second.subscribers
        events = _drain(queue)
        assert events[0]["type"] == "user_message" and events[0]["text"] == "第二条"
        # seq 与历史连续：前端按 seq 水位去重不会误丢
        seqs = [e["seq"] for e in events if "seq" in e]
        assert seqs == sorted(seqs) and seqs[0] == len(first.events)
        await manager.shutdown()

    asyncio.run(scenario())


def test_unwatch_forgets_project_without_open_streams(pool_env):
    manager, _created, _ = pool_env
    queue: asyncio.Queue = asyncio.Queue()
    manager.watch("p1", queue)
    manager.unwatch("p1", queue)
    assert ("p1", "default") not in manager._watchers


# ---- 会话状态以 CLI 自报的 idle 为准（主轮结束不代表任务结束） ----


class ScriptedClient(FakeClient):
    """按步骤推送 SDK 消息：测试逐条放入，读循环逐条消费。"""

    def __init__(self, options):
        super().__init__(options)
        self.inbox: asyncio.Queue = asyncio.Queue()

    async def receive_messages(self):
        while True:
            message = await self.inbox.get()
            if message is None:  # 放入 None 模拟 CLI 退出、消息流结束
                return
            yield message


@pytest.fixture()
def scripted_env(pool_env, monkeypatch):
    manager, created, tmp_path = pool_env

    def create_client(self, options):
        client = ScriptedClient(options)
        created.append(client)
        return client

    monkeypatch.setattr(ClaudeEngine, "_create_client", create_client)
    return manager, created, tmp_path


def _cli_state(state):
    return SystemMessage(subtype="session_state_changed", data={"state": state})


def _turn_result(origin=None):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="synthetic-session",
        origin=origin,
    )


async def _feed(client, *messages):
    for message in messages:
        client.inbox.put_nowait(message)
    for _ in range(20):
        await asyncio.sleep(0)


async def _running_session(manager, tmp_path):
    await manager.send("p1", tmp_path / "w1", "你好")
    session = await _settle(manager, "p1")
    assert session.state == "running"
    return session


def _states(session):
    return [event["state"] for event in session.events if event["type"] == "session_state"]


def test_cli_without_state_reports_turns_idle_on_result(scripted_env):
    """旧版 CLI、九问引擎不上报状态：result 后即置空闲，与原行为一致。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _turn_result())
        assert session.state == "idle"
        await manager.shutdown()

    asyncio.run(scenario())


def test_turn_result_waits_for_cli_idle(scripted_env):
    """同步派遣：result 后 CLI 随即报 idle，此时才置空闲；状态只切换一次，CLI 状态不进历史。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _cli_state("running"), _turn_result())
        assert session.state == "running"

        await _feed(created[0], _cli_state("idle"))
        assert session.state == "idle"
        assert _states(session) == ["running", "idle"]
        assert not any(event["type"] == "_cli_state" for event in session.events)
        await manager.shutdown()

    asyncio.run(scenario())


def test_background_work_keeps_session_running_until_cli_idle(scripted_env):
    """后台派遣：主轮与接续轮次之间 CLI 一直报 running，会话保持运行中、不被回收。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _cli_state("running"), _turn_result())
        session.last_activity = time.time() - 10_000
        assert await manager.reap_idle() == 0

        await _feed(created[0], _turn_result(origin={"kind": "task-notification"}))
        assert session.state == "running"

        await _feed(created[0], _cli_state("idle"))
        assert session.state == "idle"
        assert _states(session) == ["running", "idle"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_cli_idle_before_result_turns_idle_on_result(scripted_env):
    """CLI 的 idle 先于 result 到达：等 result 广播后再置空闲，前端先收到结果。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _cli_state("running"), _cli_state("idle"))
        assert session.state == "running"

        await _feed(created[0], _turn_result())
        assert session.state == "idle"
        kinds = [event["type"] for event in session.events]
        assert kinds.index("result") < len(kinds) - 1
        assert kinds[-1] == "session_state"
        await manager.shutdown()

    asyncio.run(scenario())


def test_stop_turns_idle_even_if_cli_still_running(scripted_env):
    """用户点了停止：之后的 result 直接置空闲；CLI 随后开始接续轮次时恢复运行中。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _cli_state("running"))
        await manager.interrupt("p1")
        await _feed(created[0], _turn_result())
        assert created[0].interrupted
        assert session.state == "idle"

        await _feed(created[0], _cli_state("idle"), _cli_state("running"))
        assert session.state == "running"
        await manager.shutdown()

    asyncio.run(scenario())


# ---- 引擎不再有结果时收尾：CLI 退出、长时间无响应（两个引擎共用会话层） ----


def _session_errors(session):
    return [event for event in session.events if event["type"] == "session_error"]


def test_cli_exit_while_running_reports_error(scripted_env):
    """这一轮还在跑时 CLI 退出：要告诉用户并释放槽位，不能一直显示运行中。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], None)

        assert session.state == "error"
        assert session.engine is None
        assert manager.active_count() == 0
        assert len(_session_errors(session)) == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_cli_exit_while_idle_closes_silently(scripted_env):
    """一轮结束后 CLI 退出不算出错，不给用户留错误提示。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], _turn_result())
        assert session.state == "idle"
        await _feed(created[0], None)

        assert session.state == "closed"
        assert session.engine is None
        assert _session_errors(session) == []
        await manager.shutdown()

    asyncio.run(scenario())


def test_stalled_session_times_out_and_can_continue(scripted_env, monkeypatch):
    """长时间没有引擎事件：判定执行超时并硬终止，下一条消息能重新连接。"""
    manager, created, tmp_path = scripted_env
    monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

    async def scenario():
        session = await _running_session(manager, tmp_path)
        session.last_activity = time.time() - 1801

        assert await manager.reap_stalled() == 1

        assert session.state == "error"
        assert session.engine is None
        assert created[0].terminated is True
        assert manager.active_count() == 0
        error = _session_errors(session)[-1]
        assert error["reason"] == agent_sessions.STALL_TIMEOUT_REASON
        assert "30 分钟" in error["error"]

        await manager.send("p1", tmp_path / "w1", "继续")
        session = await _settle(manager, "p1")
        assert session.state == "running"
        assert len(created) == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_only_running_sessions_without_activity_are_stalled(scripted_env, monkeypatch):
    manager, created, tmp_path = scripted_env
    monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

    async def scenario():
        session = await _running_session(manager, tmp_path)
        # 刚有过活动
        session.last_activity = time.time() - 1799
        assert await manager.reap_stalled() == 0

        # 在等用户确认：确认卡有自己的超时
        session.last_activity = time.time() - 1801
        session.pending_permissions["r1"] = object()
        assert await manager.reap_stalled() == 0
        session.pending_permissions.clear()

        # 开关关闭
        monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 0)
        assert await manager.reap_stalled() == 0
        monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

        # 已经空闲：归空闲回收管，不算超时
        await _feed(created[0], _turn_result())
        session.last_activity = time.time() - 1801
        assert await manager.reap_stalled() == 0

        assert session.engine is not None
        assert _session_errors(session) == []
        await manager.shutdown()

    asyncio.run(scenario())


# ---- 排队时间不算无响应；出错收尾时提示追加消息；运行时活动刷新计时 ----


def test_queue_wait_does_not_count_as_stall(scripted_env, monkeypatch):
    """排队等槽位的时间不算引擎无响应：接上引擎后从零开始计时。"""
    manager, created, tmp_path = scripted_env
    monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

    async def scenario():
        await _running_session(manager, tmp_path)
        await manager.send("p2", tmp_path / "w2", "排队的消息")
        queued = manager.get("p2")
        assert queued.state == "queued"
        queued.last_activity = time.time() - 1801  # 排队了 30 分钟以上

        await manager.close("p1")
        queued = await _settle(manager, "p2")
        assert queued.state == "running"
        assert await manager.reap_stalled() == 0
        assert queued.engine is not None
        await manager.shutdown()

    asyncio.run(scenario())


def test_error_notice_mentions_followups_sent_during_turn(scripted_env):
    """本轮进行中追加的消息随引擎拆除丢失：错误提示里说明，让用户重发。"""
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await manager.send("p1", tmp_path / "w1", "追加一条")
        await _feed(created[0], None)

        error = _session_errors(session)[-1]["error"]
        assert "追加的 1 条消息" in error
        await manager.shutdown()

    asyncio.run(scenario())


def test_error_notice_has_no_followup_hint_without_followups(scripted_env):
    manager, created, tmp_path = scripted_env

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await _feed(created[0], None)

        assert "追加" not in _session_errors(session)[-1]["error"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_stall_notice_mentions_followups(scripted_env, monkeypatch):
    manager, created, tmp_path = scripted_env
    monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

    async def scenario():
        session = await _running_session(manager, tmp_path)
        await manager.send("p1", tmp_path / "w1", "追加一")
        await manager.send("p1", tmp_path / "w1", "追加二")
        session.last_activity = time.time() - 1801
        assert await manager.reap_stalled() == 1

        error = _session_errors(session)[-1]
        assert error["reason"] == agent_sessions.STALL_TIMEOUT_REASON
        assert "追加的 2 条消息" in error["error"]
        await manager.shutdown()

    asyncio.run(scenario())


def test_runtime_activity_refreshes_stall_clock_without_broadcast(scripted_env, monkeypatch):
    """不转发的运行时消息（如状态消息）刷新活动时间，但不进历史。"""
    manager, created, tmp_path = scripted_env
    monkeypatch.setattr(config, "SESSION_STALL_TIMEOUT_SECONDS", 1800)

    async def scenario():
        session = await _running_session(manager, tmp_path)
        before = len(session.events)
        session.last_activity = time.time() - 1801
        await _feed(created[0], SystemMessage(subtype="status", data={}))

        assert len(session.events) == before
        assert await manager.reap_stalled() == 0
        await manager.shutdown()

    asyncio.run(scenario())
