"""去掉「确认生效」、检查只提示、资料分析停在蓝图确认（F3）。

测试部署上的现场（陪伴与自主都一样）：资料分析那一轮的报告、蓝图、6 个对象都在变更集 cs-1 里，「确认生效」被合入检查拦住
（6 条链接指向还没建的操作、别的段的对象），「确认蓝图」又要先合入这个变更集，用户只剩「撤回这一轮」。用户定：

- 每一轮结束（成功、出错、被打断）自动合入成一个版本；对话里一轮一张卡，只说改了什么，按钮只留「撤回这一轮」；
- 「撤回这一轮」= 撤掉这一轮带来的修改、回到上一个版本，之后别处的修改保留，冲突时说明原因不硬撤；
- 检查只提示不拦：指向蓝图里已规划、还没建的内容的链接记「待建」，不算问题（F8 不分段后不再有「待建」）；发布门禁不变；
- 待处理没有「修改待确认」、建议条没有「确认生效」，确认蓝图不再依赖合入；升级后遗留的待确认变更集自动合入。

资料分析停在蓝图确认见 test_f3_research_stop.py。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.kernel.changesets import service, store, worktree
from app.services import agent_sessions
from app.services.agent_sessions import SessionManager
from app.routers import sessions as session_routes
from tests.test_changesets import _pending, agent_write, api, main_write, obj, project  # noqa: F401
from tests.test_session_host import FakeEngine, result, settle

OWNER = "employee_0001"


def _session_manager(monkeypatch):
    from app import wiring

    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    monkeypatch.setattr(service, "_activity_provider", wiring._session_activity)
    return manager


def _turn(manager, project_id, root, text="补一个对象"):
    async def run():
        await manager.send(project_id, root, text, username=OWNER)
        await settle(manager, project_id)
    asyncio.run(run())


def _round_cards(manager, project_id):
    history = manager.get_history(project_id, "default")
    cards = {}
    for event in history:
        if event.get("type") in ("card", "card_updated") and event["payload"].get("card_type") == "round_changes":
            cards[event["payload"]["card_id"]] = event["payload"]
    return cards, history


def _write(rel, text, message):
    def action(ctx):
        assert worktree.is_copy(Path(ctx.cwd))
        agent_write(Path(ctx.cwd), rel, text, commit=message)
    return action


# ---- 每轮自动合入成版本，一轮一张卡 ----


def test_each_turn_becomes_a_version_with_one_card(api, monkeypatch):
    client, workspaces, project_id, root = api
    manager = _session_manager(monkeypatch)
    FakeEngine.scripts, FakeEngine.sent = [
        [_write("ontology/object/门店.md", obj("门店", "合成门店"), "agent: 门店"), result()],
        [_write("ontology/object/客户.md", obj("客户", "合成客户"), "agent: 客户"), result()],
    ], []
    seen = {}

    async def run():
        await manager.send(project_id, root, "补一个对象", username=OWNER)
        await settle(manager, project_id, until=lambda s: s.state == "idle"
                     and (store.get(root, "cs-1") or {}).get("cards"), rounds=3000)
        seen["first"] = _round_cards(manager, project_id)[0]
        seen["scope"] = client.get(f"/api/v1/projects/{project_id}/scope").json()["state"]
        seen["open"] = client.get(f"/api/v1/projects/{project_id}/conversations/default").json()["open_changeset"]
        await manager.send(project_id, root, "再补一个", username=OWNER)
        await settle(manager, project_id, until=lambda s: s.state == "idle" and len(FakeEngine.sent) == 2
                     and (store.get(root, "cs-2") or {}).get("cards"), rounds=3000)
        await manager.shutdown()

    asyncio.run(run())
    first = store.get(root, "cs-1")
    assert first["state"] == "merged" and first["merged"]["version_label"]
    assert len(seen["first"]) == 1
    card = next(iter(seen["first"].values()))
    assert card["state"] == "merged" and card["merged_version_label"] == first["merged"]["version_label"]
    assert card["status"]["label"] == first["merged"]["version_label"]
    assert "next_version_label" not in card and "remaining_failures" not in card
    assert seen["scope"] == "none" and seen["open"] is None

    assert (root / "ontology/object/门店.md").is_file() and (root / "ontology/object/客户.md").is_file()
    assert store.get(root, "cs-2")["state"] == "merged"
    cards, history = _round_cards(manager, project_id)
    # 每一轮自己一张卡，第一张不被第二轮改写成一样的
    assert sorted(card["changeset_id"] for card in cards.values()) == ["cs-1", "cs-2"]
    texts = []
    for card in cards.values():
        if card["changeset_id"] == "cs-1":
            texts += [item["text"] for group in card["groups"] for item in group["items"]]
    assert texts == ["对象 [[门店]]"]
    assert not [event for event in history if event.get("type") == "card_updated"]
    # 建议条、待处理里都没有确认生效
    bars = [event["payload"] for event in history if event.get("type") == "suggestions"]
    assert all(bar["mode"] != "pending_confirm" for bar in bars)
    actions = [item.get("action", {}).get("type") for bar in bars
               for item in [bar.get("primary") or {}, *(bar.get("secondary") or [])]]
    assert "merge_changeset" not in actions
    kinds = [item["kind"] for item in client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]]
    assert "pending_changeset" not in kinds


def test_a_failed_turn_is_merged_too(api, monkeypatch):
    _client, workspaces, project_id, root = api
    manager = _session_manager(monkeypatch)
    FakeEngine.scripts, FakeEngine.sent = [
        [_write("ontology/object/门店.md", obj("门店", "合成门店"), "agent: 门店"),
         {"type": "_engine_end", "reason": "error", "error": "模型服务断开"}],
    ], []

    async def run():
        await manager.send(project_id, root, "补一个门店", username=OWNER)
        for _ in range(3000):
            await asyncio.sleep(0.01)
            if (store.get(root, "cs-1") or {}).get("cards") and _round_cards(manager, project_id)[0]:
                break
    asyncio.run(run())
    assert (store.get(root, "cs-1") or {}).get("state") == "merged"
    assert (root / "ontology/object/门店.md").is_file()
    cards, _history = _round_cards(manager, project_id)
    assert [card["state"] for card in cards.values()] == ["merged"]
    asyncio.run(manager.shutdown())


def test_leftover_changes_are_merged_before_the_next_turn(project):
    """引擎被硬杀、会话被回收时这一轮没走到收尾：下一轮开始前先把副本里的合入，不丢。"""
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    service.observe(workspaces, project_id, running=True, conversation_id="default")
    assert store.current(root)["state"] == "open"
    service.copy_for_turn(workspaces, project_id, root)
    assert store.get(root, "cs-1")["state"] == "merged" and (root / "ontology/object/门店.md").is_file()


def test_turn_start_entries_do_not_merge_a_running_turn(project, monkeypatch):
    """Studio 转复核、执行清单也会走到 copy_for_turn：助手还在跑时不能把跑到一半的修改合进去。"""
    workspaces, project_id, root = project
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    service.observe(workspaces, project_id, running=True, conversation_id="default")
    monkeypatch.setattr(service, "_activity_provider", lambda pid: {"running": True, "conversation_id": "default"})
    service.copy_for_turn(workspaces, project_id, root)
    assert store.current(root)["state"] == "open" and not (root / "ontology/object/门店.md").exists()


def test_startup_puts_the_card_of_a_leftover_round_back_into_its_conversation(project, monkeypatch):
    workspaces, project_id, root = project
    from app import wiring
    from app.services import agent_sessions as sessions_module

    posted = []
    monkeypatch.setattr(sessions_module.manager, "append_record",
                        lambda pid, cid, item: posted.append((pid, cid, item)))
    monkeypatch.setattr(service, "_announcer", wiring._announce_round)
    _pending(workspaces, project_id, root)
    service.startup_repair(workspaces)
    assert [(cid, item["type"], item["payload"]["state"]) for _pid, cid, item in posted] == [
        ("default", "card", "merged")]


# ---- 撤回这一轮 ----


def test_revert_a_turn_keeps_later_unrelated_saves(api):
    client, workspaces, project_id, root = api
    _copy, record = _pending(workspaces, project_id, root)
    service.settle(workspaces, project_id, actor=OWNER)
    assert (root / "ontology/object/门店.md").is_file()
    main_write(root, "ontology/object/订单.md", obj("订单", "之后在 Studio 改的"))
    reverted = client.post(f"/api/v1/projects/{project_id}/changesets/{record['id']}/discard", json={})
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["state"] == "reverted" and body["version_label"]
    assert not (root / "ontology/object/门店.md").exists()
    assert "之后在 Studio 改的" in (root / "ontology/object/订单.md").read_text(encoding="utf-8")
    assert store.get(root, record["id"])["reverted"]["version_label"] == body["version_label"]
    card = service.round_card(workspaces, project_id, store.get(root, record["id"]), "rc-x")
    assert card["state"] == "reverted" and card["status"]["label"] == "已撤回"


def test_revert_refuses_when_later_changes_touched_the_same_entry(api):
    client, workspaces, project_id, root = api
    _copy, record = _pending(workspaces, project_id, root)
    service.settle(workspaces, project_id, actor=OWNER)
    main_write(root, "ontology/object/门店.md", obj("门店", "之后又改了门店"))
    refused = client.post(f"/api/v1/projects/{project_id}/changesets/{record['id']}/discard", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "merge_conflict"
    assert "门店" in refused.json()["message"]
    assert "之后又改了门店" in (root / "ontology/object/门店.md").read_text(encoding="utf-8")


# ---- 检查只提示不拦截，待建 ----


BLUEPRINT_DRAFT = {
    "goal_md": "合成目标", "out_of_scope": [],
    "slices": [
        {"name": "门店经营", "objects": ["门店"], "operations": ["补货下单"], "rules": []},
        {"name": "供应协同", "depends_on": ["门店经营"], "objects": ["供应商"], "operations": [], "rules": []},
    ],
    "terms": {"aligned": [], "ambiguous": []}, "questions": [],
}


def _blueprint_text(root: Path) -> str:
    from app.services.flow import blueprint

    module = blueprint.script(root)
    return module.dumps(module.from_draft(BLUEPRINT_DRAFT))


def test_new_check_errors_do_not_block_and_the_card_says_what_to_look_at(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root, rel="ontology/object/坏对象.md",
                             text="# 坏对象\n\n## 对象描述\n\n缺了必需章节，提到 [[不存在的东西]]\n")
    out = service.settle(workspaces, project_id, actor=OWNER)
    assert out["state"] == "merged" and (root / "ontology/object/坏对象.md").is_file()
    checks = store.get(root, record["id"])["checks"]
    assert checks["errors"] >= 1 and checks["pending"] == 0
    card = service.round_card(workspaces, project_id, store.get(root, record["id"]), "rc-x")
    texts = [hint["text"] for hint in card["hints"]]
    assert "1 处链接找不到对象" in texts
    assert not any("新错误" in text or "没通过" in text for text in [card["title"], *texts])


def test_links_to_planned_content_are_problems_without_slicing(project):
    """F8 不分段：蓝图里规划了、还没建的内容不再算「待建」（F8 之前写下的两段蓝图也一样），链接找不到照常算问题。"""
    workspaces, project_id, root = project
    from app.services.flow import blueprint

    module = blueprint.script(root)
    doc = module.from_draft(BLUEPRINT_DRAFT)
    base = doc["slices"][0]
    doc["slices"] = [{**base, "id": f"s{n}", "name": item["name"], "objects": item["objects"],
                      "operations": item["operations"], "rules": [], "depends_on": []}
                     for n, item in enumerate(BLUEPRINT_DRAFT["slices"], start=1)]
    doc["next_ids"]["slice"] = 3
    main_write(root, "research/blueprint.json", module.dumps(doc), "web: 蓝图")
    store_text = obj("门店", "合成门店，向 [[供应商]] 进货，按 [[补货下单]] 补货，另见 [[不存在的东西]]")
    _copy, record = _pending(workspaces, project_id, root, text=store_text)
    service.settle(workspaces, project_id, actor=OWNER)
    checks = store.get(root, record["id"])["checks"]
    assert checks["pending"] == 0 and checks["pending_links"] == []
    card = service.round_card(workspaces, project_id, store.get(root, record["id"]), "rc-x")
    assert {hint["text"]: hint["tone"] for hint in card["hints"]} == {"3 处链接找不到对象": "warn"}


def test_pending_classification_without_a_blueprint_counts_as_problems(project):
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root, text=obj("门店", "向 [[供应商]] 进货"))
    service.settle(workspaces, project_id, actor=OWNER)
    checks = store.get(root, record["id"])["checks"]
    assert checks["pending"] == 0 and checks["pending_links"] == []
    card = service.round_card(workspaces, project_id, store.get(root, record["id"]), "rc-x")
    assert [hint["text"] for hint in card["hints"]] == ["1 处链接找不到对象"]


# ---- 确认蓝图不依赖合入；升级迁移 ----


def test_blueprint_confirm_with_a_leftover_round_is_not_blocked_by_checks(api, monkeypatch):
    """测试部署上的现场：蓝图和几个链接失效的对象在没确认的变更集里，「确认蓝图」原来要先合入、被检查拦住 422。"""
    client, workspaces, project_id, root = api
    from app.routers import sessions as routes

    async def dispatch(*args, **kwargs):
        from fastapi.responses import JSONResponse

        return JSONResponse({"task": {"id": "task-x"}})

    monkeypatch.setattr(routes, "dispatch_command", dispatch)
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "research/blueprint.json", _blueprint_text(root))
    agent_write(copy, "ontology/object/门店.md", obj("门店", "按 [[补货下单]] 补货，另见 [[不存在的东西]]"),
                commit="research: 资料分析")
    service.observe(workspaces, project_id, running=False, conversation_id="default")
    assert store.current(root)["state"] == "pending_confirm"  # 升级前留下的
    revision = client.get(f"/api/v1/projects/{project_id}/blueprint").json()["revision"]
    confirmed = client.post(f"/api/v1/projects/{project_id}/blueprint/confirm",
                            json={"base_revision": revision, "next": "L2"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["blueprint"]["state"] == "confirmed"
    assert (root / "ontology/object/门店.md").is_file() and store.get(root, "cs-1")["state"] == "merged"


def test_blueprint_in_a_conflicting_round_points_to_the_inbox(api):
    client, workspaces, project_id, root = api
    copy = service.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "research/blueprint.json", _blueprint_text(root))
    agent_write(copy, "ontology/object/订单.md", obj("订单", "助手改的订单"), commit="research: 资料分析")
    service.observe(workspaces, project_id, running=False, conversation_id="default")
    main_write(root, "ontology/object/订单.md", obj("订单", "你在 Studio 改的订单"))
    assert service.settle(workspaces, project_id, actor=OWNER)["state"] == "conflict"
    revision = client.get(f"/api/v1/projects/{project_id}/blueprint").json()["revision"]
    refused = client.post(f"/api/v1/projects/{project_id}/blueprint/confirm",
                          json={"base_revision": revision, "next": "L2"})
    assert refused.status_code == 409 and refused.json()["code"] == "merge_conflict"
    kinds = [item["kind"] for item in client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]]
    assert "changeset_conflict" in kinds and "pending_changeset" not in kinds


def test_startup_merges_leftover_pending_rounds(project):
    """升级迁移：部署前留下的待确认变更集，没有冲突的启动时自动合入，有冲突的进待处理。"""
    workspaces, project_id, root = project
    _copy, record = _pending(workspaces, project_id, root, text=obj("门店", "另见 [[不存在的东西]]"))
    report = service.startup_repair(workspaces)
    assert store.get(root, record["id"])["state"] == "merged"
    assert (root / "ontology/object/门店.md").is_file()
    assert report is not None and "1" in report["detail"]

    other = workspaces.create_project("迁移冲突", owner=OWNER)["id"]
    other_root = workspaces.project_root(other)
    main_write(other_root, "ontology/object/订单.md", obj("订单", "原来的订单"))
    copy = service.copy_for_turn(workspaces, other, other_root)
    agent_write(copy, "ontology/object/订单.md", obj("订单", "助手改的"), commit="agent: 订单")
    service.observe(workspaces, other, running=False, conversation_id="default")
    main_write(other_root, "ontology/object/订单.md", obj("订单", "你改的"))
    service.startup_repair(workspaces)
    assert store.current(other_root)["state"] == "conflict"


def test_suggestions_never_offer_confirm(project):
    from app.services.packs import contracts

    _workspaces, _project_id, root = project
    flow_state = contracts.module_for(root, "flow_state")
    out = flow_state.suggest({"nodes": []}, {}, {"changeset": {"id": "cs-1", "state": "pending_confirm"}})
    assert out["mode"] != "pending_confirm"
    items = [out.get("primary") or {}, *(out.get("secondary") or [])]
    changeset_actions = ("merge_changeset", "discard_changeset")
    assert not [item for item in items if (item.get("action") or {}).get("type") in changeset_actions]


# ---- Studio、MCP 只拦格式错误 ----


def test_studio_link_problems_are_hints_not_review(project):
    from app.services import studio

    _workspaces, _project_id, root = project
    checks = studio.deterministic_check(root, "ontology/object/门店.md", obj("门店", "向 [[不存在的东西]] 进货"))
    assert checks["errors"] == [] and checks["review"] == []
    assert any("不存在的东西" in text for text in checks["hints"])
    broken = studio.deterministic_check(root, "ontology/object/门店.md", "# 门店\n\n## 对象描述\n\n缺章节\n")
    assert broken["errors"]  # 解析不了的结构照旧拦


def test_mcp_reference_problems_are_warnings(project):
    from app.services.onto_mcp import workspace as mcp_workspace

    records = {"object:订单": {"kind": "object", "key": "订单", "path": "ontology/object/订单.md",
                               "text": obj("订单", "合成订单")}}
    after = {**records, "object:门店": {"kind": "object", "key": "门店", "path": "ontology/object/门店.md",
                                         "text": obj("门店", "向 [[不存在的东西]] 进货")}}
    issues = mcp_workspace.integrity(after)
    assert issues and all(item["level"] == "warning" for item in issues.values())
