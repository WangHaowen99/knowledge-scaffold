"""多会话：会话索引、按会话分文件的历史与续接点、引擎换绑与忙碌守卫。

沿用 test_session_pool 的假客户端，不拉起真实 CLI 子进程。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.api import deps
from app.routers import ops
from app.routers import sessions as sessions_router
from app.services import agent_sessions, conversation_store
from app.services.agent_sessions import (
    SessionError,
    SessionManager,
    load_history,
    load_resume_id,
    save_resume_id,
    usage_summary,
)
from app.services.conversation_store import ConversationError
from app.services.engines.base import EngineContext
from app.services.engines.claude import ClaudeEngine

from test_session_pool import FakeClient, _settle


@pytest.fixture()
def conv_env(tmp_path: Path, monkeypatch):
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
    # usage_summary 与路由函数读的是模块级 manager
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(sessions_router, "manager", manager)
    monkeypatch.setattr(
        deps, "workspaces", SimpleNamespace(project_root=lambda pid: tmp_path / pid)
    )
    return SimpleNamespace(
        manager=manager,
        created=created,
        cwd=tmp_path / "w1",
        sessions_dir=tmp_path / "data" / "sessions",
        request=SimpleNamespace(state=SimpleNamespace(user=None)),  # 鉴权关闭
    )


def _write_history(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for seq, event in enumerate(events):
            f.write(json.dumps({**event, "seq": seq}, ensure_ascii=False) + "\n")


def _result(cost: float, ts: float) -> dict:
    return {
        "type": "result",
        "ts": ts,
        "total_cost_usd": cost,
        "usage": {"output_tokens": 10, "input_tokens": 5},
    }


async def _send_and_idle(env, text: str, conversation_id: str | None = None):
    """发一条消息并模拟本轮结束（假客户端不产 result）。"""
    await env.manager.send("p1", env.cwd, text, conversation_id)
    session = await _settle(env.manager, "p1")
    session.state = "idle"
    return session


# ---- 索引与标题 ----


def test_legacy_project_lists_default_without_moving_files(conv_env):
    legacy = conv_env.sessions_dir / "p1.jsonl"
    _write_history(
        legacy,
        [
            {"type": "session_state", "state": "idle", "ts": 100.0},
            {"type": "user_message", "text": "（我通过界面上传了资料：a.csv）\n梳理  订单对象", "ts": 101.0},
        ],
    )

    conversations = conversation_store.list_conversations("p1")

    assert [c["id"] for c in conversations] == ["default"]
    assert conversations[0]["title"] == "梳理 订单对象"
    assert conversations[0]["created_at"] == 100.0
    assert legacy.exists()
    # 读路径不落盘：索引在首次变更时才写
    assert not (conv_env.sessions_dir / "p1" / "index.json").exists()


def test_auto_title_strips_upload_prefix_and_truncates():
    assert conversation_store.auto_title("（我通过界面上传了资料：x.md）\n你好\n世界") == "你好 世界"
    assert len(conversation_store.auto_title("长" * 100)) == conversation_store.AUTO_TITLE_MAX


def test_manual_title_survives_auto_title(conv_env):
    async def scenario():
        conversation = conversation_store.create("p1")
        renamed = conversation_store.rename("p1", conversation["id"], "  订单   建模 ")
        assert renamed["title"] == "订单 建模" and renamed["title_manual"] is True

        await _send_and_idle(conv_env, "第一条消息", conversation["id"])
        titles = {c["id"]: c["title"] for c in conversation_store.list_conversations("p1")}
        assert titles[conversation["id"]] == "订单 建模"
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_rename_validates_title_and_id(conv_env):
    conversation = conversation_store.create("p1")
    with pytest.raises(ConversationError) as empty:
        conversation_store.rename("p1", conversation["id"], "   ")
    assert empty.value.status == 400
    with pytest.raises(ConversationError):
        conversation_store.rename("p1", conversation["id"], "名" * 61)
    with pytest.raises(ConversationError) as missing:
        conversation_store.rename("p1", "0123456789ab", "标题")
    assert missing.value.status == 404


def test_invalid_conversation_id_is_404(conv_env):
    with pytest.raises(ConversationError) as traversal:
        conversation_store.history_path("p1", "../p2")
    assert traversal.value.status == 404

    async def scenario():
        for bad in ("../p2", "0123456789ab", "DEFAULT"):
            with pytest.raises(SessionError) as exc:
                await conv_env.manager.send("p1", conv_env.cwd, "你好", bad)
            assert exc.value.status == 404
        assert conv_env.manager.get("p1") is None
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_failed_index_write_leaves_no_temporary_file(conv_env):
    conversation = conversation_store.create("p1")
    index = conv_env.sessions_dir / "p1" / "index.json"
    before = index.read_text(encoding="utf-8")

    # 不可序列化的值让 json.dump 写到一半抛 TypeError（不是 OSError）
    with pytest.raises(TypeError):
        conversation_store._write("p1", {conversation["id"]: {"title": object()}})

    assert list(index.parent.glob("index-*.tmp")) == []
    assert index.read_text(encoding="utf-8") == before


# ---- 历史与续接点按会话隔离 ----


def test_new_conversation_has_its_own_history(conv_env):
    async def scenario():
        await _send_and_idle(conv_env, "默认会话的消息")
        conversation = conversation_store.create("p1")
        cid = conversation["id"]

        session = await _send_and_idle(conv_env, "新会话的消息", cid)

        assert session.conversation_id == cid
        assert conv_env.created[0].connected is False  # 旧引擎已回收
        assert conv_env.manager.active_count() == 1
        own = load_history("p1", cid)
        assert own[0]["seq"] == 0
        assert [e["text"] for e in own if e["type"] == "user_message"] == ["新会话的消息"]
        legacy = load_history("p1")
        assert [e["text"] for e in legacy if e["type"] == "user_message"] == ["默认会话的消息"]
        assert (conv_env.sessions_dir / "p1" / f"{cid}.jsonl").exists()

        titles = {c["id"]: c["title"] for c in conversation_store.list_conversations("p1")}
        assert titles == {"default": "默认会话的消息", cid: "新会话的消息"}
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_switching_back_resumes_each_conversations_own_token(conv_env):
    async def scenario():
        save_resume_id("p1", "tok-default")
        cid = conversation_store.create("p1")["id"]
        save_resume_id("p1", "tok-second", cid)

        await _send_and_idle(conv_env, "一", "default")
        assert conv_env.created[-1].options.resume == "tok-default"
        await _send_and_idle(conv_env, "二", cid)
        assert conv_env.created[-1].options.resume == "tok-second"
        await _send_and_idle(conv_env, "三", "default")
        assert conv_env.created[-1].options.resume == "tok-default"
        assert sum(client.connected for client in conv_env.created) == 1
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_omitted_conversation_follows_live_engine(conv_env):
    async def scenario():
        cid = conversation_store.create("p1")["id"]
        await _send_and_idle(conv_env, "在新会话里", cid)
        assert conv_env.manager.resolve_conversation("p1") == cid

        # 设置页、Studio 提交等不带会话 id 的调用落到引擎绑定的会话上
        session = await conv_env.manager.send("p1", conv_env.cwd, "不带会话 id")
        assert session.conversation_id == cid
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


# ---- 忙碌守卫 ----


def test_cross_conversation_send_and_delete_rejected_while_running(conv_env):
    async def scenario():
        await conv_env.manager.send("p1", conv_env.cwd, "长任务")
        running = await _settle(conv_env.manager, "p1")
        assert running.state == "running"
        cid = conversation_store.create("p1")["id"]

        with pytest.raises(SessionError) as send_exc:
            await conv_env.manager.send("p1", conv_env.cwd, "插一句", cid)
        assert send_exc.value.status == 409
        with pytest.raises(SessionError) as delete_exc:
            await conv_env.manager.delete_conversation("p1", "default")
        assert delete_exc.value.status == 409

        assert conv_env.manager.get("p1") is running
        assert load_history("p1", cid) == []
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_cross_conversation_send_rejected_while_connecting(conv_env, monkeypatch):
    gate: dict = {}

    async def slow_connect(self):
        await gate["open"].wait()
        self.connected = True

    monkeypatch.setattr(FakeClient, "connect", slow_connect)

    async def scenario():
        gate["open"] = asyncio.Event()
        await conv_env.manager.send("p1", conv_env.cwd, "首条")
        session = conv_env.manager.get("p1")
        assert session.is_connecting() and session.state == "idle"
        cid = conversation_store.create("p1")["id"]

        with pytest.raises(SessionError) as exc:
            await conv_env.manager.send("p1", conv_env.cwd, "换一条", cid)
        assert exc.value.status == 409

        gate["open"].set()
        await _settle(conv_env.manager, "p1")
        assert session.client.queries == ["首条"]
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_concurrent_cross_conversation_sends_leave_one_engine(conv_env):
    async def scenario():
        await _send_and_idle(conv_env, "起点")
        second = conversation_store.create("p1")["id"]
        third = conversation_store.create("p1")["id"]

        results = await asyncio.gather(
            conv_env.manager.send("p1", conv_env.cwd, "去第二条", second),
            conv_env.manager.send("p1", conv_env.cwd, "去第三条", third),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        assert len(errors) == 1 and errors[0].status == 409

        await _settle(conv_env.manager, "p1")
        assert len(conv_env.manager._sessions) == 1
        assert sum(client.connected for client in conv_env.created) == 1
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_close_during_connect_disconnects_new_engine(conv_env, monkeypatch):
    gate: dict = {}

    async def slow_connect(self):
        await gate["open"].wait()
        self.connected = True

    monkeypatch.setattr(FakeClient, "connect", slow_connect)

    async def scenario():
        gate["open"] = asyncio.Event()
        await conv_env.manager.send("p1", conv_env.cwd, "首条")
        connect_task = conv_env.manager.get("p1").connect_task

        await conv_env.manager.close("p1")
        gate["open"].set()
        await connect_task

        assert conv_env.manager.get("p1") is None
        assert conv_env.created[-1].connected is False  # 没有游离在字典外的引擎
        assert conv_env.manager.active_count() == 0
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_interrupt_other_conversation_leaves_live_one_running(conv_env):
    async def scenario():
        await conv_env.manager.send("p1", conv_env.cwd, "长任务")
        running = await _settle(conv_env.manager, "p1")
        cid = conversation_store.create("p1")["id"]

        assert await conv_env.manager.interrupt("p1", cid) is False
        assert conv_env.created[0].interrupted is False
        assert running.state == "running"
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


# ---- 关闭与删除 ----


def test_close_other_conversation_only_clears_its_token(conv_env):
    async def scenario():
        cid = conversation_store.create("p1")["id"]
        save_resume_id("p1", "tok-default")
        save_resume_id("p1", "tok-second", cid)
        await conv_env.manager.send("p1", conv_env.cwd, "长任务", "default")
        running = await _settle(conv_env.manager, "p1")

        await conv_env.manager.close("p1", cid)

        assert conv_env.manager.get("p1") is running
        assert load_resume_id("p1", cid) is None
        assert load_resume_id("p1") == "tok-default"
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_close_all_conversations_clears_every_token(conv_env):
    async def scenario():
        cid = conversation_store.create("p1")["id"]
        save_resume_id("p1", "tok-default")
        save_resume_id("p1", "tok-second", cid)
        await conv_env.manager.send("p1", conv_env.cwd, "你好")
        await _settle(conv_env.manager, "p1")

        await conv_env.manager.close("p1", all_conversations=True)

        assert conv_env.manager.get("p1") is None
        assert load_resume_id("p1") is None
        assert load_resume_id("p1", cid) is None
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_discard_purges_every_conversation_of_the_project(conv_env):
    """删除项目（永久删除）清掉全部会话的历史、续接点、索引与留存用量，别的项目不受影响。"""

    async def scenario():
        await _send_and_idle(conv_env, "默认会话")
        cid = conversation_store.create("p1")["id"]
        await _send_and_idle(conv_env, "第二条会话", cid)
        save_resume_id("p1", "tok-default")
        save_resume_id("p1", "tok-second", cid)
        doomed = conversation_store.create("p1")["id"]
        _write_history(conv_env.sessions_dir / "p1" / f"{doomed}.jsonl", [_result(0.1, 100.0)])
        await conv_env.manager.delete_conversation("p1", doomed)  # 留下 deleted-usage
        assert (conv_env.sessions_dir / "p1" / "deleted-usage.jsonl").is_file()
        other_cid = conversation_store.create("p2")["id"]
        _write_history(conv_env.sessions_dir / "p2" / f"{other_cid}.jsonl", [_result(0.2, 100.0)])

        await conv_env.manager.discard("p1")

        assert not (conv_env.sessions_dir / "p1.jsonl").exists()
        assert not (conv_env.sessions_dir / "p1.meta.json").exists()
        assert not (conv_env.sessions_dir / "p1").exists()
        assert (conv_env.sessions_dir / "p2" / "index.json").is_file()
        assert (conv_env.sessions_dir / "p2" / f"{other_cid}.jsonl").is_file()
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


def test_delete_removes_files_and_never_leaves_list_empty(conv_env):
    async def scenario():
        await _send_and_idle(conv_env, "默认会话")
        cid = conversation_store.create("p1")["id"]
        await _send_and_idle(conv_env, "要删的会话", cid)
        save_resume_id("p1", "tok-second", cid)
        history_file = conv_env.sessions_dir / "p1" / f"{cid}.jsonl"
        assert history_file.exists()

        remaining = await conv_env.manager.delete_conversation("p1", cid)

        assert [c["id"] for c in remaining] == ["default"]
        assert conv_env.manager.get("p1") is None  # 绑定它的空闲引擎先回收
        assert not history_file.exists()
        assert not history_file.with_suffix(".meta.json").exists()
        with pytest.raises(SessionError) as exc:
            await conv_env.manager.send("p1", conv_env.cwd, "还在吗", cid)
        assert exc.value.status == 404

        remaining = await conv_env.manager.delete_conversation("p1", "default")
        assert len(remaining) == 1 and remaining[0]["id"] != "default"
        assert not (conv_env.sessions_dir / "p1.jsonl").exists()
        # 删掉的 default 不会因为旧路径的推导而复活
        assert [c["id"] for c in conversation_store.list_conversations("p1")] == [
            remaining[0]["id"]
        ]
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


# ---- 用量统计 ----


def test_usage_counts_all_conversations_including_deleted(conv_env):
    cid = conversation_store.create("p1")["id"]
    other = conversation_store.create("p1")["id"]
    _write_history(conv_env.sessions_dir / "p1.jsonl", [_result(0.5, 1_700_000_000.0)])
    _write_history(conv_env.sessions_dir / "p1" / f"{cid}.jsonl", [_result(0.25, 1_700_000_100.0)])
    _write_history(conv_env.sessions_dir / "p1" / f"{other}.jsonl", [_result(0.125, 1_700_000_200.0)])

    asyncio.run(conv_env.manager.delete_conversation("p1", other))
    summary = usage_summary("p1")

    assert summary["calls"] == 3
    assert summary["total_cost_usd"] == 0.875
    assert summary["output_tokens"] == 30
    assert summary["last_activity"] == 1_700_000_200.0
    assert sum(day["calls"] for day in ops._usage_daily(days=10_000)) == 3


def _model_usage(input_tokens: int, output_tokens: int, ts: float) -> dict:
    return {
        "type": "model_usage",
        "ts": ts,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def test_usage_counts_per_call_events_once(conv_env):
    """逐次记账的轮：token 按 model_usage 计，带 usage_per_call 的 result 只算一轮；
    没等到 result 的轮（打断超时、回收、重启）也记上已完成的调用。"""
    cid = conversation_store.create("p1")["id"]
    other = conversation_store.create("p1")["id"]
    finished = [
        _model_usage(1000, 40, 1_700_000_000.0),
        _model_usage(1200, 60, 1_700_000_010.0),
        {
            "type": "result",
            "ts": 1_700_000_020.0,
            "total_cost_usd": None,
            "usage": {"input_tokens": 2200, "output_tokens": 100},
            "usage_per_call": True,
        },
    ]
    cut_off = [
        _model_usage(3000, 200, 1_700_000_100.0),
        {"type": "session_interrupted", "reason": "stale", "ts": 1_700_000_200.0},
    ]
    _write_history(conv_env.sessions_dir / "p1.jsonl", [_result(0.5, 1_700_000_000.0)])
    _write_history(conv_env.sessions_dir / "p1" / f"{cid}.jsonl", finished)
    _write_history(conv_env.sessions_dir / "p1" / f"{other}.jsonl", cut_off)

    asyncio.run(conv_env.manager.delete_conversation("p1", cid))
    summary = usage_summary("p1")

    assert summary["calls"] == 2
    assert summary["input_tokens"] == 5 + 2200 + 3000
    assert summary["output_tokens"] == 10 + 100 + 200
    assert sum(day["output_tokens"] for day in ops._usage_daily(days=10_000)) == 310
    assert sum(day["calls"] for day in ops._usage_daily(days=10_000)) == 2


# ---- 路由 ----


def test_session_state_for_conversation_not_bound_to_engine(conv_env):
    async def scenario():
        await conv_env.manager.send("p1", conv_env.cwd, "长任务")
        await _settle(conv_env.manager, "p1")
        cid = conversation_store.create("p1")["id"]

        state = sessions_router.conversation_state("p1", cid, conv_env.request)  # S-05
        assert state["conversation_id"] == cid
        assert state["live"] is False and state["state"] == "idle"
        assert state["history_count"] == 0 and state["pool"]["max"] == 1

        listed = sessions_router.list_conversations("p1", conv_env.request)  # S-01
        assert listed["live"] == {"conversation_id": "default", "state": "running", "busy": True}
        assert {c["id"] for c in listed["items"]} == {"default", cid}
        default = next(c for c in listed["items"] if c["id"] == "default")
        assert default["live"] is True and default["state"] == "running" and default["kind"] == "chat"
        assert default["created_at"].endswith("Z")
        await conv_env.manager.shutdown()

    asyncio.run(scenario())


# ---- Jiuwen ----


def test_jiuwen_session_id_is_per_conversation(tmp_path):
    from app.services.engines.jiuwen import JiuwenEngine

    def make(conversation_id):
        async def permission(*_args):
            return {"behavior": "allow", "updated_input": None}

        return JiuwenEngine(
            EngineContext(
                project_id="p1",
                cwd=tmp_path,
                request_permission=permission,
                load_resume_token=lambda: None,
                save_resume_token=lambda token: None,
                clear_resume_token=lambda: None,
                log_error=lambda kind, text: None,
                conversation_id=conversation_id,
            )
        )

    assert make("default")._sid == "onto-p1"  # 存量会话沿用单会话时期的 id
    assert make("0123456789ab")._sid == "onto-p1-0123456789ab"


# ---- 事件流按会话挂接 ----


def _drain(queue: asyncio.Queue) -> list[dict]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_open_stream_attaches_only_to_its_own_conversation(conv_env):
    """事件流登记在某条会话上：引擎绑着别的会话时不挂，切到这条会话新建 Session 时即时挂上。"""

    async def scenario():
        await _send_and_idle(conv_env, "默认会话的消息")
        cid = conversation_store.create("p1")["id"]
        queue: asyncio.Queue = asyncio.Queue()
        conv_env.manager.watch("p1", queue, cid)
        assert queue not in conv_env.manager.get("p1").subscribers

        session = await _send_and_idle(conv_env, "新会话的消息", cid)
        assert queue in session.subscribers
        events = _drain(queue)
        assert events[0]["type"] == "user_message" and events[0]["text"] == "新会话的消息"

        conv_env.manager.unwatch("p1", queue, cid)
        assert queue not in session.subscribers
        assert ("p1", cid) not in conv_env.manager._watchers
        await conv_env.manager.shutdown()

    asyncio.run(scenario())
