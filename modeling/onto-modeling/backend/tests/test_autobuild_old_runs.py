"""按节点排之前开始的自主构建运行（老运行）照样能看、能复核、能撤回、能续跑（节点模型重构 1B 兼容）。

老运行的单元按段：`{id: "all", name: "全部", kind: "slice", steps: [对象, 操作]}`，后面可以跟主线节点；运行记录里各段的
进度按步骤记（`steps`），运行前的确认记录按步骤记（`state_before.object`……），台账、挂起问题记在「全部」这一段上。

- 详情（AB-05）：只有老运行有步骤列（`stages`），按段的一行按步骤填格子，节点一行没有格子；报告（AB-16）每个单元一行；
- 待处理：老运行的复核项写「建模」，改判、补上发这一段那一步的命令；留到下一次的挂起问题落到那一步的节点上；
- 撤回（AB-17 `slices/all/revert`）：按步骤记的确认记录落回提取对象、提取操作；按段的一段本来就整体撤回，不按倒序挡；
- 暂停在段中间的老运行续跑：接着做这一段没做的那一步，再做后面的节点。

假引擎见 tests/autobuild_helpers.py；测试数据全部为合成内容。
"""

from __future__ import annotations

import time

from app.services.autobuild import store
from tests.autobuild_helpers import OBJECTS, AutoEngine, ab, ask, start, wait_state  # noqa: F401
from tests.test_changesets import main_write, obj

OLD_STEPS = [{"id": "object", "label": "对象", "title": "对象提取", "action": "提取对象",
              "command": "/ontology-extract-object", "args": {}},
             {"id": "operation", "label": "操作", "title": "操作提取", "action": "提取操作",
              "command": "/ontology-extract-operation", "args": {}}]
ALL = {"id": "all", "name": "全部"}


def _states(client, project_id: str) -> dict:
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    return {node["id"]: node for node in flow["nodes"]}


def _as_old_run(project_id: str, run_id: str, unit_id: str, *, state_before: dict) -> None:
    """把一次跑完的新运行改写成老运行的样子：那一步的合入算「全部」这一段（对象、操作两步）的，台账、挂起问题记在
    这一段的操作一步上。"""
    def change(record: dict) -> None:
        info = dict(record["slices"][unit_id])
        info.update(steps={"object": "done", "operation": "done"}, state_before=state_before)
        record["units"] = [{"id": "all", "name": "全部", "kind": "slice", "steps": OLD_STEPS,
                            "before": {"object": "stale", "operation": "ready"}}]
        record["before_rows"] = [{"id": "all", "name": "全部", "group": "now",
                                  "stages": {"object": "confirmed", "operation": "ready"}}]
        record["slices"] = {"all": info}
        for item in [*record["ledger"], *record["suspensions"]]:
            item.update(slice=dict(ALL), step="operation")
    store.update(project_id, run_id, change)


