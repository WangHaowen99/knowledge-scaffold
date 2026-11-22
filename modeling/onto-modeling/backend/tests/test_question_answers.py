"""决策卡作答（契约 S-10、S-11、EC-32～EC-34）：答案完整交给助手、结构化记进历史，无效和失效的答复不放行。

合成访谈题目，不起真实模型。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import PendingPermission, Session, SessionError, SessionManager
from app.services.session_host import timeline
from app.services.workspace import WorkspaceManager

ASK = {"questions": [
    {"question": "合成多选", "multiSelect": True,
     "options": [{"label": "甲,乙"}, {"label": "方案二"}, {"label": "方案三"}]},
    {"question": "合成单选", "options": [{"label": "方案一"}, {"label": "方案二"}]},
    {"question": "合成文字", "options": [{"label": "Other"}]},
]}


def _session(manager: SessionManager, project_id: str = "project_alpha", **fields) -> Session:
    session = Session(id="session_demo", project_id=project_id, **fields)
    manager._sessions[project_id] = session
    return session


async def _ask(manager: SessionManager, session: Session, tool_input: dict) -> tuple[asyncio.Task, PendingPermission]:
    pending = asyncio.create_task(manager._request_permission(session, "AskUserQuestion", tool_input, None, None))
    for _ in range(100):
        if session.pending_permissions or pending.done():
            break
        await asyncio.sleep(0)
    return pending, next(iter(session.pending_permissions.values()), None)


def _events(session: Session, kind: str) -> list[dict]:
    return [event["payload"] for event in session.events if event["type"] == kind]


# ---- HTTP：逐题回答、历史回显 ----


@pytest.fixture()
def api(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(deps, "workspaces", WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR)))
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = SessionManager()
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    streams.reset_all()
    with TestClient(app) as client:
        project_id = client.post("/api/v1/projects", json={"name": "访谈作答", "goal": "合成场景：图书借阅"}).json()["id"]
        yield client, manager, project_id
    streams.reset_all()


def test_answers_round_trip_through_http_and_history(api):
    client, manager, project_id = api
    session = _session(manager, project_id)
    base = f"/api/v1/projects/{project_id}/conversations/{session.conversation_id}"
    pending = client.portal.start_task_soon(manager._request_permission, session, "AskUserQuestion", ASK, None, None)
    deadline = time.time() + 5
    while not session.pending_permissions and time.time() < deadline:
        time.sleep(0.02)
    request = next(iter(session.pending_permissions.values()))
    many, one, text = [item["question_id"] for item in request.questions]

    # 决策卡不能经确认接口空放行，请求留着等回答
    refused = client.post(f"{base}/permissions/{request.id}", json={"behavior": "allow"})
    assert refused.status_code == 400 and refused.json()["code"] == "answer_invalid"
    invalid = client.post(f"{base}/questions/{one}/answer", json={"option_ids": ["1.0", "1.1"]})
    assert invalid.status_code == 400 and invalid.json()["code"] == "answer_invalid"
    assert not pending.done()

    answered = client.post(f"{base}/questions/{many}/answer",
                           json={"option_ids": ["0.0", "0.1", "0.custom"], "custom_text": "人工合成补充"})
    assert answered.status_code == 200 and answered.json()["question"]["state"] == "answered"
    assert client.post(f"{base}/questions/{one}/answer", json={"option_id": "1.1"}).status_code == 200
    again = client.post(f"{base}/questions/{one}/answer", json={"option_id": "1.0"})
    assert again.status_code == 410 and again.json()["code"] == "question_gone"
    assert not pending.done()  # 一次提问的题都答完才交给助手
    assert client.post(f"{base}/questions/{text}/answer",
                       json={"option_id": "2.custom", "custom_text": "人工合成回答"}).status_code == 200

    decision = pending.result(timeout=5)
    assert decision == {"behavior": "allow", "updated_input": {**ASK, "answers": {
        "合成多选": "甲,乙、方案二、人工合成补充", "合成单选": "方案二", "合成文字": "人工合成回答"}}}

    # 换一个管理器（相当于服务重启）：历史里仍有结构化的选项与文字，问题是已回答
    manager._sessions.clear()
    items = client.get(f"{base}/messages").json()["items"]
    asked = {item["data"]["question_id"]: item["data"] for item in items if item["type"] == "question.asked"}
    answers = {item["data"]["question_id"]: item["data"] for item in items if item["type"] == "question.answered"}
    assert {item["state"] for item in asked.values()} == {"answered"}
    assert answers[many]["answer"] == {"option_ids": ["0.0", "0.1", "0.custom"], "custom_text": "人工合成补充"}
    assert answers[one]["answer"] == {"option_id": "1.1"}
    assert answers[text]["answer"] == {"option_id": "2.custom", "custom_text": "人工合成回答"}
    assert all(item["by"] == "user" for item in answers.values())


# ---- 无效答复：400，请求留着，可以更正或拒绝 ----

SINGLE = {"questions": [{"question": "合成问题", "options": [{"label": "方案一"}, {"label": "方案二"}]}]}
MULTI = {"questions": [{**SINGLE["questions"][0], "multiSelect": True}]}


@pytest.mark.parametrize("tool_input, answer", [
    (SINGLE, {}),
    (SINGLE, {"custom_text": "   "}),
    (SINGLE, {"custom_text": ["错误类型"]}),
    (SINGLE, {"option_id": "9.9"}),
    (SINGLE, {"option_id": ["0.0"]}),
    (SINGLE, {"option_id": "0.custom"}),
    (SINGLE, {"option_ids": ["0.0", "0.1"]}),
    (SINGLE, {"option_id": "0.0", "custom_text": "人工合成补充"}),
    (MULTI, {"option_ids": "0.0"}),
    (MULTI, {"option_ids": [42]}),
    (MULTI, {"option_ids": ["0.0", "未知选项"]}),
    (MULTI, {"option_ids": ["0.0", "0.0"]}),
    (MULTI, {"option_ids": ["0.0", "0.custom"]}),
])
def test_invalid_answer_keeps_request_pending(tool_input, answer):
    async def run():
        manager = SessionManager()
        session = _session(manager, discarded=True)
        pending, request = await _ask(manager, session, tool_input)
        question_id = request.questions[0]["question_id"]
        with pytest.raises(SessionError) as caught:
            manager.answer_question(session.project_id, question_id, answer)
        assert caught.value.status == 400
        assert not request.future.done() and not request.answers
        assert not _events(session, "question_answered")
        manager.answer_question(session.project_id, question_id, {"option_id": "0.1"})
        assert (await pending)["updated_input"]["answers"] == {"合成问题": "方案二"}

    asyncio.run(run())


def test_allow_without_answers_is_rejected_and_deny_withdraws():
    async def run():
        manager = SessionManager()
        session = _session(manager, discarded=True)
        pending, request = await _ask(manager, session, SINGLE)
        with pytest.raises(SessionError) as caught:
            await manager.respond_permission(session.project_id, request.id, "allow")
        assert caught.value.status == 400 and caught.value.code == "answer_invalid"
        assert not request.future.done()
        reason = "取消合成测试"
        await manager.respond_permission(session.project_id, request.id, "deny", message=reason)
        assert await pending == {"behavior": "deny", "message": reason}
        assert not session.pending_permissions
        assert _events(session, "question_withdrawn") == [{"question_id": request.questions[0]["question_id"],
                                                          "reason": reason}]

    asyncio.run(run())


# ---- 出不了卡的提问：不等人，原因交还助手 ----


@pytest.mark.parametrize("questions, reason", [
    (None, "缺少有效的问题列表"),
    ([], "缺少有效的问题列表"),
    ("invalid", "缺少有效的问题列表"),
    ([None], "第 1 题缺少有效的问题文本"),
    ([{"question": "  "}], "第 1 题缺少有效的问题文本"),
    ([{"question": "重复"}, {"question": "重复"}], "第 2 题的问题文本重复"),
    ([{"question": "合成", "options": "方案一"}], "第 1 题的选项必须是带有非空 label 的列表"),
    ([{"question": "合成", "options": [{"label": " "}]}], "第 1 题的选项必须是带有非空 label 的列表"),
    ([{"question": "合成", "options": [None]}], "第 1 题的选项必须是带有非空 label 的列表"),
])
def test_malformed_questions_go_back_to_the_assistant(questions, reason):
    async def run():
        manager = SessionManager()
        session = _session(manager, discarded=True)
        decision = await manager._request_permission(session, "AskUserQuestion", {"questions": questions}, None, None)
        assert decision == {"behavior": "deny", "message": f"{reason}，请修正后重新提问"}
        assert not session.pending_permissions and not session.events

    asyncio.run(run())


# ---- 占位选项不出卡 ----


@pytest.mark.parametrize("option", [
    {"label": "Other"}, {"label": "other "}, {"label": "其他回答"}, {"label": "自定义回答"},
    {"label": "点 Other 输入你的回答"}, {"label": "选Other后填写"},
    {"label": "占位项"}, {"label": "占位选项一"}, {"label": "占位示例2"},
    {"label": "方案甲", "description": "占位示例，仅说明格式"}, {"label": "方案甲", "description": "仅为占位"},
    "Other",
])
def test_placeholder_options_become_the_text_input(option):
    question = timeline.build_questions(
        "req", {"questions": [{"question": "合成文字", "options": [option, {"label": "有效选项"}]}]}, conversation_id="c")[0]
    assert [(item["id"], item["label"]) for item in question["options"]] == [
        ("0.0", "有效选项"), ("0.custom", timeline.CUSTOM_OPTION_LABEL)]


@pytest.mark.parametrize("label", ["其他", "其它", "其他方案继续使用", "Another option", "占位符替换规则"])
def test_meaningful_options_are_kept(label):
    question = timeline.build_questions(
        "req", {"questions": [{"question": "合成单选", "options": [{"label": label}]}]}, conversation_id="c")[0]
    assert [item["label"] for item in question["options"]] == [label, timeline.CUSTOM_OPTION_LABEL]


# ---- 失效：超时、重启、已处理 ----


def test_timeout_withdraws_the_question(monkeypatch):
    monkeypatch.setattr(config, "PERMISSION_TIMEOUT_SECONDS", 0)

    async def run():
        manager = SessionManager()
        session = _session(manager, discarded=True)
        decision = await manager._request_permission(session, "AskUserQuestion", SINGLE, None, None)
        assert decision["behavior"] == "deny"
        assert [item["reason"] for item in _events(session, "question_withdrawn")] == ["等待回答超时"]
        assert not session.pending_permissions

    asyncio.run(run())


@pytest.mark.parametrize("case", ["no_session", "missing_request", "already_resolved"])
def test_expired_answers_are_rejected_without_sending_or_saving(case, monkeypatch):
    async def run():
        manager = SessionManager()
        session = Session(id="session_demo", project_id="project_alpha", discarded=True)
        if case != "no_session":
            manager._sessions[session.project_id] = session
        if case == "already_resolved":
            future = asyncio.get_running_loop().create_future()
            future.set_result({"behavior": "deny"})
            session.pending_permissions["request_demo"] = PendingPermission(
                id="request_demo", tool_name="AskUserQuestion", tool_input=SINGLE, title=None, description=None,
                future=future, questions=timeline.build_questions("request_demo", SINGLE, conversation_id="default"))

        async def unexpected(*_args, **_kwargs):
            pytest.fail("失效答案不得自动发送为消息")

        monkeypatch.setattr(manager, "send", unexpected)
        monkeypatch.setattr(manager, "submit", unexpected)
        with pytest.raises(SessionError) as caught:
            manager.answer_question(session.project_id, "request_demo", {"custom_text": "合成回答"})
        assert caught.value.status == 410
        assert session.events == []

    asyncio.run(run())


def test_waiting_for_an_answer_is_not_an_engine_stall(monkeypatch):
    """等回答的时间不算引擎无响应：等着的时候、答完之后都不判卡死。"""
    async def run():
        manager = SessionManager()
        session = _session(manager, discarded=True)
        session.engine = object()
        session.state = "running"
        session.last_activity = 0
        pending, request = await _ask(manager, session, SINGLE)
        assert not agent_sessions._stalled(session, time.time(), 60)
        manager.answer_question(session.project_id, request.questions[0]["question_id"], {"option_id": "0.0"})
        await pending
        assert time.time() - session.last_activity < 5
        assert not agent_sessions._stalled(session, time.time(), 60)

    asyncio.run(run())
