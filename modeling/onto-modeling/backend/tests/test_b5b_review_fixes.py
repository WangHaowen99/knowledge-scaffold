"""B5b 独立审查修复（A～M 与推断几条；复现脚本见审查报告）：每条一个修复前会失败的用例。

假引擎按消息现做（tests/autobuild_helpers.py），等待一律按条件。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.api.errors import ApiError
from app.services import agent_sessions
from app.services.autobuild import store
from tests.autobuild_helpers import (AutoEngine, ab, ask, gate, run_of, start, wait_state,  # noqa: F401
                                     wait_until)
from tests.test_changesets import git, main_write, obj

ONE = {"scope": {"mode": "selected", "slice_ids": ["s1"], "until_node": "core.model"}, "budget": {"max_tokens": 500_000}}


def _apply(rel: str, text: str):
    def action(ctx):
        path = Path(ctx.cwd) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return action


def _delete(rel: str):
    def action(ctx):
        (Path(ctx.cwd) / rel).unlink(missing_ok=True)
    return action


# ---------------------------------------------------------------------------
# A：运行中别的入口绕过 409（Studio 转助手复核、取消排队、放弃执行、会话池里直接发）
# ---------------------------------------------------------------------------


def test_A_internal_entry_points_cannot_send_during_a_run(ab):
    client, _workspaces, project_id, root = ab
    main_write(root, "ontology/object/门店.md", obj("门店", "原始描述"), "web: 门店")
    release = gate("/ontology-extract-object:s1")
    run = start(client, project_id, **ONE)
    wait_until(lambda: store.get(project_id, run["run_id"])["cursor"].get("exec_id"))
    sent = len(AutoEngine.sent)
    base = f"/api/v1/projects/{project_id}/studio/entries/object:门店"
    edited = obj("门店", "用户在 Studio 改的描述") + "\n## 备注\n\n用户补充\n"
    assert client.put(f"{base}/draft", json={"content": edited}).status_code == 200
    submitted = client.post(f"{base}/submit", json={})
    assert submitted.status_code == 409 and submitted.json()["code"] == "autobuild_running"
    cancel = client.post(f"/api/v1/projects/{project_id}/conversations/{run['conversation_id']}/queue/cancel")
    assert cancel.json()["code"] == "autobuild_running"
    exec_id = store.get(project_id, run["run_id"])["cursor"]["exec_id"]
    assert client.post(f"/api/v1/projects/{project_id}/execs/{exec_id}/abandon").json()["code"] == "autobuild_running"

    # 会话池这一关：没有运行器豁免的发送一律 409（新加的内部入口也漏不掉）
    async def direct():
        await agent_sessions.manager.send(project_id, root, "你好", run["conversation_id"], username="employee_0001")

    with pytest.raises(ApiError) as caught:
        client.portal.call(direct)
    assert caught.value.code == "autobuild_running"
    assert len(AutoEngine.sent) == sent
    release.set()
    wait_state(client, project_id, run["run_id"], "completed")
    record = store.get(project_id, run["run_id"])
    files = git(root, "-c", "core.quotePath=false", "show", "--name-only", "--format=", record["slices"]["s1"]["commit"])
    assert "ontology/object/门店.md" not in files.split()


# ---------------------------------------------------------------------------
# B：硬性不做的「删除已确认内容」、发布标签
# ---------------------------------------------------------------------------


def test_B_autobuild_never_deletes_confirmed_content(ab):
    client, _workspaces, project_id, root = ab
    main_write(root, "ontology/object/门店.md", obj("门店", "已确认的对象"), "web: 门店")
    AutoEngine.hooks["/ontology-validate"] = [_delete("ontology/object/门店.md")]
    run = start(client, project_id, scope={"mode": "selected", "slice_ids": ["s1"], "until_node": "core.validate"},
                budget={"max_tokens": 500_000})
    wait_state(client, project_id, run["run_id"], "completed")
    # 合入时发现删了主线上已有的条目：这一步不生效，挂起，主线上的文件还在
    assert (root / "ontology/object/门店.md").is_file()
    assert not git(root, "log", "--format=%h", "--diff-filter=D", "--", "ontology/object/门店.md").strip()
    record = store.get(project_id, run["run_id"])
    assert record["slices"]["core.validate"]["state"] == "skipped"
    titles = [item["title"] for item in record["suspensions"]]
    assert titles == ["「校验与走查」要删除已确认的内容"]
    assert "ontology/object/门店.md" in record["suspensions"][0]["reason"]


def test_B_policy_refuses_deletes_and_release_tags(ab):
    client, _workspaces, project_id, _root = ab
    decisions: list = []

    def confirm(command: str):
        async def action(ctx):
            decisions.append(await ctx.request_permission("Bash", {"command": command}, None, None))
        return action

    AutoEngine.hooks["/ontology-extract-object:s1"] = [confirm("rm ontology/object/对象9.md"),
                                                      confirm("git tag release/9.9.9"),
                                                      confirm("git tag -a v3-合成 -m 合成")]
    run = start(client, project_id, **ONE)
    wait_state(client, project_id, run["run_id"], "completed")
    assert [item["behavior"] for item in decisions] == ["deny", "deny", "allow"]


def test_B_guard_blocks_release_tags_in_any_mode(tmp_path, monkeypatch):
    import json
    import subprocess

    from app import kernel_guards
    from app.kernel_guards import _guard_common as guard

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    def verdict(command: str) -> list:
        data = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(tmp_path)}
        return [reason for reason in (guard.check(name, data) for name in guard.GUARDS) if reason]

    for command in ("git tag release/9.9.9", "git tag -a release/1.0.0 -m x", "git tag -d release/0.3.0",
                    "git update-ref refs/tags/release/9.9.9 HEAD", "git push origin release/9.9.9"):
        assert verdict(command), command
    assert verdict("git tag -a v3-合成 -m 合成") == []
    deny = kernel_guards.settings_base()["permissions"]["deny"]
    assert "Bash(git tag release/*)" in deny and json.dumps(deny)


# ---------------------------------------------------------------------------
# C、E、M 与推断：停机、崩溃、并发控制
# ---------------------------------------------------------------------------

THREE = {"scope": {"mode": "selected", "slice_ids": ["s1", "s2", "s3"], "until_node": "core.model"},
         "budget": {"max_tokens": 500_000}}


def _restart(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers import sessions as session_routes
    from app.services.agent_sessions import SessionManager
    from app.services.autobuild.runner import runner

    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: AutoEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    runner._tasks.clear()
    return TestClient(app)


def _cancel_driver(project_id: str) -> None:
    from app.services.autobuild.runner import runner

    task = runner._tasks[project_id]
    task.get_loop().call_soon_threadsafe(task.cancel)


def test_C_shutdown_right_after_a_merge_keeps_the_slice_linked(ab, monkeypatch):
    from app.kernel.changesets import service as changesets
    from app.services.autobuild.runner import runner

    client, _workspaces, project_id, root = ab
    real = changesets.merge
    fired: dict = {}

    def merge(*args, **kwargs):
        result = real(*args, **kwargs)
        if "「合成片2」" in (kwargs.get("message") or "") and not fired:
            fired.update(cs=result["record"]["id"], commit=result["commit"])
            _cancel_driver(project_id)  # 停机时取消运行任务：合入已写进主线，运行记录还没记
        return result

    monkeypatch.setattr(changesets, "merge", merge)
    run = start(client, project_id, **THREE)
    wait_until(lambda: fired and not runner.running(project_id))
    client.__exit__(None, None, None)
    with _restart(monkeypatch) as again:
        wait_state(again, project_id, run["run_id"], "completed")
        s2 = store.get(project_id, run["run_id"])["slices"]["s2"]
        assert s2["commit"] == fired["commit"] and s2["changeset_id"] == fired["cs"]
        versions = again.get(f"/api/v1/projects/{project_id}/versions", params={"source": "autobuild"}).json()["items"]
        assert next(item for item in versions if item["commit"] == fired["commit"])["run"]["slice_id"] == "s2"
        revert = again.post(f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}/slices/s2/revert")
        assert revert.status_code == 200, revert.text


def test_C_reconcile_merged_marks_autobuild_slices_for_review(ab):
    from app.kernel.changesets import service as changesets
    from app.kernel.changesets import store as changeset_store
    from app.services.flow import state as node_state

    client, workspaces, project_id, root = ab
    run = start(client, project_id, **ONE)
    wait_state(client, project_id, run["run_id"], "completed")
    cs_id = store.get(project_id, run["run_id"])["slices"]["s1"]["changeset_id"]
    # 崩在合入提交之后、记 merged 之前：记录还开着，「需复核」也还没标
    changeset_store.update(root, cs_id, state="pending_confirm", merged=None, needs_review=False, closed_at=None)
    data = changeset_store._load(root)
    data["current"] = cs_id
    changeset_store._save(root, data)
    for item in node_state.reviews(root):
        node_state.accept_review(root, item["node_id"])
    assert changesets.reconcile_merged(workspaces, root) == cs_id
    record = changeset_store.get(root, cs_id)
    assert record["state"] == "merged" and record["needs_review"] is True
    wait_until(lambda: any(item["source"] == "autobuild" for item in node_state.reviews(root)))


def test_E_shutdown_while_pausing_pauses_once(ab, monkeypatch):
    client, _workspaces, project_id, _root = ab
    release = gate("/ontology-extract-object:s1")
    run = start(client, project_id, **THREE)
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"
    wait_until(lambda: run_of(client, project_id, run["run_id"])["current"])
    assert client.post(f"{base}/pause").json()["state"] == "pausing"
    client.__exit__(None, None, None)
    release.set()
    with _restart(monkeypatch) as again:
        record = store.get(project_id, run["run_id"])
        assert record["state"] == "paused" and not record["flags"].get("pause")
        assert again.post(f"{base}/resume").status_code == 200
        assert wait_state(again, project_id, run["run_id"], "paused", "completed")["state"] == "completed"


def test_M_shutdown_while_writing_the_report_writes_it_once(ab, monkeypatch):
    from app.services.autobuild.runner import runner

    client, _workspaces, project_id, root = ab
    real = runner._write_report
    fired: dict = {}

    def write_report(run):
        real(run)
        if not fired:
            fired["done"] = True
            _cancel_driver(project_id)

    monkeypatch.setattr(runner, "_write_report", write_report)
    run = start(client, project_id, **ONE)
    wait_until(lambda: fired and not runner.running(project_id))
    client.__exit__(None, None, None)
    with _restart(monkeypatch) as again:
        wait_state(again, project_id, run["run_id"], "completed")
        subjects = git(root, "log", "--format=%s").splitlines()
        assert subjects.count("autobuild: 自主构建运行 #1 报告") == 1


def test_finish_always_reaches_an_end_state(ab, monkeypatch):
    """推断：收尾的某一步出错，运行照样落到结束态，名额放出来。"""
    from app.services.autobuild.runner import runner

    client, _workspaces, project_id, _root = ab

    def broken(run):
        raise RuntimeError("合成故障")

    monkeypatch.setattr(runner, "_end_suggestions", broken)
    monkeypatch.setattr(runner, "_close_exec", lambda *args: (_ for _ in ()).throw(RuntimeError("合成故障")))
    run = start(client, project_id, **ONE)
    assert wait_state(client, project_id, run["run_id"], "completed", "failed")["state"] == "completed"
    assert not runner.running(project_id)


def test_stop_without_a_driver_and_stop_racing_resume(ab, monkeypatch):
    """推断：没有驱动任务的「在跑」运行，停在安全点直接收尾；停止收尾时来了续跑，不再多做一片。"""
    from app.services.autobuild.runner import runner

    client, _workspaces, project_id, _root = ab
    real = runner._safe_point
    fired: dict = {}

    async def safe_point(project_id_, run_id_, unit):
        result = await real(project_id_, run_id_, unit)
        if not fired:
            fired["done"] = True
            raise asyncio.CancelledError()  # 驱动任务在两片之间没了
        return result

    monkeypatch.setattr(runner, "_safe_point", safe_point)
    run = start(client, project_id, **THREE)
    wait_until(lambda: fired and not runner.running(project_id))
    assert store.get(project_id, run["run_id"])["state"] == "running"
    base = f"/api/v1/projects/{project_id}/autobuild/runs/{run['run_id']}"

    async def both():
        return await asyncio.gather(runner.control(project_id, run["run_id"], "stop"),
                                    runner.control(project_id, run["run_id"], "resume"), return_exceptions=True)

    results = client.portal.call(both)
    assert not isinstance(results[0], Exception) and isinstance(results[1], ApiError)
    stopped = wait_state(client, project_id, run["run_id"], "stopped")
    assert stopped["slices"]["done"] == 1 and not runner.running(project_id)
    assert client.post(f"{base}/resume").json()["code"] == "autobuild_not_running"


def test_H_two_starts_at_once_make_one_run(ab):
    from app.services.autobuild.runner import runner

    client, _workspaces, project_id, _root = ab
    base = f"/api/v1/projects/{project_id}/autobuild"
    assert client.put(f"{base}/policy", json=ONE).status_code == 200
    release = gate("/ontology-extract-object:s1")
    tokens = [client.post(f"{base}/runs/preview", json={}).json()["preview_token"] for _ in range(2)]

    async def both():
        return await asyncio.gather(*(runner.start(project_id, token, actor="employee_0001", owner="employee_0001")
                                      for token in tokens), return_exceptions=True)

    results = client.portal.call(both)
    errors = [item for item in results if isinstance(item, Exception)]
    assert len(errors) == 1 and errors[0].code == "autobuild_running"
    conversations = [item for item in client.get(f"/api/v1/projects/{project_id}/conversations").json()["items"]
                     if item["kind"] == "autobuild"]
    assert len(conversations) == 1 and len(store.runs(project_id)) == 1
    release.set()


def test_F_concurrent_session_quota_does_not_end_the_run(ab):
    client, workspaces, project_id, _root = ab
    assert client.put("/api/v1/admin/quotas", json={"per_user": {"concurrent_sessions": 1}}).status_code == 200
    other = workspaces.create_project("另一个", owner="employee_0001")["id"]
    conversation = client.get(f"/api/v1/projects/{other}/conversations").json()["items"][0]["id"]
    assert client.post(f"/api/v1/projects/{other}/conversations/{conversation}/messages",
                       json={"text": "你好"}).status_code == 202
    run = start(client, project_id, **ONE)
    assert wait_state(client, project_id, run["run_id"], "completed", "budget_exhausted", "failed")["state"] == "completed"
