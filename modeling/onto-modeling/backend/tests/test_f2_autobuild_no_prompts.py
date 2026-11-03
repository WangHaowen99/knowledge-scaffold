"""自主构建期间对话里不出要人操作的卡片（F2 第 7 条；V 报告需要改设计第 10 条）。

测试部署现场（构建方式是自主构建的项目、还没开始运行）：用户点「资料分析」，助手问「还有没有别的资料」「编号沿用吗」，
两次都出决策卡等人选——策略只在运行中代答，资料分析在运行之前。现在：

- 自主构建的项目在开始前的资料分析里提问，也由策略代答；对话里只留一行「已自动决定」，不出卡片、不进待处理、不发
  「需要你回答」通知；
- 命令模板自带的产物确认（「确认无误请回复确认」）按确认往下做，不进台账（运行中原来没写置信度就记成假设进复核）；
- 运行中策略放行工具确认时也不再发「需要你确认」通知。

假引擎见 tests/autobuild_helpers.py；测试数据全部为合成内容。
"""

from __future__ import annotations

from app.services import notifications
from app.services.autobuild import prerun, store
from tests.autobuild_helpers import (AutoEngine, ab, ask, history, start, wait_state,  # noqa: F401
                                     wait_until)

SLICES_ONLY = {"scope": {"until_node": "core.model"}, "budget": {"max_tokens": 500_000}}
OWNER = "employee_0001"
CONFIRM = {"question": "对象产物已生成，共 3 个，验收结果如上。确认无误吗？",
           "options": [{"label": "确认"}, {"label": "需要修改"}]}


def _needs_input(project_id: str) -> list[dict]:
    return [item for item in notifications.list_notifications(OWNER)
            if item.get("project_id") == project_id and item.get("kind") == "needs_input"]


def test_template_confirmation_in_a_run_is_confirmed_without_a_ledger_entry(ab):
    client, _workspaces, project_id, _root = ab
    answers: list = []
    AutoEngine.hooks["/ontology-extract-object:s1"] = [
        ask([CONFIRM, {"question": "门店编号沿用资料里的四位码吗", "header": "置信度 0.9",
                       "options": [{"label": "沿用（推荐）"}, {"label": "重新编号"}]}], into=answers),
        ask([], into=answers, tool="Write")]
    run = start(client, project_id, **SLICES_ONLY)
    wait_state(client, project_id, run["run_id"], "completed")
    assert answers[0]["updated_input"]["answers"] == {CONFIRM["question"]: "确认",
                                                      "门店编号沿用资料里的四位码吗": "沿用（推荐）"}
    ledger = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger").json()
    assert [item["question"] for item in ledger["items"]] == ["门店编号沿用资料里的四位码吗"]
    # 运行对话里没有卡片要人点；策略放行的工具确认不发「需要你确认」
    events = history(client, project_id, run["conversation_id"])
    assert not [item for item in events if item["type"] == "question.asked"]
    assert answers[1] == {"behavior": "allow", "updated_input": None}
    assert _needs_input(project_id) == []


def test_research_before_the_run_in_an_autobuild_project_is_answered_by_policy(ab):
    client, workspaces, project_id, root = ab
    workspaces._update_meta(project_id, build_mode="L4")
    answers: list = []
    AutoEngine.hooks["/ontology-research"] = [ask([
        {"question": "除了已上传的 2 份资料，还有别的本地文档要一并分析吗", "header": "置信度 0.9",
         "options": [{"label": "没有，只用现有资料（推荐）"}, {"label": "有，我来提供"}]},
        {"question": "编号沿用本轮新建的 ITEM-01～ITEM-27 吗",
         "options": [{"label": "确认沿用（推荐）"}, {"label": "需要调整（保守）"}]},
        {"question": "资料里没写的口径怎么处理"},
        CONFIRM], into=answers)]
    cid = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    sent = client.post(f"/api/v1/projects/{project_id}/conversations/{cid}/messages",
                       json={"text": "/ontology-research 仅基于本地文档"})
    assert sent.status_code == 202, sent.text
    wait_until(lambda: answers)
    given = answers[0]["updated_input"]["answers"]
    # 达到阈值取推荐项；没写置信度（按 0.5）取保守项；没有选项的按资料判断；产物确认直接确认
    assert given["除了已上传的 2 份资料，还有别的本地文档要一并分析吗"] == "没有，只用现有资料（推荐）"
    assert given["编号沿用本轮新建的 ITEM-01～ITEM-27 吗"] == "需要调整（保守）"
    assert given["资料里没写的口径怎么处理"] == prerun.OPEN_ANSWER
    assert given[CONFIRM["question"]] == "确认"
    # 注入里写明这一段由策略代答
    assert any(hint.startswith(prerun.INSTRUCTIONS[:12]) for hint in AutoEngine.hints)
    # 对话里每个决定一行「已自动决定」（产物确认不出），没有等人回答的卡片、待处理、通知
    events = wait_until(lambda: (lambda items: items if len([i for i in items if i["type"] == "question.asked"]) == 3
                                 else None)(history(client, project_id, cid)))
    asked = [item["data"] for item in events if item["type"] == "question.asked"]
    assert all(item["state"] == "answered" for item in asked)
    rows = [item["data"] for item in events if item["type"] == "question.answered"]
    assert {row["by"] for row in rows} == {"autobuild_policy"}
    assert rows[0]["summary"] == "已自动决定「除了已上传的 2 份资料，还有别的本地文档要一并分析吗」：没有，只用现有资料"
    assert rows[1]["summary"].endswith("：需要调整（假设）")
    assert rows[2]["summary"].endswith("：按资料判断（假设）")
    state = client.get(f"/api/v1/projects/{project_id}/conversations/{cid}").json()
    assert state["open_questions"] == [] and state["pending_permission"] is None
    inbox = client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "decision"}).json()
    assert inbox["items"] == []
    assert _needs_input(project_id) == []
    assert store.runs(project_id) == []  # 没有因此开出运行


def test_prerun_applies_only_to_research_in_an_autobuild_project_without_a_run(ab):
    client, workspaces, project_id, root = ab
    cid = client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"][0]["id"]
    research = {"id": "ex-1", "conversation_id": cid, "node": {"id": "core.research"}, "state": "running"}
    model = {**research, "node": {"id": "core.model"}}
    # 陪伴建模的项目：照常等人
    assert prerun.applies(root, research) is False
    workspaces._update_meta(project_id, build_mode="L4")
    assert prerun.applies(root, research) is True
    # 资料分析之后的步骤（不在运行里）是用户自己点的，照常等人
    assert prerun.applies(root, model) is False
    assert prerun.applies(root, None) is False
    # 已经在运行：归运行的策略管
    store.save({"project_id": project_id, "run_id": "run-x", "state": "running"})
    assert prerun.applies(root, research) is False
