"""B4b 独立审查修复（变更点清单、节点状态、待处理）：编号与审查清单一致，每条一个修复前会失败的用例。

复现思路取自审查的脚本（R 编号），改成断言修复后的行为。测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.kernel.changesets import service
from app.services.flow import change_lists, inbox
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.flow import view
from tests.test_changesets import agent_write, main_write, obj, project  # noqa: F401
from tests.test_flow_states import env  # noqa: F401
from tests.test_inbox import idle
from tests.test_pack_host_wiring import wait
from tests.test_session_host import FakeEngine, result


# ---- 8 只做影响分析的一轮收不到变更清单 ----


def test_08_plan_only_turn_captures_the_change_list(env):
    client, workspaces, manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    plan = [{"file": "ontology/object/门店.md", "kind": "object", "operation": "modify",
             "change_summary": "新增字段「营业面积」", "reason": "修订通知要求", "cascading": []}]

    def analyse(ctx):
        state = Path(ctx.cwd) / ".ontology-state"
        state.mkdir(exist_ok=True)
        (state / "update-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    FakeEngine.scripts = [[analyse, result()]]
    client.post(f"/api/v1/projects/{project_id}/conversations/default/messages", json={"text": "/ontology-update 修订通知"})
    lists = wait(lambda: change_lists.all_lists(root))
    assert lists[0]["conversation_id"] == "default" and lists[0]["changeset_id"] is None
    # 之后这条对话里开了变更集：清单补上编号
    def apply(ctx):
        agent_write(Path(ctx.cwd), "ontology/object/门店.md", obj("门店", "加了营业面积"), commit="agent: 改")

    idle(manager, project_id)
    FakeEngine.scripts = [[apply, result()]]
    client.post(f"/api/v1/projects/{project_id}/conversations/default/messages", json={"text": "确认"})
    wait(lambda: (change_lists.get(root, lists[0]["id"]) or {}).get("changeset_id") == "cs-1")


# ---- 12 变更清单永远到不了 executed ----


def test_12_change_list_is_executed_when_its_changeset_merges(env):
    client, workspaces, _manager, project_id, root = env
    copy = service.copy_for_turn(workspaces, project_id, root)
    plan = [{"file": "ontology/object/门店.md", "operation": "modify", "change_summary": "加字段", "reason": "修订"}]
    (copy / ".ontology-state").mkdir(exist_ok=True)
    (copy / ".ontology-state" / "update-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    listed = change_lists.capture(root, copy, conversation_id="default", changeset_id=None)
    change_lists.accept(root, listed["id"], point_ids=["p1"])
    change_lists.mark(root, listed["id"], state="confirmed")  # INC-04 发出命令之后
    agent_write(copy, "ontology/object/门店.md", obj("门店", "加了字段"))
    record = service.observe(workspaces, project_id, running=False, conversation_id="default")
    assert change_lists.get(root, listed["id"])["changeset_id"] == record["id"]
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert change_lists.get(root, listed["id"])["state"] == "executed"
    assert f"change_list:{listed['id']}" not in [item["id"] for item in inbox.collect(workspaces, project_id)]
    base = f"/api/v1/projects/{project_id}/change-lists/{listed['id']}"
    again = client.post(f"{base}/execute", json={"base_revision": client.get(base).json()["revision"]})
    assert again.status_code == 409 and again.json()["code"] == "changeset_closed"


# ---- 14 可选节点跳过后再补做完成 ----


def test_14_skipped_node_that_gets_done_is_confirmed(env):
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    node_state.skip(root, "core.metric", reason="先跳过")
    assert next(n for n in view.build(root)["nodes"] if n["id"] == "core.metric")["state"] == "skipped"
    main_write(root, "ontology/metric/合成指标.md", "# 合成指标\n\n## 指标描述\n\n合成\n")
    assert next(n for n in view.build(root)["nodes"] if n["id"] == "core.metric")["state"] == "confirmed"
    assert "core.metric" not in node_state.skipped(root)


# ---- 15 忽略记录永久生效 ----


def test_15_dismissing_an_item_does_not_hide_the_next_occurrence(env):
    _client, workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    view.build(root)
    main_write(root, "ontology/object/仓库.md", obj("仓库", "上游变了"))
    item = inbox.find(workspaces, project_id, "stale:core.model")
    inbox.dismiss(root, item["id"], by="employee_0001", fingerprint=item["_fp"])
    assert inbox.find(workspaces, project_id, "stale:core.model")["_dismissed"] is True
    from app.services.flow import slices

    slices.confirm(root, flow_model.load_for(root), [["all", "operation"]], doc=None)
    assert inbox.find(workspaces, project_id, "stale:core.model") is None
    main_write(root, "ontology/object/客户.md", obj("客户", "上游又变了"))
    items, _counts, _today = inbox.listing(workspaces, project_id)
    assert "stale:core.model" in [entry["id"] for entry in items]


# ---- 18 IN-04 刷新过期节点选成了最下游 ----


def test_18_refresh_stale_starts_the_most_upstream_node(env, monkeypatch):
    client, workspaces, _manager, project_id, root = env
    from fastapi.responses import JSONResponse

    from app.routers import flow as flow_routes

    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    main_write(root, "ontology/rule/合成规则.md", "# 合成规则\n\n## 规则描述\n\n合成\n")
    view.build(root)
    main_write(root, "ontology/object/仓库.md", obj("仓库", "上游变了"))
    started = []

    async def fake_start(project_id_, node_id, request):
        started.append(node_id)
        return JSONResponse(status_code=202, content={"task": None})

    monkeypatch.setattr(flow_routes, "start_node", fake_start)
    monkeypatch.setattr(inbox.time, "time", lambda: 1_800_000_000.0)
    response = client.post(f"/api/v1/projects/{project_id}/inbox/batch", json={"action": "refresh_stale"}).json()
    assert started == ["core.model"], response


# ---- 21 重做并确认生效后需复核标记不去掉 ----


def test_21_confirming_a_redone_node_clears_its_review_flag(env):
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    flow = flow_model.load_for(root)
    node_state.compute(root, flow, flow_model.flow_state(root).node_hits(flow, root))
    node_state.mark_needs_review(root, ["core.model"], "import", note="导入")
    main_write(root, "ontology/object/门店.md", obj("门店", "重做后的门店"))
    node_state.confirm(root, flow, ["core.model"])
    assert node_state.reviews(root) == []


def test_21_autobuild_merges_keep_their_review_flag(project):
    """自主构建自动合入的段：确认生效时不去掉，而是标「需复核（自主构建）」。"""
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    slot = service.open_changeset(workspaces, project_id, source="autobuild", build_mode="L4", run_id="run-1",
                                  slice_id="s-1")
    agent_write(copy, "ontology/object/门店.md", obj("门店", "段里写的门店"))
    service.observe(workspaces, project_id, running=False, exec_record={
        "id": "ex-1", "node": {"id": "core.object", "version": "core@0.2.0"}})
    service.merge(workspaces, project_id, slot["id"], actor="employee_0001", needs_review=True)
    reviews = node_state.reviews(root)
    assert reviews and reviews[0]["source"] == "autobuild" and reviews[0]["run_id"] == "run-1"


# ---- 22 三处小问题 ----


def test_22_review_actions_and_change_list_metamodel(env, monkeypatch):
    client, workspaces, _manager, project_id, root = env
    # 不是自主构建的需复核项没有「撤回这一段」
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    node_state.mark_needs_review(root, ["core.model"], "import")
    review = inbox.find(workspaces, project_id, "review:core.model")
    assert "revert_slice" not in review["actions"]
    # 变更清单的影响条目按项目锁定版本的元模型认目录
    from app.kernel import metamodel

    calls = []
    real = metamodel.for_project
    monkeypatch.setattr(metamodel, "for_project", lambda path: calls.append(Path(path)) or real(path))
    copy = service.copy_for_turn(workspaces, project_id, root)
    (copy / ".ontology-state").mkdir(exist_ok=True)
    (copy / ".ontology-state" / "update-plan.json").write_text(json.dumps(
        [{"file": "ontology/object/门店.md", "operation": "modify", "change_summary": "x", "reason": "y"}]),
        encoding="utf-8")
    change_lists.capture(root, copy, conversation_id="default", changeset_id=None)
    assert root in calls
