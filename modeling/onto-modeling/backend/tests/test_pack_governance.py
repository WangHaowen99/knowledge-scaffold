"""插件运行时接到治理平面的登记点（_plan/gaps.md「B3 合并」第 1 条；ADR-0042）。

- 就绪检查的 assembly 项；
- O-29 熔断的包查询；
- O-42 版本清单里的包与 packs.lock 格式；
- 提交 trailer 的 Onto-Packs 见 test_writepath.py。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.governance import health
from app.ops_app import ops_app
from app.services import auth
from app.services.packs import builtin, governance, lockfile
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets

TOKENS = {"ops-token": {"sub": "operator_ops", "role": "ops"}}


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    builtin.clear_cache()
    governance.record_reconcile({})
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    instance = WorkspaceManager(data, make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", instance)
    yield instance
    builtin.clear_cache()
    governance.record_reconcile({})


@pytest.fixture()
def ops_client(manager, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))
    return TestClient(ops_app)


def _ops() -> dict:
    return {"Authorization": "Bearer ops-token"}


def test_assembly_readiness_criteria(manager):
    probe = next(item for item in health.probe(force=True) if item["name"] == "assembly")
    assert probe["status"] == "ok" and probe["critical"] is True
    # 启动核对有项目装配失败：仍可服务（这些项目保持原版本），给 warn
    governance.record_reconcile({"repaired": [], "locked": [], "failed": {"p1": "包仓库里没有 trace 0.1.0"}})
    assert governance.assembly_probe(manager)[0] == "warn"
    governance.record_reconcile({})
    # stable core 的内容被改过：新项目装配不起来，fail
    stable = manager.packs.repository.latest("core", "stable")
    target = manager.packs.repository.version_dir("core", stable) / "commands" / "ontology.md"
    target.write_text("被改过\n", encoding="utf-8")
    status, detail = governance.assembly_probe(manager)
    assert status == "fail" and "被改过" in detail


def test_assembly_readiness_without_toolkit_is_ok(tmp_path, monkeypatch):
    builtin.clear_cache()
    bare = tmp_path / "bare"
    (bare / "workspace-rules.md").parent.mkdir(parents=True)
    instance = WorkspaceManager(tmp_path / "data2", bare)
    assert governance.assembly_probe(instance) == ("ok", "镜像里没有工具箱，插件装配未启用")


def test_breaker_lookup_returns_ops_row(ops_client, manager):
    manager.create_project("熔断")
    stable = manager.packs.repository.latest("core", "stable")
    opened = ops_client.post(f"/api/v1/ops/packs/core/{stable}/breaker", headers=_ops(),
                             json={"action": "open", "reason": "合成故障"})
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["name"] == "core" and body["version"] == stable and body["channel"] == "stable"
    assert body["kind"] == "capability" and body["projects_using"] == 1 and body["breaker"] == "open"
    missing = ops_client.post("/api/v1/ops/packs/core/9.9.9/breaker", headers=_ops(), json={"action": "open"})
    assert missing.status_code == 404 and missing.json()["code"] == "pack_version_not_found"


def test_versions_list_packs_and_lock_format(ops_client, manager):
    manager.create_project("版本清单")
    items = ops_client.get("/api/v1/ops/versions", headers=_ops()).json()["items"]
    lock = next(item for item in items if item["component"] == "packs.lock")
    assert lock == {"category": "lockfile", "component": "packs.lock", "version": str(lockfile.LOCK_VERSION),
                    "note": "2：core 拆出可选包之后的锁文件"}
    core = next(item for item in items if item["component"] == "core")
    assert core["category"] == "capability" and core["version"] == manager.packs.repository.latest("core", "stable")
