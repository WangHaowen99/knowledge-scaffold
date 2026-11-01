"""服务令牌：管理员签发 / 列出 / 吊销，只存摘要，范围与项目限定，与 MCP token 分开（ADR-0020）。

用户名、项目 ID、令牌名均为合成数据。
"""

import json
import os
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import projects
from app.services import auth, service_tokens
from app.services.service_tokens import ServiceTokenError
from app.services.workspace import WorkspaceManager

ADMIN, USER = "operator_admin", "operator_a"
TOKENS = {"admin-token": {"sub": ADMIN, "role": "admin"}, "user-token": {"sub": USER, "role": "user"}}


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir, tmp_path, monkeypatch) -> TestClient:
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    monkeypatch.setattr(projects, "workspaces", WorkspaceManager(data_dir, assets))
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    return TestClient(app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_issue_returns_plaintext_once_and_stores_only_digest(data_dir):
    issued = service_tokens.issue(
        {"name": "合成服务", "scopes": ["releases.read", "feedback.write"], "project_ids": ["p1"]}, ADMIN
    )
    raw = issued["token"]
    assert raw.startswith(service_tokens.TOKEN_PREFIX)
    stored = service_tokens.store_path().read_text(encoding="utf-8")
    assert raw not in stored and "digest" in stored
    if os.name != "nt":
        assert stat.S_IMODE(service_tokens.store_path().stat().st_mode) == 0o600
    summary = issued["summary"]
    assert summary["scopes"] == ["feedback.write", "releases.read"] and summary["project_ids"] == ["p1"]
    assert "digest" not in summary and "token" not in summary
    assert summary["created_at"].endswith("Z") and summary["expires_at"] is None
    assert service_tokens.listing() == [summary]
    unrestricted = service_tokens.issue({"name": "全部项目", "scopes": ["releases.read"]})["summary"]
    assert unrestricted["project_ids"] is None


def test_store_is_separate_from_mcp_tokens(data_dir):
    service_tokens.issue({"name": "合成服务", "scopes": ["releases.read"]})
    assert service_tokens.store_path() == data_dir / "service-tokens.json"
    assert not (data_dir / "onto-mcp.sqlite").exists()


@pytest.mark.parametrize("body", [
    {"name": "", "scopes": ["releases.read"]},
    {"name": "x" * 81, "scopes": ["releases.read"]},
    {"name": "合成服务", "scopes": []},
    {"name": "合成服务", "scopes": ["ontology.read"]},
    {"name": "合成服务", "scopes": "releases.read"},
    {"name": "合成服务", "scopes": ["releases.read"], "project_ids": ["../p1"]},
    {"name": "合成服务", "scopes": ["releases.read"], "project_ids": "p1"},
    {"name": "合成服务", "scopes": ["releases.read"], "expires_in_s": 0},
    {"name": "合成服务", "scopes": ["releases.read"], "expires_in_s": True},
    {"name": "合成服务", "scopes": ["releases.read"], "expires_in_s": 400 * 86400 * 10},
])
def test_issue_rejects_invalid_input(data_dir, body):
    with pytest.raises(ServiceTokenError) as exc:
        service_tokens.issue(body)
    assert (exc.value.status, exc.value.code) == (400, "invalid_argument")
    assert exc.value.details["fields"]


def test_verify_checks_scope_project_expiry_and_revocation(data_dir):
    raw = service_tokens.issue({"name": "合成服务", "scopes": ["releases.read"], "project_ids": ["p1"]})["token"]
    assert raw.startswith("onto_svc_")
    assert service_tokens.verify(raw, "releases.read", "p1")["name"] == "合成服务"
    for token, scope, project, status, code in [
        ("", "releases.read", "p1", 401, "unauthenticated"),
        ("onto_svc_unknown", "releases.read", "p1", 401, "unauthenticated"),
        ("onto_pat_synthetic", "releases.read", "p1", 401, "unauthenticated"),
        (raw, "feedback.write", "p1", 403, "scope_forbidden"),
        (raw, "releases.read", "p2", 403, "scope_forbidden"),
    ]:
        with pytest.raises(ServiceTokenError) as exc:
            service_tokens.verify(token, scope, project)
        assert (exc.value.status, exc.value.code) == (status, code)

    expiring = service_tokens.issue({"name": "短期", "scopes": ["releases.read"], "expires_in_s": 3600})["token"]
    service_tokens.verify(expiring, "releases.read")

    def expire(data):
        for item in data["tokens"]:
            if item["name"] == "短期":
                item["expires_at"] = int(time.time()) - 1

    service_tokens._store().update(expire)
    with pytest.raises(ServiceTokenError) as exc:
        service_tokens.verify(expiring, "releases.read")
    assert exc.value.status == 401

    token_id = next(item["id"] for item in service_tokens.listing() if item["name"] == "合成服务")
    service_tokens.revoke(token_id, ADMIN)
    with pytest.raises(ServiceTokenError) as exc:
        service_tokens.verify(raw, "releases.read", "p1")
    assert exc.value.status == 401
    with pytest.raises(ServiceTokenError) as exc:
        service_tokens.revoke(token_id)
    assert exc.value.status == 404


def test_last_used_is_recorded(data_dir):
    raw = service_tokens.issue({"name": "合成服务", "scopes": ["releases.read"]})["token"]
    assert service_tokens.listing()[0]["last_used_at"] is None
    service_tokens.verify(raw, "releases.read")
    assert service_tokens.listing()[0]["last_used_at"]


def test_admin_routes_issue_list_revoke(client):
    created = client.post(
        "/api/v1/admin/service-tokens", headers=_h("admin-token"),
        json={"name": "合成服务", "scopes": ["receipts.write"], "expires_in_s": 30 * 86400},
    )
    assert created.status_code == 201
    body = created.json()
    token_id = body["summary"]["id"]
    assert body["token"].startswith("onto_svc_") and body["summary"]["created_by"] == ADMIN
    assert body["summary"]["expires_at"].endswith("Z")

    listed = client.get("/api/v1/admin/service-tokens", headers=_h("admin-token")).json()
    assert [item["id"] for item in listed["items"]] == [token_id]
    assert body["token"] not in json.dumps(listed)

    revoked = client.delete(f"/api/v1/admin/service-tokens/{token_id}", headers=_h("admin-token"))
    assert revoked.status_code == 204
    assert client.get("/api/v1/admin/service-tokens", headers=_h("admin-token")).json()["items"] == []
    stored = json.loads(service_tokens.store_path().read_text(encoding="utf-8"))
    assert stored["tokens"][0]["revoked_by"] == ADMIN  # 吊销记录留在库里供追溯
    again = client.delete(f"/api/v1/admin/service-tokens/{token_id}", headers=_h("admin-token"))
    assert again.status_code == 404 and again.json()["code"] == "not_found"


def test_admin_routes_require_admin(client):
    assert client.get("/api/v1/admin/service-tokens").status_code == 401
    assert client.get("/api/v1/admin/service-tokens", headers=_h("user-token")).status_code == 403
    denied = client.post(
        "/api/v1/admin/service-tokens", headers=_h("user-token"),
        json={"name": "合成服务", "scopes": ["releases.read"]},
    )
    assert denied.status_code == 403
    assert service_tokens.listing() == []


def test_service_token_cannot_reach_admin_or_project_routes(client):
    raw = service_tokens.issue({"name": "合成服务", "scopes": list(service_tokens.SCOPES)})["token"]
    assert client.get("/api/v1/admin/service-tokens", headers=_h(raw)).status_code == 401
    assert client.get("/api/projects", headers=_h(raw)).status_code == 401


def test_invalid_admin_body(client):
    response = client.post(
        "/api/v1/admin/service-tokens", headers={**_h("admin-token"), "Content-Type": "application/json"},
        content=b"[1, 2]",
    )
    assert response.status_code == 400
    bad_scope = client.post(
        "/api/v1/admin/service-tokens", headers=_h("admin-token"), json={"name": "x", "scopes": ["nope"]},
    )
    assert bad_scope.status_code == 400 and bad_scope.json()["code"] == "invalid_argument"
    assert bad_scope.json()["details"]["fields"]["scopes"]
