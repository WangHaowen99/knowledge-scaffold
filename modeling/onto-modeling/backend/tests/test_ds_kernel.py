"""ADR-0103 数据源热加载 · 内核：DS-01～DS-04、采集走资料转换队列、文本版与表登记、凭据不落内核、删除连带删凭据、
多源连接器的信任与网关对账（PL-28、PL-29）、内核条件 data_sources。

网关是真的（测试进程里起 HTTP 服务、插件仓库与凭据库在临时目录），连接器是只用标准库的假插件。
"""

from __future__ import annotations

import json
import shutil
import textwrap
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.connector_gateway import egress, plugins, server
from app.connector_gateway.source import runner as source_runner
from app.connector_gateway.vault import Vault
from app.main import app
from app.services import auth, datasources, material_conversion, streams
from app.services.flow import conditions
from app.services.material_conversion import gateway
from app.services.workspace import WorkspaceManager

MANIFEST = """\
name: {name}
version: {version}
kind: provider
category: provider
label: 假数据库
description: 测试用
publisher: {{kind: {publisher}, label: ""}}
kernel_api: ">=1.0 <2.0"
engines: [claude, jiuwen]
depends: {{}}
source:
  entry: connector.py
  license: 无
  fields:
    - {{key: host, label: 主机, type: host, required: true}}
    - {{key: port, label: 端口, type: port, default: 5432}}
    - {{key: user, label: 账号, type: text, required: true}}
    - {{key: password, label: 密码, type: secret, required: true}}
"""

CONNECTOR = '''
from app.connector_gateway.source.sdk import SourceError

def test(ctx):
    ctx.check("连接", True, "")
    ctx.check("只读", ctx.secret.get("password") == "pw-1", "")

def collect(ctx):
    if ctx.secret.get("password") != "pw-1":
        raise SourceError("auth_failed", "口令不对")
    suffix = ctx.config.get("user")
    ctx.table("orders", [{"name": "id", "type": "bigint", "key": "pk"},
                         {"name": "customer_id", "type": "bigint", "key": "fk", "comment": "客户"},
                         {"name": "status", "type": "varchar(8)"}],
              [[i, i % 3, "PAID" if i % 2 else "NEW"] for i in range(1, 30)], comment="订单")
    ctx.table("customer", [{"name": "id", "type": "bigint", "key": "pk"}, {"name": "level_" + suffix, "type": "int"}],
              [[1, 1], [2, 2]])
    ctx.fact("foreign_key", table="orders", columns=["customer_id"], ref_table="customer", ref_columns=["id"],
             name="fk_orders_customer")
'''


def _plugin(root: Path, name="fake-db", version="1.0.0", publisher="official") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(MANIFEST.format(name=name, version=version, publisher=publisher),
                                        encoding="utf-8")
    (root / "connector.py").write_text(textwrap.dedent(CONNECTOR), encoding="utf-8")
    return root


