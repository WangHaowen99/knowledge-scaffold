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
from tests.test_changesets import agent_write, obj


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
