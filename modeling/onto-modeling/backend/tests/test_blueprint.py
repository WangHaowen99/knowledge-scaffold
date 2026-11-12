"""建模蓝图（详设第 9 章建议 2、6.8；契约 BP-01～BP-03、4.7 blueprint 卡、7.8 Blueprint；ADR-0050）。

F8 起不分段：草稿里的段合成一段「全部」，`mode()` 恒为 full（老的多段蓝图照样读，按全量走）；蓝图只记统一叫法与
待定问题，不用确认（卡片上没有确认按钮；BP-03 仍可调）。
- 蓝图文件由 core 的 blueprint.py 写入与校验：草稿合成一段、依赖照样校验、统一叫法进段说明；
- 调整（BP-02）：统一叫法、回答问题、改名；草案 / 调整两种保存、revision 冲突；
- 蓝图在资料分析那一轮的修改里（助手在跑）时：读、调整都对副本；F3 起那一轮结束就合入，确认蓝图只标已确认；
- 确认（BP-03）：L2 往对话发建模第一步（不带分段），L4 在自主构建（B5b）登记前 409、蓝图不动；
- 一轮写出蓝图时对话里出蓝图卡（没有确认按钮），调整后卡片跟着更新；没答的问题进待处理。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.flow import blueprint as blueprint_service
from app.services.packs import contracts
from app.services.workspace import WorkspaceManager
from tests.proc import run as run_cmd
from tests.test_changesets import git, main_write
from tests.test_session_host import FakeEngine, result, settle

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"
DRAFT = {
    "goal_md": "合成目标：按规则生成补货单",
    "out_of_scope": ["采购价格"],
    "slices": [
        {"name": "补货申请", "core_flow": "受理三道校验", "objects": ["门店", "商品品类"], "operations": ["受理补货申请"]},
        {"name": "安全库存", "depends_on": ["补货申请"], "objects": ["安全库存标准"], "operations": ["测算安全库存"],
         "rules": ["取严规则"]},
        {"name": "陈列量", "depends_on": ["补货申请"], "objects": ["陈列标准"], "operations": ["核定陈列量"]},
        {"name": "超量审批", "depends_on": ["安全库存", "陈列量"], "operations": ["生成审批提示"], "rules": ["超量规则"]},
    ],
    "terms": {"aligned": ["门店"], "ambiguous": [{"variants": ["临期", "近效期"]}]},
    "questions": [{"title": "台账要不要建成对象？", "background": "合成背景", "options": ["要", "不要"]}],
}


def bp():
    return contracts.default().load("blueprint")


# ---------------------------------------------------------------------------
# 脚本：草稿、校验、调整
# ---------------------------------------------------------------------------


def test_resubmitting_a_merged_slice_keeps_it():
    """原样再提交早先合并出来的段（带着它的 merged_from）：编号和合并来源不变，不报错。"""
    module = bp()
    doc = module.apply_slices(module.empty(), [{"id": "tmp-a", "name": "下单"}, {"id": "tmp-b", "name": "退款"}])
    assert [item["id"] for item in doc["slices"]] == ["s1", "s2"]
    doc = module.apply_slices(doc, [{"id": "tmp-m", "name": "交易", "merged_from": ["s1", "s2"]}])
    assert [(item["id"], item["merged_from"]) for item in doc["slices"]] == [("s3", ["s1", "s2"])]
    again = module.apply_slices(doc, [{"id": "s3", "name": "交易", "merged_from": ["s1", "s2"]}])
    assert [(item["id"], item["merged_from"]) for item in again["slices"]] == [("s3", ["s1", "s2"])]


def test_draft_merges_into_one_full_slice():
    module = bp()
    doc = module.from_draft(DRAFT)
    # 草稿里的四段合成一段「全部」：条目取并集（按出现顺序去重），依据也并进来
    assert [item["id"] for item in doc["slices"]] == ["all"] and doc["slices"][0]["name"] == "全部"
    full = doc["slices"][0]
    assert full["objects"] == ["门店", "商品品类", "安全库存标准", "陈列标准"]
    assert full["operations"] == ["受理补货申请", "测算安全库存", "核定陈列量", "生成审批提示"]
    assert full["rules"] == ["取严规则", "超量规则"] and full["depends_on"] == []
    assert doc["version_no"] == 1 and doc["state"] == "draft" and doc["next_ids"]["slice"] == 1
    assert doc["terms"]["ambiguous"][0]["id"] == "t1" and doc["questions"][0]["options"][1] == {"id": "B", "label": "不要"}
    assert module.mode(doc) == "full" and module.mode(None) == "full"
    view = module.view(doc)
    assert view["version_label"] == "草案 v1" and view["mode"] == "full"
    assert [item["order_label"] for item in view["slices"]] == [""]
    assert view["slices"][0]["scale"] == {"objects": 4, "rules": 2}
    # 排除的段不并进来；重新资料分析：草案号加 1，仍是一段「全部」
    again = module.from_draft({**DRAFT, "slices": [DRAFT["slices"][0], {**DRAFT["slices"][2], "excluded": True,
                                                                      "depends_on": []}]}, doc)
    assert again["version_no"] == 2 and [item["id"] for item in again["slices"]] == ["all"]
    assert again["slices"][0]["objects"] == ["门店", "商品品类"]
    # 草稿照样校验：依赖写错、条目名不合法都拒绝
    with pytest.raises(module.BlueprintError) as caught:
        module.from_draft({"slices": [{"name": "甲", "depends_on": ["不存在"]}, {"name": "乙"}]})
    assert caught.value.reason == "unknown_dependency"
    with pytest.raises(module.BlueprintError) as caught:
        module.from_draft({"slices": [{"name": "甲", "objects": ["带/斜杠"]}, {"name": "乙"}]})
    assert caught.value.reason == "member_invalid"


def test_old_multi_slice_blueprint_is_read_as_full_mode():
    """F8 之前写下的多段蓝图：照样读、校验，但按全量走（段说明是全量模式，不按段收窄）。"""
    module = bp()
    old = module.from_draft(DRAFT)
    first, second = dict(old["slices"][0]), dict(old["slices"][0])
    old["slices"] = [{**first, "id": "s1", "name": "补货申请", "objects": ["门店"], "operations": [], "rules": []},
                     {**second, "id": "s2", "name": "安全库存", "objects": ["安全库存标准"], "depends_on": ["s1"],
                      "operations": [], "rules": []}]
    assert module.validate(old) == []
    assert len(module.live_slices(old)) == 2 and module.mode(old) == "full"
    assert module.brief(old, "s2").startswith("全量模式：按全部资料建模。")
    assert module.view(old)["mode"] == "full"


def test_terms_and_answers_go_into_the_brief():
    """调整（BP-02）在全量模式下主要是统一叫法、回答问题；定下的叫法进段说明（terms_line），确认后版本号不带「草案」。"""
    module = bp()
    doc = module.from_draft(DRAFT)
    assert module.terms_line(doc) == "" and module.terms_line(None) == ""
    assert module.brief(doc, None) == "全量模式：按全部资料建模。"
    answered = module.apply_edit(doc, {"mode": "draft", "term_choices": [{"term_id": "t1", "chosen": "临期"}],
                                       "answers": [{"question_id": "q1", "option_id": "A"}]})
    assert answered["version_no"] == 1
    assert answered["terms"]["ambiguous"][0]["chosen"] == "临期" and answered["questions"][0]["answer"] == {"option_id": "A"}
    assert module.terms_line(answered) == "统一叫法：临期（不用 近效期）"
    assert module.brief(answered, "all") == "全量模式：按全部资料建模。\n统一叫法：临期（不用 近效期）"
    adjusted = module.apply_edit(answered, {"mode": "adjust", "term_choices": [{"term_id": "t1", "chosen": "近效期"}]})
    assert adjusted["version_no"] == 2 and module.terms_line(adjusted) == "统一叫法：近效期（不用 临期）"
    assert module.view(module.confirm(adjusted))["version_label"] == "v2"
    with pytest.raises(module.BlueprintError) as caught:
        module.apply_edit(doc, {"mode": "draft", "term_choices": [{"term_id": "t9", "chosen": "x"}]})
    assert caught.value.reason == "term_invalid"
    # 注入给模型的叫法有上限（最坏情况约 3000 字）
    many = module.from_draft({"slices": [{"name": "甲"}], "terms": {"ambiguous": [
        {"variants": [f"叫法{n}甲", f"叫法{n}乙"]} for n in range(1, 41)]}})
    many = module.apply_edit(many, {"mode": "draft", "term_choices": [
        {"term_id": f"t{n}", "chosen": f"叫法{n}甲"} for n in range(1, 41)]})
    assert module.terms_line(many).count("（不用") == module.TERMS_LINE_MAX


def test_cli_writes_checks_and_briefs_full_mode(tmp_path):
    draft = {**DRAFT, "terms": {"aligned": ["门店"], "ambiguous": [{"variants": ["临期", "近效期"], "chosen": "临期"}]}}
    (tmp_path / "draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    cli = [sys.executable, str(SCRIPTS / "blueprint.py")]

    def run(*args):
        return run_cmd([*cli, *args], cwd=tmp_path, capture_output=True, text=True)

    written = run("write", "draft.json")
    assert written.returncode == 0 and written.stdout.startswith("ok:research/blueprint.json:草案 v1:1 段")
    assert run("check").returncode == 0
    (tmp_path / ".onto" / "run").mkdir(parents=True)
    # F8 之前的执行记录带着分段：段说明照样是全量模式
    (tmp_path / ".onto" / "run" / "exec.json").write_text(json.dumps({"slice": {"id": "s2", "name": "安全库存"}}))
    brief = run("brief").stdout
    assert brief.strip() == "全量模式：按全部资料建模。\n统一叫法：临期（不用 近效期）"
    assert json.loads(run("slices", "--json").stdout) == {"mode": "full",
                                                         "slices": [{"id": "all", "name": "全部", "depends_on": []}]}
    assert run("assign", "s2", "object:补货台账").returncode == 1
    assert run("write", "missing.json").returncode == 1


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(blueprint_service, "_autobuild_preview", None)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    from app import wiring

    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", wiring._session_activity)
        project_id = workspaces.create_project("建模蓝图", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        yield client, workspaces, manager, project_id, root
    streams.reset_all()


def _write_mainline(root: Path, doc: dict) -> None:
    main_write(root, "research/blueprint.json", bp().dumps(doc), "research: 合成蓝图")


def test_blueprint_read_save_and_revision_conflict_on_mainline(env):
    client, _workspaces, _manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/blueprint"
    assert client.get(base).json()["code"] == "blueprint_not_found"
    assert client.put(base, json={"base_revision": "", "mode": "draft"}).json()["code"] == "blueprint_not_found"
    _write_mainline(root, bp().from_draft(DRAFT))
    got = client.get(base).json()
    assert got["version_label"] == "草案 v1" and got["state"] == "draft" and len(got["revision"]) == 16
    assert set(got) >= {"version_no", "version_label", "state", "goal_md", "goal_anchor", "slices",
                        "terms", "questions", "research", "evidence_hint", "revision", "updated_at"}
    assert got["research"] == {"verified": False, "report_id": None}
    stale = client.put(base, json={"base_revision": "0" * 16, "mode": "draft"})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["details"]["actual"] == got["revision"]
    assert [item["id"] for item in got["slices"]] == ["all"]
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "draft",
                                   "term_choices": [{"term_id": "t1", "chosen": "临期"}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["terms"]["ambiguous"][0]["chosen"] == "临期" and saved.json()["version_label"] == "草案 v1"
    assert "web: 调整建模蓝图" in git(root, "log", "-1", "--format=%s")
    adjusted = client.put(base, json={"base_revision": saved.json()["revision"], "mode": "adjust",
                                      "answers": [{"question_id": "q1", "option_id": "A"}]})
    assert adjusted.status_code == 200 and adjusted.json()["version_label"] == "草案 v2", adjusted.text
    # 唯一的一段「全部」不能排除
    only = [{"id": "all", "name": "全部", "core_flow": "", "depends_on": [], "excluded": True}]
    invalid = client.put(base, json={"base_revision": adjusted.json()["revision"], "mode": "draft", "slices": only})
    assert invalid.status_code == 400 and invalid.json()["code"] == "slice_invalid"
    assert invalid.json()["details"]["reason"] == "empty"
    bad_term = client.put(base, json={"base_revision": adjusted.json()["revision"], "mode": "draft",
                                      "term_choices": [{"term_id": "t9", "chosen": "x"}]})
    assert bad_term.status_code == 400 and bad_term.json()["details"]["fields"] == {"term_choices": "invalid"}
    assert client.put(base, json={"base_revision": "", "mode": "bogus"}).json()["code"] == "invalid_argument"


def test_confirm_l4_is_409_until_autobuild_registers_and_l2_starts_modeling(env, monkeypatch):
    """BP-03 仍可调（F8 之前的界面与脚本）：L2 发建模第一步，全量模式不带分段。"""
    client, _workspaces, manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/blueprint"
    _write_mainline(root, bp().from_draft(DRAFT))
    revision = client.get(base).json()["revision"]
    refused = client.post(f"{base}/confirm", json={"base_revision": revision, "next": "L4"})
    assert refused.status_code == 409 and refused.json()["code"] == "autobuild_not_available"
    assert client.get(base).json()["state"] == "draft" and client.get(base).json()["revision"] == revision
    assert client.post(f"{base}/confirm", json={"base_revision": "0" * 16}).json()["code"] == "revision_conflict"
    confirmed = client.post(f"{base}/confirm", json={"base_revision": revision, "next": "L2"})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["blueprint"]["state"] == "confirmed" and body["blueprint"]["version_label"] == "v1"
    assert body["command_task"]["title"].startswith("助手正在执行")
    for _ in range(200):
        if FakeEngine.sent:
            break
        time.sleep(0.01)
    assert FakeEngine.sent and FakeEngine.sent[0].startswith("/ontology-extract-object")
    assert "slice=" not in FakeEngine.sent[0]
    assert "web: 确认建模蓝图" in git(root, "log", "--format=%s", "-3")
    # B5b 登记之后 L4 返回开始前预览
    seen = {}

    def preview(manager_, project_id_, blueprint, *, actor):
        seen.update(blueprint=blueprint, actor=actor)
        return {"preview_token": "pv-1", "slices": [{"id": item["id"]} for item in blueprint["slices"]]}

    monkeypatch.setattr(blueprint_service, "_autobuild_preview", preview)
    again = client.post(f"{base}/confirm", json={"base_revision": "", "next": "L4"}).json()
    assert again["autobuild_preview"]["preview_token"] == "pv-1" and seen["blueprint"]["state"] == "confirmed"


def _research_turn(manager, project_id, root):
    """资料分析这一轮：助手在副本里用脚本写出蓝图草案（假引擎按脚本动作执行）。"""

    def write(ctx):
        cwd = Path(ctx.cwd)
        (cwd / ".ontology-state").mkdir(exist_ok=True)
        (cwd / ".ontology-state" / "blueprint-draft.json").write_text(json.dumps(DRAFT, ensure_ascii=False))
        done = run_cmd([sys.executable, str(cwd / ".claude" / "scripts" / "blueprint.py"), "write",
                        ".ontology-state/blueprint-draft.json"], cwd=cwd, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    FakeEngine.scripts = [[write, result()]]

    async def turn():
        await manager.send(project_id, root, "生成建模蓝图", username="employee_0001")
        await settle(manager, project_id)
        await manager.shutdown()

    asyncio.run(turn())


def test_blueprint_from_the_research_turn_is_on_mainline_and_confirm_only_marks_it(env):
    """F3：资料分析那一轮结束就合入，蓝图在主线上；调整、回答经写入事务写主线，确认蓝图不再先确认生效。"""
    client, workspaces, manager, project_id, root = env
    _research_turn(manager, project_id, root)
    assert (root / "research" / "blueprint.json").is_file()
    history = manager.get_history(project_id, "default")
    cards = [event["payload"] for event in history if event.get("type") == "card"]
    kinds = [card["card_type"] for card in cards]
    assert "round_changes" in kinds and "blueprint" in kinds
    assert "build_choice" not in kinds
    card = next(card for card in cards if card["card_type"] == "blueprint")
    assert card["title"] == "建模蓝图 · 草案 v1" and card["open"] == {"kind": "dialog", "target": "blueprint"}
    assert card["questions_pending"] == 1 and card["terms"] == {"aligned": 1, "ambiguous": 1}
    assert card["confirm_options"] == []  # F8：蓝图不用确认，卡片上没有确认按钮
    rc = next(card for card in cards if card["card_type"] == "round_changes")
    assert rc["groups"][0]["items"][0]["target"]["title"] == "建模蓝图"
    base = f"/api/v1/projects/{project_id}/blueprint"
    got = client.get(base).json()
    assert got["state"] == "draft" and [item["id"] for item in got["slices"]] == ["all"]
    # 待处理：蓝图里没答的问题
    items = client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]
    question = next(item for item in items if item["id"] == "decision:bq-q1")
    assert question["kind"] == "decision"
    # 调整（统一叫法）写主线；蓝图卡跟着更新
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "adjust",
                                   "term_choices": [{"term_id": "t1", "chosen": "临期"}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version_label"] == "草案 v2"
    assert json.loads((root / "research" / "blueprint.json").read_text(encoding="utf-8"))["version_no"] == 2
    updated = [event["payload"] for event in manager.get_history(project_id, "default")
               if event.get("type") == "card_updated" and event["payload"].get("card_type") == "blueprint"]
    assert updated and updated[-1]["version_label"] == "草案 v2"
    # 回答问题（待处理 IN-03）
    answered = client.post(f"/api/v1/projects/{project_id}/inbox/decision:bq-q1/actions",
                           json={"action": "answer", "option_id": "A"})
    assert answered.status_code == 200, answered.text
    assert client.get(base).json()["questions"][0]["answer"] == {"option_id": "A"}
    # 确认蓝图：只在主线上标已确认
    confirmed = client.post(f"{base}/confirm", json={"base_revision": client.get(base).json()["revision"],
                                                     "next": "L2"})
    assert confirmed.status_code == 200, confirmed.text
    on_main = json.loads((root / "research" / "blueprint.json").read_text(encoding="utf-8"))
    assert on_main["state"] == "confirmed" and on_main["version_no"] == 2
    assert on_main["terms"]["ambiguous"][0]["chosen"] == "临期"
    assert changesets.current(workspaces, project_id) is None
    assert "Onto-Changeset: cs-1" in git(root, "log", "--format=%B", "-6")
    # I1 合并：确认之后蓝图卡所在对话的建议条跟着换，不再停在「确认蓝图」
    bars = [event["payload"] for event in manager.get_history(project_id, "default") if event.get("type") == "suggestions"]
    assert bars and (bars[-1].get("primary") or {}).get("id") != "confirm_blueprint"


def test_saving_while_the_assistant_runs_is_423(env, monkeypatch):
    client, workspaces, manager, project_id, root = env
    _research_turn(manager, project_id, root)
    # 助手又开始改蓝图（这一轮还在跑）：蓝图在副本里，调整回 423
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    draft = json.loads((copy / "research" / "blueprint.json").read_text(encoding="utf-8"))
    draft["slices"][0]["name"] = "助手正在改"
    (copy / "research" / "blueprint.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    changesets.observe(workspaces, project_id, running=True, conversation_id="default")
    got = client.get(f"/api/v1/projects/{project_id}/blueprint").json()
    assert got["slices"][0]["name"] == "助手正在改"
    monkeypatch.setattr(changesets, "_activity_provider",
                        lambda pid: {"running": True, "conversation_id": "default", "exec": None, "affected": []})
    locked = client.put(f"/api/v1/projects/{project_id}/blueprint",
                        json={"base_revision": got["revision"], "mode": "draft"})
    assert locked.status_code == 423 and locked.json()["code"] == "write_scope_locked"


def test_blueprint_card_has_no_confirm_buttons_even_with_autobuild(env, monkeypatch):
    """F8：蓝图不用确认，草案、已确认、自主构建登记与否，卡片底部都没有确认按钮。"""
    _client, workspaces, _manager, project_id, root = env
    _write_mainline(root, bp().from_draft(DRAFT))
    monkeypatch.setattr(blueprint_service, "_autobuild_preview", lambda *args, **kwargs: {})
    cards = blueprint_service.round_cards(workspaces, project_id, {"added": ["research/blueprint.json"]}, "seed")
    assert [card["card_type"] for card in cards] == ["blueprint"]
    assert cards[0]["confirm_options"] == []
    confirmed = blueprint_service.blueprint_card(bp(), bp().confirm(bp().from_draft(DRAFT)), "c", research_verified=True)
    assert confirmed["confirm_options"] == []
    assert blueprint_service.round_cards(workspaces, project_id, {"modified": ["ontology/object/x.md"]}, "s") == []
