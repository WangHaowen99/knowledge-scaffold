"""F16 数据库提供方（ADR-0086；证据采集接口 §9）：语句白名单、脱敏、推断关系、记录形状、错误信息，不连库。

用一个按语句应答的假 DB-API 连接把 db-mysql 的四个操作完整跑一遍；PostgreSQL 方言只核对语句都过白名单和错误映射，
真库在 test_f16_db_integration.py。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import secrets
from decimal import Decimal

import pytest

from app.connector_gateway.conn import db_common, db_mysql, db_postgres, masking, sqlguard
from app.connector_gateway.conn.context import ConnError, Context

PASSWORD = "pw-" + secrets.token_hex(8)  # 每次随机，测试只核对它不出现在输出里
HOST = "db.example.invalid"
SALT = "a1" * 16


# ---------------- 语句白名单 ----------------

ALLOWED = [
    ("SELECT 1", "mysql"),
    ("select id from t", "postgres"),
    ("sElEcT 1;", "mysql"),
    ("SELECT 1 ;  \n", "postgres"),
    ("SHOW GRANTS", "mysql"),
    ("DESC t", "mysql"),
    ("DESCRIBE t", "mysql"),
    ("EXPLAIN SELECT * FROM t", "postgres"),
    ("WITH x AS (SELECT 1 AS a) SELECT a FROM x", "postgres"),
    ("SELECT 'DELETE FROM t; DROP TABLE t' AS s", "mysql"),
    ("SELECT 'it''s; DROP TABLE t'", "postgres"),
    ("SELECT 1 /* DROP TABLE t; */", "mysql"),
    ("SELECT 1 -- DROP TABLE t\n", "mysql"),
    ("SELECT 1 # DROP TABLE t", "mysql"),
    ("SELECT 1 -- ; DROP TABLE t", "postgres"),
    ("SELECT 1 /* outer /* inner */ DROP TABLE t */", "postgres"),
    ("SELECT `delete`, `drop``x` FROM `update`", "mysql"),
    ('SELECT "insert", "a""b" FROM "drop"', "postgres"),
    ("SELECT UPDATE_RULE, DELETE_RULE, TABLE_COMMENT FROM information_schema.REFERENTIAL_CONSTRAINTS", "mysql"),
    ("SELECT has_table_privilege(c.oid, 'INSERT, UPDATE') FROM pg_class c", "postgres"),
    ("SELECT 1 --1", "mysql"),
    ("SELECT 1 # 2", "postgres"),
    ("SELECT a FROM t ORDER BY a DESC LIMIT %s", "mysql"),
]

BLOCKED = [
    ("INSERT INTO t VALUES (1)", "mysql"),
    ("update t set a = 1", "postgres"),
    ("DeLeTe FROM t", "mysql"),
    ("DROP TABLE t", "postgres"),
    ("TRUNCATE t", "postgres"),
    ("CALL sp_x()", "mysql"),
    ("SET x = 1", "mysql"),
    ("SELECT 1; DROP TABLE t", "mysql"),
    ("select 1;delete from t", "postgres"),
    ("SELECT 1; SELECT 2", "mysql"),
    ("SELECT * FROM t FOR UPDATE", "postgres"),
    ("SELECT * FROM t FOR SHARE", "postgres"),
    ("SELECT * FROM t FOR NO KEY UPDATE", "postgres"),
    ("SELECT * FROM t LOCK IN SHARE MODE", "mysql"),
    ("SELECT * FROM t INTO OUTFILE '/tmp/x'", "mysql"),
    ("SELECT * INTO t2 FROM t", "postgres"),
    ("SELECT sleep(5)", "mysql"),
    ("SELECT SLEEP (5)", "mysql"),
    ("SELECT pg_sleep(1)", "postgres"),
    ("SELECT BENCHMARK(1000000, MD5('a'))", "mysql"),
    ("SELECT nextval('s')", "postgres"),
    ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ("SELECT query_to_xml('select 1', true, true, '')", "postgres"),
    ("SELECT 1 /*!50000 , (DROP TABLE t) */", "mysql"),
    ("SELECT /*+ MAX_EXECUTION_TIME(1) */ 1", "mysql"),
    ("SELECT 1 /* outer /* inner */ DROP TABLE t */", "mysql"),
    ("SELECT 1 # x\n; DROP TABLE t", "mysql"),
    ("SELECT 1 # 2; DROP TABLE t", "postgres"),
    ("SELECT 1 --DROP TABLE t", "mysql"),
    ("SELECT 'abc", "mysql"),
    ("SELECT 1 /* never closed", "postgres"),
    ("SELECT 'a\\' ; DROP TABLE t; -- '", "mysql"),
    ("SELECT E'\\' ; DROP TABLE t; --'", "postgres"),
    ("SELECT $$ DROP TABLE t $$", "postgres"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "postgres"),
    ("EXPLAIN ANALYZE SELECT 1", "postgres"),
    ("LOCK TABLES t READ", "mysql"),
    ("COPY t TO '/tmp/x'", "postgres"),
    ("VALUES (1)", "postgres"),
    ("TABLE t", "mysql"),
    ("HANDLER t OPEN", "mysql"),
    ("LOAD DATA INFILE 'x' INTO TABLE t", "mysql"),
    ("", "mysql"),
    ("   ", "postgres"),
]


@pytest.mark.parametrize("sql,dialect", ALLOWED)
def test_sqlguard_allows_reads(sql, dialect):
    assert sqlguard.check(sql, dialect) in ("select", "show", "desc", "describe", "explain", "with")


@pytest.mark.parametrize("sql,dialect", BLOCKED)
def test_sqlguard_blocks_writes_and_ambiguity(sql, dialect):
    with pytest.raises(ConnError) as caught:
        sqlguard.check(sql, dialect)
    assert caught.value.code == "statement_blocked"


def test_sqlguard_session_statements_are_fixed():
    for sql in ("SET SESSION TRANSACTION READ ONLY", "SET SESSION max_execution_time = 30000", "START TRANSACTION READ ONLY"):
        sqlguard.check_session(sql, "mysql")
    for sql in ("SET default_transaction_read_only = on", "SET statement_timeout = 30000", "BEGIN READ ONLY"):
        sqlguard.check_session(sql, "postgres")
    for sql, dialect in [("SET SESSION sql_mode = ''", "mysql"), ("SET GLOBAL max_execution_time = 1", "mysql"),
                         ("START TRANSACTION", "mysql"), ("SET default_transaction_read_only = off", "postgres"),
                         ("BEGIN READ ONLY; DROP TABLE t", "postgres"), ("SELECT 1", "postgres"),
                         ("SET statement_timeout = 0; DROP TABLE t", "postgres")]:
        with pytest.raises(ConnError):
            sqlguard.check_session(sql, dialect)


# ---------------- 脱敏 ----------------

PHONES = ["15500000001", "15500000002", "15500000003"]
IDS = ["990000199001010010", "990000199102020020", "99000019920303003X"]


@pytest.mark.parametrize("column,kwargs,expected", [
    ("password_hash", {}, "secret"),
    ("api_key", {}, "secret"),
    ("登录密码", {}, "secret"),
    ("col_x", {"comment": "登录口令"}, "secret"),
    ("phone", {}, "phone"),
    ("receiverMobile", {}, "phone"),
    ("contact", {"values": PHONES}, "phone"),
    ("id_card", {}, "id_number"),
    ("doc", {"values": IDS}, "id_number"),
    ("customer_name", {}, "person_name"),
    ("receiver_name", {}, "person_name"),
    ("name", {"table": "customers"}, "person_name"),
    ("联系人", {}, "person_name"),
    ("birthday", {"type_class": "temporal"}, "birth_date"),
    ("email", {}, "email"),
    ("contact_info", {"values": ["a@example.invalid", "b@example.invalid"]}, "email"),
    ("card_no", {}, "bank_card"),
    ("ref", {"values": ["6200000000000001", "6200000000000002"]}, "bank_card"),
    ("address", {}, "address"),
    ("location", {"values": ["测试省示例市演示区样例路1号", "测试省示例市演示区样例路2号"]}, "address"),
    ("total_amount", {"type_class": "numeric"}, "amount"),
    ("余额", {"type_class": "numeric"}, "amount"),
    ("customer_id", {"type_class": "numeric", "join_key": True}, "join_key"),
    ("note", {"values": ["请尽快发货", "工作日送达", "放门口"]}, "text"),
    ("note", {}, "text"),
    ("status", {"values": ["OK", "OK", "FAIL", "OK"]}, "none"),
    ("status", {"values": ["OK", "联系 15500000001", "OK", "OK"]}, "text"),
    ("product_name", {"values": ["演示商品1", "演示商品2"]}, "none"),
    ("order_no", {"values": ["SO1", "SO2"]}, "none"),
    ("quantity", {"type_class": "numeric", "values": [1, 2, 3]}, "none"),
    ("created_at", {"type_class": "temporal"}, "none"),
    ("photo", {"type_class": "binary"}, "binary"),
])
def test_masking_classify(column, kwargs, expected):
    assert masking.classify(column, **kwargs) == expected


def test_masking_treatments():
    masker = masking.Masker(SALT)
    assert masker.apply("phone", "15500000001") == "155****0001"
    assert masker.apply("id_number", "990000199001010010") == "99" + "*" * 14 + "10"
    assert masker.apply("person_name", "赵测1") == "赵**"
    assert masker.apply("email", "user1@example.invalid") == "u***@***.invalid"
    assert masker.apply("bank_card", "6200000000000001") == "*" * 12 + "0001"
    assert masker.apply("address", "测试省示例市演示区样例路1号") == "测试省***"
    assert masker.apply("birth_date", dt.date(1990, 1, 2)) == "1990-**-**"
    assert masker.apply("text", "随便写的备注") == "***"
    assert masker.apply("binary", "<binary>") == "<binary>"
    assert masker.apply("phone", None) is None
    assert masker.apply("none", Decimal("12.50")) == "12.50"
    assert masker.apply("none", 4.5) == "4.5"
    assert masker.apply("none", dt.datetime(2026, 1, 1, 8, 0)) == "2026-01-01T08:00:00"
    assert [masking.bucket(v) for v in (Decimal("1234.56"), 0, -50, Decimal("0.5"), 7, "x")] == [
        "[1000,10000)", "0", "-[10,100)", "(0,1)", "[1,10)", "***"]
    assert re.fullmatch(r"[0-9a-f]{12}", masker.apply("join_key", 42))


def test_masking_salt_changes_hash_and_keeps_joins():
    first, second = masking.Masker(SALT), masking.Masker("b2" * 16)
    assert first.digest(7) != second.digest(7)
    assert first.digest(7) == first.digest("7") == masking.Masker(SALT).digest(7)
    assert masking.Masker("").ephemeral_salt and not first.ephemeral_salt


# ---------------- 推断关系 ----------------


def _table(name):
    return db_common.Table("shop", name)


def _column(table, name):
    return db_common.Column("shop", table, name, 1, "bigint", False, type_class="numeric")


def test_infer_refs_by_naming():
    tables = [_table(name) for name in ("customers", "orders", "order_notes", "categories", "goods")]
    columns = {
        ("shop", "customers"): [_column("customers", "id")],
        ("shop", "orders"): [_column("orders", "id"), _column("orders", "customer_id")],
        ("shop", "order_notes"): [_column("order_notes", n) for n in ("id", "order_id", "customer_id", "foo_id")],
        ("shop", "categories"): [_column("categories", "cat_key")],
        ("shop", "goods"): [_column("goods", "id"), _column("goods", "category_id")],
    }
    constraints = {
        ("shop", "orders"): [db_common.Constraint("shop", "orders", "fk_orders_customer", "fk", ["customer_id"],
                                                  ref_schema="shop", ref_table="customers", ref_columns=["id"])],
        ("shop", "categories"): [db_common.Constraint("shop", "categories", "pk_c", "pk", ["cat_key"])],
    }
    refs = {(r.table, r.column): (r.target_table, r.target_column)
            for r in db_common.infer_refs(tables, columns, constraints)}
    assert refs == {("order_notes", "order_id"): ("orders", "id"),
                    ("order_notes", "customer_id"): ("customers", "id"),
                    ("goods", "category_id"): ("categories", "cat_key")}


# ---------------- 假 MySQL：完整跑一遍 ----------------

RO_GRANTS = ["GRANT USAGE ON *.* TO `onto_ro`@`%`", "GRANT SELECT, SHOW VIEW ON `shop`.* TO `onto_ro`@`%`",
             "GRANT SHOW_ROUTINE ON *.* TO `onto_ro`@`%`"]
RW_GRANTS = ["GRANT USAGE ON *.* TO `onto_rw`@`%`", "GRANT SELECT, INSERT, UPDATE, DELETE ON `shop`.* TO `onto_rw`@`%`"]

# 表 → [(列, DATA_TYPE, COLUMN_TYPE, 可空, 默认, EXTRA, 注释)], 行
SCHEMA = {
    "customers": ("客户", [
        ("id", "bigint", "bigint", "NO", None, "auto_increment", "客户编号"),
        ("customer_name", "varchar", "varchar(32)", "NO", None, "", "客户姓名"),
        ("phone", "varchar", "varchar(16)", "NO", None, "", "手机号"),
        ("id_card", "varchar", "varchar(18)", "NO", None, "", "身份证号"),
        ("password_hash", "char", "char(64)", "NO", None, "", "登录密码哈希"),
        ("level", "varchar", "varchar(8)", "NO", "NORMAL", "", "客户等级"),
        ("balance", "decimal", "decimal(12,2)", "YES", None, "", "余额"),
    ], [
        (1, "赵测1", PHONES[0], IDS[0], "f" * 64, "GOLD", Decimal("1200.50")),
        (2, "钱测2", PHONES[1], IDS[1], "e" * 64, "GOLD", Decimal("35.00")),
        (3, "孙测3", PHONES[2], IDS[2], "d" * 64, "NORMAL", None),
        (4, "李测4", "15500000004", "990000199304040040", "c" * 64, "GOLD", Decimal("0.10")),
    ]),
    "orders": ("订单", [
        ("id", "bigint", "bigint", "NO", None, "auto_increment", ""),
        ("customer_id", "bigint", "bigint", "NO", None, "", "下单客户"),
        ("status", "varchar", "varchar(12)", "NO", "CREATED", "", "订单状态"),
        ("item_count", "int", "int", "NO", "0", "", "件数"),
        ("rating", "double", "double", "YES", None, "", "评分"),
        ("created_at", "datetime", "datetime", "NO", "CURRENT_TIMESTAMP", "DEFAULT_GENERATED", "下单时间"),
    ], [
        (10, 1, "PAID", 2, 4.5, dt.datetime(2026, 3, 1, 9)),
        (11, 2, "PAID", 1, None, dt.datetime(2026, 3, 1, 10)),
        (12, 1, "DONE", 5, 3.0, dt.datetime(2026, 3, 2, 9)),
        (13, 3, "PAID", 3, 5.0, dt.datetime(2026, 3, 3, 9)),
    ]),
    "order_notes": ("订单备注", [
        ("id", "bigint", "bigint", "NO", None, "auto_increment", ""),
        ("order_id", "bigint", "bigint", "NO", None, "", ""),
        ("note", "varchar", "varchar(200)", "YES", None, "", "备注"),
    ], [
        (1, 10, "请尽快发货"),
        (2, 12, f"联系电话 {PHONES[0]}"),
        (3, 99, "工作日送达"),
    ]),
}
KEYS = [
    ("customers", "PRIMARY", "PRIMARY KEY", "id", None, None, None, None, None),
    ("customers", "uk_customers_phone", "UNIQUE", "phone", None, None, None, None, None),
    ("orders", "PRIMARY", "PRIMARY KEY", "id", None, None, None, None, None),
    ("orders", "fk_orders_customer", "FOREIGN KEY", "customer_id", "shop", "customers", "id", "NO ACTION", "CASCADE"),
    ("order_notes", "PRIMARY", "PRIMARY KEY", "id", None, None, None, None, None),
]
CHECKS = [("orders", "chk_orders_status", "(`status` in (_utf8mb4'PAID',_utf8mb4'DONE'))")]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.log.append(sql)
        rows = self.conn.answer(sql, params)
        self.description = None if rows is None else [("c",)]
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeMySQL:
    def __init__(self, grants=None, fail_on=None):
        self.grants = RO_GRANTS if grants is None else grants
        self.fail_on = fail_on
        self.log: list[str] = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def answer(self, sql, params):
        if self.fail_on and self.fail_on in sql:
            raise PyMySQLError(1146, "Table 'shop.x' doesn't exist")
        if sql.startswith(("SET ", "START ", "ROLLBACK")):
            return None
        if sql == "SHOW GRANTS":
            return [(line,) for line in self.grants]
        if sql == "SELECT VERSION()":
            return [("8.0.0-fake",)]
        if sql == "SELECT @@transaction_read_only":
            return [(1,)]
        if "information_schema.TABLES" in sql:
            rows = [("shop", name, "BASE TABLE", len(spec[2]), 16384, spec[0]) for name, spec in SCHEMA.items()]
            return rows + [("shop", "v_customer_orders", "VIEW", None, None, "VIEW")]
        if "information_schema.COLUMNS" in sql:
            rows = []
            for name, (_, columns, _) in SCHEMA.items():
                for index, (column, base, full, nullable, default, extra, comment) in enumerate(columns, 1):
                    rows.append(("shop", name, column, index, default, nullable, base, full, extra, comment))
            return rows + [("shop", "v_customer_orders", "customer_id", 1, None, "NO", "bigint", "bigint", "", "")]
        if "KEY_COLUMN_USAGE" in sql:
            return [("shop", *row) for row in KEYS]
        if "CHECK_CONSTRAINTS" in sql:
            return [("shop", *row) for row in CHECKS]
        if "information_schema.VIEWS" in sql:
            return [("shop", "v_customer_orders", "select `c`.`id` AS `customer_id` from `shop`.`customers` `c`")]
        if "information_schema.TRIGGERS" in sql:
            return []
        if "information_schema.ROUTINES" in sql:
            return [("shop", "sp_cancel_order", "PROCEDURE", "UPDATE orders SET status = 'CANCELLED'", "取消订单", "SQL")]
        if "information_schema.PARAMETERS" in sql:
            return [("shop", "sp_cancel_order", "PROCEDURE", "IN", "p_order_id", "bigint")]
        match = re.search(r"FROM `shop`\.`(\w+)`", sql)
        if match:
            return [tuple(row) for row in SCHEMA[match.group(1)][2][: params[0]]]
        raise AssertionError(f"假库不认识：{sql}")


class PyMySQLError(Exception):
    """模仿 pymysql.err 的异常：args = (错误号, 消息)。"""


PyMySQLError.__module__ = "pymysql.err"


def _head(operation, *, scope=(), budget=None, selector=None, limit=None, salt=SALT, provider="db-mysql"):
    return {
        "provider": provider, "operation": operation,
        "source": {"id": "shop-db", "provider": provider,
                   "connection": {"host": HOST, "port": 3306, "database": "shop", "username": "onto_ro"}},
        "scope": list(scope), "budget": budget or {}, "rate_limit": {"qps": 0, "query_timeout_s": 30},
        "masking": {"policy": "mask-v1", "salt": salt}, "selector": selector, "limit": limit,
    }


def _run(monkeypatch, operation, fake=None, **head):
    fake = fake or FakeMySQL()
    monkeypatch.setattr(db_mysql.DIALECT, "connect", lambda ctx: fake)
    out = io.StringIO()
    ctx = Context(_head(operation, **head), {"password": PASSWORD}, out, scheme="db")
    error = None
    try:
        getattr(db_mysql, operation)(ctx)
    except ConnError as exc:
        error = exc
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return events, fake, error, out.getvalue()


def _records(events):
    return {event["record"]["id"]: event["record"] for event in events if event["event"] == "record"}


def _walk(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


def test_snapshot_records_follow_section_9(monkeypatch):
    events, fake, error, raw = _run(monkeypatch, "snapshot")
    assert error is None
    records = _records(events)
    prefix = "db:shop-db/shop."
    for anchor in ("customers", "customers.phone", "customers.pk", "customers.uk:uk_customers_phone",
                   "orders.fk:fk_orders_customer", "orders.check:chk_orders_status", "order_notes.ref:order_id",
                   "view:v_customer_orders", "proc:sp_cancel_order", "customers#profile", "customers#sample"):
        assert prefix + anchor in records, anchor
    types = {record["type"] for record in records.values()}
    assert types == {"entity", "field", "constraint", "relation", "operation", "profile", "sample"}
    assert all(record["hash"] and record["evidence"]["provider"] == "db-mysql@0.1.0" for record in records.values())
    assert not any(isinstance(value, float) for record in records.values() for value in _walk(record))

    table = records[prefix + "customers"]
    assert table["attrs"]["kind"] == "table" and table["label"] == "客户"
    assert table["attrs"]["ddl"].startswith("CREATE TABLE `shop`.`customers` (")
    assert "PRIMARY KEY (`id`)" in table["attrs"]["ddl"] and "COMMENT='客户'" in table["attrs"]["ddl"]
    phone = records[prefix + "customers.phone"]
    assert phone["parent"] == prefix + "customers" and phone["data_type"] == "varchar(16)"
    assert phone["nullable"] is False and phone["label"] == "手机号"
    assert phone["attrs"]["masked"] is True and phone["attrs"]["sensitivity"] == "phone"
    assert records[prefix + "orders.item_count"]["attrs"] == {
        "comment": "件数", "default": "0", "pk": False, "position": 4, "masked": False, "sensitivity": "none"}
    assert records[prefix + "customers.id"]["attrs"]["pk"] is True
    assert records[prefix + "customers.id"]["attrs"]["sensitivity"] == "join_key"

    fk = records[prefix + "orders.fk:fk_orders_customer"]
    assert fk["from"] == prefix + "orders" and fk["to"] == prefix + "customers"
    assert fk["attrs"]["columns"] == [["customer_id", "id"]] and fk["evidence"]["confidence"] == "declared"
    assert fk["attrs"]["on_delete"] == "CASCADE"
    ref = records[prefix + "order_notes.ref:order_id"]
    assert ref["to"] == prefix + "orders" and ref["evidence"]["confidence"] == "inferred"
    assert ref["attrs"]["basis"] == "naming+values" and ref["attrs"]["overlap_permille"] == 666
    check = records[prefix + "orders.check:chk_orders_status"]
    assert check["type"] == "constraint" and check["attrs"]["columns"] == ["status"]
    proc = records[prefix + "proc:sp_cancel_order"]
    assert proc["type"] == "operation" and proc["attrs"]["signature"] == "sp_cancel_order(IN p_order_id bigint)"
    view = records[prefix + "view:v_customer_orders"]
    assert view["attrs"]["definition"].startswith("select") and view["attrs"]["columns"][0]["name"] == "customer_id"

    # 样本：口令列丢弃、手机号掩码、关联键加盐哈希且两表对得上，原始个人信息一个都不出现
    sample = records[prefix + "customers#sample"]["attrs"]
    assert "password_hash" not in sample["columns"] and sample["dropped_columns"] == ["password_hash"]
    row = dict(zip(sample["columns"], sample["rows"][0]))
    assert row["phone"] == "155****0001" and row["customer_name"] == "赵**" and row["balance"] == "[1000,10000)"
    assert row["level"] == "GOLD"
    orders = records[prefix + "orders#sample"]["attrs"]
    assert dict(zip(orders["columns"], orders["rows"][0]))["customer_id"] == row["id"] != 1
    for secret in PHONES + IDS + ["f" * 64]:
        assert secret not in raw

    # 画像：敏感列只有空值率和去重数；低基数不敏感列给 top-k；数值不敏感列给最小、最大值
    profile = {item["column"]: item for item in records[prefix + "customers#profile"]["attrs"]["columns"]}
    assert set(profile["phone"]) == {"column", "null_pct", "distinct"}
    assert profile["level"]["top_k"][0] == {"value": "GOLD", "count": 3}
    assert profile["balance"]["null_pct"] == 250 and "stats" not in profile["balance"]
    order_profile = {item["column"]: item for item in records[prefix + "orders#profile"]["attrs"]["columns"]}
    assert order_profile["item_count"]["stats"] == {"min": 1, "max": 5}
    assert order_profile["rating"]["stats"] == {"min": "3.0", "max": "5.0"}

    # 每条语句都有一条访问审计；会话设置在前；都过了白名单
    access = [event for event in events if event["event"] == "access"]
    assert len(access) == len(fake.log)
    assert fake.log[:5] == db_mysql.DIALECT.session_statements(Context(_head("snapshot"), {}, io.StringIO(),
                                                                         scheme="db"))
    assert fake.closed
    assert any("触发器" in event["text"] for event in events if event["event"] == "limitation")
    result = events[-1]
    assert result == {"event": "result", "complete": True, "coverage_pct": 100.0}
    inventory = next(event for event in events if event["event"] == "inventory")
    assert {item["object"] for item in inventory["items"]} >= {"shop.customers", "shop.view:v_customer_orders",
                                                              "shop.proc:sp_cancel_order"}


def test_snapshot_rejects_write_account_before_reading_data(monkeypatch):
    events, fake, error, _ = _run(monkeypatch, "snapshot", fake=FakeMySQL(grants=RW_GRANTS))
    assert error is not None and error.code == "account_not_readonly"
    assert "INSERT" in error.message and "UPDATE" in error.message
    assert not any("FROM `shop`." in sql for sql in fake.log)
    assert not any(event["event"] == "record" for event in events) and fake.closed


def test_scope_filters_tables(monkeypatch):
    events, _, error, _ = _run(monkeypatch, "snapshot", scope=["shop.order*"])
    assert error is None
    names = {record["name"] for record in _records(events).values() if record["type"] == "entity"}
    assert names == {"orders", "order_notes"}


def test_budget_truncates_snapshot(monkeypatch):
    events, _, error, _ = _run(monkeypatch, "snapshot", budget={"max_rows": 12})
    assert error is None
    assert sum(event["event"] == "record" for event in events) == 12
    result = events[-1]
    assert result["event"] == "result" and result["complete"] is False and result["coverage_pct"] < 100
    inventory = next(event for event in events if event["event"] == "inventory")
    assert {"object": "shop.orders", "reason": "budget"} in inventory["skipped"]


def test_profile_failure_skips_table_and_continues(monkeypatch):
    events, fake, error, _ = _run(monkeypatch, "snapshot", fake=FakeMySQL(fail_on="FROM `shop`.`orders`"))
    assert error is None
    records = _records(events)
    assert "db:shop-db/shop.orders" in records and "db:shop-db/shop.orders#sample" not in records
    assert "db:shop-db/shop.customers#sample" in records
    assert "ROLLBACK" in fake.log and events[-1]["complete"] is True


def test_discover_only_inventory(monkeypatch):
    events, _, error, _ = _run(monkeypatch, "discover")
    assert error is None and not any(event["event"] == "record" for event in events)
    inventory = next(event for event in events if event["event"] == "inventory")
    assert {"object": "shop.customers", "kind": "table", "estimate_rows": 4, "estimate_bytes": 16384} in inventory["items"]


def test_sample_reads_one_table(monkeypatch):
    events, fake, error, raw = _run(monkeypatch, "sample", selector="db:shop-db/shop.customers", limit=2)
    assert error is None
    records = list(_records(events).values())
    assert [record["id"] for record in records] == ["db:shop-db/shop.customers#sample"]
    assert len(records[0]["attrs"]["rows"]) == 2
    assert [sql for sql in fake.log if "FROM `shop`." in sql] == [next(s for s in fake.log if "`customers`" in s)]
    assert PHONES[0] not in raw
    # 只展示 2 行，但遮不遮按和快照相同的行数判断，两边的 masked_columns 一致
    snapshot = _records(_run(monkeypatch, "snapshot")[0])["db:shop-db/shop.customers#sample"]
    assert records[0]["attrs"]["masked_columns"] == snapshot["attrs"]["masked_columns"]
    _, _, error, _ = _run(monkeypatch, "sample", selector={"object": "ops.audit_log"})
    assert error.code == "out_of_scope"
    _, _, error, _ = _run(monkeypatch, "sample", selector="shop.view:v")
    assert error.code == "invalid_selector"


def test_test_operation_checks(monkeypatch):
    events, _, error, _ = _run(monkeypatch, "test")
    assert error is None
    checks = {check["name"]: check for check in events[-1]["checks"]}
    assert list(checks) == ["connectivity", "readonly_account", "statement_allowlist"]
    assert checks["connectivity"]["ok"] and isinstance(checks["connectivity"]["latency_ms"], int)
    assert checks["readonly_account"]["ok"] and checks["statement_allowlist"]["ok"]
    events, _, _, _ = _run(monkeypatch, "test", fake=FakeMySQL(grants=RW_GRANTS))
    checks = {check["name"]: check for check in events[-1]["checks"]}
    assert not checks["readonly_account"]["ok"] and "INSERT" in checks["readonly_account"]["detail"]


def test_errors_never_carry_password_or_host(monkeypatch):
    ctx = Context(_head("test"), {"password": PASSWORD}, io.StringIO(), scheme="db")
    cases = [
        (db_mysql.DIALECT, PyMySQLError(1045, "Access denied for user 'onto_ro'@'192.0.2.10' (using password: YES)")),
        (db_mysql.DIALECT, PyMySQLError(2003, f"Can't connect to MySQL server on '{HOST}' ([Errno 111])")),
        (db_mysql.DIALECT, RuntimeError(f"mysql://onto_ro:{PASSWORD}@{HOST}:3306/shop failed")),
        (db_postgres.DIALECT, _pg_error({"C": "28P01", "M": 'password authentication failed for user "onto_ro"'})),
        (db_postgres.DIALECT, _pg_error(f"Can't create a connection to host {HOST} and port 5432")),
        (db_postgres.DIALECT, ValueError(f"bad password {PASSWORD} for 198.51.100.7")),
    ]
    for dialect, exc in cases:
        error = dialect.error(exc, ctx)
        assert PASSWORD not in error.message and HOST not in error.message
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", error.message), error.message
    assert db_mysql.DIALECT.error(cases[0][1], ctx).code == "auth_failed"
    assert db_mysql.DIALECT.error(cases[1][1], ctx).code == "connect_failed"
    assert db_postgres.DIALECT.error(cases[3][1], ctx).code == "auth_failed"
    assert db_postgres.DIALECT.error(cases[4][1], ctx).code == "connect_failed"

    def refuse(_ctx):
        raise PyMySQLError(2003, f"Can't connect to MySQL server on '{HOST}' (password {PASSWORD})")

    monkeypatch.setattr(db_mysql.DIALECT, "connect", refuse)
    out = io.StringIO()
    db_mysql.test(Context(_head("test"), {"password": PASSWORD}, out, scheme="db"))
    assert PASSWORD not in out.getvalue() and HOST not in out.getvalue()
    checks = json.loads(out.getvalue().splitlines()[-1])["checks"]
    assert [check["ok"] for check in checks] == [False, False, True]


def _pg_error(arg):
    error = type("DatabaseError", (Exception,), {"__module__": "pg8000.exceptions"})
    return error(arg)


def test_grants_parsing():
    assert db_mysql.grants_readonly(RO_GRANTS)[0]
    assert not db_mysql.grants_readonly(RW_GRANTS)[0]
    assert db_mysql.grants_readonly(["GRANT SELECT (a, b) ON `shop`.`t` TO `u`@`%`"])[0]
    for lines in (["GRANT SELECT ON *.* TO `u`@`%` WITH GRANT OPTION"],
                  ["GRANT SELECT, TRIGGER ON `shop`.* TO `u`@`%`"],
                  ["GRANT SELECT, EXECUTE ON `shop`.* TO `u`@`%`"],
                  ["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"],
                  ["GRANT SELECT (a), UPDATE (b) ON `shop`.`t` TO `u`@`%`"],
                  ["GRANT SELECT ON `shop`.* TO `u`@`%`", "GRANT `app_role`@`%` TO `u`@`%`"],
                  ["GRANT USAGE ON *.* TO `u`@`%`"]):
        assert not db_mysql.grants_readonly(lines)[0], lines


# ---------------- PostgreSQL 方言：语句都过白名单 ----------------


class FakePg:
    def __init__(self, superuser=False):
        self.log: list[str] = []
        self.superuser = superuser

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def answer(self, sql, params):
        if sql.startswith(("SET ", "BEGIN", "ROLLBACK")):
            return None
        if "FROM pg_roles r" in sql:
            return [(self.superuser, False, False, [])]
        if "FROM pg_class c JOIN pg_namespace n" in sql and "relkind" in sql:
            return [("shop", "t", "r", 3, 8192, "表", True, False, 16384)]
        return []


def test_postgres_statements_pass_guard_and_readonly_check():
    for superuser, expected in ((False, True), (True, False)):
        fake = FakePg(superuser)
        ctx = Context(_head("snapshot", provider="db-postgres"), {"password": PASSWORD}, io.StringIO(), scheme="db")
        cur = db_common.GuardedCursor(ctx, fake, db_postgres.DIALECT)
        for sql in db_postgres.DIALECT.session_statements(ctx):
            cur.session(sql)
        tables = db_postgres.DIALECT.list_tables(cur)
        assert db_postgres.DIALECT.readonly_check(cur, tables)[0] is expected
        db_postgres.DIALECT.columns(cur, ["shop"])
        db_postgres.DIALECT.constraints(cur, ["shop"])
        db_postgres.DIALECT.views(cur, [db_common.Table("shop", "v", "view", key=1)])
        db_postgres.DIALECT.triggers(cur, ["shop"])
        db_postgres.DIALECT.routines(cur, ["shop"])
        cur.query(db_postgres.DIALECT.version_sql())
        cur.query(db_postgres.DIALECT.read_only_probe_sql())
        cur.recover()
        assert cur.statements == len(fake.log) >= 15


def test_wide_tables_read_fewer_profile_rows():
    ctx = Context(_head("snapshot"), {}, io.StringIO(), scheme="db")
    collection = db_common.Collection(ctx, db_common.GuardedCursor(ctx, FakeMySQL(), db_mysql.DIALECT), [])
    for width, expected in ((10, 10_000), (100, 2000), (5000, 200)):
        collection.columns[("shop", "t")] = [_column("t", f"c{i}") for i in range(width)]
        assert collection.row_limit(_table("t"), 10_000) == expected