def test_an_old_run_shows_reviews_and_reverts_onto_the_split_nodes(ab):
    client, _workspaces, project_id, root = ab
    main_write(root, "ontology/object/对象9.md", obj("对象9", "运行前就有的合成对象"))
    AutoEngine.hooks["/ontology-extract-operation"] = [ask([
        {"question": "临期按几级分", "header": "置信度 0.6", "options": [{"label": "四级（推荐）"}, {"label": "三级（保守）"}]},
        {"question": "审批交给谁", "header": "新增对象 · 置信度 0.9", "options": [{"label": "区域（推荐）"}, {"label": "门店"}]}])]
    run = start(client, project_id, scope={"until_node": "core.operation"}, advanced={"must_suspend_kinds": ["新增对象"]})
    wait_state(client, project_id, run["run_id"], "completed")
    # 按段时代记的运行前记录（没有判定签名、提交号）：提取对象那时是过期的
    stale = {"fp": "0" * 16, "kinds": {"research": "0" * 16}, "at": 1.0, "merged": True}
    _as_old_run(project_id, run["run_id"], "core.operation", state_before={"object": stale})
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"

    # 详情：步骤列只给老运行；按段的一行按步骤填格子
    detail = client.get(base).json()
    assert detail["stages"] == [{"id": "object", "label": "对象"}, {"id": "operation", "label": "操作"}]
    assert [(row["slice_id"], row["state"], row["cells"], row["result"]) for row in detail["matrix"]] == [
        ("all", "done", {"object": "auto", "operation": "auto"}, "完成 · 挂起 1 项")]
    assert detail["slices"] == {"done": 1, "total": 1} and detail["tail_text"] == ""
    report = client.get(f"{base}/report").json()
    assert [(row["slice_id"], row["result"], row["revertible"]) for row in report["slices"]] == [("all", "done", True)]

    # 待处理：复核项写「建模」，不提分段；改判、补上用这一段操作那一步的命令
    from app.services.autobuild import plan, review as review_module

    items = client.get(f"/api/v1/projects/{project_id}/inbox", params={"filter": "autobuild_review"}).json()["items"]
    review = next(item for item in items if item["kind"] == "review" and not item["high_confidence"])
    suspended = next(item for item in items if item["kind"] == "suspension")
    assert review["title"] == "提取操作：三级" and review["slice"] == ALL and review["actions"][:3] == [
        "accept", "rejudge", "revert_slice"]
    assert review["impact"] == [{"label": "步骤", "items": ["建模"]}]
    record = store.get(project_id, run["run_id"])
    entry = record["ledger"][0]
    assert review_module._command_for(record, entry["slice"], entry["step"]) == "/ontology-extract-operation"
    # 挂起问题留到下一次：下一次运行在提取操作这一步补上
    assert suspended["id"] == f"suspension:{record['suspensions'][0]['id']}"
    deferred = client.post(f"/api/v1/projects/{project_id}/autobuild/suspensions/{record['suspensions'][0]['id']}"
                           "/answer", json={"custom_text": "区域", "action": "defer_to_next_run"})
    assert deferred.status_code == 200 and deferred.json()["suspension"]["state"] == "deferred", deferred.text
    held = plan.carried(project_id)
    assert list(held) == ["core.operation"] and held["core.operation"][0]["run_id"] == run["run_id"]

    # 撤回「全部」：按步骤记的确认记录落回这一段两步的节点上——提取对象放回运行前的（仍是过期，判定签名按现在的节点
    # 记，不误标方法升级）；提取操作运行前没有记录，按主线现状重算（操作撤掉了，回到可开始）
    reverted = client.post(f"{base}/slices/all/revert")
    assert reverted.status_code == 200, reverted.text
    states = _states(client, project_id)
    assert states["core.object"]["state"] == "stale" and states["core.object"]["flags"]["needs_review"] is False
    assert states["core.operation"]["state"] == "ready"
    assert not (root / "ontology" / "operation" / "操作1.md").exists()
    row = client.get(f"{base}/report").json()["slices"][0]
    assert row["reverted"] is True and row["revertible"] is False
    assert client.post(f"{base}/slices/all/revert").json()["details"]["reason"] == "already_reverted"
    accepted = client.post(f"/api/v1/projects/{project_id}/autobuild/ledger/{entry['id']}/accept")
    assert accepted.status_code == 409 and accepted.json()["code"] == "slice_reverted"
    assert plan.carried(project_id) == {}  # 撤回了的这一段上的挂起问题不再带到下一次


def _paused_old_run(project_id: str, root) -> dict:
    """暂停在「全部」这一段中间的老运行：对象那一步做完了，操作那一步还没做；后面跟校验。"""
    from app.services import conversation_store, mainline
    from app.services.autobuild import budget, policy

    conversation = conversation_store.create(project_id, kind="autobuild", run_id="run-1", title="自主构建 · 运行 #1")
    pol = policy.defaults()
    pol["scope"].update(until_node="core.validate", mode="selected", slice_ids=["all"])
    head = mainline.state(root)
    now = time.time()
    record = {
        "project_id": project_id, "run_id": "run-1", "run_no": 1, "state": "paused",
        "conversation_id": conversation["id"], "owner": "employee_0001", "actor": "employee_0001",
        "created_at": now, "started_at": now, "ended_at": None, "policy": pol,
        "units": [{"id": "all", "name": "全部", "kind": "slice", "steps": OLD_STEPS,
                   "before": {"object": "ready", "operation": "not_ready"}},
                  {"id": "core.validate", "name": "校验", "kind": "node",
                   "steps": [{"id": "core.validate", "label": "校验", "title": "校验", "action": "校验",
                              "command": "/ontology-validate", "args": {}}]}],
        "before_rows": [{"id": "all", "name": "全部", "group": "todo", "stages": {"object": "ready",
                                                                                "operation": "not_ready"}}],
        "slices": {"all": {"state": "running", "started_at": now, "steps": {"object": "done", "operation": "queued"},
                           "current_step": None, "decisions": [], "suspensions": [],
                           "timeline": [{"type": "step", "id": "object", "text": "提取对象", "meta": "1 分",
                                         "state": "done", "started_at": now, "duration_ms": 60000}],
                           "fix_rounds": 0, "produced": {}, "checks": {}, "state_before": {}}},
        "cursor": {"unit": "all", "step": None, "exec_id": None, "continues": 0}, "flags": {},
        "budget": budget.empty(), "ledger": [], "suspensions": [], "notes": [], "cards": [], "recovered": 0,
        "resume_pending": False, "base_commit": head.get("head"), "base_version_label": head.get("version_label"),
        "failure": None, "report": None, "pending_rejudges": [], "pause_reason": None,
    }
    return store.save(record)


