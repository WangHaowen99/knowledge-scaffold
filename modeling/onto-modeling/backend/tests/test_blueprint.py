"""建模蓝图（详设第 9 章建议 2、6.8；契约 BP-01～BP-03、4.7 blueprint 卡、7.8 Blueprint；ADR-0050）。

- 蓝图文件由 core 的 blueprint.py 写入与校验：草稿编号、依赖解析、全量模式（只有一片）；
- 调整（BP-02）：合并、排除、改名、调序、新增都是整份切片列表；草案 / 调整两种保存、revision 冲突；
- 蓝图在资料分析那一轮的修改里时：读、调整都对副本，确认蓝图先确认生效这一轮再标已确认；
- 确认（BP-03）：L2 往对话发「开始第一个切片」，L4 在自主构建（B5b）登记前 409、蓝图不动；
- 一轮写出蓝图时对话里出蓝图卡，调整后卡片跟着更新；没答的问题进待处理。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
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


def test_draft_gets_ids_dependencies_and_full_mode_for_one_slice():
    module = bp()
    doc = module.from_draft(DRAFT)
    assert [item["id"] for item in doc["slices"]] == ["s1", "s2", "s3", "s4"]
    assert doc["slices"][3]["depends_on"] == ["s2", "s3"] and doc["version_no"] == 1 and doc["state"] == "draft"
    assert doc["terms"]["ambiguous"][0]["id"] == "t1" and doc["questions"][0]["options"][1] == {"id": "B", "label": "不要"}
    assert module.mode(doc) == "sliced"
    view = module.view(doc)
    assert view["version_label"] == "草案 v1"
    assert [item["order_label"] for item in view["slices"]] == ["推荐先做", "第 2 个", "第 3 个", "建议最后做"]
    assert view["slices"][0]["scale"] == {"objects": 2, "rules": 0}
    # 重新规划：草案号加 1，同名切片沿用旧编号
    again = module.from_draft({**DRAFT, "slices": DRAFT["slices"][:2] + [{"name": "新的一片", "depends_on": ["s1"]}]},
                              doc)
    assert again["version_no"] == 2 and [item["id"] for item in again["slices"]] == ["s1", "s2", "s5"]
    # 只有一片就是全量模式
    single = module.from_draft({"goal_md": "小项目", "slices": [{"name": "全部"}]})
    assert single["slices"][0]["id"] == "all" and module.mode(single) == "full"
    assert module.mode(None) == "full"
    with pytest.raises(module.BlueprintError) as caught:
        module.from_draft({"slices": [{"name": "甲", "depends_on": ["不存在"]}, {"name": "乙"}]})
    assert caught.value.reason == "unknown_dependency"
    with pytest.raises(module.BlueprintError) as caught:
        module.from_draft({"slices": [{"name": "甲", "objects": ["带/斜杠"]}, {"name": "乙"}]})
    assert caught.value.reason == "member_invalid"


def _inputs(doc):
    return [{"id": item["id"], "name": item["name"], "core_flow": item["core_flow"],
             "depends_on": list(item["depends_on"]), "excluded": item["excluded"]} for item in doc["slices"]]


def test_slice_edits_merge_exclude_rename_reorder_and_add():
    module = bp()
    doc = module.from_draft(DRAFT)
    # 合并「安全库存」「陈列量」：条目取并集，依赖它们的片改指新片，被合并的片去掉
    items = _inputs(doc)
    merged = {"id": "new-1", "name": "库存与陈列", "core_flow": "", "depends_on": ["s1"], "excluded": False,
              "merged_from": ["s2", "s3"]}
    items = [items[0], merged, {**items[3], "depends_on": ["new-1"]}]
    result = module.apply_edit(doc, {"mode": "adjust", "slices": items})
    ids = [item["id"] for item in result["slices"]]
    assert ids == ["s1", "s5", "s4"] and result["version_no"] == 2
    combined = result["slices"][1]
    assert combined["objects"] == ["安全库存标准", "陈列标准"] and combined["rules"] == ["取严规则"]
    assert combined["merged_from"] == ["s2", "s3"] and result["slices"][2]["depends_on"] == ["s5"]
    # 排除、改名、新增；草案保存草案号不变
    items = _inputs(result)
    items[2] = {**items[2], "excluded": True}
    items[1] = {**items[1], "name": "库存陈列"}
    items.append({"id": "new-2", "name": "补充一片", "core_flow": "", "depends_on": ["s1"], "excluded": False})
    edited = module.apply_edit(result, {"mode": "draft", "slices": items})
    assert edited["version_no"] == 2 and edited["slices"][1]["name"] == "库存陈列"
    assert edited["slices"][2]["excluded"] and edited["slices"][3]["id"] == "s6"
    assert module.view(edited)["slices"][2]["order_label"] == ""
    # 调序：依赖的片排到后面就拒绝
    swapped = _inputs(edited)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    with pytest.raises(module.BlueprintError) as caught:
        module.apply_edit(edited, {"mode": "draft", "slices": swapped})
    assert caught.value.reason == "dependency_order"
    reordered = _inputs(edited)
    reordered[1], reordered[3] = reordered[3], reordered[1]
    assert [item["id"] for item in module.apply_edit(edited, {"mode": "draft", "slices": reordered})["slices"]] == \
        ["s1", "s6", "s4", "s5"]
    bad_cases = [
        ([{**item, "excluded": True} for item in _inputs(edited)], "empty"),
        (_inputs(edited)[:2], "missing"),
        (_inputs(edited) + [{"id": "new-3", "name": "x", "merged_from": ["s1"]}], "merge_too_few"),
        ([{**item, "name": "同名"} if index < 2 else item for index, item in enumerate(_inputs(edited))],
         "duplicate_name"),
        ([{**item, "depends_on": ["s4"]} if item["id"] == "s5" else item for item in _inputs(edited)],
         "dependency_excluded"),
    ]
    for slices, reason in bad_cases:
        with pytest.raises(module.BlueprintError) as caught:
            module.apply_edit(edited, {"mode": "draft", "slices": slices})
        assert caught.value.reason == reason, (reason, caught.value.reason)
    answered = module.apply_edit(edited, {"mode": "draft", "term_choices": [{"term_id": "t1", "chosen": "临期"}],
                                          "answers": [{"question_id": "q1", "option_id": "A"}]})
    assert answered["terms"]["ambiguous"][0]["chosen"] == "临期" and answered["questions"][0]["answer"] == {"option_id": "A"}
    assert module.view(module.confirm(answered))["version_label"] == "v2"


def test_cli_writes_checks_and_briefs_a_slice(tmp_path):
    (tmp_path / "draft.json").write_text(json.dumps(DRAFT, ensure_ascii=False), encoding="utf-8")
    cli = [sys.executable, str(SCRIPTS / "blueprint.py")]

    def run(*args):
        return subprocess.run([*cli, *args], cwd=tmp_path, capture_output=True, text=True)

    written = run("write", "draft.json")
    assert written.returncode == 0 and written.stdout.startswith("ok:research/blueprint.json:草案 v1:4 片")
    assert run("check").returncode == 0
    (tmp_path / ".onto" / "run").mkdir(parents=True)
    (tmp_path / ".onto" / "run" / "exec.json").write_text(json.dumps({"slice": {"id": "s2", "name": "安全库存"}}))
    brief = run("brief").stdout
    assert "切片：安全库存（s2）" in brief and "依赖：补货申请" in brief and "门店、商品品类" in brief
    assert run("assign", "s2", "object:补货台账", "object:门店").stdout.strip() == "ok:1"
    doc = json.loads((tmp_path / "research" / "blueprint.json").read_text(encoding="utf-8"))
    assert doc["slices"][1]["objects"] == ["安全库存标准", "补货台账"]
    assert json.loads(run("slices", "--json").stdout)["mode"] == "sliced"
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
    assert set(got) >= {"version_no", "version_label", "state", "goal_md", "goal_anchor", "out_of_scope", "slices",
                        "terms", "questions", "research", "evidence_hint", "revision", "updated_at"}
    assert got["research"] == {"verified": False, "report_id": None}
    stale = client.put(base, json={"base_revision": "0" * 16, "mode": "draft", "out_of_scope": ["x"]})
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    assert stale.json()["details"]["actual"] == got["revision"]
    slices = [{"id": item["id"], "name": item["name"], "core_flow": item["core_flow"],
               "depends_on": item["depends_on"], "excluded": item["excluded"]} for item in got["slices"]]
    slices[0]["name"] = "补货申请受理"
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "draft", "slices": slices})
    assert saved.status_code == 200, saved.text
    assert saved.json()["slices"][0]["name"] == "补货申请受理" and saved.json()["version_label"] == "草案 v1"
    assert "web: 调整建模蓝图" in git(root, "log", "-1", "--format=%s")
    adjusted = client.put(base, json={"base_revision": saved.json()["revision"], "mode": "adjust",
                                      "slices": [{**slices[0], "excluded": True}, *[{**item, "depends_on": []}
                                                                                     for item in slices[1:]]]})
    assert adjusted.status_code == 200 and adjusted.json()["version_label"] == "草案 v2", adjusted.text
    invalid = client.put(base, json={"base_revision": adjusted.json()["revision"], "mode": "draft",
                                     "slices": slices[:1]})
    assert invalid.status_code == 400 and invalid.json()["code"] == "slice_invalid"
    assert invalid.json()["details"]["reason"] == "missing"
    assert client.put(base, json={"base_revision": "", "mode": "bogus"}).json()["code"] == "invalid_argument"


def test_confirm_l4_is_409_until_autobuild_registers_and_l2_starts_the_first_slice(env, monkeypatch):
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
    assert FakeEngine.sent and FakeEngine.sent[0].startswith("/ontology-extract-object slice=s1")
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
        done = subprocess.run([sys.executable, str(cwd / ".claude" / "scripts" / "blueprint.py"), "write",
                               ".ontology-state/blueprint-draft.json"], cwd=cwd, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    FakeEngine.scripts = [[write, result()]]

    async def turn():
        await manager.send(project_id, root, "生成建模蓝图", username="employee_0001")
        await settle(manager, project_id)
        await manager.shutdown()

    asyncio.run(turn())


def test_blueprint_in_the_research_changeset_is_edited_in_the_copy_and_confirm_merges_it(env):
    client, workspaces, manager, project_id, root = env
    _research_turn(manager, project_id, root)
    assert not (root / "research" / "blueprint.json").exists()
    history = manager.get_history(project_id, "default")
    cards = [event["payload"] for event in history if event.get("type") == "card"]
    kinds = [card["card_type"] for card in cards]
    assert "round_changes" in kinds and "blueprint" in kinds and "build_choice" in kinds
    card = next(card for card in cards if card["card_type"] == "blueprint")
    assert card["title"] == "建模蓝图 · 草案 v1" and card["open"] == {"kind": "dialog", "target": "blueprint"}
    assert card["questions_pending"] == 1 and card["terms"] == {"aligned": 1, "ambiguous": 1}
    choice = next(card for card in cards if card["card_type"] == "build_choice")
    assert [option["mode"] for option in choice["options"]] == ["L2"]  # 自主构建没登记：只给能用的
    rc = next(card for card in cards if card["card_type"] == "round_changes")
    assert rc["groups"][0]["items"][0]["target"]["title"] == "建模蓝图"
    base = f"/api/v1/projects/{project_id}/blueprint"
    got = client.get(base).json()
    assert got["state"] == "draft" and len(got["slices"]) == 4
    # 待处理：蓝图里没答的问题
    items = client.get(f"/api/v1/projects/{project_id}/inbox").json()["items"]
    question = next(item for item in items if item["id"] == "decision:bq-q1")
    assert question["kind"] == "decision"
    # 调整写进副本（这一轮的修改的一部分），主线不动；蓝图卡跟着更新
    slices = [{"id": item["id"], "name": item["name"], "core_flow": item["core_flow"],
               "depends_on": item["depends_on"], "excluded": item["excluded"]} for item in got["slices"]]
    slices[2]["excluded"] = True
    slices[3]["depends_on"] = ["s2"]
    saved = client.put(base, json={"base_revision": got["revision"], "mode": "adjust", "slices": slices})
    assert saved.status_code == 200, saved.text
    assert saved.json()["version_label"] == "草案 v2"
    assert not (root / "research" / "blueprint.json").exists()
    copy = changesets.existing_copy(root)
    assert json.loads((copy / "research" / "blueprint.json").read_text(encoding="utf-8"))["version_no"] == 2
    updated = [event["payload"] for event in manager.get_history(project_id, "default")
               if event.get("type") == "card_updated" and event["payload"].get("card_type") == "blueprint"]
    assert updated and updated[-1]["version_label"] == "草案 v2"
    # 回答问题（待处理 IN-03）也写进副本
    answered = client.post(f"/api/v1/projects/{project_id}/inbox/decision:bq-q1/actions",
                           json={"action": "answer", "option_id": "A"})
    assert answered.status_code == 200, answered.text
    assert client.get(base).json()["questions"][0]["answer"] == {"option_id": "A"}
    # 确认蓝图：先把这一轮确认生效，再在主线上标已确认
    confirmed = client.post(f"{base}/confirm", json={"base_revision": client.get(base).json()["revision"],
                                                     "next": "L2"})
    assert confirmed.status_code == 200, confirmed.text
    on_main = json.loads((root / "research" / "blueprint.json").read_text(encoding="utf-8"))
    assert on_main["state"] == "confirmed" and on_main["version_no"] == 2 and on_main["slices"][2]["excluded"]
    assert changesets.current(workspaces, project_id) is None or \
        changesets.current(workspaces, project_id)["state"] in ("open", "pending_confirm")
    assert "Onto-Changeset: cs-1" in git(root, "log", "--format=%B", "-4")


def test_saving_while_the_assistant_runs_is_423(env, monkeypatch):
    client, _workspaces, manager, project_id, root = env
    _research_turn(manager, project_id, root)
    got = client.get(f"/api/v1/projects/{project_id}/blueprint").json()
    monkeypatch.setattr(changesets, "_activity_provider",
                        lambda pid: {"running": True, "conversation_id": "default", "exec": None, "affected": []})
    locked = client.put(f"/api/v1/projects/{project_id}/blueprint",
                        json={"base_revision": got["revision"], "mode": "draft", "out_of_scope": ["x"]})
    assert locked.status_code == 423 and locked.json()["code"] == "write_scope_locked"
