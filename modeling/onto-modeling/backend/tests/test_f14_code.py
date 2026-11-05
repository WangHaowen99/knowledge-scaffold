"""F14 代码仓库提供方 code-repo：加固克隆与 Java / Python / TypeScript 静态分析（ADR-0084；证据采集接口 §10）。

测试仓库全是合成代码（`connectors/testbed/code/`）；git 服务用同目录的 `server.py` 起在 127.0.0.1 随机端口，
令牌每次随机生成。克隆走 http，靠提供方的测试参数 `allow_http` 放开本机地址。
"""

import importlib.util
import io
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.connector_gateway.conn import code_git, code_python, code_repo, code_ts
from app.connector_gateway.conn.code_model import SNIPPET_LINES, Sink
from app.connector_gateway.conn.context import ConnError, Context
from app.evidence import RECORD_TYPES
from app.evidence.spec import record_hash

TESTBED = Path(__file__).resolve().parents[2] / "connectors" / "testbed" / "code"
SOURCE = "demo-repo"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="没有 git")


def _load_server():
    spec = importlib.util.spec_from_file_location("testbed_git_server", TESTBED / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _analyze(project: str, analyzer, suffix: str) -> tuple[list[dict], Sink]:
    records: list[dict] = []
    sink = Sink(SOURCE, records.append)
    root = TESTBED / project
    for path in sorted(root.rglob(f"*{suffix}")):
        analyzer(sink, path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
    sink.finish()
    return records, sink


def _by_id(records: list[dict]) -> dict[str, dict]:
    return {record["id"]: record for record in records}


def _has_float(value) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(_has_float(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_float(item) for item in value)
    return False


def _check_records(records: list[dict]) -> None:
    ids = set()
    for record in records:
        assert record["id"].startswith(f"code:{SOURCE}/") and record["id"] not in ids
        ids.add(record["id"])
        assert record["type"] in RECORD_TYPES
        assert not _has_float(record), record["id"]
        loc = record["attrs"]["loc"]
        assert isinstance(loc["file"], str) and 1 <= loc["line"] <= loc["end_line"]
        if record["type"] in ("entity", "operation"):
            assert 0 < len(record["attrs"]["snippet"].splitlines()) <= SNIPPET_LINES
        assert record["evidence"]["provider"].startswith("code-repo@")
        assert record["evidence"]["confidence"] in ("declared", "observed", "inferred")


# ---------------------------------------------------------------------------
# 分析：三个合成项目
# ---------------------------------------------------------------------------


def test_java_project():
    code_java = pytest.importorskip("app.connector_gateway.conn.code_java")
    records, sink = _analyze("orders-java", code_java.analyze, ".java")
    _check_records(records)
    by_id = _by_id(records)
    base = f"code:{SOURCE}/sym:com::example::orders::"
    order = by_id[base + "domain::Order"]
    assert order["type"] == "entity" and order["label"] == "订单" and order["attrs"]["orm"] == "jpa"
    assert order["attrs"]["table"] == "orders" and order["evidence"]["confidence"] == "declared"
    assert order["attrs"]["snippet"].startswith("/** 订单 */") or order["attrs"]["snippet"].startswith("@Entity")
    amount = by_id[base + "domain::Order::totalAmount"]
    assert amount["data_type"] == "BigDecimal" and amount["attrs"]["column"] == "total_amount"
    assert amount["label"] == "订单金额" and amount["parent"] == base + "domain::Order"
    customer = by_id[base + "domain::Order::customer"]
    assert customer["attrs"]["column"] == "customer_id" and customer["attrs"]["nullable"] is False
    relations = [record for record in records if record["type"] == "relation"]
    assert {(item["name"], item["from"].rsplit("::", 1)[-1], (item["to"] or "").rsplit("::", 1)[-1])
            for item in relations if item["evidence"]["confidence"] == "declared"} == {
        ("one_to_many", "Customer", "Order"), ("many_to_one", "Order", "Customer"),
        ("one_to_many", "Order", "OrderItem"), ("many_to_one", "OrderItem", "Order"),
        ("many_to_one", "OrderItem", "Product")}
    assert all(item["id"].startswith(f"code:{SOURCE}/rel:") for item in relations)
    status = by_id[base + "domain::OrderStatus"]
    assert status["attrs"]["kind"] == "enum" and status["attrs"]["values"] == ["CREATED", "PAID", "SHIPPED",
                                                                               "CANCELLED"]
    # 校验注解与约束
    rule = by_id[f"code:{SOURCE}/rule:com::example::orders::domain::Product::sku::Pattern"]
    assert rule["type"] == "rule" and rule["evidence"]["confidence"] == "declared"
    assert rule["attrs"]["params"] == {"regexp": "[A-Z]{3}-\\d{4}"} and rule["parent"] == base + "domain::Product::sku"
    assert by_id[f"code:{SOURCE}/rule:com::example::orders::domain::Order::totalAmount::DecimalMin"]["attrs"][
        "params"] == {"value": "0.01"}
    assert f"code:{SOURCE}/rule:com::example::orders::web::CreateOrderRequest::ItemLine::quantity::Positive" in by_id
    # 路由：类上的前缀 + 方法上的路径，handler 指向方法锚点
    route = by_id[f"code:{SOURCE}/route:POST%20%2Fapi%2Forders%2F{{id}}%2Fcancel"]
    assert route["attrs"]["method"] == "POST" and route["attrs"]["url"] == "/api/orders/{id}/cancel"
    assert route["attrs"]["handler"] == base + "web::OrderController::cancel" and route["attrs"]["handler"] in by_id
    assert f"code:{SOURCE}/route:GET%20%2Fapi%2Forders%2Fsearch" in by_id
    constant = [item for item in records if item["attrs"].get("kind") == "route" and "STATS" in item["name"]]
    assert constant and constant[0]["evidence"]["confidence"] == "inferred"
    assert any("常量" in text for text in sink.limitations())
    # 服务的公开方法、内嵌 SQL、仓库接口上的 @Query
    assert base + "service::OrderService::submit" in by_id and base + "service::OrderService::audit" not in by_id
    reserve = by_id[base + "service::InventoryService::reserve"]
    assert reserve["attrs"]["sql"].startswith("UPDATE products") and reserve["attrs"]["tables"] == ["products"]
    totals = by_id[base + "repository::OrderRepository::totalsByCustomer"]
    assert totals["attrs"]["native"] is True and totals["attrs"]["tables"] == ["orders", "customers"]
    calls = {(item["from"].rsplit("::", 1)[-1], item["to"].rsplit("::", 1)[-1]) for item in relations
             if item["name"] == "calls"}
    assert ("submit", "reserve") in calls and ("create", "submit") in calls
    assert all(item["evidence"]["confidence"] == "inferred" for item in relations if item["name"] == "calls")


def test_python_project():
    records, _ = _analyze("orders-python", code_python.analyze, ".py")
    _check_records(records)
    by_id = _by_id(records)
    base = f"code:{SOURCE}/sym:app::orders::"
    order = by_id[base + "models::Order"]
    assert order["attrs"]["orm"] == "sqlalchemy" and order["attrs"]["table"] == "orders" and order["label"] == "订单"
    customer_name = by_id[base + "models::Customer::name"]
    assert customer_name["data_type"] == "String(64)" and customer_name["label"] == "客户名称"
    assert by_id[base + "models::Customer::email"]["attrs"]["nullable"] is True
    assert by_id[base + "models::Order::items"]["data_type"] == "list[OrderItem]"
    relations = {(item["name"], item["from"].rsplit("::", 1)[-1], (item["to"] or "").rsplit("::", 1)[-1])
                 for item in records if item["type"] == "relation"}
    assert {("references", "Order", "Customer"), ("many_to_one", "Order", "Customer"),
            ("one_to_many", "Customer", "Order"), ("one_to_many", "Order", "OrderItem"),
            ("calls", "create_order", "submit"), ("calls", "submit", "reserve")} <= relations
    fk = next(item for item in records if item["type"] == "relation" and item["name"] == "references"
              and item["from"].endswith("::Order"))
    assert fk["attrs"]["columns"] == [["customer_id", "id"]]
    # 约束与校验器
    check = by_id[f"code:{SOURCE}/rule:app::orders::models::Order::ck_order_amount"]
    assert check["attrs"]["params"] == {"expression": "total_amount >= 0"}
    assert by_id[f"code:{SOURCE}/rule:app::orders::models::Order::total_amount::check_amount"]["attrs"][
        "framework"] == "sqlalchemy"
    assert by_id[f"code:{SOURCE}/rule:app::orders::schemas::OrderItemIn::quantity::ge"]["attrs"]["params"] == {
        "value": 1}
    # 浮点约束转成字符串
    assert by_id[f"code:{SOURCE}/rule:app::orders::schemas::OrderCreate::discount_rate::le"]["attrs"]["params"] == {
        "value": "0.5"}
    assert by_id[f"code:{SOURCE}/rule:app::orders::schemas::OrderCreate::items::no_duplicate_sku"]["attrs"][
        "params"]["decorator"] == "field_validator"
    # 路由：APIRouter 前缀
    route = by_id[f"code:{SOURCE}/route:POST%20%2Fapi%2Forders%2F{{order_id}}%2Fcancel"]
    assert route["attrs"]["handler"] == base + "api::cancel_order" and route["attrs"]["handler"] in by_id
    assert f"code:{SOURCE}/route:GET%20%2Fhealth" in by_id
    totals = by_id[base + "service::OrderService::totals_by_customer"]
    assert totals["attrs"]["tables"] == ["orders", "customers"] and "GROUP BY" in totals["attrs"]["sql"]
    assert base + "service::OrderService::_audit" not in by_id
    assert by_id[base + "models::OrderStatus"]["attrs"]["values"] == ["CREATED", "PAID", "SHIPPED", "CANCELLED"]


def test_typescript_project():
    records, _ = _analyze("orders-ts", code_ts.analyze, ".ts")
    _check_records(records)
    by_id = _by_id(records)
    entity = f"code:{SOURCE}/sym:src%2Forders%2Forder%2Eentity%2Ets::Order"
    order = by_id[entity]
    assert order["attrs"]["orm"] == "typeorm" and order["attrs"]["table"] == "orders" and order["label"] == "订单"
    amount = by_id[entity + "::totalAmount"]
    assert amount["attrs"]["column"] == "total_amount" and amount["attrs"]["column_type"] == "decimal"
    assert amount["label"] == "订单金额" and amount["data_type"] == "string"
    customer = f"code:{SOURCE}/sym:src%2Forders%2Fcustomer%2Eentity%2Ets::Customer"
    assert by_id[customer]["attrs"]["table"] == "customers"
    assert by_id[customer + "::name"]["label"] == "客户名称"
    relations = {(item["name"], item["from"], item["to"]) for item in records if item["type"] == "relation"}
    assert ("many_to_one", entity, customer) in relations and ("one_to_many", customer, entity) in relations
    dto = f"code:{SOURCE}/rule:src%2Forders%2Fdto%2Fcreate-order%2Edto%2Ets::CreateOrderItemDto::"
    assert by_id[dto + "sku::Matches"]["attrs"]["params"] == {"args": ["/^[A-Z]{3}-\\d{4}$/"]}
    assert by_id[dto + "quantity::Max"]["attrs"]["params"] == {"args": [999]}
    handler = f"code:{SOURCE}/sym:src%2Forders%2Forders%2Econtroller%2Ets::OrdersController::cancel"
    route = by_id[f"code:{SOURCE}/route:POST%20%2Fapi%2Forders%2F:id%2Fcancel"]
    assert route["attrs"]["handler"] == handler and handler in by_id
    service = f"code:{SOURCE}/sym:src%2Forders%2Forders%2Eservice%2Ets::OrdersService"
    totals = by_id[service + "::totalsByCustomer"]
    assert totals["attrs"]["tables"] == ["orders", "customers"]
    # 返回类型里的对象字面量类型不能把方法体切错
    assert totals["attrs"]["loc"]["end_line"] - totals["attrs"]["loc"]["line"] >= 4
    assert service + "::audit" not in by_id
    assert (f"{service}::submit", f"code:{SOURCE}/sym:src%2Finventory%2Finventory%2Eservice%2Ets::"
            "InventoryService::reserve") in {(item[1], item[2]) for item in relations}
    status = by_id[f"code:{SOURCE}/sym:src%2Forders%2Forder-status%2Eenum%2Ets::OrderStatus"]
    assert status["attrs"]["values"] == ["Created", "Paid", "Shipped", "Cancelled"]


# ---------------------------------------------------------------------------
# 分析：其他常见写法与识别不了的写法
# ---------------------------------------------------------------------------


DJANGO = '''
from django.db import models
from django.core.validators import MinValueValidator


class Store(models.Model):
    name = models.CharField("门店名称", max_length=64, unique=True)

    class Meta:
        db_table = "stores"
        verbose_name = "门店"


class Ticket(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="tickets")
    seats = models.IntegerField(validators=[MinValueValidator(1)], null=True)
'''

FLASK = '''
from flask import Blueprint, Flask
from pydantic import BaseModel, Field
from typing import Annotated, Optional

app = Flask(__name__)
bp = Blueprint("tickets", __name__, url_prefix="/tickets")


@bp.route("/<int:ticket_id>", methods=["GET", "DELETE"])
def ticket(ticket_id):
    return "SELECT id FROM tickets WHERE id = %s"


@app.route("/ping")
def ping():
    return "pong"


app.add_url_rule("/legacy", view_func=ping)
audit = Table("audit_log", metadata, Column("id", Integer, primary_key=True))


class TicketIn(BaseModel):
    seats: Annotated[int, Field(gt=0, le=12, description="座位数")]
    note: Optional[str] = None
'''


def test_python_other_frameworks():
    records: list[dict] = []
    sink = Sink(SOURCE, records.append)
    code_python.analyze(sink, "shop/models.py", DJANGO)
    code_python.analyze(sink, "shop/views.py", FLASK)
    sink.finish()
    _check_records(records)
    by_id = _by_id(records)
    store = by_id[f"code:{SOURCE}/sym:shop::models::Store"]
    assert store["attrs"]["orm"] == "django" and store["attrs"]["table"] == "stores" and store["label"] == "门店"
    assert by_id[f"code:{SOURCE}/sym:shop::models::Store::name"]["label"] == "门店名称"
    assert by_id[f"code:{SOURCE}/rule:shop::models::Store::name::max_length"]["attrs"]["params"] == {"value": 64}
    assert f"code:{SOURCE}/rule:shop::models::Store::name::unique" in by_id
    assert by_id[f"code:{SOURCE}/rule:shop::models::Ticket::seats::MinValueValidator"]["attrs"]["params"] == {
        "value": 1}
    relation = next(item for item in records if item["type"] == "relation")
    assert relation["name"] == "many_to_one" and relation["to"] == f"code:{SOURCE}/sym:shop::models::Store"
    assert relation["attrs"]["related_name"] == "tickets"
    routes = {item["name"] for item in records if item["attrs"].get("kind") == "route"}
    assert routes == {"GET /tickets/<int:ticket_id>", "DELETE /tickets/<int:ticket_id>", "GET /ping"}
    assert by_id[f"code:{SOURCE}/sym:shop::views::ticket"]["attrs"]["tables"] == ["tickets"]
    assert by_id[f"code:{SOURCE}/rule:shop::views::TicketIn::seats::le"]["attrs"]["params"] == {"value": 12}
    assert by_id[f"code:{SOURCE}/sym:shop::views::TicketIn::seats"]["label"] == "座位数"
    assert by_id[f"code:{SOURCE}/sym:shop::views::TicketIn::note"]["attrs"]["nullable"] is True
    notes = " / ".join(sink.limitations())
    assert "add_url_rule" in notes and "Table()" in notes


def test_unrecognized_ts_patterns_become_limitations():
    records: list[dict] = []
    sink = Sink(SOURCE, records.append)
    code_ts.analyze(sink, "src/main.ts", "async function boot() {\n  app.setGlobalPrefix('api');\n}\n")
    code_ts.analyze(sink, "src/legacy.ts", "router.get('/legacy', (req, res) => res.send('x'));\n")
    code_ts.analyze(sink, "src/paths.controller.ts", "@Controller(PATHS.root)\nexport class PathsController {\n"
                                                     "  @Get(PATHS.list)\n  list() {\n    return [];\n  }\n}\n")
    sink.finish()
    text = " / ".join(sink.limitations())
    assert "setGlobalPrefix" in text and "Express" in text and "常量" in text
    route = next(item for item in records if item["attrs"].get("kind") == "route")
    assert route["name"] == "GET /{PATHS.root}/{PATHS.list}" and route["evidence"]["confidence"] == "inferred"


def test_anchor_collisions_get_suffix():
    records: list[dict] = []
    sink = Sink(SOURCE, records.append)
    code_python.analyze(sink, "app/svc.py", "class PayService:\n    def pay(self, a):\n        pass\n\n"
                                            "    def pay(self, a, b):\n        pass\n")
    ids = [item["id"] for item in records if item["type"] == "operation"]
    assert ids == [f"code:{SOURCE}/sym:app::svc::PayService::pay", f"code:{SOURCE}/sym:app::svc::PayService::pay~2"]


# ---------------------------------------------------------------------------
# 克隆：起 git 智能 HTTP 测试服务
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(scope="module")
def git_server(tmp_path_factory):
    if shutil.which("git") is None:
        pytest.skip("没有 git")
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        os.environ.pop(name, None)
    server_module = _load_server()
    root = tmp_path_factory.mktemp("git-root")
    commits = server_module.init_projects(root)
    token = secrets.token_hex(16)
    server = server_module.make_server(root, token)
    server_module.serve_in_thread(server)
    port = server.server_address[1]
    work = tmp_path_factory.mktemp("git-work")

    # 边界仓库：符号链接、子模块、超大文件、解析失败的文件
    edge = work / "edge"
    (edge / "app").mkdir(parents=True)
    (edge / "app" / "models.py").write_text("class Thing:\n    def run(self):\n        return 'SELECT a FROM things'\n")
    (edge / "app" / "Broken.java").write_text("public record Point(int x, int y) {}\n")
    (edge / "app" / "broken.py").write_text("def broken(:\n")
    (edge / "big").mkdir()
    (edge / "big" / "huge.py").write_text("# 合成的超大文件\n" + "x = 1\n" * 200_000)
    os.symlink("/etc/passwd", edge / "link.py")
    (edge / ".gitmodules").write_text(f'[submodule "sub"]\n\tpath = sub\n\turl = http://127.0.0.1:{port}/sub.git\n')
    _git("init", "-q", "-b", "main", cwd=edge)
    _git("add", "-A", cwd=edge)
    _git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub", cwd=edge)
    _git("commit", "-q", "-m", "edge", cwd=edge)
    server_module.publish(edge, root / "edge.git")
    server_module.commit_tree(TESTBED / "orders-ts", root / "sub.git")

    # 两个提交：按第一个提交号取
    history = work / "history"
    history.mkdir()
    (history / "first.py").write_text("class FirstService:\n    def one(self):\n        pass\n")
    _git("init", "-q", "-b", "main", cwd=history)
    _git("add", "-A", cwd=history)
    _git("commit", "-q", "-m", "first", cwd=history)
    first = _git("rev-parse", "HEAD", cwd=history)
    (history / "second.py").write_text("class SecondService:\n    def two(self):\n        pass\n")
    _git("add", "-A", cwd=history)
    _git("commit", "-q", "-m", "second", cwd=history)
    server_module.publish(history, root / "history.git")

    # 预算：40 个相近的小文件
    many = work / "many"
    (many / "pkg").mkdir(parents=True)
    for index in range(40):
        body = "".join(f"    def op_{index}_{item}(self):\n        return {item}\n" for item in range(40))
        (many / "pkg" / f"m{index:02d}.py").write_text(f"class Unit{index}Service:\n{body}")
    _git("init", "-q", "-b", "main", cwd=many)
    _git("add", "-A", cwd=many)
    _git("commit", "-q", "-m", "many", cwd=many)
    server_module.publish(many, root / "many.git")

    yield {"port": port, "token": token, "log": server.log, "commits": commits, "first": first}
    server.shutdown()


def _ctx(server, repo: str, *, token=None, ref="main", egress=None, budget=None, url=None, username=None):
    out = io.StringIO()
    connection = {"repo_url": url or f"http://127.0.0.1:{server['port']}/{repo}.git", "ref": ref}
    if username is not None:
        connection["username"] = username
    head = {"provider": "code-repo", "operation": "snapshot",
            "source": {"id": SOURCE, "provider": "code-repo", "connection": connection},
            "egress": egress if egress is not None else [{"host": "127.0.0.1", "port": server["port"]}],
            "budget": budget or {"max_rows": 200000, "max_bytes": 64 * 1024 * 1024, "max_duration_s": 120},
            "scope": []}
    return Context(head, {"token": server["token"] if token is None else token}, out, scheme="code"), out


def _events(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.fixture
def private_tmp(tmp_path, monkeypatch):
    """克隆的临时目录放到这个测试自己的目录里，方便核对用完删掉、没建符号链接。"""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


@needs_git
@pytest.mark.parametrize("project", ["orders-python", "orders-ts", "orders-java"])
def test_snapshot_end_to_end(git_server, private_tmp, project):
    if project == "orders-java":
        pytest.importorskip("javalang")
    ctx, out = _ctx(git_server, project)
    code_repo.snapshot(ctx, allow_http=True)
    events = _events(out)
    records = [item["record"] for item in events if item["event"] == "record"]
    _check_records(records)
    for record in records:
        assert record["hash"] == record_hash({key: value for key, value in record.items() if key != "hash"})
    result = events[-1]
    assert result["event"] == "result" and result["complete"] is True and result["coverage_pct"] == 100.0
    assert result["artifact"] == {"repo_url": f"http://127.0.0.1:{git_server['port']}/{project}.git", "ref": "main",
                                  "commit": git_server["commits"][project]}
    assert result["tool"]["name"] == ("javalang" if project == "orders-java" else "git") and result["tool"]["version"]
    inventory = next(item for item in events if item["event"] == "inventory")
    assert inventory["items"] and all(item["estimate_bytes"] >= 0 for item in inventory["items"])
    assert git_server["token"] not in out.getvalue()
    assert list(private_tmp.iterdir()) == []


@needs_git
def test_oauth2_username(git_server, private_tmp):
    ctx, out = _ctx(git_server, "orders-python", username="oauth2")
    code_repo.snapshot(ctx, allow_http=True)
    result = _events(out)[-1]
    assert "oauth2" not in result["artifact"]["repo_url"] and result["complete"] is True
    # 测试服务只认 x-access-token 和 oauth2
    ctx, _ = _ctx(git_server, "orders-python", username="someone")
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "auth_failed"
    for bad in ("a b", "x;rm", "$(id)", "-c"):
        with pytest.raises(ConnError):
            code_git.check_username(bad)


@needs_git
def test_wrong_token_rejected_without_leaking(git_server, private_tmp):
    wrong = secrets.token_hex(16)
    ctx, out = _ctx(git_server, "orders-python", token=wrong)
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "auth_failed"
    assert wrong not in caught.value.message and git_server["token"] not in caught.value.message
    assert wrong not in out.getvalue()
    assert list(private_tmp.iterdir()) == []
    ctx, out = _ctx(git_server, "orders-python", token="")
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "auth_failed"


@needs_git
def test_host_and_scheme_checks(git_server, private_tmp):
    before = len(git_server["log"])
    ctx, _ = _ctx(git_server, "orders-python", egress=[{"host": "git.example.invalid", "port": 443}])
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "egress_denied"
    ctx, _ = _ctx(git_server, "orders-python", egress=[{"host": "127.0.0.1", "port": git_server["port"] + 1}])
    with pytest.raises(ConnError):
        code_repo.snapshot(ctx, allow_http=True)
    # 生产路径不放开 http
    ctx, _ = _ctx(git_server, "orders-python")
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx)
    assert caught.value.code == "invalid_connection"
    assert len(git_server["log"]) == before
    for url in ("http://git.example.invalid/a.git", "file:///etc", "ssh://git.example.invalid/a.git",
                "ext::sh -c touch% /tmp/x", "https://git.example.invalid/a.git?x=1", "https://-x/a.git",
                "https://git.example.invalid/a b.git", "--upload-pack=x"):
        with pytest.raises(ConnError):
            code_git.parse_url(url, allow_http=True)
    secret = secrets.token_hex(8)
    with pytest.raises(ConnError) as caught:
        code_git.parse_url(f"https://user:{secret}@git.example.invalid/a.git")
    assert secret not in caught.value.message
    with pytest.raises(ConnError):
        code_git.parse_url("https://oauth2@git.example.invalid/a.git")
    for ref in ("-c", "--upload-pack=x", "a..b", "main:refs/x", "a b", "/x"):
        with pytest.raises(ConnError):
            code_git.check_ref(ref)
    assert code_git.parse_url("https://git.example.invalid/team/repo.git").port == 443


@needs_git
def test_redirect_not_followed(git_server, private_tmp):
    log = git_server["log"]
    before = len(log)
    url = f"http://127.0.0.1:{git_server['port']}/moved/orders-python.git"
    ctx, _ = _ctx(git_server, "orders-python", url=url)
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code in ("redirect_refused", "git_failed")
    paths = [item["path"] for item in log[before:]]
    assert paths and all(path.startswith("/moved/") for path in paths)


@needs_git
def test_submodule_symlink_and_large_file(git_server, private_tmp, monkeypatch):
    seen_symlinks = []
    original = code_git.Clone.read

    def read(self, entries, *, timeout):
        for folder, dirs, files in os.walk(self.workspace.root):
            seen_symlinks.extend(name for name in dirs + files if os.path.islink(os.path.join(folder, name)))
        return original(self, entries, timeout=timeout)

    monkeypatch.setattr(code_git.Clone, "read", read)
    before = len(git_server["log"])
    ctx, out = _ctx(git_server, "edge")
    code_repo.snapshot(ctx, allow_http=True)
    events = _events(out)
    inventory = next(item for item in events if item["event"] == "inventory")
    skipped = {item["object"]: item["reason"] for item in inventory["skipped"]}
    assert skipped == {"sub": "submodule", "link.py": "symlink", "big/huge.py": "too_large"}
    assert seen_symlinks == []
    assert not any(item["path"].startswith("/sub.git") for item in git_server["log"][before:])
    limitations = " / ".join(item["text"] for item in events if item["event"] == "limitation")
    assert "1 MiB" in limitations and "子模块" in limitations and "Python 解析失败：app/broken.py" in limitations
    if importlib.util.find_spec("javalang"):
        assert "Java 解析失败：app/Broken.java" in limitations
    records = [item["record"] for item in events if item["event"] == "record"]
    assert {item["name"] for item in records} >= {"Thing", "run"}
    assert "root:" not in out.getvalue()
    assert events[-1]["complete"] is True


@needs_git
def test_fetch_by_commit(git_server, private_tmp):
    ctx, out = _ctx(git_server, "history", ref=git_server["first"])
    code_repo.snapshot(ctx, allow_http=True)
    events = _events(out)
    artifact = events[-1]["artifact"]
    assert artifact["commit"] == git_server["first"] and artifact["ref"] == git_server["first"]
    names = {item["record"]["name"] for item in events if item["event"] == "record"}
    assert "FirstService" in names and "SecondService" not in names
    ctx, _ = _ctx(git_server, "history", ref="no-such-branch")
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "ref_not_found"


@needs_git
def test_budget_truncation_is_partial(git_server, private_tmp):
    ctx, out = _ctx(git_server, "many", budget={"max_rows": 200000, "max_bytes": 60_000, "max_duration_s": 120})
    code_repo.snapshot(ctx, allow_http=True)
    events = _events(out)
    result = events[-1]
    assert result["event"] == "result" and result["complete"] is False and 0 < result["coverage_pct"] < 100
    inventory = next(item for item in events if item["event"] == "inventory")
    assert any(item["reason"] == "budget" for item in inventory["skipped"])
    assert any("预算" in item["text"] for item in events if item["event"] == "limitation")
    # 记录条数到顶：停下来标不完整，已发的记录保留
    ctx, out = _ctx(git_server, "many", budget={"max_rows": 30, "max_bytes": 64 * 1024 * 1024,
                                                "max_duration_s": 120})
    code_repo.snapshot(ctx, allow_http=True)
    events = _events(out)
    assert sum(1 for item in events if item["event"] == "record") == 30
    assert events[-1]["complete"] is False
    # 仓库本身超过预算：取的时候就停，报错
    ctx, _ = _ctx(git_server, "orders-java", budget={"max_rows": 10, "max_bytes": 4000, "max_duration_s": 120})
    with pytest.raises(ConnError) as caught:
        code_repo.snapshot(ctx, allow_http=True)
    assert caught.value.code == "repo_too_large"
    assert list(private_tmp.iterdir()) == []


@needs_git
def test_connection_test(git_server, private_tmp):
    ctx, out = _ctx(git_server, "orders-ts")
    code_repo.test(ctx, allow_http=True)
    result = _events(out)[-1]
    check = result["checks"][0]
    assert result["complete"] is True and check["name"] == "connectivity" and check["ok"] is True
    assert "只读" in check["detail"] and isinstance(check["latency_ms"], int)
    wrong = secrets.token_hex(16)
    ctx, out = _ctx(git_server, "orders-ts", token=wrong)
    code_repo.test(ctx, allow_http=True)
    result = _events(out)[-1]
    assert result["checks"][0]["ok"] is False and wrong not in out.getvalue()
    ctx, out = _ctx(git_server, "orders-ts", ref="missing")
    code_repo.test(ctx, allow_http=True)
    assert _events(out)[-1]["checks"][0]["ok"] is False


@needs_git
def test_git_environment_is_minimal(git_server, private_tmp, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:3128")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/nonexistent")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    monkeypatch.setenv(code_git.CA_FILE_ENV, "/certs/git.crt")
    token = secrets.token_hex(16)
    target = code_git.parse_url("https://git.example.invalid/team/repo.git")
    with code_git.Workspace(target, token) as workspace:
        env = workspace.env()
        assert "HTTPS_PROXY" not in env and env["HOME"].startswith(str(private_tmp))
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull and "GIT_SSL_NO_VERIFY" not in env
        assert env["GIT_SSL_CAINFO"] == "/certs/git.crt"
        assert all(token not in value for value in env.values())
        command = workspace.command("fetch")
        for item in ("protocol.allow=never", "protocol.https.allow=always", "http.followRedirects=false",
                     "core.hooksPath=/dev/null", "core.symlinks=false", "submodule.recurse=false",
                     "credential.helper=", "core.fsmonitor=false"):
            assert item in command
        assert "protocol.http.allow=always" not in command
        assert all(token not in part for part in command)
        token_file = os.path.join(workspace.root, "token")
        assert os.stat(token_file).st_mode & 0o777 == 0o600
        workspace.drop_token()
        assert not os.path.exists(token_file) and "GIT_ASKPASS" not in workspace.env()
        assert workspace.clean(f"fatal: https://x-access-token:{token}@host/a.git failed") == \
            "fatal: https://host/a.git failed"
    assert list(private_tmp.iterdir()) == []
