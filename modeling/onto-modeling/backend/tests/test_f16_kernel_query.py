"""F16 内核：数据库快照的检索视图（EV-23、EV-25、EV-27、EV-28、EV-29）与代码快照的记录详情（spec §9、§10）。"""

from __future__ import annotations

import secrets

from app.evidence import query, spec
from app.kernel.writepath import ChangeTx, Origin
from tests import f13_support
from tests.f13_support import CODE, DB_SOURCE, db_anchor, db_table, make_db_bundle

f13 = f13_support.f13


def _ready(f13):
    project_id, root = f13.project()
    f13.ready_db(project_id)
    snapshot_id = f13.run_db(project_id)["result"]["snapshot_id"]
    return project_id, root, snapshot_id, f"/api/v1/projects/{project_id}/evidence"


def test_summary_tree_groups_by_database(f13):
    project_id, root, snapshot_id, base = _ready(f13)
    with ChangeTx(f13.workspaces, project_id, Origin("web", "local"), summary="测试") as tx:
        tx.write("ontology/object/客户.md", f"# 数据来源\n{db_anchor('crm', 'customer')}\n")
        tx.commit("web: 写对象")
    tree = f13.client.get(f"{base}/summary-tree", params={"snapshot": snapshot_id}).json()
    assert [(domain["name"], domain["count"]) for domain in tree["domains"]] == [("crm", 3), ("stock", 1)]
    names = [(entity["name"], entity["kind"]) for entity in tree["domains"][0]["entities"]]
    assert names == [("active_customer", "view"), ("customer", "table"), ("orders", "table")]
    customer = tree["domains"][0]["entities"][1]
    assert customer["label"] == "客户" and customer["type"] == "entity"
    assert customer["mapped_to"] is not None and tree["domains"][0]["entities"][2]["mapped_to"] is None
    # 被引用：最新完整快照被 ontology/ 里的数据库锚点引用（引用计数、清理保护对数据库锚点也生效）
    listed = f13.client.get(f"{base}/snapshots").json()["items"]
    assert listed[0]["referenced"] == 1
    assert f13.client.delete(f"{base}/snapshots/{snapshot_id}").json()["code"] == "snapshot_referenced"


def test_record_detail_for_table_and_column(f13):
    project_id, _root, snapshot_id, base = _ready(f13)
    customer = db_anchor("crm", "customer")
    shown = f13.client.get(f"{base}/record", params={"anchor": customer})
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert [field["name"] for field in body["fields"]] == ["id", "name", "phone", "level"]
    assert body["fields"][1] == {"name": "name", "type": "varchar(40)", "note": "名称", "confidence": "declared",
                                 "masked": True, "anchor": db_anchor("crm", "customer", "name"), "pk": False,
                                 "fk": False, "nullable": True}
    assert body["fields"][0]["pk"] is True and body["fields"][0]["nullable"] is False
    assert body["ddl"].startswith("CREATE TABLE customer (")
    assert body["samples"]["columns"] == ["id", "name", "phone", "level"] and body["samples"]["max"] == 20
    assert body["samples"]["shown"] == 2 and body["samples"]["rows"][0] == ["1", "王*", "130****0000", "A"]
    assert body["samples"]["snapshot_id"] == snapshot_id and body["samples"]["masked_columns"] == ["name", "phone"]
    level = next(item for item in body["profile"] if item["column"] == "level")
    assert level["top_k"] == [{"value": "A", "pct": 50.0}, {"value": "«已遮蔽»", "pct": 30.0}]
    assert level["null_pct"] == 0.0 and level["distinct"] == 3 and "stats" not in level
    ident = next(item for item in body["profile"] if item["column"] == "id")
    assert ident["stats"] == {"min": 1, "max": 1200, "null_pct": 0.0}
    phone = next(item for item in body["profile"] if item["column"] == "phone")
    assert phone["null_pct"] == 12.5 and "top_k" not in phone
    assert body["rows"] == 1200 and body["snapshot"]["id"] == snapshot_id

    column = f13.client.get(f"{base}/record", params={"anchor": db_anchor("crm", "customer", "phone")}).json()
    assert column["type"] == "field" and column["masked"] is True and column["parent_anchor"] == customer
    assert column["data_type"] == "varchar(20)" and column["nullable"] is True
    orders = f13.client.get(f"{base}/record", params={"anchor": db_anchor("crm", "orders")}).json()
    assert [(field["name"], field["pk"], field["fk"]) for field in orders["fields"]] == [
        ("id", True, False), ("customer_id", False, True), ("item_code", False, False), ("amount", False, False)]
    view = f13.client.get(f"{base}/record", params={"anchor": db_anchor("crm", "view:active_customer")}).json()
    assert [(field["name"], field["type"]) for field in view["fields"]] == [("id", "bigint"), ("level", "varchar(8)")]
    assert view["ddl"] == "select id, level from customer"
    item = f13.client.get(f"{base}/record", params={"anchor": db_anchor("stock", "item")}).json()
    assert "samples" not in item and "profile" not in item and item["rows"] == 100
    missing = f13.client.get(f"{base}/record", params={"anchor": db_anchor("crm", "nope")})
    assert missing.status_code == 404 and missing.json()["code"] == "anchor_not_found"


