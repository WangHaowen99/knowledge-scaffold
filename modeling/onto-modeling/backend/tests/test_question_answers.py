"""合成访谈控件回归：完整答案回填、持久化与无效提交。"""

import asyncio
import time

import pytest

from app.services import agent_sessions


def test_question_answers_are_forwarded_and_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_sessions, "_history_path", lambda *_: tmp_path / "history.jsonl")

    async def run():
        manager = agent_sessions.SessionManager()
        session = agent_sessions.Session(id="session_demo", project_id="project_alpha")
        session.engine = object()
        session.state = "running"
        session.last_activity = 0
        manager._sessions[session.project_id] = session
        questions = [{"question": name} for name in ["合成单选", "合成多选", "合成文字一", "合成文字二"]]
        answers = {"合成单选": "方案一", "合成多选": "渠道一,渠道二",
                   "合成文字一": "人工合成回答一", "合成文字二": "人工合成回答二"}
        pending = asyncio.create_task(manager._request_permission(
            session, "AskUserQuestion", {"questions": questions}, None, None))
        await asyncio.sleep(0)
        request_id = next(iter(session.pending_permissions))
        assert not agent_sessions._stalled(session, time.time(), 60)
        await manager.respond_permission(session.project_id, request_id, "allow", answers=answers)
        decision = await pending
        assert time.time() - session.last_activity < 5
        assert not agent_sessions._stalled(session, time.time(), 60)
        assert decision["updated_input"]["answers"] == answers
        assert session.events[-1]["answers"] == answers
        assert agent_sessions.load_history(session.project_id)[-1]["answers"] == answers

    asyncio.run(run())


@pytest.mark.parametrize("answers", [None, {}, {"合成文字一": "人工合成回答一"},
    {"合成文字一": "人工合成回答一", "合成文字二": "  "},
    {"合成文字一": "人工合成回答一", "合成文字二": ["错误类型"]},
    {"合成文字一": "人工合成回答一", "合成文字二": "人工合成回答二", "额外问题": "值"}])
def test_incomplete_answers_keep_request_pending(answers):
    async def run():
        manager = agent_sessions.SessionManager()
        session = agent_sessions.Session(id="session_demo", project_id="project_alpha", discarded=True)
        manager._sessions[session.project_id] = session
        request = agent_sessions.PendingPermission(
            id="request_demo", tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "合成文字一"}, {"question": "合成文字二"}]},
            title=None, description=None, future=asyncio.get_running_loop().create_future())
        session.pending_permissions[request.id] = request
        with pytest.raises(agent_sessions.SessionError) as caught:
            await manager.respond_permission(session.project_id, request.id, "allow", answers=answers)
        assert caught.value.status == 400
        assert not request.future.done()
        await manager.respond_permission(session.project_id, request.id, "deny", message="取消合成测试")
        assert request.future.result()["behavior"] == "deny"

    asyncio.run(run())


@pytest.mark.parametrize("questions", [[], "invalid", [None], [{"question": "  "}],
    [{"question": "重复"}, {"question": "重复"}]])
def test_invalid_questions_keep_request_pending(questions):
    async def run():
        manager = agent_sessions.SessionManager()
        session = agent_sessions.Session(id="session_demo", project_id="project_alpha", discarded=True)
        manager._sessions[session.project_id] = session
        request = agent_sessions.PendingPermission(
            id="request_demo", tool_name="AskUserQuestion", tool_input={"questions": questions},
            title=None, description=None, future=asyncio.get_running_loop().create_future())
        session.pending_permissions[request.id] = request
        with pytest.raises(agent_sessions.SessionError) as caught:
            await manager.respond_permission(session.project_id, request.id, "allow", answers={"重复": "值"})
        assert caught.value.status == 400
        assert not request.future.done()

    asyncio.run(run())
