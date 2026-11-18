"""增量更新响应「新资料到了」、自主构建按 need 判资格（节点模型重构 3）。

- 内核条件 materials_added：主线上对象和操作都有了以后上传资料，记下这一轮（`kernel:materials_added`）；增量更新出现在
  FL-01、被第 2 级推荐、S-18 里有它的按钮，助手副本的 state_helper 和后端给同一份；
- 这一轮处理完就隐藏：上传之后开始的一次增量更新正常做完（走到过执行更新；取消、中断、放弃、上传时已经在跑的那次都不算）、
  触发之后发布过、「这一轮不处理」（FL-03）；再上传开始新的一轮；处理完记在记录上，之后不再翻执行记录、不起 git；
- 触发出错不影响上传；R-02 的 `is_revision` 不另外发增量更新命令；
- 自主构建只做必需节点，打开「包括可选步骤」加上绘制流程图、提取规则与指标，不加生成脚本；第三方包的必需节点没写
  `autobuild: true` 不做；`until_node` 之后的可选节点不做；AB-01 的 `optional_nodes`（带 under、order）和 `until_order`。

项目、条目、资料都是合成的。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from app import config
from app.kernel_guards import _guard_common
from app.routers import sessions as session_routes
from app.services.autobuild import plan
from app.services.flow import activation, assemble, conditions, model, view
from app.services.session_host import exec_records, next_steps
from tests.test_changesets import git, main_write, obj
from tests.test_flow_states import env  # noqa: F401 - 共用 fixture
from tests.test_session_host import FakeEngine

KEY = activation.KERNEL_MATERIALS
P = "/api/v1/projects"


def _modeled(root) -> None:
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
    main_write(root, "ontology/operation/下单.md", "## 逻辑描述\n\n读取[[订单]]。\n")


def _upload(workspaces, project_id, name: str = "新资料.md") -> dict:
    return workspaces.save_resource(project_id, "others", name, "# 合成资料\n".encode("utf-8"), author="tester")


def _nodes(root) -> dict:
    return {node["id"]: node for node in view.build(root)["nodes"]}


def _flow_state_file(root) -> dict:
    return json.loads((root / view.FLOW_STATE_REL).read_text(encoding="utf-8"))


APPLIED = ["analyze", "confirm", "apply", "validate"]  # 正常做完：确认后执行更新、校验
CANCELLED = ["analyze", "confirm"]  # 在确认卡上选了「取消」：release 后停下，执行记录也是 done


def _update_run(project_id, root, *, state: str = "done", phases=APPLIED) -> dict:
    """一次增量更新的执行记录（开始时间就是现在），停在 state，命令锁走过 phases。"""
    record = exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                                command="/ontology-update", command_name="update")
    if state != "running":
        record = exec_records.update(project_id, record["id"], state=state, ended_at=time.time(),
                                     phases_seen=list(phases))
    return record


# ---- 触发 ----


def test_upload_before_anything_is_built_does_not_trigger(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    main_write(root, "research/订单.md", "# 合成资料分析\n")  # 只有资料分析，还没有对象、操作
    _upload(workspaces, project_id)
    assert activation.load(root) == {}
    assert "core.update" not in _nodes(root)


def test_objects_without_operations_do_not_trigger(env):  # noqa: F811
    """只有对象、没有操作：增量更新的前置（逻辑已提取）不满足，不开新一轮，下一步照旧是提取操作；两样都有了再上传才触发。"""
    client, workspaces, _manager, project_id, root = env
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
    _upload(workspaces, project_id)
    assert activation.load(root) == {}
    assert "core.update" not in _nodes(root)
    assert client.get(f"{P}/{project_id}/flow").json()["suggestions"]["primary"]["label"] == "提取操作"

    main_write(root, "ontology/operation/下单.md", "## 逻辑描述\n\n读取[[订单]]。\n")
    _upload(workspaces, project_id, "再一份.md")
    assert activation.load(root)[KEY]["files"] == ["resource/others/再一份.md"]
    assert "core.update" in _nodes(root)


def test_upload_after_modeling_shows_the_update_everywhere(env):  # noqa: F811
    client, workspaces, _manager, project_id, root = env
    _modeled(root)
    assert "core.update" not in _nodes(root)
    commit = _upload(workspaces, project_id)["commit"]
    record = activation.load(root)[KEY]
    assert record["commit"] == commit and record["files"] == ["resource/others/新资料.md"] and "handled" not in record

    body = client.get(f"{P}/{project_id}/flow").json()
    node = next(item for item in body["nodes"] if item["id"] == "core.update")
    # 条件成立时上导航栏，排在它挂的提取操作后面
    assert node["need"] == {"when": "materials_added"} and node["nav"] is True and node["under"] == "core.operation"
    assert node["section"] == "on_demand" and not node["counts_in_progress"]
    primary = body["suggestions"]["primary"]
    assert (primary["label"], primary["priority"], primary["reason"]) == ("增量更新", 2, "上传了新资料")
    assert primary["action"]["command"].strip() == "/ontology-update"

    group = next(item for item in session_routes.command_catalog(root)["groups"] if item["id"] == "core.operation")
    button = next(item for item in group["items"] if item["node_id"] == "core.update")
    assert (button["label"], button["target"], button["primary"]) == ("增量更新", "/ontology-update", True)

    # 助手副本：flow-state.json 里有它，state_helper suggest 和后端同一份
    assert _flow_state_file(root)["nodes"]["core.update"]["state"] == "ready"
    built = view.build(root)["suggestions"]
    assert next_steps.suggest(root) == {key: value for key, value in built.items() if key != "rev"}


def test_files_pile_up_within_a_round(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id, "第一份.md")
    first = activation.load(root)[KEY]
    second_commit = _upload(workspaces, project_id, "第二份.md")["commit"]
    second = activation.load(root)[KEY]
    assert second["files"] == ["resource/others/第一份.md", "resource/others/第二份.md"]
    assert second["commit"] == second_commit and second["at"] > first["at"]


# ---- 这一轮处理完 ----


def test_an_update_started_after_the_upload_hides_it_and_one_already_running_does_not(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    running = _update_run(project_id, root, state="running")  # 上传前就在跑（手动输入的命令）
    _upload(workspaces, project_id)
    exec_records.update(project_id, running["id"], state="done", ended_at=time.time(), phases_seen=APPLIED)
    assert "core.update" in _nodes(root)  # 在跑的那次没看过这次的资料

    _update_run(project_id, root)
    assert "core.update" not in _nodes(root)
    assert activation.load(root)[KEY]["handled"]["by"] == "update"
    # 隐藏时助手副本里它是「不出现」：flow-state.json 记成跳过并标 hidden，state_helper 不推它、和后端同一份
    assert _flow_state_file(root)["nodes"]["core.update"] == {"state": "skipped", "flags": {"hidden": True}}
    built = view.build(root)["suggestions"]
    helper = next_steps.suggest(root)
    assert helper == {key: value for key, value in built.items() if key != "rev"}
    assert (helper["primary"] or {}).get("label") != "增量更新"


def test_a_cancelled_failed_interrupted_or_abandoned_update_leaves_it_there(env):  # noqa: F811
    """只有正常做完的那一次结束这一轮：确认卡上取消（done 但没走到执行更新）、前置不满足就停下（锁都没拿）、
    被打断、放弃的都不算。"""
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id)
    _update_run(project_id, root, phases=CANCELLED)
    assert "core.update" in _nodes(root)
    _update_run(project_id, root, phases=[])
    _update_run(project_id, root, state="interrupted", phases=APPLIED)
    _update_run(project_id, root, state="abandoned", phases=APPLIED)
    assert "core.update" in _nodes(root) and "handled" not in activation.load(root)[KEY]
    _update_run(project_id, root)
    assert "core.update" not in _nodes(root)


def test_handled_round_is_remembered_without_git_or_exec_records(env, monkeypatch):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id)
    _update_run(project_id, root)
    assert not activation.materials_pending(root)

    def boom(*_args, **_kwargs):
        raise AssertionError("处理完之后不该再查")

    monkeypatch.setattr(activation, "_handled_by", boom)
    assert not activation.materials_pending(root)
    assert "core.update" not in _nodes(root)


def test_no_record_means_no_git_and_no_exec_records(env, monkeypatch):  # noqa: F811
    _client, _workspaces, _manager, _project_id, root = env

    def boom(*_args, **_kwargs):
        raise AssertionError("没有记录时不该查")

    monkeypatch.setattr(_guard_common, "_latest_release", boom)
    monkeypatch.setattr(exec_records, "query", boom)
    assert not activation.materials_pending(root) and not conditions.holds("materials_added", root)


def test_release_ends_the_round_and_the_next_upload_starts_over(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id)
    assert "core.update" in _nodes(root)
    git(root, "tag", "release/0.1.0")
    assert "core.update" not in _nodes(root)
    assert activation.load(root)[KEY]["handled"]["by"] == "release"

    _upload(workspaces, project_id, "再一份.md")
    assert "core.update" in _nodes(root)
    assert activation.load(root)[KEY]["files"] == ["resource/others/再一份.md"]  # 新的一轮，不带上一轮的文件


def test_not_now_ends_the_round_and_the_next_upload_starts_over(env):  # noqa: F811
    """「这一轮不处理」复用 FL-03：条件节点的跳过就是结束这一轮，节点隐藏，不记跳过（下一轮照常出现）。"""
    client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id)
    base = f"{P}/{project_id}/flow/nodes/core.update"
    restore = client.post(f"{base}/restore", json={})
    assert restore.status_code == 409 and restore.json()["code"] == "node_not_skipped"

    ended = client.post(f"{base}/skip", json={"reason": "这次的资料不影响已建内容"})
    assert ended.status_code == 200, ended.text
    assert ended.json()["node"] is None and ended.json()["flow_hash"]
    assert "core.update" not in _nodes(root)
    handled = activation.load(root)[KEY]["handled"]
    assert (handled["by"], handled["reason"]) == ("skip", "这次的资料不影响已建内容")
    assert client.post(f"{base}/skip", json={}).json()["code"] == "node_not_found"  # 已经不在流程里

    _upload(workspaces, project_id, "再一份.md")
    nodes = _nodes(root)
    assert nodes["core.update"]["state"] == "ready"  # 没有留下「已跳过」


def test_a_failing_trigger_does_not_fail_the_upload(env, monkeypatch):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)

    def boom(*_args, **_kwargs):
        raise RuntimeError("合成故障")

    monkeypatch.setattr(activation, "_built", boom)
    saved = _upload(workspaces, project_id)
    assert saved["commit"] and (root / saved["path"]).is_file()
    assert activation.load(root) == {}


def test_revision_upload_does_not_send_its_own_update(env):  # noqa: F811
    """R-02 `is_revision`：不另外发增量更新命令（`command_task` 为 null），由条件节点出现、建议条推；不会触发两次。"""
    client, _workspaces, _manager, project_id, root = env
    _modeled(root)
    for name in ("修订一.md", "修订二.md"):
        response = client.post(f"{P}/{project_id}/resources", data={"is_revision": "true", "batch_id": "b1"},
                               files={"file": (name, "# 合成修订\n".encode("utf-8"), "text/markdown")})
        assert response.status_code == 201, response.text
        assert response.json()["command_task"] is None
    assert FakeEngine.sent == []
    assert exec_records.query(project_id) == []
    assert activation.load(root)[KEY]["files"] == ["resource/others/修订一.md", "resource/others/修订二.md"]
    assert client.get(f"{P}/{project_id}/flow").json()["suggestions"]["primary"]["label"] == "增量更新"


# ---- 自主构建按 need ----


def test_autobuild_runs_required_nodes_and_optional_ones_only_when_asked(env):  # noqa: F811
    _client, workspaces, _manager, project_id, root = env
    _modeled(root)
    _upload(workspaces, project_id)
    flow = plan.flow(root)
    assert "core.update" in {node["id"] for node in flow["nodes"]}  # 条件成立也不做

    def rows(include_optional: bool) -> list[str]:
        policy = {"scope": {"until_node": None, "refresh_stale_first": True, "include_optional": include_optional}}
        return [row["id"] for row in plan.lay_out(root, flow, policy)[1]]

    required = rows(False)
    assert required == ["core.object", "core.operation", "core.validate", "trace.trace"]
    every = rows(True)
    assert set(every) - set(required) == {"diagram.flowchart", "core.metric"}
    assert not {"codegen.codegen", "codegen.solver", "core.update", "core.publish"} & set(every)
    ids = [node["id"] for node in flow["nodes"]]
    assert plan.optional_nodes(flow) == [
        {"id": "diagram.flowchart", "name": "绘制流程图", "under": "core.research", "order": ids.index("diagram.flowchart")},
        {"id": "core.metric", "name": "提取规则与指标", "under": "core.operation", "order": ids.index("core.metric")}]


def test_optional_nodes_after_until_node_are_not_done(env):  # noqa: F811
    """做到哪一步（until_node）之后的可选节点不做：后端走到 until_node 就停，AB-01 的 until_order 和每项的 order 同一个顺序。"""
    client, _workspaces, _manager, project_id, root = env
    flow = plan.flow(root)

    def picked(until: str) -> set[str]:
        policy = {"scope": {"until_node": until, "refresh_stale_first": True, "include_optional": True}}
        return {node["id"] for node in plan.candidates(flow, policy)}

    assert "diagram.flowchart" in picked("core.object") and "core.metric" not in picked("core.object")
    assert "core.metric" not in picked("core.operation")  # 挂在提取操作下面，但排在它后面
    assert {"diagram.flowchart", "core.metric"} <= picked("core.validate")

    url = f"{P}/{project_id}/autobuild/policy"
    body = client.put(url, json={"scope": {"until_node": "core.object"}}).json()
    assert body["until_order"] == plan.position(flow, "core.object")
    kept = [item["id"] for item in body["optional_nodes"] if item["order"] <= body["until_order"]]
    assert kept == ["diagram.flowchart"] and kept == [node_id for node_id in ("diagram.flowchart", "core.metric")
                                                      if node_id in picked("core.object")]
    assert client.put(url, json={"scope": {"until_node": None}}).json()["until_order"] is None


def _core() -> tuple[str, str, dict]:
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "flow.yaml"
    return ("core", "0.6.1", yaml.safe_load(path.read_text(encoding="utf-8")))


def _third_party_node(node_id: str, **fields) -> dict:
    return {"id": node_id, "title": f"合成{node_id}", "kind": "agent", "after": "core.validate",
            "actions": [{"label": f"合成{node_id}", "command": f"/x-{node_id.split('.')[1]}"}], **fields}


def test_third_party_nodes_stay_out_of_autobuild_unless_they_opt_in():
    third = ("x", "0.1.0", {"nodes": [
        _third_party_node("x.must"),
        _third_party_node("x.opted", autobuild=True, after="x.must"),
        _third_party_node("x.maybe", need="optional", after="x.must"),
    ]})
    flow = assemble.assemble([_core(), third])
    policy = {"scope": {"until_node": None, "include_optional": True}}
    picked = {node["id"] for node in plan.candidates(flow, policy)}
    assert "x.opted" in picked and not {"x.must", "x.maybe"} & picked
    assert {"core.object", "core.operation", "core.validate", "core.metric"} <= picked
    assert [item["id"] for item in plan.optional_nodes(flow)] == ["core.metric"]
    # 镜像内置入库的官方包：没写 autobuild 的必需节点照做
    trusted = assemble.assemble([_core(), third], trusted={"x"})
    assert "x.must" in {node["id"] for node in plan.candidates(trusted, policy)}


def test_no_optional_nodes_means_an_empty_list():
    flow = {"nodes": [{"id": "core.research", "title": "资料分析", "need": "required",
                       "actions": [{"label": "资料分析", "command": "/ontology-research"}]},
                      {"id": "core.object", "title": "提取对象", "need": "required",
                       "actions": [{"label": "提取对象", "command": "/ontology-extract-object"}]},
                      {"id": "x.panel", "title": "合成面板", "need": "optional", "panel": True,
                       "actions": [{"label": "合成面板", "command": "/x-panel"}]}]}
    assert plan.optional_nodes(flow) == []
