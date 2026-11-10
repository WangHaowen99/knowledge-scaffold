"""空闲回收对用户看不出来（ADR-0089）：命令停着等你下一句时被回收，不记中断、不放续跑卡，下一条消息照常接着做；
在跑时被回收仍记中断；开着确认卡的会话不回收，回答照常交给助手。"""
from __future__ import annotations

import asyncio

from app import config
from app.services.agent_sessions import PendingPermission
from app.services.session_host import exec_records
from tests.test_session_host import FakeEngine, host, lock, project_history, release, result, settle  # noqa: F401


def _text(text: str) -> dict:
    return {"type": "assistant_message", "blocks": [{"kind": "text", "text": text}]}


def _waiting(project_id):
    return lambda s: s.exec_id and (exec_records.get(project_id, s.exec_id) or {}).get("state") == "waiting"


def test_idle_recycle_keeps_a_step_waiting_for_a_typed_reply(host, monkeypatch):
    manager, project_id, root = host
    monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", 60)
    FakeEngine.scripts = [
        [lock("validate", "confirm"), _text("找到 2 个问题，要处理哪些？回复全部或编号。"), result()],
        [release("validate"), _text("两条都改好了。"), result()],
    ]

    async def scenario():
        await manager.send(project_id, root, "/ontology-validate")
        session = await settle(manager, project_id, until=_waiting(project_id))
        exec_id = session.exec_id
        session.last_activity = 0  # 用户想了很久
        reaped = await manager.reap_idle()
        after_reap = exec_records.get(project_id, exec_id)
        gone = manager.get(project_id) is None
        await manager.send(project_id, root, "全部")
        await settle(manager, project_id, until=lambda s: s.state == "idle" and s.exec_id is None)
        await manager.shutdown()
        return reaped, gone, after_reap, exec_records.get(project_id, exec_id)

    reaped, gone, after_reap, final = asyncio.run(scenario())
    assert reaped == 1 and gone
    assert after_reap["state"] == "waiting" and not after_reap.get("interrupted")
    kinds = [item["type"] for item in project_history(manager, project_id)]
    assert "session.interrupted" not in kinds
    assert not [item for item in project_history(manager, project_id)
                if item["type"] == "card.created" and (item["data"].get("card_type") == "resume")]
    # 回复照常交给助手，接着这份执行做完
    assert FakeEngine.sent[-1] == "全部"
    assert final["state"] == "done"


def test_recycle_while_running_still_interrupts(host):
    manager, project_id, root = host
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    FakeEngine.scripts = [
        [lock("extract-operation", "source"),
         {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Bash", "tool_use_id": "tu-1",
                                                   "input": {"command": "python3 x.py", "description": "抽取操作"}}]}],
    ]

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-operation")
        session = await settle(manager, project_id, until=lambda s: s.state == "running" and s.turn and s.turn.steps)
        exec_id = session.exec_id
        await manager.recycle_project(project_id)
        record = exec_records.get(project_id, exec_id)
        await manager.shutdown()
        return record

    record = asyncio.run(scenario())
    assert record["state"] == "interrupted" and record["interrupted"]["reason"] == "recycled"


def test_open_question_is_not_recycled_and_the_answer_reaches_the_assistant(host, monkeypatch):
    manager, project_id, root = host
    monkeypatch.setattr(config, "SESSION_IDLE_TTL_SECONDS", 60)
    FakeEngine.scripts = [[_text("先读资料"), result()]]

    async def scenario():
        await manager.send(project_id, root, "看看资料")
        session = await settle(manager, project_id)
        future = asyncio.get_running_loop().create_future()
        question = {"question_id": "q-1", "title": "用哪个口径", "options": [
            {"id": "0.0", "key": "A", "label": "按门店", "recommended": True, "custom": False},
            {"id": "0.1", "key": "B", "label": "按仓库", "recommended": False, "custom": False}]}
        session.pending_permissions["r1"] = PendingPermission("r1", "AskUserQuestion",
                                                              {"questions": [{"question": "用哪个口径"}]}, None, None,
                                                              future=future, questions=[question])
        session.last_activity = 0
        reaped = await manager.reap_idle()
        still_there = manager.get(project_id) is session
        manager.answer_question(project_id, "q-1", {"option_id": "0.0"})
        decision = await asyncio.wait_for(future, timeout=2)
        session.pending_permissions.clear()
        await manager.shutdown()
        return reaped, still_there, decision

    reaped, still_there, decision = asyncio.run(scenario())
    assert reaped == 0 and still_there
    assert decision["behavior"] == "allow"
    assert "按门店" in str(decision.get("updated_input"))
