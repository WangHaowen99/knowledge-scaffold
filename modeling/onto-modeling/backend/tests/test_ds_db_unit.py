"""ADR-0104 数据源连接器 db-mysql、db-postgres 与平台通用件 dbkit：用假连接测，不连真库。

真库的测试在 test_ds_db_integration.py。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
from pathlib import Path

import pytest

from app.connector_gateway.source import dbkit, sqlguard
from app.connector_gateway.source.sdk import SourceContext, SourceError

PACKS = Path(__file__).resolve().parents[2] / "assets" / "packs"
PASSWORD = "synthetic-Pw-0042"
HOST = "db.example.invalid"


def _load(pack: str):
    spec = importlib.util.spec_from_file_location(f"connector_{pack.replace('-', '_')}", PACKS / pack / "connector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MYSQL = _load("db-mysql")
POSTGRES = _load("db-postgres")


def _ctx(operation="collect", *, tables="", database="shop", timeout_s=60, clock=None, user="onto_ro"):
    head = {"operation": operation, "limits": {"timeout_s": timeout_s, "qps": 0},
            "config": {"host": HOST, "port": 3306, "database": database, "user": user, "tables": tables}}
    out = io.StringIO()
    kwargs = {"clock": clock} if clock else {}
    return SourceContext(head, {"password": PASSWORD}, out, **kwargs), out


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


# ---------------- 假 MySQL ----------------

RO_GRANTS = ["GRANT USAGE ON *.* TO `onto_ro`@`%`", "GRANT SELECT, SHOW VIEW ON `shop`.* TO `onto_ro`@`%`"]
RW_GRANTS = ["GRANT USAGE ON *.* TO `onto_rw`@`%`", "GRANT SELECT, INSERT, UPDATE, DELETE ON `shop`.* TO `onto_rw`@`%`"]

# 表名 → (表注释, [(列, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, 注释)], 行)
SCHEMA = {
    "customers": ("客户", [("id", "bigint", "bigint", "NO", "客户编号"), ("customer_name", "varchar", "varchar(32)", "NO", "姓名"),
                          ("phone", "varchar", "varchar(16)", "NO", "手机号"), ("photo", "blob", "blob", "YES", "")],
                  [(i, f"测{i}", f"1550000{i:04d}", 0) for i in range(1, 6)]),
    "orders": ("订单", [("id", "bigint", "bigint", "NO", ""), ("customer_id", "bigint", "bigint", "NO", "下单客户"),
                       ("status", "varchar", "varchar(8)", "NO", "状态")],
               [(i, 1 + i % 5, "PAID" if i % 2 else "NEW") for i in range(1, 9)]),
    "order_notes": ("", [("id", "bigint", "bigint", "NO", ""), ("order_id", "bigint", "bigint", "NO", "")],
                    [(1, 3)]),
}
KEYS = [  # (表, 约束, 类型, 列, 引用库, 引用表, 引用列)
    ("customers", "PRIMARY", "PRIMARY KEY", "id", None, None, None),
    ("customers", "uk_phone", "UNIQUE", "phone", None, None, None),
    ("orders", "PRIMARY", "PRIMARY KEY", "id", None, None, None),
    ("orders", "fk_orders_customer", "FOREIGN KEY", "customer_id", "shop", "customers", "id"),
    ("order_notes", "PRIMARY", "PRIMARY KEY", "id", None, None, None),
]
CHECKS = [("orders", "chk_orders_status", "(`status` in (_utf8mb4\\'PAID\\',_utf8mb4\\'NEW\\'))")]


class PyMySQLError(Exception):
    """模仿 pymysql.err：args = (错误号, 消息)。"""


PyMySQLError.__module__ = "pymysql.err"


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
    def __init__(self, grants=None, fail_on=None, schemas=("shop",)):
        self.grants = RO_GRANTS if grants is None else grants
        self.fail_on = fail_on
        self.schemas = schemas
        self.log: list[str] = []

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass

    def answer(self, sql, params):
        if self.fail_on and self.fail_on in sql:
            raise PyMySQLError(1146, f"Table '{self.fail_on}' on {HOST} doesn't exist")
        if sql.startswith(("SET ", "START ", "ROLLBACK")):
            return None
        if sql == "SHOW GRANTS":
            return [(line,) for line in self.grants]
        if sql == "SELECT VERSION()":
            return [("8.0.0-fake",)]
        if sql == "SELECT @@transaction_read_only":
            return [(1,)]
        if "information_schema.TABLES" in sql:
            rows = [(schema, name, "BASE TABLE", SCHEMA[name][0]) for schema in sorted(self.schemas)
                    for name in sorted(SCHEMA)]
            return rows + [("shop", "v_orders", "VIEW", "VIEW")]
        if "information_schema.COLUMNS" in sql:
            return [(schema, name, column, index, nullable, base, full, comment)
                    for schema in self.schemas for name, (_, columns, _) in SCHEMA.items()
                    for index, (column, base, full, nullable, comment) in enumerate(columns, 1)]
        if "KEY_COLUMN_USAGE" in sql:
            return [(schema, *row) for schema in self.schemas for row in KEYS]
        if "CHECK_CONSTRAINTS" in sql:
            return [(schema, *row) for schema in self.schemas for row in CHECKS]
        if "information_schema.VIEWS" in sql:
            return [("shop", "v_orders", "select `o`.`id` AS `id` from `shop`.`orders` `o`")]
        if "information_schema.TRIGGERS" in sql:
            return []
        if "information_schema.ROUTINES" in sql:
            return [("shop", "sp_cancel_order", "PROCEDURE", "UPDATE orders SET status = 'NEW'")]
        if "information_schema.PARAMETERS" in sql:
            return [("shop", "sp_cancel_order", "PROCEDURE", "IN", "p_order_id", "bigint")]
        match = re.search(r"FROM `(\w+)`\.`(\w+)`", sql)
        if match:
            return [tuple(row) for row in SCHEMA[match.group(2)][2][: params[0]]]
        raise AssertionError(f"假库不认识：{sql}")


def _run_mysql(monkeypatch, operation="collect", fake=None, **ctx_kwargs):
    fake = fake or FakeMySQL()
    monkeypatch.setattr(MYSQL.DIALECT, "connect", lambda ctx: fake)
    ctx, out = _ctx(operation, **ctx_kwargs)
    error = None
    try:
        getattr(MYSQL, operation)(ctx)
    except SourceError as exc:
        error = exc
    return _events(out), error, fake


def _facts(events, kind):
    return [event["fact"] for event in events if event["event"] == "fact" and event["fact"]["kind"] == kind]


def _assert_guarded(log, dialect):
    for sql in log:
        try:
            sqlguard.check_session(sql, dialect)
        except SourceError:
            sqlguard.check(sql, dialect)


def test_mysql_collect_tables_facts_and_guard(monkeypatch):
    events, error, fake = _run_mysql(monkeypatch)
    assert error is None
    tables = [event for event in events if event["event"] == "table"]
    assert [table["name"] for table in tables] == ["customers", "order_notes", "orders"]
    customers = tables[0]
    assert customers["comment"] == "客户"
    assert [(c["name"], c["key"], c["type"], c["nullable"]) for c in customers["columns"]] == [
        ("id", "pk", "bigint", False), ("customer_name", "", "varchar(32)", False),
        ("phone", "uk", "varchar(16)", False), ("photo", "", "blob", True)]
    assert customers["rows"][0][:3] == [1, "测1", "15500000001"]
    assert tables[2]["columns"][1]["key"] == "fk"
    assert _facts(events, "foreign_key") == [{"kind": "foreign_key", "table": "orders", "columns": ["customer_id"],
                                              "ref_table": "customers", "ref_columns": ["id"],
                                              "name": "fk_orders_customer"}]
    assert _facts(events, "check")[0]["expression"] == "(`status` in (_utf8mb4'PAID',_utf8mb4'NEW'))"
    assert _facts(events, "reference") == [{"kind": "reference", "table": "order_notes", "columns": ["order_id"],
                                            "ref_table": "orders", "ref_columns": ["id"], "basis": "命名"}]
    assert _facts(events, "view")[0]["name"] == "v_orders"
    routine = _facts(events, "routine")[0]
    assert routine["type"] == "procedure" and routine["signature"] == "sp_cancel_order(IN p_order_id bigint)"
    assert {fact["table"] for fact in _facts(events, "primary_key")} == {"customers", "orders", "order_notes"}
    assert any("触发器" in event["text"] for event in events if event["event"] == "limitation")
    # 每条发出去的语句都过了白名单；二进制列只取「是否为空」，文本列只取前 200 字，按主键排序
    _assert_guarded(fake.log, "mysql")
    select = next(sql for sql in fake.log if "FROM `shop`.`customers`" in sql)
    assert "(`photo` IS NULL)" in select and "SUBSTRING(`phone`, 1, 200)" in select and "ORDER BY `id`" in select
    assert [event["done"] for event in events if event["event"] == "progress"][-1] == 3


def test_mysql_write_account_refused_before_reading_data(monkeypatch):
    events, error, fake = _run_mysql(monkeypatch, fake=FakeMySQL(grants=RW_GRANTS))
    assert error.code == "account_not_readonly" and "INSERT" in error.message
    assert not any("FROM `shop`." in sql for sql in fake.log)
    assert not [event for event in events if event["event"] == "table"]


@pytest.mark.parametrize("tables,expected", [
    ("", ["customers", "order_notes", "orders"]),
    ("orders, cust*", ["customers", "orders"]),
    ("shop.orders", ["orders"]),
    ("other.*", []),
])
def test_mysql_scope(monkeypatch, tables, expected):
    events, error, _ = _run_mysql(monkeypatch, tables=tables)
    assert error is None
    assert [event["name"] for event in events if event["event"] == "table"] == expected
    if not expected:
        assert any(event.get("text") == "范围内没有可读的表" for event in events)


def test_names_prefixed_across_schemas(monkeypatch):
    events, error, _ = _run_mysql(monkeypatch, fake=FakeMySQL(schemas=("shop", "ops")), tables="shop.*, ops.*")
    names = [event["name"] for event in events if event["event"] == "table"]
    assert names == ["ops.customers", "ops.order_notes", "ops.orders", "shop.customers", "shop.order_notes",
                     "shop.orders"]
    fk = [fact for fact in _facts(events, "foreign_key") if fact["table"] == "ops.orders"][0]
    assert fk["ref_table"] == "shop.customers"  # 假库的外键都指 shop


def test_failed_table_is_skipped_and_collection_continues(monkeypatch):
    events, error, fake = _run_mysql(monkeypatch, fake=FakeMySQL(fail_on="FROM `shop`.`orders`"))
    assert error is None
    tables = {event["name"]: event for event in events if event["event"] == "table"}
    assert tables["orders"]["rows"] == [] and tables["customers"]["rows"]
    texts = [event["text"] for event in events if event["event"] == "limitation"]
    assert any(text.startswith("orders 的样本没取到") and HOST not in text for text in texts)
    assert "START TRANSACTION READ ONLY" in fake.log[fake.log.index("ROLLBACK") + 1:]


def test_deadline_keeps_structure_without_samples(monkeypatch):
    now = {"t": 0.0}

    def clock():
        now["t"] += 0.5
        return now["t"]

    # 限速时每条语句看三次钟（各走 0.5 秒），会话设置另看两次：元数据 14 条走到 22.5 秒，截止 23.5 秒，
    # 第一张表读得到样本，第二张起到点
    events, error, _ = _run_mysql(monkeypatch, clock=clock, timeout_s=23)
    assert error is None
    tables = [event for event in events if event["event"] == "table"]
    assert len(tables) == 3 and tables[0]["rows"] and not tables[-1]["rows"]
    assert any("超过任务时限" in event.get("text", "") for event in events)


def test_row_limit_follows_width():
    assert dbkit._row_limit([object()] * 3) == 200
    assert dbkit._row_limit([object()] * 500) == 40
    assert dbkit._row_limit([object()] * 50_000) == 1


def test_infer_refs_by_naming():
    tables = [dbkit.Table("s", name) for name in ("order", "categories", "box", "order_items", "notes")]
    columns = {("s", "order_items"): [dbkit.Column("s", "order_items", n, 0, "int", False)
                                      for n in ("id", "order_id", "category_id", "box_id", "nobody_id")],
               ("s", "categories"): [dbkit.Column("s", "categories", "id", 0, "int", False)],
               ("s", "box"): [dbkit.Column("s", "box", "box_no", 0, "int", False)],
               ("s", "order"): [dbkit.Column("s", "order", "order_no", 0, "int", False)]}
    constraints = {("s", "order"): [dbkit.Constraint("s", "order", "pk", "pk", ["order_no"])]}
    refs = dbkit.infer_refs(tables, columns, constraints)
    assert [(ref.column, ref.target_table, ref.target_column) for ref in refs] == [
        ("order_id", "order", "order_no"), ("category_id", "categories", "id")]


# ---------------- 测试连接与错误消息 ----------------


def _checks(events):
    return [(event["name"], event["ok"]) for event in events if event["event"] == "check"]


def test_mysql_test_checks(monkeypatch):
    events, _, fake = _run_mysql(monkeypatch, "test")
    assert _checks(events) == [("连接", True), ("只读", True), ("只读会话", True)]
    _assert_guarded(fake.log, "mysql")
    events, _, _ = _run_mysql(monkeypatch, "test", fake=FakeMySQL(grants=RW_GRANTS))
    assert _checks(events) == [("连接", True), ("只读", False), ("只读会话", True)]


def test_connect_failure_hides_password_and_host(monkeypatch):
    def refuse(_ctx):
        raise PyMySQLError(2003, f"Can't connect to MySQL server on '{HOST}' (password {PASSWORD})")

    monkeypatch.setattr(MYSQL.DIALECT, "connect", refuse)
    ctx, out = _ctx("test")
    MYSQL.test(ctx)
    assert PASSWORD not in out.getvalue() and HOST not in out.getvalue()
    assert _checks(_events(out)) == [("连接", False)]


def _pg_error(arg):
    error = type("DatabaseError", (Exception,), {"__module__": "pg8000.exceptions"})
    return error(arg)


def test_errors_never_carry_password_host_or_ip():
    ctx, _ = _ctx()
    cases = [
        (MYSQL.DIALECT, PyMySQLError(1045, "Access denied for user 'onto_ro'@'192.0.2.10' (using password: YES)"),
         "auth_failed"),
        (MYSQL.DIALECT, PyMySQLError(2003, f"Can't connect to MySQL server on '{HOST}' ([Errno 111])"), "connect_failed"),
        (MYSQL.DIALECT, RuntimeError(f"mysql://onto_ro:{PASSWORD}@{HOST}:3306/shop failed"), "db_error"),
        (POSTGRES.DIALECT, _pg_error({"C": "28P01", "M": 'password authentication failed for user "onto_ro"'}),
         "auth_failed"),
        (POSTGRES.DIALECT, _pg_error(f"Can't create a connection to host {HOST} and port 5432"), "connect_failed"),
        (POSTGRES.DIALECT, ValueError(f"bad password {PASSWORD} for 198.51.100.7"), "db_error"),
        (POSTGRES.DIALECT, TimeoutError("timed out"), "connect_failed"),
    ]
    for dialect, exc, code in cases:
        error = dialect.error(exc, ctx)
        assert error.code == code, (exc, error.code)
        assert PASSWORD not in error.message and HOST not in error.message and "onto_ro" not in error.message
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", error.message), error.message


def test_grants_parsing():
    assert MYSQL.grants_readonly(RO_GRANTS)[0]
    assert not MYSQL.grants_readonly(RW_GRANTS)[0]
    assert MYSQL.grants_readonly(["GRANT SELECT (a, b) ON `shop`.`t` TO `u`@`%`"])[0]
    for lines in (["GRANT SELECT ON *.* TO `u`@`%` WITH GRANT OPTION"],
                  ["GRANT SELECT, TRIGGER ON `shop`.* TO `u`@`%`"],
                  ["GRANT SELECT, EXECUTE ON `shop`.* TO `u`@`%`"],
                  ["GRANT ALL PRIVILEGES ON *.* TO `u`@`%`"],
                  ["GRANT SELECT (a), UPDATE (b) ON `shop`.`t` TO `u`@`%`"],
                  ["GRANT SELECT ON `shop`.* TO `u`@`%`", "GRANT `app_role`@`%` TO `u`@`%`"],
                  ["GRANT USAGE ON *.* TO `u`@`%`"]):
        assert not MYSQL.grants_readonly(lines)[0], lines


# ---------------- PostgreSQL 方言 ----------------


class FakePg:
    def __init__(self, superuser=False, writable=False):
        self.log: list[str] = []
        self.superuser = superuser
        self.writable = writable

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
            return [("public", "t", "r", "表", True, self.writable, 16384),
                    ("sales", "t2", "r", "", True, False, 16385)]
        if "FROM pg_constraint con" in sql:
            return [("public", "t", "t_pkey", "p", "PRIMARY KEY (id)", ["id"], None, None, []),
                    ("public", "t", "t_flag_check", "c", "CHECK (flag IS NOT NULL)", ["flag"], None, None, [])]
        if "FROM pg_attribute a" in sql:
            return [("public", "t", "id", 1, False, "bigint", "N", "int8", "编号"),
                    ("public", "t", "flag", 2, True, "boolean", "B", "bool", None),
                    ("sales", "t2", "t_id", 1, False, "bigint", "N", "int8", None)]
        if "FROM \"" in sql:
            return [(1, True)]
        if "current_setting" in sql:
            return [("on",)]
        return []


def test_postgres_statements_pass_guard_and_readonly_check(monkeypatch):
    for superuser, writable, expected in ((False, False, True), (True, False, False), (False, True, False)):
        fake = FakePg(superuser, writable)
        ctx, _ = _ctx()
        cur = dbkit.GuardedCursor(ctx, fake, POSTGRES.DIALECT)
        for sql in POSTGRES.DIALECT.session_statements(ctx):
            cur.session(sql)
        tables = POSTGRES.DIALECT.list_tables(cur)
        assert POSTGRES.DIALECT.readonly_check(cur, tables)[0] is expected
        POSTGRES.DIALECT.columns(cur, ["public"])
        POSTGRES.DIALECT.constraints(cur, ["public"])
        POSTGRES.DIALECT.views(cur, [dbkit.Table("public", "v", "view", key=1)])
        POSTGRES.DIALECT.triggers(cur, ["public"])
        POSTGRES.DIALECT.routines(cur, ["public"])
        cur.query(POSTGRES.DIALECT.version_sql())
        cur.query(POSTGRES.DIALECT.read_only_probe_sql())
        cur.recover()
        assert cur.statements == len(fake.log) >= 15
        _assert_guarded(fake.log, "postgres")


def test_postgres_collect_prefixes_schemas_and_reads_check(monkeypatch):
    fake = FakePg()
    monkeypatch.setattr(POSTGRES.DIALECT, "connect", lambda ctx: fake)
    ctx, out = _ctx(database="shopdb")
    POSTGRES.collect(ctx)
    events = _events(out)
    assert [event["name"] for event in events if event["event"] == "table"] == ["public.t", "sales.t2"]
    assert _facts(events, "check")[0]["expression"] == "flag IS NOT NULL"
    # 推断只在同一个 schema 里找目标表：sales.t2.t_id 不会对到 public.t
    assert _facts(events, "reference") == []
    select = next(sql for sql in fake.log if 'FROM "public"."t"' in sql)
    assert 'ORDER BY "id"' in select and '"flag"' in select
    _assert_guarded(fake.log, "postgres")
    # 只在一个 schema 时不带前缀
    fake = FakePg()
    monkeypatch.setattr(POSTGRES.DIALECT, "connect", lambda ctx: fake)
    ctx, out = _ctx(database="shopdb", tables="public.*")
    POSTGRES.collect(ctx)
    assert [event["name"] for event in _events(out) if event["event"] == "table"] == ["t"]


def test_manifests_parse_as_source_connectors():
    import yaml

    from app.connector_gateway import plugins

    for pack in ("db-mysql", "db-postgres"):
        plugin = plugins.load_plugin(PACKS / pack, origin="builtin", name=pack)
        assert plugin.kind == "source" and plugin.source.info.official
        assert plugin.source.info.secret_keys == ("password",)
        manifest = yaml.safe_load((PACKS / pack / "manifest.yaml").read_text(encoding="utf-8"))
        assert manifest["publisher"]["kind"] == "official"
