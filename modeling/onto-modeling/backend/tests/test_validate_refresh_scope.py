"""校验过期后只查改动条目（flow.yaml 节点字段 `refresh_scope: changed_entries`；state.refresh_scope）。

- 确认记录记下主线提交 `rev`；老记录没有 rev 时按确认时刻沿主线找；
- 校验确认之后改了对象、操作：建议条第 7 条、节点卡片的刷新、待处理的刷新、FL-05 都发
  `/ontology-validate <条目名…> --since <确认时提交前 12 位>`，「运行质量检查」照旧全量；
- 删了条目、改了 ontology/logic/、只改了 index.md：照旧发整条命令；改了又改回去：回到已确认，不推刷新；
- 没声明刷新范围的节点（指标）刷新命令不变；节点升级待复核、待处理里「重做这一步」整条重做。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.services.flow import model as flow_model
from app.services.flow import slices, view
from app.services.flow import state as node_state
from tests import proc
from tests.test_changesets import git, main_write, obj
from tests.test_flow_states import env  # noqa: F401 - 共用 fixture
from tests.test_inbox import idle
from tests.test_session_host import FakeEngine

VALIDATE = "/ontology-validate"


def op(name: str, links: str = "") -> str:
    return f"# {name}\n\n## 业务描述\n\n合成操作{links}\n"


def _validated(root: Path) -> str:
    """建模、指标、校验都做完并确认，返回确认时的主线提交。"""
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/object/仓库.md", obj("仓库", "合成仓库"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    main_write(root, "ontology/rule/取严规则.md", "# 取严规则\n\n约束[[受理申请]]\n")
    main_write(root, "ontology/metric/达标率.md", "# 达标率\n\n合成指标\n")
    main_write(root, "reports/20260101-rule.md", "# 合成检查报告\n\n- [x] 已处理\n")
    flow = flow_model.load_for(root)
    node_state.confirm(root, flow, ["core.research", "core.model", "core.metric", "core.validate"])
    slices.confirm(root, flow, [["all", "object"], ["all", "operation"], ["all", "rule"]], doc=None)
    return git(root, "rev-parse", "HEAD").strip()


def _delete(root: Path, rel: str) -> None:
    (root / rel).unlink()
    git(root, "add", "-A")
    git(root, "commit", "-qm", "web: 合成删除")


def _node(built: dict, node_id: str) -> dict:
    return next(node for node in built["nodes"] if node["id"] == node_id)


def _flow_node(root: Path, node_id: str) -> dict:
    return next(node for node in flow_model.load_for(root)["nodes"] if node["id"] == node_id)


def _suggestions(built: dict) -> dict:
    items = [built["suggestions"]["primary"], *built["suggestions"]["secondary"]]
    return {item["id"]: item for item in items if item}


def _refresh_action(built: dict, node_id: str) -> dict | None:
    return next((action for action in _node(built, node_id)["actions"] if action["kind"] == "refresh"), None)


def _sent_after(count: int) -> str:
    for _ in range(500):
        if len(FakeEngine.sent) > count:
            return FakeEngine.sent[count]
        time.sleep(0.01)
    raise AssertionError("命令没有发到对话")


def _frontend_body(text: str) -> dict:
    """前端 suggestionCommand / commandFromTarget：第一个词是命令，`键=值` 进 args，其余词拼成 text。"""
    words = text.split()
    return {"command": words[0], "text": " ".join(word for word in words[1:] if "=" not in word),
            "origin": {"page": "flow", "label": "刷新校验"}, "conversation_id": "-"}


def test_confirm_and_first_baseline_record_the_mainline_revision(env):
    _client, _workspaces, _manager, _project_id, root = env
    head = _validated(root)
    assert node_state.load(root)["nodes"]["core.validate"]["rev"] == head
    stored = node_state.load(root)
    stored["nodes"].pop("core.validate")
    node_state.save(root, stored)
    view.build(root)  # 第一次看到已满足完成判定的节点：记成基线，同样带主线提交
    record = node_state.load(root)["nodes"]["core.validate"]
    assert record["rev"] == head and _node(view.build(root), "core.validate")["state"] == "confirmed"


def test_stale_validate_refreshes_only_changed_entries_everywhere(env):
    client, _workspaces, manager, project_id, root = env
    base = _validated(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，补了一句"))
    main_write(root, "ontology/operation/补货.md", op("补货", "[[仓库]]"))
    names = sorted(["门店", "补货"])
    expected = f"{VALIDATE} {' '.join(names)} --since {base[:12]}"
    scope = node_state.refresh_scope(root, _flow_node(root, "core.validate"))
    assert scope == {"since": base, "names": names, "command": expected}

    built = view.build(root)
    assert _node(built, "core.validate")["state"] == "stale"
    # 节点卡片：刷新只查改动条目，「运行质量检查」照旧全量
    actions = _node(built, "core.validate")["actions"]
    assert actions[0]["kind"] == "refresh" and actions[0]["target"] == expected
    assert [action["target"] for action in actions if action["label"] == "运行质量检查"] == [VALIDATE]
    # 建议条第 7 条与写给助手的 flow-state.json（state_helper suggest）同一条命令
    item = _suggestions(built)["refresh:core.validate"]
    assert item["action"] == {"type": "send", "text": expected, "command": VALIDATE}
    copy_state = root / view.FLOW_STATE_REL
    from app.kernel.changesets import worktree

    if (worktree.copy_path(root) / ".git").is_file():
        copy_state = worktree.copy_path(root) / view.FLOW_STATE_REL
    written = json.loads(copy_state.read_text(encoding="utf-8"))
    assert written["context"]["stale_scope"]["core.validate"]["command"] == expected

    # FL-05：一行命令；前端按建议条发 S-20：命令名后另起一段的就是参数，两种引擎都按第一个空白切开
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/core.validate/start", json={})
    assert started.status_code == 202, started.text
    assert _sent_after(0) == expected
    idle(manager, project_id)
    sent = client.post(f"/api/v1/projects/{project_id}/commands", json=_frontend_body(item["action"]["text"]))
    assert sent.status_code == 202, sent.text
    assert _sent_after(1) == f"{VALIDATE}\n\n{' '.join(names)} --since {base[:12]}"
    idle(manager, project_id)


@pytest.mark.parametrize("change", ["delete", "logic", "index"])
def test_unscoped_changes_fall_back_to_the_full_command(env, change):
    client, _workspaces, manager, project_id, root = env
    _validated(root)
    if change == "delete":
        main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，补了一句"))
        _delete(root, "ontology/object/仓库.md")
    elif change == "logic":
        main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，补了一句"))
        main_write(root, "ontology/logic/旧操作.md", op("旧操作"))
    else:
        main_write(root, "ontology/object/Index.md", "# 对象索引\n\n- [[门店]]\n")
    assert node_state.refresh_scope(root, _flow_node(root, "core.validate")) is None
    built = view.build(root)
    assert _node(built, "core.validate")["state"] == "stale"
    assert _refresh_action(built, "core.validate")["target"] == VALIDATE
    assert _suggestions(built)["refresh:core.validate"]["action"]["text"] == VALIDATE
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/core.validate/start", json={})
    assert started.status_code == 202, started.text
    assert _sent_after(0) == VALIDATE
    idle(manager, project_id)


def test_old_record_without_rev_finds_its_base_by_time(env):
    _client, _workspaces, _manager, _project_id, root = env
    base = _validated(root)
    head_time = int(proc.run(["git", "log", "-1", "--format=%ct"], cwd=root, capture_output=True, text=True).stdout)
    stored = node_state.load(root)
    record = stored["nodes"]["core.validate"]
    record.pop("rev")
    record["at"] = head_time
    node_state.save(root, stored)
    time.sleep(1.1)  # 下一个提交落在确认之后的那一秒
    main_write(root, "ontology/rule/取严规则.md", "# 取严规则\n\n约束[[受理申请]]，补一句\n")
    expected = f"{VALIDATE} 取严规则 --since {base[:12]}"
    assert node_state.refresh_scope(root, _flow_node(root, "core.validate"))["since"] == base
    assert _refresh_action(view.build(root), "core.validate")["target"] == expected


def test_change_then_revert_returns_to_confirmed_without_refresh(env):
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，补了一句"))
    assert _node(view.build(root), "core.validate")["state"] == "stale"
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    built = view.build(root)
    assert _node(built, "core.validate")["state"] == "confirmed"
    assert _refresh_action(built, "core.validate") is None
    assert not [key for key in _suggestions(built) if key.startswith("refresh:")]


def test_nodes_without_refresh_scope_keep_their_refresh_command(env):
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    # 指标只在对象、操作有增减时过期（ADR-0102）：新增一个对象
    main_write(root, "ontology/object/商品.md", obj("商品", "合成商品"))
    built = view.build(root)
    assert _node(built, "core.metric")["state"] == "stale"
    assert _refresh_action(built, "core.metric")["target"] == "/ontology-extract-rule-metric"
    assert node_state.refresh_scope(root, _flow_node(root, "core.metric")) is None
    assert _refresh_action(built, "core.validate")["target"].startswith(f"{VALIDATE} 商品 --since ")


def test_upgrade_review_and_redo_run_the_full_check(env):
    """节点升级（判定变了）待复核：之前查过的也要按新判定重看，刷新整条；导入待复核时刷新仍只查改动，
    待处理里「重做这一步」整步重做。"""
    client, workspaces, manager, project_id, root = env
    base = _validated(root)
    node_state.mark_needs_review(root, ["core.validate"], "import", note="合成导入")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店，补了一句"))
    expected = f"{VALIDATE} 门店 --since {base[:12]}"
    assert _refresh_action(view.build(root), "core.validate")["target"] == expected
    inbox_base = f"/api/v1/projects/{project_id}/inbox"
    refreshed = client.post(f"{inbox_base}/stale:core.validate/actions", json={"action": "refresh"})
    assert refreshed.status_code == 200, refreshed.text
    assert _sent_after(0) == expected
    idle(manager, project_id)
    redone = client.post(f"{inbox_base}/review:core.validate/actions", json={"action": "rejudge"})
    assert redone.status_code == 200, redone.text
    assert _sent_after(1) == VALIDATE
    idle(manager, project_id)
    node_state.mark_needs_review(root, ["core.validate"], "upgrade")
    assert node_state.refresh_scope(root, _flow_node(root, "core.validate")) is None
    assert _refresh_action(view.build(root), "core.validate")["target"] == VALIDATE
