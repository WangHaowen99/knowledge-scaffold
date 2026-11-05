"""F14 数据库提供方连真库（ADR-0084；证据采集接口 §9）：测试床 `connectors/testbed/db`，两个库各跑一遍。

没给 `F14_DB_MYSQL_HOST` / `F14_DB_PG_HOST` 或没装驱动就跳过。账号密码：`F14_DB_RO_PASSWORD`、`F14_DB_RW_PASSWORD`。
"""

from __future__ import annotations

import io
import json
import os
import time
from collections import Counter

import pytest

from app.connector_gateway.conn import db_common, db_mysql, db_postgres
from app.connector_gateway.conn.context import ConnError, Context

SALT = "c3" * 16

DATABASES = {
    "mysql": {"module": db_mysql, "provider": "db-mysql", "driver": "pymysql", "env": "F14_DB_MYSQL", "port": 3306,
              "database": "shop", "procs": 1, "rules": 0},
    "postgres": {"module": db_postgres, "provider": "db-postgres", "driver": "pg8000", "env": "F14_DB_PG",
                 "port": 5432, "database": "shopdb", "procs": 2, "rules": 1},
}

# 测试床 shop 里的对象（两个库一样）
EXPECTED = {"entity": 8, "field": 48, "constraint": 16, "relation": 7, "profile": 7, "sample": 7}


@pytest.fixture(params=list(DATABASES))
def db(request):
    spec = DATABASES[request.param]
    host = os.environ.get(f"{spec['env']}_HOST")
    if not host or not os.environ.get("F14_DB_RO_PASSWORD"):
        pytest.skip(f"没给 {spec['env']}_HOST / F14_DB_RO_PASSWORD")
    pytest.importorskip(spec["driver"])
    return {**spec, "name": request.param, "host": host,
            "port": int(os.environ.get(f"{spec['env']}_PORT") or spec["port"]),
            "database": os.environ.get(f"{spec['env']}_DATABASE") or spec["database"]}


def _head(db, operation, *, user="onto_ro", scope=("shop.*",), budget=None, selector=None, limit=None):
    return {
        "provider": db["provider"], "operation": operation,
        "source": {"id": "shop-db", "provider": db["provider"],
                   "connection": {"host": db["host"], "port": db["port"], "database": db["database"], "username": user}},
        "scope": list(scope), "budget": budget or {}, "rate_limit": {"qps": 0, "query_timeout_s": 20},
        "masking": {"policy": "mask-v1", "salt": SALT}, "selector": selector, "limit": limit,
    }


class Counting:
    """包住驱动连接，数真正发出去的语句。"""

    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []

    def cursor(self):
        outer = self
        inner = self.conn.cursor()

        class Cursor:
            def execute(self, sql, *args):
                outer.executed.append(sql)
                return inner.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(inner, name)

        return Cursor()

    def close(self):
        self.conn.close()


def _run(db, operation, monkeypatch, *, rw=False, **head):
    dialect = db["module"].DIALECT
    original = type(dialect).connect
    holder = {}

    def connect(ctx):
        holder["conn"] = Counting(original(dialect, ctx))
        return holder["conn"]

    monkeypatch.setattr(dialect, "connect", connect)
    password = os.environ.get("F14_DB_RW_PASSWORD" if rw else "F14_DB_RO_PASSWORD")
    out = io.StringIO()
    ctx = Context(_head(db, operation, user="onto_rw" if rw else "onto_ro", **head), {"password": password}, out,
                  scheme="db")
    started = time.monotonic()
    error = None
    try:
        getattr(db["module"], operation)(ctx)
    except ConnError as exc:
        error = exc
    elapsed = int((time.monotonic() - started) * 1000)
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    executed = holder["conn"].executed if "conn" in holder else []
    return events, error, out.getvalue(), executed, elapsed


def _records(events):
    return [event["record"] for event in events if event["event"] == "record"]


def _originals(db) -> set[str]:
    """测试床里的原始个人信息（只读账号直接查），用来确认快照里一个都没漏出来。"""
    password = os.environ["F14_DB_RO_PASSWORD"]
    queries = ["SELECT customer_name, phone, id_card, password_hash, email, address FROM shop.customers",
               "SELECT receiver_name, receiver_phone, shipping_address FROM shop.orders",
               "SELECT card_no FROM shop.payments WHERE card_no IS NOT NULL"]
    if db["name"] == "mysql":
        import pymysql
        conn = pymysql.connect(host=db["host"], port=db["port"], user="onto_ro", password=password,
                               database=db["database"])
    else:
        import pg8000.dbapi
        conn = pg8000.dbapi.connect(host=db["host"], port=db["port"], user="onto_ro", password=password,
                                    database=db["database"])
    values: set[str] = set()
    try:
        cursor = conn.cursor()
        for sql in queries:
            cursor.execute(sql)
            values.update(str(value) for row in cursor.fetchall() for value in row if value is not None)
    finally:
        conn.close()
    return values


def test_test_operation(db, monkeypatch):
    events, error, raw, _, _ = _run(db, "test", monkeypatch)
    assert error is None
    checks = {check["name"]: check for check in events[-1]["checks"]}
    assert all(check["ok"] for check in checks.values()), checks
    assert checks["connectivity"]["latency_ms"] >= 0
    events, error, raw, _, _ = _run(db, "test", monkeypatch, rw=True)
    checks = {check["name"]: check for check in events[-1]["checks"]}
    assert checks["connectivity"]["ok"] and not checks["readonly_account"]["ok"]
    assert os.environ["F14_DB_RW_PASSWORD"] not in raw and db["host"] not in raw


