"""I1 审查修复第 1 条：报告与资料分析的读取不跟随工作区、副本里的符号链接（RP-01～RP-04、R-07～R-09）。

场景：助手（agent 用户，能在副本里执行 bash）在副本里建 reports/leak.md -> 项目外的文件
（这里用 tmp 下的一个"后端私有"文件模拟 /proc/self/environ 或密钥目录）。
变更集待确认时，RP-04 从副本读，返回目标文件内容。
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    project_id = workspaces.create_project("符号链接", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        yield client, workspaces, project_id, workspaces.project_root(project_id), tmp_path
    streams.reset_all()


def _plant(workspaces, pid, root, tmp_path):
    secret = tmp_path / "data" / "secrets" / "backend-only.key"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("JWT_SECRET=top-secret-value\n", encoding="utf-8")
    other = workspaces.create_project("别人的项目", owner="employee_0002")["id"]
    other_root = workspaces.project_root(other)
    (other_root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (other_root / "ontology" / "object" / "机密.md").write_text("# 机密\n\n别人的项目内容\n", encoding="utf-8")
    outside = tmp_path / "outside-dir"
    outside.mkdir()
    (outside / "dirleak.md").write_text("# 外面\n\ntop-secret-value\n", encoding="utf-8")
    copy = changesets.copy_for_turn(workspaces, pid, root)
    (copy / "reports").mkdir(exist_ok=True)
    (copy / "research").mkdir(exist_ok=True)
    (copy / "research" / "normal.md").write_text("# 资料分析\n", encoding="utf-8")
    record = changesets.observe(workspaces, pid, running=False, conversation_id="default")
    assert record is not None and record["state"] == "pending_confirm", record
    # 一轮结束的核对会删掉指到副本外的链接；这里在核对之后再建，测的是读取一侧（一轮进行中也有这个窗口）
    os.symlink(str(secret), copy / "reports" / "leak.md")
    os.symlink(os.path.relpath(other_root / "ontology" / "object" / "机密.md", copy / "reports"),
               copy / "reports" / "cross.md")
    os.symlink(str(outside), copy / "reports" / "sub")
    os.symlink(str(secret), copy / "research" / "leak2.md")
    return copy, secret


def test_reports_do_not_follow_symlinks_in_copy(api):
    client, workspaces, pid, root, tmp_path = api
    _plant(workspaces, pid, root, tmp_path)
    base = f"/api/v1/projects/{pid}"
    for report_id in ("leak", "cross", "research:leak2", "sub/dirleak"):
        for suffix in ("/raw", "", "/items"):
            resp = client.get(f"{base}/reports/{report_id}{suffix}")
            assert resp.status_code == 404, (report_id, suffix, resp.status_code)
            assert "top-secret-value" not in resp.text and "别人的项目内容" not in resp.text
    listing = client.get(f"{base}/reports").json()
    ids = [item["id"] for item in listing["items"]]
    assert not {"leak", "cross", "research:leak2", "sub/dirleak"} & set(ids), ids
    # 资料分析的视图（R-07～R-09）也不读链接
    for path in ("materials/map", "materials/verification", "materials/candidates"):
        assert "top-secret-value" not in client.get(f"{base}/{path}").text


def test_report_dirs_that_are_links_are_ignored(api):
    client, workspaces, pid, root, tmp_path = api
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.md").write_text("# 外面\n\ntop-secret-value\n", encoding="utf-8")
    import shutil
    shutil.rmtree(root / "reports", ignore_errors=True)
    os.symlink(str(outside), root / "reports")
    base = f"/api/v1/projects/{pid}"
    assert client.get(f"{base}/reports/x/raw").status_code == 404
    assert "x" not in [item["id"] for item in client.get(f"{base}/reports").json()["items"]]
