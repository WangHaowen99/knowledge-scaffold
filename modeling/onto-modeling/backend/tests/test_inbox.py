"""待处理汇总（详设 4.7；契约 3.13 IN-01～IN-04、EP-02）与变更点清单（契约 3.14 INC-01～INC-05）。

- 各来源：决策、续跑、修改待确认、合入冲突、过期、需复核、报告待处理、变更清单；
- 计数按筛选与按包（左栏插件页角标，B4c 的登记点）；忽略后不计数；处理一项转给等价接口；
- 变更清单：一轮结束时收进来、记下所在对话，执行时发 `/ontology-update change_list_id=…`。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import PendingPermission, Session, SessionManager
from app.services.flow import change_lists, inbox
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.session_host import exec_records
from app.services.workspace import WorkspaceManager
from tests.test_changesets import agent_write, main_write, obj
from tests.test_event_streams import buffered
from tests.test_pack_host_wiring import wait
from tests.test_session_host import FakeEngine, result


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        project_id = workspaces.create_project("待处理", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        yield client, workspaces, manager, project_id, root
    streams.reset_all()


def idle(manager, project_id: str):
    """会话在 TestClient 的事件循环里跑：轮询等这一轮收尾（不能在别的事件循环里 await）。"""
    def done():
        session = manager.get(project_id)
        return (session is not None and session.engine is not None and session.state == "idle"
                and not session.sending and not session.steers and not session.turns_in_flight)
    return wait(done)


def kinds(client, project_id: str, **params) -> dict:
    body = client.get(f"/api/v1/projects/{project_id}/inbox", params=params).json()
    return {item["id"]: item for item in body["items"]}


def pending(workspaces, project_id, root, rel="ontology/object/门店.md", text=None):
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, rel, text or obj("门店", "合成门店"), commit="agent: 门店")
    return changesets.observe(workspaces, project_id, running=False, conversation_id="default")


def conflicted(workspaces, project_id, root, rel="ontology/object/客户.md", name="客户"):
    """这一轮和你在 Studio 的修改改到同一条：合入时冲突，进待处理（F3 起唯一会出现的变更集事项）。"""
    main_write(root, rel, obj(name, "原来的"))
    pending(workspaces, project_id, root, rel=rel, text=obj(name, "助手改的"))
    main_write(root, rel, obj(name, "你改的"))
    assert changesets.settle(workspaces, project_id, actor="employee_0001")["state"] == "conflict"


def test_sources_counts_and_detail_shapes(env):
    client, workspaces, _manager, project_id, root = env
    # 过期的场景模拟节点（场景模拟包的节点，计进它的角标）、需复核的对象节点、报告、续跑；外部反馈批次留在工作区，不再列出
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    main_write(root, "ontology/testcase/补货.md", "# 测试场景：补货\n\n## 场景 1：合成\n")
    assert inbox.collect(workspaces, project_id) is not None  # 记基线
    main_write(root, "ontology/object/仓库.md", obj("仓库", "上游变了"))
    node_state.mark_needs_review(root, ["core.object"], "import", note="导入的知识包带进来的产物")
    main_write(root, "reports/20261102-rule.md", "# 校验\n\n- [ ] 门店缺少数据来源\n- [x] 已处理\n")
    (root / "feedback").mkdir(exist_ok=True)
    main_write(root, "feedback/20261102-fb1.json", json.dumps({"feedback_id": "fb1", "source": "评测服务",
                                                               "items": [{"verdict": "fail"}]}))
    flow = flow_model.load_for(root)
    record = exec_records.start(project_id, "default", root=root, flow=flow, command="/ontology-research",
                                command_name="research")
    exec_records.interrupt(project_id, record["id"], "restart", root=root, flow=flow)
    conflicted(workspaces, project_id, root)

    body = client.get(f"/api/v1/projects/{project_id}/inbox").json()
    found = {item["id"]: item for item in body["items"]}
    assert "stale:trace.trace" in found and found["stale:trace.trace"]["pack"] == "trace"
    assert found["stale:core.operation"]["pack"] is None and "stale:core.object" not in found
    assert found["review:core.object"]["source"] == "import"
    assert found["report:20261102-rule"]["title"].endswith("1 条待处理")
    assert not [key for key in found if key.startswith("feedback:")]
    assert found[f"resumable:{record['id']}"]["actions"] == ["resume", "abandon", "dismiss"]
    assert found["changeset_conflict:cs-1"]["actions"] == ["resolve", "discard", "dismiss"]
    assert not [key for key in found if key.startswith("pending_changeset:")]
    # 说明槽位 meta 不再下发（UI 文案口径 ⑤）
    item_keys = {"id", "kind", "kind_label", "title", "slice", "run", "source", "confidence",
                 "high_confidence", "pack", "impact", "created_at", "actions", "batchable"}
    assert all(set(item) == item_keys for item in body["items"])
    counts = body["counts"]
    assert counts["total"] == len(body["items"]) and counts["by_filter"]["stale"] >= 1
    assert counts["by_filter"]["resumable"] == 1 and "feedback" not in counts["by_filter"]
    assert counts["by_pack"]["trace"] >= 1
    assert "evidence_conflict" not in counts["by_filter"]  # 证据采集已删除，没有这一类
    assert set(body["today"]) == {"handled", "total", "eta_minutes", "remaining_by_filter"}
    assert body["autobuild_summary"] is None
    assert set(kinds(client, project_id, filter="stale")) == {key for key in found if key.startswith("stale:")}
    assert client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "bogus"}).status_code == 400
    assert client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "evidence_conflict"}).status_code == 400
    # 详情按 kind
    detail = client.get(f"/api/v1/projects/{project_id}/inbox/changeset_conflict:cs-1").json()["detail"]
    assert detail["changeset"]["id"] == "cs-1" and detail["entities"][0]["id"] == "object:客户"
    stale = client.get(f"/api/v1/projects/{project_id}/inbox/stale:trace.trace").json()["detail"]
    assert set(stale) == {"upstream_diff", "impact", "siblings"}
    assert client.get(f"/api/v1/projects/{project_id}/inbox/nope:1").json()["code"] == "not_found"
    # 左栏角标：场景模拟页（声明了 badge: inbox）
    nav = client.get(f"/api/v1/projects/{project_id}/extensions").json()["nav"]
    scenarios = next(item for item in nav if item.get("pack") == "trace")
    assert scenarios["badge"]["count"] == counts["by_pack"]["trace"]


def test_actions_go_to_the_equivalent_endpoints(env):
    client, workspaces, _manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/inbox"
    conflicted(workspaces, project_id, root)
    merged = client.post(f"{base}/changeset_conflict:cs-1/actions",
                         json={"action": "resolve", "choices": [{"target": "object:客户", "keep": "changeset"}]})
    assert merged.status_code == 200, merged.text
    assert merged.json()["resolved"] is True and merged.json()["result"]["state"] == "merged"
    assert "助手改的" in (root / "ontology/object/客户.md").read_text(encoding="utf-8")
    assert client.post(f"{base}/changeset_conflict:cs-1/actions",
                       json={"action": "resolve", "choices": []}).json()["code"] == "not_found"
    # 需复核：接受后去掉
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    node_state.mark_needs_review(root, ["core.object"], "upgrade")
    assert client.post(f"{base}/review:core.object/actions", json={"action": "discard"}).status_code == 400
    accepted = client.post(f"{base}/review:core.object/actions", json={"action": "accept"})
    assert accepted.json()["resolved"] is True and node_state.reviews(root) == []
    # 忽略：不再计数，当天处理数加一
    main_write(root, "reports/r.md", "- [ ] 一条\n")
    before = client.get(base).json()
    dismissed = client.post(f"{base}/report:r/actions", json={"action": "dismiss"})
    assert dismissed.json()["resolved"] is True
    after = client.get(base).json()
    assert after["counts"]["total"] == before["counts"]["total"] - 1
    assert after["today"]["handled"] >= 3
    # 每次处理后项目流推 inbox.updated
    updates = [event["data"] for event in buffered(project_id, "inbox.updated")]
    assert updates and set(updates[-1]) == {"counts", "added", "removed", "changed"}


def test_refresh_stale_and_resume_send_commands(env):
    client, workspaces, manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/inbox"
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    inbox.collect(workspaces, project_id)
    main_write(root, "ontology/object/仓库.md", obj("仓库", "上游变了"))
    batch = client.post(f"{base}/batch", json={"action": "refresh_stale"})
    assert batch.status_code == 200, batch.text
    assert batch.json()["accepted"] == 1 and batch.json()["command_task"]["title"].startswith("助手正在执行")
    idle(manager, project_id)
    assert client.post(f"{base}/batch", json={"action": "bogus"}).status_code == 400


def test_stale_handed_down_to_a_confirmed_check_is_not_listed(env, monkeypatch):
    """已交给已确认的校验的过期，待处理不列。

    校验已确认、它修对象让提取操作、指标过期：建议条按 ADR-0090 不推刷新，待处理也不列（实测：自主构建跑完
    报「全部完成」，待处理却多出「建模过期了」「指标过期了」）。校验没跑过、跳过时照列。"""
    client, workspaces, manager, project_id, root = env
    from app.services.flow import view

    flow = flow_model.load_for(root)
    real = view.node_states

    def fake(validate):
        def states(root_, flow_, hits):
            _states, context, broken = real(root_, flow_, hits)
            out = {node["id"]: {"state": "confirmed", "flags": {}, "display_state": "confirmed"}
                   for node in flow["nodes"]}
            for node_id in ("core.operation", "core.metric"):
                out[node_id] = {"state": "stale", "flags": {}, "display_state": "stale"}
            out["core.validate"] = {"state": validate, "flags": {}, "display_state": validate}
            return out, context, broken
        return states

    monkeypatch.setattr(view, "node_states", fake("confirmed"))
    assert not [key for key in kinds(client, project_id) if key.startswith("stale:")]
    # 校验还没跑：必经的提取操作要你刷新；可选的指标交给之后的校验（ADR-0090 补充）。校验跳过了：两个都要你刷新
    monkeypatch.setattr(view, "node_states", fake("ready"))
    assert [key for key in kinds(client, project_id) if key.startswith("stale:")] == ["stale:core.operation"]
    monkeypatch.setattr(view, "node_states", fake("skipped"))
    assert {"stale:core.operation", "stale:core.metric"} <= set(kinds(client, project_id))


def test_decision_items_come_from_the_live_session(env):
    client, workspaces, manager, project_id, root = env
    session = Session(id="s1", project_id=project_id, conversation_id="default")

    async def make():
        future = asyncio.get_running_loop().create_future()
        question = {"question_id": "q-1", "title": "用哪个口径", "options": [
            {"id": "0.0", "key": "A", "label": "按门店", "recommended": True, "custom": False},
            {"id": "0.1", "key": "B", "label": "按仓库", "recommended": False, "custom": False}],
            "affected": [{"kind": "object", "id": "object:门店", "key": "门店", "name": "门店"}]}
        session.pending_permissions["r1"] = PendingPermission("r1", "AskUserQuestion", {}, None, None,
                                                              future=future, questions=[question])
        manager._sessions[project_id] = session
        return future

    future = asyncio.run(make())
    items = kinds(client, project_id, filter="decision")
    assert list(items) == ["decision:q-1"] and "meta" not in items["decision:q-1"]
    assert items["decision:q-1"]["batchable"] is False
    detail = client.get(f"/api/v1/projects/{project_id}/inbox/decision:q-1").json()["detail"]
    assert detail["question_id"] == "q-1"
    # 决策卡改动预览里的条目经会话状态进写入范围（ADR-0047）
    from app import wiring

    assert wiring._session_activity(project_id)["affected"][0]["id"] == "object:门店"
    assert not future.done()
    manager._sessions.pop(project_id, None)


def test_change_list_is_captured_with_its_conversation_and_executed(env):
    client, workspaces, manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    plan = [{"file": "ontology/object/门店.md", "kind": "object", "operation": "modify",
             "change_summary": "新增字段「营业面积」", "reason": "修订通知要求",
             "cascading": [{"file": "ontology/data-mock/门店.md", "needs_change": True, "reason": "补一列"}]},
            {"file": "ontology/object/旧门店.md", "kind": "object", "operation": "delete",
             "change_summary": "删除废弃对象", "reason": "已合并", "cascading": []}]

    def analyse(ctx):
        state = Path(ctx.cwd) / ".ontology-state"
        state.mkdir(exist_ok=True)
        (state / "update-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    def execute(ctx):
        agent_write(Path(ctx.cwd), "ontology/object/门店.md", obj("门店", "合成门店（加营业面积）"), commit="agent: 改")

    FakeEngine.scripts = [[analyse, result()], [execute, result()]]
    client.post(f"/api/v1/projects/{project_id}/conversations/default/messages", json={"text": "/ontology-update 修订通知"})

    def round_card():
        return [event["payload"] for event in manager.get_history(project_id, "default")
                if event.get("type") in ("card", "card_updated") and event["payload"].get("card_type") == "round_changes"]

    wait(lambda: change_lists.all_lists(root))
    idle(manager, project_id)
    lists = change_lists.all_lists(root)
    assert len(lists) == 1
    listed = change_lists.view(lists[0])
    # 只做了影响分析的一轮没有改动、没有变更集：清单先不挂编号，等执行那一轮
    assert listed["conversation_id"] == "default" and listed["changeset_id"] is None
    assert [point["confidence"] for point in listed["points"]] == ["high", "low"]
    assert listed["points"][0]["impact"][0]["id"] == "object:门店"
    assert round_card() == []
    base = f"/api/v1/projects/{project_id}/change-lists/{listed['id']}"
    assert client.get(base).json()["id"] == listed["id"]
    assert kinds(client, project_id, filter="change_list")[f"change_list:{listed['id']}"]["kind"] == "change_list"
    # 低把握的不能批量接受
    batch = client.post(f"{base}/accept", json={"point_ids": ["p1", "p2"]})
    assert batch.status_code == 422 and batch.json()["code"] == "low_confidence_batch"
    client.post(f"{base}/accept", json={"high_confidence": True})
    saved = client.put(base, json={"base_revision": client.get(base).json()["revision"], "points": [
        {"id": "p1"}, {"id": "p2", "rewritten_text": "保留旧门店，只标停用"}]})
    assert saved.status_code == 200 and saved.json()["change_list"]["points"][1]["state"] == "rewritten"
    stale_revision = client.put(base, json={"base_revision": "old", "points": []})
    assert stale_revision.json()["code"] == "revision_conflict"
    executed = client.post(f"{base}/execute", json={"base_revision": client.get(base).json()["revision"]})
    assert executed.status_code == 202, executed.text
    assert "change_list_id=" in FakeEngine.sent[-1]
    exported = changesets.existing_copy(root) / ".onto" / "run" / "change-lists" / f"{listed['id']}.json"
    assert json.loads(exported.read_text(encoding="utf-8"))["id"] == listed["id"]
    idle(manager, project_id)
    # 执行那一轮结束就合入（F3）：清单挂上这一轮的变更集、记为已执行，卡片带上清单
    wait(round_card)
    assert change_lists.view(change_lists.get(root, listed["id"]))["state"] == "executed"
    assert round_card()[-1]["change_list_id"] == listed["id"] and round_card()[-1]["state"] == "merged"
    assert "营业面积" in (root / "ontology/object/门店.md").read_text(encoding="utf-8")