class GatewayServer:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.httpd = None
        self.port = 0
        self.store = plugins.Store(tmp / "builtin", tmp / "plugins")
        self.vault = Vault(tmp / "secrets")

    def start(self):
        instance = server.Gateway(None, server.KeySource(url="test", fetch=lambda: gateway.issuer().public_jwk()),
                                  store=self.store, vault=self.vault,
                                  source_limits=source_runner.SourceLimits(timeout_s=20))
        self.httpd = server.serve(self.port, instance, host="127.0.0.1")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "SECRETS_DIR", "")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(material_conversion, "start", lambda: None)
    monkeypatch.setattr(egress, "_addresses", lambda host: {"192.0.2.10"} if host.endswith(".invalid") else set())
    material_conversion.reset()
    gateway.reset()
    _plugin(tmp_path / "gw" / "builtin" / "fake-db")
    gw = GatewayServer(tmp_path / "gw").start()
    monkeypatch.setenv(gateway.URL_ENV, gw.url)
    project_id = workspaces.create_project("数据源", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, workspaces, project_id, workspaces.project_root(project_id), gw
    gw.stop()
    material_conversion.reset()
    gateway.reset()
    streams.reset_all()


CONFIG = {"host": "db.example.invalid", "user": "reader"}


def _create(client, pid, name="订单库", config=CONFIG, secrets=None):
    return client.post(f"/api/v1/projects/{pid}/sources", json={
        "name": name, "connector": "fake-db", "config": config, "secrets": secrets or {"password": "pw-1"}})


def _materials(client, pid):
    groups = client.get(f"/api/v1/projects/{pid}/resources").json()["groups"]
    return {item["path"]: item for group in groups for item in group["items"]}


def test_connectors_and_test_connection(env):
    client, _ws, pid, _root, gw = env
    body = client.get("/api/v1/source-connectors").json()
    assert body["gateway"]["status"] == "ok" and [item["id"] for item in body["items"]] == ["fake-db"]
    assert body["items"][0]["fields"][3] == {"key": "password", "label": "密码", "type": "secret", "required": True,
                                             "default": None, "placeholder": "", "options": []}
    ok = client.post(f"/api/v1/projects/{pid}/sources/test",
                     json={"connector": "fake-db", "config": CONFIG, "secrets": {"password": "pw-1"}}).json()
    assert ok["ok"] is True and len(ok["checks"]) == 2 and ok["error"] is None
    bad = client.post(f"/api/v1/projects/{pid}/sources/test",
                      json={"connector": "fake-db", "config": CONFIG, "secrets": {"password": "x"}}).json()
    assert bad["ok"] is False
    missing = client.post(f"/api/v1/projects/{pid}/sources/test", json={"connector": "fake-db", "config": {}})
    assert missing.status_code == 400 and missing.json()["details"]["fields"] == {
        "host": "required", "user": "required", "password": "required"}
    internal = client.post(f"/api/v1/projects/{pid}/sources/test", json={
        "connector": "fake-db", "config": {**CONFIG, "host": "127.0.0.1"}, "secrets": {"password": "pw-1"}}).json()
    assert internal["ok"] is False and internal["error"]["code"] == "egress_forbidden"
    assert client.post(f"/api/v1/projects/{pid}/sources/test",
                       json={"connector": "nope", "config": CONFIG}).status_code == 404
    gw.stop()
    gateway.reset()
    assert client.get("/api/v1/source-connectors").json() == {"gateway": {"status": "down"}, "items": []}
    assert client.post(f"/api/v1/projects/{pid}/sources/test",
                       json={"connector": "fake-db", "config": CONFIG}).status_code == 503


def test_create_collects_into_text_version(env):
    client, workspaces, pid, root, gw = env
    response = _create(client, pid)
    assert response.status_code == 201, response.text
    material = response.json()["material"]
    rel = "resource/excel/订单库.datasource"
    assert material["path"] == rel and material["kind"] == "source" and material["type_label"] == "数据源"
    assert material["source"]["config"] == {"host": "db.example.invalid", "port": 5432, "user": "reader"}
    assert material["source"]["secret_fields"] == ["password"] and material["source"]["connector"]["available"]
    assert material["purpose"] == "schema"
    # 凭据不在资料里、不在内核数据目录里
    assert b"pw-1" not in (root / rel).read_bytes()
    assert not any(b"pw-1" in path.read_bytes() for path in (root.parent.parent).rglob("*") if path.is_file()
                   and path.stat().st_size < 1_000_000)
    assert material_conversion.drain() >= 1
    text = (root / "resource/text/excel/订单库.datasource.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- onto:text source=\"订单库.datasource\"") and "converter=\"fake-db@1.0.0\"" in text
    assert "<!-- 工作表：orders -->" in text and "## 外键" in text
    item = _materials(client, pid)[rel]
    assert item["text"]["state"] == "converted" and item["sheets"] == 2
    scope = item["schema_scope"]
    assert scope and sorted(scope["included_sheets"]) == ["customer", "orders"]
    assert conditions.holds("data_sources", root)
    # 重名
    assert _create(client, pid).status_code == 409
    # 密钥放进 config 一律拒绝
    leaked = _create(client, pid, name="另一个", config={**CONFIG, "password": "pw-1"})
    assert leaked.status_code == 400 and leaked.json()["details"]["fields"] == {"password": "invalid"}


def test_update_recollects_and_keeps_secret(env):
    client, _ws, pid, root, gw = env
    _create(client, pid)
    material_conversion.drain()
    rel = "resource/excel/订单库.datasource"
    response = client.patch(f"/api/v1/projects/{pid}/sources", json={"path": rel, "config": {**CONFIG, "user": "r2"}})
    assert response.status_code == 200, response.text
    material_conversion.drain()
    text = (root / "resource/text/excel/订单库.datasource.md").read_text(encoding="utf-8")
    assert "level_r2" in text
    # 带 path 测试用已存的凭据
    assert client.post(f"/api/v1/projects/{pid}/sources/test", json={"path": rel}).json()["ok"] is True
    # 换错密码：重新采集失败，原因是人话
    client.patch(f"/api/v1/projects/{pid}/sources", json={"path": rel, "secrets": {"password": "bad"}})
    material_conversion.drain()
    item = _materials(client, pid)[rel]
    assert item["text"]["state"] == "failed" and item["text"]["reason"] == "账号或密码不对"
    # 重新转换就是重新采集
    client.patch(f"/api/v1/projects/{pid}/sources", json={"path": rel, "secrets": {"password": "pw-1"}})
    material_conversion.drain()
    assert client.post(f"/api/v1/projects/{pid}/resources/convert", json={"path": rel}).status_code == 202
    material_conversion.drain()
    assert _materials(client, pid)[rel]["text"]["state"] == "converted"


def test_delete_removes_secret_and_copied_source_needs_password(env):
    client, _ws, pid, root, gw = env
    _create(client, pid)
    material_conversion.drain()
    rel = "resource/excel/订单库.datasource"
    secret_files = list((gw.tmp / "secrets" / pid).glob("*.bin"))
    assert len(secret_files) == 1
    assert client.delete(f"/api/v1/projects/{pid}/resources", params={"path": rel}).status_code == 200
    assert not secret_files[0].exists()
    assert not conditions.holds("data_sources", root)


def test_waiting_when_gateway_down_then_collects(env):
    client, _ws, pid, root, gw = env
    _create(client, pid)
    gw.stop()
    gateway.reset()
    material_conversion.drain()
    rel = "resource/excel/订单库.datasource"
    assert _materials(client, pid)[rel]["text"]["state"] == "waiting"
    gw.start()
    gateway.reset()
    material_conversion.retry_waiting()
    material_conversion.drain()
    assert _materials(client, pid)[rel]["text"]["state"] == "converted"


def test_trust_hot_loads_uploaded_connector(env, tmp_path):
    client, workspaces, pid, _root, gw = env
    repository = workspaces.packs.repository
    workspaces.packs.core_version()
    source = _plugin(tmp_path / "upload" / "tenant-db", name="tenant-db", version="0.2.0", publisher="tenant")
    from app.services.packs.repository import write_checksums

    write_checksums(source)
    # 用户上传、装到自己名下的私有包（PL-26）：对账时也要算它，不能当成没信任删掉
    repository.claim("tenant-db", "local")
    repository.install(source, origin="upload", by="local")
    repository.set_channel("tenant-db", "stable", "0.2.0", by="local")
    version = repository.latest("tenant-db", "stable")
    card = client.get("/api/v1/packs", params={"category": "provider"}).json()
    tenant = next(item for item in card["items"] if item["name"] == "tenant-db")
    assert tenant["source"] == {"fields": 4, "license": "无"} and tenant["trust"]["state"] == "untrusted"
    assert [item["id"] for item in client.get("/api/v1/source-connectors").json()["items"]] == ["fake-db"]

    trusted = client.post(f"/api/v1/admin/packs/tenant-db/versions/{version}/trust")
    assert trusted.status_code == 200, trusted.text
    assert trusted.json()["trust"] == {"state": "trusted", "installed": True}
    # 信任后触发的对账跑完，插件还在
    assert datasources.wait_reconciled()
    assert any(p.name == "tenant-db" for p in gw.store.plugins())
    gateway.reset()
    assert sorted(item["id"] for item in client.get("/api/v1/source-connectors").json()["items"]) == [
        "fake-db", "tenant-db"]
    # 网关插件卷丢了：下一次刷新清单时对账补上
    shutil.rmtree(gw.tmp / "plugins" / "tenant-db")
    gw.store.reload()
    gateway.reset()
    gateway.catalog(force=True)
    assert datasources.wait_reconciled()
    assert any(p.name == "tenant-db" for p in gw.store.plugins())

    untrusted = client.delete(f"/api/v1/admin/packs/tenant-db/versions/{version}/trust")
    assert untrusted.status_code == 200 and untrusted.json()["trust"]["state"] == "untrusted"
    assert datasources.wait_reconciled()
    assert not any(p.name == "tenant-db" for p in gw.store.plugins())
    assert client.post("/api/v1/admin/packs/fake-db/versions/1.0.0/trust").status_code in (400, 404)


def test_pack_tar_is_deterministic(tmp_path):
    folder = _plugin(tmp_path / "p")
    first = datasources.pack_tar(folder)
    (folder / "connector.py").touch()
    assert datasources.pack_tar(folder) == first
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert datasources.pack_tar(folder) == first


def test_create_collects_once_and_password_change_recollects(env, monkeypatch):
    """接入只采一次（写入事件排的那次）；只改密码时资料内容不变，也要重采一次。"""
    client, _ws, pid, _root, _gw = env
    calls = []
    original = datasources.collect
    monkeypatch.setattr(datasources, "collect", lambda project_id, data: calls.append(1) or original(project_id, data))
    _create(client, pid)
    material_conversion.drain()
    assert len(calls) == 1
    rel = "resource/excel/订单库.datasource"
    client.patch(f"/api/v1/projects/{pid}/sources", json={"path": rel, "secrets": {"password": "pw-1"}})
    material_conversion.drain()
    assert len(calls) == 2
