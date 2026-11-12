"""I1 审查修复第 6、7 条：ST-09 推出来的 key 同样校验、在写入之前；名称按 UTF-8 字节数限长（原子写的临时文件名不超过 255 字节），超了回 400。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager
from tests import proc


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    project_id = workspaces.create_project("新建条目", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        monkeypatch.setattr(changesets, "_activity_provider", None)
        yield client, workspaces, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def _log(root: Path) -> str:
    return proc.run(["git", "log", "--format=%s", "-3"], cwd=root, capture_output=True, text=True).stdout


def test_dot_name_rejected_before_write(api):
    client, _ws, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    before = _log(root)
    resp = client.post(base, json={"kind": "object", "name": ".草稿"})
    assert resp.status_code == 400 and resp.json()["code"] == "invalid_argument", resp.text
    assert not (root / "ontology/object/.草稿.md").exists() and _log(root) == before


def test_long_cjk_name_is_400_not_500(api):
    client, _ws, pid, root = api
    base = f"/api/v1/projects/{pid}/studio/entries"
    before = _log(root)
    resp = client.post(base, json={"kind": "object", "name": "长" * 80})
    assert resp.status_code == 400 and resp.json()["details"]["fields"]["name"] == "too_long", resp.text
    assert _log(root) == before
    # 短一些的中文名照常能建
    ok = client.post(base, json={"kind": "object", "name": "长" * 40})
    assert ok.status_code == 201, ok.text
