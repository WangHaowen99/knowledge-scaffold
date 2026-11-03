"""建模目标上限与 modeling 创建页一致（2000 字符），全文保存不截断。数据全部合成。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.services import auth
from app.services.workspace import WorkspaceManager
from app.services.workspace.layout import META_FILE


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> TestClient:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    monkeypatch.setattr(deps, "workspaces", WorkspaceManager(tmp_path / "data", assets))
    monkeypatch.setattr(auth, "ENABLED", False)
    return TestClient(app)


def _meta(project_id: str) -> dict:
    return json.loads((deps.workspaces.project_root(project_id) / META_FILE).read_text(encoding="utf-8"))


def test_long_goal_saved_in_full(client):
    goal = "合成场景：图书借阅建模。" + "借" * 1988
    assert len(goal) == 2000
    created = client.post("/api/v1/projects", json={"name": "长目标", "goal": goal})
    assert created.status_code in (200, 201), created.text
    meta = _meta(created.json()["id"])
    assert meta["goal"] == goal and meta["background"] == goal


def test_goal_over_limit_rejected_not_truncated(client):
    created = client.post("/api/v1/projects", json={"name": "超长目标", "goal": "借" * 2001})
    assert created.status_code == 400
    assert created.json()["details"]["fields"]["goal"] == "length"


def test_goal_update_accepts_full_length(client):
    project_id = client.post("/api/v1/projects", json={"name": "改目标", "goal": "合成目标"}).json()["id"]
    goal = "还" * 2000
    assert client.patch(f"/api/v1/projects/{project_id}", json={"goal": goal}).status_code == 200
    assert _meta(project_id)["goal"] == goal
    assert client.patch(f"/api/v1/projects/{project_id}", json={"goal": goal + "还"}).status_code == 400
