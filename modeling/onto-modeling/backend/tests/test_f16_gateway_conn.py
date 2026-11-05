"""F16 连接型提供方的网关基础设施：凭据库、子进程运行器、审计钩子、任务说明核对、测试连接、快照包（证据采集接口 §8；ADR-0086）。

提供方用测试专用的假模块 `tests/f16_conn_fake.py`（临时加进 `catalog.PROVIDERS`），子进程是真起的；票据用真的 Issuer。
密码是每次运行随机生成的合成值，连接目标只用本机和保留网段。
"""

import base64
import hashlib
import io
import json
import logging
import os
import secrets as pysecrets
import socket
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.connector_gateway import bundle, jobs, providers, server
from app.connector_gateway.conn import catalog
from app.connector_gateway.conn.runner import SubprocessProvider
from app.connector_gateway.conn.worker import MASK
from app.connector_gateway.secrets import SecretStore
from app.evidence import tickets
from app.evidence.spec import encode_job_header, job_scope, record_hash, scope_hash
from app.evidence.tickets import Issuer

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SOURCE = "f16-demo-db"
PASSWORD = "pw-" + pysecrets.token_urlsafe(18)
FAKES = ("f16-fake", "f16-fake-exec", "f16-fake-code")


def _key() -> bytes:
    return base64.b64encode(os.urandom(32)) + b"\n"


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    base = {**catalog._COMMON, "label": "合成数据库", "kind": "db", "scheme": "db", "module": "tests.f16_conn_fake",
            "evidence_types": ["entity", "field"], "capabilities": ["discover", "snapshot", "sample"],
            "cred_fields": [{"name": "password", "label": "密码", "secret": True}], "readonly_method": "合成",
            "default_port": 3306, "anchor_rule": "db:<源>/<库>.<表>", "license_note": "", "drivers": [],
            "executables": []}
    monkeypatch.setitem(catalog.PROVIDERS, "f16-fake", {**base, "id": "f16-fake"})
    monkeypatch.setitem(catalog.PROVIDERS, "f16-fake-exec", {**base, "id": "f16-fake-exec",
                                                             "executables": ["sleep", "git"]})
    monkeypatch.setitem(catalog.PROVIDERS, "f16-fake-code", {**base, "id": "f16-fake-code", "kind": "code",
                                                             "scheme": "code", "capabilities": ["snapshot"],
                                                             "default_port": 443, "executables": ["git"]})


