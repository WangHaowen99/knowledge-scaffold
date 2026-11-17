"""自主构建的复核、挂起与撤回（详设 8.5、4.7、13 章 P3 验收；契约 AB-10～AB-17、IN-01～IN-04）。

F8 起不分段：一次运行只有一段「全部」，撤回就是撤回这次运行合入的修改。
- 挂起的问题与需复核的产出都进待处理（自主构建复核）：低于阈值的一条一项，高置信的合成一项，挂起的一个一项；
- 接受、一键接受高置信、改判、回答挂起问题；复核完去掉「需复核」；
- 撤回这一段：反向提交，运行前已有的条目不受影响；合入之后又被改过的条目挡住撤回；
- 检查修了几轮还不过：照样合入、标需复核，不挂起（F3）。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

from app.kernel.changesets import gates
from tests.autobuild_helpers import (AutoEngine, ab, ask, history, op, start, wait_state, wait_until)  # noqa: F401
from tests.test_changesets import git, main_write, obj

MODEL_ONLY = {"scope": {"until_node": "core.operation"}, "advanced": {"must_suspend_kinds": ["新增对象"]}}


def _inbox(client, project_id: str, **params) -> dict:
    return client.get(f"/api/v1/projects/{project_id}/inbox", params=params).json()


def test_suspensions_and_reviews_go_to_the_inbox_and_get_resolved(ab):
    client, _workspaces, project_id, _root = ab
    AutoEngine.hooks["/ontology-extract-operation:all"] = [ask([
        {"question": "审批提示交给谁", "header": "新增对象 · 置信度 0.9", "options": [{"label": "新增区域审批单（推荐）"},
                                                                         {"label": "挂到补货台账"}]},
        {"question": "临期按几级分", "header": "置信度 0.6", "options": [{"label": "四级（推荐）"},
                                                                     {"label": "三级（保守）"}]},
        {"question": "同一商品多类出现时取哪个", "header": "置信度 0.95", "options": [{"label": "取最严（推荐）"},
                                                                              {"label": "取第一个"}]}])]
    AutoEngine.hooks["/ontology-extract-object:all"] = [ask([
        {"question": "超量比较用哪个上限", "header": "置信度 0.85", "options": [{"label": "逐类取（推荐）"},
                                                                         {"label": "统一配额"}]}])]
    run = start(client, project_id, **MODEL_ONLY)
    wait_state(client, project_id, run["run_id"], "completed")
    body = _inbox(client, project_id, filter="autobuild_review")
    by_kind = {}
    for item in body["items"]:
        by_kind.setdefault(item["kind"], []).append(item)
    assert body["counts"]["by_filter"]["autobuild_review"] == 3
    reviews = by_kind.get("review", [])
    assert reviews and all("high_confidence" in item for item in reviews), by_kind
    low, = [item for item in reviews if not item.get("high_confidence")]
    high, = [item for item in reviews if item.get("high_confidence")]
    suspended, = by_kind.get("suspension", [])
    assert low["title"] == "全部：三级" and low["source"] == "autobuild" and low["slice"]["id"] == "all"
    assert low["actions"] == ["accept", "rejudge", "revert_slice", "dismiss"]
    assert high["id"] == f"review:{run['run_id']}:high" and high["actions"] == ["accept", "dismiss"]
    assert suspended["title"] == "审批提示交给谁" and suspended["actions"] == ["answer", "use_generic", "dismiss"]
    summary = body["autobuild_summary"]
    assert summary["run_id"] == run["run_id"] and summary["decisions"] == 3 and summary["suspended"] == 1
    assert summary["high_confidence_pending"] == 2 and summary["assumptions"] == 1
    # 详情：台账条目加版本；高置信合并项；挂起的问题
    detail = client.get(f"/api/v1/projects/{project_id}/inbox/{low['id']}").json()["detail"]
    assert detail["question"] == "临期按几级分" and detail["chosen"] == "三级" and detail["assumption"] is True
    assert detail["base_version_label"] and detail["slice_version_label"]
    group = client.get(f"/api/v1/projects/{project_id}/inbox/{high['id']}").json()["detail"]
    assert group["group"] == "high_confidence" and len(group["entries"]) == 2 and group["threshold"] == 0.8
    sus = client.get(f"/api/v1/projects/{project_id}/inbox/{suspended['id']}").json()["detail"]
    assert sus["kind"] == "question" and [option["label"] for option in sus["options"]] == [
        "新增区域审批单（推荐）", "挂到补货台账"]
    # 「自主完成」：复核之前建模节点显示 auto
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    states = {node["id"]: node["display_state"] for node in flow["nodes"]}
    assert states["core.object"] == states["core.operation"] == "auto"
    rows = {row["name"]: row for row in client.get(f"/api/v1/projects/{project_id}/studio/entries").json()["items"]}
    assert rows["操作2"]["needs_review"] is True and rows["对象2"]["needs_review"] is True
    detail_entry = client.get(f"/api/v1/projects/{project_id}/studio/entries/{rows['操作2']['id']}").json()
    assert detail_entry["needs_review"] == {"source": "autobuild", "run_id": run["run_id"]}
    # 一键接受高置信（IN-04）
    accepted = client.post(f"/api/v1/projects/{project_id}/inbox/batch", json={"action": "accept_high_confidence"}).json()
    assert accepted["accepted"] == 2
    # 接受低于阈值的那一条（IN-03 → AB-11）：都复核完，「需复核」去掉
    done = client.post(f"/api/v1/projects/{project_id}/inbox/{low['id']}/actions", json={"action": "accept"}).json()
    assert done["resolved"] is True and done["result"]["entry"]["review_state"] == "accepted"
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    for node_id in ("core.object", "core.operation"):
        model = next(node for node in flow["nodes"] if node["id"] == node_id)
        assert model["flags"]["needs_review"] is False and model["display_state"] != "auto"
    rows = {row["name"]: row for row in client.get(f"/api/v1/projects/{project_id}/studio/entries").json()["items"]}
    assert not any(row["needs_review"] for row in rows.values())
    # 回答挂起的问题（IN-03 → AB-15）：运行结束了，往运行对话发这一步的命令补上（全量模式不带分段）
    answered = client.post(f"/api/v1/projects/{project_id}/inbox/{suspended['id']}/actions",
                           json={"action": "answer", "option_id": sus["options"][1]["id"]}).json()
    assert answered["resolved"] is True and answered["result"]["suspension"]["state"] == "answered"
    assert answered["result"]["command_task"]["title"] == "补上：审批提示交给谁"
    wait_until(lambda: any(text.startswith("/ontology-extract-operation\n\n「审批提示交给谁」的回答：挂到补货台账")
                           for text in AutoEngine.sent))
    assert _inbox(client, project_id, filter="autobuild_review")["counts"]["by_filter"]["autobuild_review"] == 0
    ledger = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger",
                        params={"review_state": "pending"}).json()
    assert ledger["items"] == [] and ledger["counts"]["total"] == 3


def test_rejudge_during_a_run_is_queued_until_it_ends(ab):
    client, _workspaces, project_id, _root = ab
    from tests.autobuild_helpers import gate

    AutoEngine.hooks["/ontology-extract-object:all"] = [ask([
        {"question": "门店编号几位", "header": "置信度 0.7", "options": [{"label": "四位（推荐）"}, {"label": "六位"}]},
        {"question": "要不要新增门店分组", "header": "新增对象", "options": [{"label": "要"}, {"label": "不要"}]}])]
    release = gate("/ontology-extract-operation:all")
    run = start(client, project_id, **MODEL_ONLY)
    entry = wait_until(lambda: client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger")
                       .json()["items"])[0]
    queued = client.post(f"/api/v1/projects/{project_id}/autobuild/ledger/{entry['id']}/rejudge",
                         json={"option_id": entry["options"][1]["id"]})
    assert queued.status_code == 202
    assert queued.json()["command_task"]["state"] == "queued" and queued.json()["entry"]["rejudged_to"] == "六位"
    # 运行中回答挂起的问题：同样排到运行结束后补上
    suspended = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/suspensions").json()["items"][0]
    answered = client.post(f"/api/v1/projects/{project_id}/autobuild/suspensions/{suspended['id']}/answer",
                           json={"custom_text": "先不分组"}).json()
    assert answered["suspension"]["state"] == "answered" and answered["command_task"]["state"] == "queued"
    assert client.post(f"/api/v1/projects/{project_id}/autobuild/suspensions/{suspended['id']}/answer",
                       json={"custom_text": "再答一次"}).json()["code"] == "suspension_not_found"
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")
    wait_until(lambda: any(text.startswith("/ontology-extract-object\n\n改判：「门店编号几位」") for text in AutoEngine.sent))
    wait_until(lambda: any("「要不要新增门店分组」的回答：先不分组" in text for text in AutoEngine.sent))
    bad = client.post(f"/api/v1/projects/{project_id}/autobuild/ledger/led-missing/accept")
    assert bad.status_code == 404 and bad.json()["code"] == "ledger_entry_not_found"


def test_reverting_the_run_restores_what_was_there_before(ab):
    """按段撤回在全量模式下就是撤回这次运行合入的那一段：运行前已有的条目不动；合入后又被改过的条目挡住撤回。"""
    client, _workspaces, project_id, root = ab
    # 运行前已有对象：对象那一步算做过，这次运行只做操作
    main_write(root, "ontology/object/对象9.md", obj("对象9", "运行前就有的合成对象"))
    run = start(client, project_id, scope={"until_node": "core.operation"})
    wait_state(client, project_id, run["run_id"], "completed")
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    # 合入之后这一段的条目又被改过：不能撤回；改回原样后可以
    main_write(root, "ontology/operation/操作2.md", "# 操作2\n\n## 业务描述\n\n人改过的合成操作\n")
    blocked = client.post(f"{base}/slices/all/revert")
    assert blocked.status_code == 409 and blocked.json()["details"]["reason"] == "later_changes"
    main_write(root, "ontology/operation/操作2.md", op("操作2"))
    before = git(root, "rev-list", "--first-parent", "--count", "HEAD").strip()
    reverted = client.post(f"{base}/slices/all/revert")
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["reverted_to_version_label"] == run["base_version_label"] and body["commit"]
    assert int(git(root, "rev-list", "--first-parent", "--count", "HEAD").strip()) == int(before) + 1
    assert git(root, "log", "-1", "--format=%s").startswith("revert: 撤回")
    # 这次运行写的操作都没了，运行前就有的对象还在
    assert not any((root / "ontology" / "operation" / f"操作{n}.md").exists() for n in range(1, 6))
    assert (root / "ontology" / "object" / "对象9.md").is_file()
    report = client.get(f"{base}/report").json()
    row = next(item for item in report["slices"] if item["slice_id"] == "all")
    assert row["reverted"] is True and row["revertible"] is False and row["result_text"].startswith("已回退到运行前")
    again = client.post(f"{base}/slices/all/revert")
    assert again.status_code == 409 and again.json()["details"]["reason"] == "already_reverted"
    assert client.post(f"{base}/slices/s3/revert").json()["details"]["reason"] == "not_in_run"
    cards = [item["data"] for item in history(client, project_id, run["conversation_id"]) if item["type"] == "card"]
    report_card = next(card for card in cards if card["card_type"] == "run_report")
    assert next(row for row in report_card["slices"] if row["slice_id"] == "all")["reverted"] is True


def test_check_errors_are_fixed_a_few_rounds_then_merged_anyway(ab, monkeypatch):
    """F3：检查只提示不拦——合入前有新错误先让助手修（最多策略里的轮数），还有就照样合入、标需复核，不再挂起这一段。"""
    client, _workspaces, project_id, root = ab
    finding = {"level": "error", "code": "RULE-01", "message": "合成错误", "path": "ontology/object/对象1.md"}
    calls = {"n": 0}

    def compare(main_tree, *args, **kwargs):
        from app.services.autobuild import store

        calls["n"] += 1
        if (store.get(project_id, "run-1") or {}).get("cursor", {}).get("unit") == "all":
            return {"errors": 1, "warnings": 0, "findings": [finding]}
        return {"errors": 0, "warnings": 0, "findings": []}

    monkeypatch.setattr(gates, "compare", compare)
    run = start(client, project_id, scope={"until_node": "core.operation"}, advanced={"max_fix_rounds": 2})
    done = wait_state(client, project_id, run["run_id"], "completed")
    fixes = [text for text in AutoEngine.sent if text.startswith("合入前的检查发现 1 个新问题")]
    assert len(fixes) == 2 and "合成错误" in fixes[0]
    assert done["slices"] == {"done": 1, "total": 1}
    assert (root / "ontology" / "object" / "对象1.md").is_file() and (root / "ontology" / "object" / "对象2.md").is_file()
    suspensions = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/suspensions").json()["items"]
    assert suspensions == []


def test_a_failed_run_does_not_leave_the_slice_waiting_for_confirmation(ab, monkeypatch):
    """F3：停在一段中间时这一段照样合入；合不进去（这里是合入本身出错）的留在变更集里，下一次收尾时合入，
    不进待处理的「修改待确认」。"""
    client, workspaces, project_id, root = ab
    from app.services.workspace.errors import WorkspaceError

    def unavailable(*args, **kwargs):
        raise WorkspaceError("门禁跑不起来", 503, "service_unavailable", {"dependency": "gate"})

    from app.kernel.changesets import service as changeset_service

    real_merge = changeset_service.merge
    monkeypatch.setattr("app.kernel.changesets.service.merge", unavailable)
    run = start(client, project_id, scope={"until_node": "core.operation"})
    failed = wait_state(client, project_id, run["run_id"], "failed")
    assert failed["matrix"][0]["result"] in ("未开始", "部分完成")
    assert not [item for item in _inbox(client, project_id)["items"] if item["kind"] == "pending_changeset"]
    from app.kernel.changesets import service as changesets

    monkeypatch.setattr(changesets, "merge", real_merge)

    out = changesets.settle(workspaces, project_id, actor="employee_0001")
    assert out["state"] == "merged" and out["record"]["needs_review"] is True
    assert (root / "ontology" / "object" / "对象1.md").is_file()
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["state"] == "none"
