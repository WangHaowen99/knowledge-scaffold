"""F14 内核：连接型数据源的登记、凭据（EV-02/04/06/07）、测试连接（EV-08）、提供方（ADR-0084；spec §8）。

凭据都是每个用例随机生成的合成值；断言它经内核转交给了网关，但数据目录、日志、审计、响应里都找不到。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from app import config, evidence
from app.evidence import collector, sources
from tests import f13_support
from tests.f13_support import CODE, DB_SOURCE

f13 = f13_support.f13
ADMIN_BASE = "/api/v1/admin/evidence/sources"


def _files_containing(root: Path, needle: str) -> list[str]:
    data = needle.encode("utf-8")
    return [str(path) for path in root.rglob("*") if path.is_file() and data in path.read_bytes()]


def test_secret_write_rotate_delete_and_cred_ref(f13):
    project_id, _root = f13.project()
    first, second = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    created = f13.ready_db(project_id, secret=first)
    assert created["cred_ref"] == f"cred://{DB_SOURCE}/1"
    assert created["secret_fields"] == [{"name": "password", "set": True, "written_at": "2026-11-05T08:00:00.000Z"}]
    assert f13.fake.secrets[DB_SOURCE] == [{"password": first}]
    assert "password" not in created["connection"]

    rotated = f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret", json={"fields": {"password": second}})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json() == {"cred_ref": f"cred://{DB_SOURCE}/2", "written_at": "2026-11-05T08:00:00.000Z"}
    assert sources.get(DB_SOURCE)["cred_ref"] == f"cred://{DB_SOURCE}/2"
    # EV-04 带 secret 也转交：再出一个版本
    patched = f13.client.patch(f"{ADMIN_BASE}/{DB_SOURCE}", json={"display_name": "订单库 2",
                                                                   "secret": {"password": first}})
    assert patched.status_code == 200 and patched.json()["cred_ref"] == f"cred://{DB_SOURCE}/3"

    unknown = f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret", json={"fields": {"token": second}})
    assert unknown.status_code == 400 and second not in unknown.text
    empty = f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret", json={"fields": {}})
    assert empty.status_code == 400

    removed = f13.client.delete(f"{ADMIN_BASE}/{DB_SOURCE}/secret")
    assert removed.status_code == 204
    assert f13.fake.deleted_secrets == [DB_SOURCE]
    view = f13.client.get("/api/v1/evidence/sources").json()["items"][0]
    assert view["cred_ref"] is None and view["secret_fields"] == [{"name": "password", "set": False,
                                                                    "written_at": None}]
    # 网关里本来就没有也算删掉
    assert f13.client.delete(f"{ADMIN_BASE}/{DB_SOURCE}/secret").status_code == 204
    # 自助类（REA）不用凭据
    artifact = f13.upload(b"synthetic-electron-app")
    assert f13.register(artifact).status_code == 201
    rea = f13.client.put(f"{ADMIN_BASE}/{f13_support.SOURCE}/secret", json={"fields": {"password": first}})
    assert rea.status_code == 400


def test_plaintext_never_lands_in_data_dir_logs_or_audit(f13, caplog):
    caplog.set_level(logging.DEBUG)
    project_id, _root = f13.project()
    plain = [secrets.token_urlsafe(24) for _ in range(4)]
    f13.ready_db(project_id, secret=plain[0])
    rotated = f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret", json={"fields": {"password": plain[1]}})
    assert rotated.status_code == 200
    # 网关拒绝时：错误码带回，消息里回显的明文遮掉；登记撤回
    f13.fake.secret_error = (422, "secret_rejected", f"不接受 {plain[2]}")
    failed = f13.client.post(ADMIN_BASE, json=f13.db_body(id="other-db", secret={"password": plain[2]}))
    assert failed.status_code == 422 and failed.json()["code"] == "secret_rejected"
    assert plain[2] not in failed.text and "«已遮蔽»" in failed.json()["message"]
    assert sources.get("other-db") is None
    # 鉴权类错误不原样透传（免得前端当成登录过期）
    f13.fake.secret_error = (401, "ticket_invalid", plain[3])
    upstream = f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret", json={"fields": {"password": plain[3]}})
    assert upstream.status_code == 502 and upstream.json()["code"] == "upstream_unavailable"
    assert plain[3] not in upstream.text
    # EV-04 写凭据失败：改动回滚
    patched = f13.client.patch(f"{ADMIN_BASE}/{DB_SOURCE}",
                               json={"display_name": "改了", "secret": {"password": plain[3]}})
    assert patched.status_code == 502 and sources.get(DB_SOURCE)["display_name"] == "订单库"
    f13.fake.secret_error = None
    job = f13.run_db(project_id)
    assert job["state"] == "succeeded", job

    assert f13.fake.secrets[DB_SOURCE] == [{"password": plain[0]}, {"password": plain[1]}]
    data_dir = Path(config.DATA_DIR)
    assert list((data_dir / "audit").glob("*.jsonl")), "审计应该写了"
    for value in plain:
        assert _files_containing(data_dir, value) == [], value
        assert value not in caplog.text
    # 任务说明里只有凭据引用
    assert f13.fake.specs[-1]["cred_ref"] == f"cred://{DB_SOURCE}/2"
    assert all(value not in str(f13.fake.specs) for value in plain)


def test_connection_registration_rules(f13):
    bob, admin = f13.login("bob"), f13.login("root_a", "admin")
    body = f13.db_body()
    denied = f13.client.post(ADMIN_BASE, json=body, headers=bob)
    assert denied.status_code == 403 and denied.json()["code"] == "forbidden"
    assert f13.client.post("/api/v1/evidence/source-requests", json=body, headers=bob).status_code == 202

    def rejected(**changes) -> dict:
        response = f13.client.post(ADMIN_BASE, json={**body, **changes}, headers=admin)
        assert response.status_code == 400, response.text
        return response.json()["details"]["fields"]

    secret = secrets.token_urlsafe(12)
    leaked = f13.client.post(ADMIN_BASE, json={**body, "connection": {**body["connection"], "password": secret}},
                             headers=admin)
    assert leaked.status_code == 400 and secret not in leaked.text
    assert rejected(connection={**body["connection"], "schema": "x"}) == {"connection.schema": "unknown"}
    assert rejected(connection={"host": "db.example.com", "port": 3306, "database": "crm"}) == {
        "connection.username": "required"}
    assert rejected(egress=[]) == {"egress": "required"}
    assert rejected(egress=[{"host": "other.example.com", "port": 3306}]) == {"egress": "missing_target"}
    assert rejected(egress=[{"host": "db.example.com", "port": 3307}]) == {"egress": "missing_target"}
    assert rejected(scope_allowlist=[]) == {"scope_allowlist": "required"}
    assert rejected(scope_allowlist=["crm.*x"]) == {"scope_allowlist": "pattern"}
    assert rejected(rate_limit={"concurrency": 1, "qps": 0, "query_timeout_s": 30}) == {"rate_limit": "positive"}
    assert rejected(secret={"token": "x"}) == {"fields.token": "unknown"}
    assert rejected(masking_policy="mask-v3") == {"masking_policy": "unsupported"}
    assert rejected(masking_policy="none") == {"masking_policy": "unsupported"}
    # 端口写成文字也认；主机大小写不敏感
    ok = f13.client.post(ADMIN_BASE, json={**body, "connection": {**body["connection"], "port": "3306",
                                                                   "host": "DB.example.com"}}, headers=admin)
    assert ok.status_code == 201, ok.text
    assert ok.json()["connection"]["port"] == 3306 and ok.json()["cred_ref"] is None
    defaulted = f13.client.post(ADMIN_BASE, json={**body, "id": "dflt-db", "masking_policy": None}, headers=admin)
    assert defaulted.status_code == 201 and defaulted.json()["masking_policy"] == "mask-v1"
    # 修改后整体再核一遍：换了主机，出口白名单也要跟着
    moved = f13.client.patch(f"{ADMIN_BASE}/{DB_SOURCE}", json={"connection": {**body["connection"],
                                                                                "host": "db2.example.com"}},
                             headers=admin)
    assert moved.status_code == 400 and moved.json()["details"]["fields"] == {"egress": "missing_target"}
    assert f13.client.patch(f"{ADMIN_BASE}/{DB_SOURCE}", json={
        "connection": {**body["connection"], "host": "db2.example.com"},
        "egress": [{"host": "db2.example.com", "port": 3306}]}, headers=admin).status_code == 200


def test_code_repo_registration(f13):
    f13.fake.providers.append(dict(CODE))
    collector.reset()
    body = {"id": "shop-code", "display_name": "商城代码", "provider": "code-repo", "environment": "test",
            "connection": {"repo_url": "https://git.example.com/demo/shop.git", "ref": "main"},
            "scope_allowlist": [], "egress": [{"host": "git.example.com", "port": 443}],
            "budget": {"max_rows": 100000, "max_bytes": 10000000, "max_duration_s": 600},
            "rate_limit": {"concurrency": 1, "qps": 5, "query_timeout_s": 30}}
    for url, reason in (("http://git.example.com/demo/shop.git", "https"),
                        ("https://bot:abc@git.example.com/demo/shop.git", "userinfo")):
        response = f13.client.post(ADMIN_BASE, json={**body, "connection": {"repo_url": url, "ref": "main"}})
        assert response.status_code == 400 and response.json()["details"]["fields"] == {"connection.repo_url": reason}
        assert "abc" not in response.text
    wrong_port = f13.client.post(ADMIN_BASE, json={**body, "egress": [{"host": "git.example.com", "port": 22}]})
    assert wrong_port.status_code == 400
    token = secrets.token_urlsafe(18)
    created = f13.client.post(ADMIN_BASE, json={**body, "secret": {"token": token}})
    assert created.status_code == 201, created.text
    assert created.json()["cred_ref"] == "cred://shop-code/1" and created.json()["scope_allowlist"] == []
    assert created.json()["masking_policy"] == "none"  # 缺省按提供方
    assert f13.client.patch(f"{ADMIN_BASE}/shop-code", json={"masking_policy": "mask-v1"}).status_code == 400
    assert f13.fake.secrets["shop-code"] == [{"token": token}]
    # 删登记时凭据一起删掉
    assert f13.client.delete(f"{ADMIN_BASE}/shop-code").status_code == 204
    assert "shop-code" in f13.fake.deleted_secrets and "shop-code" not in f13.fake.secrets


def test_connection_test_goes_through_gateway(f13):
    project_id, _root = f13.project()
    f13.client.post(ADMIN_BASE, json=f13.db_body(grant_project_id=project_id))
    url = f"/api/v1/evidence/sources/{DB_SOURCE}/test"
    missing = f13.client.post(url, json={"project_id": project_id}).json()
    assert missing["ok"] is False and missing["ticket"] is None and f13.fake.tests == []
    assert f13.client.put(f"{ADMIN_BASE}/{DB_SOURCE}/secret",
                          json={"fields": {"password": secrets.token_urlsafe(12)}}).status_code == 200

    tested = f13.client.post(url, json={"project_id": project_id})
    assert tested.status_code == 200, tested.text
    result = tested.json()
    assert result["ok"] and [check["name"] for check in result["checks"]] == [
        "connectivity", "readonly_account", "statement_allowlist"]
    assert result["checks"][0]["latency_ms"] == 12
    assert len(result["ticket"]["id_prefix"]) == 8 and result["ticket"]["expires_in_s"] == 600
    assert result["ticket"]["consumed"] is True
    sent = f13.fake.tests[-1]
    assert sent == {"source": {"id": DB_SOURCE, "provider": "db-mysql",
                               "connection": {"host": "db.example.com", "port": 3306, "database": "crm",
                                              "username": "reader"}},
                    "cred_ref": f"cred://{DB_SOURCE}/1", "egress": [{"host": "db.example.com", "port": 3306}],
                    "rate_limit": {"concurrency": 1, "qps": 5, "query_timeout_s": 30}}
    # 只读自检不过：照样 200，前端按 ok 判断；checks 原样转（不一定三项都有）
    failing = {"name": "readonly_account", "ok": False, "detail": "账号能写", "code": "account_not_readonly"}
    f13.fake.test_result = {"ok": False, "checks": [failing]}
    answer = f13.client.post(url, json={"project_id": project_id}).json()
    assert answer["ok"] is False and answer["checks"] == [failing]

    f13.fake.down = True
    collector.reset()
    f13.monkeypatch.setenv("ONTO_WEB_CONNECTOR_GATEWAY_URL", "http://127.0.0.1:9")
    collector.gateway.reset()
    down = f13.client.post(url, json={"project_id": project_id})
    assert down.status_code == 503 and down.json()["code"] == "gateway_unavailable"


def test_available_providers_include_connection_kinds(f13):
    f13.fake.providers.append(dict(CODE))
    collector.reset()
    assert evidence.available_providers() == {"rea", "db", "code"}
    f13.fake.providers[-1]["installed"] = False
    f13.fake.providers[1]["installed"] = False
    collector.reset()
    assert evidence.available_providers() == {"rea"}
