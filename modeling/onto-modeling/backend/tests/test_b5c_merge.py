"""B5c 合回交付分支后的衔接：B4b 审查修复与节点包两边的行为合在一起都成立。

- 插件变更集不占「没关闭的变更集」位置（`store.record_closed`）：助手的修改没确认时，不改流程的切换也记成变更集；
- 人工节点的决策卡进待处理的「决策」，回答后消失；
- 节点状态：升级标的需复核在人确认后清除、跳过的可选插件节点补做后不再算跳过、IN-04 按流程顺序选过期节点且不选
  熔断了的节点；
- 包声明的提交门禁在助手一轮的合入里只提示（F3）。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import copy

import pytest
from fastapi.responses import JSONResponse

from app.kernel.changesets import service as changesets
from app.kernel.changesets import store
from app.services.flow import inbox
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.packs import lockfile
from app.services.packs import service as pack_service
from app.services.workspace.errors import WorkspaceError
from tests.pack_helpers import NODE_PACK_FLOW
from tests.test_changesets import agent_write, main_write, obj
from tests.test_node_pack_lifecycle import (  # noqa: F401
    HUMAN_NODE,
    enable_and_apply,
    env,
    flow_before_model,
    git,
    install,
    kinds_flow,
    nodes_of,
    trailer,
)
from tests.test_pack_extensions import env as ext_env  # noqa: F401
from tests.test_pack_extensions import item_md

# ---- 插件变更集：只追加已关闭的记录，不动 current ----


def test_record_closed_appends_without_moving_current(env):
    _client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    assistant = store.create(root, purpose="助手的修改")
    closed = store.record_closed(root, source="plugin", state="merged", pack="naming")
    assert closed["id"] != assistant["id"] and closed["closed_at"]
    assert store.current(root)["id"] == assistant["id"]
    with pytest.raises(ValueError):
        store.record_closed(root, source="plugin", state="open")
    store.update(root, assistant["id"], state="discarded")
    store.record_closed(root, source="plugin", state="discarded")
    assert store.current(root) is None


def test_switch_while_assistant_changes_are_pending_is_a_revertible_changeset(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    copy_dir = changesets.copy_for_turn(manager, project_id, root)
    agent_write(copy_dir, "ontology/object/读者.md", obj("读者", "合成读者"), commit="agent: 读者")
    pending = changesets.observe(manager, project_id, running=False, conversation_id="default")
    lock_before = (root / lockfile.LOCK_REL).read_text(encoding="utf-8")

    # 改流程的照旧 423（口径不变）
    blocked = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"})
    assert blocked.status_code == 423 and blocked.json()["code"] == "write_scope_locked"
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "industry-retail"})
    assert enabled.status_code == 201, enabled.text
    cs = enabled.json()["changeset_id"]
    assert cs and cs != pending["id"] and trailer(root, "Onto-Changeset") == cs
    assert changesets.current(manager, project_id)["id"] == pending["id"]
    # 撤回要在主线写反向提交：助手那组修改确认或撤回之前不行，之后照常
    with pytest.raises(WorkspaceError) as exc:
        changesets.revert_merged(manager, project_id, cs, actor="local")
    assert exc.value.code == "write_scope_locked"
    changesets.discard(manager, project_id, pending["id"], actor="local")
    changesets.revert_merged(manager, project_id, cs, actor="local")
    assert (root / lockfile.LOCK_REL).read_text(encoding="utf-8") == lock_before


def test_plugin_changeset_cut_off_mid_switch_is_reconciled_at_startup(env, monkeypatch):
    _client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    from app.services.packs import changes

    finish = changes.finish

    def crash(*_args, **_kwargs):
        raise RuntimeError("模拟停机：提交之后、记已合入之前")

    monkeypatch.setattr(changes, "finish", crash)
    with pytest.raises(RuntimeError):
        pack_service.enable(manager, project_id, "naming")
    monkeypatch.setattr(changes, "finish", finish)
    cut = next(record for record in changesets.records(manager, project_id, sources=["plugin"])
               if record.get("committing"))
    assert cut["state"] == "discarded" and trailer(root, "Onto-Changeset") == cut["id"]
    with manager._project_lock(project_id):  # 记下了、还没提交就停机
        never = changes.begin(manager, project_id, purpose="没提交成", pack="naming", kind="upgrade",
                              flow_changed=False)

    changesets.startup_repair(manager)
    fixed = changesets.get(manager, project_id, cut["id"])
    assert fixed["state"] == "merged" and not fixed.get("committing")
    assert fixed["merged"]["commit"] == git(root, "rev-parse", "HEAD").strip()
    left = changesets.get(manager, project_id, never["id"])
    assert left["state"] == "discarded" and not left.get("committing")
    assert changesets.current(manager, project_id) is None
    # trailer 按整行认：cs-1 不会认成 cs-12 的提交
    commits = [("c12", "plugin: 启用\n\nOnto-Changeset: cs-12\n"), ("c1", "assistant: 改\n\nOnto-Changeset: cs-1\n")]
    assert changesets._commit_of(commits, "cs-1") == "c1"


# ---- 人工节点的决策卡进待处理 ----


def start_signoff(client, manager, project_id, tmp_path) -> dict:
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=kinds_flow(HUMAN_NODE, probes={
        "naming_signoff": {"files": ["reports"], "glob": "signoff-naming.md"}}))
    enable_and_apply(client, manager, project_id)
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.signoff/start", json={})
    assert started.status_code == 200, started.text
    return started.json()["question"]


def test_human_node_card_is_a_decision_in_the_inbox_until_answered(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    question = start_signoff(client, manager, project_id, tmp_path)
    item_id = f"decision:{question['question_id']}"

    listed = client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "decision"}).json()
    item = next(entry for entry in listed["items"] if entry["id"] == item_id)
    assert item["kind_label"] == "决策" and item["source"] == "plugin" and item["pack"] == "naming"
    assert item["title"] == "线下会签结果" and "meta" not in item and item["batchable"] is False
    assert item["actions"] == ["answer", "dismiss"]
    assert listed["counts"]["by_filter"]["decision"] == 1 and listed["counts"]["by_pack"]["naming"] >= 1
    detail = client.get(f"/api/v1/projects/{project_id}/inbox/{item_id}").json()["detail"]
    assert detail["question_id"] == question["question_id"] and [o["id"] for o in detail["options"]] == ["pass", "fail"]

    url = f"/api/v1/projects/{project_id}/inbox/{item_id}/actions"
    assert client.post(url, json={"action": "answer"}).json()["code"] == "answer_invalid"
    answered = client.post(url, json={"action": "answer", "option_id": "pass"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["resolved"] is True and answered.json()["result"]["question"]["state"] == "answered"
    assert "结论：已签署" in (root / "reports" / "signoff-naming.md").read_text(encoding="utf-8")
    assert inbox.find(manager, project_id, item_id) is None
    s11 = (f"/api/v1/projects/{project_id}/conversations/{question['conversation_id']}"
           f"/questions/{question['question_id']}/answer")
    assert client.post(s11, json={"option_id": "pass"}).status_code == 410
    assert nodes_of(root)["naming.signoff"]["state"] == "confirmed"


def test_human_node_card_answered_in_the_conversation_leaves_the_inbox(env):
    client, manager, project_id, tmp_path = env
    question = start_signoff(client, manager, project_id, tmp_path)
    item_id = f"decision:{question['question_id']}"
    assert inbox.find(manager, project_id, item_id) is not None
    s11 = (f"/api/v1/projects/{project_id}/conversations/{question['conversation_id']}"
           f"/questions/{question['question_id']}/answer")
    assert client.post(s11, json={"option_id": "fail"}).status_code == 200
    assert inbox.find(manager, project_id, item_id) is None


def test_card_of_a_disabled_pack_is_not_listed(env):
    client, manager, project_id, tmp_path = env
    question = start_signoff(client, manager, project_id, tmp_path)
    assert client.post(f"/api/v1/projects/{project_id}/packs/naming/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    assert inbox.find(manager, project_id, f"decision:{question['question_id']}") is None


# ---- 节点状态：B4b 修复与节点包的标记合在一起 ----


def test_upgrade_review_is_cleared_when_the_redone_node_is_confirmed(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    enable_and_apply(client, manager, project_id)
    main_write(root, "reports/naming-1.md", "# 命名检查\n\n合成报告\n")
    assert nodes_of(root)["naming.check"]["state"] == "confirmed"
    newer = copy.deepcopy(NODE_PACK_FLOW)
    newer["probes"]["naming_clean"] = {"pending": ["reports"], "glob": "naming-*.md"}
    newer["nodes"][0]["done"] = ["naming_report", "!naming_clean"]
    install(manager, tmp_path, "0.2.0", flow=newer)
    upgraded = client.post(f"/api/v1/projects/{project_id}/packs/naming/upgrade", json={"to_version": "0.2.0"})
    assert upgraded.status_code == 200, upgraded.text
    pack_service.prepare_for_message(manager, project_id)
    assert nodes_of(root)["naming.check"]["flags"]["review_source"] == "upgrade"  # B5c：升级标需复核
    review = inbox.find(manager, project_id, "review:naming.check")
    assert review["source"] == "upgrade" and review["pack"] == "naming" and "revert_slice" not in review["actions"]

    # 重做这一步、确认生效（B4b 第 21 条：人确认过就去掉需复核）
    copy_dir = changesets.copy_for_turn(manager, project_id, root)
    agent_write(copy_dir, "reports/naming-2.md", "# 命名检查\n\n重做后的合成报告\n", commit="agent: 重做命名检查")
    record = changesets.observe(manager, project_id, running=False, conversation_id="default", exec_record={
        "id": "ex-1", "node": {"id": "naming.check", "version": "naming@0.2.0"}})
    changesets.merge(manager, project_id, record["id"], actor="local")
    node = nodes_of(root)["naming.check"]
    assert node["state"] == "confirmed" and node["flags"]["needs_review"] is False
    assert node["display_state"] == "confirmed"
    assert inbox.find(manager, project_id, "review:naming.check") is None
    compat = client.get(f"/api/v1/packs/naming/versions/0.2.0/compatibility?project_id={project_id}").json()
    assert compat["impact"]["needs_review_after_upgrade"] == 0


def test_skipped_optional_plugin_node_done_later_is_confirmed(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0].update({"optional": True, "section": "optional", "placement": "branch"})
    install(manager, tmp_path, flow=flow)
    enable_and_apply(client, manager, project_id)
    skipped = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.check/skip", json={"reason": "先不做"})
    assert skipped.status_code == 200 and skipped.json()["node"]["state"] == "skipped"
    main_write(root, "reports/naming-1.md", "# 命名检查\n\n合成报告\n")
    node = nodes_of(root)["naming.check"]
    assert node["state"] == "confirmed" and node["flags"]["backfill"] is False
    assert "naming.check" not in node_state.skipped(root)


def test_refresh_stale_follows_flow_order_and_skips_broken_nodes(env, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    from app.governance import ops_state
    from app.routers import flow as flow_routes

    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    main_write(root, "ontology/operation/借书.md", "# 借书\n\n## 业务描述\n\n合成操作\n")
    install(manager, tmp_path, flow=flow_before_model())
    enable_and_apply(client, manager, project_id)
    main_write(root, "reports/naming-1.md", "# 命名检查\n\n合成报告\n")
    nodes = nodes_of(root)
    assert nodes["naming.check"]["state"] == nodes["core.object"]["state"] == nodes["core.operation"]["state"] == "confirmed"
    order = [node["id"] for node in flow_model.load_for(root)["nodes"]]
    assert order.index("naming.check") < order.index("core.object")
    main_write(root, "ontology/object/图书.md", obj("图书", "上游变了"))
    started = []

    async def fake_start(_project_id, node_id, _request):
        started.append(node_id)
        return JSONResponse(status_code=202, content={"task": None})

    monkeypatch.setattr(flow_routes, "start_node", fake_start)
    batch = f"/api/v1/projects/{project_id}/inbox/batch"
    response = client.post(batch, json={"action": "refresh_stale"}).json()
    assert started == ["naming.check"], response  # 插件节点在流程里更靠上游
    ops_state.set_breaker("naming", "0.1.0", "open", "ops")
    try:
        ids = [item["id"] for item in inbox.collect(manager, project_id) if not item.get("_dismissed")]
        assert "stale:naming.check" not in ids and "stale:core.operation" in ids  # 对象变了：提取操作过期
        client.post(batch, json={"action": "refresh_stale"})
        assert started[-1] == "core.operation"
    finally:
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")


# ---- 包声明的提交门禁：助手一轮的合入只提示（F3），问题记进变更集、卡片上说 ----


def test_commit_gate_only_hints_when_merging_an_assistant_changeset(ext_env, monkeypatch):
    _client, manager, project_id, _tmp = ext_env
    monkeypatch.setattr(changesets, "_activity_provider", None)
    root = manager.project_root(project_id)
    rel = "ext/checklist/items/借阅检查.md"
    copy_dir = changesets.copy_for_turn(manager, project_id, root)
    agent_write(copy_dir, rel, item_md("借阅检查", None), commit="agent: 检查项")
    record = changesets.observe(manager, project_id, running=False, conversation_id="default")
    merged = changesets.merge(manager, project_id, record["id"], actor="local")
    assert merged["commit"] and (root / rel).is_file()
    checks = changesets.get(manager, project_id, record["id"])["checks"]
    assert checks["errors"] == 1 and checks["findings"][0]["code"] == "level-set"
    card = changesets.round_card(manager, project_id, changesets.get(manager, project_id, record["id"]), "rc-x")
    assert [hint["tone"] for hint in card["hints"]] == ["warn"] and card["hints"][0]["text"].startswith("1 处")
    assert trailer(root, "Onto-Changeset") == record["id"]
