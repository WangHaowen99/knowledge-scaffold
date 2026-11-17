"""包的节点按事件触发（ADR-0091）：启用增量构建后，已经建了一部分又上传新资料才出现。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from app.kernel.changesets import service as changesets
from app.services.flow import activation, assemble, conditions, view
from app.services.packs import checks
from app.services.packs import service as pack_service
from tests.pack_helpers import make_pack
from tests.test_changesets import main_write, obj
from tests.test_incremental_confine import (  # noqa: F401 - 共用 fixture 与工具
    denied,
    git,
    guard,
    project,
    write,
)
from tests.test_incremental_pack import _core, _gate, _incremental, _modeled, _next_command

STEPS = ("incremental.scope", "incremental.apply", "incremental.check")
OBJECT = "ontology/object/订单.md"
OPERATION = "ontology/operation/下单.md"
SCOPE_FILE = "incremental/scope.json"


def _enable(client, manager, project_id) -> None:
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "incremental"}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)


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


def test_upload_before_anything_is_built_does_not_trigger(project):  # noqa: F811
    client, manager, project_id, root = project
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    assert activation.load(root) == {}
    assert not _visible(root)
    assert _gate(client, project_id) is None
    assert _next_command(client, project_id) != "/incremental-scope"


def test_built_without_new_materials_stays_out_of_the_way(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    assert not _visible(root)
    assert _gate(client, project_id) is None
    assert _next_command(client, project_id) != "/incremental-scope"
    assert not _blocked(client, manager, project_id, root, OBJECT)


def test_upload_after_modeling_triggers_and_locks_existing_entries(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    before = client.get(f"/api/v1/projects/{project_id}/flow").json()["flow_hash"]
    _upload(manager, project_id)
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    assert body["flow_hash"] != before and _visible(root)
    assert body["suggestions"]["primary"]["action"]["command"].strip() == "/incremental-scope"
    assert "新资料" in body["suggestions"]["note"]
    assert _gate(client, project_id)["blocking"]
    assert activation.load(root)["incremental"]["files"] == ["resource/others/新资料.md"]

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
    assert _next_command(client, project_id) == "/incremental-scope"


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
    assert _visible(root) and _next_command(client, project_id) == "/incremental-apply"
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
    main_write(root, "incremental/applied.json", "{}\n")
    main_write(root, "incremental/check.md", "# 传播核对\n\n没有。\n")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert all(nodes[step]["state"] == "confirmed" for step in STEPS)

    git(root, "tag", "release/0.1.0")
    assert not _visible(root) and _gate(client, project_id) is None
    assert not _blocked(client, manager, project_id, root, OPERATION)

    # 上一轮的文件还在，新一轮的节点不能直接算完成
    _upload(manager, project_id, "再一份资料.md")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert not any(nodes[step]["state"] in ("confirmed", "stale") for step in STEPS)
    assert _next_command(client, project_id) == "/incremental-scope"
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
    assert _visible(root) and _next_command(client, project_id) == "/incremental-scope"
    assert _blocked(client, manager, project_id, root, OBJECT)
    assert activation.load(root)["incremental"]["files"] == ["resource/others/中途的资料.md"]


def test_files_pile_up_until_the_trigger_is_handled(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id, "第一份.md")
    _upload(manager, project_id, "第二份.md")
    assert activation.load(root)["incremental"]["files"] == ["resource/others/第一份.md", "resource/others/第二份.md"]


def test_disabling_the_pack_lifts_everything(project):  # noqa: F811
    client, manager, project_id, root = project
    _modeled(root)
    _enable(client, manager, project_id)
    _upload(manager, project_id)
    assert client.post(f"/api/v1/projects/{project_id}/packs/incremental/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    assert not _visible(root) and _gate(client, project_id) is None
    assert not _blocked(client, manager, project_id, root, OBJECT)


def test_projects_without_the_pack_never_record_a_trigger(project):  # noqa: F811
    _client, manager, project_id, root = project
    _modeled(root)
    _upload(manager, project_id)
    assert activation.load(root) == {}


def _changed(edit) -> tuple:
    pack, version, data = _incremental()
    data = copy.deepcopy(data)
    edit(data)
    return pack, version, data


@pytest.mark.parametrize("edit, message", [
    (lambda d: d["activation"].update(event="commit"), "activation.event 不认识"),
    (lambda d: d["activation"].update(requires=["object"]), "activation.requires"),
    (lambda d: d["activation"].update(session="incremental/没有这个.json"), "activation.session"),
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
