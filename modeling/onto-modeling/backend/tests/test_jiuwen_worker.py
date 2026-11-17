"""Jiuwen 独立 worker（方案 B11；详设 4.2 目标 6、8.3 措施 5）：一个 worker 子进程承载多个会话，
回调（确认卡、续接点、错误留档）经协议回到内核；worker 崩溃时它上面的会话都收到结束事件，下次连接重拉。

worker 里跑假引擎（tests/fake_worker_engine.py），不需要模型。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from app import config
from app.services.engines import jiuwen_remote
from app.services.engines.base import EngineContext
from app.services.engines.jiuwen_remote import RemoteJiuwenEngine, WorkerClient


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    client = WorkerClient("tests.fake_worker_engine:FakeWorkerEngine")
    jiuwen_remote.use_client(client)
    yield client
    jiuwen_remote.use_client(None)


def make_engine(tmp_path: Path, project_id: str, log: dict, answer: str = "allow") -> RemoteJiuwenEngine:
    async def permission(tool_name, tool_input, title, description):
        log.setdefault("asked", []).append(tool_name)
        return {"behavior": answer}

    return RemoteJiuwenEngine(EngineContext(
        project_id=project_id, cwd=tmp_path, request_permission=permission,
        load_resume_token=lambda: log.get("token"),
        save_resume_token=lambda token: log.__setitem__("token", token),
        clear_resume_token=lambda: log.pop("token", None),
        log_error=lambda kind, text: log.setdefault("errors", []).append((kind, text)),
        load_history=lambda: [{"type": "user_message", "text": "历史"}], conversation_id="c1",
    ))


async def next_event(engine: RemoteJiuwenEngine, events, kind: str, timeout: float = 10) -> dict:
    while True:
        event = await asyncio.wait_for(events.__anext__(), timeout)
        if event.get("type") == kind:
            return event


def test_sessions_share_one_worker_and_callbacks_reach_kernel(worker, tmp_path):
    first_log, second_log = {}, {}

    async def scenario():
        first = make_engine(tmp_path, "p1", first_log)
        second = make_engine(tmp_path, "p2", second_log, answer="deny")
        assert await first.connect() is False and await second.connect() is False
        assert first.context_limits() == (1000, 800)
        first_events, second_events = first.events(), second.events()
        await first.send("你好")
        reply = await next_event(first, first_events, "assistant_message")
        await next_event(first, first_events, "result")
        await second.send("提问")
        asked = await next_event(second, second_events, "assistant_message")
        await first.disconnect()
        await second.kill()
        await worker.stop()
        return reply, asked

    reply, asked = asyncio.run(scenario())
    text = reply["blocks"][0]["text"]
    project, message, history, pid = text.split(":")
    assert (project, message, history) == ("p1", "你好", "1")  # 历史快照随连接带过去
    assert int(pid) != os.getpid()  # 在子进程里跑
    assert worker.starts == 1  # 两个会话共用一个 worker
    assert first_log["token"] == "jiuwen:p1" and first_log["errors"] == [("fake", "合成留档")]
    assert second_log["asked"] == ["AskUserQuestion"] and asked["blocks"][0]["text"] == "deny"


def test_worker_crash_ends_its_sessions_and_next_connect_restarts(worker, tmp_path):
    first_log, second_log = {}, {}

    async def scenario():
        first = make_engine(tmp_path, "p1", first_log)
        second = make_engine(tmp_path, "p2", second_log)
        await first.connect()
        await second.connect()
        first_events, second_events = first.events(), second.events()
        pid = worker.process.pid
        await first.send("崩溃")
        ended = [await next_event(first, first_events, "_engine_end"),
                 await next_event(second, second_events, "_engine_end")]
        assert not worker.alive and worker.sessions == {}
        third = make_engine(tmp_path, "p3", {})
        await third.connect()
        restarted = worker.process.pid
        await third.kill()
        await worker.stop()
        return ended, pid, restarted

    ended, pid, restarted = asyncio.run(scenario())
    assert all(event["reason"] == "error" for event in ended)
    assert restarted != pid and worker.starts == 2
    assert ("jiuwen_worker", "Jiuwen worker 进程退出，会话随之结束") in first_log["errors"]


def test_connect_failure_is_reported(worker, tmp_path):
    async def scenario():
        engine = make_engine(tmp_path, "refuse", {})
        with pytest.raises(RuntimeError, match="合成的连接失败"):
            await engine.connect()
        assert engine.sid not in worker.sessions
        await worker.stop()

    asyncio.run(scenario())


def test_worker_injects_pack_prompt_rules_like_the_kernel(worker, tmp_path):
    """worker 进程自己登记包提示规则（app/wiring.install_worker）：按内核给的数据目录读装配，
    启用了声明提示规则的包（领域包扩展）的项目带提示，没启用的不带。"""
    from app.services.workspace import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    with_pack = manager.project_root(manager.create_project("有提示", packs=["domain-extend"])["id"])
    without = manager.project_root(manager.create_project("无提示", packs=[])["id"])

    async def ask(root: Path) -> str:
        engine = make_engine(root, root.name, {})
        await engine.connect()
        events = engine.events()
        await engine.send("注入：这一轮就到这，结束本轮")
        reply = await next_event(engine, events, "assistant_message", timeout=30)
        await engine.disconnect()
        return reply["blocks"][0]["text"]

    async def scenario():
        replies = [await ask(with_pack), await ask(without)]
        await worker.stop()
        return replies

    hinted, plain = asyncio.run(scenario())
    assert "/domain-extend-add" in hinted
    assert plain == ""


def test_engine_selection_follows_worker_switch(monkeypatch):
    from app.services.engines import get_engine_class
    from app.services.engines.jiuwen import JiuwenEngine

    monkeypatch.setattr(config, "JIUWEN_WORKER", True)
    assert get_engine_class("jiuwen") is RemoteJiuwenEngine
    monkeypatch.setattr(config, "JIUWEN_WORKER", False)
    assert get_engine_class("jiuwen") is JiuwenEngine


def test_worker_context_carries_the_assembly_changed_note(tmp_path):
    """宿主的注入对象不跨进程：装配换过版本的提醒经 connect 的 ctx 带进 worker（实测内测引擎丢过）。"""
    import io

    from app.services.engines.jiuwen_worker import Worker

    worker = Worker(engine_class=None, out=io.StringIO())
    base = {"project_id": "p-demo", "cwd": str(tmp_path), "conversation_id": "c1"}
    assert worker._context("s1", {**base, "assembly_changed": True}).injector.assembly_changed is True
    assert worker._context("s2", base).injector.assembly_changed is False
