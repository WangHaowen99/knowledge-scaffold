"""ADR-0103 数据源连接器连真库：测试床 `connectors/testbed/db`（合成的订单库），经网关子进程 + 渲染跑一遍。

两种跑法：
- 给了 `DS_DB_MYSQL_HOST`、`DS_DB_PG_HOST`（端口 `DS_DB_MYSQL_PORT`、`DS_DB_PG_PORT`）和账号密码 `DS_DB_RO_PASSWORD`、
  `DS_DB_RW_PASSWORD` 就连它们（Linux 测试镜像里经 host.docker.internal 连本机的测试床）；
- 否则本机有 docker、也有 mysql:8.0 和 postgres:16 镜像时，自己起一套测试床（临时端口、随机密码），跑完删掉。
都没有、或没装驱动时跳过。
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from app.connector_gateway.source import render, runner
from app.services.table_sources import rules

ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "assets" / "packs"
TESTBED = ROOT / "connectors" / "testbed" / "db"
LIMITS = runner.SourceLimits(timeout_s=120)
SALT = "c3" * 16

DATABASES = {
    "mysql": {"pack": "db-mysql", "driver": "pymysql", "env": "DS_DB_MYSQL", "database": "shop", "tables": ""},
    "postgres": {"pack": "db-postgres", "driver": "pg8000", "env": "DS_DB_PG", "database": "shopdb",
                 "tables": "shop.*"},
}
SHOP_TABLES = ["customers", "order_items", "order_notes", "orders", "payments", "products", "shipments"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        images = subprocess.run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True,
                                text=True, timeout=20, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return False
    return "mysql:8.0" in images and "postgres:16" in images


@pytest.fixture(scope="module")
def testbed():
    if os.environ.get("DS_DB_MYSQL_HOST") and os.environ.get("DS_DB_PG_HOST"):
        yield {"mysql": (os.environ["DS_DB_MYSQL_HOST"], int(os.environ.get("DS_DB_MYSQL_PORT") or 3306)),
               "postgres": (os.environ["DS_DB_PG_HOST"], int(os.environ.get("DS_DB_PG_PORT") or 5432)),
               "ro": os.environ["DS_DB_RO_PASSWORD"], "rw": os.environ["DS_DB_RW_PASSWORD"]}
        return
    if not _docker_ready():
        pytest.skip("没给 DS_DB_*_HOST，本机也没有 docker 或 mysql:8.0、postgres:16 镜像")
    project = f"onto-ds-db-{os.getpid()}"
    env = {**os.environ, "TESTBED_DB_ROOT_PASSWORD": secrets.token_hex(12), "TESTBED_DB_RO_PASSWORD": secrets.token_hex(12),
           "TESTBED_DB_RW_PASSWORD": secrets.token_hex(12), "TESTBED_MYSQL_PORT": str(_free_port()),
           "TESTBED_PG_PORT": str(_free_port()), "TESTBED_DB_NETWORK": f"{project}-biz"}
    compose = ["docker", "compose", "-p", project, "-f", str(TESTBED / "docker-compose.yml")]
    try:
        subprocess.run([*compose, "up", "-d", "--wait"], env=env, check=True, capture_output=True, timeout=300)
        yield {"mysql": ("127.0.0.1", int(env["TESTBED_MYSQL_PORT"])),
               "postgres": ("127.0.0.1", int(env["TESTBED_PG_PORT"])),
               "ro": env["TESTBED_DB_RO_PASSWORD"], "rw": env["TESTBED_DB_RW_PASSWORD"]}
    finally:
        subprocess.run([*compose, "down", "-v"], env=env, capture_output=True, timeout=120)


@pytest.fixture(params=list(DATABASES))
def db(request, testbed):
    spec = DATABASES[request.param]
    pytest.importorskip(spec["driver"])
    host, port = testbed[request.param]
    return {**spec, "name": request.param, "host": host, "port": port, "ro": testbed["ro"], "rw": testbed["rw"]}


def _run(db, operation, *, rw=False, tables=None):
    config = {"host": db["host"], "port": db["port"], "database": db["database"], "user": "onto_rw" if rw else "onto_ro",
              "tables": db["tables"] if tables is None else tables}
    return runner.run(PACKS / db["pack"] / "connector.py", None, operation, config,
                      {"password": db["rw"] if rw else db["ro"]}, [{"host": db["host"], "port": db["port"]}], LIMITS)


def _originals(db) -> list[str]:
    """测试床里的原始个人信息（只读账号直接查），用来确认文本版里一个都没漏出来。"""
    queries = ["SELECT customer_name, phone, id_card, email, address, password_hash FROM shop.customers",
               "SELECT receiver_name, receiver_phone, shipping_address FROM shop.orders",
               "SELECT card_no FROM shop.payments WHERE card_no IS NOT NULL"]
    if db["name"] == "mysql":
        import pymysql

        conn = pymysql.connect(host=db["host"], port=db["port"], user="onto_ro", password=db["ro"], database="shop")
    else:
        import pg8000.dbapi

        conn = pg8000.dbapi.connect(host=db["host"], port=db["port"], user="onto_ro", password=db["ro"],
                                    database="shopdb")
    values: list[str] = []
    try:
        cursor = conn.cursor()
        for sql in queries:
            cursor.execute(sql)
            values += [str(value) for row in cursor.fetchall() for value in row if value is not None]
    finally:
        conn.close()
    return values


def test_connection_checks(db):
    outcome = _run(db, "test")
    assert outcome.ok and [(c["name"], c["ok"]) for c in outcome.checks] == [("连接", True), ("只读", True),
                                                                               ("只读会话", True)]
    outcome = _run(db, "test", rw=True)
    assert dict((c["name"], c["ok"]) for c in outcome.checks)["只读"] is False


def test_write_account_refused(db):
    outcome = _run(db, "collect", rw=True)
    assert not outcome.ok and outcome.error_code == "account_not_readonly"
    assert db["rw"] not in outcome.error_message and not outcome.collected.tables


def test_collect_renders_masked_text_version(db):
    outcome = _run(db, "collect")
    assert outcome.ok, (outcome.error_code, outcome.error_message)
    markdown, stats, limitations = render.render(outcome.collected, SALT)
    assert stats["tables"] == 7 and stats["rows"] == 7 * 20
    facts = render.parse_facts(markdown)
    assert sorted(facts["tables"]) == SHOP_TABLES
    foreign = {(fk["table"], tuple(fk["columns"]), fk["ref_table"]) for fk in facts["foreign_keys"]}
    assert ("orders", ("customer_id",), "customers") in foreign and len(foreign) == 5
    inferred = {(ref["table"], ref["columns"][0], ref["ref_table"]) for ref in facts["references"]}
    assert inferred == {("order_notes", "order_id", "orders"), ("order_notes", "customer_id", "customers")}
    assert {"table": "orders", "column": "status",
            "values": ["CANCELLED", "CREATED", "DONE", "PAID", "SHIPPED"]} in facts["values"]
    assert any(check["table"] == "products" for check in facts["checks"])
    assert facts["tables"]["customers"]["comment"] == "客户"
    assert "## 视图" in markdown and "v_customer_orders" in markdown and "## 存储过程" in markdown
    if db["name"] == "mysql":
        assert any("触发器" in item for item in limitations)
    else:
        assert "trg_order_items_amount" in markdown
    # 个人信息一个都没漏，密码哈希整列清空
    leaked = [value for value in _originals(db) if len(value) >= 6 and value in markdown]
    assert not leaked, leaked[:5]
    customers = next(sheet for sheet in rules.text_sheets(markdown) if sheet["name"] == "customers")
    header = customers["rows"][0]
    assert header[:3] == ["id", "customer_name", "phone"]
    assert all(row[header.index("password_hash")] == "" for row in customers["rows"][1:])
    assert rules.classify_sheet("customers", customers["rows"]) == rules.TABLE


def test_postgres_all_schemas_prefixed(testbed):
    pytest.importorskip("pg8000")
    host, port = testbed["postgres"]
    db = {**DATABASES["postgres"], "name": "postgres", "host": host, "port": port, "ro": testbed["ro"],
          "rw": testbed["rw"]}
    outcome = _run(db, "collect", tables="")
    assert outcome.ok
    names = [table["name"] for table in outcome.collected.tables]
    assert "ops.audit_log" in names and "shop.orders" in names
    fk = next(fact for fact in outcome.collected.facts if fact["kind"] == "foreign_key" and fact["table"] == "shop.orders")
    assert fk["ref_table"] == "shop.customers"
