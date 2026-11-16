"""运行中发消息（契约 EC-10 补充 delivery=steer|next、message.delivery、next.updated、S-08 returned）：

- 消息排队收回平台：文字插进正在跑的这一轮（steer），命令由平台代存（next），这一轮结束后同一步开始；
- 只有引擎没在跑、也没有待送达的插话才发 idle；执行记录挂的命令就是引擎实际在跑的命令（复现 07:03 的错绑）；
- 打断撤回没被看到的插话（引擎侧一并撤回），代存的命令暂停不自动开始；
- 两个引擎对会话层的同一套信号：Claude 读 CLI 的 command_lifecycle 与 user_message_uuids、打断带 cancel_queued；
  Jiuwen 用只挂在主 agent 上的 Rail 插话，本轮结束还没插的拿来开下一轮。

假引擎、假 CLI 原始消息流，不起真实模型。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionError, SessionManager
from app.services.engines.base import EngineContext
from app.services.session_host import command_tasks, exec_records
from app.services.workspace import WorkspaceManager
from tests.test_session_host import FakeEngine, lock, release, result
from tests.test_session_host import settle as _settle


def _quiet(session) -> bool:
    # 回到空闲之后收尾还在线程里记执行记录（finishing）：等它做完，否则紧接着停机会把这一步记成中断
    return session.state == "idle" and not session.sending and not session.finishing


async def settle(manager, project_id, *, until=None):
    # 全量跑时机器忙：多等一会儿（等不到就让后面的断言去失败）
    return await _settle(manager, project_id, until=until or _quiet, rounds=1500)


# 场景函数的多个返回值：解包用法不变
CommandRun = namedtuple("CommandRun", "steer held occupied first_exec second_exec final")
InterruptRun = namedtuple("InterruptRun", "engine steer returned paused sent_before started")


class SteerEngine(FakeEngine):
    """会插话的假引擎。

    一轮开着直到测试调 finish；插话在 model_call 时插进去（seen），结束时没插的拿第一条开下一轮
    （new_turn）；打断带 cancel_queued 时撤回还没插的。
    """

    name = "fake-steer"

    def __init__(self, ctx: EngineContext):
        super().__init__(ctx)
        self.pending: list[tuple[str, str]] = []
        self.turn_ids: list[str] = []
        self.cancel_flags: list[bool] = []
        self.loop = None

    async def connect(self) -> bool:
        self.loop = asyncio.get_running_loop()
        return False

    async def send(self, text: str, context: str | None = None, message_id: str | None = None) -> None:
        FakeEngine.sent.append(text)
        self.turn_ids = [message_id] if message_id else []
        script = FakeEngine.scripts.pop(0) if FakeEngine.scripts else []
        for item in script:
            await self.queue.put(item if callable(item) else dict(item))

    async def steer(self, text: str, message_id: str) -> None:
        self.pending.append((message_id, text))

    async def emit(self, *items) -> None:
        for item in items:
            await self.queue.put(item if callable(item) else dict(item))

    async def model_call(self) -> None:
        items, self.pending = self.pending, []
        for message_id, _ in items:
            self.turn_ids.append(message_id)
            await self.queue.put({"type": "_delivery", "message_id": message_id, "state": "seen"})

    async def finish(self, *before) -> None:
        await self.emit(*before)
        await self.queue.put(result(message_ids=list(self.turn_ids)))
        if self.pending:
            message_id, text = self.pending.pop(0)
            FakeEngine.sent.append(text)
            await self.queue.put({"type": "_delivery", "message_id": message_id, "state": "new_turn"})
            self.turn_ids = [message_id]

    async def interrupt(self, cancel_queued: bool = False) -> list[str] | None:
        self.cancel_flags.append(cancel_queued)
        withdrawn = None
        if cancel_queued:
            withdrawn, self.pending = [message_id for message_id, _ in self.pending], []
        await self.queue.put({"type": "result", "subtype": "interrupted", "is_error": False, "duration_ms": 1,
                              "message_ids": list(self.turn_ids)})
        return withdrawn


@pytest.fixture()
def steer_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 4)
    FakeEngine.scripts, FakeEngine.sent = [], []
    manager = SessionManager()
    engine_cls = {"cls": SteerEngine}
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: engine_cls["cls"])
    monkeypatch.setattr(agent_sessions, "manager", manager)
    streams.reset_all()
    project_id = workspaces.create_project("插话与排队", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    yield manager, project_id, root, engine_cls
    streams.reset_all()


def records(manager, project_id, kinds, conversation_id="default"):
    return [event for event in manager.get_history(project_id, conversation_id) if event.get("type") in kinds]


def deliveries(manager, project_id, message_id):
    return [event["payload"] for event in records(manager, project_id, {"message_delivery"})
            if event["payload"]["message_id"] == message_id]


def between(manager, project_id, first, second, kind):
    """两条 result 之间的某类记录（按历史顺序）。"""
    history = manager.get_history(project_id)
    results = [index for index, event in enumerate(history) if event.get("type") == "result"]
    return [event for event in history[results[first] + 1:results[second]] if event.get("type") == kind]


async def running(manager, project_id):
    return await settle(manager, project_id, until=lambda s: s.state == "running" and s.engine is not None)


# ---- 07:03 的场景：命令跑着时又发命令，执行记录不能挂错 ----


def test_command_sent_while_running_starts_after_turn_and_exec_matches_engine(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")], [lock("extract-object", "source")]]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        first_exec = session.exec_id
        _, steer = await manager.submit(project_id, root, "补充：只看规则", meta={"display_text": "补充：只看规则"})
        _, held = await manager.submit(project_id, root, "/ontology-extract-object", meta={
            "command": "/ontology-extract-object", "command_label": "对象提取", "display_text": "提取对象",
            "origin": {"kind": "page", "page": "wb-l2", "label": "全部命令"}})
        # 第二条命令不能再排：409 next_occupied（带排着的那条的名字）
        with pytest.raises(SessionError) as occupied:
            await manager.submit(project_id, root, "/ontology-extract-operation", meta={"command": "/ontology-extract-operation"})
        await session.engine.model_call()
        await settle(manager, project_id, until=lambda s: not s.steers)
        await session.engine.finish(release("validate"))
        session = await settle(manager, project_id, until=lambda s: s.exec_id and s.exec_id != first_exec
                               and len(FakeEngine.sent) == 2)
        second_exec = session.exec_id
        await session.engine.finish(release("extract-object"))
        final = (await settle(manager, project_id)).state
        await manager.shutdown()
        return CommandRun(steer, held, occupied.value, first_exec, second_exec, final)

    steer, held, occupied, first_exec, second_exec, final = asyncio.run(scenario())
    assert steer["delivery"] == "steer" and steer["message_id"]
    assert held["delivery"] == "next" and held["message_id"] is None and held["record"] is None
    assert held["next"]["label"] == "对象提取" and held["next"]["paused"] is False
    assert held["next"]["origin"] == {"kind": "page", "page": "wb-l2", "label": "全部命令"}
    assert occupied.code == "next_occupied" and occupied.details == {"label": "对象提取"}
    # 引擎实际跑的命令 = 执行记录挂的命令
    assert FakeEngine.sent == ["/ontology-validate", "/ontology-extract-object"]
    assert exec_records.get(project_id, first_exec)["command_name"] == "validate"
    assert exec_records.get(project_id, first_exec)["state"] == "done"
    assert exec_records.get(project_id, second_exec)["command_name"] == "extract-object"
    # 两轮之间不出 idle、不推建议条
    assert all(event["state"] != "idle" for event in between(manager, project_id, 0, 1, "session_state"))
    assert between(manager, project_id, 0, 1, "suggestions") == []
    # 代存的命令开始那一刻才有用户气泡（immediate，带原来的来源与显示文字），排在第一轮的 result 之后
    users = records(manager, project_id, {"user_message"})
    assert [(item["text"], item["delivery"]) for item in users] == [
        ("/ontology-validate", "immediate"), ("补充：只看规则", "steer"), ("提取对象", "immediate")]
    assert users[2]["origin"]["label"] == "全部命令" and users[2]["command"] == "/ontology-extract-object"
    assert between(manager, project_id, 0, 1, "user_message")[0]["text"] == "提取对象"
    assert [item["state"] for item in deliveries(manager, project_id, steer["message_id"])] == ["pending", "seen"]
    nexts = [event["payload"]["next"] for event in records(manager, project_id, {"next_updated"})]
    assert nexts[0]["id"] == held["next"]["id"] and nexts[-1] is None
    assert final == "idle"


# ---- 插话 ----


def test_steer_not_seen_in_turn_starts_next_turn_without_idle(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")]]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        first_exec = session.exec_id
        _, steer = await manager.submit(project_id, root, "再看一下第三条规则")
        await session.engine.finish()  # 这一轮没有模型调用可插：结束后拿它开下一轮
        session = await settle(manager, project_id, until=lambda s: not s.steers and s.turn is not None)
        assert session.state == "running"
        await session.engine.finish(release("validate"))
        final = (await settle(manager, project_id)).state
        await manager.shutdown()
        return steer, first_exec, final

    steer, first_exec, final = asyncio.run(scenario())
    assert [item["state"] for item in deliveries(manager, project_id, steer["message_id"])] == ["pending", "new_turn"]
    assert all(event["state"] != "idle" for event in between(manager, project_id, 0, 1, "session_state"))
    assert between(manager, project_id, 0, 1, "suggestions") == []
    # 普通文字不开新的执行记录：接着等待中的那份
    assert len(exec_records.query(project_id, None, "default")) == 1
    assert exec_records.get(project_id, first_exec)["state"] == "done"
    assert final == "idle"


def test_steer_waiting_on_subagent_is_reported_and_cleared(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")]]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        await session.engine.emit({"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-1",
                                   "description": "语义检查"})
        await settle(manager, project_id, until=lambda s: s.open_subagents)
        _, steer = await manager.submit(project_id, root, "顺便看看命名")
        await session.engine.emit({"type": "subagent_finished", "task_id": "task-1", "tool_use_id": "tu-1",
                                   "description": "语义检查", "status": "completed"})
        await settle(manager, project_id, until=lambda s: not s.open_subagents)
        await session.engine.model_call()
        await session.engine.finish(release("validate"))
        await settle(manager, project_id)
        await manager.shutdown()
        return steer

    steer = asyncio.run(scenario())
    states = deliveries(manager, project_id, steer["message_id"])
    assert states[0]["state"] == "pending" and states[0]["waiting_on"]["kind"] == "subagent"
    assert states[0]["waiting_on"]["title"] == "语义检查" and states[0]["waiting_on"]["since"]
    assert states[1]["state"] == "pending" and states[1]["waiting_on"] is None
    assert states[-1]["state"] == "seen"


def test_engine_without_delivery_signals_keeps_one_result_per_send(steer_host):
    """不会插话、result 不带 message_ids 的引擎（旧版 CLI、旧假引擎）：插话当一条新消息发，下一个 result 之后它就是下一轮。"""
    manager, project_id, root, engine_cls = steer_host
    engine_cls["cls"] = FakeEngine
    FakeEngine.scripts = [[lock("validate", "start")],
                          [result(), {"type": "assistant_message", "blocks": [{"kind": "text", "text": "看过了"}]},
                           release("validate"), result()]]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        await running(manager, project_id)
        _, steer = await manager.submit(project_id, root, "再看一下第三条规则")
        await settle(manager, project_id, until=lambda s: s.state == "idle" and not s.steers and s.turn is None)
        await manager.shutdown()
        return steer

    steer = asyncio.run(scenario())
    assert FakeEngine.sent == ["/ontology-validate", "再看一下第三条规则"]
    assert [item["state"] for item in deliveries(manager, project_id, steer["message_id"])] == ["pending", "new_turn"]
    assert all(event["state"] != "idle" for event in between(manager, project_id, 0, 1, "session_state"))


# ---- 打断 ----


def test_interrupt_returns_unseen_steers_and_pauses_next(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")], [lock("extract-object", "source"), release("extract-object"),
                                                        result()]]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        engine = session.engine
        _, steer = await manager.submit(project_id, root, "先别改对象", meta={"display_text": "先别改对象"})
        await manager.submit(project_id, root, "/ontology-extract-object", meta={"command": "/ontology-extract-object"})
        await manager.interrupt(project_id)
        session = await settle(manager, project_id)
        returned = manager.returned_steers.get((project_id, "default"))
        paused = manager.next_view(project_id, "default")
        sent_before = list(FakeEngine.sent)
        _, started = await manager.start_next(project_id, "default")
        await settle(manager, project_id, until=lambda s: s.state == "idle" and len(FakeEngine.sent) == 2)
        await manager.shutdown()
        return InterruptRun(engine, steer, returned, paused, sent_before, started)

    engine, steer, returned, paused, sent_before, started = asyncio.run(scenario())
    assert engine.cancel_flags == [True] and engine.pending == []
    assert returned == [{"message_id": steer["message_id"], "text": "先别改对象"}]
    assert deliveries(manager, project_id, steer["message_id"])[-1]["state"] == "returned"
    # 打断之后不自动开始代存的命令；「现在开始」才开始
    assert paused["paused"] is True and sent_before == ["/ontology-validate"]
    assert started["delivery"] == "immediate" and FakeEngine.sent[-1] == "/ontology-extract-object"
    assert manager.next_view(project_id, "default") is None


def test_cancel_next_drops_command_task(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")]]
    task = command_tasks.create(project_id, origin={"page": "wb-l2"}, title="助手正在执行「对象提取」",
                                conversation_id="default", command="/ontology-extract-object")

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        await manager.submit(project_id, root, "/ontology-extract-object",
                             meta={"command": "/ontology-extract-object", "task_id": task["task_id"]})
        cancelled = manager.cancel_next(project_id, "default")
        await session.engine.finish(release("validate"))
        await settle(manager, project_id)
        await manager.shutdown()
        return cancelled

    assert asyncio.run(scenario()) is True
    assert command_tasks.get(project_id, task["task_id"])["state"] == "cancelled"
    assert FakeEngine.sent == ["/ontology-validate"]
    assert records(manager, project_id, {"next_updated"})[-1]["payload"]["next"] is None


# ---- 连续重复提交（develop !596）与代存命令的配合 ----


def test_duplicate_command_rejected_while_held_and_chained_start_still_runs(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")], [lock("extract-object", "source")]]
    command = "/ontology-extract-object"

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        await manager.submit(project_id, root, command, meta={"command": command})
        with pytest.raises(SessionError) as duplicate:
            await manager.submit(project_id, root, f"  {command}\n", meta={"command": command})
        # 平台在上一轮结束后接着开始代存的命令：同一条文字，不能被当成重复
        await session.engine.finish(release("validate"))
        await settle(manager, project_id, until=lambda s: len(FakeEngine.sent) == 2 and s.state == "running")
        held_after = manager.next_view(project_id, "default")
        await manager.shutdown()
        return duplicate.value, held_after

    duplicate, held_after = asyncio.run(scenario())
    assert duplicate.status == 409 and duplicate.code == "message_duplicate"
    assert FakeEngine.sent == ["/ontology-validate", command]
    assert held_after is None


def test_interrupt_and_cancel_next_allow_resubmitting_same_text(steer_host):
    manager, project_id, root, _ = steer_host
    FakeEngine.scripts = [[lock("validate", "start")], [lock("validate", "start")]]
    command = "/ontology-extract-object"

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await running(manager, project_id)
        await manager.submit(project_id, root, command, meta={"command": command})
        manager.cancel_next(project_id, "default")
        _, held_again = await manager.submit(project_id, root, command, meta={"command": command})
        _, steer = await manager.submit(project_id, root, "先别改对象")
        with pytest.raises(SessionError):
            await manager.submit(project_id, root, "先别改对象")
        await manager.interrupt(project_id)
        # 撤回的插话放回输入框后马上重发（「打断并发送」）：不等这一轮收尾也不算重复
        _, resent = await manager.submit(project_id, root, "先别改对象")
        await settle(manager, project_id, until=lambda s: s.state == "running" and len(FakeEngine.sent) == 2)
        await session.engine.finish()
        await settle(manager, project_id)
        await manager.shutdown()
        return held_again, steer, resent

    held_again, steer, resent = asyncio.run(scenario())
    assert held_again["delivery"] == "next" and steer["delivery"] == "steer"
    assert resent["delivery"] in ("immediate", "steer")


# ---- 接口：S-07 delivery、S-05 next、S-08 returned、next/cancel、next/start ----


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: SteerEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"name": "插话接口", "goal": "合成场景：图书借阅"}).json()["id"]
        yield client, manager, project_id
    streams.reset_all()


def wait(predicate, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("等待超时")


def test_double_click_command_cancels_second_task(api):
    client, manager, project_id = api
    base = f"/api/v1/projects/{project_id}"
    FakeEngine.scripts = [[lock("validate", "start")]]
    first = client.post(f"{base}/commands", json={"command": "/ontology-validate", "origin": {"page": "reports"}})
    assert first.status_code == 202
    cid = first.json()["conversation_id"]
    wait(lambda: manager.get(project_id) and manager.get(project_id).state == "running")
    again = client.post(f"{base}/commands", json={"command": "/ontology-validate", "conversation_id": cid,
                                                  "origin": {"page": "reports"}})
    assert again.status_code == 409 and again.json()["code"] == "message_duplicate"
    tasks = [task for task in command_tasks.query(project_id, states=list(command_tasks.STATES))
             if task["task_id"] != first.json()["task"]["task_id"]]
    assert [task["state"] for task in tasks] == ["cancelled"]
    assert client.get(f"{base}/conversations/{cid}").json()["next"] is None
    client.post(f"{base}/conversations/{cid}/interrupt")


def test_send_while_running_api_shapes(api):
    client, manager, project_id = api
    base = f"/api/v1/projects/{project_id}"
    FakeEngine.scripts = [[lock("validate", "start")]]
    first = client.post(f"{base}/commands", json={"command": "/ontology-validate", "origin": {"page": "reports"}})
    assert first.status_code == 202 and first.json()["delivery"] == "immediate"
    cid = first.json()["conversation_id"]
    wait(lambda: manager.get(project_id) and manager.get(project_id).state == "running")

    steer = client.post(f"{base}/conversations/{cid}/messages", json={"text": "补充：只看第三条"})
    assert steer.status_code == 202 and steer.json()["delivery"] == "steer" and steer.json()["message_id"]
    assert steer.json()["next"] is None

    held = client.post(f"{base}/commands", json={"command": "/ontology-extract-object", "conversation_id": cid,
                                                 "origin": {"page": "wb-l2", "label": "全部命令"}})
    assert held.status_code == 202, held.text
    body = held.json()
    catalog = client.get(f"{base}/commands/catalog").json()
    catalog_items = [item for group in catalog["groups"] for item in group["items"]]
    label = next(item["label"] for item in catalog_items if item["command"] == "/ontology-extract-object")
    assert body["delivery"] == "next" and body["message_id"] is None and body["task"]["state"] == "queued"
    assert set(body["next"]) == {"id", "label", "command", "text", "created_at", "paused", "origin"}
    assert body["next"]["label"] == label and body["next"]["origin"]["label"] == "全部命令"
    assert client.get(f"{base}/conversations/{cid}").json()["next"]["id"] == body["next"]["id"]

    occupied = client.post(f"{base}/commands", json={"command": "/ontology-extract-operation", "conversation_id": cid,
                                                     "origin": {"page": "wb-l2"}})
    assert occupied.status_code == 409 and occupied.json()["code"] == "next_occupied"
    assert occupied.json()["details"]["label"] == label
    replaced = client.post(f"{base}/conversations/{cid}/messages",
                           json={"text": "/ontology-extract-operation", "replace": True})
    assert replaced.status_code == 202 and replaced.json()["delivery"] == "next"
    assert replaced.json()["next"]["command"] == "/ontology-extract-operation"
    assert command_tasks.get(project_id, body["task"]["task_id"])["state"] == "cancelled"

    busy = client.post(f"{base}/conversations/{cid}/next/start")
    assert busy.status_code == 409 and busy.json()["code"] == "session_busy"

    stopped = client.post(f"{base}/conversations/{cid}/interrupt").json()
    assert stopped["interrupted"] is True
    assert stopped["returned"] == [{"message_id": steer.json()["message_id"], "text": "补充：只看第三条"}]
    wait(lambda: manager.get(project_id).state == "idle")
    assert client.get(f"{base}/conversations/{cid}").json()["next"]["paused"] is True

    assert client.post(f"{base}/conversations/{cid}/next/cancel").json() == {"next": None}
    assert client.get(f"{base}/conversations/{cid}").json()["next"] is None
    missing = client.post(f"{base}/conversations/{cid}/next/start")
    assert missing.status_code == 404 and missing.json()["code"] == "next_not_found"

    items = client.get(f"{base}/conversations/{cid}/messages").json()["items"]
    delivered = [item["data"] for item in items if item["type"] == "message.delivery"]
    assert [(item["message_id"], item["state"]) for item in delivered] == [(steer.json()["message_id"], "returned")]
    assert set(delivered[0]) == {"message_id", "state", "at", "waiting_on"}
    nexts = [item["data"] for item in items if item["type"] == "next.updated"]
    assert nexts == [{"next": None}]
    user_messages = [item["data"] for item in items if item["type"] == "message.user"]
    user = next(data for data in user_messages if data["message_id"] == steer.json()["message_id"])
    assert user["delivery"] == "steer"


# ---- Claude 引擎：读 CLI 的原始消息流 ----


class RawQuery:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.controls: list[dict] = []
        self.control_response: dict | None = None

    async def receive_messages(self):
        while True:
            item = await self.inbox.get()
            if item is None:
                return
            yield item

    async def _send_control_request(self, request: dict) -> dict | None:
        self.controls.append(request)
        return self.control_response


class RawClient:
    def __init__(self):
        self._query = RawQuery()
        self.written: list[dict] = []
        self.plain_interrupts = 0

    async def query(self, prompt):
        async for message in prompt:
            self.written.append(message)

    async def interrupt(self):
        self.plain_interrupts += 1


def claude_engine(tmp_path: Path):
    from app.services.engines.claude import ClaudeEngine

    async def permission(*_):
        return {"behavior": "allow", "updated_input": None}

    ctx = EngineContext(project_id="p-demo", cwd=tmp_path, request_permission=permission,
                        load_resume_token=lambda: None, save_resume_token=lambda token: None,
                        clear_resume_token=lambda: None, log_error=lambda kind, text: None)
    engine = ClaudeEngine(ctx)
    engine.client = RawClient()
    return engine


def lifecycle(command_uuid: str, state: str) -> dict:
    return {"type": "command_lifecycle", "command_uuid": command_uuid, "state": state, "session_id": "s-1"}


def raw_result(uuids: list[str] | None) -> dict:
    data = {"type": "result", "subtype": "success", "duration_ms": 10, "duration_api_ms": 8, "is_error": False,
            "num_turns": 1, "session_id": "s-1", "total_cost_usd": 0.0, "usage": {}, "result": "好了"}
    if uuids is not None:
        data["user_message_uuids"] = uuids
    return data


async def collect(engine, until_results: int) -> list[dict]:
    events = []
    async for event in engine.events():
        events.append(event)
        if sum(1 for item in events if item.get("type") == "result") >= until_results:
            break
    return events


def test_claude_steer_seen_mid_turn_and_result_lists_consumed(tmp_path):
    async def scenario():
        engine = claude_engine(tmp_path)
        await engine.send("开始校验", message_id="m-1")
        await engine.steer("只看第三条", "m-2")
        first, second = (message["uuid"] for message in engine.client.written)
        for item in (lifecycle(first, "queued"), lifecycle(first, "started"), lifecycle(second, "queued"),
                     lifecycle(second, "started"), lifecycle(second, "completed"), raw_result([first, second])):
            engine.client._query.inbox.put_nowait(item)
        return engine, await collect(engine, 1)

    engine, events = asyncio.run(scenario())
    assert all(message["type"] == "user" and message["uuid"] for message in engine.client.written)
    assert [event for event in events if event["type"] == "_delivery"] == [
        {"type": "_delivery", "message_id": "m-2", "state": "seen"}]
    assert next(event for event in events if event["type"] == "result")["message_ids"] == ["m-1", "m-2"]


def test_claude_late_steer_becomes_new_turn(tmp_path):
    async def scenario():
        engine = claude_engine(tmp_path)
        await engine.send("开始校验", message_id="m-1")
        await engine.steer("只看第三条", "m-2")
        first, second = (message["uuid"] for message in engine.client.written)
        for item in (lifecycle(first, "started"), lifecycle(second, "queued"), raw_result([first]),
                     lifecycle(first, "completed"), lifecycle(second, "started"), raw_result([second])):
            engine.client._query.inbox.put_nowait(item)
        return await collect(engine, 2)

    events = asyncio.run(scenario())
    kinds = [(event["type"], event.get("state") or event.get("message_ids")) for event in events
             if event["type"] in ("_delivery", "result")]
    assert kinds == [("result", ["m-1"]), ("_delivery", "new_turn"), ("result", ["m-2"])]


def test_claude_interrupt_cancels_queued_and_reports_withdrawn(tmp_path):
    async def scenario():
        engine = claude_engine(tmp_path)
        await engine.send("开始校验", message_id="m-1")
        await engine.steer("只看第三条", "m-2")
        second = engine.client.written[1]["uuid"]
        engine.client._query.control_response = {"still_queued": [], "cancelled": [second]}
        # 真实 CLI 先在消息流里报 cancelled，再回打断的应答
        engine._on_lifecycle(lifecycle(second, "cancelled"))
        return engine, await engine.interrupt(cancel_queued=True)

    engine, withdrawn = asyncio.run(scenario())
    assert withdrawn == ["m-2"]
    assert engine.client._query.controls == [{"subtype": "interrupt", "cancel_queued": True}]
    assert engine.client.plain_interrupts == 0


def test_claude_without_lifecycle_reports_unknown_consumption(tmp_path):
    """这版 CLI 不报送达：result 的 message_ids 为 None，会话层按一次发送一个 result 处理。"""
    async def scenario():
        engine = claude_engine(tmp_path)
        await engine.send("开始校验", message_id="m-1")
        engine.client._query.inbox.put_nowait(raw_result([engine.client.written[0]["uuid"]]))
        return await collect(engine, 1)

    events = asyncio.run(scenario())
    assert next(event for event in events if event["type"] == "result")["message_ids"] is None


def test_claude_subagent_waiting_on_model_reports_thinking(tmp_path):
    """子代理一次模型调用久了（只在思考、不调工具）：和 Jiuwen 一样按间隔报「思考中」，它说了话就停。"""
    started = {"type": "system", "subtype": "task_started", "task_id": "task-1", "tool_use_id": "tu-1",
               "description": "语义检查", "task_type": "general-purpose", "session_id": "s-1", "uuid": "u-1"}
    said = {"type": "assistant", "parent_tool_use_id": "tu-1", "session_id": "s-1", "uuid": "u-2",
            "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "查完了"}]}}

    async def scenario():
        engine = claude_engine(tmp_path)
        engine._thinking_every = 0.05
        inbox = engine.client._query.inbox
        events = []

        async def read():
            async for event in engine.events():
                events.append(event)

        reader = asyncio.create_task(read())
        inbox.put_nowait(started)
        await asyncio.sleep(0.18)
        inbox.put_nowait(said)
        await asyncio.sleep(0.05)
        quiet = len(events)
        await asyncio.sleep(0.15)
        reader.cancel()
        return events, quiet

    events, quiet = asyncio.run(scenario())
    thinking = [event for event in events if event.get("kind") == "thinking"]
    assert len(thinking) >= 2
    assert all(event["type"] == "subagent_activity" and event["task_id"] == "task-1"
               and event["tool_use_id"] == "tu-1" and event["step_id"] == "task-1-thinking" for event in thinking)
    assert thinking[-1]["elapsed_ms"] > thinking[0]["elapsed_ms"] and thinking[0]["since"]
    assert len(events) == quiet  # 子代理说了话：这次调用结束，不再报


# ---- Jiuwen 引擎：主 agent 的插话 Rail、没插上的开下一轮、打断撤回 ----


def jiuwen_engine(tmp_path: Path):
    from app.services.engines.jiuwen import JiuwenEngine
    from tests.test_session_injection import make_ctx

    return JiuwenEngine(make_ctx(tmp_path))


def drain(engine) -> list[dict]:
    events = []
    while not engine._queue.empty():
        events.append(engine._queue.get_nowait())
    return events


def test_jiuwen_steer_rail_injects_only_into_main_agent():
    from app.services.engines import jiuwen_progress
    from app.services.engines.jiuwen import _steer_rail

    pending = [("m-2", "只看第三条")]
    delivered: list[list[str]] = []
    added: list = []

    def take():
        items = list(pending)
        pending.clear()
        return items

    async def mark(message_ids):
        delivered.append(message_ids)

    async def add_messages(message):
        added.append(message)

    rail = _steer_rail(take, mark)
    ctx = SimpleNamespace(inputs=SimpleNamespace(model_context=SimpleNamespace(add_messages=add_messages)),
                          context=None)

    async def scenario():
        token = jiuwen_progress._CURRENT_TASK.set(object())  # 子代理的模型调用：不插
        try:
            await rail.before_model_call(ctx)
        finally:
            jiuwen_progress._CURRENT_TASK.reset(token)
        assert added == [] and pending
        await rail.before_model_call(ctx)

    asyncio.run(scenario())
    assert len(added) == 1 and "只看第三条" in added[0].content and "不是新的一轮" in added[0].content
    assert delivered == [["m-2"]] and pending == []


def test_jiuwen_unseen_steer_starts_next_turn_and_results_list_consumed(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    gate: dict = {}
    queries: list[str] = []

    def fake_stream(agent, inputs, session=None):
        queries.append(inputs["query"])
        first = len(queries) == 1

        async def gen():
            if first:
                gate["running"].set()
                await gate["release"].wait()
            yield SimpleNamespace(type="llm_output", payload={"content": "好"})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = jiuwen_engine(tmp_path)
    engine.client = object()

    async def scenario():
        gate["running"], gate["release"] = asyncio.Event(), asyncio.Event()
        await engine.send("开始校验", message_id="m-1")
        await asyncio.wait_for(gate["running"].wait(), timeout=2)
        await engine.steer("先看第一条", "m-2")
        # 主 agent 的一次模型调用把它插了进去（Rail 做的事）
        await engine._steers_delivered([message_id for message_id, _ in engine._take_steers()])
        await engine.steer("再看第三条", "m-3")
        gate["release"].set()
        await asyncio.wait_for(engine._turn_task, timeout=5)
        return drain(engine)

    events = asyncio.run(scenario())
    marks = [(event["type"], event.get("message_id") or event.get("message_ids"), event.get("state"))
             for event in events if event["type"] in ("_delivery", "result")]
    assert marks == [("_delivery", "m-2", "seen"), ("result", ["m-1", "m-2"], None),
                     ("_delivery", "m-3", "new_turn"), ("result", ["m-3"], None)]
    assert queries[1].endswith("再看第三条")


def test_jiuwen_interrupt_with_cancel_queued_withdraws_steers(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    gate: dict = {}

    def fake_stream(agent, inputs, session=None):
        async def gen():
            gate["running"].set()
            await asyncio.Event().wait()
            yield SimpleNamespace(type="llm_output", payload={"content": "不会到这里"})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = jiuwen_engine(tmp_path)

    async def scenario():
        gate["running"] = asyncio.Event()
        engine.client = SimpleNamespace(abort=lambda: engine._turn_task.cancel())
        await engine.send("开始校验", message_id="m-1")
        await asyncio.wait_for(gate["running"].wait(), timeout=2)
        await engine.steer("先别改", "m-2")
        withdrawn = await engine.interrupt(cancel_queued=True)
        await asyncio.sleep(0.05)
        return withdrawn, drain(engine)

    withdrawn, events = asyncio.run(scenario())
    assert withdrawn == ["m-2"] and engine._steers == []
    assert [event for event in events if event["type"] == "_delivery"] == []
    results = [event for event in events if event["type"] == "result"]
    assert len(results) == 1 and results[0]["subtype"] == "interrupted" and results[0]["message_ids"] == ["m-1"]