def test_lineage_fk_and_inferred(f13):
    project_id, _root, snapshot_id, base = _ready(f13)
    orders, customer, item = db_anchor("crm", "orders"), db_anchor("crm", "customer"), db_anchor("stock", "item")
    out = f13.client.get(f"{base}/lineage", params={"anchor": orders}).json()["items"]
    by_anchor = {entry["anchor"]: entry for entry in out}
    assert by_anchor[customer]["basis"] == "外键邻居" and by_anchor[customer]["confidence"] == "declared"
    assert by_anchor[customer]["label"] == "客户" and by_anchor[customer]["columns"] == [["customer_id", "id"]]
    assert by_anchor[item]["basis"] == "取值重合 98%" and by_anchor[item]["confidence"] == "inferred"
    assert by_anchor[item]["relation"] == f"{orders}.ref:item_code"
    back = f13.client.get(f"{base}/lineage", params={"anchor": customer}).json()["items"]
    assert [(entry["anchor"], entry["basis"]) for entry in back] == [(orders, "外键邻居（被引用）")]
    column = f13.client.get(f"{base}/lineage", params={"anchor": db_anchor("crm", "orders", "customer_id")}).json()
    assert [(entry["anchor"], entry["basis"]) for entry in column["items"]] == [(orders, "上级"),
                                                                                 (customer, "外键邻居")]


def test_er_view(f13):
    project_id, _root, snapshot_id, base = _ready(f13)
    orders, customer, item = db_anchor("crm", "orders"), db_anchor("crm", "customer"), db_anchor("stock", "item")
    er = f13.client.get(f"{base}/er", params={"snapshot": snapshot_id}).json()
    assert [table["name"] for table in er["tables"]] == ["customer", "item", "orders"]
    by_name = {table["name"]: table for table in er["tables"]}
    assert by_name["orders"]["fields"][1] == {"name": "customer_id", "type": "bigint", "pk": False, "fk": True,
                                              "masked": False}
    assert by_name["orders"]["fields"][0]["pk"] and by_name["customer"]["fields"][2]["masked"]
    assert by_name["orders"]["fields"][2]["fk"] is False  # 推断关系不算外键
    assert by_name["customer"]["label"] == "客户" and er["truncated"] is False and er["total"] == 3
    relations = {(rel["from_anchor"], rel["to_anchor"]): rel for rel in er["relations"]}
    assert relations[(orders, customer)]["confidence"] == "declared"
    assert relations[(orders, customer)]["basis"] == "外键邻居"
    assert relations[(orders, item)] == {"from_anchor": orders, "to_anchor": item, "confidence": "inferred",
                                         "basis": "取值重合 98%", "anchor": f"{orders}.ref:item_code",
                                         "columns": [["item_code", "code"]]}
    assert relations[(orders, customer)]["columns"] == [["customer_id", "id"]]

    crm = f13.client.get(f"{base}/er", params={"snapshot": snapshot_id, "domain": "crm"}).json()
    assert [table["name"] for table in crm["tables"]] == ["customer", "orders"] and len(crm["relations"]) == 1
    focused = f13.client.get(f"{base}/er", params={"snapshot": snapshot_id, "focus": customer}).json()
    assert [table["anchor"] for table in focused["tables"]] == [customer, orders]
    deeper = f13.client.get(f"{base}/er", params={"snapshot": snapshot_id, "focus": customer, "depth": 2}).json()
    assert [table["anchor"] for table in deeper["tables"]] == [customer, orders, item]
    by_column = f13.client.get(f"{base}/er", params={"snapshot": snapshot_id,
                                                      "focus": db_anchor("crm", "customer", "phone")}).json()
    assert by_column["tables"][0]["anchor"] == customer
    for params, code in (({"depth": 4}, "invalid_argument"), ({"focus": "bin:x-1/sym:a"}, "anchor_invalid"),
                         ({"focus": db_anchor("crm", "nope")}, "anchor_not_found")):
        assert f13.client.get(f"{base}/er", params={"snapshot": snapshot_id, **params}).json()["code"] == code


def test_er_caps_at_60_tables(f13):
    project_id, _root = f13.project()
    f13.ready_db(project_id, scope_allowlist=["big.*"])
    rows = []
    for number in range(70):
        rows += db_table("big", f"t{number:02d}", [("id", "bigint", "", True, False)])

    def er_bundle(job_id, job):
        return make_db_bundle(job_id, records=rows)

    f13.fake.bundle_for = er_bundle
    snapshot_id = f13.run_db(project_id)["result"]["snapshot_id"]
    er = f13.client.get(f"/api/v1/projects/{project_id}/evidence/er", params={"snapshot": snapshot_id}).json()
    assert len(er["tables"]) == 60 and er["total"] == 70 and er["truncated"] is True


