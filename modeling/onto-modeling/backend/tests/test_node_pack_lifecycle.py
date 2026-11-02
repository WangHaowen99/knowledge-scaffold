"""节点包的启用、生效、停用、升级（详设 6.9 第 2～6 步、13 章 P3 第一条验收；方案 D18；ADR-0065、0066）。

- 启用即变更集：packs.lock 与 flow.lock 在同一个带 `Onto-Changeset` 的提交里，撤回用 `revert_merged` 恢复；
- 助手的修改没确认时，会改流程的切换回 423；
- 下一条消息生效、可补做、停用解除关卡、升级标需复核（见后面的用例）。

节点包全部合成（tests/pack_helpers），用仓库里真实的 core 与官方包组装流程；假引擎不起真实 CLI。
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth
from app.services.flow import flowlock
from app.services.flow import view
from app.services.packs import builtin, lockfile
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import NODE_PACK_FLOW, make_node_pack
from tests.test_changesets import agent_write, main_write, obj


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "节点包", "goal": "合成场景：图书借阅建模"}).json()["id"]
    yield client, manager, project_id, tmp_path
    builtin.clear_cache()


def install(manager, tmp_path: Path, version: str = "0.1.0", *, flow: dict | None = None, name: str = "naming",
            files: dict | None = None) -> None:
    source = make_node_pack(tmp_path / "src" / f"{name}-{version}", name, version, flow=flow, files=files)
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel(name, "stable", version, by="admin")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


def trailer(root: Path, key: str) -> str:
    return git(root, "log", "-1", f"--format=%(trailers:key={key},valueonly)").strip()


def node_ids(root: Path) -> set[str]:
    return flowlock.node_ids(flowlock.read(root))


def test_new_project_has_flow_lock(env):
    _client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)
    data = flowlock.read(root)
    assert data and data["flow_lock_version"] == 1 and "core.validate" in node_ids(root)
    assert git(root, "ls-files", flowlock.FLOW_LOCK_REL).strip() == flowlock.FLOW_LOCK_REL


def test_enable_is_a_changeset_and_revert_restores(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    lock_before = (root / lockfile.LOCK_REL).read_text(encoding="utf-8")
    flow_before = (root / flowlock.FLOW_LOCK_REL).read_text(encoding="utf-8")

    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"})
    assert enabled.status_code == 201, enabled.text
    body = enabled.json()
    assert body["changeset_id"] and body["effective"] == "next_message"
    assert trailer(root, "Onto-Source") == "plugin" and trailer(root, "Onto-Changeset") == body["changeset_id"]
    changed = sorted(git(root, "show", "--name-only", "--format=", "HEAD").split())
    assert changed == sorted([lockfile.LOCK_REL, flowlock.FLOW_LOCK_REL])
    assert "naming.check" in node_ids(root)
    entry = next(item for item in flowlock.read(root)["nodes"] if item["id"] == "naming.check")
    assert entry["pack"] == "naming" and entry["version"] == "naming@0.1.0" and entry["after"] == "core.validate"
    record = changesets.get(manager, project_id, body["changeset_id"])
    assert record["source"] == "plugin" and record["state"] == "merged"
    assert record["merged"]["commit"] == git(root, "rev-parse", "HEAD").strip()
    assert changesets.current(manager, project_id) is None  # 已合入，不占着没关闭的位置

    reverted = changesets.revert_merged(manager, project_id, body["changeset_id"], actor="local")
    assert reverted["commit"]
    assert (root / lockfile.LOCK_REL).read_text(encoding="utf-8") == lock_before
    assert (root / flowlock.FLOW_LOCK_REL).read_text(encoding="utf-8") == flow_before


def test_cancel_pending_reverts_the_changeset(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    cs = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"}).json()["changeset_id"]
    cancelled = client.post(f"/api/v1/projects/{project_id}/packs/naming/cancel-pending")
    assert cancelled.status_code == 200
    assert changesets.get(manager, project_id, cs).get("reverted")
    assert lockfile.read(root).get("naming") is None and "naming.check" not in node_ids(root)
    assert client.get(f"/api/v1/projects/{project_id}/assembly").json()["state"] == "applied"


def test_unconfirmed_assistant_changes_block_flow_changes_only(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    copy_dir = changesets.copy_for_turn(manager, project_id, root)
    agent_write(copy_dir, "ontology/object/读者.md", obj("读者", "合成读者"), commit="agent: 读者")
    changesets.observe(manager, project_id, running=False, conversation_id="default")
    assert changesets.current(manager, project_id)["state"] == "pending_confirm"

    blocked = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"})
    assert blocked.status_code == 423 and blocked.json()["code"] == "write_scope_locked"
    assert lockfile.read(root).get("naming") is None
    # 不改流程的包照常启用（没有插件变更集，助手那组修改的位置不受影响）
    plain = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "websearch"})
    assert plain.status_code == 201, plain.text
    assert plain.json()["changeset_id"] is None
    assert changesets.current(manager, project_id)["state"] == "pending_confirm"


# ---- 可补做、停用解除关卡、升级标需复核、熔断 ----

def flow_after_object(**fields) -> dict:
    """节点挂在对象提取之后（操作提取之前）：主线做到操作提取的项目已经越过它。"""
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["nodes"][0].update({"after": "core.object", "before": "core.operation", **fields})
    return flow


def enable_and_apply(client, manager, project_id: str, name: str = "naming") -> None:
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": name})
    assert response.status_code == 201, response.text
    pack_service.prepare_for_message(manager, project_id)  # 下一条消息前的切换


def nodes_of(root: Path) -> dict[str, dict]:
    return {node["id"]: node for node in view.build(root)["nodes"]}


def test_backfill_when_project_already_passed_the_insertion_point(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    main_write(root, "ontology/operation/借书.md", "# 借书\n\n合成操作\n")
    before = view.build(root)
    assert before["nodes"][[n["id"] for n in before["nodes"]].index("core.operation")]["state"] == "confirmed"
    install(manager, tmp_path, flow=flow_after_object())
    enable_and_apply(client, manager, project_id)

    built = view.build(root)
    node = next(item for item in built["nodes"] if item["id"] == "naming.check")
    assert node["flags"]["backfill"] is True and node["display_state"] == "backfill"
    assert node["counts_in_progress"] is False and node["plugin"]["name"] == "naming"
    # 主线状态不往回退：做过的照旧已确认，进度分母不变
    assert next(item for item in built["nodes"] if item["id"] == "core.operation")["state"] == "confirmed"
    assert built["progress"] == before["progress"]
    from app.services.flow import model as flow_model

    assert flow_model.progress(root)["total"] == before["progress"]["total"]
    # 补做不抢主线下一步
    assert not built["suggestions"]["primary"]["id"].startswith("backfill:")


def test_new_step_without_progress_beyond_is_plainly_ready(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=flow_after_object())
    enable_and_apply(client, manager, project_id)
    node = nodes_of(root)["naming.check"]
    assert node["state"] == "ready" and node["flags"]["backfill"] is False


def test_disable_keeps_artifacts_marks_disabled_and_releases_gate(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=flow_after_object(placement="gate", blocks=["core.operation"], before=None))
    enable_and_apply(client, manager, project_id)
    assert nodes_of(root)["core.operation"]["state"] == "blocked"
    main_write(root, "reports/naming-1.md", "# 命名检查\n\n合成报告\n")

    assert client.post(f"/api/v1/projects/{project_id}/packs/naming/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    nodes = nodes_of(root)
    assert nodes["core.operation"]["state"] == "ready"  # 关卡解除
    gate = nodes["naming.check"]
    assert gate["flags"]["disabled"] is True and gate["display_state"] == "disabled"
    assert gate["actions"] == [] and gate["counts_in_progress"] is False
    assert (root / "reports" / "naming-1.md").is_file()  # 产物保留
    assert "naming.check" not in node_ids(root)


def test_upgrade_with_changed_done_marks_needs_review(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path)
    enable_and_apply(client, manager, project_id)
    main_write(root, "reports/naming-1.md", "# 命名检查\n\n合成报告\n")
    assert nodes_of(root)["naming.check"]["state"] == "confirmed"

    newer = copy.deepcopy(NODE_PACK_FLOW)
    newer["probes"]["naming_clean"] = {"pending": ["reports"], "glob": "naming-*.md"}
    newer["nodes"][0]["done"] = ["naming_report", "!naming_clean"]
    install(manager, tmp_path, "0.2.0", flow=newer)
    compat = client.get(f"/api/v1/packs/naming/versions/0.2.0/compatibility?project_id={project_id}").json()
    assert compat["ok"] and compat["impact"]["needs_review_after_upgrade"] == 1
    upgraded = client.post(f"/api/v1/projects/{project_id}/packs/naming/upgrade", json={"to_version": "0.2.0"})
    assert upgraded.status_code == 200 and upgraded.json()["changeset_id"]
    pack_service.prepare_for_message(manager, project_id)
    node = nodes_of(root)["naming.check"]
    assert node["flags"]["needs_review"] is True and node["flags"]["review_source"] == "upgrade"
    assert node["display_state"] == "needs_review"


def test_breaker_skips_nodes_and_releases_gates(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=flow_after_object(placement="gate", blocks=["core.operation"], before=None))
    enable_and_apply(client, manager, project_id)
    assert nodes_of(root)["core.operation"]["state"] == "blocked"
    from app.governance import ops_state

    ops_state.set_breaker("naming", "0.1.0", "open", "ops")
    try:
        nodes = nodes_of(root)
        assert nodes["naming.check"]["state"] == "skipped" and nodes["naming.check"]["actions"] == []
        assert nodes["core.operation"]["state"] == "ready"
    finally:
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")
        ops_state.set_breaker("naming", "0.1.0", "close", "ops")


# ---- 四类节点：脚本在沙箱里跑、人工只出决策卡、外部要提供方 ----

LINT_SCRIPT = '''import os, sys
names = sorted(os.path.splitext(name)[0] for _, _, files in os.walk(sys.argv[1]) for name in files)
print("# 命名格式检查")
print()
for name in names:
    print(f"- {name}：通过")
if "--fail" in sys.argv:
    sys.exit(3)
'''


def kinds_flow(*extra_nodes: dict, probes: dict | None = None) -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["probes"].update(probes or {})
    flow["nodes"] += list(extra_nodes)
    return flow


SCRIPT_NODE = {"id": "naming.lint", "title": "命名格式检查", "kind": "script", "placement": "branch",
               "after": "core.object", "done": ["naming_lint"], "produces": ["reports/lint-naming.md"],
               "script": {"name": "naming_lint.py", "inputs": ["ontology/object/*.md"], "output": "reports/lint-naming.md"}}
HUMAN_NODE = {"id": "naming.signoff", "title": "命名会签", "kind": "human", "placement": "gate", "after": "core.object",
              "blocks": ["core.operation"], "done": ["naming_signoff"], "produces": ["reports/signoff-naming.md"],
              "decision": {"title": "线下会签结果", "options": [{"id": "pass", "label": "已签署"}, {"id": "fail", "label": "未通过"}],
                           "record": "reports/signoff-naming.md"}}
EXTERNAL_NODE = {"id": "naming.fetch", "title": "取系统命名表", "kind": "external", "placement": "on_demand",
                 "done": [], "provider": {"kind": "db-generic"}}


@pytest.fixture()
def fake_sandbox(monkeypatch):
    """假的脚本验证网关：在本机用同一个执行器跑打好的包（真实环境里在 jiuwenbox 沙箱里）。"""
    import sys

    from app.services.flow import node_kinds

    client = node_kinds._client()
    calls = []

    def gateway_call(_url, method, path, payload=None, timeout=10.0):
        calls.append((method, path, payload["entry"]))
        done = subprocess.run([sys.executable, "-I", "-c", client.RUNNER, payload["bundle"], payload["entry"],
                               payload["cases"], "1500", "20"], capture_output=True, text=True, timeout=60)
        return {"results": client._parse_results(done.stdout, payload["case_count"])}

    monkeypatch.setattr(client, "_gateway_call", gateway_call)
    monkeypatch.setattr(client, "gateway_url", lambda: "http://sandbox.example.invalid")
    return client, calls


def test_script_node_runs_in_sandbox_and_writes_its_output(env, fake_sandbox):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=kinds_flow(SCRIPT_NODE, probes={"naming_lint": {"files": ["reports"], "glob": "lint-naming.md"}}),
            files={"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": LINT_SCRIPT})
    enable_and_apply(client, manager, project_id)

    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["run"]["state"] == "passed" and body["run"]["runtime"] == "jiuwenbox"
    assert fake_sandbox[1] == [("POST", "/v1/verify", "naming_lint.py")]
    assert "- 读者：通过" in (root / "reports" / "lint-naming.md").read_text(encoding="utf-8")
    assert trailer(root, "Onto-Node") == "naming.lint@0.1.0" and trailer(root, "Onto-Source") == "plugin"
    assert body["node"]["state"] == "confirmed"


def test_script_node_never_falls_back_to_the_main_container(env, fake_sandbox, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    install(manager, tmp_path, flow=kinds_flow({**SCRIPT_NODE, "script": {**SCRIPT_NODE["script"], "args": ["--fail"]}},
                                               probes={"naming_lint": {"files": ["reports"], "glob": "lint-naming.md"}}),
            files={"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": LINT_SCRIPT})
    enable_and_apply(client, manager, project_id)
    failed = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert failed.status_code == 422 and failed.json()["code"] == "node_run_failed"
    monkeypatch.setattr(fake_sandbox[0], "gateway_url", lambda: "")
    unavailable = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "service_unavailable"
    assert not (root / "reports" / "lint-naming.md").exists()


def test_human_node_only_asks_one_decision_card(env):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    install(manager, tmp_path, flow=kinds_flow(HUMAN_NODE, probes={"naming_signoff": {"files": ["reports"], "glob": "signoff-naming.md"}}))
    enable_and_apply(client, manager, project_id)
    assert nodes_of(root)["core.operation"]["state"] == "blocked"

    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.signoff/start", json={})
    assert started.status_code == 200, started.text
    question = started.json()["question"]
    assert question["kind"] == "decision" and [option["id"] for option in question["options"]] == ["pass", "fail"]
    again = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.signoff/start", json={}).json()["question"]
    assert again["question_id"] == question["question_id"]  # 只出一张
    cid = question["conversation_id"]
    from app.services.agent_sessions import manager as sessions

    asked = [event for event in sessions.get_history(project_id, cid) if event.get("type") == "question_asked"]
    assert len(asked) == 1

    url = f"/api/v1/projects/{project_id}/conversations/{cid}/questions/{question['question_id']}/answer"
    assert client.post(url, json={"option_id": "nope"}).json()["code"] == "answer_invalid"
    answered = client.post(url, json={"option_id": "pass"})
    assert answered.status_code == 200 and answered.json()["question"]["state"] == "answered"
    assert "结论：已签署" in (root / "reports" / "signoff-naming.md").read_text(encoding="utf-8")
    assert client.post(url, json={"option_id": "pass"}).status_code == 410
    nodes = nodes_of(root)
    assert nodes["naming.signoff"]["state"] == "confirmed" and nodes["core.operation"]["state"] == "ready"


def test_external_node_reports_missing_provider(env):
    client, manager, project_id, tmp_path = env
    install(manager, tmp_path, flow=kinds_flow(EXTERNAL_NODE))
    compat = client.get(f"/api/v1/packs/naming/versions/0.1.0/compatibility?project_id={project_id}").json()
    depends = next(check for check in compat["checks"] if check["id"] == "depends")
    assert not compat["ok"] and not depends["ok"] and "db-generic" in depends["detail"]
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "naming"})
    assert enabled.status_code == 409 and enabled.json()["code"] == "pack_unavailable"
    assert enabled.json()["details"] == {"reason": "not_installed", "providers": ["db-generic"]}
