import base64
import io
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from app.services.modeling_package_sync import sync_workspace_package
from app.services.modeling_mcp import ModelingMcpError
from app.services.workspace import WorkspaceManager


@pytest.fixture
def workspace(tmp_path):
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit/templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# Synthetic rules")
    manager = WorkspaceManager(tmp_path / "data", assets)
    meta = manager.create_project("Synthetic package", owner="operator_a")
    meta = manager._update_meta(meta["id"], modeling_project_id="synthetic-core")
    return manager, meta


def test_empty_workspace_is_not_synced(workspace, monkeypatch):
    manager, meta = workspace
    monkeypatch.setattr("app.services.modeling_mcp._management_request", lambda *a, **kw: pytest.fail("unexpected request"))
    assert sync_workspace_package(meta, None, manager)["synced"] is False


def test_sync_receipt_retry_draft_and_conflict_preserve_local_state(workspace, monkeypatch):
    manager, meta = workspace
    target = manager.project_root(meta["id"]) / "ontology/object/Item.md"
    target.write_text("# Item\n\n# 字段\n\n| 字段名 | 中文名 | 数据类型 | 是否必填 |\n| --- | --- | --- | --- |\n| id | 编号 | string | 是 |\n", encoding="utf-8")
    calls = []

    def sync(method, path, request, body, **kw):
        calls.append(body)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(body["archive"]))) as zf:
            manifest = zf.read(next(n for n in zf.namelist() if n.endswith("/manifest.yaml")))
            assert b"status: draft" in manifest
        return {"revision": "synthetic-revision", "synced": True}

    monkeypatch.setattr("app.services.modeling_mcp._management_request", sync)
    assert sync_workspace_package(meta, SimpleNamespace(), manager)["synced"]
    assert sync_workspace_package(meta, SimpleNamespace(), manager)["changed"] is False
    assert len(calls) == 1
    receipt = manager.get_project(meta["id"])
    target.write_text(target.read_text(encoding="utf-8") + "\nSynthetic local update", encoding="utf-8")

    def conflict(method, path, request, body, **kw):
        assert body["expected_revision"] == "synthetic-revision"
        raise ModelingMcpError("Revision conflict", 409)

    monkeypatch.setattr("app.services.modeling_mcp._management_request", conflict)
    with pytest.raises(ModelingMcpError):
        sync_workspace_package(meta, SimpleNamespace(), manager)
    after = manager.get_project(meta["id"])
    assert after["modeling_package_digest"] == receipt["modeling_package_digest"]
    assert after["modeling_package_revision"] == receipt["modeling_package_revision"]