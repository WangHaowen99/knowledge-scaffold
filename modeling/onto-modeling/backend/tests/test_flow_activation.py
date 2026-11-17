"""包的节点按事件触发（ADR-0091）：启用带 activation 的包后，已经建了一部分又上传新资料才出现、抢先推荐，
范围确认前已有条目只读，这一轮结束或发布后消失；组装和上传时校验声明。

用 tests/pack_helpers 里合成的触发包（圈定范围 → 范围内修改 → 核对，核对挡发布，带 confine.lock_on_activation），
其余用仓库里真实的资产。项目、条目、资料都是合成的。
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest
import yaml

from app import config
from app.kernel.changesets import service as changesets
from app.services import publish as publish_service
from app.services.autobuild import plan
from app.services.flow import activation, assemble, conditions, view
from app.services.packs import checks
from app.services.packs import service as pack_service
from tests.pack_helpers import ROUND_PACK, ROUND_PACK_FLOW, ROUND_SCOPE_FILE, ROUND_STEPS, make_pack, make_round_pack
from tests.test_changesets import main_write, obj
from tests.test_dev_skill import pack_tool, skill_dir
from tests.test_flow_confine import (  # noqa: F401 - 共用 fixture 与工具
    _core,
    _round,
    denied,
    git,
    guard,
    project,
    write,
)
from tests.test_private_packs import AS_OWNER, MINE, post_zip, project as make_project, real_flow_env  # noqa: F401

STEPS = ROUND_STEPS
OBJECT = "ontology/object/订单.md"
OPERATION = "ontology/operation/下单.md"
SCOPE_FILE = ROUND_SCOPE_FILE
ORDER_WITH_PACK = ["core.research", "diagram.flowchart", "core.model", "intake.scope", "intake.apply", "intake.check",
                   "core.metric", "core.validate", "trace.trace", "codegen.codegen", "codegen.solver", "core.publish",
                   "core.update"]
MAINLINE = ["core.research", "core.model", "intake.scope", "intake.apply", "intake.check", "core.validate",
            "trace.trace", "core.publish"]


def _yaml(rel: str) -> dict:
    return yaml.safe_load((Path(config.ASSETS_DIR) / rel).read_text(encoding="utf-8"))


def _flow_file(root: Path) -> dict:
    return json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))


def _gate(client, project_id: str) -> dict | None:
    gates = client.get(f"/api/v1/projects/{project_id}/publish/check").json()["gates"]
    return next((item for item in gates if item["id"] == "flow:intake.check"), None)


def _modeled(root: Path) -> None:
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, OBJECT, obj("订单", "合成订单"))
    main_write(root, OPERATION, "## 逻辑描述\n\n读取[[订单]]。\n")


def _next_command(client, project_id: str) -> str:
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    return body["suggestions"]["primary"]["action"]["command"].strip()


def _enable(client, manager, project_id) -> None:
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": ROUND_PACK}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)


# ---- 组装进主线 ----


def test_assembly_puts_the_nodes_between_modeling_and_validation():
    flow = assemble.assemble([_core(), _round()])
    ids = [node["id"] for node in flow["nodes"]]
    assert ids.index("core.model") < ids.index("intake.scope") < ids.index("intake.apply") \
        < ids.index("intake.check") < ids.index("core.validate") < ids.index("core.publish")
    nodes = {node["id"]: node for node in flow["nodes"]}
    for step in STEPS:
        node = nodes[step]
        assert node["placement"] == "spine" and node["section"] == "required" and node["autobuild"] is False
    assert nodes["intake.check"]["blocks"] == ["core.publish"]
    assert nodes["intake.check"]["stale_on"] == ["object", "operation", "rule", "metric"]
    assert flow["commands"]["intake-scope"]["node"] == "intake.scope"
    assert flow["activations"] == {ROUND_PACK: ROUND_PACK_FLOW["activation"]}
    assert "activations" not in assemble.assemble([_core()])


def test_autobuild_leaves_the_nodes_to_the_user(project):  # noqa: F811
    _client, _manager, _project_id, root = project
    flow = assemble.assemble([_core(), _round(), ("trace", "0.1.0", _yaml("packs/trace/flow.yaml"))])
    policy = {"scope": {"until_node": None, "include_optional": False}}
    titles = {unit.get("node_id") or unit.get("node") or unit.get("id") for unit in plan._tail(root, flow, policy)}
    assert not {name for name in titles if str(name).startswith("intake.")}
    assert "core.validate" in titles


# ---- 真实项目：触发、抢先推荐、发布关卡 ----


def _upload(manager, project_id, name: str = "新资料.md") -> str:
    return manager.save_resource(project_id, "others", name, "# 合成资料\n".encode("utf-8"), author="tester")["commit"]


def _visible(root) -> bool:
    ids = {node["id"] for node in view.build(root)["nodes"]}
    assert ids & set(STEPS) in (set(), set(STEPS))
    return set(STEPS) <= ids


def _head(root) -> str:
    return git(root, "rev-parse", "HEAD").strip()


def _confirm_scope(root, *, editable=(OBJECT,), status="active") -> None:
    head = _head(root)
    main_write(root, SCOPE_FILE, json.dumps({
        "round": 1, "goal": "合成目标", "baseline": {"commit": head, "entries": 2}, "covers": head, "seeds": [], "chain": [],
        "editable": list(editable), "status": status, "max_depth": 2}, ensure_ascii=False))


def _blocked(client, manager, project_id, root, rel: str) -> bool:
    copy_root = changesets.copy_for_turn(manager, project_id, root)
    return denied(guard(copy_root, write(rel), cwd=copy_root))


def test_enabled_project_walks_the_mainline_and_publish_waits_for_the_check(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)

    flow = _flow_file(root)
    assert [node["id"] for node in flow["nodes"]] == ORDER_WITH_PACK
    assert [node["id"] for node in flow["nodes"] if node["id"] in MAINLINE] == MAINLINE
    assert flow["confine"]["scope_file"] == SCOPE_FILE
    assert {"intake-scope", "intake-apply", "intake-check"} <= {
        path.stem for path in (root / ".claude" / "commands").glob("*.md")}

    # 没传新资料：节点不出现，不挡发布
    assert _next_command(client, project_id) != "/intake-scope" and _gate(client, project_id) is None
    _upload(manager, project_id)

    # 建了一部分又传了新资料：下一步是圈定范围；发布被核对挡着
    assert _next_command(client, project_id) == "/intake-scope"
    gate = _gate(client, project_id)
    assert gate["blocking"] and gate["status"] == "not_run" and gate["source"]["pack"] == ROUND_PACK

    main_write(root, SCOPE_FILE, json.dumps({"status": "active"}))
    assert _next_command(client, project_id) == "/intake-apply"
    # 范围内修改改了对象：建模的操作一步因此过期，但这一轮的传播交给核对，不推整段重新提取（范围限定生效）
    main_write(root, OBJECT, obj("订单", "合成订单加了期望送达时间"))
    main_write(root, "intake/applied.json", "{}\n")
    assert _next_command(client, project_id) == "/intake-check"
    assert _gate(client, project_id)["blocking"]

    # 报告里还有 `[ ]`：还没做完
    main_write(root, "intake/check.md", "# 核对\n\n- [ ] ontology/operation/下单.md\n")
    assert _next_command(client, project_id) == "/intake-check"
    assert _gate(client, project_id)["blocking"]

    main_write(root, "intake/check.md", "# 核对\n\n没有。\n")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert nodes["intake.check"]["state"] == "confirmed"
    gate = _gate(client, project_id)
    assert not gate["blocking"] and gate["status"] == "pass"

    # 核对之后本体又变了：过期，要重新核对才能发布
    main_write(root, OBJECT, obj("订单", "合成订单改过"))
    gate = _gate(client, project_id)
    assert gate["blocking"] and gate["status"] == "fail"

    # 停用：关卡和范围限定一起去掉
    assert client.post(f"/api/v1/projects/{project_id}/packs/{ROUND_PACK}/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    flow = _flow_file(root)
    assert "confine" not in flow and not [node for node in flow["nodes"] if node["id"].startswith("intake.")]
    assert _gate(client, project_id) is None
    assert "intake-scope" not in {path.stem for path in (root / ".claude" / "commands").glob("*.md")}


def test_imported_package_goes_straight_to_scoping(project, tmp_path):  # noqa: F811
    """导入知识包后建模已完成、资料分析算越过（ADR-0084）；启用触发包后上传新资料，下一步就是圈定范围（ADR-0091）。"""
    client, manager, source, _root = project
    _modeled(manager.project_root(source))
    archive = tmp_path / "package.zip"
    publish_service.export_package_zip(manager, source, archive, allow_unbound=True)
    files = {"file": ("package.zip", archive.read_bytes(), "application/zip")}
    upload_id = client.post("/api/v1/uploads", data={"purpose": "project_import"}, files=files).json()["upload_id"]
    created = client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": "导入"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    _enable(client, manager, project_id)
    assert _next_command(client, project_id) == "/ontology-validate"
    _upload(manager, project_id)
    built = view.build(manager.project_root(project_id))
    nodes = {node["id"]: node for node in built["nodes"]}
    assert nodes["core.research"]["flags"]["backfill"] is True
    assert nodes["intake.scope"]["flags"]["backfill"] is False
    assert _next_command(client, project_id) == "/intake-scope"


def test_upload_before_anything_is_built_does_not_trigger(project):  # noqa: F811
    client, manager, project_id, root = project
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    assert activation.load(root) == {}
    assert not _visible(root)
    assert _gate(client, project_id) is None
    assert _next_command(client, project_id) != "/intake-scope"


def test_built_without_new_materials_stays_out_of_the_way(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    assert not _visible(root)
    assert _gate(client, project_id) is None
    assert _next_command(client, project_id) != "/intake-scope"
    assert not _blocked(client, manager, project_id, root, OBJECT)


def test_upload_after_modeling_triggers_and_locks_existing_entries(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    before = client.get(f"/api/v1/projects/{project_id}/flow").json()["flow_hash"]
    _upload(manager, project_id)
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    assert body["flow_hash"] != before and _visible(root)
    assert body["suggestions"]["primary"]["action"]["command"].strip() == "/intake-scope"
    assert "新资料" in body["suggestions"]["note"]
    assert _gate(client, project_id)["blocking"]
    assert activation.load(root)[ROUND_PACK]["files"] == ["resource/others/新资料.md"]

    copy_root = changesets.copy_for_turn(manager, project_id, root)
    answer = guard(copy_root, write(OBJECT), cwd=copy_root)
    assert denied(answer) and "上传了新资料" in json.loads(answer)["hookSpecificOutput"]["permissionDecisionReason"]
    assert not denied(guard(copy_root, write("ontology/object/新对象.md"), cwd=copy_root))


def test_partly_built_project_scopes_before_continuing_to_model(project):  # noqa: F811
    client, manager, project_id, root = project
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, OBJECT, obj("订单", "合成订单"))
    _enable(client, manager, project_id)
    assert _next_command(client, project_id) == "/ontology-extract-operation"
    _upload(manager, project_id)
    assert _next_command(client, project_id) == "/intake-scope"


def test_upload_before_enabling_the_pack_does_not_trigger(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _upload(manager, project_id)
    _enable(client, manager, project_id)
    assert activation.load(root) == {} and not _visible(root)


def test_confirmed_scope_takes_over_and_closing_ends_the_round(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    _confirm_scope(root)
    assert _visible(root) and _next_command(client, project_id) == "/intake-apply"
    assert not _blocked(client, manager, project_id, root, OBJECT)
    assert _blocked(client, manager, project_id, root, OPERATION)

    _confirm_scope(root, status="closed")
    assert not _visible(root) and _gate(client, project_id) is None
    assert not _blocked(client, manager, project_id, root, OPERATION)


def test_release_ends_the_round_and_the_next_upload_starts_over(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    _confirm_scope(root)
    main_write(root, "intake/applied.json", "{}\n")
    main_write(root, "intake/check.md", "# 核对\n\n没有。\n")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert all(nodes[step]["state"] == "confirmed" for step in STEPS)

    git(root, "tag", "release/0.1.0")
    assert not _visible(root) and _gate(client, project_id) is None
    assert not _blocked(client, manager, project_id, root, OPERATION)

    # 上一轮的文件还在，新一轮的节点不能直接算完成
    _upload(manager, project_id, "再一份资料.md")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert not any(nodes[step]["state"] in ("confirmed", "stale") for step in STEPS)
    assert _next_command(client, project_id) == "/intake-scope"
    assert _gate(client, project_id)["blocking"]
    assert _blocked(client, manager, project_id, root, OBJECT)


def test_upload_during_a_round_triggers_again_after_it_closes(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    _confirm_scope(root)
    _upload(manager, project_id, "中途的资料.md")
    assert _visible(root)
    assert not _blocked(client, manager, project_id, root, OBJECT)

    scope = json.loads((root / SCOPE_FILE).read_text(encoding="utf-8"))
    main_write(root, SCOPE_FILE, json.dumps({**scope, "status": "closed"}, ensure_ascii=False))
    assert _visible(root) and _next_command(client, project_id) == "/intake-scope"
    assert _blocked(client, manager, project_id, root, OBJECT)
    assert activation.load(root)[ROUND_PACK]["files"] == ["resource/others/中途的资料.md"]


def test_files_pile_up_until_the_trigger_is_handled(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id, "第一份.md")
    _upload(manager, project_id, "第二份.md")
    assert activation.load(root)[ROUND_PACK]["files"] == ["resource/others/第一份.md", "resource/others/第二份.md"]


def test_disabling_the_pack_lifts_everything(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    assert client.post(f"/api/v1/projects/{project_id}/packs/{ROUND_PACK}/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    assert not _visible(root) and _gate(client, project_id) is None
    assert not _blocked(client, manager, project_id, root, OBJECT)


def test_projects_without_the_pack_never_record_a_trigger(project):  # noqa: F811
    _client, manager, project_id, root = project
    _modeled(root)
    _upload(manager, project_id)
    assert activation.load(root) == {}


# ---- 声明的校验：组装和上传检查 ----


def _changed(edit) -> tuple:
    pack, version, data = _round()
    data = copy.deepcopy(data)
    edit(data)
    return pack, version, data


@pytest.mark.parametrize("edit, message", [
    (lambda d: d["activation"].update(event="commit"), "activation.event 不认识"),
    (lambda d: d["activation"].update(requires=["object"]), "activation.requires"),
    (lambda d: d["activation"].update(session="intake/没有这个.json"), "activation.session"),
    (lambda d: d["activation"].update(extra=1), "activation 要写"),
    (lambda d: d.pop("activation"), "没有声明 activation"),
    (lambda d: d["nodes"][0].pop("visible_if"), "preempt 只能用在"),
    (lambda d: (d.pop("activation"), [node.pop("visible_if") for node in d["nodes"]],
                [node.pop("preempt") for node in d["nodes"]]), "lock_on_activation"),
], ids=["事件", "探针", "状态文件", "多余的键", "没声明", "preempt", "锁定没声明"])
def test_assembly_refuses_a_bad_activation(edit, message):
    with pytest.raises(assemble.FlowAssemblyError, match=message):
        assemble.assemble([_core(), _changed(edit)])


GOOD = {"event": "materials_added", "requires": ["my_built"], "session": "my-scope/scope.json"}


def _pack(base, activation_spec, *, visible="activated", preempt=True, confine=None):
    node = {"id": "my-scope.scope", "title": "合成范围", "kind": "agent", "placement": "step", "section": "required",
            "after": "core.model", "done": [], "produces": ["my-scope/scope.json"], "entry": {"command": "/my-scope-run"}}
    if visible:
        node["visible_if"] = visible
    if preempt:
        node["preempt"] = preempt
    flow = {"probes": {"my_built": {"files": ["ontology/object"]}}, "nodes": [node]}
    if activation_spec is not None:
        flow["activation"] = activation_spec
    if confine is not None:
        flow["confine"] = confine
    return make_pack(base, "my-scope", "0.1.0", files={
        "commands/my-scope-run.md": "运行 $ARGUMENTS\n", "flow.yaml": yaml.safe_dump(flow, allow_unicode=True),
    }, contributes={"flow": "flow.yaml", "nodes": ["my-scope.scope"]})


@pytest.mark.parametrize("spec, result", [
    (GOOD, "pass"),
    ({**GOOD, "event": "commit"}, "fail"),
    ({**GOOD, "requires": ["object"]}, "fail"),
    ({**GOOD, "requires": "my_built"}, "fail"),
    ({**GOOD, "session": "my-scope/other.json"}, "fail"),
    ({**GOOD, "session": "my-scope/*.json"}, "fail"),
    ({**GOOD, "extra": 1}, "fail"),
    (None, "fail"),  # 节点写了 visible_if: activated，没声明 activation
], ids=["正确", "事件", "探针不是本包的", "requires 不是列表", "状态文件", "通配", "多余的键", "没声明"])
def test_upload_check_reads_activation(tmp_path, spec, result):
    _, items = checks.run(_pack(tmp_path, spec), core_names={})
    assert next(item for item in items if item["id"] == "activation")["result"] == result


def test_upload_check_refuses_preempt_on_an_ordinary_node(tmp_path):
    _, items = checks.run(_pack(tmp_path, None, visible=None), core_names={})
    assert next(item for item in items if item["id"] == "activation")["result"] == "fail"


def test_upload_check_has_no_item_without_the_declaration(tmp_path):
    _, items = checks.run(_pack(tmp_path, None, visible=None, preempt=None), core_names={})
    assert not [item for item in items if item["id"] == "activation"]


@pytest.mark.parametrize("spec, confine, result", [
    (GOOD, {"scope_file": "my-scope/scope.json", "lock_on_activation": True}, "pass"),
    (None, {"scope_file": "my-scope/scope.json", "lock_on_activation": True}, "fail"),
    (GOOD, {"scope_file": "my-scope/scope.json", "lock_on_activation": "yes"}, "fail"),
], ids=["正确", "没声明 activation", "不是布尔值"])
def test_upload_check_reads_lock_on_activation(tmp_path, spec, confine, result):
    _, items = checks.run(_pack(tmp_path, spec, visible=None if spec is None else "activated",
                                preempt=None if spec is None else True, confine=confine), core_names={})
    assert next(item for item in items if item["id"] == "confine")["result"] == result


def test_hidden_node_reanchors_followers():
    """visible_if 不成立的节点去掉后，挂在它后面的节点改挂到它的上游，连线、命令、钩子一起去掉。"""
    flow = {"nodes": [{"id": "core.a"}, {"id": "x.mid", "after": "core.a", "visible_if": "nope"},
                      {"id": "y.tail", "after": "x.mid"}],
            "edges": [{"from": "core.a", "to": "x.mid"}, {"from": "x.mid", "to": "y.tail"}],
            "commands": {"mid": {"node": "x.mid"}, "a": {"node": "core.a"}},
            "hooks": [{"id": "z.h", "node": "x.mid"}], "flow_hash": "old"}
    out = conditions.apply(flow, Path("/nonexistent"))
    assert [node["id"] for node in out["nodes"]] == ["core.a", "y.tail"]
    assert out["nodes"][1]["after"] == "core.a" and out["hidden_nodes"] == ["x.mid"]
    assert out["edges"] == [] and out["commands"] == {"a": {"node": "core.a"}} and out["hooks"] == []
    assert out["flow_hash"] != "old" and flow["nodes"][2]["after"] == "x.mid"  # 不改原流程
    plain = {"nodes": [{"id": "core.a"}]}
    assert conditions.apply(plain, Path("/nonexistent")) is plain


# ---- 开发 skill 和用户上传 ----


def test_pack_tool_builds_it_and_user_upload_passes(real_flow_env):  # noqa: F811
    """带 activation、visible_if、preempt、confine.lock_on_activation、kernel 探针和提示规则的包，开发 skill 能打包，
    用户上传能过检查。"""
    client, _manager, tmp_path = real_flow_env
    make_project(client)  # core 入库，依赖和 requires_flow 要比对它
    skill = skill_dir(client, tmp_path, "capability")
    assert "confine" in (skill / "references" / "protocol.md").read_text(encoding="utf-8")
    work = tmp_path / "work"
    source = work / ROUND_PACK
    shutil.copytree(make_round_pack(tmp_path / "src", checksums=False), source)
    built = pack_tool(skill, "build", source, "--json", cwd=work)
    assert built.returncode == 0, built.stdout + built.stderr
    report = json.loads(built.stdout)
    results = {item["id"]: item["result"] for item in report["checks"]}
    assert results["confine"] == "pass" and results["activation"] == "pass"

    accepted = post_zip(client, MINE, Path(report["zip"]).read_bytes(), AS_OWNER)
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"{MINE}/{accepted.json()['id']}", headers=AS_OWNER).json()
    assert detail["state"] == "passed", [item for item in detail["checks"] if item["result"] == "fail"]