def test_write_account_is_refused(db, monkeypatch):
    events, error, raw, executed, _ = _run(db, "snapshot", monkeypatch, rw=True)
    assert error is not None and error.code == "account_not_readonly"
    assert not _records(events)
    assert not any(" FROM `shop`.`" in sql or ' FROM "shop"."' in sql for sql in executed)


def test_session_is_read_only_even_past_the_allowlist(db, monkeypatch):
    """只读三重保证之二独立生效：写账号进了只读会话，绕过白名单直接发写语句也会被库拒绝。"""
    dialect = db["module"].DIALECT
    ctx = Context(_head(db, "snapshot", user="onto_rw"), {"password": os.environ.get("F14_DB_RW_PASSWORD")},
                  io.StringIO(), scheme="db")
    if not os.environ.get("F14_DB_RW_PASSWORD"):
        pytest.skip("没给 F14_DB_RW_PASSWORD")
    cur = db_common.open_cursor(ctx, dialect)
    try:
        with pytest.raises(ConnError) as caught:
            cur._run("UPDATE shop.order_notes SET note = 'x' WHERE id = 1", None)
        assert caught.value.code == "readonly_violation", caught.value.message
    finally:
        cur.close()


def test_snapshot(db, monkeypatch):
    events, error, raw, executed, elapsed = _run(db, "snapshot", monkeypatch)
    assert error is None, error and error.message
    records = _records(events)
    counts = Counter(record["type"] for record in records)
    assert {key: counts[key] for key in EXPECTED} == EXPECTED
    assert counts["operation"] == db["procs"] and counts["rule"] == db["rules"]
    by_id = {record["id"]: record for record in records}
    prefix = "db:shop-db/shop."
    for anchor in ("orders.fk:fk_orders_customer", "order_items.fk:fk_items_product", "order_items.check:chk_items_quantity",
                   "products.uk:uk_products_sku", "customers.pk", "view:v_customer_orders", "proc:sp_cancel_order",
                   "order_notes.ref:order_id", "order_notes.ref:customer_id", "payments#sample", "payments#profile"):
        assert prefix + anchor in by_id, anchor
    assert by_id[prefix + "order_notes.ref:order_id"]["attrs"]["overlap_permille"] == 1000
    assert by_id[prefix + "order_notes.ref:customer_id"]["to"] == prefix + "customers"
    check = by_id[prefix + "order_items.check:chk_items_quantity"]["attrs"]
    assert check["columns"] == ["quantity"] and check["expression"].replace("`", "").strip("()") == "quantity > 0"
    assert by_id[prefix + "customers"]["label"] == "客户"
    assert by_id[prefix + "customers.phone"]["attrs"]["sensitivity"] == "phone"
    if db["rules"]:
        trigger = by_id[prefix + "order_items.trigger:trg_order_items_amount"]
        assert trigger["attrs"]["timing"] == "BEFORE" and trigger["attrs"]["event"] == "INSERT"
    else:
        assert any("触发器" in event["text"] for event in events if event["event"] == "limitation")

    # 样本：口令列不出现，原始个人信息一个都不漏
    sample = by_id[prefix + "customers#sample"]["attrs"]
    assert "password_hash" in sample["dropped_columns"] and len(sample["rows"]) == 20
    assert {"phone", "id_card", "customer_name", "email", "address", "id"} <= set(sample["masked_columns"])
    leaked = [value for value in _originals(db) if value in raw]
    assert not leaked, leaked[:3]
    profile = {item["column"]: item for item in by_id[prefix + "customers#profile"]["attrs"]["columns"]}
    for column in ("phone", "id_card", "customer_name", "email", "address"):
        assert "top_k" not in profile[column] and "stats" not in profile[column]
    assert profile["level"]["top_k"] and by_id[prefix + "customers#profile"]["attrs"]["rows"] == 60

    access = [event for event in events if event["event"] == "access"]
    assert len(access) == len(executed)
    assert events[-1]["event"] == "result" and events[-1]["complete"] is True
    print(f"\n[{db['name']}] 记录 {len(records)} 条 {dict(sorted(counts.items()))}；语句 {len(executed)} 条；"
          f"耗时 {elapsed} ms")


def test_scope_filter(db, monkeypatch):
    events, error, _, _, _ = _run(db, "snapshot", monkeypatch, scope=("shop.order*",))
    assert error is None
    tables = {record["name"] for record in _records(events) if record["type"] == "entity"}
    assert tables == {"orders", "order_items", "order_notes"}


def test_discover_and_sample(db, monkeypatch):
    events, error, _, _, _ = _run(db, "discover", monkeypatch, scope=())
    assert error is None and not _records(events)
    inventory = next(event for event in events if event["event"] == "inventory")
    objects = {item["object"]: item for item in inventory["items"]}
    assert objects["shop.customers"]["kind"] == "table" and "shop.view:v_customer_orders" in objects
    events, error, raw, executed, _ = _run(db, "sample", monkeypatch, selector="db:shop-db/shop.payments", limit=5)
    assert error is None
    [record] = _records(events)
    assert record["id"] == "db:shop-db/shop.payments#sample" and len(record["attrs"]["rows"]) == 5
    assert "card_no" in record["attrs"]["masked_columns"]
    assert not [value for value in _originals(db) if value in raw]


def test_budget_truncates(db, monkeypatch):
    events, error, _, _, _ = _run(db, "snapshot", monkeypatch, budget={"max_rows": 30})
    assert error is None
    assert len(_records(events)) == 30
    assert events[-1]["complete"] is False and events[-1]["coverage_pct"] < 100
    inventory = next(event for event in events if event["event"] == "inventory")
    assert any(item["reason"] == "budget" for item in inventory["skipped"])
