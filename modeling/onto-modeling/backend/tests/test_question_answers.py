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


def test_invalid_question_can_be_declined_for_reask():
    async def run():
        manager = agent_sessions.SessionManager()
        session = agent_sessions.Session(id="session_demo", project_id="project_alpha", discarded=True)
        manager._sessions[session.project_id] = session
        pending = asyncio.create_task(manager._request_permission(
            session, "AskUserQuestion", {"questions": [None]}, None, None))
        await asyncio.sleep(0)
        request_id = next(iter(session.pending_permissions))
        reason = "合成格式错误，请修正后重新提问"
        await manager.respond_permission(session.project_id, request_id, "deny", message=reason)
        assert await pending == {"behavior": "deny", "message": reason}
        assert not session.pending_permissions
        assert session.events[-1]["behavior"] == "deny"
        assert "reason" not in session.events[-1]

    asyncio.run(run())


def test_forwarded_answers_survive_history_reload(monkeypatch, tmp_path):
    manager, session = _forwarding_env(monkeypatch)
    history_path = tmp_path / "synthetic_history.jsonl"
    monkeypatch.setattr(agent_sessions, "_history_path", lambda *_: history_path)
    session.discarded = False
    session.events = []
    for event in [
        {"type": "permission_request", "request_id": "request_demo", "tool_name": "AskUserQuestion",
         "tool_input": {"questions": [{"question": "合成问题"}]}},
        {"type": "permission_resolved", "request_id": "request_demo", "behavior": "expired"},
    ]:
        session.broadcast(event)

    async def send(*_args, **_kwargs):
        return session

    monkeypatch.setattr(manager, "send", send)
    asyncio.run(manager.forward_question_answer(session.project_id, tmp_path, "request_demo", {"合成问题": "合成回答"}))
    saved = agent_sessions.load_history(session.project_id)[-1]
    assert saved["behavior"] == "forwarded"
    assert saved["answers"] == {"合成问题": "合成回答"}


def test_timeout_reason_is_preserved_without_changing_deny_contract(monkeypatch):
    monkeypatch.setattr(agent_sessions.config, "PERMISSION_TIMEOUT_SECONDS", 0)

    async def run():
        manager = agent_sessions.SessionManager()
        session = agent_sessions.Session(id="session_demo", project_id="project_alpha", discarded=True)
        decision = await manager._request_permission(
            session, "AskUserQuestion", {"questions": [{"question": "合成问题"}]}, None, None)
        assert decision["behavior"] == "deny"
        assert session.events[-1]["reason"] == "timeout"
        assert not session.pending_permissions

    asyncio.run(run())


def _forwarding_env(monkeypatch):
    manager = agent_sessions.SessionManager()
    session = agent_sessions.Session(id="session_demo", project_id="project_alpha", discarded=True)
    session.events = [
        {"type": "permission_request", "request_id": "request_demo", "tool_name": "AskUserQuestion",
         "tool_input": {"questions": [{"question": "合成问题"}]}},
        {"type": "permission_resolved", "request_id": "request_demo", "behavior": "expired"},
    ]
    manager._sessions[session.project_id] = session
    monkeypatch.setattr(manager, "resolve_conversation", lambda _project, conversation=None: conversation or "default")
    return manager, session


def test_forwarded_answers_are_saved_only_after_acceptance_and_cannot_repeat(monkeypatch, tmp_path):
    manager, session = _forwarding_env(monkeypatch)
    sends = []

    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            sends.append((args, kwargs))
            started.set()
            await finish.wait()
            return session

        monkeypatch.setattr(manager, "send", send)
        answers = {"合成问题": "合成回答"}
        pending = asyncio.create_task(manager.forward_question_answer(
            session.project_id, tmp_path, "request_demo", answers, "default", "operator_a"))
        await started.wait()
        assert session.events[-1]["behavior"] == "expired"
        with pytest.raises(agent_sessions.SessionError, match="正在补发"):
            await manager.forward_question_answer(session.project_id, tmp_path, "request_demo", answers)
        finish.set()
        assert await pending is session
        assert session.events[-1]["behavior"] == "forwarded"
        assert session.events[-1]["answers"] == answers
        assert sends == [((session.project_id, tmp_path, "合成问题\n我的回答：合成回答", "default"), {"username": "operator_a"})]
        with pytest.raises(agent_sessions.SessionError, match="已处理"):
            await manager.forward_question_answer(session.project_id, tmp_path, "request_demo", answers)
        assert len(sends) == 1

    asyncio.run(run())


def test_forward_failure_keeps_expired_state_and_allows_retry(monkeypatch, tmp_path):
    manager, session = _forwarding_env(monkeypatch)

    async def run():
        async def fail(*_args, **_kwargs):
            raise agent_sessions.SessionError("合成发送冲突", 409)

        monkeypatch.setattr(manager, "send", fail)
        with pytest.raises(agent_sessions.SessionError, match="合成发送冲突"):
            await manager.forward_question_answer(session.project_id, tmp_path, "request_demo", {"合成问题": "合成回答"})
        assert session.events[-1]["behavior"] == "expired"
        assert not manager._forwarding_questions

        async def accept(*_args, **_kwargs):
            return session

        monkeypatch.setattr(manager, "send", accept)
        await manager.forward_question_answer(session.project_id, tmp_path, "request_demo", {"合成问题": "合成回答"})
        assert session.events[-1]["behavior"] == "forwarded"

    asyncio.run(run())


@pytest.mark.parametrize("case", ["missing", "incomplete", "live", "answered", "cancelled"])
def test_forward_rejects_unknown_incomplete_or_already_handled_questions(case, monkeypatch, tmp_path):
    manager, session = _forwarding_env(monkeypatch)

    async def run():
        if case == "live":
            session.pending_permissions["request_demo"] = object()
        elif case == "answered":
            session.events[-1]["behavior"] = "allow"
        elif case == "cancelled":
            session.events[-1]["behavior"] = "deny"
        with pytest.raises(agent_sessions.SessionError):
            await manager.forward_question_answer(session.project_id, tmp_path,
                "unknown_request" if case == "missing" else "request_demo",
                {} if case == "incomplete" else {"合成问题": "合成回答"})
        assert not manager._forwarding_questions
        assert len(session.events) == 2

    asyncio.run(run())
