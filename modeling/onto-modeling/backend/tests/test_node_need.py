"""节点挂法收成 need 一个字段（节点模型重构 2a）。

- 旧写法按映射表折算成 need（内核 need.py 与助手副本 flow_state 两份对齐），同时写了以 need 为准，旧字段按 need 补齐；
- need 取值校验、整理幂等、主命令与 entry 兼容；
- 第三方包的节点默认不进自主构建，上传检查列出会被执行的节点；
- 装配格式升级后所有项目重新装配；读老格式的装配目录时补上 need；
- FL-01 的 need、nav、under，进度只算必需节点（项目列表同一口径），只能跳过可选节点；
- 旧写法的触发包（visible_if、preempt）照样出现、抢先推荐；增量更新的条件没成立（没传新资料）时节点不出现。

项目、条目、包都是合成的。
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from app import config
from app.services.flow import assemble, conditions, need, view
from app.services.flow import model as flow_model
from app.services.packs import assembler as pack_assembler
from app.services.packs import checks
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import (
    ROUND_PACK,
    ROUND_PACK_FLOW,
    ROUND_PACK_FLOW_LEGACY,
    ROUND_STEPS,
    install_round_pack,
    make_assets,
    make_node_pack,
    make_pack,
)
from tests.test_changesets import main_write, obj
from tests.test_flow_states import env  # noqa: F401 - 共用 fixture
from tests.test_node_pack_lifecycle import enable_and_apply, install
from tests.test_node_pack_lifecycle import env as lifecycle_env  # noqa: F401

ASSETS = Path(config.ASSETS_DIR)
TOOLKIT = ASSETS / "workspace-toolkit"


def _flow_state():
    spec = importlib.util.spec_from_file_location("flow_state_need", TOOLKIT / "scripts" / "flow_state.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


flow_state = _flow_state()


def _core() -> tuple[str, str, dict]:
    return ("core", "0.6.0", yaml.safe_load((TOOLKIT / "flow.yaml").read_text(encoding="utf-8")))


def _pack(name: str) -> tuple[str, str, dict]:
    return (name, "test", yaml.safe_load((ASSETS / "packs" / name / "flow.yaml").read_text(encoding="utf-8")))


def _node(**fields) -> dict:
    return {"id": "x.node", "title": "合成节点", "kind": "agent", "after": "core.validate",
            "actions": [{"label": "合成动作", "command": "/x-run"}], **fields}


# ---- 映射表 ----

# (旧写法, 折算出的 need, 是不是按需节点：加 autobuild: false)，自上而下取第一条命中的
LEGACY_ROWS = {
    "visible_if": ({"visible_if": "activated", "preempt": "说明", "placement": "spine"}, {"when": "activated"}, False),
    "visible_if 先于 on_demand": ({"visible_if": "某条件", "placement": "on_demand"}, {"when": "某条件"}, False),
    "placement on_demand": ({"placement": "on_demand", "section": "on_demand", "progress": False}, "optional", True),
    "section on_demand": ({"section": "on_demand"}, "optional", True),
    "on_demand 先于 optional": ({"placement": "on_demand", "optional": True}, "optional", True),
    "optional": ({"optional": True, "placement": "step"}, "optional", False),
    "section optional": ({"section": "optional"}, "optional", False),
    "branch": ({"placement": "branch"}, "optional", False),
    "只有 progress false": ({"progress": False}, "optional", False),
    "progress false 先于 gate": ({"placement": "gate", "progress": False}, "optional", False),
    "gate": ({"placement": "gate", "section": "pre_publish"}, "required", False),
    "pre_publish": ({"section": "pre_publish"}, "required", False),
    "step": ({"placement": "step", "section": "required"}, "required", False),
    "spine": ({"placement": "spine"}, "required", False),
    "什么都没写": ({}, "required", False),
}


@pytest.mark.parametrize("row", LEGACY_ROWS, ids=list(LEGACY_ROWS))
def test_every_mapping_row(row):
    fields, expected, on_demand = LEGACY_ROWS[row]
    node = _node(**fields)
    assert need.need_of(node) == expected
    assert flow_state.need_of(node) == expected, "助手副本的映射表和内核的不一致"
    normalized = need.normalize_node(node)
    assert normalized["need"] == expected
    assert normalized.get("autobuild") is (False if on_demand else None)
    # 旧字段按 need 补齐：旧脚本读出来的和 need 一致
    kind = need.need_kind(normalized)
    assert bool(normalized.get("optional")) is (kind == "optional")
    assert normalized.get("progress", True) is (kind == "required")
    assert normalized.get("visible_if") == (expected["when"] if kind == "when" else None)
    assert flow_state.need_of({key: value for key, value in normalized.items() if key != "need"}) == expected


@pytest.mark.parametrize("declared, expected", [
    ("required", "required"), ("optional", "optional"), ({"when": " materials_added "}, {"when": "materials_added"}),
    ("sometimes", "optional"), ({"when": ""}, "optional"),  # 写错的按旧写法（这里是 branch）折算，组装检查另外报错
])
def test_declared_need_reads_the_same_on_both_sides(declared, expected):
    node = _node(need=declared, placement="branch")
    assert need.need_of(node) == flow_state.need_of(node) == expected


def test_old_fields_follow_need_when_both_are_written():
    required = need.normalize_node(_node(need="required", placement="branch", section="optional", optional=True,
                                         progress=False, visible_if="某条件", preempt=True))
    assert required["need"] == "required"
    assert (required["placement"], required["section"]) == ("spine", "required")
    assert not {"optional", "progress", "visible_if", "preempt"} & set(required)
    gate = need.normalize_node(_node(need="required", placement="gate", section="pre_publish"))
    assert (gate["placement"], gate["section"]) == ("gate", "pre_publish")  # 和 need 不矛盾的旧值留着
    optional = need.normalize_node(_node(need="optional", placement="spine", section="required"))
    assert (optional["placement"], optional["section"], optional["optional"], optional["progress"]) == (
        "branch", "optional", True, False)
    on_demand = need.normalize_node(_node(need="optional", placement="on_demand"))
    assert on_demand["placement"] == "on_demand" and "autobuild" not in on_demand  # 写了 need 就不按旧写法加
    when = need.normalize_node(_node(need={"when": " materials_added "}, optional=True))
    assert when["need"] == {"when": "materials_added"}
    assert (when["visible_if"], when["progress"], when["preempt"]) == ("materials_added", False, True)
    assert "optional" not in when


@pytest.mark.parametrize("value", ["sometimes", "", ["required"], {"when": ""}, {"when": "x", "also": 1}, {"if": "x"}])
def test_need_values_are_checked(value):
    contribution = ("x", "0.1.0", {"nodes": [_node(need=value)]})
    with pytest.raises(assemble.FlowAssemblyError) as caught:
        assemble.assemble([_core(), contribution])
    assert any("need 只能是 required、optional 或 {when: <条件>}" in problem for problem in caught.value.problems)
    assert need.need_problems(_node(need=value))
    assert not need.need_problems(_node(need="optional")) and not need.need_problems(_node())


@pytest.mark.parametrize("fields", [*(row[0] for row in LEGACY_ROWS.values()),
                                    {"need": "required", "placement": "branch", "optional": True},
                                    {"need": {"when": "activated"}, "preempt": "说明"},
                                    {"entry": {"command": "/x-old"}, "actions": []}],
                         ids=[*LEGACY_ROWS, "need 和旧字段", "条件节点", "只写了 entry"])
@pytest.mark.parametrize("trusted", [True, False], ids=["官方", "第三方"])
def test_normalize_is_idempotent(fields, trusted):
    once = need.normalize_node(_node(**fields), trusted=trusted)
    assert need.normalize_node(once, trusted=trusted) == once
    assert need.normalize_node(once) == once


def test_assembled_flow_is_already_normalized():
    flow = assemble.assemble([_core(), _pack("trace"), _pack("diagram"), _pack("codegen")],
                             trusted={"trace", "diagram", "codegen"})
    assert assemble.normalize_flow(flow)["nodes"] == flow["nodes"]


# ---- 主命令与 entry ----


def test_primary_command_is_the_first_action_and_entry_is_only_compat():
    node = need.normalize_node(_node(actions=[{"label": "说明", "text": "没有命令"},
                                             {"label": "第一", "command": " /x-run 全部 "},
                                             {"label": "第二", "command": "/x-other"}]))
    assert need.primary_command(node) == "/x-run 全部"
    assert node["entry"] == {"command": "/x-run 全部"}  # 旧脚本读的 entry 缺省取主命令
    # 节点名就是主命令名（节点模型重构 2b）：主命令的叫法换成节点名，变体不动
    assert [action["label"] for action in node["actions"]] == ["说明", "合成节点", "第二"]
    page = need.normalize_node(_node(actions=[{"label": "去发布", "page": "publish"}, {"label": "x", "command": "/x"}]))
    assert need.primary_command(page) == "" and "entry" not in page
    assert [action["label"] for action in page["actions"]] == ["合成节点", "x"]
    # 只写了 entry 的老节点：入口补成第一个动作
    old = need.normalize_node(_node(actions=[], entry={"command": "/x-old"}))
    assert old["actions"] == [{"label": "合成节点", "command": "/x-old"}]
    assert need.primary_command(old) == "/x-old"
    # entry 和主命令不一致时以主命令为准（资料分析原来是这样）
    research = next(item for item in assemble.assemble([_core()])["nodes"] if item["id"] == "core.research")
    assert need.primary_command(research) == "/ontology-research 仅基于本地文档"
    assert flow_state.primary_command(research) == need.primary_command(research)


def test_confine_records_the_primary_command():
    flow = assemble.assemble([_core(), _pack("domain-extend")], trusted={"domain-extend"})
    assert flow["confine"]["entry"] == "/domain-extend-bind"


# ---- 资产 ----


def test_core_and_official_packs_declare_need_only():
    old = {"placement", "section", "optional", "progress", "visible_if", "preempt", "entry"}
    declared = [("core", _core()[2])] + [(name, _pack(name)[2]) for name in ("trace", "diagram", "codegen", "domain-extend")]
    for pack, flow in declared:
        for node in flow["nodes"]:
            assert "need" in node and not old & set(node), f"{pack} 的 {node['id']} 还写着旧字段"
            assert need.valid(node["need"])
    nodes = {node["id"]: node for _, flow in declared for node in flow["nodes"]}
    assert nodes["core.update"]["need"] == {"when": "materials_added"}
    assert nodes["codegen.codegen"]["autobuild"] is False
    assert [node_id for node_id, node in nodes.items() if node.get("display_after")] == ["codegen.solver",
                                                                                         "domain-extend.check"]
    for name in ("trace", "diagram", "codegen", "domain-extend"):
        manifest = yaml.safe_load((ASSETS / "packs" / name / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["depends"] == {"core": ">=0.6"}
    assert yaml.safe_load((TOOLKIT / "manifest.yaml").read_text(encoding="utf-8"))["version"] == "0.6.1"


def test_update_stays_hidden_until_new_materials_arrive(tmp_path):
    """条件 materials_added 内核有了（节点模型重构 3）：没有上传记录时不成立，节点不出现。"""
    assert "materials_added" in conditions.CONDITIONS and not conditions.holds("materials_added", tmp_path)
    flow = conditions.apply(assemble.assemble([_core()]), tmp_path)
    assert "core.update" in flow["hidden_nodes"]
    assert "core.update" not in {node["id"] for node in flow["nodes"]}
    assert "update" not in flow["commands"]


def test_solver_keeps_its_start_condition():
    """求解适配仍挂在提取操作后面：没做代码生成、场景模拟时，提取操作做完就能开始。"""
    flow = assemble.assemble([_core(), _pack("trace"), _pack("codegen")], trusted={"trace", "codegen"})
    nodes = {node["id"]: node for node in flow["nodes"]}
    assert nodes["codegen.solver"]["after"] == "core.operation"
    ids = list(nodes)
    assert ids.index("codegen.codegen") < ids.index("codegen.solver")  # 显示仍排在代码生成后面
    assert view.under(nodes["codegen.solver"], nodes, ids) == "core.operation"


# ---- 第三方包不进自主构建 ----


def test_untrusted_pack_nodes_stay_out_of_autobuild_unless_they_opt_in():
    third = ("x", "0.1.0", {"nodes": [
        _node(id="x.must"),
        _node(id="x.opted", autobuild=True, after="x.must"),
        _node(id="x.maybe", need="optional", after="x.must"),
    ]})
    nodes = {node["id"]: node for node in assemble.assemble([_core(), third])["nodes"]}
    assert nodes["x.must"]["autobuild"] is False and nodes["x.maybe"]["autobuild"] is False
    assert nodes["x.opted"]["autobuild"] is True
    assert "autobuild" not in nodes["core.object"]  # core 总算官方
    trusted = {node["id"]: node for node in assemble.assemble([_core(), third], trusted={"x"})["nodes"]}
    assert "autobuild" not in trusted["x.must"]


def test_uploaded_node_pack_assembles_out_of_autobuild(lifecycle_env):  # noqa: F811
    client, manager, project_id, tmp_path = lifecycle_env
    install(manager, tmp_path)
    enable_and_apply(client, manager, project_id)
    root = manager.project_root(project_id)
    assembled = {node["id"]: node for node in json.loads((root / ".claude" / "flow.yaml").read_text())["nodes"]}
    assert assembled["naming.check"]["need"] == "required" and assembled["naming.check"]["autobuild"] is False
    # 官方包（镜像内置入库）照旧：场景模拟没写 autobuild
    assert "autobuild" not in assembled["trace.trace"] and "autobuild" not in assembled["core.validate"]


def _check(items: list[dict], check_id: str) -> dict | None:
    return next((item for item in items if item["id"] == check_id), None)


def test_upload_check_lists_nodes_autobuild_would_run(tmp_path):
    flow = {"nodes": [_node(id="naming.check", actions=[{"label": "检查", "command": "/naming-check"}]),
                      _node(id="naming.fix", title="修正命名", after="naming.check", autobuild=True,
                            actions=[{"label": "修正", "command": "/naming-check 修正"}])]}
    _, items = checks.run(make_node_pack(tmp_path / "opted", flow=flow), core_names={})
    item = _check(items, "autobuild")
    assert item["result"] == "warn" and "naming.fix（修正命名）" in item["detail"]
    assert "naming.check" not in item["detail"]
    flow["nodes"][1].pop("autobuild")
    _, items = checks.run(make_node_pack(tmp_path / "default", flow=flow), core_names={})
    assert _check(items, "autobuild")["result"] == "pass"
    # 官方包、core、没有节点的包没有这一项
    _, items = checks.run(make_node_pack(tmp_path / "official", flow=flow), core_names={}, official=True)
    assert _check(items, "autobuild") is None
    _, items = checks.run(make_pack(tmp_path / "plain", "plain", "0.1.0", files={"commands/plain-run.md": "x\n"}),
                          core_names={})
    assert _check(items, "autobuild") is None


def test_upload_check_need_and_primary_command(tmp_path):
    def run(name: str, node: dict) -> list[dict]:
        return checks.run(make_node_pack(tmp_path / name, flow={"nodes": [node]}), core_names={})[1]

    base = _node(id="naming.check", actions=[{"label": "检查", "command": "/naming-check"}])
    items = run("ok", {**base, "need": "required"})
    assert _check(items, "node_need") is None
    assert _check(items, "node_pack")["result"] == "pass"  # 第三方包能声明必需节点
    assert _check(run("bad", {**base, "need": "sometimes"}), "node_need")["result"] == "fail"
    legacy = _check(run("legacy", {**base, "per_slice": True, "blocks_in": "sliced"}), "node_need")
    assert legacy["result"] == "warn" and "per_slice" in legacy["detail"] and "blocks_in" in legacy["detail"]
    foreign = run("foreign", {**base, "actions": [{"label": "校验", "command": "/ontology-validate"}]})
    assert _check(foreign, "node_entry")["result"] == "fail" and _check(foreign, "node_pack")["result"] == "fail"
    # 老包只写 entry、没有动作：入口当主命令，照样通过
    old = run("old", {**{key: value for key, value in base.items() if key != "actions"},
                      "entry": {"command": "/naming-check"}, "placement": "step"})
    assert _check(old, "node_entry")["result"] == "pass" and _check(old, "node_pack")["result"] == "pass"


# ---- 装配格式升级 ----


def test_assembly_format_bump_reassembles_every_project(tmp_path, monkeypatch):
    from app.services.packs import service as pack_service

    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    root = manager.project_root(manager.create_project("老装配")["id"])
    before = manager.packs.assembler.active(root)["assembly_hash"]
    monkeypatch.setattr(pack_assembler, "ASSEMBLY_FORMAT", pack_assembler.ASSEMBLY_FORMAT + 1)
    assert manager.packs.status(root)["state"] == "pending"
    pack_service.reconcile_all(manager)
    active = manager.packs.assembler.active(root)
    assert active["assembly_hash"] != before
    assert json.loads((Path(active["dir"]) / "MANIFEST.json").read_text())["format"] == pack_assembler.ASSEMBLY_FORMAT


def test_old_format_assembly_gets_need_when_read(env):  # noqa: F811
    _client, _workspaces, _manager, _project_id, root = env
    target = (root / ".claude").resolve() / "flow.yaml"
    old = json.loads(target.read_text(encoding="utf-8"))
    for node in old["nodes"]:
        node.pop("need", None)
    target.chmod(0o644)
    target.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    nodes = {node["id"]: node for node in flow_model._assembled(root)["nodes"]}
    assert nodes and all(need.valid(node["need"]) for node in nodes.values())


# ---- FL-01、进度、跳过 ----


def test_fl01_nodes_carry_need_nav_and_under(env):  # noqa: F811
    client, _workspaces, _manager, project_id, _root = env
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    nodes = {node["id"]: node for node in body["nodes"]}
    required = ["core.research", "core.object", "core.operation", "core.validate", "trace.trace", "core.publish"]
    assert [node_id for node_id, node in nodes.items() if node["nav"]] == required
    assert all(nodes[node_id]["need"] == "required" and nodes[node_id]["under"] is None for node_id in required)
    assert {node_id: (node["need"], node["under"]) for node_id, node in nodes.items() if not node["nav"]} == {
        "diagram.flowchart": ("optional", "core.research"),
        "core.metric": ("optional", "core.operation"),
        "codegen.codegen": ("optional", "trace.trace"),
        "codegen.solver": ("optional", "core.operation"),
    }
    assert "core.update" not in nodes  # 条件还没有
    sections = {section["id"]: section["nodes"] for section in body["sections"]}
    assert sections["required"] == required and sections["pre_publish"] == []
    assert set(sections["optional"]) == {"diagram.flowchart", "core.metric", "codegen.codegen", "codegen.solver"}
    assert all(node["section"] == ("required" if node["nav"] else "optional") for node in nodes.values())
    # 进度只算必需节点；项目列表的 x/N 同一口径
    assert body["progress"] == {"done": 0, "total": len(required)}
    assert [node_id for node_id, node in nodes.items() if node["counts_in_progress"]] == required
    card = next(item for item in _workspaces.list_projects() if item["id"] == project_id)
    assert (card["stages_done"], card["stages_total"]) == (0, len(required))


def test_progress_counts_done_required_nodes_only(env):  # noqa: F811
    client, workspaces, _manager, project_id, root = env
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
    main_write(root, "ontology/metric/订单量.md", "# 订单量\n")  # 可选节点做完不加分子
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    progress = flow_model.progress(root)
    assert (progress["done"], progress["total"]) == (flow["progress"]["done"], flow["progress"]["total"])
    assert flow["progress"]["done"] == sum(1 for node in flow["nodes"]
                                           if node["counts_in_progress"] and node["state"] in ("confirmed", "stale"))
    assert flow["progress"]["total"] == 6


def test_only_optional_nodes_can_be_skipped(env):  # noqa: F811
    client, _workspaces, _manager, project_id, _root = env
    base = f"/api/v1/projects/{project_id}/flow/nodes"
    for node_id in ("core.research", "trace.trace", "core.publish"):
        refused = client.post(f"{base}/{node_id}/skip", json={})
        assert refused.status_code == 409 and refused.json()["code"] == "node_not_skippable"
    for node_id in ("diagram.flowchart", "codegen.solver"):
        skipped = client.post(f"{base}/{node_id}/skip", json={})
        assert skipped.status_code == 200 and skipped.json()["node"]["state"] == "skipped"


def test_start_node_sends_the_primary_command(env):  # noqa: F811
    from tests.test_session_host import FakeEngine

    client, _workspaces, _manager, project_id, _root = env
    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/core.research/start",
                          json={"origin": {"page": "wb-l2", "label": "开始"}})
    assert started.status_code == 202, started.text
    import time

    for _ in range(100):
        if FakeEngine.sent:
            break
        time.sleep(0.05)
    assert FakeEngine.sent and FakeEngine.sent[0].startswith("/ontology-research 仅基于本地文档")


# ---- 旧写法的触发包 ----


def test_legacy_round_pack_assembles_to_the_same_need():
    def nodes(flow: dict) -> dict:
        contribution = (ROUND_PACK, "0.1.0", copy.deepcopy(flow))
        return {node["id"]: node for node in assemble.assemble([_core(), contribution])["nodes"]}

    new, old = nodes(ROUND_PACK_FLOW), nodes(ROUND_PACK_FLOW_LEGACY)
    for step in ROUND_STEPS:
        assert new[step]["need"] == old[step]["need"] == {"when": "activated"}
        assert need.primary_command(new[step]) == need.primary_command(old[step])
        assert new[step].get("after") == old[step].get("after")
    assert list(new) == list(old)
    assert old["intake.scope"]["preempt"] == "上传了新资料，先圈定这次要改的范围"  # 旧脚本还读


def test_preempt_is_no_longer_checked():
    flow = copy.deepcopy(ROUND_PACK_FLOW_LEGACY)
    flow.pop("activation")
    flow.pop("confine")
    for node in flow["nodes"]:
        node.pop("visible_if")  # 普通节点带着 preempt：不报错，也不起作用
    assemble.assemble([_core(), (ROUND_PACK, "0.1.0", flow)])


@pytest.fixture()
def legacy_round(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.kernel.changesets import service as changesets
    from app.main import app
    from app.services import auth
    from app.services.packs import builtin

    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(auth, "ENABLED", False)
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    project_id = manager.create_project("旧写法触发")["id"]
    install_round_pack(manager, tmp_path, flow=ROUND_PACK_FLOW_LEGACY)
    yield TestClient(app), manager, project_id, manager.project_root(project_id)
    builtin.clear_cache()


def test_legacy_round_pack_still_appears_and_goes_first(legacy_round):
    from app.services.packs import service as pack_service

    client, manager, project_id, root = legacy_round
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
    main_write(root, "ontology/operation/下单.md", "## 逻辑描述\n\n读取[[订单]]。\n")
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": ROUND_PACK}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)
    assert not set(ROUND_STEPS) & {node["id"] for node in view.build(root)["nodes"]}
    manager.save_resource(project_id, "others", "新资料.md", "# 合成资料\n".encode("utf-8"), author="tester")
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    nodes = {node["id"]: node for node in body["nodes"]}
    assert set(ROUND_STEPS) <= set(nodes)
    assert all(nodes[step]["need"] == {"when": "activated"} and nodes[step]["nav"] for step in ROUND_STEPS)
    assert nodes["intake.scope"]["under"] == "core.operation"
    assert body["suggestions"]["primary"]["action"]["command"].strip() == "/intake-scope"
    assert body["suggestions"]["note"] is None  # preempt 的字符串不再当说明行
    assert not any(nodes[step]["counts_in_progress"] for step in ROUND_STEPS)
    refused = client.post(f"/api/v1/projects/{project_id}/flow/nodes/intake.scope/skip", json={})
    # 按包触发的节点：这一轮由包自己的状态文件管，不能「这一轮不处理」
    assert refused.status_code == 409 and refused.json()["code"] == "node_not_skippable"
