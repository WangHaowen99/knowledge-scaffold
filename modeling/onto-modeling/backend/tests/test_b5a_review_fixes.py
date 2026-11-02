"""B5a 独立审查查出的缺陷（逐条一个用例，修复前失败）。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.main import app
from app.services import auth
from app.services.packs import builtin, checks, ui as pack_ui
from app.services.packs.repository import write_checksums
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets
from tests.test_pack_ui import FIXTURE, _publish_and_enable, zipped

NOTE = "sticky-notes.note"


def _pack(base: Path, *, view: str | None = None) -> Path:
    target = Path(base) / "sticky-notes"
    shutil.copytree(FIXTURE, target)
    if view is not None:
        (target / "views" / "note.yaml").write_text(view, encoding="utf-8")
    write_checksums(target)
    return target


def _result(items: list[dict], check_id: str) -> tuple[str, str]:
    item = next((item for item in items if item["id"] == check_id), None)
    return (item["result"], item["detail"]) if item else ("missing", "")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    pack_ui.revoke_all()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app, raise_server_exceptions=False)
    project_id = client.post("/api/v1/projects", json={"name": "便签", "goal": "合成场景：图书借阅建模"}).json()["id"]
    yield client, manager, project_id, tmp_path
    builtin.clear_cache()
    pack_ui.revoke_all()


# ---- 第 3 条：视图声明的字段类型写错 ----

BAD_VIEWS = [
    "artifact: sticky-notes.note\ndetail: [editor, note]\ncard: {title: title}\n",
    "artifact: sticky-notes.note\ndetail: {editor: note}\ncard: {title: title, chips: 5}\n",
    "artifact: sticky-notes.note\ncard: {title: [a], fields: {x: 1}}\n",
    "artifact: sticky-notes.note\nlist: {columns: 3}\n",
    "artifact: sticky-notes.note\ndetail: {editor: [note], side: title}\n",
    "artifact: sticky-notes.other\ncard: {title: title}\n",
    "- not a mapping\n",
]


@pytest.mark.parametrize("view", BAD_VIEWS)
def test_malformed_view_fails_static_checks_without_crashing(tmp_path, view):
    manifest, items = checks.run(_pack(tmp_path, view=view), tenant=True)
    assert manifest is not None
    assert _result(items, "views")[0] == "fail", items


def test_upload_with_malformed_view_ends_failed_not_checking(env):
    client, _, _, tmp_path = env
    accepted = client.post("/api/v1/admin/packs/uploads",
                           files={"file": ("p.zip", zipped(_pack(tmp_path / "a", view=BAD_VIEWS[0])), "application/zip")})
    detail = client.get(f"/api/v1/admin/packs/uploads/{accepted.json()['id']}").json()
    assert detail["state"] == "failed"


def test_bad_view_in_repository_does_not_break_other_packs(env):
    """绕过静态检查入库（比如旧版本）的坏视图：只影响这个类型的视图，产物接口不 500，别的包照常。"""
    client, manager, project_id, tmp_path = env
    repository = manager.packs.repository
    repository.install(_pack(tmp_path / "bad", view=BAD_VIEWS[1]), origin="upload")
    repository.set_channel("sticky-notes", "stable", "0.1.0")
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "sticky-notes"}).status_code == 201
    response = client.get(f"/api/v1/projects/{project_id}/artifact-types")
    assert response.status_code == 200, response.text
    note = next(item for item in response.json()["items"] if item["type"] == NOTE)
    assert note["view"]["card"]["chips"] == ["color"]  # 坏的 chips 丢掉，回到缺省（状态字段）
    assert client.get(f"/api/v1/projects/{project_id}/artifacts/{NOTE}").status_code == 200


def test_static_check_crash_marks_upload_failed(env, monkeypatch):
    """检查本身出错（不管是哪一项）也落成 failed，不停在 checking。"""
    client, _, _, tmp_path = env

    def boom(*_args, **_kwargs):
        raise AttributeError("synthetic")

    monkeypatch.setattr(checks, "run", boom)
    accepted = client.post("/api/v1/admin/packs/uploads",
                           files={"file": ("p.zip", zipped(_pack(tmp_path / "c")), "application/zip")})
    detail = client.get(f"/api/v1/admin/packs/uploads/{accepted.json()['id']}").json()
    assert detail["state"] == "failed" and detail["checks"][0]["id"] == "internal"
