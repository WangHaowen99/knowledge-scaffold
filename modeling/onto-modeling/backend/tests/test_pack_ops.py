"""插件运维（契约 O-25～O-29、O-42；详设 4.10.3、8.1 第 2 步；ADR-0067）。

- O-25 列表：通道上的、项目在用的、禁用的版本各一行，带可回滚到的版本；
- O-27 回滚通道：只改指针，跟随通道的项目下一条消息换过去，锁定版本的不动；
- O-28 禁用版本：新项目不能再启用它，通道指着它时改取最新的可用版本；core 的默认版本不能禁用；
- O-26 灰度：只做按项目（beta 带比例，跟随 stable 的项目按分桶拿 beta），按租户回 501；
- O-42 带 flow.lock 格式版本；就绪检查的 assembly 项见 test_pack_governance.py。

包全部合成（tests/pack_helpers）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.ops_app import ops_app
from app.services import auth
from app.services.packs import builtin, governance, lockfile
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack

TOKENS = {"ops-token": {"sub": "operator_ops", "role": "ops"}}
OPS = {"Authorization": "Bearer ops-token"}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    governance.record_reconcile({})
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    manager = WorkspaceManager(data, make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))
    yield TestClient(ops_app), manager, tmp_path
    builtin.clear_cache()
    governance.record_reconcile({})


def publish(manager, tmp_path: Path, version: str, channel: str = "stable", rollout: dict | None = None) -> None:
    source = make_pack(tmp_path / "src" / version, "lint", version, files={"commands/lint-run.md": f"版本 {version}\n"})
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("lint", channel, version, by="admin", rollout=rollout)


def project_with_lint(manager, name: str = "插件运维") -> tuple[str, Path]:
    project_id = manager.create_project(name, owner="employee_0001")["id"]
    pack_service.enable(manager, project_id, "lint")
    pack_service.prepare_for_message(manager, project_id)
    return project_id, manager.project_root(project_id)


def row(client, name: str, version: str) -> dict:
    items = client.get("/api/v1/ops/packs", headers=OPS).json()["items"]
    return next(item for item in items if item["name"] == name and item["version"] == version)


def test_list_rollback_channel_and_followers(env):
    client, manager, tmp_path = env
    publish(manager, tmp_path, "0.1.0")
    project_id, root = project_with_lint(manager)
    publish(manager, tmp_path, "0.2.0")
    pack_service.prepare_for_message(manager, project_id)  # 跟随 stable 升到 0.2.0
    assert lockfile.read(root).get("lint").version == "0.2.0"

    current = row(client, "lint", "0.2.0")
    assert current["channel"] == "stable" and current["rollout_pct"] == 100 and current["projects_using"] == 1
    assert current["rollback_to"] == "0.1.0" and current["breaker"] == "closed"
    assert row(client, "core", manager.packs.repository.latest("core", "stable"))["kind"] == "capability"

    rolled = client.post("/api/v1/ops/packs/lint/rollback", headers=OPS, json={"channel": "stable", "to_version": "0.1.0"})
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"] == "0.1.0" and rolled.json()["channel"] == "stable"
    assert row(client, "lint", "0.2.0")["channel"] == "retired"
    pack_service.prepare_for_message(manager, project_id)  # 跟随通道的项目下一条消息换过去
    assert lockfile.read(root).get("lint").version == "0.1.0"
    bad = client.post("/api/v1/ops/packs/lint/rollback", headers=OPS, json={"channel": "nightly", "to_version": "0.1.0"})
    assert bad.status_code == 400


def test_disable_version_blocks_new_enables_and_moves_channel(env):
    client, manager, tmp_path = env
    publish(manager, tmp_path, "0.1.0")
    publish(manager, tmp_path, "0.2.0")
    assert client.post("/api/v1/ops/packs/lint/0.2.0/disable", headers=OPS, json={}).status_code == 400
    disabled = client.post("/api/v1/ops/packs/lint/0.2.0/disable", headers=OPS, json={"reason": "合成故障"})
    assert disabled.status_code == 200 and disabled.json()["channel"] == "retired"
    assert disabled.json()["rollout_pct"] == 0
    assert manager.packs.repository.latest("lint", "stable") == "0.1.0"
    project_id = manager.create_project("新项目", owner="employee_0001")["id"]
    with pytest.raises(pack_service.PackError) as exc:
        pack_service.enable(manager, project_id, "lint", "0.2.0")
    assert exc.value.code == "pack_unavailable"
    assert pack_service.enable(manager, project_id, "lint")["pack"]["locked_version"] == "0.1.0"
    core = manager.packs.repository.latest("core", "stable")
    refused = client.post(f"/api/v1/ops/packs/core/{core}/disable", headers=OPS, json={"reason": "合成"})
    assert refused.status_code == 409 and refused.json()["code"] == "pack_required"
    missing = client.post("/api/v1/ops/packs/lint/9.9.9/disable", headers=OPS, json={"reason": "合成"})
    assert missing.status_code == 404


def test_rollout_by_project_moves_stable_followers_to_beta(env):
    client, manager, tmp_path = env
    publish(manager, tmp_path, "0.1.0")
    project_id, root = project_with_lint(manager)
    publish(manager, tmp_path, "0.3.0-rc.1", channel="beta", rollout={"percent": 0, "by": "project", "targets": []})
    assert row(client, "lint", "0.3.0-rc.1")["channel"] == "beta"
    stable = client.put("/api/v1/ops/packs/lint/0.1.0/rollout", headers=OPS, json={"percent": 50, "by": "project"})
    assert stable.status_code == 400
    tenant = client.put("/api/v1/ops/packs/lint/0.3.0-rc.1/rollout", headers=OPS, json={"percent": 50, "by": "tenant"})
    assert tenant.status_code == 501 and tenant.json()["code"] == "rollout_not_available"

    full = client.put("/api/v1/ops/packs/lint/0.3.0-rc.1/rollout", headers=OPS, json={"percent": 100, "by": "project"})
    assert full.status_code == 200 and full.json()["rollout_pct"] == 100 and full.json()["rollout_by"] == "project"
    pack_service.prepare_for_message(manager, project_id)
    assert lockfile.read(root).get("lint").version == "0.3.0-rc.1"
    client.put("/api/v1/ops/packs/lint/0.3.0-rc.1/rollout", headers=OPS, json={"percent": 0, "by": "project"})
    pack_service.prepare_for_message(manager, project_id)  # 收回灰度：回到 stable
    assert lockfile.read(root).get("lint").version == "0.1.0"
    named = client.put("/api/v1/ops/packs/lint/0.3.0-rc.1/rollout", headers=OPS,
                       json={"percent": 0, "by": "project", "targets": [project_id]})
    assert named.status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    assert lockfile.read(root).get("lint").version == "0.3.0-rc.1"
    assert governance.rollout_bucket(project_id) == governance.rollout_bucket(project_id)


def test_versions_include_flow_lock_format(env):
    client, manager, _tmp = env
    manager.create_project("版本清单", owner="employee_0001")
    items = client.get("/api/v1/ops/versions", headers=OPS).json()["items"]
    assert {"category": "lockfile", "component": "flow.lock", "version": "1", "note": "组装后的流程节点"} in items


def test_read_requires_ops_role(env):
    client, _manager, _tmp = env
    assert client.get("/api/v1/ops/packs").status_code == 401
