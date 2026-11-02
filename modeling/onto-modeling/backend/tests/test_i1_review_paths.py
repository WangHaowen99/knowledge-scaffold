"""I1 审查修复第 9、10 条：路径里带 NUL 的回 400（EV-29 逐项判为不可解析，不整批 500）；V-04、F-02 的路径先规范化再判受保护目录，`..` 一律拒绝。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
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
    project_id = workspaces.create_project("坏输入", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, project_id
    streams.reset_all()


def test_nul_in_path_is_400_not_500(api):
    client, pid = api
    base = f"/api/v1/projects/{pid}"
    assert client.post(f"{base}/resources", files={"file": ("a.md", "合成。\n".encode())}).status_code == 201
    for resp in (client.get(f"{base}/resources/preview", params={"path": "resource/others/a\x00.md"}),
                 client.get(f"{base}/resources/references", params={"path": "resource/others/a\x00.md"}),
                 client.get(f"{base}/versions/diff", params={"from": "HEAD", "path": "ontology/a\x00.md"}),
                 client.get(f"{base}/files/content", params={"path": "ontology/a\x00.md"})):
        assert resp.status_code == 400, (resp.request.url, resp.status_code, resp.text[:160])
    one = client.get(f"{base}/anchors/resolve", params={"anchor": "file:resource/others/a%00.md#L1"})
    assert one.status_code == 200 and one.json()["items"][0]["resolved"] is False
    batch = client.post(f"{base}/anchors/resolve", json={"anchors": ["file:resource/others/a.md#L1", "file:resource/others/a%00.md"]})
    assert batch.status_code == 200
    items = batch.json()["items"]
    assert items[0]["resolved"] is True and items[1]["resolved"] is False


def test_dotdot_cannot_reach_protected_paths(api):
    client, pid = api
    base = f"/api/v1/projects/{pid}"
    for path in ("ontology/../.git/config", "ontology/../.onto-web.json", "./.git/config", ".git/config",
                 "ontology/./../.onto/packs.lock"):
        diff = client.get(f"{base}/versions/diff", params={"from": "HEAD", "path": path})
        content = client.get(f"{base}/files/content", params={"path": path})
        for resp in (diff, content):
            assert resp.status_code in (400, 403), (path, resp.request.url.path, resp.status_code)
            assert "[core]" not in resp.text and "owner" not in resp.text
