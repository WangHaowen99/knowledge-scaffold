"""启动自检报告（详设 4.9 目标 4、P0 验收；API 契约 O-35）。

启动时清残留锁、列未提交改动，写日志并留存，运维接口能取到。项目全部合成。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, startup_report
from app.services.workspace import WorkspaceManager, _git


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    # 这些用例经业务面调运维接口：打开 ONTO_WEB_OPS_ON_MAIN（默认运维面只在独立监听上，见 test_governance）
    monkeypatch.setattr(config, "OPS_ON_MAIN", True)
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    instance = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(deps, "workspaces", instance)
    return instance


def _dirty_project(manager: WorkspaceManager, name: str) -> Path:
    root = manager.project_root(manager.create_project(name)["id"])
    (root / "ontology" / "object" / "订单.md").write_text("# 订单\n\n未提交的合成说明\n", encoding="utf-8")
    return root


def test_report_lists_locks_and_uncommitted_changes(manager, caplog):
    dirty = _dirty_project(manager, "有改动")
    clean = manager.project_root(manager.create_project("没改动")["id"])
    (dirty / ".git" / "index.lock").write_text("")

    with caplog.at_level(logging.INFO):
        report = startup_report.run(manager)

    items = {item["id"]: item for item in report["items"]}
    assert items["index_lock"]["result"] == "cleared" and items["index_lock"]["tone"] == "fix"
    assert items["index_lock"]["locks"] == [f"{dirty.name}/.git/index.lock"]
    assert not (dirty / ".git" / "index.lock").exists()
    uncommitted = items["uncommitted"]
    assert uncommitted["result"] == "awaiting_user" and uncommitted["tone"] == "warn"
    assert [project["project_id"] for project in uncommitted["projects"]] == [dirty.name]
    assert uncommitted["projects"][0]["paths"] == ["ontology/object/订单.md"]
    # 未提交改动保留原样，等所有者处理，不替用户提交
    assert _git(dirty, "status", "--porcelain").strip()
    assert "启动自检" in caplog.text and dirty.name in caplog.text
    assert startup_report.latest()["items"] == report["items"]


def test_clean_start_reports_ok(manager):
    manager.create_project("干净项目")
    report = startup_report.run(manager)
    # 启动恢复注册表（方案 B8）登记的全部步骤；外部依赖的结论取决于运行环境（认证服务、沙箱可不可达）
    results = [(item["id"], item["result"]) for item in report["items"]]
    assert [item_id for item_id, _ in results] == [
        "index_lock", "write_intents", "uncommitted", "assembly", "history_repair", "interrupted_execs",
        "dependencies", "purge", "material_text",
    ]
    assert [result for item_id, result in results if item_id != "dependencies"] == ["ok"] * 8


def test_startup_runs_self_check_and_ops_endpoint_returns_it(manager, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)
    dirty = _dirty_project(manager, "重启前没提交")
    (dirty / ".git" / "index.lock").write_text("")

    with TestClient(app) as client:  # lifespan：会话开始之前做启动自检
        assert not (dirty / ".git" / "index.lock").exists()
        body = client.get("/api/v1/ops/startup-report").json()

    assert body["at"].endswith("Z")
    results = {item["id"]: item["result"] for item in body["items"]}
    # 前台步骤在就绪之前做完；外部依赖、清扫残留目录在后台，做完才补进报告
    foreground = ("index_lock", "write_intents", "uncommitted", "assembly", "history_repair", "interrupted_execs")
    assert {key: results[key] for key in foreground} == {
        "index_lock": "cleared", "write_intents": "ok", "uncommitted": "awaiting_user", "assembly": "ok",
        "history_repair": "ok", "interrupted_execs": "ok"}
