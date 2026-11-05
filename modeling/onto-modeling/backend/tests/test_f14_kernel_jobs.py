"""F14 内核：连接型采集任务（EV-13/14、EV-09）、discover 清单、sample 补充快照、入库时的个人信息扫描（spec §8、§9）。"""

from __future__ import annotations

import hashlib
import json

from app.evidence import jobs, pii, spec, storage
from tests import f13_support
from tests.f13_support import (DB_SOURCE, FAKE_CARD, FAKE_EMAIL, FAKE_ID_CARD, FAKE_PHONE, db_anchor, db_records,
                               make_db_bundle)

f13 = f13_support.f13


def _base(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/evidence"


def test_snapshot_job_spec_salt_access_log(f13):
    project_id, root = f13.project()
    f13.ready_db(project_id)
    job = f13.run_db(project_id)
    assert job["state"] == "succeeded", job
    sent = f13.fake.specs[-1]
    assert sent["cred_ref"] == f"cred://{DB_SOURCE}/1" and sent["operation"] == "snapshot"
    assert sent["egress"] == [{"host": "db.example.com", "port": 3306}]
    assert sent["rate_limit"] == {"concurrency": 1, "qps": 5, "query_timeout_s": 30}
    assert sent["masking"]["policy"] == "mask-v1" and len(sent["masking"]["salt"]) == 64
    assert sent["selector"] is None and sent["limit"] is None and sent["scope"] == ["crm.*", "stock.item"]
    assert "artifact" not in sent and f13.fake.received[job["id"]] == b""  # 连接型没有请求体
    salt = storage.root() / "salts" / f"{project_id}.key"
    assert salt.read_bytes().hex() == sent["masking"]["salt"] and oct(salt.stat().st_mode & 0o777) == "0o600"
    # 同一项目的盐不变，别的项目另一把
    f13.run_db(project_id)
    assert f13.fake.specs[-1]["masking"]["salt"] == sent["masking"]["salt"]
    assert storage.project_salt("other-project") != sent["masking"]["salt"]

    snapshot_id = job["result"]["snapshot_id"]
    manifest = json.loads((root / "resource/systems" / DB_SOURCE / snapshot_id / "manifest.json").read_text())
    assert manifest["operation"] == "snapshot" and manifest["cred_ref"] == f"cred://{DB_SOURCE}/1"
    assert manifest["access_log"] == f13_support.access_entries()
    assert jobs.get(project_id, job["id"])["access_log"] == f13_support.access_entries()

    log = f13.client.get(f"/api/v1/evidence/sources/{DB_SOURCE}/access-log", params={"project_id": project_id}).json()
    assert len(log["items"]) == 4
    first = log["items"][-1]
    assert first["statement_digest"].startswith("select:") and first["operation"] == "snapshot"
    assert {item["rows"] for item in log["items"]} == {10, 20} and log["items"][0]["at"]


def test_job_without_credential_fails_with_notify_admin(f13):
    project_id, _root = f13.project()
    f13.client.post("/api/v1/admin/evidence/sources", json=f13.db_body(grant_project_id=project_id))
    job = f13.run_db(project_id)
    assert job["state"] == "failed" and job["error"]["code"] == "credential_missing"
    assert "notify_admin" in jobs.view(job)["actions"] and f13.fake.specs == []


def test_scope_must_fall_inside_allowlist(f13):
    project_id, _root = f13.project()
    f13.ready_db(project_id)
    base = _base(project_id)
    for scope in (["erp.orders"], ["crm*"], ["stock.*"], ["crm.c *"]):
        response = f13.client.post(f"{base}/jobs/preview", json={"source_id": DB_SOURCE, "scope": scope})
        assert response.status_code == 400, scope
        assert response.json()["details"]["fields"] == {"scope": "outside_allowlist"}
        assert f13.client.post(f"{base}/jobs", json={"source_id": DB_SOURCE, "scope": scope}).status_code == 400
    job = f13.run_db(project_id, scope=["crm.customer", "crm.o*", "stock.item"])
    assert f13.fake.specs[-1]["scope"] == ["crm.customer", "crm.o*", "stock.item"] and job["state"] == "succeeded"


def test_discover_keeps_inventory_and_commits_no_snapshot(f13):
    project_id, root = f13.project()
    f13.ready_db(project_id)
    base = _base(project_id)
    before = f13.client.post(f"{base}/jobs/preview", json={"source_id": DB_SOURCE, "operation": "discover"}).json()
    assert before["summary"]["estimate"] is None and before["summary"]["tables"] is None

    job = f13.run_db(project_id, operation="discover")
    assert job["state"] == "succeeded", job
    assert "snapshot_id" not in job["result"] and job["result"]["inventory"] == {"items": 4, "skipped": 0}
    assert not (root / "resource/systems").exists()
    assert f13.client.get(f"{base}/snapshots").json()["items"] == []
    saved = storage.read_json(storage.inventory_path(project_id, DB_SOURCE))
    assert [item["object"] for item in saved["items"]] == ["crm.customer", "crm.orders", "stock.item",
                                                           "crm.view:active_customer"]
    assert job["access_log"] and "browse" not in jobs.view(job)["actions"]

    preview = f13.client.post(f"{base}/jobs/preview", json={"source_id": DB_SOURCE, "scope": ["crm.*"]}).json()
    assert preview["summary"]["tables"] == 2  # 视图不算表
    assert preview["summary"]["estimate"] == {"rows": 6200, "bytes": 490000}
    assert preview["summary"]["masking"] == {"policy": "mask-v1", "sample_rows": 20}
    one = f13.client.post(f"{base}/jobs/preview", json={"source_id": DB_SOURCE, "operation": "sample",
                                                         "sample": {"selector": "crm.customer", "limit": 5}}).json()
    assert one["summary"]["tables"] == 1 and one["summary"]["scope_text"] == "crm.customer"
    assert one["summary"]["masking"]["sample_rows"] == 5


def test_sample_becomes_supplementary_snapshot(f13):
    project_id, root = f13.project()
    f13.ready_db(project_id)
    base = _base(project_id)
    full = f13.run_db(project_id)["result"]["snapshot_id"]
    bad = (({"selector": "crm.*"}, "required"), ({"selector": "erp.x"}, "outside_allowlist"),
           ({"selector": "crm.customer", "limit": 21}, "range"), ({}, "required"))
    for sample, reason in bad:
        response = f13.client.post(f"{base}/jobs", json={"source_id": DB_SOURCE, "operation": "sample",
                                                         "sample": sample})
        assert response.status_code == 400 and reason in json.dumps(response.json()["details"]), sample

    customer = db_anchor("crm", "customer")
    fresh = [r for r in db_records() if r["id"] == f"{customer}#sample"]
    fresh[0]["attrs"]["rows"] = [[9, "张*", "130****9999", "B"]]
    fresh[0]["hash"] = spec.record_hash(fresh[0])
    f13.fake.bundle_for = lambda job_id, job: make_db_bundle(job_id, records=fresh, complete=True, operation="sample")
    job = f13.run_db(project_id, operation="sample", sample={"selector": "crm.customer", "limit": 5})
    assert job["state"] == "succeeded", job
    sent = f13.fake.specs[-1]
    assert sent["operation"] == "sample" and sent["selector"] == "crm.customer" and sent["limit"] == 5
    sample_id = job["result"]["snapshot_id"]
    manifest = json.loads((root / "resource/systems" / DB_SOURCE / sample_id / "manifest.json").read_text())
    assert manifest["operation"] == "sample" and manifest["complete"] is False and manifest["scope"] == ["crm.customer"]
    assert manifest["previous"] == full
    # 最新完整快照还是原来那份；记录详情的样本取最新的补充样本
    shown = f13.client.get(f"{base}/record", params={"anchor": customer}).json()
    assert shown["snapshot"]["id"] == full
    assert shown["samples"]["rows"] == [["9", "张*", "130****9999", "B"]]
    assert shown["samples"]["snapshot_id"] == sample_id
    # 重试沿用 selector
    f13.fake.script = [{"state": "failed", "error_code": "provider_failed", "error_message": "合成失败"}]
    failed = f13.run_db(project_id, operation="sample", sample={"selector": "crm.orders"})
    assert failed["state"] == "failed"
    f13.fake.script = [{"state": "succeeded", "phase": "完成", "done": 1, "total": 1, "unit": "表"}]
    retried = f13.client.post(f"{base}/jobs/{failed['id']}/retry", json={})
    assert retried.status_code == 202, retried.text
    f13.wait(project_id, retried.json()["id"], timeout=60.0)
    assert f13.fake.specs[-1]["selector"] == "crm.orders" and f13.fake.specs[-1]["limit"] == 20


def test_pii_rescan_on_ingest(f13):
    project_id, root = f13.project()
    f13.ready_db(project_id)
    job = f13.run_db(project_id)
    snapshot_id = job["result"]["snapshot_id"]
    folder = root / "resource/systems" / DB_SOURCE / snapshot_id
    manifest = json.loads((folder / "manifest.json").read_text())
    text = (folder / "records/sample.jsonl").read_text() + (folder / "records/profile.jsonl").read_text()
    for value in (FAKE_PHONE, FAKE_ID_CARD, FAKE_CARD, FAKE_EMAIL):
        assert value not in text, value
    assert manifest["masked_hits"] == 4 and "个人信息" in job["result"]["message"]
    sample = json.loads((folder / "records/sample.jsonl").read_text().splitlines()[0])
    assert sample["attrs"]["rows"][1] == ["2", "«已遮蔽»", "«已遮蔽»", "«已遮蔽»"] or \
        sample["attrs"]["rows"][1] == [2, "«已遮蔽»", "«已遮蔽»", "«已遮蔽»"]
    assert sample["attrs"]["rows"][0][2] == "130****0000"  # 网关遮过的不再算
    assert sample["hash"] == spec.record_hash(sample)
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    # 表结构里的列名、注释不扫个人信息（不是取值）
    fields = (folder / "records/field.jsonl").read_text()
    assert "phone" in fields


def test_pii_patterns():
    hits = [0]
    assert pii.mask_value(f"手机 {FAKE_PHONE}", hits) == pii.MASK
    assert pii.mask_value(FAKE_ID_CARD[:-1] + "X", hits) == pii.MASK
    assert pii.mask_value(FAKE_EMAIL, hits) == pii.MASK
    assert pii.mask_value("4000000000000002", hits) == pii.MASK
    assert pii.mask_value(FAKE_CARD.replace(" ", "-"), hits) == pii.MASK
    assert pii.mask_value(13000000000, hits) == pii.MASK
    assert hits == [6]
    for value in ("130****0000", "1300000000", "订单 20261105", "12345678901234567890123", "A-01", "user@", 1200,
                  "2026-11-05 08:00:00", "023-0000-0000"):
        assert pii.mask_value(value, hits) == value, value
    assert hits == [6]
    # 统计量不动；列表元素不继承键名
    profile = {"rows": 13000000000, "columns": [{"column": "phone", "null_pct": 125, "distinct": 13000000000,
                                                 "top_k": [{"value": FAKE_PHONE, "count": 3}]}]}
    masked = pii.mask_value(profile, hits)
    assert masked["rows"] == 13000000000 and masked["columns"][0]["distinct"] == 13000000000
    assert masked["columns"][0]["top_k"] == [{"value": pii.MASK, "count": 3}] and hits == [7]
    record = db_records()[-1]
    out, count = pii.mask_record(record)
    assert count == 3 and out["id"] == record["id"] and out["hash"] == spec.record_hash(out) != record["hash"]
    same, zero = pii.mask_record(db_records()[0])
    assert zero == 0 and same == db_records()[0]