def test_a_paused_old_run_resumes_the_rest_of_its_slice(ab):
    client, _workspaces, project_id, root = ab
    for name in OBJECTS:  # 对象那一步已经做完
        main_write(root, f"ontology/object/{name}.md", obj(name, "合成对象"))
    run = _paused_old_run(project_id, root)
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    paused = client.get(base).json()
    assert paused["state"] == "paused" and paused["stages"] == [{"id": "object", "label": "对象"},
                                                               {"id": "operation", "label": "操作"}]
    assert [(row["slice_id"], row["cells"]) for row in paused["matrix"]] == [
        ("all", {"object": "auto", "operation": "queued"}), ("core.validate", {})]
    resumed = client.post(f"{base}/resume")
    assert resumed.status_code == 200, resumed.text
    done = wait_state(client, project_id, run["run_id"], "completed")
    # 接着做这一段没做的操作，再做校验；对象那一步不重做
    assert [text for text in AutoEngine.sent if text.startswith("/ontology-")] == [
        "/ontology-extract-operation", "/ontology-validate"]
    assert (root / "ontology" / "operation" / "操作5.md").is_file()
    assert any(hint.startswith("自主构建 · 运行 #1 · 全部（1/2）") for hint in AutoEngine.hints)
    assert done["slices"] == {"done": 2, "total": 2}
    assert [(row["slice_id"], row["state"], row["cells"]) for row in done["matrix"]] == [
        ("all", "done", {"object": "auto", "operation": "auto"}), ("core.validate", "done", {})]
    report = client.get(f"{base}/report").json()
    assert [(row["slice_id"], row["result"], row["revertible"]) for row in report["slices"]] == [
        ("all", "done", True), ("core.validate", "done", True)]
    from tests.test_changesets import git

    assert any(line.startswith("autobuild: 自主构建运行 #1「全部」")
               for line in git(root, "log", "--format=%s").splitlines())


def test_an_old_run_that_only_did_validation_still_shows_the_finished_slice(tmp_path, monkeypatch):
    """老运行开始时建模已经做完（「全部」不在运行里，只跑了校验）：详情照旧先列一行「运行前已完成」的建模，再列校验。"""
    from app import config
    from app.services.autobuild import views

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    validate = {"id": "core.validate", "name": "校验", "kind": "node",
                "steps": [{"id": "core.validate", "label": "校验", "title": "校验", "action": "校验",
                           "command": "/ontology-validate", "args": {}}]}
    run = {"project_id": "task-x", "run_id": "run-1", "run_no": 1, "state": "completed", "conversation_id": "c1",
           "policy": {}, "units": [validate], "ledger": [], "suspensions": [], "budget": {},
           "before_rows": [{"id": "all", "name": "全部", "group": "done",
                            "stages": {"object": "confirmed", "operation": "auto"}}],
           "slices": {"core.validate": {"state": "done", "commit": "c" * 40, "changeset_id": "cs-1"}}}
    view = views.run_view(run)
    assert view["stages"] == [{"id": "object", "label": "对象"}, {"id": "operation", "label": "操作"}]
    assert [(row["slice_id"], row["result"], row["cells"]) for row in view["matrix"]] == [
        ("all", "运行前已完成", {"object": "confirmed", "operation": "confirmed"}), ("core.validate", "完成", {})]
    assert view["slices"] == {"done": 1, "total": 1}


def test_an_old_slice_is_not_held_back_by_later_steps(ab):
    """老运行按段的一段整体撤回：后面跟着的节点单元（校验用到对象、操作）合入了也不挡；节点单元之间照样倒序。"""
    from app.services.autobuild import review

    _client, _workspaces, _project_id, root = ab
    validate = {"id": "core.validate", "name": "校验", "kind": "node", "steps": []}
    trace = {"id": "trace.trace", "name": "场景模拟", "kind": "node", "steps": []}
    run = {"units": [{"id": "all", "name": "全部", "kind": "slice", "steps": OLD_STEPS}, validate, trace],
           "slices": {"all": {"commit": "a" * 40}, "core.validate": {"commit": "b" * 40},
                      "trace.trace": {"commit": "c" * 40}}}
    assert review._later_user(run, run["units"][0], root) is None
    assert review._later_user(run, validate, root) is None  # 场景模拟不看校验报告
    node_run = {**run, "units": [{"id": "core.operation", "name": "提取操作", "kind": "node", "steps": []}, validate,
                                 trace],
                "slices": {**run["slices"], "core.operation": {"commit": "d" * 40}}}
    assert review._later_user(node_run, node_run["units"][0], root) == trace
    node_run["slices"]["trace.trace"]["reverted"] = {"commit": "e" * 40}
    assert review._later_user(node_run, node_run["units"][0], root) == validate
