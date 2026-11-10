"""节点状态与统一下一步（详设 6.6、6.9、6.10；方案 D13、D15、D16；契约 FL-01～FL-05、7.8）。

- 8 种状态：未就绪、可开始、进行中、待确认、已确认、过期、已跳过、被挡住；需复核（节点升级、导入）；
- 进度不再因为文件落盘就打勾：助手副本里的改动确认生效之后才算；
- FL-03～FL-05：跳过、恢复（只对可选节点）、开始节点（等价于 S-20）；
- `state_helper suggest` 与 FL-02、宿主注入给出同一个下一步。

测试数据全部为合成内容；假引擎不起真实 CLI。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.routers import sessions as session_routes
from app.services import agent_sessions, auth, streams
from app.services.agent_sessions import SessionManager
from app.services.flow import model as flow_model
from app.services.flow import state as node_state
from app.services.flow import view
from app.services.session_host import exec_records, injection, next_steps
from app.services.workspace import WorkspaceManager
from tests.test_changesets import agent_write, main_write, obj
from tests.test_session_host import FakeEngine, lock, release, result, settle

@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda pid: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    monkeypatch.setattr(session_routes, "manager", manager)
    FakeEngine.scripts, FakeEngine.sent = [], []
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        project_id = workspaces.create_project("节点状态", owner="employee_0001")["id"]
        root = workspaces.project_root(project_id)
        yield client, workspaces, manager, project_id, root
    streams.reset_all()


def states(root: Path) -> dict:
    return {node["id"]: node["state"] for node in view.build(root)["nodes"]}


def test_states_follow_exec_records_changesets_and_mainline(env, monkeypatch):
    _client, workspaces, _manager, project_id, root = env
    first = states(root)
    assert first["core.research"] == "ready" and first["core.model"] == "not_ready"
    # 进行中：有没结束的执行记录（建模命令记在建模节点、全量模式那一段上）
    flow = flow_model.load_for(root)
    record = exec_records.start(project_id, "default", root=root, flow=flow, command="/ontology-extract-object",
                                command_name="extract-object")
    assert record["slice"] == {"id": "all", "name": "全部"} and record["step"] == "object"
    assert states(root)["core.model"] == "running"
    exec_records.update(project_id, record["id"], state="done")
    # 待确认：节点的改动在这一轮还没合入的变更集里（合入前的一瞬）；主线上还没有，进度不打勾
    copy = changesets.copy_for_turn(workspaces, project_id, root)
    agent_write(copy, "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")
    agent_write(copy, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n", commit="agent: 补货")
    changesets.observe(workspaces, project_id, running=False, conversation_id="default", exec_record=record)
    built = view.build(root)
    node = next(item for item in built["nodes"] if item["id"] == "core.model")
    assert node["state"] == "pending_confirm" and node["display_state"] == "pending"
    progress_before = built["progress"]["done"]
    # F3：没有「修改待确认」的提示，建议条也不出「确认生效」
    assert not [item for item in built["notices"] if item["kind"] in ("pending_changeset", "changeset_conflict")]
    assert built["suggestions"]["mode"] != "pending_confirm"
    assert (built["suggestions"].get("primary") or {}).get("action", {}).get("type") != "merge_changeset"
    # 已确认：合入之后主线满足完成判定，进度才打勾
    monkeypatch.setattr(changesets.gates, "compare", lambda *args, **kwargs: {"errors": 0, "warnings": 0, "findings": []})
    changesets.merge(workspaces, project_id, "cs-1", actor="employee_0001")
    from app import wiring

    wiring._confirm_nodes(project_id, ["core.model"], changesets.get(workspaces, project_id, "cs-1"))
    after = view.build(root)
    assert next(item for item in after["nodes"] if item["id"] == "core.model")["state"] == "confirmed"
    assert after["slices"][0]["stages"]["object"] == "confirmed"
    assert after["progress"]["done"] == progress_before + 1


def test_stale_when_upstream_fingerprint_changes(env):
    _client, workspaces, _manager, project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    assert states(root)["core.model"] == "confirmed"  # 存量：第一次看到直接记基线
    main_write(root, "ontology/object/仓库.md", obj("仓库", "上游变了"))
    built = view.build(root)
    model = next(item for item in built["nodes"] if item["id"] == "core.model")
    assert model["state"] == "stale" and model["actions"][0]["kind"] == "refresh"
    assert built["slices"][0]["stages"]["operation"] == "stale"
    stale = [item for item in [built["suggestions"]["primary"], *built["suggestions"]["secondary"]]
             if item["id"].startswith("refresh:core.model")]
    assert stale and stale[0]["priority"] == 2
    assert stale[0]["action"]["command"] == "/ontology-extract-operation"  # 刷新过期的那一步
    assert stale[0]["label"] == "刷新「操作」" and built["suggestions"]["note"] == "上游「对象」改过"  # F7：写清哪一步
    # 重新跑过（这一步的变更集确认生效）：按新的上游重记基线
    from app.services.flow import slices

    slices.confirm(root, flow_model.load_for(root), [["all", "operation"]], doc=None)
    assert states(root)["core.model"] == "confirmed"


def test_validation_fixing_an_object_does_not_push_re_extraction(env):
    """端到端验证发现：校验自动修复改了对象，建模的「操作」一步过期，建议条主推「刷新『操作』」（整段重新提取）而不是主线下一步。
    校验在改动之后确认（报告写完、没有待处理），说明这批改动核对过了：不再推上游刷新。"""
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    assert states(root)["core.model"] == "confirmed"
    main_write(root, "ontology/object/门店.md", obj("门店", "校验补了单位字段"), "fix: address rule check issues")
    main_write(root, "reports/20261102-120000-all-rule.md", "## 规则检查结果\n\n- ✅ ❌ [结构缺失] 门店缺单位\n",
               "fix: address rule check issues")
    built = view.build(root)
    got = {item["id"]: item["state"] for item in built["nodes"]}
    assert got["core.model"] == "stale" and got["core.validate"] == "confirmed"
    suggested = [item["id"] for item in [built["suggestions"]["primary"], *built["suggestions"]["secondary"]] if item]
    assert not any(item.startswith("refresh:core.model") for item in suggested), suggested


def test_skip_restore_and_start_nodes(env):
    client, _workspaces, _manager, project_id, root = env
    base = f"/api/v1/projects/{project_id}/flow/nodes"
    assert client.post(f"{base}/core.model/skip", json={}).json()["code"] == "node_not_skippable"
    assert client.post(f"{base}/nope/skip", json={}).json()["code"] == "node_not_found"
    skipped = client.post(f"{base}/core.metric/skip", json={"reason": "没有指标"})
    assert skipped.status_code == 200 and skipped.json()["node"]["state"] == "skipped"
    assert skipped.json()["node"]["display_state"] == "skipped" and skipped.json()["flow_hash"]
    restored = client.post(f"{base}/core.metric/restore", json={})
    assert restored.json()["node"]["state"] != "skipped"
    assert client.post(f"{base}/core.metric/restore", json={}).json()["code"] == "node_not_skipped"
    started = client.post(f"{base}/core.research/start", json={"origin": {"page": "wb-l2", "label": "开始"}})
    assert started.status_code == 202, started.text
    assert started.json()["task"]["title"].startswith("助手正在执行「")


def test_blocked_by_an_unconfirmed_gate(tmp_path):
    root = tmp_path / "data" / "workspaces" / "gate"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    flow = {"nodes": [
        {"id": "core.a", "title": "甲", "done": ["a"]},
        {"id": "x.gate", "title": "关卡", "placement": "gate", "after": "core.a", "done": ["g"],
         "blocks": ["core.b"]},
        {"id": "core.b", "title": "乙", "after": "core.a", "done": ["b"]},
    ], "probes": {}}
    result = node_state.compute(root, flow, {"core.a": True, "x.gate": False, "core.b": False}, persist=False)
    assert result["core.b"]["state"] == "blocked" and result["x.gate"]["state"] == "ready"
    result = node_state.compute(root, flow, {"core.a": True, "x.gate": True, "core.b": False}, persist=False)
    assert result["core.b"]["state"] == "ready"


def test_needs_review_after_upgrade_and_import(env):
    _client, _workspaces, _manager, _project_id, root = env
    main_write(root, "ontology/object/门店.md", obj("门店", "合成门店"))
    main_write(root, "ontology/operation/补货.md", "# 补货\n\n## 业务描述\n\n合成\n")
    flow = flow_model.load_for(root)
    hits = flow_model.flow_state(root).node_hits(flow, root)
    assert node_state.compute(root, flow, hits)["core.model"]["flags"]["needs_review"] is False
    # 节点升级：完成判定变了，旧的「已确认」标需复核
    upgraded = json.loads(json.dumps(flow))
    next(node for node in upgraded["nodes"] if node["id"] == "core.model")["done"] = ["object", "operation", "rule"]
    upgraded_hits = {**hits, "core.model": True}
    item = node_state.compute(root, upgraded, upgraded_hits)["core.model"]
    assert item["flags"] == {"needs_review": True, "review_source": "upgrade", "disabled": False, "backfill": False}
    assert item["display_state"] == "needs_review"
    assert node_state.reviews(root)[0]["source"] == "upgrade"
    assert node_state.accept_review(root, "core.model") is True
    # 导入的产物
    assert node_state.mark_imported(root) == ["core.model"]
    assert node_state.reviews(root)[0]["source"] == "import"
    # 自主构建（P3 调）：显示为「自主完成」
    node_state.mark_needs_review(root, ["core.model"], "autobuild", run_id="run-1", slice_id="s1")
    assert node_state.compute(root, flow, hits)["core.model"]["display_state"] == "auto"
    with pytest.raises(ValueError):
        node_state.mark_needs_review(root, ["core.model"], "bogus")


def test_state_helper_suggest_matches_flow_and_host_injection(env):
    """D16：一轮结束后，界面建议条（FL-02）、宿主注入、助手收尾的 state_helper suggest 给同一个下一步。"""
    client, workspaces, manager, project_id, root = env

    def work(ctx):
        agent_write(Path(ctx.cwd), "ontology/object/门店.md", obj("门店", "合成门店"), commit="agent: 门店")

    FakeEngine.scripts = [[lock("extract-object", "objects"), work, release("extract-object"), result()]]

    async def turn():
        await manager.send(project_id, root, "/ontology-extract-object", username="employee_0001")
        await settle(manager, project_id)
        await manager.shutdown()

    asyncio.run(turn())
    copy = changesets.existing_copy(root)
    assert copy is not None and (copy / ".onto" / "run" / "flow-state.json").is_file()
    from_ui = client.get(f"/api/v1/projects/{project_id}/flow/suggestions").json()
    from_helper = next_steps.suggest(copy)
    assert from_helper == {key: value for key, value in from_ui.items() if key != "rev"}  # 版本号只有后端给
    # F3：这一轮结束已经合入，建议条直接给下一步
    assert from_ui["mode"] == "normal" and from_ui["primary"]["label"] != "确认生效"
    assert len([from_ui["primary"], *from_ui["secondary"]]) <= 3
    hint = injection.next_step_hint(copy)
    labels = [item["label"] for item in [from_ui["primary"], *from_ui["secondary"]]]
    assert hint and all(label in hint for label in labels)
    # 命令行接口不变：detect-stage、check-prereq 仍输出旧阶段名与 ok/missing
    helper = ["python3", str(copy / ".claude" / "scripts" / "state_helper.py")]
    stage = subprocess.run([*helper, "detect-stage"], cwd=copy, capture_output=True, text=True).stdout.strip()
    assert stage in flow_model.flow_state(root).stage_names(flow_model.load_for(root))
    prereq = subprocess.run([*helper, "check-prereq", "extract-operation"], cwd=copy, capture_output=True,
                            text=True).stdout.strip()
    assert prereq == "ok" or prereq.startswith("missing:")
