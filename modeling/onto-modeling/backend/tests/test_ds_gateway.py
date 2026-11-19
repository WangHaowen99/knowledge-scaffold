"""ADR-0103 数据源热加载 · 网关：连接字段、SDK 与子进程、遮蔽与渲染、插件仓库、凭据库、HTTP 接口。

连接器用测试里现写的假插件（只用标准库），不依赖数据库驱动；真库的测试在 test_ds_db_connectors.py。
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import tarfile
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.connector_gateway import egress, plugins, server
from app.connector_gateway.errors import GatewayError
from app.connector_gateway.source import render, runner
from app.connector_gateway.vault import Vault
from app.evidence import sources
from app.evidence.tickets import Issuer, sha256_hex

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

MANIFEST = """\
name: {name}
version: {version}
kind: provider
category: provider
label: 假数据库
description: 测试用
publisher: {{kind: tenant, label: ""}}
kernel_api: ">=1.0 <2.0"
source:
  entry: connector.py
  license: 无
  fields:
    - {{key: host, label: 主机, type: host, required: true}}
    - {{key: port, label: 端口, type: port, default: 5432}}
    - {{key: user, label: 账号, type: text, required: true}}
    - {{key: password, label: 密码, type: secret, required: true}}
    - {{key: tables, label: 表范围, type: text}}
