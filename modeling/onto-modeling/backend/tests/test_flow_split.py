"""建模拆成提取对象、提取操作，去掉分段（节点模型重构 1；详设 6.6、6.8；契约 FL-01、FL-03～FL-05、PB-01 流程关卡；
ADR-0078、ADR-0090）。

- 流程形状：资料分析 → 提取对象 → 提取操作 → 校验（装了场景模拟包，后面接「场景模拟」）→ 发布；指标是旁支；
  「规则」一步没了；`core.model` 按别名认成提取操作，没升级的包写的旧锚点、老包里的 `slice_steps` 照样能装配；
- 记录迁移（`state.migrate_split`）：F8 项目、残留 0.2 记录、带需复核标记、跳过过规则四类数据，顺序不能乱，只做一次；
- 老的执行记录、变更集、FL-03～FL-05 请求里带的分段与步骤照样能读，不报错；
- 写入范围：提取对象只能写对象，提取操作还能写规则、逻辑、指标、对象；
- 下一步：蓝图草案不用确认，资料分析做完直接往下建；目标问题没答时先推荐去回答，不拦着。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from app import config, wiring
from app.kernel.changesets import service as changesets
from app.services.flow import assemble, view
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.packs import contracts
from app.services.session_host import exec_records
from tests import proc
from tests.test_changesets import agent_write, main_write, obj
from tests.test_flow_states import env  # noqa: F401 - 共用 fixture

ASSETS = Path(config.ASSETS_DIR)
DRAFT = {
    "goal_md": "合成目标",
    "slices": [
        {"name": "申请受理", "objects": ["门店", "商品"], "operations": ["受理申请"]},
        {"name": "库存测算", "depends_on": ["申请受理"], "objects": ["库存标准"], "operations": ["测算库存"],
         "rules": ["取严规则"]},
        {"name": "审批提示", "depends_on": ["库存测算"], "operations": ["生成提示"]},
    ],
}
FULL = {"id": "all", "name": "全部"}


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))


def _core() -> tuple[str, str, dict]:
    return "core", "0.5.0", _yaml("workspace-toolkit/flow.yaml")


def op(name: str, links: str = "") -> str:
    return f"# {name}\n\n## 业务描述\n\n合成操作{links}\n"


def _blueprint(root: Path, *, confirmed: bool = True, old: bool = True, draft: dict | None = None) -> dict:
    """写蓝图进主线。old：F8 之前按段写下的三段蓝图（现在的脚本不再写多段，按老格式拼出来）；否则是现在的一段「全部」。"""
    module = contracts.default().load("blueprint")
    doc = module.from_draft(draft or DRAFT)
    if old:
        base = doc["slices"][0]
        doc["slices"] = [{**base, "id": f"s{n}", "name": item["name"], "objects": item.get("objects", []),
                          "operations": item.get("operations", []), "rules": item.get("rules", []),
                          "depends_on": [f"s{n - 1}"] if n > 1 else []}
                         for n, item in enumerate((draft or DRAFT)["slices"], start=1)]
        doc["next_ids"]["slice"] = len(doc["slices"]) + 1
        assert module.validate(doc) == [] and module.mode(doc) == "full"
    if confirmed:
        doc = module.confirm(doc)
    main_write(root, "research/blueprint.json", module.dumps(doc), "research: 合成蓝图")
    return doc


def _node(built: dict, node_id: str) -> dict:
    return next(node for node in built["nodes"] if node["id"] == node_id)


def _labels(built: dict) -> list[str]:
    return [item["label"] for item in [built["suggestions"]["primary"], *built["suggestions"]["secondary"]] if item]


# ---------------------------------------------------------------------------
# 流程形状与装配
# ---------------------------------------------------------------------------


def test_modeling_is_two_nodes_and_the_rule_step_is_gone():
    bare = assemble.assemble([_core()])
    ids = [node["id"] for node in bare["nodes"]]
    assert ids == ["core.research", "core.object", "core.operation", "core.update", "core.metric", "core.validate",
                   "core.publish"]
    by_id = {node["id"]: node for node in bare["nodes"]}
    found = by_id["core.object"]
    assert (found["title"], found["after"], found["done"], found["stale_on"]) == (
        "提取对象", "core.research", ["object"], ["research"])
    assert found["produces"] == ["ontology/object/*"] and "writes" not in found
    found = by_id["core.operation"]
    assert (found["title"], found["after"], found["done"], found["stale_on"]) == (
        "提取操作", "core.object", ["operation"], ["object"])
    assert found["produces"] == ["ontology/operation/*", "ontology/rule/*"]
    assert found["writes"] == ["ontology/logic/*", "ontology/metric/*", "ontology/object/*"]
    assert all(by_id[node_id]["after"] == "core.operation" for node_id in ("core.update", "core.metric", "core.validate"))
    assert not any(node.get("per_slice") or node.get("steps") for node in bare["nodes"])
    # 「规则」一步删了：补抽规则就是指标旁支那一条命令
    assert {name: spec["node"] for name, spec in bare["commands"].items() if spec.get("node")} == {
        "research": "core.research", "extract-object": "core.object", "extract-operation": "core.operation",
        "extract-rule-metric": "core.metric", "validate": "core.validate", "update": "core.update"}
    assert bare["aliases"] == {"core.model": "core.operation", "core.rule_metric": "core.metric",
                               "core.consistency": "core.validate"}
    assert by_id["core.publish"]["placement"] == "spine"
    assert by_id["core.metric"]["placement"] == "branch" and by_id["core.metric"]["optional"]
    with_trace = assemble.assemble([_core(), ("trace", "0.10.0", _yaml("packs/trace/flow.yaml"))])
    by_id = {node["id"]: node for node in with_trace["nodes"]}
    assert by_id["core.validate"]["title"] == "校验" and by_id["trace.trace"]["title"] == "场景模拟"
    assert {"from": "core.validate", "to": "trace.trace"} in with_trace["edges"]
    assert {"from": "trace.trace", "to": "core.publish"} in with_trace["edges"]


def test_official_packs_anchor_on_the_operation_node():
    for rel, node_id in (("packs/codegen/flow.yaml", "codegen.solver"), ("packs/domain-extend/flow.yaml",
                                                                         "domain-extend.bind")):
        declared = _yaml(rel)
        node = next(item for item in declared["nodes"] if item["id"] == node_id)
        assert node["after"] == "core.operation"
        assert "core.model" not in json.dumps(declared)


def test_packs_that_have_not_upgraded_still_assemble():
    """跟随通道升级时 core 先换（packs/runtime.follow）：这时别的包还是旧版本，写的是旧锚点、老的按段声明。"""
    old_codegen = {"nodes": [
        {"id": "codegen.solver", "title": "求解适配", "placement": "branch", "section": "optional", "optional": True,
         "after": "core.model", "display_after": ["core.model"], "done": ["solver"], "panel": True, "actions": []}],
        "commands": {"solver-evaluator": {"node": "codegen.solver"}}}
    old_extra = {"nodes": [
        {"id": "extra-review.review", "title": "合成复核", "placement": "on_demand", "section": "on_demand",
         "progress": False, "after": "core.object", "before": "core.operation", "done": []}],
        "commands": {"extra-review": {"node": "extra-review.review", "requires": []}}}
    # F8 之前的场景模拟：往建模里加「场景」一步、改校验的显示名
    old_trace = {"slice_steps": [{"node": "core.model", "step": {"id": "scenario", "label": "场景", "after": "check"}}],
                 "node_titles": {"core.validate": "校验与走查"},
                 "nodes": [{"id": "trace.trace", "title": "场景模拟", "placement": "spine", "section": "required",
                            "after": "core.validate", "done": ["testcase"], "produces": ["ontology/testcase/*"],
                            "entry": {"command": "/ontology-trace"}}],
                 "commands": {"trace": {"node": "trace.trace"}}}
    flow = assemble.assemble([_core(), ("codegen", "0.9.1", old_codegen), ("extra-review", "0.3.1", old_extra),
                              ("trace", "0.9.2", old_trace)])
    by_id = {node["id"]: node for node in flow["nodes"]}
    assert by_id["codegen.solver"]["after"] == "core.operation"
    assert by_id["codegen.solver"]["display_after"] == ["core.operation"]
    # 提取对象、提取操作都是真节点：原来插在两者之间的旧节点照样插在中间
    assert by_id["extra-review.review"]["after"] == "core.object"
    assert by_id["extra-review.review"]["before"] == "core.operation"
    # 按段的声明不再起作用：校验不改名，节点上没有步骤
    assert by_id["core.validate"]["title"] == "校验"
    assert not any(node.get("steps") for node in flow["nodes"])
    assert {"from": "trace.trace", "to": "core.publish"} in flow["edges"]


def test_flow_api_shape_for_a_new_project(env):
    _client, _workspaces, _manager, _project_id, root = env
    built = view.build(root)
    # FL-01 只加不删：分段的字段留着，恒为空
    assert built["slice_stages"] == [] and built["slices"] == [] and built["blueprint"] == {"state": "none"}
    assert all(node["per_slice"] is False and node["progress"] is None for node in built["nodes"])
    assert _node(built, "core.object")["title"] == "提取对象" and _node(built, "core.object")["state"] == "not_ready"
    assert _node(built, "core.operation")["state"] == "not_ready"
    assert built["commands"]["extract-object"] == "core.object"
    assert built["commands"]["extract-operation"] == "core.operation"
    assert built["commands"]["extract-rule-metric"] == "core.metric"
    publish = _node(built, "core.publish")
    assert publish["actions"] == [{"kind": "start", "label": "去发布", "target": "publish"}]


# ---------------------------------------------------------------------------
# 记录迁移
# ---------------------------------------------------------------------------


def _flow() -> dict:
    return assemble.assemble([_core()])


def _review(source: str = "autobuild") -> dict:
    return {"source": source, "at": 1.0, "run_id": "run-0001" if source == "autobuild" else None,
            "slice_id": "all" if source == "autobuild" else None, "note": ""}


def test_migrate_an_f8_project():
    flow = _flow()
    stored = {
        "nodes": {"core.research": {"fp": "", "sig": "r", "at": 1.0},
                  "core.model": {"fp": "a" * 16, "sig": "model-sig", "at": 2.0, "kinds": {"research": "a" * 16}},
                  "core.validate": {"fp": "c" * 16, "sig": "v", "at": 3.0}},
        "skipped": {},
        "slices": {"all": {"object": {"fp": "a" * 16, "kinds": {"research": "a" * 16}, "at": 2.0, "merged": True},
                           "operation": {"fp": "b" * 16, "kinds": {"object": "b" * 16}, "at": 2.5, "merged": True},
                           "rule": {"fp": "d" * 16, "kinds": {}, "at": 2.6}}},
        "slice_skips": {"all": {}},
        "legacy_migrated": True,
    }
    assert node_state.migrate_split(stored, flow) is True
    by_id = {node["id"]: node for node in flow["nodes"]}
    assert stored["nodes"]["core.object"] == {"fp": "a" * 16, "kinds": {"research": "a" * 16}, "at": 2.0,
                                              "sig": node_state.done_signature(flow, by_id["core.object"])}
    assert stored["nodes"]["core.operation"] == {"fp": "b" * 16, "kinds": {"object": "b" * 16}, "at": 2.5,
                                                 "sig": node_state.done_signature(flow, by_id["core.operation"])}
    # 别的节点不动；建模、分段的记录都去掉，记下已经迁过
    assert stored["nodes"]["core.research"] == {"fp": "", "sig": "r", "at": 1.0}
    assert stored["nodes"]["core.validate"] == {"fp": "c" * 16, "sig": "v", "at": 3.0}
    assert "core.model" not in stored["nodes"] and stored["skipped"] == {}
    assert not {"slices", "slice_skips", "legacy_migrated"} & set(stored) and stored["split_migrated"] is True
    # 只做一次：迁过之后新记的提取对象不会被当成 0.2 的残留删掉
    stored["nodes"]["core.object"] = {"fp": "e" * 16, "sig": "s", "at": 9.0}
    assert node_state.migrate_split(stored, flow) is False
    assert stored["nodes"]["core.object"]["fp"] == "e" * 16


def test_migrate_drops_leftover_0_2_records_first():
    """0.2 的对象提取、操作提取记录在 0.3 起并进建模后没删：先删掉，再从分段、建模的记录搬，残留的不会被当成新节点的。"""
    flow = _flow()
    leftover = {"core.object": {"fp": "1" * 16, "sig": "legacy", "at": 0.5},
                "core.operation": {"fp": "2" * 16, "sig": "legacy", "at": 0.5}}
    stored = {"nodes": {**leftover, "core.model": {"fp": "a" * 16, "sig": "m", "at": 2.0}}, "skipped": {},
              "slices": {"all": {"object": {"fp": "b" * 16, "at": 2.0}}}, "legacy_migrated": True}
    node_state.migrate_split(stored, flow)
    # 提取对象取分段里「对象」那一步的（不取残留，也不取建模的）；提取操作没有「操作」那一步的记录：残留删了，不搬
    assert stored["nodes"]["core.object"]["fp"] == "b" * 16
    assert "core.operation" not in stored["nodes"]
    # 分段里没有对象这一步：取建模的
    stored = {"nodes": {**leftover, "core.model": {"fp": "a" * 16, "sig": "m", "at": 2.0}}, "skipped": {}}
    node_state.migrate_split(stored, flow)
    assert stored["nodes"]["core.object"]["fp"] == "a" * 16 and "core.operation" not in stored["nodes"]
    # 停在 0.2、从没按分段算过的项目：残留删掉，按主线现状重记基线
    stored = {"nodes": dict(leftover), "skipped": {}}
    node_state.migrate_split(stored, flow)
    assert stored["nodes"] == {} and stored["split_migrated"] is True


def test_migrate_keeps_needs_review_but_not_the_old_signature():
    flow = _flow()
    by_id = {node["id"]: node for node in flow["nodes"]}
    stored = {"nodes": {}, "skipped": {},
              "slices": {"all": {"object": {"fp": "a" * 16, "at": 2.0, "needs_review": _review()},
                                 "operation": {"fp": "b" * 16, "at": 2.0, "kinds": {"object": "b" * 16},
                                               "needs_review": _review()}}}}
    node_state.migrate_split(stored, flow)
    assert stored["nodes"]["core.object"]["needs_review"] == _review()
    assert stored["nodes"]["core.operation"]["needs_review"] == _review()
    # 没有分段记录、建模节点升级后待复核：提取对象带上标记；判定签名按新节点记，不会再误标一次「方法升级」
    stored = {"nodes": {"core.model": {"fp": "a" * 16, "sig": "model-sig", "at": 2.0,
                                       "needs_review": _review("upgrade")}}, "skipped": {}}
    node_state.migrate_split(stored, flow)
    record = stored["nodes"]["core.object"]
    assert record["needs_review"] == _review("upgrade")
    assert record["sig"] == node_state.done_signature(flow, by_id["core.object"]) != "model-sig"
    # 只有需复核、没有基线的记录不搬：按主线现状重记
    stored = {"nodes": {"core.model": {"fp": None, "sig": None, "at": 2.0, "needs_review": _review()}}, "skipped": {}}
    node_state.migrate_split(stored, flow)
    assert "core.object" not in stored["nodes"]


def test_migrate_turns_a_skipped_rule_step_into_a_skipped_metric():
    flow = _flow()
    skip = {"at": 1.0, "reason": "资料里没有规则"}
    stored = {"nodes": {}, "skipped": {}, "slice_skips": {"all": {"rule": skip}}}
    node_state.migrate_split(stored, flow)
    assert stored["skipped"] == {"core.metric": skip} and "slice_skips" not in stored
    # 指标本来就跳过了：留原来那条
    own = {"at": 2.0, "reason": "不做指标"}
    stored = {"nodes": {}, "skipped": {"core.metric": own}, "slice_skips": {"all": {"rule": skip}}}
    node_state.migrate_split(stored, flow)
    assert stored["skipped"] == {"core.metric": own}


def test_migrate_waits_for_the_split_flow():
    """项目锁定旧 core（流程里还有 core.model）：记录照旧，等它升级到拆开的流程再迁。"""
    old = _flow()
    old["nodes"] = [*old["nodes"], {"id": "core.model", "title": "建模", "done": ["object", "operation"]}]
    stored = {"nodes": {"core.model": {"fp": "a" * 16, "sig": "m", "at": 2.0}}, "skipped": {},
              "slices": {"all": {"object": {"fp": "a" * 16, "at": 2.0}}}}
    before = json.loads(json.dumps(stored))
    assert node_state.migrate_split(stored, old) is False and stored == before


def _old_records(root: Path, *, object_review: bool = True) -> dict:
    """F8 项目在主线上的样子：对象、操作都有，记录是建模节点与「全部」那一段的；操作一步确认之后对象又改过。"""
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    research = node_state.fingerprint(root, ["research"])
    with node_state._lock:
        stored = node_state.load(root)
        stored["nodes"] = {"core.model": {"fp": research, "sig": "model-sig", "at": 1.0},
                           "core.object": {"fp": "0" * 16, "sig": "legacy", "at": 0.5}}
        stored["slices"] = {"all": {
            "object": {"fp": research, "kinds": {"research": research}, "at": 1.0, "merged": True,
                       **({"needs_review": _review()} if object_review else {})},
            "operation": {"fp": "0" * 16, "kinds": {"object": "0" * 16}, "at": 1.0, "merged": True}}}
        stored["slice_skips"] = {"all": {"rule": {"at": 1.0, "reason": "资料里没有规则"}}}
        stored["legacy_migrated"] = True
        node_state.save(root, stored)
    return stored


def test_an_old_project_shows_the_same_progress_after_the_split(env):
    _client, _workspaces, _manager, _project_id, root = env
    _old_records(root)
    built = view.build(root)
    found = _node(built, "core.object")
    # 对象一步是自主构建合入的：提取对象已确认、显示自主完成；操作一步原来就过期：提取操作仍过期
    assert found["state"] == "confirmed" and found["display_state"] == "auto"
    assert _node(built, "core.operation")["state"] == "stale"
    assert _node(built, "core.metric")["state"] == "skipped"
    assert built["progress"]["done"] >= 3  # 资料分析、提取对象、提取操作都还算做过（过期也算），进度不往回退
    stored = node_state.load(root)
    assert stored["split_migrated"] is True and "core.model" not in stored["nodes"]
    assert not {"slices", "slice_skips", "legacy_migrated"} & set(stored)
    # 刷新之后（这一轮合入）回到已确认
    node_state.confirm(root, flow_model.load_for(root), ["core.operation"])
    assert _node(view.build(root), "core.operation")["state"] == "confirmed"


def test_a_merge_before_the_first_compute_is_not_taken_for_a_leftover(env):
    """升级后第一次算状态之前就合入了一轮：先迁旧记录再记这一轮，迁移不会把这一轮记的当成 0.2 的残留删掉。"""
    _client, _workspaces, _manager, _project_id, root = env
    _old_records(root, object_review=False)
    flow = flow_model.load_for(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店（改了口径）"))
    node_state.confirm(root, flow, ["core.operation"])
    stored = node_state.load(root)
    assert stored["split_migrated"] is True
    assert stored["nodes"]["core.operation"]["fp"] == node_state.fingerprint(root, ["object"])
    built = view.build(root)
    assert _node(built, "core.operation")["state"] == "confirmed" and _node(built, "core.object")["state"] == "confirmed"


def test_review_marks_written_before_the_first_compute_survive_the_migration(env):
    """升级后第一次算状态之前就给新节点标了需复核（导入、自主构建合入）：先迁旧记录再标，迁移不会把标记删掉。"""
    _client, _workspaces, _manager, _project_id, root = env
    _old_records(root, object_review=False)
    node_state.mark_needs_review(root, ["core.object"], "import", note="合成")
    assert node_state.load(root)["split_migrated"] is True
    found = _node(view.build(root), "core.object")
    assert found["state"] == "confirmed" and found["flags"]["review_source"] == "import"


# ---------------------------------------------------------------------------
# 老的执行记录、变更集、请求
# ---------------------------------------------------------------------------


def test_exec_records_land_on_the_new_nodes(env):
    _client, _workspaces, _manager, project_id, root = env
    _blueprint(root)
    flow = flow_model.load_for(root)

    def start(text):
        command = text.split()[0]
        return exec_records.start(project_id, "default", root=root, flow=flow, command=command,
                                  command_name=command[len("/ontology-"):], text=text)

    found = start("/ontology-extract-object")
    assert (found["node"]["id"], found["command_label"]) == ("core.object", "提取对象")
    found = start("/ontology-extract-operation slice=s2")  # F8 之前的写法：分段参数不再起作用
    assert (found["node"]["id"], found["command_label"]) == ("core.operation", "提取操作")
    found = start("/ontology-extract-rule-metric slice=s2")  # 原来是建模的「规则」一步，现在就是指标
    # 执行记录不再记分段与步骤（老记录照样按 node_of 认，见下一条）；视图里的 slice 恒为 null
    assert found["node"]["id"] == "core.metric" and not {"slice", "step"} & set(found)
    assert exec_records.view(found)["slice"] is None


def test_old_exec_records_are_read_as_the_new_nodes(env):
    _client, _workspaces, _manager, project_id, root = env
    flow = flow_model.load_for(root)

    def old(**fields) -> dict:
        return {"node": {"id": "core.model", "version": "core@0.5.0", "title": "建模"}, **fields}

    assert exec_records.node_of(old(slice=FULL, step="object"), flow) == "core.object"
    assert exec_records.node_of(old(slice=FULL, step="operation"), flow) == "core.operation"
    assert exec_records.node_of(old(slice={"id": "s2", "name": "库存测算"}, step="rule"), flow) == "core.metric"
    assert exec_records.node_of(old(command_name="extract-object"), flow) == "core.object"
    assert exec_records.node_of(old(), flow) == "core.operation"  # 认不出时按别名
    assert exec_records.node_of({"node": {"id": "core.validate"}}, flow) == "core.validate"
    locked = {"nodes": [{"id": "core.model"}]}  # 锁定旧 core：原样
    assert exec_records.node_of(old(step="object"), locked) == "core.model"
    # 升级前开始、还没结束的建模执行：算在它那一步的节点上
    record = exec_records.start(project_id, "default", root=root, flow=flow, command="/ontology-extract-object",
                                command_name="extract-object")
    exec_records.update(project_id, record["id"], node=old()["node"], slice={"id": "s1", "name": "申请受理"},
                        step="object")
    built = view.build(root)
    assert _node(built, "core.object")["state"] == "running" and _node(built, "core.operation")["state"] != "running"
    # 中断后续跑前的检查按新节点算：core 升级了，按新版从头做
    exec_records.interrupt(project_id, record["id"], "restart", root=root, flow=flow)
    assert exec_records.not_resumable_reason(exec_records.get(project_id, record["id"]), root, flow) == "node_upgraded"


def test_old_changesets_confirm_the_steps_they_did(env):
    _client, _workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    view.build(root)  # 先按新流程算一次（迁移）
    with node_state._lock:
        stored = node_state.load(root)
        stored["nodes"] = {}
        node_state.save(root, stored)
    wiring._confirm_nodes(project_id, ["core.model"], {"nodes": ["core.model"], "slice_steps": [["all", "object"]]})
    assert set(node_state.load(root)["nodes"]) == {"core.object"}
    wiring._confirm_nodes(project_id, ["core.model", "core.validate"], {"nodes": ["core.model", "core.validate"]})
    assert set(node_state.load(root)["nodes"]) == {"core.object", "core.operation", "core.validate"}
    # 自主构建合入的老变更集：两个节点都标需复核
    wiring._confirm_nodes(project_id, ["core.model"], {"nodes": ["core.model"], "needs_review": True,
                                                       "run_id": "run-0001", "slice_id": "all", "purpose": "合成"})
    built = view.build(root)
    assert _node(built, "core.object")["display_state"] == "auto"
    assert _node(built, "core.operation")["display_state"] == "auto"


def test_skip_restore_and_start_ignore_slice_fields_and_take_old_node_ids(env):
    client, _workspaces, _manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/flow/nodes"
    # core.model 按别名认成提取操作：必经，不能跳过
    refused = client.post(f"{base}/core.model/skip", json={"slice_id": "all", "step": "rule"})
    assert refused.status_code == 409 and refused.json()["code"] == "node_not_skippable"
    assert client.post(f"{base}/core.nope/skip", json={}).status_code == 404
    # 请求里带的分段与步骤忽略，不报错
    skipped = client.post(f"{base}/core.metric/skip", json={"slice_id": "all", "step": "rule", "reason": "不做"})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["node"]["id"] == "core.metric" and skipped.json()["node"]["state"] == "skipped"
    assert node_state.skipped(root) == {"core.metric": {"at": node_state.skipped(root)["core.metric"]["at"],
                                                        "reason": "不做"}}
    assert client.post(f"{base}/core.metric/restore", json={"slice_id": "all"}).status_code == 200
    assert client.post(f"{base}/core.metric/restore", json={"slice_id": "all"}).json()["code"] == "node_not_skipped"
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    started = client.post(f"{base}/core.model/start", json={"slice_id": "s2", "step": "rule",
                                                             "origin": {"page": "wb-l2", "label": "开始"}})
    assert started.status_code == 202, started.text
    from tests.test_session_host import FakeEngine
    import time

    for _ in range(200):
        if FakeEngine.sent:
            break
        time.sleep(0.01)
    assert FakeEngine.sent[0].startswith("/ontology-extract-operation") and "slice=" not in FakeEngine.sent[0]


def test_new_entries_are_not_assigned_to_slices_at_turn_end(env):
    """F8 之前一轮结束时把新条目记进这一段；现在不分段，老蓝图、带老分段的执行记录也不再改蓝图、不记分段步骤。"""
    _client, workspaces, _manager, project_id, root = env
    _blueprint(root)
    before = (root / "research" / "blueprint.json").read_text(encoding="utf-8")
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/补货台账.md", obj("补货台账", "合成"), commit="agent: 新对象")
    record = changesets.observe(workspaces, project_id, running=False, conversation_id="default",
                                exec_record={"id": "ex-1", "command_label": "建模",
                                             "node": {"id": "core.model", "version": "core@0.3.0"},
                                             "slice": {"id": "s2", "name": "库存测算"}, "step": "object"})
    assert (copy / "research" / "blueprint.json").read_text(encoding="utf-8") == before
    assert "research/blueprint.json" not in record["paths"] and "slice_steps" not in record


# ---------------------------------------------------------------------------
# 写入范围
# ---------------------------------------------------------------------------


def _running(record: dict):
    return lambda pid: {"running": True, "conversation_id": "default", "exec": record, "affected": []}


def test_scope_of_each_extraction_node(env, monkeypatch):
    _client, workspaces, _manager, project_id, _root = env
    object_only = ["ontology/object/*"]
    with_rules = ["ontology/operation/*", "ontology/rule/*", "ontology/logic/*", "ontology/metric/*",
                  "ontology/object/*"]
    for node_id, wanted in (("core.object", object_only), ("core.operation", with_rules)):
        record = {"id": "ex-1", "node": {"id": node_id, "version": "core@0.5.0", "title": "合成"}, "started_at": 1.0}
        monkeypatch.setattr(changesets, "_activity_provider", _running(record))
        lock = changesets.scope_lock(workspaces, project_id)
        assert lock["state"] == "assistant_running" and lock["slice"] is None and lock["paths"] == wanted
    # 老的执行记录（建模、带分段）：按别名认成提取操作，锁住原来建模的整个范围，不再收窄到一段
    old = {"id": "ex-1", "node": {"id": "core.model", "version": "core@0.3.0", "title": "建模"},
           "slice": {"id": "s1", "name": "订单受理"}, "step": "object", "started_at": 1.0}
    monkeypatch.setattr(changesets, "_activity_provider", _running(old))
    lock = changesets.scope_lock(workspaces, project_id)
    assert lock["paths"] == with_rules and lock["slice"] is None and "这一段" not in lock["reason_text"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="需要 bash")
def test_guard_keeps_extract_object_to_objects_and_ignores_old_protect(env):
    _client, workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/客户.md", obj("客户", "合成客户"))
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]

    def guard(rel: str) -> bool:
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(copy / rel), "content": "x"}}
        done = proc.run(["bash", str(copy / ".claude" / "scripts" / "ontology_scope_guard.sh")],
                        input=json.dumps(payload, ensure_ascii=False), cwd=copy, capture_output=True,
                        text=True, timeout=30, env={**os.environ, "CLAUDE_PROJECT_DIR": str(copy)})
        assert done.returncode == 0, done.stderr
        output = done.stdout.strip()
        return bool(output) and json.loads(output.splitlines()[-1]).get("hookSpecificOutput", {}).get(
            "permissionDecision") == "deny"

    proc.run([*helper, "acquire", "extract-object", "objects"], cwd=copy, check=True, capture_output=True)
    active = copy / ".onto" / "run" / "active.json"
    spec = json.loads(active.read_text(encoding="utf-8"))["commands"]["extract-object"]
    assert spec["node"] == "core.object" and spec["allow"] == ["ontology/object/*"] and "protect" not in spec
    assert not guard("ontology/object/订单.md") and guard("ontology/operation/受理订单.md")
    assert guard("ontology/rule/取严规则.md")
    # 分段时代的 state_helper 记过 protect（段外已有的条目）：守卫不再看
    data = json.loads(active.read_text(encoding="utf-8"))
    data["commands"]["extract-object"]["protect"] = ["ontology/object/客户.md"]
    active.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert not guard("ontology/object/客户.md")
    proc.run([*helper, "release", "extract-object"], cwd=copy, check=True, capture_output=True)
    proc.run([*helper, "acquire", "extract-operation", "operation"], cwd=copy, check=True, capture_output=True)
    for rel in ("ontology/operation/受理订单.md", "ontology/rule/取严规则.md", "ontology/metric/达标率.md",
                "ontology/object/客户.md", "ontology/logic/旧逻辑.md"):
        assert not guard(rel), rel
    assert guard("ontology/testcase/受理订单.md")
    proc.run([*helper, "release", "extract-operation"], cwd=copy, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# 下一步与发布关卡
# ---------------------------------------------------------------------------


def test_draft_blueprint_does_not_stop_after_research(env):
    """F8：蓝图草案不用确认，资料分析做完就是做完；目标问题没答时主推去回答，后面跟主线下一步。"""
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    _blueprint(root, confirmed=False, old=False)
    built = view.build(root)
    research = _node(built, "core.research")
    assert research["state"] == "confirmed" and built["blueprint"] == {"state": "draft"}
    every = [built["suggestions"]["primary"], *built["suggestions"]["secondary"]]
    assert every[0]["action"]["command"] == "/ontology-extract-operation" and every[0]["label"] == "提取操作"
    assert not {"确认蓝图", "调整分段", "重新规划分段", "规划分段"} & {item["label"] for item in every}
    goal = {"kind": "goal", "title": "资料讲的是门店盘点，和目标对不上，以哪个为准？", "background": "合成",
            "suggested_goal": "合成目标：按盘点结果生成补货单"}
    _blueprint(root, confirmed=False, old=False, draft={**DRAFT, "questions": [goal]})
    built = view.build(root)
    primary, secondary = built["suggestions"]["primary"], built["suggestions"]["secondary"]
    assert primary["id"] == "answer_goal" and primary["action"]["params"] == {"dialog": "blueprint"}
    assert secondary[0]["action"]["command"] == "/ontology-extract-operation"
    assert _node(built, "core.research")["state"] == "confirmed"


def test_sliced_blueprint_never_gates_publish(env):
    """F8：按段建过的项目（老蓝图）也没有流程关卡。"""
    client, _workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    _blueprint(root)
    check = client.get(f"/api/v1/projects/{project_id}/publish/check").json()
    assert not [gate for gate in check["gates"] if gate["source"]["kind"] == "flow"]
    assert _node(view.build(root), "core.publish")["state"] != "blocked"


def test_project_card_and_autobuild_preview_with_an_old_sliced_blueprint(env):
    """老的三段蓝图：项目卡片不显示分段；自主构建按节点排，不看蓝图里的段（一个节点一行，状态取各自的节点）。"""
    client, _workspaces, _manager, project_id, root = env
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] is None
    _blueprint(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] is None
    preview = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/preview",
                          json={"policy_overrides": {"scope": {"until_node": "core.operation"}}})
    assert preview.status_code == 200, preview.text
    assert [(row["id"], row["current"], row["included"]) for row in preview.json()["slices"]] == [
        ("core.object", "已确认", False), ("core.operation", "未开始", True)]


# ---------------------------------------------------------------------------
# 规则与指标提取补回链（端到端实测）：建模不往回退，不推刚跑完的同一条命令
# ---------------------------------------------------------------------------


def _modeled_with_rules(root: Path) -> dict:
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    main_write(root, "ontology/rule/取严规则.md", "# 取严规则\n\n约束[[受理申请]]\n")
    flow = flow_model.load_for(root)
    node_state.confirm(root, flow, ["core.research", "core.object", "core.operation", "core.metric", "core.validate"])
    return flow


def test_a_backlink_in_an_operation_does_not_roll_back_modeling(env):
    """规则与指标提取在操作里补了一处回链：提取操作的上游是对象，不算过期；建议条不推重新提取操作或补抽规则。"""
    _client, _workspaces, _manager, _project_id, root = env
    _modeled_with_rules(root)
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，按[[取严规则]]校验"))
    built = view.build(root)
    assert _node(built, "core.object")["state"] == "confirmed" and _node(built, "core.operation")["state"] == "confirmed"
    labels = _labels(built)
    assert not any(label.startswith("刷新") or "有变化，刷新提取" in label for label in labels)
    assert not any(label.endswith("刷新指标") for label in labels)


def _validated(root: Path) -> None:
    """建模、指标、校验都做完：在 _modeled_with_rules 上补指标和校验报告，重记基线。"""
    flow = _modeled_with_rules(root)
    main_write(root, "ontology/metric/达标率.md", "# 达标率\n\n合成指标\n")
    main_write(root, "reports/20260101-rule.md", "# 合成检查报告\n\n- [x] 已处理\n")
    node_state.confirm(root, flow, ["core.metric", "core.validate"])


def test_stale_label_names_what_changed_not_every_input(env):
    """过期的按钮写确认之后真变了的那几类：只改了指标，校验写「指标有变化」，不把对象、操作、规则、指标全列出来；
    节点上的刷新按钮同一个说法。"""
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/metric/达标率.md", "# 达标率\n\n合成指标，改了口径\n")
    built = view.build(root)
    assert _node(built, "core.validate")["state"] == "stale"
    assert "指标有变化，刷新校验" in _labels(built)
    assert _node(built, "core.validate")["actions"][0]["label"] == "指标有变化，刷新校验"
    assert built["suggestions"]["note"] is None


def test_optional_metric_going_stale_is_left_to_validation(env):
    """指标（可选旁支）过期时交给后面的校验，不单独推：单独推会和建模来回兜圈子——指标补对象的关联让提取操作过期，
    重抽操作又让指标过期。必经的刷新排在前面。"""
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，补一句"))
    built = view.build(root)
    assert _node(built, "core.metric")["state"] == "stale"
    labels = _labels(built)
    assert labels[0] == "操作有变化，刷新校验"
    assert not any(label.endswith("刷新指标") for label in labels)


def test_an_object_change_pushes_the_operation_refresh_until_validation_has_checked_it(env):
    """对象在主线上改了：提取操作过期（上游是对象），校验还没核对过时推刷新提取操作；校验在这之后确认过就交给它。"""
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店（改了口径）"))
    built = view.build(root)
    assert _node(built, "core.operation")["state"] == "stale" and _node(built, "core.object")["state"] == "confirmed"
    assert _labels(built)[0] == "对象有变化，刷新提取操作"
    node_state.confirm(root, flow_model.load_for(root), ["core.validate"])
    assert "对象有变化，刷新提取操作" not in _labels(view.build(root))


def test_old_records_find_what_changed_from_git(env):
    """改动前留下的确认记录没存分类指纹：按确认那一刻主线上的提交比到现在，只改了操作就写「操作有变化」，不把四类全列出来。"""
    import time

    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    head_time = int(proc.run(["git", "log", "-1", "--format=%ct"], cwd=root, capture_output=True, text=True).stdout)
    stored = node_state.load(root)
    for record in stored["nodes"].values():
        record.pop("kinds", None)
        record["at"] = head_time
    node_state.save(root, stored)
    time.sleep(1.1)  # 下一个提交落在确认之后的那一秒
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，补一句"))
    assert _labels(view.build(root))[0] == "操作有变化，刷新校验"
