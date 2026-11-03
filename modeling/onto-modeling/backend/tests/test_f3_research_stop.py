"""资料分析停在蓝图确认（F3 修 F2-engine 第 7 条引入的回归）。

测试部署上的现场（用户手测项目的会话历史第 661～662 条）：自主构建项目开始前的资料分析，蓝图写完后助手用提问问
「资料分析和建模蓝图都已完成，划分了 3 段。接下来怎么建？」，策略代答选了「提取对象（推荐）」，助手就在蓝图还是草案时
把第 1 段的对象建了出来。现在：策略只代答业务歧义；资料分析里（任何构建方式）决定流程走向的问题不出卡、不代答，
回答「停在蓝图确认」。

测试数据全部为合成内容（问题文字仿照现场的句式）；假引擎见 tests/autobuild_helpers.py。
"""

from __future__ import annotations

from tests.autobuild_helpers import ab  # noqa: F401


FLOW_QUESTION = {
    "question": "资料分析和建模蓝图都已完成，划分了 3 段。接下来怎么建？", "header": "置信度 0.80",
    "options": [{"label": "提取对象（推荐）", "description": "从第一段开始提取业务对象"},
                {"label": "先画流程图", "description": "先为第一段创建业务流程图"},
                {"label": "先看蓝图", "description": "暂停，先查看建模蓝图详情"}],
}
BUSINESS_QUESTION = {"question": "典型接入方案按一个对象建，还是每种方案各建一个", "header": "置信度 0.9",
                     "options": [{"label": "逐一建模（推荐）"}, {"label": "合成一个对象"}]}


def test_flow_direction_questions_are_recognised():
    from app.services.autobuild import decide

    assert decide.flow_direction(FLOW_QUESTION) is True
    assert decide.flow_direction({"question": "下一步先做哪一段？", "options": [{"label": "门店经营"}]}) is True
    assert decide.flow_direction(BUSINESS_QUESTION) is False
    assert decide.flow_direction({"question": "规则编号沿不沿用资料里的", "options": [{"label": "沿用"}]}) is False


def _research_flow_question(ab_env, mode):
    from app.services.autobuild import prerun, store as runs
    from tests.autobuild_helpers import AutoEngine, ask, history, wait_until

    client, workspaces, project_id, _root = ab_env
    workspaces._update_meta(project_id, build_mode=mode)
    answers: list = []
    AutoEngine.hooks["/ontology-research"] = [ask([BUSINESS_QUESTION, FLOW_QUESTION], into=answers)]
    cid = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    sent = client.post(f"/api/v1/projects/{project_id}/conversations/{cid}/messages",
                       json={"text": "/ontology-research 仅基于本地文档"})
    assert sent.status_code == 202, sent.text
    wait_until(lambda: answers, timeout=20, message="流程走向的问题在等人回答")
    given = answers[0]["updated_input"]["answers"]
    assert given[FLOW_QUESTION["question"]] == prerun.STOP_ANSWER
    events = history(client, project_id, cid)
    titles = [item["data"].get("title") for item in events if item["type"] == "question.asked"]
    assert FLOW_QUESTION["question"] not in titles  # 不出卡，也不出「已自动决定」
    assert runs.runs(project_id) == []
    if mode == "L4":
        assert given[BUSINESS_QUESTION["question"]] == "逐一建模（推荐）"  # 业务歧义照常代答
    return given


def test_research_flow_question_in_autobuild_project(ab):  # noqa: F811
    _research_flow_question(ab, "L4")


def test_research_flow_question_in_companion_project(ab):  # noqa: F811
    from app.services.autobuild import prerun
    from tests.autobuild_helpers import AutoEngine, ask, wait_until

    client, workspaces, project_id, _root = ab
    workspaces._update_meta(project_id, build_mode="L2")

    answers: list = []
    AutoEngine.hooks["/ontology-research"] = [ask([FLOW_QUESTION], into=answers)]
    cid = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    sent = client.post(f"/api/v1/projects/{project_id}/conversations/{cid}/messages",
                       json={"text": "/ontology-research 仅基于本地文档"})
    assert sent.status_code == 202, sent.text
    wait_until(lambda: answers, timeout=20, message="陪伴建模里流程走向的问题出了卡在等人")
    assert answers[0]["updated_input"]["answers"][FLOW_QUESTION["question"]] == prerun.STOP_ANSWER

