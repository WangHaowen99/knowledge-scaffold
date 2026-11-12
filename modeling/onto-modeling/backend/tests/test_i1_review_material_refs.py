"""I1 审查修复第 8 条：只写了文件名的引用要卡住文件名边界——「周报告.md」不算引用了「报告.md」（R-05、R-01 referenced、R-04 broken_references、资料地图的「读过」）。"""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager
from tests.test_changesets import main_write, obj

_log = logging.getLogger(__name__)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("引用", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app) as client:
        yield client, workspaces, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def test_name_substring_is_not_a_reference(api):
    client, _ws, pid, root = api
    base = f"/api/v1/projects/{pid}"
    for name in ("报告.md", "周报告.md"):
        assert client.post(f"{base}/resources", files={"file": (name, "合成。\n".encode())}).status_code == 201
    main_write(root, "ontology/object/门店.md", obj("门店", "合成，依据见 周报告.md"), "web: 门店")
    refs = client.get(f"{base}/resources/references", params={"path": "resource/others/报告.md"}).json()
    listed = {item["path"]: item["referenced"] for group in client.get(f"{base}/resources").json()["groups"]
              for item in group["items"]}
    deleted = client.request("DELETE", f"{base}/resources", params={"path": "resource/others/报告.md"}).json()
    _log.info("R-05 报告.md: %s %s", refs["total"], [item["target"].get("id") for item in refs["items"]])
    _log.info("R-01 referenced: %s", listed)
    _log.info("R-04 broken_references: %s", deleted.get("broken_references"))
    assert refs["total"] == 0 and deleted["broken_references"] == 0
    assert listed["resource/others/周报告.md"] == 1
