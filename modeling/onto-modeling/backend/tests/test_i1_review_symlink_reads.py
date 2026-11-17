"""I1 审查修复第 1 条（全局排查）：按路径读工作区文件的入口不跟随符号链接。

按路径读工作区文件、把内容或派生信息回给前端的入口，都不跟随指到工作区外
（或受保护目录）的符号链接。助手能在工作区、副本里随手建链接，后端以更高权限运行。
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager

SECRET = "top-secret-value-0451"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("链接", owner="employee_0001")["id"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(f"# 外面\n\n## 规则\n\n{SECRET}\n\n## 对象描述\n\n{SECRET}\n", encoding="utf-8")
    (outside / "secret.json").write_text(json.dumps({"feedback_id": "fb-x", "summary": SECRET}), encoding="utf-8")
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, workspaces.project_root(project_id), f"/api/v1/projects/{project_id}", outside
    streams.reset_all()


def _link(root: Path, rel: str, target: Path) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir() and not path.is_symlink():
        import shutil
        shutil.rmtree(path)
    os.symlink(str(target), path)


def test_query_search_does_not_read_linked_files(api):
    client, root, base, outside = api
    _link(root, "ontology/object/泄露.md", outside / "secret.md")
    resp = client.get(f"{base}/query", params={"kind": "search", "term": "secret"})
    assert SECRET not in resp.text


def test_knowledge_does_not_follow_linked_dir(api):
    client, root, base, outside = api
    _link(root, "ontology/knowledge", outside)
    for path in ("knowledge", "knowledge/secret"):
        assert SECRET not in client.get(f"{base}/{path}").text


def test_studio_does_not_read_linked_entries(api):
    client, root, base, outside = api
    _link(root, "ontology/object/泄露.md", outside / "secret.md")
    _link(root, "ontology/rule", outside)
    for path in ("studio/graph", "studio/entries", "studio/stats"):
        resp = client.get(f"{base}/{path}")
        assert SECRET not in resp.text and "泄露" not in resp.text, path


def test_files_content_rejects_links_into_protected_dirs(api):
    client, root, base, _outside = api
    os.symlink("../../.git/config", root / "ontology" / "object" / "配置.md")
    resp = client.get(f"{base}/files/content", params={"path": "ontology/object/配置.md"})
    assert resp.status_code in (403, 404) and "[core]" not in resp.text
    diff = client.get(f"{base}/versions/diff", params={"from": "HEAD", "path": "ontology/object/配置.md"})
    assert "[core]" not in diff.text


def test_resources_list_does_not_follow_linked_subdir(api):
    client, root, base, outside = api
    _link(root, "resource/others", outside)
    listed = client.get(f"{base}/resources").json()
    names = [item["name"] for group in listed["groups"] for item in group["items"]]
    assert "secret.md" not in names and "secret.json" not in names


def test_feedback_and_solver_do_not_follow_links(api):
    client, root, base, outside = api
    _link(root, "feedback/fb-x.json", outside / "secret.json")
    _link(root, "ontology/solver/偷看", outside)
    assert SECRET not in client.get(f"{base}/inbox").text
    assert SECRET not in client.get(f"{base}/solver/contract").text


def test_blueprint_link_is_not_read(api):
    client, root, base, outside = api
    (outside / "bp.json").write_text(json.dumps({"format": "onto-blueprint/1", "goal": SECRET}), encoding="utf-8")
    _link(root, "research/blueprint.json", outside / "bp.json")
    assert SECRET not in client.get(f"{base}/blueprint").text


def test_research_receipt_inputs_cannot_probe_outside(api, tmp_path):
    from app.services.packs import contracts

    _client, root, _base, outside = api
    review = contracts.module_for(root, "research_review")
    assert review.file_revision(Path("/dev/zero")) == ""
    os.symlink(str(outside / "secret.md"), root / "research-link.md")
    assert review.file_revision(root / "research-link.md") == ""
    assert review.file_revision(outside / "secret.md") != ""  # 普通文件照常算


def test_entry_endpoints_do_not_read_linked_files(api):
    client, root, base, outside = api
    _link(root, "ontology/object/泄露.md", outside / "secret.md")
    os.symlink("../../.git/config", root / "ontology" / "object" / "配置.md")
    for entry in ("object:泄露", "object:配置"):
        for suffix in ("", "/evidence", "/draft"):
            resp = client.get(f"{base}/studio/entries/{entry}{suffix}")
            assert resp.status_code == 404 and SECRET not in resp.text and "[core]" not in resp.text, (entry, suffix)
    diff = client.get(f"{base}/versions/diff", params={"from": "HEAD", "entry": "object:泄露"})
    assert SECRET not in diff.text