def test_anchor_resolution_and_where_used_for_db(f13):
    project_id, _root, snapshot_id, base = _ready(f13)
    phone = db_anchor("crm", "customer", "phone")
    got = f13.client.post(f"/api/v1/projects/{project_id}/anchors/resolve",
                          json={"anchors": [phone, db_anchor("crm", "gone"), "db:no-snap/crm.x"]}).json()["items"]
    assert got[0]["resolved"] and got[0]["location_label"] == "crm.customer.phone"
    assert got[0]["snapshot_id"] == snapshot_id
    assert got[1]["resolved"] is False and got[1]["reason"] == "not_in_snapshot"
    assert got[2]["resolved"] is False and got[2]["reason"] == "not_available"  # 没有快照的源照旧
    with ChangeTx(f13.workspaces, project_id, Origin("web", "local"), summary="测试") as tx:
        tx.write("ontology/object/客户.md", f"# 数据来源\n`{db_anchor('crm', 'customer')}`\n\n列 {phone}。\n")
        tx.commit("web: 写对象")
    used = f13.client.get(f"{base}/where-used", params={"anchor": db_anchor("crm", "customer"), "prefix": True}).json()
    assert sorted(item["anchor"] for item in used["items"]) == [db_anchor("crm", "customer"), phone]
    assert query.parse_anchor(phone) == {"scheme": "db", "source_id": DB_SOURCE, "kind": "db",
                                         "name": "crm.customer.phone"}
    code = "code:shop-code/sym:com::example::order::OrderService::submit"
    assert query.ANCHOR.findall(f"见 {phone}。也见 `{code}`，以及 {db_anchor('crm', 'customer')}.") == [
        phone, code, db_anchor("crm", "customer") + "."]
    refs = query.references(_root)
    assert phone in refs.anchors and db_anchor("crm", "customer") in refs.anchors


def test_code_snapshot_keeps_generic_views(f13):
    f13.fake.providers.append(dict(CODE))
    from app.evidence import collector

    collector.reset()
    project_id, _root = f13.project()
    body = {"id": "shop-code", "display_name": "商城代码", "provider": "code-repo", "environment": "test",
            "connection": {"repo_url": "https://git.example.com/demo/shop.git", "ref": "main"},
            "scope_allowlist": [], "egress": [{"host": "git.example.com", "port": 443}],
            "budget": {"max_rows": 1000, "max_bytes": 1000000, "max_duration_s": 60},
            "rate_limit": {"concurrency": 1, "qps": 1, "query_timeout_s": 30},
            "secret": {"token": secrets.token_urlsafe(18)}, "grant_project_id": project_id}
    assert f13.client.post("/api/v1/admin/evidence/sources", json=body).status_code == 201

    def code(kind, anchor, name, **fields):
        record = {"id": anchor, "type": kind, "parent": None, "name": name, "label": None,
                  "evidence": {"provider": "code-repo@0.1.0", "method": "javalang", "confidence": "declared"},
                  "attrs": {}, **fields}
        record["hash"] = spec.record_hash(record)
        return record

    entity = "code:shop-code/sym:com::example::order::Order"
    rows = [code("entity", entity, "Order", attrs={"kind": "class", "orm": "jpa", "table": "orders",
                                                   "loc": {"file": "src/Order.java", "line": 12, "end_line": 40},
                                                   "snippet": "@Entity\nclass Order {}"}),
            code("field", f"{entity}::amount", "amount", parent=entity, data_type="BigDecimal",
                 attrs={"column": "amount", "signature": "BigDecimal amount",
                        "loc": {"file": "src/Order.java", "line": 20, "end_line": 20}}),
            code("rule", "code:shop-code/rule:com::example::order::Order::NotNull", "NotNull", parent=entity,
                 attrs={"kind": "not_null", "loc": {"file": "src/Order.java", "line": 19, "end_line": 19}})]

    def code_bundle(job_id, job):
        return make_db_bundle(job_id, records=rows, source="shop-code")

    f13.fake.bundle_for = code_bundle
    done = f13.run_db(project_id, source_id="shop-code")
    assert done["state"] == "succeeded", done
    base = f"/api/v1/projects/{project_id}/evidence"
    shown = f13.client.get(f"{base}/record", params={"anchor": entity}).json()
    assert shown["attrs"]["snippet"] == "@Entity\nclass Order {}" and shown["attrs"]["loc"]["line"] == 12
    assert shown["fields"] == [
        {"name": "amount", "type": "BigDecimal", "note": "", "confidence": "declared", "masked": False,
         "anchor": f"{entity}::amount"},
        {"name": "NotNull", "type": "not_null", "note": "", "confidence": "declared", "masked": False,
         "anchor": "code:shop-code/rule:com::example::order::Order::NotNull"}]
    er = f13.client.get(f"{base}/er", params={"snapshot": done["result"]["snapshot_id"]}).json()
    assert er == {"tables": [], "relations": []}
    tree = f13.client.get(f"{base}/summary-tree", params={"snapshot": done["result"]["snapshot_id"]}).json()
    assert tree["domains"][0]["entities"][0]["anchor"] == entity
    resolved = f13.client.post(f"/api/v1/projects/{project_id}/anchors/resolve", json={"anchors": [entity]}).json()
    assert resolved["items"][0]["resolved"] and resolved["items"][0]["location_label"] == "src/Order.java:12"
