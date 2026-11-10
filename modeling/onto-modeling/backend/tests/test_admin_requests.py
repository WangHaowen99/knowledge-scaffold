"""管理员审核申请（AD-08～AD-10）：普通用户申请登记数据源 → 管理员在列表里看到 → 补写凭据通过后登记并授权给申请人的
项目、申请人收到通知；退回要写原因，申请人在数据源页看得到。知识提名并进同一个列表。凭据是每个用例随机生成的合成值。"""
from __future__ import annotations

import secrets

from app.evidence import grants, sources
from app.services import knowledge_layers, notifications
from tests import f13_support

f13 = f13_support.f13
REQUESTS = "/api/v1/admin/requests"


def _apply(f13, headers: dict, project_id: str, **fields) -> str:
    body = f13.db_body(owner="bob", **fields)
    body.pop("masking_policy")
    response = f13.client.post("/api/v1/evidence/source-requests", json={**body, "project_id": project_id},
                               headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def _mine(f13, headers: dict, project_id: str) -> list[dict]:
    response = f13.client.get("/api/v1/evidence/sources", params={"project_id": project_id}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["my_requests"]


def test_source_request_is_approved_with_credentials_and_granted_to_the_requester(f13):
    bob, admin = f13.login("bob"), f13.login("root_a", "admin")
    project_id, _root = f13.project(owner="bob", name="订单取证")
    request_id = _apply(f13, bob, project_id)
    # 申请人马上看得到自己的申请
    assert [(item["id"], item["state"], item["display_name"]) for item in _mine(f13, bob, project_id)] == [
        (request_id, "pending", "订单库")]

    assert f13.client.get(REQUESTS, headers=bob).status_code == 403
    listed = f13.client.get(REQUESTS, params={"state": "pending"}, headers=admin).json()
    item = next(entry for entry in listed["items"] if entry["id"] == request_id)
    assert item["kind"] == "source_registration" and item["project_id"] == project_id
    assert item["requested_by"] == "bob" and item["payload"]["project_name"] == "订单取证"
    assert item["payload"]["provider"] == "db-mysql" and "secret" not in item["payload"]

    # 数据源申请通过要先写凭据
    missing = f13.client.post(f"{REQUESTS}/{request_id}/approve", json={}, headers=admin)
    assert missing.status_code == 400 and missing.json()["details"]["fields"] == {"secret": "required"}
    password = secrets.token_urlsafe(18)
    approved = f13.client.post(f"{REQUESTS}/{request_id}/approve",
                               json={"note": "已开只读账号", "secret": {"password": password}}, headers=admin)
    assert approved.status_code == 200, approved.text
    view = approved.json()
    assert view["state"] == "approved" and view["source_id"] == f13_support.DB_SOURCE and view["reviewed_by"] == "root_a"
    assert password not in approved.text
    assert sources.get(f13_support.DB_SOURCE)["owner"] == "bob"
    assert f13.fake.secrets[f13_support.DB_SOURCE] == [{"password": password}]
    assert f13_support.DB_SOURCE in grants.authorized_ids(project_id)
    note = notifications.list_notifications("bob")[0]
    assert note["kind"] == "request_reviewed" and note["project_id"] == project_id
    assert note["action"] == {"label": "去看看", "page": "sources"}

    again = f13.client.post(f"{REQUESTS}/{request_id}/approve", json={"secret": {"password": password}}, headers=admin)
    assert again.status_code == 409 and again.json()["details"] == {"state": "approved"}
    # 通过的已经是数据源，不再列在「我的申请」里
    assert _mine(f13, bob, project_id) == []


def test_rejected_request_shows_the_reason_to_the_requester(f13):
    bob, admin = f13.login("bob"), f13.login("root_a", "admin")
    project_id, _root = f13.project(owner="bob")
    request_id = _apply(f13, bob, project_id, id="crm-db", display_name="客户库")
    empty = f13.client.post(f"{REQUESTS}/{request_id}/reject", json={"reason": "  "}, headers=admin)
    assert empty.status_code == 400 and empty.json()["details"]["fields"] == {"reason": "required"}
    rejected = f13.client.post(f"{REQUESTS}/{request_id}/reject", json={"reason": "生产库不开放，请改用测试库"}, headers=admin)
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    assert sources.get("crm-db") is None
    mine = _mine(f13, bob, project_id)
    assert [(item["state"], item["note"]) for item in mine] == [("rejected", "生产库不开放，请改用测试库")]
    assert notifications.list_notifications("bob")[0]["title"] == "数据源登记申请被退回"
    assert f13.client.get(REQUESTS, params={"state": "rejected", "kind": "source_registration"},
                          headers=admin).json()["items"][0]["note"] == "生产库不开放，请改用测试库"
    assert f13.client.post(f"{REQUESTS}/esr-000000000000/reject", json={"reason": "x"},
                           headers=admin).status_code == 404


def test_admin_is_notified_with_a_link_to_the_review_tab(f13):
    project_id, _root = f13.project()
    _apply(f13, {}, project_id)  # 本地模式：通知发给本地用户
    note = notifications.list_notifications("")[0]
    assert note["kind"] == "request_pending"
    assert note["action"] == {"label": "去处理", "page": "plugin-upload", "params": {"tab": "req"}}


def test_knowledge_nominations_join_the_list(f13):
    admin = f13.login("root_a", "admin")
    project_id, _root = f13.project(owner="bob")
    record = knowledge_layers.submit_nomination(project_id, "k-restock", "industry-retail",
                                                "# 补货单金额口径\n\n按明细数量乘单价汇总（合成）。", "bob")
    listed = f13.client.get(REQUESTS, params={"kind": "knowledge_nomination"}, headers=admin).json()["items"]
    assert [(item["id"], item["state"], item["summary"]) for item in listed] == [
        (record["nomination_id"], "pending", "补货单金额口径")]
    approved = f13.client.post(f"{REQUESTS}/{record['nomination_id']}/approve", json={}, headers=admin)
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert notifications.list_notifications("bob")[0]["kind"] == "nomination_reviewed"
    assert f13.client.post(f"{REQUESTS}/{record['nomination_id']}/reject", json={"reason": "x"},
                           headers=admin).status_code == 409
    paged = f13.client.get(REQUESTS, params={"limit": 1}, headers=admin).json()
    assert len(paged["items"]) == 1
