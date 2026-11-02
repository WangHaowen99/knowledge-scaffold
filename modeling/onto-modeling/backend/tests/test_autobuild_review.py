"""自主构建的复核、挂起与按片撤回（详设 8.5、4.7、13 章 P3 验收；契约 AB-10～AB-17、IN-01～IN-04）。

- 挂起的问题与需复核的产出都进待处理（自主构建复核）：低于阈值的一条一项，高置信的合成一项，挂起的一个一项；
- 接受、一键接受高置信、改判、回答挂起问题；复核完的片去掉「需复核」；
- 撤回其中一片：反向提交，别的片不受影响；合入之后又被改过的片不能单独撤回；
- 门禁修了几轮还不过：这一片的修改撤回、挂起，接着做下一片。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

from app.kernel.changesets import gates
from tests.autobuild_helpers import (AutoEngine, ab, ask, history, start, wait_state, wait_until)  # noqa: F401
from tests.test_changesets import git, main_write, obj

THREE = {"scope": {"mode": "selected", "slice_ids": ["s1", "s2", "s3"], "until_node": "core.model"},
         "budget": {"max_tokens": 500_000},
         "advanced": {"must_suspend_kinds": ["新增对象"]}}


def _inbox(client, project_id: str, **params) -> dict:
    return client.get(f"/api/v1/projects/{project_id}/inbox", params=params).json()


def test_suspensions_and_reviews_go_to_the_inbox_and_get_resolved(ab):
    client, _workspaces, project_id, _root = ab
    AutoEngine.hooks["/ontology-extract-operation:s2"] = [ask([
        {"question": "审批提示交给谁", "header": "新增对象 · 置信度 0.9", "options": [{"label": "新增区域审批单（推荐）"},
                                                                         {"label": "挂到补货台账"}]},
        {"question": "临期按几级分", "header": "置信度 0.6", "options": [{"label": "四级（推荐）"},
                                                                     {"label": "三级（保守）"}]},
        {"question": "同一商品多类出现时取哪个", "header": "置信度 0.95", "options": [{"label": "取最严（推荐）"},
                                                                              {"label": "取第一个"}]}])]
    AutoEngine.hooks["/ontology-extract-operation:s3"] = [ask([
        {"question": "超量比较用哪个上限", "header": "置信度 0.85", "options": [{"label": "逐类取（推荐）"},
                                                                         {"label": "统一配额"}]}])]
    run = start(client, project_id, **THREE)
    wait_state(client, project_id, run["run_id"], "completed")
    body = _inbox(client, project_id, filter="autobuild_review")
    by_kind = {}
    for item in body["items"]:
        by_kind.setdefault(item["kind"], []).append(item)
    assert body["counts"]["by_filter"]["autobuild_review"] == 3
    low, = [item for item in by_kind["review"] if not item["high_confidence"]]
    high, = [item for item in by_kind["review"] if item["high_confidence"]]
    suspended, = by_kind["suspension"]
    assert low["title"] == "合成片2：三级" and low["source"] == "autobuild" and low["slice"]["id"] == "s2"
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
    # 切片「自主完成」：复核之前 s2 的格子是 auto
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    assert {item["id"]: item for item in flow["slices"]}["s2"]["stages"]["operation"] == "auto"
    rows = {row["name"]: row for row in client.get(f"/api/v1/projects/{project_id}/studio/entries").json()["items"]}
    assert rows["操作2"]["needs_review"] is True and rows["对象2"]["needs_review"] is True
    detail_entry = client.get(f"/api/v1/projects/{project_id}/studio/entries/{rows['操作2']['id']}").json()
    assert detail_entry["needs_review"] == {"source": "autobuild", "run_id": run["run_id"]}
    # 一键接受高置信（IN-04）
    accepted = client.post(f"/api/v1/projects/{project_id}/inbox/batch", json={"action": "accept_high_confidence"}).json()
    assert accepted["accepted"] == 2
    # 接受低于阈值的那一条（IN-03 → AB-11）：s2、s3 都复核完，「需复核」去掉
    done = client.post(f"/api/v1/projects/{project_id}/inbox/{low['id']}/actions", json={"action": "accept"}).json()
    assert done["resolved"] is True and done["result"]["entry"]["review_state"] == "accepted"
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    slices = {item["id"]: item for item in flow["slices"]}
    assert slices["s2"]["stages"]["operation"] == "confirmed" and slices["s1"]["stages"]["object"] == "confirmed"
    model = next(node for node in flow["nodes"] if node["id"] == "core.model")
    assert model["flags"]["needs_review"] is False
    rows = {row["name"]: row for row in client.get(f"/api/v1/projects/{project_id}/studio/entries").json()["items"]}
    assert not any(row["needs_review"] for row in rows.values())
    # 回答挂起的问题（IN-03 → AB-15）：运行结束了，往运行对话发这一片这一步的命令补上
    answered = client.post(f"/api/v1/projects/{project_id}/inbox/{suspended['id']}/actions",
                           json={"action": "answer", "option_id": sus["options"][1]["id"]}).json()
    assert answered["resolved"] is True and answered["result"]["suspension"]["state"] == "answered"
    assert answered["result"]["command_task"]["title"] == "补上：审批提示交给谁"
    wait_until(lambda: any(text.startswith("/ontology-extract-operation slice=s2\n\n「审批提示交给谁」的回答：挂到补货台账")
                           for text in AutoEngine.sent))
    assert _inbox(client, project_id, filter="autobuild_review")["counts"]["by_filter"]["autobuild_review"] == 0
    ledger = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/ledger",
                        params={"review_state": "pending"}).json()
    assert ledger["items"] == [] and ledger["counts"]["total"] == 3


def test_rejudge_during_a_run_is_queued_until_it_ends(ab):
    client, _workspaces, project_id, _root = ab
    from tests.autobuild_helpers import gate

    AutoEngine.hooks["/ontology-extract-object:s1"] = [ask([
        {"question": "门店编号几位", "header": "置信度 0.7", "options": [{"label": "四位（推荐）"}, {"label": "六位"}]},
        {"question": "要不要新增门店分组", "header": "新增对象", "options": [{"label": "要"}, {"label": "不要"}]}])]
    release = gate("/ontology-extract-operation:s1")
    run = start(client, project_id, **THREE)
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
    wait_until(lambda: any(text.startswith("/ontology-extract-object slice=s1\n\n改判：「门店编号几位」") for text in AutoEngine.sent))
    wait_until(lambda: any("「要不要新增门店分组」的回答：先不分组" in text for text in AutoEngine.sent))
    bad = client.post(f"/api/v1/projects/{project_id}/autobuild/ledger/led-missing/accept")
    assert bad.status_code == 404 and bad.json()["code"] == "ledger_entry_not_found"


def test_reverting_one_slice_leaves_the_others_alone(ab):
    client, _workspaces, project_id, root = ab
    run = start(client, project_id, scope={"until_node": "core.model"}, budget={"max_tokens": 500_000})
    wait_state(client, project_id, run["run_id"], "completed")
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    before = git(root, "rev-list", "--first-parent", "--count", "HEAD").strip()
    reverted = client.post(f"{base}/slices/s3/revert")
    assert reverted.status_code == 200, reverted.text
    body = reverted.json()
    assert body["reverted_to_version_label"] == run["base_version_label"] and body["commit"]
    assert int(git(root, "rev-list", "--first-parent", "--count", "HEAD").strip()) == int(before) + 1
    assert git(root, "log", "-1", "--format=%s").startswith("revert: 撤回")
    # 只动这一片：s3 的条目没了，别的片都在
    assert not (root / "ontology" / "object" / "对象3.md").exists()
    assert not (root / "ontology" / "operation" / "操作3.md").exists()
    assert not (root / "ontology" / "testcase" / "操作3.md").exists()
    for n in (1, 2, 4, 5):
        assert (root / "ontology" / "object" / f"对象{n}.md").is_file()
        assert (root / "ontology" / "operation" / f"操作{n}.md").is_file()
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    slices = {item["id"]: item for item in flow["slices"]}
    assert slices["s3"]["group"] == "todo" and slices["s3"]["stages"]["object"] == "ready"
    assert all(slices[f"s{n}"]["group"] == "done" for n in (1, 2, 4, 5))
    report = client.get(f"{base}/report").json()
    row = next(item for item in report["slices"] if item["slice_id"] == "s3")
    assert row["reverted"] is True and row["revertible"] is False and row["result_text"].startswith("已回退到运行前")
    again = client.post(f"{base}/slices/s3/revert")
    assert again.status_code == 409 and again.json()["details"]["reason"] == "already_reverted"
    # 合入之后这一片的条目又被改过：不能单独撤回
    main_write(root, "ontology/object/对象2.md", obj("对象2", "人改过的合成对象"))
    blocked = client.post(f"{base}/slices/s2/revert")
    assert blocked.status_code == 409 and blocked.json()["details"]["reason"] == "later_changes"
    cards = [item["data"] for item in history(client, project_id, run["conversation_id"]) if item["type"] == "card"]
    report_card = next(card for card in cards if card["card_type"] == "run_report")
    assert next(row for row in report_card["slices"] if row["slice_id"] == "s3")["reverted"] is True


def test_gate_failures_are_fixed_or_the_slice_is_suspended(ab, monkeypatch):
    client, _workspaces, project_id, root = ab
    finding = {"level": "error", "code": "RULE-01", "message": "合成错误", "path": "ontology/object/对象1.md"}
    calls = {"n": 0}

    def compare(main_tree, *args, **kwargs):
        from app.services.autobuild import store

        calls["n"] += 1
        if (store.get(project_id, "run-1") or {}).get("cursor", {}).get("unit") == "s1":
            return {"errors": 1, "warnings": 0, "findings": [finding]}
        return {"errors": 0, "warnings": 0, "findings": []}

    monkeypatch.setattr(gates, "compare", compare)
    run = start(client, project_id, scope={"mode": "selected", "slice_ids": ["s1", "s2"], "until_node": "core.model"},
                budget={"max_tokens": 500_000}, advanced={"max_fix_rounds": 2})
    done = wait_state(client, project_id, run["run_id"], "completed")
    fixes = [text for text in AutoEngine.sent if text.startswith("确认生效前的检查发现 1 个新错误")]
    assert len(fixes) == 2 and "合成错误" in fixes[0]
    assert done["matrix"][0]["result"] == "检查没通过" and done["slices"] == {"done": 1, "total": 2}
    assert not (root / "ontology" / "object" / "对象1.md").exists() and (root / "ontology" / "object" / "对象2.md").is_file()
    suspensions = client.get(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/suspensions").json()["items"]
    assert [item["title"] for item in suspensions] == ["「合成片1」检查没通过"]
    assert suspensions[0]["handled_as"] == "这一片没生效，先做下一片"


def test_a_failed_run_leaves_the_slice_for_the_user_to_confirm(ab, monkeypatch):
    client, workspaces, project_id, _root = ab
    from app.services.workspace.errors import WorkspaceError

    def unavailable(*args, **kwargs):
        raise WorkspaceError("门禁跑不起来", 503, "service_unavailable", {"dependency": "gate"})

    monkeypatch.setattr("app.kernel.changesets.service.merge", unavailable)
    run = start(client, project_id, scope={"mode": "selected", "slice_ids": ["s1"], "until_node": "core.model"},
                budget={"max_tokens": 500_000})
    failed = wait_state(client, project_id, run["run_id"], "failed")
    assert failed["matrix"][0]["result"] in ("未开始", "部分完成")
    # 这一片的修改留给用户：待处理里「修改待确认」，之后的对话照常
    pending = [item for item in _inbox(client, project_id)["items"] if item["kind"] == "pending_changeset"]
    assert len(pending) == 1
    assert client.get(f"/api/v1/projects/{project_id}/scope").json()["state"] == "pending_confirm"