class Env:
    def __init__(self, tmp_path, *, key=True):
        self.tmp = tmp_path
        key_file = tmp_path / "master.key"
        if key:
            key_file.write_bytes(_key())
        self.store = SecretStore(tmp_path / "secrets", key_file if key else "")
        self.issuer = Issuer(tmp_path / "issuer.pem")
        self.providers = {pid: SubprocessProvider(pid, self.store, root=tmp_path / "conn", kill_grace_s=0.5)
                          for pid in FAKES}
        self.table = jobs.Jobs(self.providers, tmp_path / "jobs", poll_s=0.02, grace_s=0.3, cancel_wait_s=5,
                               unreachable_s=2)
        self.gateway = server.Gateway({}, server.KeySource(url="test", fetch=self.issuer.public_jwk),
                                      evidence_providers=self.providers, job_table=self.table, secrets=self.store)
        self.httpd = server.serve(0, self.gateway, host="127.0.0.1")
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def call(self, method, path, token=None, data=None, headers=None):
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                         headers={**({"Authorization": f"Bearer {token}"} if token else {}),
                                                  **(headers or {})})
        try:
            with OPENER.open(request, timeout=60) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def ticket(self, operation, scope, job_id="ej-ticket"):
        return self.issuer.issue(job_id=job_id, operation=operation, scope_hash=scope, budget={})

    def put_secret(self, fields, source=SOURCE, *, scope_fields=None):
        token = self.ticket("secret", scope_hash({"source": source, "fields": sorted(scope_fields or fields)}))
        status, body = self.call("PUT", f"/v1/secrets/{source}", token, json.dumps({"fields": fields}).encode(),
                                 {"Content-Type": "application/json"})
        return status, json.loads(body)

    def delete_secret(self, source=SOURCE):
        status, body = self.call("DELETE", f"/v1/secrets/{source}",
                                 self.ticket("secret", scope_hash({"source": source, "delete": True})))
        return status, json.loads(body)

    def cred(self):
        status, body = self.put_secret({"password": PASSWORD})
        assert status == 200, body
        return body["cred_ref"]

    def submit(self, spec):
        operation = spec["operation"] if spec["operation"] in tickets.OPERATIONS else "snapshot"
        token = self.ticket(operation, scope_hash(spec), spec["job_id"])
        status, body = self.call("POST", "/v1/jobs", token, b"", {"X-Onto-Job": encode_job_header(spec)})
        return status, json.loads(body)

    def job(self, method, job_id, suffix=""):
        status, body = self.call(method, f"/v1/jobs/{job_id}{suffix}", self.ticket("job", job_scope(job_id), job_id))
        return status, (body if suffix == "/bundle" and status == 200 else json.loads(body))

    def wait(self, job_id, states=jobs.TERMINAL, timeout=30, phase_prefix=None):
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            status, body = self.job("GET", job_id)
            if status == 200 and (body["state"] in states if phase_prefix is None
                                  else str(body["phase"]).startswith(phase_prefix)):
                return body
            time.sleep(0.03)
        raise AssertionError(f"任务 {job_id} 没到 {states}/{phase_prefix}：{body}")

    def test_source(self, spec, *, scope_spec=None, source=SOURCE):
        token = self.ticket("test", scope_hash(scope_spec or spec))
        status, body = self.call("POST", f"/v1/sources/{source}/test", token, json.dumps(spec).encode(),
                                 {"Content-Type": "application/json"})
        return status, json.loads(body)


@pytest.fixture()
def env(tmp_path):
    instance = Env(tmp_path)
    yield instance
    instance.close()


def _connection(mode="ok", port=3306, **extra):
    return {"host": "127.0.0.1", "port": port, "database": "shop", "username": "reader", "mode": mode, **extra}


def _spec(job_id, cred_ref, mode="ok", *, provider="f16-fake", operation="snapshot", connection=None, egress=None,
          budget=None, **extra):
    return {"job_id": job_id, "operation": operation,
            "source": {"id": SOURCE, "provider": provider, "connection": connection or _connection(mode)},
            "scope": ["shop.*"], "budget": {"max_rows": 1000, "max_bytes": 1 << 20, "max_duration_s": 60,
                                            **(budget or {})},
            "cred_ref": cred_ref, "egress": egress if egress is not None else [{"host": "127.0.0.1", "port": 3306}],
            "rate_limit": {"concurrency": 1, "qps": 0, "query_timeout_s": 30},
            "masking": {"policy": "mask-v1", "salt": "00ff"}, "selector": None, "limit": None, **extra}


def _test_spec(cred_ref, mode="ok", *, provider="f16-fake", connection=None, egress=None):
    return {"source": {"id": SOURCE, "provider": provider, "connection": connection or _connection(mode)},
            "cred_ref": cred_ref, "egress": egress if egress is not None else [{"host": "127.0.0.1", "port": 3306}],
            "rate_limit": {"concurrency": 1, "qps": 0, "query_timeout_s": 30}}


