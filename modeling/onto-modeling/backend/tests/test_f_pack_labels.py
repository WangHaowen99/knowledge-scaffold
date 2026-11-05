"""F 第一批第 10 条（V 子线 c）：节点上显示的是包名，不是清单里写的名字；只发布到 beta 的包，详情页显示 stable。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.api import deps
from app.services import auth
from app.services.flow import view
from app.services.packs import builtin, catalog, governance
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack


def test_plugin_nodes_show_manifest_label(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    project_id = workspaces.create_project("插件节点", owner="employee_0001")["id"]
    plugins = {node["id"]: node["plugin"] for node in view.build(workspaces.project_root(project_id))["nodes"]
               if node["plugin"]}
    assert plugins["trace.trace"] == {"name": "trace", "label": "场景模拟"}
    assert plugins["codegen.codegen"] == {"name": "codegen", "label": "代码生成与求解"}


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    governance.record_reconcile({})
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    yield manager
    builtin.clear_cache()
    governance.record_reconcile({})


def _publish(manager, tmp_path: Path, version: str, channel: str) -> None:
    source = make_pack(tmp_path / "src" / version, "lint", version, files={"commands/lint-run.md": f"版本 {version}\n"})
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("lint", channel, version, by="admin")


def test_pack_only_on_beta_shows_beta(manager, tmp_path):
    _publish(manager, tmp_path, "0.1.0", "beta")
    found = catalog.card(manager, "lint", None)
    assert (found["version"], found["channel"]) == ("0.1.0", "beta")


def test_pack_on_stable_and_beta_shows_stable(manager, tmp_path):
    _publish(manager, tmp_path, "0.1.0", "stable")
    _publish(manager, tmp_path, "0.2.0", "beta")
    found = catalog.card(manager, "lint", None)
    assert (found["version"], found["channel"]) == ("0.1.0", "stable")
