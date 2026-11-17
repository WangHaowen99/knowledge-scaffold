"""I1 合回交付分支的衔接。

I1 新加的发命令入口经会话池同一道运行中拦截；自主构建的通知进通知中心（落盘、同组替换）；
自主构建的写入（台账、报告、按段撤回的反向提交）与 I1 的符号链接防护（每轮核对、提交检查）互不误伤。
"""

from __future__ import annotations

import os

from app.services import notifications
from app.services.autobuild import store
from tests.autobuild_helpers import (AutoEngine, ab, gate, run_of, start, wait_state,  # noqa: F401
                                     wait_until)
from tests.test_changesets import git, main_write

ONE = {"scope": {"until_node": "core.operation"}}
OWNER = "employee_0001"


def test_i1_command_entries_are_blocked_during_a_run(ab):
    client, _workspaces, project_id, root = ab
    main_write(root, "reports/20261102-120000-all-rule.md", "# 规则检查报告\n\n## 错误\n\n- [ ] E-01 合成问题\n", "web: 报告")
    release = gate("/ontology-extract-object:all")
    run = start(client, project_id, **ONE)
    wait_until(lambda: "/ontology-extract-object" in AutoEngine.sent)
    sent = len(AutoEngine.sent)
    base = f"/api/v1/projects/{project_id}"
    for resp in (client.post(f"{base}/reports/20261102-120000-all-rule/fix", json={}),
                 client.post(f"{base}/reports/recheck", json={"kind": "rule"})):
        assert resp.status_code == 409 and resp.json()["code"] == "autobuild_running", resp.text[:200]
    assert len(AutoEngine.sent) == sent
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")


def test_autobuild_notifications_go_to_the_notification_center(ab):
    client, _workspaces, project_id, _root = ab
    run = start(client, project_id, **ONE)
    wait_state(client, project_id, run["run_id"], "completed")
    stored = notifications.list_notifications(OWNER)
    finished = [item for item in stored if item["kind"] == "autobuild_finished"]
    assert finished and finished[0]["project_id"] == project_id
    assert finished[0]["action"]["page"] == "inbox"
    assert notifications.unread_count(OWNER) >= 1


def test_autobuild_writes_and_symlink_guards_coexist(ab, tmp_path):
    client, _workspaces, project_id, root = ab
    outside = tmp_path / "outside.md"
    outside.write_text("# 外面\n", encoding="utf-8")
    os.symlink(str(outside), root / "ontology" / "object" / "外链.md")  # 每轮核对会删掉指到根外的链接
    run = start(client, project_id, scope={"until_node": "core.operation"})
    wait_state(client, project_id, run["run_id"], "completed")
    assert not os.path.lexists(root / "ontology" / "object" / "外链.md")
    record = store.get(project_id, run["run_id"])
    report_files = git(root, "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", "HEAD", "reports/autobuild/").split()
    run_no = run["run_id"].rsplit("-", 1)[-1]
    assert f"reports/autobuild/{run_no}/report.json" in report_files and f"reports/autobuild/{run_no}/ledger.json" in report_files
    assert all(info.get("commit") for info in record["slices"].values() if info.get("state") == "merged")
    # 按段撤回写反向提交：不被提交检查当成链接拦下
    reverted = client.post(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/slices/all/revert")
    assert reverted.status_code == 200, reverted.text
    assert git(root, "log", "-1", "--format=%s").startswith("revert: 撤回")
    assert run_of(client, project_id, run["run_id"])["state"] == "completed"


def test_suspension_notifications_replace_within_a_run(tmp_path, monkeypatch):
    """同一次运行的「新增挂起问题」没读时只留最新一条（和会话通知同一套 replaces），正文是还没回答的总数。"""
    from app import config
    from app.services import auth, streams
    from app.services.autobuild import runner as runner_module

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", True)
    pushed = []
    monkeypatch.setattr(streams, "publish_user", lambda user, kind, data, project_id=None: pushed.append(data))
    monkeypatch.setattr(runner_module, "_ws", lambda: type("W", (), {"get_project": staticmethod(lambda pid: {"name": "合成"})})())
    run = {"project_id": "task-x", "run_id": "run-1", "conversation_id": "c1", "owner": "alice", "state": "running",
           "ledger": [], "suspensions": [], "slices": {}}
    monkeypatch.setattr(runner_module.views, "summary", lambda r: {"needs_review": 0, "suspended": len(pushed) + 1})
    notify = runner_module.AutobuildRunner.__new__(runner_module.AutobuildRunner)._notify
    notify(run, "autobuild_suspension", count=1)
    notify(run, "autobuild_suspension", count=1)
    stored = notifications.list_notifications("alice")
    assert len(stored) == 1 and stored[0]["body"] == "2 个"
    assert pushed[1]["replaces"] == [pushed[0]["id"]]


def test_pause_and_resume_refresh_the_run_suggestions(ab):
    """暂停后运行对话的建议条只有「运行详情」，续跑后回到三个控制（不再停在暂停前的那一份）。"""
    from tests.autobuild_helpers import history

    client, _workspaces, project_id, _root = ab
    release = gate("/ontology-extract-object:all")
    run = start(client, project_id, **ONE)
    wait_until(lambda: "/ontology-extract-object" in AutoEngine.sent)
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    assert client.post(f"{base}/pause").status_code == 200
    release.set()
    wait_state(client, project_id, run["run_id"], "paused")

    def last_bar():
        bars = [event["data"] for event in history(client, project_id, run["conversation_id"])
                if event["type"] == "suggestions.updated"]
        return bars[-1] if bars else {}

    wait_until(lambda: last_bar().get("primary") is None and last_bar().get("mode") == "autobuild_controls")
    assert [item["id"] for item in last_bar()["secondary"]] == ["autobuild_details"]
    assert client.post(f"{base}/resume").status_code == 200
    wait_until(lambda: (last_bar().get("primary") or {}).get("id") == "pause" or
               run_of(client, project_id, run["run_id"])["state"] == "completed")
    wait_state(client, project_id, run["run_id"], "completed")