def _untar(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _wait_dead(pid: int, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


# ---- 凭据库 ----


def test_secret_store_rotation_merge_and_retention(tmp_path):
    key_file = tmp_path / "master.key"
    key_file.write_bytes(_key())
    store = SecretStore(tmp_path / "secrets", key_file)
    assert store.ready()
    first = store.put(SOURCE, {"password": PASSWORD, "extra": "x1"})
    assert first["cred_ref"] == f"cred://{SOURCE}/1" and first["written_at"].endswith("Z")
    path = tmp_path / "secrets" / SOURCE / "1.bin"
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    assert PASSWORD.encode() not in path.read_bytes()
    # 轮换一个字段：其余字段沿用上一版
    rotated = "pw-" + pysecrets.token_urlsafe(18)
    assert store.put(SOURCE, {"password": rotated})["cred_ref"] == f"cred://{SOURCE}/2"
    assert store.open(f"cred://{SOURCE}/2") == {"password": rotated, "extra": "x1"}
    assert store.open(f"cred://{SOURCE}/1")["password"] == PASSWORD
    # 第 3 版写入后只留最近 2 版；正在用的旧版本先不删，用完再删
    store.acquire(f"cred://{SOURCE}/2")
    store.put(SOURCE, {"password": "v3-" + pysecrets.token_hex(8)})
    store.put(SOURCE, {"password": "v4-" + pysecrets.token_hex(8)})
    assert store.versions(SOURCE) == [2, 3, 4]
    store.release(f"cred://{SOURCE}/2")
    assert store.versions(SOURCE) == [3, 4]
    assert store.exists(f"cred://{SOURCE}/4", SOURCE) and not store.exists(f"cred://{SOURCE}/4", "f16-other")
    assert not store.exists(f"cred://{SOURCE}/1", SOURCE) and not store.exists("cred://bad", SOURCE)
    assert store.delete(SOURCE) == {"source": SOURCE, "deleted": 2}
    assert store.versions(SOURCE) == [] and not (tmp_path / "secrets" / SOURCE).exists()
    for bad in ({}, {"Password": "x"}, {"password": 1}, {"password": "x" * 9000}):
        with pytest.raises(Exception) as caught:
            store.put(SOURCE, bad)
        assert getattr(caught.value, "status", None) == 400


def test_secret_store_bound_to_source_version_and_key(tmp_path):
    key_file = tmp_path / "master.key"
    key_file.write_bytes(_key())
    store = SecretStore(tmp_path / "secrets", key_file)
    store.put(SOURCE, {"password": PASSWORD})
    # 文件挪到别的源下解不开（附加数据绑定源和版本）
    other = tmp_path / "secrets" / "f16-other"
    other.mkdir(mode=0o700)
    (other / "1.bin").write_bytes((tmp_path / "secrets" / SOURCE / "1.bin").read_bytes())
    with pytest.raises(Exception) as caught:
        store.open("cred://f16-other/1")
    assert caught.value.code == "cred_undecryptable"
    # 换了主密钥也解不开
    key_file.write_bytes(_key())
    with pytest.raises(Exception) as caught:
        SecretStore(tmp_path / "secrets", key_file).open(f"cred://{SOURCE}/1")
    assert caught.value.code == "cred_undecryptable"
    # 主密钥不是 32 字节：不可用
    key_file.write_bytes(base64.b64encode(b"short"))
    assert not SecretStore(tmp_path / "secrets", key_file).ready()
    assert not SecretStore(tmp_path / "secrets", "").ready()


def test_secret_http_put_rotate_delete_and_no_read(env):
    status, body = env.put_secret({"password": PASSWORD})
    assert status == 200 and body["cred_ref"] == f"cred://{SOURCE}/1"
    status, body = env.put_secret({"password": "pw-" + pysecrets.token_hex(8)})
    assert status == 200 and body["cred_ref"] == f"cred://{SOURCE}/2"
    # 票据的范围哈希要覆盖字段名
    status, body = env.put_secret({"password": PASSWORD, "token": "t"}, scope_fields={"password": 1})
    assert status == 401 and body["error"] == "ticket_invalid"
    # 没有任何读出明文的接口
    for method in ("GET", "POST"):
        status, body = env.call(method, f"/v1/secrets/{SOURCE}", env.ticket("secret", "x"), b"{}" if method == "POST"
                                else None)
        assert status == 404 and PASSWORD.encode() not in body
    status, body = env.delete_secret()
    assert status == 200 and body == {"source": SOURCE, "deleted": 2}
    assert env.store.versions(SOURCE) == []
    status, body = env.call("PUT", f"/v1/secrets/{SOURCE}", None, b"{}")
    assert status == 401


def test_without_master_key_secrets_unavailable(tmp_path):
    instance = Env(tmp_path, key=False)
    try:
        status, body = instance.put_secret({"password": PASSWORD})
        assert status == 503 and body["error"] == "secrets_unavailable"
        status, body = instance.delete_secret()
        assert status == 503 and body["error"] == "secrets_unavailable"
        listed = json.loads(instance.call("GET", "/v1/providers")[1])["providers"]
        assert {item["id"]: item["installed"] for item in listed} == {pid: False for pid in FAKES}
        status, body = instance.submit(_spec("ej-nokey", f"cred://{SOURCE}/1"))
        assert status == 409 and body["error"] == "provider_not_installed"
        status, body = instance.test_source(_test_spec(f"cred://{SOURCE}/1"))
        assert status == 503 and body["error"] == "secrets_unavailable"
    finally:
        instance.close()


def test_ticket_operation_secret():
    assert "secret" in tickets.OPERATIONS


# ---- 采集任务与快照包 ----


def test_snapshot_job_bundle_with_access_log(env):
    cred_ref = env.cred()
    spec = _spec("ej-f16-1", cred_ref, connection=_connection("ok", access=1005))
    status, body = env.submit(spec)
    assert status == 202, body
    final = env.wait("ej-f16-1")
    assert final["state"] == "succeeded", final
    status, tar_bytes = env.job("GET", "ej-f16-1", "/bundle")
    files = _untar(tar_bytes)
    manifest = json.loads(files["manifest.json"])
    assert manifest["source"] == {"id": SOURCE, "provider": "f16-fake"} and manifest["provider"] == "f16-fake@0.1.0"
    assert manifest["masking_policy"] == "mask-v1" and manifest["artifact"] is None
    assert manifest["records"] == {"entity": 2} and manifest["complete"] is True
    assert manifest["limitations"] == ["视图定义没有读"]
    # 访问审计：最多 1000 条，超出的和最后一条合并成一条汇总
    log = manifest["access_log"]
    assert len(log) == 1000 and log[0]["statement_digest"].startswith("select:") and log[0]["rows"] == 1
    assert log[0]["at"].endswith("Z") and "SELECT" not in json.dumps(log)
    assert log[-1] == {"statement_digest": "*", "count": 6, "rows": 6, "bytes": 60, "duration_ms": 12}
    for line in files["records/entity.jsonl"].decode().splitlines():
        record = json.loads(line)
        assert record["id"].startswith(f"db:{SOURCE}/shop.") and record["hash"] == record_hash(record)
    summary = files["summary.md"].decode()
    assert summary.splitlines()[0] == bundle.SUMMARY_FIRST_LINE_CONNECTION and "- 提供方：" in summary
    assert "外部接口" not in summary and "程序类型" not in summary
    inventory = json.loads(files["inventory.json"])
    assert inventory["items"][0]["estimate_rows"] == 12 and inventory["skipped"][0]["object"] == "shop.audit"
    # 记录只留在快照包目录，运行目录清掉
    assert env.job("DELETE", "ej-f16-1")[0] == 200
    assert not (env.tmp / "conn" / "ej-f16-1").exists() and not (env.tmp / "jobs" / "ej-f16-1").exists()


def test_code_repo_artifact_from_result(env):
    cred_ref = env.cred()
    artifact = {"repo_url": "https://192.0.2.1/demo/shop.git", "ref": "main", "commit": "0" * 40}
    connection = {"repo_url": "https://192.0.2.1/demo/shop.git", "ref": "main", "mode": "ok", "artifact": artifact}
    spec = _spec("ej-f16-code", cred_ref, provider="f16-fake-code", connection=connection,
                 egress=[{"host": "192.0.2.1", "port": 443}], masking=None)
    spec["scope"] = []
    assert env.submit(spec)[0] == 202
    assert env.wait("ej-f16-code")["state"] == "succeeded"
    files = _untar(env.job("GET", "ej-f16-code", "/bundle")[1])
    manifest = json.loads(files["manifest.json"])
    assert manifest["artifact"] == artifact and manifest["masking_policy"] == "none"
    assert json.loads(files["records/entity.jsonl"].decode().splitlines()[0])["id"].startswith(f"code:{SOURCE}/")
    assert "`https://192.0.2.1/demo/shop.git` @ `" + "0" * 40 in files["summary.md"].decode()


def test_secret_only_via_stdin(env):
    spec = _spec("ej-f16-env", env.cred(), "inspect")
    assert env.submit(spec)[0] == 202
    assert env.wait("ej-f16-env")["state"] == "succeeded"
    files = _untar(env.job("GET", "ej-f16-env", "/bundle")[1])
    attrs = json.loads(files["records/entity.jsonl"])["attrs"]
    # 子进程拿到的就是这个凭据，但环境变量、命令行里都没有；环境变量只有最少的几个
    assert attrs["secret_sha"] == hashlib.sha256(PASSWORD.encode()).hexdigest()
    assert attrs["env_has_secret"] is False and attrs["proc_environ_has_secret"] is False
    assert attrs["argv_has_secret"] is False and attrs["stdin_closed"] is True
    assert set(attrs["env_keys"]) <= {"PATH", "LANG", "HOME", "TMPDIR", "LC_CTYPE"}
    assert PASSWORD.encode() not in json.dumps(files, default=lambda value: value.decode()).encode()


@pytest.mark.parametrize("mode,code", [("raise_secret", "provider_error"), ("connerror_secret", "auth_failed"),
                                       ("stderr_crash", "crashed")])
def test_error_messages_never_carry_password(env, caplog, mode, code):
    caplog.set_level(logging.DEBUG)
    job_id = f"ej-f16-{mode.replace('_', '-')}"
    assert env.submit(_spec(job_id, env.cred(), mode))[0] == 202
    final = env.wait(job_id)
    assert final["state"] == "failed" and final["error_code"] == code
    assert PASSWORD not in json.dumps(final, ensure_ascii=False)
    assert PASSWORD not in caplog.text
    if mode == "connerror_secret":
        assert MASK in final["error_message"]


def test_password_in_records_and_stdout_is_masked(env, caplog):
    caplog.set_level(logging.DEBUG)
    assert env.submit(_spec("ej-f16-leak", env.cred(), "leak_record"))[0] == 202
    assert env.wait("ej-f16-leak")["state"] == "succeeded"
    files = _untar(env.job("GET", "ej-f16-leak", "/bundle")[1])
    record = json.loads(files["records/entity.jsonl"])
    # 遮蔽后重算了 hash，网关的记录校验照样通过
    assert record["label"] == f"连接串 user:{MASK}@db" and record["hash"] == record_hash(record)
    assert all(PASSWORD.encode() not in data for data in files.values())
    assert PASSWORD not in caplog.text


def test_budget_truncation_marks_partial(env):
    assert env.submit(_spec("ej-f16-budget", env.cred(), "many", budget={"max_rows": 4}))[0] == 202
    final = env.wait("ej-f16-budget")
    assert final["state"] == "partial"
    manifest = json.loads(_untar(env.job("GET", "ej-f16-budget", "/bundle")[1])["manifest.json"])
    assert manifest["complete"] is False and manifest["records"] == {"entity": 4}
    assert 0 < manifest["coverage_pct"] < 100


def test_no_result_is_failure(env):
    assert env.submit(_spec("ej-f16-nores", env.cred(), "no_result"))[0] == 202
    final = env.wait("ej-f16-nores")
    assert final["state"] == "failed" and final["error_code"] == "no_result"


def test_cancel_and_timeout_kill_whole_group(env):
    cred_ref = env.cred()
    assert env.submit(_spec("ej-f16-cancel", cred_ref, "sleep_child", provider="f16-fake-exec"))[0] == 202
    child = int(env.wait("ej-f16-cancel", phase_prefix="child:")["phase"].split(":")[1])
    worker = env.providers["f16-fake-exec"]._runs["ej-f16-cancel"].process.pid
    assert _alive(child)
    assert env.job("POST", "ej-f16-cancel", "/cancel")[0] == 200
    assert env.wait("ej-f16-cancel")["state"] == "cancelled"
    assert _wait_dead(child) and _wait_dead(worker)

    spec = _spec("ej-f16-timeout", cred_ref, "sleep_child", provider="f16-fake-exec", budget={"max_duration_s": 2})
    assert env.submit(spec)[0] == 202
    child = int(env.wait("ej-f16-timeout", phase_prefix="child:")["phase"].split(":")[1])
    final = env.wait("ej-f16-timeout", timeout=15)
    assert final["state"] == "failed" and final["error_code"] == "timeout"
    assert _wait_dead(child)


# ---- 审计钩子（经测试连接跑） ----


@pytest.fixture()
def listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    yield sock.getsockname()[1]
    sock.close()


def test_audit_hook_allows_egress_and_blocks_the_rest(env, listener):
    cred_ref = env.cred()
    egress = [{"host": "127.0.0.1", "port": listener}]

    def run(mode, provider="f16-fake", **extra):
        spec = _test_spec(cred_ref, provider=provider, connection=_connection(mode, port=listener, **extra),
                          egress=egress)
        status, body = env.test_source(spec)
        assert status == 200, body
        return body

    assert run("connect", target={"host": "127.0.0.1", "port": listener})["ok"] is True
    # 同一主机、白名单外的端口；保留网段的地址
    for target in ({"host": "127.0.0.1", "port": listener + 1 if listener < 65535 else 1},
                   {"host": "192.0.2.10", "port": 3306}):
        body = run("connect", target=target)
        assert body["ok"] is False and body["error_code"] == "egress_denied", body
    body = run("resolve", target_host="blocked.example.invalid")
    assert body["error_code"] == "egress_denied" and "blocked.example.invalid" in body["error_message"]
    # 提供方吞掉了被拦的异常也算失败
    assert run("swallow")["error_code"] == "egress_denied"
    # 数据库类不放行任何子进程；code-repo 类只放行 catalog 里列的
    assert run("popen", argv=["true"])["error_code"] == "exec_denied"
    assert run("system")["error_code"] == "exec_denied"
    assert run("popen", provider="f16-fake-exec", argv=["git", "--version"])["ok"] is True
    assert run("popen", provider="f16-fake-exec", argv=["ls"])["error_code"] == "exec_denied"


# ---- 测试连接 ----


def test_test_endpoint(env):
    cred_ref = env.cred()
    status, body = env.test_source(_test_spec(cred_ref))
    assert status == 200 and body == {"ok": True, "checks": [
        {"name": "connectivity", "ok": True, "detail": "", "latency_ms": 3},
        {"name": "readonly_account", "ok": True, "detail": "只有 SELECT 权限"}]}
    status, body = env.test_source(_test_spec(cred_ref, "connerror_secret"))
    assert status == 200 and body["ok"] is False and body["error_code"] == "auth_failed"
    assert PASSWORD not in json.dumps(body, ensure_ascii=False)
    # 票据范围对不上、路径和请求体里的源不一致、凭据不是这个源的
    spec = _test_spec(cred_ref)
    assert env.test_source(spec, scope_spec={**spec, "egress": []})[0] == 401
    assert env.test_source(spec, source="f16-other")[0] == 400
    status, body = env.test_source(_test_spec("cred://f16-other/1"))
    assert status == 409 and body["error"] == "cred_not_found"
    assert env.test_source({**spec, "source": {**spec["source"], "provider": "rea-static"}})[0] == 409


# ---- 任务说明核对 ----


def test_job_spec_rejections(env):
    cred_ref = env.cred()
    env.store.put("f16-other", {"password": "pw-" + pysecrets.token_hex(8)})
    code_conn = {"repo_url": "https://192.0.2.1/demo/shop.git", "ref": "main", "mode": "ok"}
    cases = [
        (dict(cred_ref=None), 409, "cred_not_found"),
        (dict(cred_ref="cred://f16-other/1"), 409, "cred_not_found"),
        (dict(cred_ref=f"cred://{SOURCE}/9"), 409, "cred_not_found"),
        (dict(egress=[]), 400, "egress_invalid"),
        (dict(egress=[{"host": "127.0.0.1", "port": "3306"}]), 400, "egress_invalid"),
        (dict(egress=[{"host": "127.0.0.1", "port": 3307}]), 400, "egress_mismatch"),
        (dict(connection=_connection(port=5432)), 400, "egress_mismatch"),
        (dict(connection={"mode": "ok"}), 400, "bad_request"),
        (dict(operation="sample", selector="shop.t0", limit=21), 400, "bad_request"),
        (dict(operation="sample", selector="shop.t0", limit=0), 400, "bad_request"),
        (dict(operation="sample", selector="crm.customer", limit=5), 400, "bad_request"),
        (dict(operation="sample", selector=None, limit=5), 400, "bad_request"),
        (dict(rate_limit="fast"), 400, "bad_request"),
        (dict(provider="f16-fake-code", operation="discover", connection=code_conn,
              egress=[{"host": "192.0.2.1", "port": 443}]), 400, "operation_not_supported"),
        (dict(provider="f16-fake-code", connection={**code_conn, "repo_url": "http://192.0.2.1/demo.git"},
              egress=[{"host": "192.0.2.1", "port": 443}]), 400, "bad_request"),
        (dict(provider="f16-fake-code", connection={**code_conn, "repo_url": "https://u:p@192.0.2.1/demo.git"},
              egress=[{"host": "192.0.2.1", "port": 443}]), 400, "bad_request"),
        (dict(provider="f16-fake-code", connection=code_conn, egress=[{"host": "192.0.2.1", "port": 80}]),
         400, "egress_mismatch"),
        (dict(operation="incremental"), 400, "bad_request"),
    ]
    for index, (overrides, status, error) in enumerate(cases):
        spec = _spec(f"ej-f16-bad{index}", cred_ref, **{key: value for key, value in overrides.items()
                                                         if key not in ("cred_ref",)})
        if "cred_ref" in overrides:
            spec["cred_ref"] = overrides["cred_ref"]
        got_status, body = env.submit(spec)
        assert (got_status, body.get("error")) == (status, error), (index, overrides, body)
    # 取样：范围内一个对象、不超过 20 行
    spec = _spec("ej-f16-sample", cred_ref, operation="sample", selector="shop.t0", limit=20)
    assert env.submit(spec)[0] == 202
    assert env.wait("ej-f16-sample")["state"] == "succeeded"


# ---- 提供方清单 ----


def test_providers_listing_and_installed(tmp_path, monkeypatch):
    key_file = tmp_path / "master.key"
    key_file.write_bytes(_key())
    ready = SecretStore(tmp_path / "secrets", key_file)
    # 没配主密钥文件：连接型提供方不加载（和没配旁路容器的 rea-static 一样）
    assert providers.load(SecretStore(tmp_path / "secrets", "")) == {}
    loaded = providers.load(ready)
    assert set(loaded) >= {"db-mysql", "db-postgres", "code-repo"}
    for provider_id in ("db-mysql", "db-postgres", "code-repo"):
        info = loaded[provider_id].describe()
        assert not set(info) & set(catalog.INTERNAL)
        assert info["input"] == "connection" and info["self_service"] is False and info["runtime"] == "subprocess"
    found = SubprocessProvider("db-mysql", ready, find_spec=lambda name: object(), which=lambda name: "/usr/bin/x")
    missing = SubprocessProvider("db-mysql", ready, find_spec=lambda name: None)
    no_git = SubprocessProvider("code-repo", ready, find_spec=lambda name: object(), which=lambda name: None)
    no_key = SubprocessProvider("db-mysql", SecretStore(tmp_path / "secrets", tmp_path / "absent.key"),
                                find_spec=lambda name: object())
    before = set(sys.modules)
    assert (found.installed(), missing.installed(), no_git.installed(), no_key.installed()) == (True, False, False,
                                                                                                False)
    for provider in loaded.values():
        provider.installed()
    # 网关进程不导入驱动
    assert not {"pymysql", "pg8000", "javalang"} & (set(sys.modules) - before)
    # 配了主密钥文件的网关在 /v1/providers 里列出三个连接型提供方
    monkeypatch.setenv("ONTO_CONNECTOR_MASTER_KEY_FILE", str(key_file))
    monkeypatch.setenv("ONTO_CONNECTOR_SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.delenv("ONTO_CONNECTOR_REA_URL", raising=False)
    listed = server.Gateway({}).providers()["providers"]
    assert {item["id"] for item in listed} >= {"db-mysql", "db-postgres", "code-repo"}
