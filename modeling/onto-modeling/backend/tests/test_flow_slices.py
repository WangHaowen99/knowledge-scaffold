"""建模节点的状态与主流程（详设 6.6、6.8；契约 FL-01 slices / slice_stages、FL-03～FL-05、PB-01 流程关卡；
ADR-0051、ADR-0078）。

F8（ADR-0078）起不再分段：建模节点只有一段「全部」，命令一次做完整个项目（与插件化之前一致）；F8 之前按段建的项目
（蓝图里存着多段）照样按全量算。
- 流程形状：资料分析 → 建模 → 校验（装了场景模拟包，后面接「场景模拟」）→ 发布；规则是建模里可选的补抽，
  场景模拟不再往建模里加「场景」一步；没升级的包写的旧锚点照样认；
- 「全部」的状态：进行中、待确认、已确认、过期、跳过规则；0.2 的已确认状态映射过来，进度不往回退；
- 执行记录一律记在「全部」（老命令带的 slice= 也一样）；一轮结束不再把新条目归到某一段；
- 下一步：蓝图草案不用确认，资料分析做完直接往下建；目标问题没答时先推荐去回答，不拦着。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from app import config
from app.kernel.changesets import service as changesets
from app.services.flow import assemble, slices, view
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.packs import contracts
from app.services.session_host import exec_records
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


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))


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


def _slices(root: Path) -> dict:
    built = view.build(root)
    return {item["id"]: item for item in built["slices"]}, built


def _node(built: dict, node_id: str) -> dict:
    return next(node for node in built["nodes"] if node["id"] == node_id)


FULL = {"id": "all", "name": "全部"}


# ---------------------------------------------------------------------------
# 流程形状
# ---------------------------------------------------------------------------


def test_flow_shape_without_slicing_and_with_scenario_walkthrough():
    core = ("core", "0.3.0", _yaml("workspace-toolkit/flow.yaml"))
    bare = assemble.assemble([core])
    ids = [node["id"] for node in bare["nodes"]]
    assert ids[:5] == ["core.research", "core.model", "core.metric", "core.validate", "core.publish"]
    by_id = {node["id"]: node for node in bare["nodes"]}
    assert by_id["core.validate"]["title"] == "校验" and "core.consistency" not in by_id  # 11-05 删了
    steps = by_id["core.model"]["steps"]
    assert [step["id"] for step in steps] == ["object", "operation", "rule", "check"]
    assert next(step for step in steps if step["id"] == "rule")["optional"] is True  # 规则随操作一起抽，这一步是补抽
    assert by_id["core.publish"]["placement"] == "spine"
    assert by_id["core.metric"]["placement"] == "branch" and by_id["core.metric"]["optional"]
    with_trace = assemble.assemble([core, ("trace", "0.10.0", _yaml("packs/trace/flow.yaml"))])
    by_id = {node["id"]: node for node in with_trace["nodes"]}
    assert by_id["core.validate"]["title"] == "校验" and by_id["trace.trace"]["title"] == "场景模拟"
    # 场景模拟不再往建模里加「场景」一步：场景在校验之后做
    assert [step["id"] for step in by_id["core.model"]["steps"]] == ["object", "operation", "rule", "check"]
    assert {"from": "core.validate", "to": "trace.trace"} in with_trace["edges"]
    assert {"from": "trace.trace", "to": "core.publish"} in with_trace["edges"]
    assert "trace.mock" not in by_id and with_trace["commands"]["mock"]["node"] == "trace.trace"
    # 没升级的包写的 0.2 锚点（core.operation）照样认
    legacy = {"nodes": [{"id": "old.node", "title": "旧包节点", "placement": "branch", "section": "optional",
                         "optional": True, "after": "core.operation", "done": []}],
              "commands": {"old-cmd": {"node": "old.node"}}}
    assembled = assemble.assemble([core, ("old", "1.0.0", legacy)])
    assert next(node for node in assembled["nodes"] if node["id"] == "old.node")["after"] == "core.model"
    bad_hook = {"slice_steps": [{"node": "core.validate", "step": {"id": "x", "label": "X"}}]}
    try:
        assemble.assemble([core, ("bad", "1.0.0", bad_hook)])
    except assemble.FlowAssemblyError as exc:
        assert any("按段的节点" in problem for problem in exc.problems)
    else:
        raise AssertionError("只能往按段的节点里加步骤")


def test_flow_api_shape_for_a_new_project(env):
    _client, _workspaces, _manager, _project_id, root = env
    built = view.build(root)
    model = _node(built, "core.model")
    assert model["per_slice"] is True and model["progress"] is None
    assert [stage["id"] for stage in built["slice_stages"]] == ["object", "operation", "rule", "check"]
    # 没有蓝图：只有一段「全部」
    assert [item["id"] for item in built["slices"]] == ["all"] and built["blueprint"] == {"state": "none"}
    assert set(built["slices"][0]) == {"id", "name", "core_flow", "group", "stages", "note", "no_rules"}
    publish = _node(built, "core.publish")
    assert publish["actions"] == [{"kind": "start", "label": "去发布", "target": "publish"}]
    assert built["commands"]["extract-object"] == "core.model"


# ---------------------------------------------------------------------------
# 「全部」的状态
# ---------------------------------------------------------------------------


def _no_gate(monkeypatch):
    """合成的操作文档不全，门禁（另有用例）在这里放过：这里只看分段状态。"""
    from app.kernel.changesets import gates

    monkeypatch.setattr(gates, "compare", lambda *args, **kwargs: {"errors": 0, "warnings": 0, "findings": []})


def test_old_sliced_project_runs_as_one_full_slice(env, monkeypatch):
    """F8 之前按段建的项目（蓝图里三段）：只算「全部」一段，进行中、待确认、已确认、过期照常。"""
    _client, workspaces, _manager, project_id, root = env
    _no_gate(monkeypatch)
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    _blueprint(root)
    rows, built = _slices(root)
    assert list(rows) == ["all"] and rows["all"]["group"] == "todo" and rows["all"]["no_rules"] is False
    # 资料分析还没验收：建模未就绪；不按段，没有「n/m 段」
    assert _node(built, "core.model")["progress"] is None and _node(built, "core.model")["state"] == "not_ready"
    # 对象已经在主线上（存量）：对象一步已确认；规则是可选的补抽，不挡建模
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/object/商品.md", obj("商品", "合成商品"))
    rows, built = _slices(root)
    full = rows["all"]
    assert full["stages"]["object"] == "confirmed" and full["stages"]["operation"] == "ready"
    assert full["stages"]["rule"] == "not_ready" and full["no_rules"] is False
    assert full["group"] == "now" and full["note"] == "停在「操作」"
    assert _node(built, "core.model")["state"] == "running"
    # 下一步不带分段
    assert built["suggestions"]["primary"]["action"] == {"type": "send", "text": "",
                                                         "command": "/ontology-extract-operation"}
    # 进行中：老命令带了 slice=s1 也记在「全部」
    flow = flow_model.load_for(root)
    record = exec_records.start(project_id, "default", root=root, flow=flow, command="/ontology-extract-operation",
                                command_name="extract-operation", text="/ontology-extract-operation slice=s1")
    assert record["node"]["id"] == "core.model" and record["slice"] == FULL and record["step"] == "operation"
    rows, built = _slices(root)
    assert rows["all"]["stages"]["operation"] == "running" and rows["all"]["note"] == "正在做「操作」"
    # 待确认：变更集里做了操作
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/operation/受理申请.md", op("受理申请", "[[门店]][[商品]]"), commit="agent: 受理申请")
    exec_records.update(project_id, record["id"], state="done")
    changeset = changesets.observe(workspaces, project_id, running=False, conversation_id="default",
                                   exec_record=exec_records.get(project_id, record["id"]))
    assert changeset["slice_steps"] == [["all", "operation"]] and changeset["slice_id"] == "all"
    rows, _built = _slices(root)
    assert rows["all"]["stages"]["operation"] == "pending_confirm" and rows["all"]["stages"]["check"] == "pending_confirm"
    assert rows["all"]["group"] == "wait" and rows["all"]["note"] == "待确认"
    # 合入：这一步记为确认过，写入即查跟着确认；对象、操作都有了，建模做完（规则、场景不挡）
    changesets.merge(workspaces, project_id, changeset["id"], actor="employee_0001")
    from app import wiring

    wiring._confirm_nodes(project_id, changeset.get("nodes") or ["core.model"],
                          changesets.get(workspaces, project_id, changeset["id"]))
    rows, built = _slices(root)
    assert rows["all"]["stages"]["operation"] == "confirmed" and rows["all"]["stages"]["check"] == "confirmed"
    assert rows["all"]["group"] == "done" and _node(built, "core.model")["state"] == "confirmed"
    assert node_state.load(root)["slices"]["all"]["operation"]["merged"] is True
    # 过期：对象在主线上又改了，操作一步过期；节点显示过期
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店（改了口径）"))
    rows, built = _slices(root)
    assert rows["all"]["stages"]["operation"] == "stale" and rows["all"]["group"] == "wait"
    assert rows["all"]["note"].endswith("步过期")
    assert _node(built, "core.model")["state"] == "stale"


def test_old_sliced_blueprint_does_not_hold_back_completion(env):
    """老蓝图里第二段的操作还没建：不按段算，对象、操作都有了建模就做完；项目列表的进度与 FL-01 同口径。"""
    _client, _workspaces, _manager, _project_id, root = env
    _blueprint(root, draft={"slices": [{"name": "甲", "objects": ["对象一"], "operations": ["操作一"]},
                                       {"name": "乙", "objects": ["对象二"], "operations": ["操作二"]}]})
    main_write(root, "ontology/object/对象一.md", obj("对象一", "合成"))
    rows, built = _slices(root)
    assert list(rows) == ["all"] and _node(built, "core.model")["state"] == "running"
    assert flow_model.progress(root)["hits"]["core.model"] is False
    main_write(root, "ontology/operation/操作一.md", op("操作一", "[[对象一]]"))
    rows, built = _slices(root)
    assert rows["all"]["group"] == "done" and _node(built, "core.model")["state"] == "confirmed"
    assert _node(built, "core.model")["progress"] is None
    assert flow_model.progress(root)["hits"]["core.model"] is True


def test_full_mode_and_legacy_confirmations_do_not_regress(env):
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    flow = flow_model.load_for(root)
    # 0.2 的记录：对象提取、操作提取已确认；规则与指标被跳过；之后资料分析又改过（对象提取当时已经过期）
    with node_state._lock:
        stored = node_state.load(root)
        stored["nodes"] = {"core.object": {"fp": "0" * 16, "sig": "legacy", "at": 1.0},
                           "core.operation": {"fp": node_state.fingerprint(root, ["object"]), "sig": "legacy", "at": 1.0}}
        stored["skipped"] = {"core.rule_metric": {"at": 1.0, "reason": "", "slice_id": None}}
        node_state.save(root, stored)
    rows, built = _slices(root)
    full = rows["all"]
    assert full["name"] == "全部" and full["stages"]["object"] == "stale" and full["stages"]["operation"] == "confirmed"
    assert full["stages"]["rule"] == "skipped"
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    assert model["state"] == "stale" and model["flags"]["needs_review"] is False and model["progress"] is None
    metric = next(node for node in built["nodes"] if node["id"] == "core.metric")
    assert metric["state"] == "skipped"
    assert built["progress"]["done"] >= 2  # 资料分析、建模都还算做过（过期也算），进度不往回退
    stored = node_state.load(root)
    assert stored["legacy_migrated"] is True and stored["slices"]["all"]["object"]["merged"] is True
    # 刷新之后（这一步确认生效）回到已确认
    node_state.confirm(root, flow, ["core.model"])
    slices.confirm(root, flow, [["all", "object"]], doc=None)
    rows, built = _slices(root)
    assert rows["all"]["stages"]["object"] == "confirmed"
    assert next(node for node in built["nodes"] if node["id"] == "core.model")["state"] == "confirmed"


# ---------------------------------------------------------------------------
# 执行记录、跳过、开始、不再归段
# ---------------------------------------------------------------------------


def test_exec_records_land_on_the_full_slice(env):
    _client, _workspaces, _manager, project_id, root = env
    _blueprint(root)
    flow = flow_model.load_for(root)

    def start(text):
        command = text.split()[0]
        name = command[len("/ontology-"):]
        return exec_records.start(project_id, "default", root=root, flow=flow, command=command, command_name=name,
                                  text=text)

    # 带分段的规则提取（F8 之前的写法）仍是建模的「规则」一步，记在「全部」；不带是指标旁支
    rule = start("/ontology-extract-rule-metric slice=s2")
    assert (rule["node"]["id"], rule["slice"], rule["step"]) == ("core.model", FULL, "rule")
    metric = start("/ontology-extract-rule-metric")
    assert metric["node"]["id"] == "core.metric" and metric["slice"] is None
    inferred = start("/ontology-extract-object")
    assert inferred["slice"] == FULL and inferred["step"] == "object"
    unknown = start("/ontology-extract-object slice=s9")
    assert unknown["node"]["id"] == "core.model" and unknown["slice"] == FULL


def test_skip_rule_step_and_start_the_next_step(env):
    client, _workspaces, _manager, project_id, root = env
    _blueprint(root)
    base = f"/api/v1/projects/{project_id}/flow/nodes/core.model"
    assert client.post(f"{base}/skip", json={}).json()["code"] == "node_not_skippable"
    assert client.post(f"{base}/skip", json={"slice_id": "all", "step": "object"}).json()["code"] == "node_not_skippable"
    skipped = client.post(f"{base}/skip", json={"slice_id": "all", "reason": "资料里没有规则"})
    assert skipped.status_code == 200, skipped.text
    rows, _built = _slices(root)
    assert rows["all"]["stages"]["rule"] == "skipped"
    assert client.post(f"{base}/restore", json={"slice_id": "all"}).status_code == 200
    assert client.post(f"{base}/restore", json={"slice_id": "all"}).json()["code"] == "node_not_skipped"
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    main_write(root, "ontology/object/商品.md", obj("商品", "合成"))
    started = client.post(f"{base}/start", json={"origin": {"page": "wb-l2", "label": "开始"}})
    assert started.status_code == 202, started.text
    from tests.test_session_host import FakeEngine
    import time

    for _ in range(200):
        if FakeEngine.sent:
            break
        time.sleep(0.01)
    # 下一步是操作提取，一次做完整个项目，不带分段
    assert FakeEngine.sent[0].startswith("/ontology-extract-operation") and "slice=" not in FakeEngine.sent[0]


def test_new_entries_are_not_assigned_to_slices_at_turn_end(env):
    """F8 之前一轮结束时把新条目记进这一段；现在不分段，老蓝图、带老分段的执行记录也不再改蓝图。"""
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
    assert "research/blueprint.json" not in record["paths"]


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
    assert every[0]["action"]["command"] == "/ontology-extract-operation"
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



def test_new_core_assembles_with_packs_that_have_not_upgraded_yet():
    """跟随通道升级时 core 先换（packs/runtime.follow）：这时别的包还是旧版本，写的是 0.2 的锚点与节点。"""
    core = ("core", "0.3.0", _yaml("workspace-toolkit/flow.yaml"))
    old_trace = {"nodes": [
        {"id": "trace.mock", "title": "示例数据", "placement": "spine", "section": "required", "after": "core.validate",
         "done": ["mock"], "stale_on": ["object"], "produces": ["ontology/data-mock/*"],
         "entry": {"command": "/ontology-mock"}},
        {"id": "trace.trace", "title": "场景验证", "placement": "spine", "section": "required", "after": "trace.mock",
         "done": ["testcase"], "produces": ["ontology/testcase/*", "ext/trace/*"], "entry": {"command": "/ontology-trace"}}],
        "commands": {"mock": {"node": "trace.mock"}, "trace": {"node": "trace.trace"}}}
    old_codegen = {"nodes": [
        {"id": "codegen.solver", "title": "求解适配", "placement": "branch", "section": "optional", "optional": True,
         "after": "core.operation", "done": ["solver"], "panel": True, "actions": []}],
        "commands": {"solver-evaluator": {"node": "codegen.solver"}}}
    old_evo = {"nodes": [
        {"id": "evo.evolution", "title": "反馈演进", "placement": "on_demand", "section": "on_demand", "progress": False,
         "after": "core.operation", "display_after": ["core.update"], "done": []}],
        "commands": {"evolution": {"node": "evo.evolution", "requires": []}}}
    flow = assemble.assemble([core, ("trace", "0.9.2", old_trace), ("codegen", "0.9.1", old_codegen),
                              ("evo", "0.3.1", old_evo)])
    by_id = {node["id"]: node for node in flow["nodes"]}
    assert by_id["codegen.solver"]["after"] == "core.model" and by_id["evo.evolution"]["after"] == "core.model"
    assert {"from": "trace.trace", "to": "core.publish"} in flow["edges"]


def test_project_card_and_run_slices_for_autobuild(env):
    client, workspaces, _manager, project_id, root = env
    from app.services.flow import blueprint

    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] is None
    full = blueprint.run_slices(workspaces, project_id)
    assert [item["id"] for item in full] == ["all"] and full[0]["brief"].startswith("全量模式")
    # 老的三段蓝图：项目卡片不显示分段，自主构建也只跑「全部」一段、说明是全量模式
    _blueprint(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] is None
    items = blueprint.run_slices(workspaces, project_id)
    assert [item["id"] for item in items] == ["all"] and items[0]["depends_on"] == []
    assert items[0]["brief"].startswith("全量模式") and items[0]["stages"]["object"] == "confirmed"


# ---------------------------------------------------------------------------
# 规则与指标提取补回链（端到端实测）：建模不往回退，不推刚跑完的同一条命令
# ---------------------------------------------------------------------------


def _modeled_with_rules(root: Path) -> dict:
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]"))
    main_write(root, "ontology/rule/取严规则.md", "# 取严规则\n\n约束[[受理申请]]\n")
    flow = flow_model.load_for(root)
    node_state.confirm(root, flow, ["core.research", "core.model", "core.metric", "core.validate"])
    slices.confirm(root, flow, [["all", "object"], ["all", "operation"], ["all", "rule"]], doc=None)
    return flow


def test_optional_rule_step_going_stale_does_not_roll_back_modeling(env):
    """规则与指标提取在操作里补了一处回链：「规则」一步（可选补抽）过期，但建模的必经步骤都在，不算过期；
    后面的校验能核对这批改动，建议条不推「刷新『规则』」（那是刚跑完的同一条命令）。"""
    _client, _workspaces, _manager, _project_id, root = env
    _modeled_with_rules(root)
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，按[[取严规则]]校验"))
    rows, built = _slices(root)
    assert rows["all"]["stages"]["rule"] == "stale"
    assert _node(built, "core.model")["state"] == "confirmed"
    labels = [item["label"] for item in [built["suggestions"]["primary"], *built["suggestions"]["secondary"]] if item]
    assert not any("规则" in label and label.startswith("刷新") for label in labels)


def test_rule_metric_run_also_confirms_the_rule_step(env):
    """全量模式下合入「规则与指标提取」（记在指标旁支）时，建模里用同一条命令的「规则」一步一并确认。"""
    _client, _workspaces, _manager, project_id, root = env
    flow = _modeled_with_rules(root)
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，按[[取严规则]]校验"))
    assert _slices(root)[0]["all"]["stages"]["rule"] == "stale"
    assert slices.steps_done_by(root, flow, ["core.metric"], None) == [["all", "rule"]]
    assert slices.steps_done_by(root, flow, ["core.validate"], None) == []
    from app import wiring

    wiring._confirm_nodes(project_id, ["core.metric"], {"slice_steps": []})
    assert _slices(root)[0]["all"]["stages"]["rule"] == "confirmed"


def _labels(built: dict) -> list[str]:
    return [item["label"] for item in [built["suggestions"]["primary"], *built["suggestions"]["secondary"]] if item]


def _validated(root: Path) -> None:
    """建模、指标、校验都做完：在 _modeled_with_rules 上补指标和校验报告，重记基线。"""
    flow = _modeled_with_rules(root)
    main_write(root, "ontology/metric/达标率.md", "# 达标率\n\n合成指标\n")
    main_write(root, "reports/20260101-rule.md", "# 合成检查报告\n\n- [x] 已处理\n")
    node_state.confirm(root, flow, ["core.metric", "core.validate"])


def test_stale_label_names_what_changed_not_every_input(env):
    """过期的按钮写确认之后真变了的那几类：只改了指标，校验写「指标有变化」，不把对象、操作、规则、指标全列出来；
    建模步骤抽屉里的刷新按钮同一个说法。"""
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/metric/达标率.md", "# 达标率\n\n合成指标，改了口径\n")
    built = view.build(root)
    assert _node(built, "core.validate")["state"] == "stale"
    assert "指标有变化，刷新校验" in _labels(built)
    assert _node(built, "core.validate")["actions"][0]["label"] == "指标有变化，刷新校验"
    assert built["suggestions"]["note"] is None


def test_optional_metric_going_stale_is_left_to_validation(env):
    """指标（可选旁支）过期时交给后面的校验，不单独推：单独推会和建模来回兜圈子——指标补对象的关联让「操作」过期，
    重抽操作又让指标过期。必经的刷新排在前面。"""
    _client, _workspaces, _manager, _project_id, root = env
    _validated(root)
    main_write(root, "ontology/operation/受理申请.md", op("受理申请", "[[门店]]，补一句"))
    built = view.build(root)
    assert _node(built, "core.metric")["state"] == "stale"
    labels = _labels(built)
    assert labels[0] == "操作有变化，刷新校验"
    assert not any(label.endswith("刷新指标") for label in labels)