"""

CONNECTOR = '''
import os, socket, subprocess
from app.connector_gateway.source.sdk import SourceError

def test(ctx):
    ctx.check("连接", True, ctx.config["host"])
    ctx.check("口令", ctx.secret.get("password") == "pw-1234", "")

def collect(ctx):
    mode = ctx.config.get("user")
    if mode == "boom":
        raise SourceError("auth_failed", "账号 reader 口令 " + ctx.secret["password"] + " 不对")
    if mode == "crash":
        os._exit(3)
    if mode == "dial":
        try:
            socket.create_connection(("192.0.2.10", 3306), timeout=1)
        except Exception:
            pass
    if mode == "spawn":
        subprocess.run(["/bin/echo", "x"])
    print("插件自己的输出，不该混进事件流")
    rows = [[i, "C%d" % (i % 3), "PAID" if i % 2 else "NEW", "1380000%04d" % i, i * 1000] for i in range(1, 80)]
    ctx.table("orders", [
        {"name": "id", "type": "bigint", "key": "pk", "nullable": False, "comment": "订单号"},
        {"name": "customer_id", "type": "varchar(32)", "key": "fk"},
        {"name": "status", "type": "varchar(8)", "comment": "状态"},
        {"name": "mobile", "type": "varchar(20)"},
        {"name": "amount", "type": "decimal(12,2)"},
    ], rows, comment="订单")
    ctx.table("customer", [{"name": "id", "type": "varchar(32)", "key": "pk"},
                           {"name": "contact_name", "type": "varchar(32)"}], [["C1", "王小明"], [None, None]])
    ctx.fact("foreign_key", table="orders", columns=["customer_id"], ref_table="customer", ref_columns=["id"],
             name="fk_orders_customer")
    ctx.fact("check", table="orders", name="ck_amount", expression="amount >= 0")
    ctx.limitation("测试局限")
'''


def _info(**overrides) -> sources.SourceInfo:
    import yaml

    data = yaml.safe_load(MANIFEST.format(name="fake-db", version="0.1.0"))
    data.update(overrides)
    return sources.parse_spec(data)


def _write_plugin(root: Path, name="fake-db", version="0.1.0", code=CONNECTOR) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.yaml").write_text(MANIFEST.format(name=name, version=version), encoding="utf-8")
    (root / "connector.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return root


def _tar(directory: Path, *, checksums=True, extra: dict | None = None) -> bytes:
    files = {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}
    if checksums:
        files["checksums.sha256"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {rel}\n"
                                            for rel, data in sorted(files.items())).encode()
    files.update(extra or {})
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for rel, data in sorted(files.items()):
            member = tarfile.TarInfo(rel)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


# ---- 清单与连接字段 ----


def test_parse_spec_and_field_rules():
    info = _info()
    assert info.secret_keys == ("password",) and [f.key for f in info.fields][:2] == ["host", "port"]
    with pytest.raises(sources.SpecError):
        _info(converter={"entry": "x.py"})
    with pytest.raises(sources.SpecError, match="host"):
        _info(source={"entry": "c.py", "fields": [{"key": "a", "label": "a", "type": "host"},
                                                 {"key": "b", "label": "b", "type": "host"}]})
    with pytest.raises(sources.SpecError, match="default"):
        _info(source={"entry": "c.py", "fields": [{"key": "p", "label": "p", "type": "secret", "default": "x"}]})
    with pytest.raises(sources.SpecError, match="options"):
        _info(source={"entry": "c.py", "fields": [{"key": "m", "label": "m", "type": "select"}]})


def test_coerce_config_and_egress():
    info = _info()
    config, secrets, errors = sources.coerce_config(info, {"host": "db.example.invalid", "port": "3307", "user": "r"},
                                                    {"password": "x"})
    assert errors == {} and config == {"host": "db.example.invalid", "port": 3307, "user": "r", "tables": ""}
    assert secrets == {"password": "x"}
    assert sources.egress(info, config) == [{"host": "db.example.invalid", "port": 3307}]
    # 缺省值、必填、非法取值、密钥放错地方、不认识的键
    config, _, errors = sources.coerce_config(info, {"host": "h", "user": "u", "password": "x", "zzz": 1}, {})
    assert config["port"] == 5432 and errors == {"password": "invalid", "zzz": "invalid"}
    _, _, errors = sources.coerce_config(info, {"host": "a b", "port": 70000, "user": ""}, {})
    assert errors == {"host": "invalid", "port": "invalid", "user": "required", "password": "required"}
    # 已存过的密钥字段没给不算缺
    _, _, errors = sources.coerce_config(info, {"host": "h", "user": "u"}, {}, stored_secrets=("password",))
    assert errors == {}


def test_datasource_document_roundtrip():
    data = sources.dump_document("fake-db", {"host": "h"}, "ds-0123456789ab", ["password"])
    assert b"pw" not in data
    assert sources.load_document(data) == {"connector": "fake-db", "config": {"host": "h"},
                                           "secret": "ds-0123456789ab", "secret_fields": ["password"]}
    assert sources.load_document(b'{"format": "x"}') is None
    assert sources.load_document(data.replace(b"ds-0123456789ab", b"../../etc"))["secret"] == ""


# ---- 子进程、遮蔽、渲染 ----


LIMITS = runner.SourceLimits(timeout_s=20)


def _run(tmp_path, operation, config=None, secret=None, egress_list=(), code=CONNECTOR):
    plugin = _write_plugin(tmp_path / "plugin", code=code)
    return runner.run(plugin / "connector.py", None, operation, config or {"user": "ok"}, secret or {},
                      list(egress_list), LIMITS)


def test_runner_test_and_collect(tmp_path):
    outcome = _run(tmp_path, "test", {"host": "h", "user": "ok"}, {"password": "pw-1234"})
    assert outcome.ok and [c["ok"] for c in outcome.checks] == [True, True]
    outcome = _run(tmp_path, "collect")
    assert outcome.ok and [t["name"] for t in outcome.collected.tables] == ["orders", "customer"]
    assert len(outcome.collected.tables[0]["rows"]) == 79 and outcome.collected.limitations == ["测试局限"]


def test_runner_scrubs_secret_and_reports_errors(tmp_path):
    outcome = _run(tmp_path, "collect", {"user": "boom"}, {"password": "pw-1234"})
    assert not outcome.ok and outcome.error_code == "auth_failed"
    assert "pw-1234" not in outcome.error_message and "«已遮蔽»" in outcome.error_message
    assert _run(tmp_path, "collect", {"user": "crash"}).error_code == "crashed"


def test_runner_guard_blocks_egress_and_subprocess(tmp_path):
    # 插件吞了异常也算失败
    assert _run(tmp_path, "collect", {"user": "dial"}).error_code == "egress_denied"
    assert _run(tmp_path, "collect", {"user": "spawn"}).error_code == "exec_denied"


def test_runner_timeout(tmp_path):
    slow = "import time\ndef collect(ctx):\n    time.sleep(30)\n"
    plugin = _write_plugin(tmp_path / "slow", code=slow)
    outcome = runner.run(plugin / "connector.py", None, "collect", {}, {}, [], runner.SourceLimits(timeout_s=1))
    assert outcome.error_code == "timeout"


def test_render_masks_and_formats(tmp_path):
    outcome = _run(tmp_path, "collect")
    markdown, stats, limitations = render.render(outcome.collected, "salt-a")
    assert stats == {"tables": 2, "facts": 3, "rows": 22} and limitations == ["测试局限"]
    assert "<!-- 工作表：orders -->\n## orders\n\n| id | customer_id | status | mobile | amount |" in markdown
    assert "13800000001" not in markdown and "138****0001" in markdown
    assert "王小明" not in markdown and "王**" in markdown
    # 关联键哈希：orders.customer_id 与 customer.id 的同一个值哈希相同
    facts = render.parse_facts(markdown)
    assert facts["foreign_keys"][0]["ref_table"] == "customer"
    assert {"table": "orders", "column": "status", "values": ["PAID", "NEW"]} in facts["values"]
    assert facts["tables"]["orders"]["columns"][3]["sensitivity"] == "phone"
    orders_lines = markdown.split("<!-- 工作表：customer -->")[0].splitlines()
    c1 = next(line for line in orders_lines if line.startswith("| ") and "PAID" in line).split(" | ")[1]
    assert c1 in markdown.split("<!-- 工作表：customer -->")[1]
    # 每表最多 20 行样本；全空的行也留着
    assert markdown.count("\n| ") >= 20 and "|  |  |" in markdown
    # 同一项目的盐换了，哈希就变
    assert render.render(outcome.collected, "salt-b")[0] != markdown
    assert "<!-- onto:anchors 1\n1 sheet:orders\n4 row:1\n6 row:2" in markdown
    assert "## 检查约束" in markdown and "## 码值" in markdown


def test_render_limits():
    collected = render.Collected(tables=[{"name": f"t{i}", "comment": "", "columns": [{"name": "a", "type": "int"}],
                                          "rows": [["x" * 500]]} for i in range(render.MAX_TABLES + 3)])
    markdown, stats, limitations = render.render(collected, "s")
    assert stats["tables"] == render.MAX_TABLES and any("前 500 张表" in item for item in limitations)
    assert "x" * 201 not in markdown


def test_table_sources_reads_rendered_text(tmp_path):
    """渲染结果按 Excel 文本版的规则能读出工作表和首行（发布绑定、表登记都靠它）。"""
    from app.services.table_sources import rules

    outcome = _run(tmp_path, "collect")
    markdown, _, _ = render.render(outcome.collected, "s")
    sheets = rules.text_sheets(markdown)
    assert [sheet["name"] for sheet in sheets] == ["orders", "customer"]
    assert sheets[0]["rows"][0] == ["id", "customer_id", "status", "mobile", "amount"]
    assert rules.classify_sheet("orders", sheets[0]["rows"]) == rules.TABLE


# ---- 出口检查 ----


def test_egress_forbids_internal_targets(monkeypatch):
    fake = {"db.example.invalid": {"192.0.2.10"}, "meta.example.invalid": {"169.254.169.254"},
            "onto-modeling": {"172.30.0.5"}, "sneaky.example.invalid": {"172.30.0.5"}}
    resolve = lambda host: fake.get(host, set())  # noqa: E731
    egress.ensure_allowed([{"host": "db.example.invalid", "port": 3306}], resolve=resolve)
    egress.ensure_allowed([{"host": "10.1.2.3", "port": 3306}], resolve=resolve)
    for host in ("127.0.0.1", "localhost", "::1", "169.254.169.254", "onto-modeling", "meta.example.invalid",
                 "sneaky.example.invalid", "0.0.0.0"):
        with pytest.raises(GatewayError) as caught:
            egress.ensure_allowed([{"host": host, "port": 1}], resolve=resolve)
        assert caught.value.code == "egress_forbidden"


# ---- 插件仓库 ----


def test_store_builtin_install_remove_and_versions(tmp_path):
    builtin = tmp_path / "builtin"
    _write_plugin(builtin / "fake-db", version="0.1.0")
    store = plugins.Store(builtin, tmp_path / "vol")
    assert store.sources()["fake-db"].info.version == "0.1.0" and store.sources()["fake-db"].info.official
    source = _write_plugin(tmp_path / "src", version="0.2.0")
    data = _tar(source)
    plugin = store.install("fake-db", "0.2.0", data, sha256_hex(data))
    assert plugin.origin == "installed" and store.sources()["fake-db"].info.version == "0.2.0"
    assert not store.sources()["fake-db"].info.official
    # 同一份再装一次幂等；另一份同版本冲突
    store.install("fake-db", "0.2.0", data, sha256_hex(data))
    other = _tar(source, extra={"README.md": b"x"}, checksums=False)
    with pytest.raises(GatewayError) as caught:
        store.install("fake-db", "0.2.0", other, sha256_hex(other))
    assert caught.value.code == "plugin_conflict"
    # 重启后还在（插件卷）
    assert plugins.Store(builtin, tmp_path / "vol").sources()["fake-db"].info.version == "0.2.0"
    assert store.remove("fake-db", "0.2.0") and store.sources()["fake-db"].info.version == "0.1.0"
    with pytest.raises(GatewayError) as caught:
        store.remove("fake-db", "0.1.0")
    assert caught.value.code == "plugin_builtin"


def test_version_key_orders_semver():
    assert plugins.version_key("1.10.0") > plugins.version_key("1.9.3") > plugins.version_key("1.9.3-rc1")


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: _tar(d, extra={"../evil.py": b"x"}), "路径"),
    (lambda d: _tar(d, extra={"/abs.py": b"x"}), "路径"),
    (lambda d: _tar(d, extra={"connector.py": b"tampered"}, checksums=True) if False else
     _tar_with_bad_checksum(d), "校验和"),
    (lambda d: _symlink_tar(d), "文件类型"),
    (lambda d: b"not a tar", "tar"),
])
def test_store_rejects_bad_packages(tmp_path, mutate, needle):
    store = plugins.Store(tmp_path / "none", tmp_path / "vol")
    data = mutate(_write_plugin(tmp_path / "src", version="0.3.0"))
    with pytest.raises(GatewayError) as caught:
        store.install("fake-db", "0.3.0", data, sha256_hex(data))
    assert caught.value.code == "plugin_invalid" and needle in caught.value.message
    assert not (tmp_path / "vol" / "fake-db" / "0.3.0").exists()
    assert [p.name for p in (tmp_path / "vol").iterdir()] == []


def test_store_rejects_name_or_version_mismatch(tmp_path):
    store = plugins.Store(tmp_path / "none", tmp_path / "vol")
    data = _tar(_write_plugin(tmp_path / "src", version="0.3.0"))
    with pytest.raises(GatewayError, match="version"):
        store.install("fake-db", "0.4.0", data, sha256_hex(data))
    with pytest.raises(GatewayError, match="name"):
        store.install("other-db", "0.3.0", data, sha256_hex(data))


def _tar_with_bad_checksum(directory: Path) -> bytes:
    good = _tar(directory)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(good), mode="r:") as src, tarfile.open(fileobj=buffer, mode="w:") as dst:
        for member in src.getmembers():
            data = src.extractfile(member).read()
            if member.name == "connector.py":
                data += b"\n# tampered\n"
                member.size = len(data)
            dst.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def _symlink_tar(directory: Path) -> bytes:
    buffer = io.BytesIO(_tar(directory, checksums=False))
    with tarfile.open(fileobj=buffer, mode="a:") as archive:
        link = tarfile.TarInfo("link.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    return buffer.getvalue()


# ---- 凭据库 ----


def test_vault_merge_bind_and_generated_key(tmp_path):
    vault = Vault(tmp_path / "secrets")
    assert vault.put("pj_1", "ds-0123456789ab", {"password": "a"}) == ["password"]
    assert vault.put("pj_1", "ds-0123456789ab", {"token": "t"}) == ["password", "token"]
    assert vault.read("pj_1", "ds-0123456789ab") == {"password": "a", "token": "t"}
    assert (tmp_path / "secrets" / ".master.key").stat().st_mode & 0o777 == 0o600
    raw = (tmp_path / "secrets" / "pj_1" / "ds-0123456789ab.bin").read_bytes()
    assert b"password" not in raw
    # 挪到别的项目下解不开
    (tmp_path / "secrets" / "pj_2").mkdir()
    (tmp_path / "secrets" / "pj_2" / "ds-0123456789ab.bin").write_bytes(raw)
    with pytest.raises(GatewayError) as caught:
        vault.read("pj_2", "ds-0123456789ab")
    assert caught.value.code == "secret_undecryptable"
    assert vault.mask_salt("pj_1") == Vault(tmp_path / "secrets").mask_salt("pj_1") != vault.mask_salt("pj_2")
    assert vault.delete("pj_1", "ds-0123456789ab") and vault.read("pj_1", "ds-0123456789ab") is None
    with pytest.raises(GatewayError):
        vault.put("../x", "ds-0123456789ab", {"password": "a"})


# ---- HTTP ----


@pytest.fixture()
def gw(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "_addresses", lambda host: {"192.0.2.10"} if host.endswith(".invalid") else set())
    issuer = Issuer(tmp_path / "issuer.pem")
    store = plugins.Store(tmp_path / "builtin", tmp_path / "vol")
    instance = server.Gateway(None, server.KeySource(url="test", fetch=issuer.public_jwk), store=store,
                              vault=Vault(tmp_path / "secrets"), max_concurrent=1, max_waiting=0,
                              source_limits=runner.SourceLimits(timeout_s=20))
    httpd = server.serve(0, instance, host="127.0.0.1")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", issuer, tmp_path
    httpd.shutdown()


def _call(url, method, path, data=b"", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(f"{url}{path}", data=data or None, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _token(issuer, op, scope_bytes, **claims):
    return issuer.issue(job_id="j", operation=op, scope_hash=sha256_hex(scope_bytes), budget={}, claims=claims)


def test_http_hot_load_secret_test_collect(gw):
    url, issuer, tmp = gw
    assert _call(url, "GET", "/v1/providers")[1]["sources"] == []
    data = _tar(_write_plugin(tmp / "src", version="0.1.0"))
    # 票据要写明是哪个插件
    status, body = _call(url, "PUT", "/v1/plugins/fake-db/0.1.0", data, _token(issuer, "plugin_install", data))
    assert status == 401
    token = _token(issuer, "plugin_install", data, plugin="fake-db@0.1.0")
    status, body = _call(url, "PUT", "/v1/plugins/fake-db/0.1.0", data, token)
    assert status == 200 and body["plugin"]["sha256"] == sha256_hex(data)
    providers = _call(url, "GET", "/v1/providers")[1]
    assert providers["sources"][0]["id"] == "fake-db" and providers["plugins"][0]["origin"] == "installed"

    secret_body = json.dumps({"fields": {"password": "pw-1234"}}).encode()
    status, body = _call(url, "PUT", "/v1/secrets/pj_1/ds-0123456789ab", secret_body,
                         _token(issuer, "secret", secret_body, project="pj_1"))
    assert status == 200 and body == {"secret_fields": ["password"]}
    # 别的项目的票据用不了
    status, _ = _call(url, "PUT", "/v1/secrets/pj_1/ds-0123456789ab", secret_body,
                      _token(issuer, "secret", secret_body, project="pj_2"))
    assert status == 401

    config = {"host": "db.example.invalid", "user": "ok"}
    test_body = json.dumps({"project": "pj_1", "connector": "fake-db", "config": config,
                            "secret": "ds-0123456789ab"}).encode()
    status, body = _call(url, "POST", "/v1/sources/test", test_body,
                         _token(issuer, "source_test", test_body, project="pj_1"))
    assert status == 200 and body["ok"] and len(body["checks"]) == 2
    # 这次填的密码覆盖已存的，不保存
    wrong = json.dumps({"project": "pj_1", "connector": "fake-db", "config": config, "secret": "ds-0123456789ab",
                        "secrets": {"password": "nope"}}).encode()
    status, body = _call(url, "POST", "/v1/sources/test", wrong, _token(issuer, "source_test", wrong, project="pj_1"))
    assert status == 200 and body["ok"] is False

    collect_body = test_body
    status, body = _call(url, "POST", "/v1/sources/collect", collect_body,
                         _token(issuer, "source_collect", collect_body, project="pj_1"))
    assert status == 200 and body["state"] == "converted" and body["stats"]["tables"] == 2
    assert "<!-- 工作表：orders -->" in body["markdown"] and "pw-1234" not in body["markdown"]

    # 别的项目拿这个引用取不到凭据
    other = json.dumps({"project": "pj_2", "connector": "fake-db", "config": config,
                        "secret": "ds-0123456789ab"}).encode()
    status, body = _call(url, "POST", "/v1/sources/collect", other, _token(issuer, "source_collect", other, project="pj_2"))
    assert status == 409 and body["error"] == "secret_missing"

    # 连接地址不能是内部服务
    local = json.dumps({"project": "pj_1", "connector": "fake-db", "config": {**config, "host": "127.0.0.1"},
                        "secret": "ds-0123456789ab"}).encode()
    status, body = _call(url, "POST", "/v1/sources/test", local, _token(issuer, "source_test", local, project="pj_1"))
    assert status == 400 and body["error"] == "egress_forbidden"

    # 删掉插件后连接器不在了
    scope = b"fake-db@0.1.0"
    assert _call(url, "DELETE", "/v1/plugins/fake-db/0.1.0", token=_token(issuer, "plugin_remove", scope))[0] == 200
    status, body = _call(url, "POST", "/v1/sources/test", test_body, _token(issuer, "source_test", test_body,
                                                                           project="pj_1"))
    assert status == 404 and body["error"] == "connector_not_found"
    status, body = _call(url, "DELETE", "/v1/secrets/pj_1/ds-0123456789ab",
                         token=_token(issuer, "secret", b"delete:pj_1/ds-0123456789ab", project="pj_1"))
    assert status == 200 and body == {"deleted": True}


def test_http_rejects_bad_requests(gw):
    url, issuer, _ = gw
    assert _call(url, "POST", "/v1/sources/test", b"{}")[0] == 401
    body = b"not json"
    status, payload = _call(url, "POST", "/v1/sources/test", body, _token(issuer, "source_test", body, project="p"))
    assert status == 400 and payload["error"] == "bad_request"
    body = json.dumps({"project": "p", "connector": "nope", "config": {}}).encode()
    status, payload = _call(url, "POST", "/v1/sources/test", body, _token(issuer, "source_test", body, project="p"))
    assert status == 404 and payload["error"] == "connector_not_found"
