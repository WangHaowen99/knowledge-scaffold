"""资料上传的类型白名单与 20 MiB 上限（契约 1.8，移植自 develop 的同名测试）。

暂存上传（U-01）、直接上传（R-02 multipart）和服务层 save_resource 三处同一份规则：边界值收、多一个字节拒，
不支持的类型拒，被拒的替换不覆盖原文件。前端的同一份规则来自 C-01 的 limits 与 material_types。
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.api.errors import ApiError
from app.main import app
from app.services import auth, uploads
from app.services.workspace import WorkspaceManager

REAL_LIMIT = uploads.LIMITS["material_upload_bytes"]
# 边界用例把上限临时调小（读取上限的地方都在调用时取值），20 MiB 的实际值单独断言；整 20 MiB 走 multipart 和 git 太慢
LIMIT = 64 * 1024
FRONTEND_RULES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "resourceUpload.js"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setitem(uploads.LIMITS, "material_upload_bytes", LIMIT)
    monkeypatch.setitem(uploads.PURPOSE_LIMITS, "material", LIMIT)
    pid = manager.create_project("project_alpha")["id"]
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, manager, pid


def test_limit_is_20_mib_and_published_in_config(api):
    client, _, _ = api
    assert REAL_LIMIT == 20 * 1024 * 1024 == uploads.LIMITS["attachment_bytes"]
    body = client.get("/api/v1/client-config").json()
    assert body["limits"]["material_upload_bytes"] == LIMIT  # 下发的就是生效的上限
    assert body["material_types"] == list(uploads.MATERIAL_TYPES)


def test_direct_upload_accepts_exact_limit_and_rejects_one_more_byte(api):
    client, manager, pid = api
    url = f"/api/v1/projects/{pid}/resources"
    ok = client.post(url, files={"file": ("资料.pdf", b"x" * LIMIT)})
    assert ok.status_code == 201, ok.text
    assert ok.json()["material"]["size"] == LIMIT
    too_big = client.post(url, files={"file": ("资料.pdf", b"y" * (LIMIT + 1))})
    assert too_big.status_code == 413
    assert too_big.json()["code"] == "payload_too_large"
    assert too_big.json()["details"]["limit_bytes"] == LIMIT
    # 被拒的替换不覆盖原文件
    assert (manager.project_root(pid) / "resource" / "others" / "资料.pdf").read_bytes() == b"x" * LIMIT


def test_staged_upload_has_the_same_boundary(api):
    client, _, _ = api
    ok = client.post("/api/v1/uploads", files={"file": ("资料.PDF", b"x" * LIMIT)}, data={"purpose": "material"})
    assert ok.status_code == 201
    too_big = client.post("/api/v1/uploads", files={"file": ("资料.pdf", b"x" * (LIMIT + 1))},
                          data={"purpose": "material"})
    assert too_big.status_code == 413 and too_big.json()["code"] == "payload_too_large"


@pytest.mark.parametrize("filename", ["程序.exe", "配置.json", "图形.svg", "动图.gif", "没有扩展名"])
def test_unsupported_types_are_rejected_everywhere(api, filename):
    client, manager, pid = api
    staged = client.post("/api/v1/uploads", files={"file": (filename, b"x")}, data={"purpose": "material"})
    assert staged.status_code == 415 and staged.json()["code"] == "unsupported_media_type"
    assert staged.json()["details"]["allowed"] == list(uploads.MATERIAL_TYPES)
    direct = client.post(f"/api/v1/projects/{pid}/resources", files={"file": (filename, b"x")})
    assert direct.status_code == 415
    with pytest.raises(ApiError) as error:
        manager.save_resource(pid, "others", filename, b"x")
    assert error.value.status == 415


def test_service_layer_cannot_be_bypassed(api):
    _, manager, pid = api
    assert manager.save_resource(pid, "others", "资料.TXT", b"x" * LIMIT)["size"] == LIMIT
    with pytest.raises(ApiError) as error:
        manager.save_resource(pid, "others", "资料.txt", b"x" * (LIMIT + 1))
    assert (error.value.status, error.value.code) == (413, "payload_too_large")


def test_frontend_fallback_rules_match_backend():
    """前端没取到 C-01 时用的兜底清单必须和后端一致，两边不各写一份不同的规则。"""
    source = FRONTEND_RULES.read_text(encoding="utf-8")
    listed = re.search(r"RESOURCE_TYPES = \[([^\]]*)\]", source).group(1)
    assert re.findall(r"'(\.[a-z0-9]+)'", listed) == list(uploads.MATERIAL_TYPES)
    assert "MAX_RESOURCE_BYTES = 20 * 1024 * 1024" in source
