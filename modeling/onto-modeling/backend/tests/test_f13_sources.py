"""F13 数据源登记（EV-01～EV-05、EV-08～EV-12、EV-30）：自助登记与可见性、权限、提供方列表、授权。"""

from __future__ import annotations

import json

from app.evidence import collector, storage
from app.services import notifications
from tests import f13_support
from tests.f13_support import SOURCE

f13 = f13_support.f13

ADMIN_BASE = "/api/v1/admin/evidence/sources"


def test_self_service_registration_visibility_and_grants(f13):
    alice, bob, admin = f13.login("alice"), f13.login("bob"), f13.login("root_a", "admin")
    alice_project, _ = f13.project(owner="alice")
    bob_project, _ = f13.project(owner="bob", name="另一个")
    artifact = f13.upload(b"synthetic-app", headers=alice)

    no_note = f13.register(artifact, headers=alice, authorization_note="")
    assert no_note.status_code == 400 and "authorization_note" in no_note.json()["details"]["fields"]
    created = f13.register(artifact, headers=alice, owner="someone_else", grant_project_id=alice_project)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["owner"] == "alice" and body["environment"] == "test" and body["self_service"]
    assert body["provider"] == {"id": "rea-static", "version": "0.1.0", "label": "桌面程序逆向"}
    assert body["connection"]["artifact_sha256"] == artifact["sha256"] and body["authorized_for_project"] is True
    assert body["budget"] == {"max_rows": 200000, "max_bytes": 268435456, "max_duration_s": 1800}
    assert f13.register(artifact, headers=alice).json()["code"] == "source_id_taken"

    # 自助登记只有本人和管理员看得到
    def listed(headers, **params):
        return f13.client.get("/api/v1/evidence/sources", params=params, headers=headers).json()
    assert [item["id"] for item in listed(alice)["items"]] == [SOURCE]
    assert listed(bob)["items"] == []
    assert [item["id"] for item in listed(admin)["items"]] == [SOURCE]
    mine = listed(alice, project_id=alice_project, filter="authorized")
    assert mine["authorized_count"] == 1 and mine["items"][0]["authorized_for_project"] is True

    # 别人改不了、删不了、授权不了
    assert f13.client.patch(f"{ADMIN_BASE}/{SOURCE}", json={"display_name": "x"}, headers=bob).status_code == 404
    assert f13.client.delete(f"{ADMIN_BASE}/{SOURCE}", headers=bob).status_code == 404
    grant = f13.client.put(f"/api/v1/projects/{bob_project}/evidence/grants/{SOURCE}", json={"authorized": True},
                           headers=bob)
    assert grant.status_code == 404
    # 登记人换版本：PATCH 成新上传的程序
    new_artifact = f13.upload(b"synthetic-app-v2", headers=alice)
    patched = f13.client.patch(f"{ADMIN_BASE}/{SOURCE}", headers=alice, json={
        "display_name": "演示桌面程序 v2", "connection": {"artifact_sha256": new_artifact["sha256"],
                                                    "artifact_filename": "demo-v2.zip"}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["connection"]["artifact_sha256"] == new_artifact["sha256"]
    assert patched.json()["scope_summary"] == "demo-v2.zip"
    missing = f13.client.patch(f"{ADMIN_BASE}/{SOURCE}", headers=alice,
                               json={"connection": {"artifact_sha256": "1" * 64}})
    assert missing.status_code == 422 and missing.json()["code"] == "artifact_not_found"

    grants = f13.client.get(f"/api/v1/projects/{alice_project}/evidence/grants", headers=alice).json()["items"]
    assert grants[0]["source_id"] == SOURCE and grants[0]["authorized"] and grants[0]["granted_by"] == "alice"
    revoked = f13.client.put(f"/api/v1/projects/{alice_project}/evidence/grants/{SOURCE}", json={"authorized": False},
                             headers=alice)
    assert revoked.json()["authorized"] is False

    assert f13.client.delete(f"{ADMIN_BASE}/{SOURCE}", headers=alice).status_code == 204
    assert listed(admin)["items"] == []


def test_credential_providers_need_admin(f13):
    bob, admin = f13.login("bob"), f13.login("root_a", "admin")
    body = {"id": "orders-db", "display_name": "订单库", "provider": "db-mysql", "environment": "test",
            "connection": {"host": "db.example.com", "port": 3306, "database": "orders"},
            "scope_allowlist": ["orders.*"], "egress": [{"host": "db.example.com", "port": 3306}],
            "budget": {"max_rows": 1000, "max_bytes": 1000000, "max_duration_s": 60},
            "rate_limit": {"concurrency": 1, "qps": 2, "query_timeout_s": 30}, "masking_policy": "mask-v3",
            "owner": "bob"}
    denied = f13.client.post(ADMIN_BASE, json=body, headers=bob)
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    secret = f13.client.post(ADMIN_BASE, json={**body, "secret": {"password": "x"}}, headers=admin)
    assert secret.status_code == 501
    incomplete = f13.client.post(ADMIN_BASE, json={**body, "budget": None}, headers=admin)
    assert incomplete.status_code == 400
    created = f13.client.post(ADMIN_BASE, json=body, headers=admin)
    assert created.status_code == 201, created.text
    assert created.json()["owner"] == "bob" and not created.json()["self_service"]
    assert created.json()["secret_fields"] == [{"name": "password", "set": False, "written_at": None}]
    # 管理员登记的大家都看得到，但连接参数只给管理员和负责人
    view = f13.client.get("/api/v1/evidence/sources", headers=f13.login("carol")).json()["items"][0]
    assert view["id"] == "orders-db" and view["connection"] == {} and view["egress"] == []
    assert f13.client.patch(f"{ADMIN_BASE}/orders-db", json={"display_name": "x"}, headers=bob).status_code == 403
    unknown = f13.client.post(ADMIN_BASE, json={**body, "id": "other-db", "provider": "nope"}, headers=admin)
    assert unknown.status_code == 409 and unknown.json()["code"] == "provider_not_installed"
    # 凭据接口这一轮仍是 501
    assert f13.client.put(f"{ADMIN_BASE}/orders-db/secret", json={}, headers=admin).status_code == 501


def test_providers_and_gateway_down(f13):
    body = f13.client.get("/api/v1/evidence/providers").json()
    assert body["gateway"]["status"] == "ok"
    rea = next(item for item in body["items"] if item["id"] == "rea-static")
    assert rea["self_service"] and rea["input"] == "artifact" and rea["kind"] == "rea" and rea["installed"]
    assert rea["max_artifact_bytes"] == 1024 * 1024 * 1024
    db = next(item for item in body["items"] if item["id"] == "db-mysql")
    assert db["input"] == "connection" and db["max_artifact_bytes"] is None
    assert db["cred_fields"] == [{"name": "password", "secret": True, "label": "密码"}]
    from app import evidence

    assert evidence.available_providers() == {"rea", "db"}
    f13.fake.providers[0]["installed"] = False
    collector.reset()
    assert evidence.available_providers() == {"db"}

    f13.monkeypatch.delenv("ONTO_WEB_CONNECTOR_GATEWAY_URL")
    collector.reset()
    collector.gateway.reset()
    down = f13.client.get("/api/v1/evidence/providers").json()
    assert down["gateway"]["status"] == "down" and down["items"] == []
    assert evidence.available_providers() == set()
    artifact_sha = "2" * 64
    blocked = f13.register({"sha256": artifact_sha, "filename": "x", "size": 1})
    assert blocked.status_code == 503 and blocked.json()["code"] == "gateway_unavailable"


def test_test_connection_access_log_and_summary(f13):
    project_id, _root = f13.project()
    artifact = f13.ready_source(project_id)
    tested = f13.client.post(f"/api/v1/evidence/sources/{SOURCE}/test", json={"project_id": project_id}).json()
    assert tested["ok"] and [check["name"] for check in tested["checks"]] == ["connectivity"]
    storage.blob_path(artifact["sha256"]).unlink()
    broken = f13.client.post(f"/api/v1/evidence/sources/{SOURCE}/test", json={}).json()
    assert broken["ok"] is False and "blob" in broken["checks"][0]["detail"]
    f13.upload(b"synthetic-electron-app")  # 放回去

    f13.snapshot(project_id)
    log = f13.client.get(f"/api/v1/evidence/sources/{SOURCE}/access-log", params={"project_id": project_id}).json()
    assert len(log["items"]) == 1 and log["items"][0]["statement_digest"] == f"artifact:{artifact['sha256'][:12]}"
    assert log["items"][0]["rows"] == 8 and log["next_cursor"] is None

    summary = f13.client.get(f"/api/v1/projects/{project_id}/evidence/summary").json()
    assert summary["authorized_sources"] == [{"id": SOURCE, "display_name": "演示桌面程序", "environment": "test"}]
    assert summary["snapshots"]["count"] == 1 and summary["jobs_this_month"]["succeeded"] == 1
    assert summary["gateway"] == {"status": "ok", "providers_loaded": 2}
    other, _ = f13.project(name="没授权")
    denied = f13.client.post(f"/api/v1/evidence/sources/{SOURCE}/test", json={"project_id": other})
    assert denied.status_code == 403 and denied.json()["code"] == "source_not_authorized"


def test_requests_and_notify_admin(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    requested = f13.client.post("/api/v1/evidence/source-requests", json={
        "id": "orders-db", "display_name": "订单库", "provider": "db-mysql", "environment": "production"})
    assert requested.status_code == 202
    request_id = requested.json()["request_id"]
    saved = json.loads((storage.root() / "requests" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert saved["kind"] == "register" and saved["body"]["id"] == "orders-db"
    assert f13.client.post("/api/v1/evidence/source-requests",
                           json={"id": "x-db", "display_name": "x", "provider": "db-mysql",
                                 "secret": {"password": "x"}}).status_code == 400
    notified = f13.client.post(f"/api/v1/evidence/sources/{SOURCE}/notify-admin",
                               json={"project_id": project_id, "reason": "connection_failed"})
    assert notified.status_code == 202
    kinds = [item["kind"] for item in notifications.list_notifications("")]
    assert kinds.count("request_pending") == 2


def test_pack_not_enabled(f13):
    from app.routers import evidence as evidence_router
    from app.services import research_view

    f13.monkeypatch.setattr(evidence_router, "_pack_enabled", lambda root: research_view.pack_enabled(root, "rev"))
    project_id, _root = f13.project()
    response = f13.client.get(f"/api/v1/projects/{project_id}/evidence/snapshots")
    assert response.status_code == 409 and response.json()["code"] == "pack_not_enabled"
    # 锚点解析不受包限制
    assert f13.client.get(f"/api/v1/projects/{project_id}/anchors/resolve",
                          params={"anchor": f"bin:{SOURCE}/sym:Order"}).status_code == 200
