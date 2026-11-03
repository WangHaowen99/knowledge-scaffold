"""按详设 6.8 重排的主流程与按段的节点状态（详设 6.6、6.8、第 9 章；契约 FL-01 slices / slice_stages、
FL-03～FL-05、PB-01 流程关卡；ADR-0051）。

- 流程形状：资料分析 → 建模（按段）→ 校验（装了场景走查是「校验与走查」+ 全量回归）→ 全局一致性 → 发布；
  场景走查往分段里加「场景」一步；没升级的包写的旧锚点照样认；
- 按段算状态：进行中、待确认、已确认、过期、跳过（没规划规则）、写入即查；节点按各段汇总、给「n/m 段」；
- 全量模式：没有蓝图（存量项目）时只有一段「全部」；0.2 的已确认状态映射过来，进度不往回退；
- 执行记录按命令带的分段（或这一步还没做完的第一段）记分段与步骤；确认生效时这一段这一步记为确认过；
- 下一步：这一段的下一步、蓝图草案时「确认蓝图」；按段建模时全局一致性挡住发布。

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


def _blueprint(root: Path, *, confirmed: bool = True) -> dict:
    module = contracts.default().load("blueprint")
    doc = module.from_draft(DRAFT)
    if confirmed:
        doc = module.confirm(doc)
    main_write(root, "research/blueprint.json", module.dumps(doc), "research: 合成蓝图")
    return doc


def _slices(root: Path) -> dict:
    built = view.build(root)
    return {item["id"]: item for item in built["slices"]}, built


# ---------------------------------------------------------------------------
# 流程形状
# ---------------------------------------------------------------------------


def test_flow_shape_with_and_without_scenario_walkthrough():
    core = ("core", "0.3.0", _yaml("workspace-toolkit/flow.yaml"))
    bare = assemble.assemble([core])
    ids = [node["id"] for node in bare["nodes"]]
    assert ids[:6] == ["core.research", "core.model", "core.metric", "core.validate", "core.consistency", "core.publish"]
    by_id = {node["id"]: node for node in bare["nodes"]}
    assert by_id["core.validate"]["title"] == "校验" and by_id["core.consistency"]["after"] == "core.validate"
    assert [step["id"] for step in by_id["core.model"]["steps"]] == ["object", "operation", "rule", "check"]
    assert by_id["core.publish"]["placement"] == "spine" and by_id["core.consistency"]["blocks"] == ["core.publish"]
    assert by_id["core.metric"]["placement"] == "branch" and by_id["core.metric"]["optional"]
    with_trace = assemble.assemble([core, ("trace", "0.10.0", _yaml("packs/trace/flow.yaml"))])
    by_id = {node["id"]: node for node in with_trace["nodes"]}
    assert by_id["core.validate"]["title"] == "校验与走查"
    steps = by_id["core.model"]["steps"]
    assert [step["id"] for step in steps] == ["object", "operation", "rule", "check", "scenario"]
    assert steps[-1]["pack"] == "trace"
    assert {"from": "core.validate", "to": "trace.trace"} in with_trace["edges"]
    assert {"from": "trace.trace", "to": "core.consistency"} in with_trace["edges"]
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
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    assert model["per_slice"] is True and model["progress"] is None
    assert [stage["id"] for stage in built["slice_stages"]] == ["object", "operation", "rule", "check", "scenario"]
    assert built["slice_stages"][-1] == {"id": "scenario", "label": "场景", "plugin": "trace"}
    # 没有蓝图：全量模式，只有一段「全部」；`blueprint.state` 让前端把「调整分段」换成发起分段规划
    assert [item["id"] for item in built["slices"]] == ["all"] and built["blueprint"] == {"state": "none"}
    assert set(built["slices"][0]) == {"id", "name", "core_flow", "group", "stages", "note", "no_rules"}
    publish = next(node for node in built["nodes"] if node["id"] == "core.publish")
    assert publish["actions"] == [{"kind": "start", "label": "去发布", "target": "publish"}]
    assert built["commands"]["extract-object"] == "core.model"


# ---------------------------------------------------------------------------
# 按段的状态
# ---------------------------------------------------------------------------


def _no_gate(monkeypatch):
    """合成的操作文档不全，门禁（另有用例）在这里放过：这里只看分段状态。"""
    from app.kernel.changesets import gates

    monkeypatch.setattr(gates, "compare", lambda *args, **kwargs: {"errors": 0, "warnings": 0, "findings": []})


def test_slice_states_running_pending_confirmed_skipped_and_stale(env, monkeypatch):
    _client, workspaces, _manager, project_id, root = env
    _no_gate(monkeypatch)
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    _blueprint(root)
    rows, built = _slices(root)
    assert list(rows) == ["s1", "s2", "s3"] and all(row["group"] == "todo" for row in rows.values())
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    # 资料分析还没验收：建模未就绪
    assert model["progress"] == {"done": 0, "total": 3, "label": "0/3 段"} and model["state"] == "not_ready"
    # s1 的对象已经在主线上（存量）：对象一步已确认；没规划规则：规则跳过、不挡这一段
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/object/商品.md", obj("商品", "合成商品"))
    rows, built = _slices(root)
    assert rows["s1"]["stages"]["object"] == "confirmed" and rows["s1"]["stages"]["operation"] == "ready"
    assert rows["s1"]["stages"]["rule"] == "skipped" and rows["s1"]["no_rules"] is True
    assert rows["s1"]["group"] == "now" and rows["s1"]["note"] == "停在「操作」"
    assert rows["s2"]["stages"]["rule"] == "not_ready" and rows["s2"]["no_rules"] is False
    assert next(node for node in built["nodes"] if node["id"] == "core.model")["state"] == "running"
    assert built["suggestions"]["primary"]["action"] == {"type": "send", "text": "", "command": "/ontology-extract-operation",
                                                         "args": {"slice": "s1"}}
    # 进行中：执行记录带分段与步骤
    flow = flow_model.load_for(root)
    record = exec_records.start(project_id, "default", root=root, flow=flow, command="/ontology-extract-operation",
                                command_name="extract-operation", text="/ontology-extract-operation slice=s1")
    assert record["node"]["id"] == "core.model" and record["slice"] == {"id": "s1", "name": "申请受理"}
    assert record["step"] == "operation"
    rows, built = _slices(root)
    assert rows["s1"]["stages"]["operation"] == "running" and rows["s1"]["group"] == "now"
    assert rows["s1"]["note"] == "正在做「操作」"
    assert built["suggestions"]["primary"] is None or built["suggestions"]["primary"]["id"] != "core.model:s1:operation"
    # 待确认：变更集里做了 s1 的操作
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/operation/受理申请.md", op("受理申请", "[[门店]][[商品]]"), commit="agent: 受理申请")
    exec_records.update(project_id, record["id"], state="done")
    changeset = changesets.observe(workspaces, project_id, running=False, conversation_id="default",
                                   exec_record=exec_records.get(project_id, record["id"]))
    assert changeset["slice_steps"] == [["s1", "operation"]] and changeset["slice_id"] == "s1"
    rows, _built = _slices(root)
    assert rows["s1"]["stages"]["operation"] == "pending_confirm" and rows["s1"]["stages"]["check"] == "pending_confirm"
    assert rows["s1"]["group"] == "wait" and rows["s1"]["note"] == "待确认"
    # 确认生效：这一段这一步记为确认过；写入即查跟着确认；这一段做完
    changesets.merge(workspaces, project_id, changeset["id"], actor="employee_0001")
    from app import wiring

    wiring._confirm_nodes(project_id, changeset.get("nodes") or ["core.model"],
                          changesets.get(workspaces, project_id, changeset["id"]))
    rows, built = _slices(root)
    assert rows["s1"]["stages"]["operation"] == "confirmed" and rows["s1"]["stages"]["check"] == "confirmed"
    # 装了场景走查：这一段还差「场景」一步
    assert rows["s1"]["stages"]["scenario"] == "ready" and rows["s1"]["note"] == "停在「场景」"
    main_write(root, "ontology/testcase/受理申请.md", "# 测试场景：受理申请\n\n## 场景 1：合成\n")
    rows, built = _slices(root)
    assert rows["s1"]["group"] == "done" and rows["s1"]["stages"]["scenario"] == "confirmed"
    assert next(node for node in built["nodes"] if node["id"] == "core.model")["progress"]["label"] == "1/3 段"
    stored = node_state.load(root)
    assert stored["slices"]["s1"]["operation"]["merged"] is True
    # 过期：s1 的对象在主线上又改了，操作一步过期；节点显示过期
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店（改了口径）"))
    rows, built = _slices(root)
    assert rows["s1"]["stages"]["operation"] == "stale" and rows["s1"]["group"] == "wait"
    assert rows["s1"]["note"].endswith("步过期")
    assert next(node for node in built["nodes"] if node["id"] == "core.model")["state"] == "stale"
    # 别的段的条目变了不影响 s2（按段取上游）
    assert rows["s2"]["stages"]["object"] == "ready"


def test_slice_completion_progress_and_project_list(env):
    _client, _workspaces, _manager, _project_id, root = env
    module = contracts.default().load("blueprint")
    doc = module.from_draft({"slices": [{"name": "甲", "objects": ["对象一"], "operations": ["操作一"]},
                                        {"name": "乙", "objects": ["对象二"], "operations": ["操作二"]}]})
    main_write(root, "research/blueprint.json", module.dumps(module.confirm(doc)), "research: 合成蓝图")
    for name in ("对象一", "对象二"):
        main_write(root, f"ontology/object/{name}.md", obj(name, "合成"))
    main_write(root, "ontology/operation/操作一.md", op("操作一", "[[对象一]]"))
    main_write(root, "ontology/testcase/操作一.md", "# 测试场景：操作一\n\n## 场景 1：合成\n")
    rows, built = _slices(root)
    assert rows["s1"]["group"] == "done" and rows["s2"]["group"] == "now"
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    assert model["progress"]["label"] == "1/2 段" and model["state"] == "running"
    # 项目列表的进度与 FL-01 同口径：按段模式下要全部段做完建模才算完成
    assert flow_model.progress(root)["hits"]["core.model"] is False
    main_write(root, "ontology/operation/操作二.md", op("操作二", "[[对象二]]"))
    main_write(root, "ontology/testcase/操作二.md", "# 测试场景：操作二\n\n## 场景 1：合成\n")
    rows, built = _slices(root)
    model = next(node for node in built["nodes"] if node["id"] == "core.model")
    assert model["state"] == "confirmed" and model["progress"]["label"] == "2/2 段"
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
# 执行记录、跳过、开始、自动归段
# ---------------------------------------------------------------------------


def test_exec_records_pick_the_slice_and_step(env):
    _client, _workspaces, _manager, project_id, root = env
    _blueprint(root)
    flow = flow_model.load_for(root)

    def start(text):
        command = text.split()[0]
        name = command[len("/ontology-"):]
        return exec_records.start(project_id, "default", root=root, flow=flow, command=command, command_name=name,
                                  text=text)

    rule = start("/ontology-extract-rule-metric slice=s2")
    assert (rule["node"]["id"], rule["slice"]["id"], rule["step"]) == ("core.model", "s2", "rule")
    metric = start("/ontology-extract-rule-metric")
    assert metric["node"]["id"] == "core.metric" and metric["slice"] is None
    inferred = start("/ontology-extract-object")
    assert inferred["slice"] == {"id": "s1", "name": "申请受理"} and inferred["step"] == "object"
    unknown = start("/ontology-extract-object slice=s9")
    assert unknown["node"]["id"] == "core.model" and unknown["slice"] is None


def test_skip_rule_step_and_start_a_slice(env):
    client, _workspaces, _manager, project_id, root = env
    _blueprint(root)
    base = f"/api/v1/projects/{project_id}/flow/nodes/core.model"
    assert client.post(f"{base}/skip", json={}).json()["code"] == "node_not_skippable"
    assert client.post(f"{base}/skip", json={"slice_id": "s2", "step": "object"}).json()["code"] == "node_not_skippable"
    skipped = client.post(f"{base}/skip", json={"slice_id": "s2", "reason": "资料里没有规则"})
    assert skipped.status_code == 200, skipped.text
    rows, _built = _slices(root)
    assert rows["s2"]["stages"]["rule"] == "skipped"
    assert client.post(f"{base}/restore", json={"slice_id": "s2"}).status_code == 200
    assert client.post(f"{base}/restore", json={"slice_id": "s2"}).json()["code"] == "node_not_skipped"
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    main_write(root, "ontology/object/商品.md", obj("商品", "合成"))
    started = client.post(f"{base}/start", json={"slice_id": "s1", "origin": {"page": "wb-l2", "label": "开始"}})
    assert started.status_code == 202, started.text
    from tests.test_session_host import FakeEngine
    import time

    for _ in range(200):
        if FakeEngine.sent:
            break
        time.sleep(0.01)
    assert FakeEngine.sent[0].startswith("/ontology-extract-operation slice=s1")


def test_new_entries_are_assigned_to_the_slice_at_turn_end(env):
    _client, workspaces, _manager, project_id, root = env
    _blueprint(root)
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/补货台账.md", obj("补货台账", "合成"), commit="agent: 新对象")
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成"), commit="agent: 规划里的对象")
    record = changesets.observe(workspaces, project_id, running=False, conversation_id="default",
                                exec_record={"id": "ex-1", "command_label": "建模",
                                             "node": {"id": "core.model", "version": "core@0.3.0"},
                                             "slice": {"id": "s2", "name": "库存测算"}, "step": "object"})
    doc = json.loads((copy / "research" / "blueprint.json").read_text(encoding="utf-8"))
    assert "补货台账" in doc["slices"][1]["objects"] and "门店" not in doc["slices"][1]["objects"]
    assert "research/blueprint.json" in record["paths"]


# ---------------------------------------------------------------------------
# 下一步与发布关卡
# ---------------------------------------------------------------------------


def test_blueprint_draft_suggestions_and_research_waits_for_confirmation(env):
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "research/20260101-合成.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    _blueprint(root, confirmed=False)
    built = view.build(root)
    research = next(node for node in built["nodes"] if node["id"] == "core.research")
    assert research["state"] == "pending_confirm"
    assert research["actions"][0] == {"kind": "confirm", "label": "去确认", "target": "wb-blueprint"}
    primary = built["suggestions"]["primary"]
    assert built["blueprint"] == {"state": "draft"}
    assert primary["label"] == "确认蓝图" and primary["action"] == {"type": "confirm_blueprint", "next": "L2"}
    labels = [item["label"] for item in built["suggestions"]["secondary"]]
    assert labels == ["调整分段", "重新划分"]


def test_consistency_gates_publish_only_when_slicing(env):
    client, _workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    check = client.get(f"/api/v1/projects/{project_id}/publish/check").json()
    assert not [gate for gate in check["gates"] if gate["source"]["kind"] == "flow"]
    _blueprint(root)
    check = client.get(f"/api/v1/projects/{project_id}/publish/check").json()
    gate = next(gate for gate in check["gates"] if gate["source"]["kind"] == "flow")
    assert gate["id"] == "flow:core.consistency" and gate["blocking"] and gate["status"] == "not_run"
    assert gate["action"] == {"type": "start_node", "node_id": "core.consistency"}
    refused = client.post(f"/api/v1/projects/{project_id}/releases", json={"degrade": True})
    assert refused.status_code == 422 and refused.json()["code"] == "gate_blocked"
    publish = next(node for node in view.build(root)["nodes"] if node["id"] == "core.publish")
    assert publish["state"] == "blocked"


def test_consistency_check_script_reports_relations_terms_and_cross_slice(tmp_path):
    import subprocess
    import sys

    module = contracts.default().load("blueprint")
    doc = module.from_draft({**DRAFT, "terms": {"ambiguous": [{"variants": ["门店", "要货方"]}]}})
    doc = module.apply_edit(doc, {"mode": "draft", "term_choices": [{"term_id": "t1", "chosen": "门店"}]})
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "blueprint.json").write_text(module.dumps(doc), encoding="utf-8")
    for rel, text in {
        "ontology/object/门店.md": obj("门店", "合成"),
        "ontology/object/库存标准.md": obj("库存标准", "合成"),
        "ontology/operation/受理申请.md": op("受理申请", "[[门店]] 要货方提交 [[不存在的对象]]"),
        "ontology/operation/生成提示.md": op("生成提示", "[[门店]][[库存标准]]"),
        "ontology/operation/段外操作.md": op("段外操作", "[[门店]]"),
    }.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    script = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "consistency_check.py"
    done = subprocess.run([sys.executable, str(script), "--json"], cwd=tmp_path, capture_output=True, text=True)
    assert done.returncode == 1, done.stderr
    result = json.loads(done.stdout)
    codes = {item["code"] for item in result["findings"]}
    assert {"dangling_link", "term_variant", "unassigned", "planned_missing"} <= codes
    report = (tmp_path / "reports" / "consistency" / "consistency.md").read_text(encoding="utf-8")
    assert "- [ ]" in report and "要货方" in report
    # 已勾掉的结论下一次保留
    first = next(item for item in result["findings"] if item["code"] == "orphan_object" or item["code"] == "unassigned")
    report = report.replace(f"- [ ] {first['text']} ({first['id']})", f"- [x] {first['text']} ({first['id']})")
    (tmp_path / "reports" / "consistency" / "consistency.md").write_text(report, encoding="utf-8")
    again = json.loads(subprocess.run([sys.executable, str(script), "--json"], cwd=tmp_path, capture_output=True,
                                      text=True).stdout)
    assert again["pending"] == result["pending"] - 1


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
    assert {"from": "trace.trace", "to": "core.consistency"} in flow["edges"]


def test_project_card_and_run_slices_for_autobuild(env):
    client, workspaces, _manager, project_id, root = env
    from app.services.flow import blueprint

    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] is None  # 全量模式：分段待规划
    full = blueprint.run_slices(workspaces, project_id)
    assert [item["id"] for item in full] == ["all"] and full[0]["brief"].startswith("全量模式")
    _blueprint(root)
    main_write(root, "ontology/object/门店.md", obj("门店", "合成"))
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    # 规划的对象还差一个：这一段还没开始
    assert card["slices"] == {"total": 3, "confirmed": 0, "cells": ["ready", "ready", "ready"]}
    main_write(root, "ontology/object/商品.md", obj("商品", "合成"))
    card = next(item for item in client.get("/api/v1/projects").json()["items"] if item["id"] == project_id)
    assert card["slices"] == {"total": 3, "confirmed": 0, "cells": ["running", "ready", "ready"]}
    items = blueprint.run_slices(workspaces, project_id)
    assert [item["id"] for item in items] == ["s1", "s2", "s3"] and items[1]["depends_on"] == ["s1"]
    assert items[0]["group"] == "now" and items[0]["stages"]["object"] == "confirmed"
    assert items[1]["brief"].startswith("分段：库存测算（s2）")
