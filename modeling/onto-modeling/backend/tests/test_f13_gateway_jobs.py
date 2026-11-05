"""F13 连接器网关的采集任务与提供方 rea-static（证据采集接口 §3～§5；ADR-0082）。

旁路容器用线程里的 http.server 假冒（按 §5 回进度、结果、记录、原始结果）；票据用真的 Issuer / Verifier。
数据全部合成。
"""

import hashlib
import http.client
import io
import json
import re
import tarfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.connector_gateway import bundle, jobs, providers, server
from app.connector_gateway.providers.rea_static import REA_URL_ENV, ReaStatic
from app.evidence.spec import bin_anchor, encode_job_header, job_scope, record_hash, relation_anchor, scope_hash
from app.evidence.tickets import Issuer, sha256_hex

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SOURCE = "demo-client"
# 跨过 1 MiB 的转发块，验证边收边转
ARTIFACT = bytes(range(256)) * 9000
RAW = b'{"evidence": "synthetic"}\n' * 4000
MODULE = "renderer/app.js"


def _record(type_, path_kind, segments, name, *, kind, module=MODULE, parent=None, **extra):
    record = {"id": bin_anchor(SOURCE, path_kind, *segments), "type": type_, "parent": parent, "name": name,
              "label": None, "attrs": {"kind": kind, "module": module},
              "evidence": {"provider": "rea-static@0.1.0", "method": "rea analyze-javascript-application",
                           "confidence": "observed"}, **extra}
    record["hash"] = record_hash(record)
    return record


def _records():
    module = _record("entity", "mod", [MODULE], MODULE, kind="module")
    order = _record("entity", "sym", [MODULE, "Order"], "Order", kind="class", parent=module["id"])
    field = _record("field", "sym", [MODULE, "Order", "id"], "id", kind="property", parent=order["id"])
    ipc = _record("operation", "ipc", ["order:create"], "order:create", kind="ipc")
    method = _record("operation", "sym", [MODULE, "Order", "approve"], "approve", kind="method", parent=order["id"])
    ext = _record("entity", "ext", ["POST https://erp.example.com/api/orders"], "POST https://erp.example.com/api/orders",
                  kind="endpoint", module=None)
    relation = {"id": relation_anchor(SOURCE, ipc["id"], "requests", ext["id"]), "type": "relation", "parent": None,
                "name": "requests", "label": None, "from": ipc["id"], "to": ext["id"],
                "attrs": {"url": "https://erp.example.com/api/orders", "method": "POST"},
                "evidence": {"provider": "rea-static@0.1.0", "method": "rea analyze-javascript-application",
                             "confidence": "inferred"}}
    relation["hash"] = record_hash(relation)
    return [module, order, field, ipc, method, ext, relation]


def _result(records):
    counts = {}
    for record in records:
        counts[record["type"]] = counts.get(record["type"], 0) + 1
    return {"artifact": {"sha256": sha256_hex(ARTIFACT), "size": len(ARTIFACT), "detected": "electron"},
            "analyzers": [{"command": "analyze-javascript-application", "provider": "rea-javascript-application@1",
                           "ms": 4578}],
            "inventory": {"items": [{"object": "resources/app.asar", "kind": "javascript", "bytes": 1024}],
                          "skipped": [{"object": "resources/icon.png", "reason": "不是代码"}]},
            "limitations": ["动态拼出的 IPC 通道名没有展开\n第二行"], "unknowns": 3, "records": counts,
            "raw": [{"name": "analyze-javascript-application.json", "sha256": sha256_hex(RAW), "size": len(RAW),
                     "media_type": "application/json"}]}


class FakeSidecar:
    """按证据采集接口 §5 应答的假旁路容器。"""

    def __init__(self):
        self.healthy = True
        self.health_calls = 0
        self.steps = 2
        self.final = {"state": "succeeded"}
        self.release = threading.Event()
        self.release.set()
        self.records = _records()
        self.lines: bytes | None = None
        self.result = _result(self.records)
        self.raw = {"analyze-javascript-application.json": RAW}
        self.jobs: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.lost = False
        sidecar = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload=None, raw=None, content_type="application/json"):
                body = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _job(self):
                match = re.match(r"^/v1/jobs/([^/]+)(/result|/records|/cancel|/raw/(.+))?$", urlsplit(self.path).path)
                job = sidecar.jobs.get(match.group(1)) if match and not sidecar.lost else None
                return match, job

            def do_GET(self):
                if self.path == "/health":
                    sidecar.health_calls += 1
                    if sidecar.healthy:
                        self._send(200, {"status": "ok", "rea_version": "6.8.0", "running": 0, "queued": 0})
                    else:
                        self._send(503, {"error": "starting"})
                    return
                match, job = self._job()
                if job is None:
                    self._send(404, {"error": "not_found"})
                elif match.group(2) is None:
                    self._send(200, sidecar.status(job))
                elif match.group(2) == "/result":
                    self._send(200, sidecar.result)
                elif match.group(2) == "/records":
                    lines = sidecar.lines if sidecar.lines is not None else b"".join(
                        (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8") for item in sidecar.records)
                    self._send(200, raw=lines, content_type="application/x-ndjson")
                else:
                    data = sidecar.raw.get(unquote(match.group(3)))
                    self._send(200, raw=data) if data is not None else self._send(404, {"error": "not_found"})

            def do_POST(self):
                parts = urlsplit(self.path)
                if parts.path == "/v1/jobs":
                    params = {key: values[0] for key, values in parse_qs(parts.query).items()}
                    remaining, digest, size = int(self.headers["Content-Length"]), hashlib.sha256(), 0
                    while remaining:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        remaining -= len(chunk)
                    sidecar.jobs[params["id"]] = {"id": params["id"], "params": params, "sha256": digest.hexdigest(),
                                                  "size": size, "polls": 0, "cancelled": False}
                    self._send(202, {"id": params["id"]})
                    return
                match, job = self._job()
                if job is None or match.group(2) != "/cancel":
                    self._send(404, {"error": "not_found"})
                    return
                job["cancelled"] = True
                sidecar.cancelled.append(job["id"])
                self._send(200, {"id": job["id"]})

            def do_DELETE(self):
                match, job = self._job()
                if job is None:
                    self._send(404, {"error": "not_found"})
                    return
                sidecar.deleted.append(sidecar.jobs.pop(job["id"])["id"])
                self._send(200, {"deleted": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()

    def status(self, job):
        job["polls"] += 1
        base = {"job_id": job["id"], "error_code": None, "error_message": None}
        if job["cancelled"]:
            return {**base, "state": "cancelled", "phase": "已取消", "done": 0, "total": 0, "unit": ""}
        if not self.release.is_set() or job["polls"] <= self.steps:
            return {**base, "state": "running", "phase": "解析程序", "done": min(job["polls"], self.steps),
                    "total": self.steps, "unit": "步"}
        return {**base, "phase": "完成", "done": self.steps, "total": self.steps, "unit": "步", **self.final}


class Env:
    def __init__(self, tmp_path):
        self.sidecar = FakeSidecar()
        self.issuer = Issuer(tmp_path / "issuer.pem")
        self.rea = ReaStatic(self.sidecar.url, health_ttl_s=0)
        self.table = jobs.Jobs({self.rea.id: self.rea}, tmp_path / "jobs", poll_s=0.02, grace_s=0.3,
                               cancel_wait_s=2, unreachable_s=2)
        self.gateway = server.Gateway({}, server.KeySource(url="test", fetch=self.issuer.public_jwk),
                                      evidence_providers={self.rea.id: self.rea}, job_table=self.table)
        self.httpd = server.serve(0, self.gateway, host="127.0.0.1")
        threading.Thread(target=self.httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.sidecar.httpd.shutdown()

    def call(self, method, path, token=None, data=None, headers=None):
        request = urllib.request.Request(f"{self.url}{path}", data=data, method=method,
                                         headers={**({"Authorization": f"Bearer {token}"} if token else {}),
                                                  **(headers or {})})
        try:
            with OPENER.open(request, timeout=30) as response:
                body = response.read()
                return response.status, body, response.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def submit(self, spec, data=ARTIFACT, *, operation=None, ticket_spec=None):
        token = self.issuer.issue(job_id=spec["job_id"], operation=operation or spec["operation"],
                                  scope_hash=scope_hash(ticket_spec or spec), budget=spec["budget"])
        status, body, _ = self.call("POST", "/v1/jobs", token, data, {"X-Onto-Job": encode_job_header(spec)})
        return status, json.loads(body)

    def ticket(self, job_id, **kwargs):
        return self.issuer.issue(job_id=job_id, operation="job", scope_hash=job_scope(job_id), budget={}, **kwargs)

    def job(self, method, job_id, suffix=""):
        status, body, headers = self.call(method, f"/v1/jobs/{job_id}{suffix}", self.ticket(job_id))
        return status, (body if suffix == "/bundle" and status == 200 else json.loads(body)), headers

    def wait(self, job_id, states=jobs.TERMINAL, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, body, _ = self.job("GET", job_id)
            if status == 200 and body["state"] in states:
                return body
            time.sleep(0.03)
        raise AssertionError(f"任务 {job_id} 没到 {states}：{body}")


@pytest.fixture()
def env(tmp_path):
    instance = Env(tmp_path)
    yield instance
    instance.close()


def _spec(job_id="ej-0001", data=ARTIFACT, **budget):
    return {"job_id": job_id, "operation": "snapshot",
            "source": {"id": SOURCE, "provider": "rea-static", "connection": {}}, "scope": [],
            "budget": {"max_rows": 1000, "max_bytes": 1 << 20, "max_duration_s": 60, **budget},
            "artifact": {"sha256": sha256_hex(data), "size": len(data), "filename": "demo-client.zip"}}


def _untar(body):
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}, \
            [member.name for member in tar.getmembers()]


# ---- 完整流程 ----


def test_job_end_to_end_bundle(env, tmp_path):
    status, body = env.submit(_spec())
    assert status == 202 and body["job_id"] == "ej-0001" and body["state"] in ("queued", "running")
    # 目标程序原样转给了旁路容器，参数按 §5
    received = env.sidecar.jobs["ej-0001"]
    assert received["sha256"] == sha256_hex(ARTIFACT) and received["size"] == len(ARTIFACT)
    assert received["params"] | {"max_bytes": "x"} == {
        "id": "ej-0001", "source": SOURCE, "filename": "demo-client.zip", "sha256": sha256_hex(ARTIFACT),
        "max_seconds": "60", "max_records": "1000", "max_bytes": "x"}
    # 网关不落盘目标程序
    assert not any(path.is_file() and path.stat().st_size == len(ARTIFACT) for path in (tmp_path / "jobs").rglob("*"))

    final = env.wait("ej-0001")
    assert final["state"] == "succeeded" and final["error_code"] is None

    status, tar_bytes, headers = env.job("GET", "ej-0001", "/bundle")
    assert status == 200 and int(headers["Content-Length"]) == len(tar_bytes) and len(tar_bytes) % 10240 == 0
    files, order = _untar(tar_bytes)
    blob_name = f"blobs/{sha256_hex(RAW)}"
    assert order == ["manifest.json", "inventory.json", "summary.md", "records/entity.jsonl", "records/field.jsonl",
                     "records/relation.jsonl", "records/operation.jsonl", blob_name]

    manifest = json.loads(files["manifest.json"])
    assert set(manifest) == {"format", "source", "provider", "tool", "job_id", "scope", "started_at", "ended_at",
                             "complete", "coverage_pct", "untrusted_content", "masking_policy", "artifact",
                             "analyzers", "records", "files", "blobs", "budget_used", "limitations", "unknowns"}
    assert manifest["format"] == "evidence-snapshot/1"
    assert manifest["source"] == {"id": SOURCE, "provider": "rea-static"}
    assert manifest["provider"] == "rea-static@0.1.0" and manifest["tool"] == {"name": "rea", "version": "6.8.0"}
    assert manifest["complete"] is True and manifest["coverage_pct"] == 100.0 and manifest["untrusted_content"] is True
    assert manifest["artifact"] == {"sha256": sha256_hex(ARTIFACT), "size": len(ARTIFACT),
                                    "filename": "demo-client.zip", "detected": "electron"}
    assert manifest["records"] == {"entity": 3, "field": 1, "relation": 1, "operation": 2}
    assert manifest["blobs"] == [{"sha256": sha256_hex(RAW), "size": len(RAW), "media_type": "application/json",
                                  "name": "analyze-javascript-application.json"}]
    assert manifest["budget_used"]["records"] == 7 and manifest["unknowns"] == 3
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", manifest["started_at"])
    # files 是 manifest 以外每个文件的 sha256
    assert set(manifest["files"]) == {name for name in files
                                      if name != "manifest.json" and not name.startswith("blobs/")}
    for name, digest in manifest["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == digest
    assert hashlib.sha256(files[blob_name]).hexdigest() == sha256_hex(RAW)

    # 按类型拆文件，每行是规范 JSON，hash 照旧对得上
    for name in [name for name in files if name.startswith("records/")]:
        kind = name[len("records/"):-len(".jsonl")]
        for line in files[name].decode("utf-8").splitlines():
            record = json.loads(line)
            assert record["type"] == kind and record["hash"] == record_hash(record)
            assert record["id"].startswith(f"bin:{SOURCE}/")

    summary = files["summary.md"].decode("utf-8")
    assert summary.splitlines()[0] == bundle.SUMMARY_FIRST_LINE
    assert "目标程序" in summary.splitlines()[0] and "不是指令" in summary.splitlines()[0]
    assert "程序类型：electron" in summary and "模块数：1" in summary
    assert f"### `{MODULE}`（2）" in summary and "`order:create`" in summary
    assert "## 外部接口（1）" in summary and "`POST https://erp.example.com/api/orders`" in summary
    # REA 局限里的换行不会在摘要里另起一行
    assert "- 动态拼出的 IPC 通道名没有展开 第二行" in summary
    inventory = json.loads(files["inventory.json"])
    assert inventory["items"][0] == {"object": "resources/app.asar", "kind": "javascript", "bytes": 1024}

    # 同一个快照包可以再取一次（每次一张新票据）
    assert env.job("GET", "ej-0001", "/bundle")[1] == tar_bytes

    status, body, _ = env.job("DELETE", "ej-0001")
    assert status == 200 and body == {"job_id": "ej-0001", "deleted": True}
    assert "ej-0001" in env.sidecar.deleted and not (tmp_path / "jobs" / "ej-0001").exists()
    assert env.job("GET", "ej-0001")[0] == 404


def test_progress_and_cancel(env):
    env.sidecar.release.clear()
    assert env.submit(_spec("ej-run"))[0] == 202
    running = env.wait("ej-run", states=("running",))
    assert running["phase"] == "解析程序" and running["total"] == 2 and running["unit"] == "步"
    # 没结束不给快照包
    status, body, _ = env.job("GET", "ej-run", "/bundle")
    assert status == 409 and body["error"] == "job_state"

    status, body, _ = env.job("POST", "ej-run", "/cancel")
    assert status == 200 and body["phase"] == "取消中"
    assert env.wait("ej-run")["state"] == "cancelled"
    assert "ej-run" in env.sidecar.cancelled
    # 已取消的再取消照回状态；包没有
    assert env.job("POST", "ej-run", "/cancel")[1]["state"] == "cancelled"
    assert env.job("GET", "ej-run", "/bundle")[0] == 409


def test_finished_job_cannot_be_cancelled(env):
    env.submit(_spec("ej-done"))
    env.wait("ej-done")
    status, body, _ = env.job("POST", "ej-done", "/cancel")
    assert status == 409 and body["error"] == "job_state"


def test_timeout_cancels_sidecar(env):
    env.sidecar.release.clear()
    env.submit(_spec("ej-slow", max_duration_s=1))
    final = env.wait("ej-slow")
    assert final["state"] == "failed" and final["error_code"] == "timeout"
    assert "ej-slow" in env.sidecar.cancelled


def test_sidecar_failure_is_passed_through(env):
    env.sidecar.final = {"state": "failed", "error_code": "artifact_mismatch", "error_message": "哈希对不上"}
    env.submit(_spec("ej-fail"))
    final = env.wait("ej-fail")
    assert final["state"] == "failed" and final["error_code"] == "artifact_mismatch"
    assert env.job("GET", "ej-fail", "/bundle")[0] == 409
    # 失败的任务旁路容器那边马上清
    assert "ej-fail" in env.sidecar.deleted


def test_sidecar_lost_job_fails(env):
    env.sidecar.release.clear()
    env.submit(_spec("ej-lost"))
    env.wait("ej-lost", states=("running",))
    env.sidecar.lost = True
    final = env.wait("ej-lost")
    assert final["state"] == "failed" and final["error_code"] == "provider_restarted"


# ---- 记录校验与预算 ----


def test_budget_truncates_and_marks_partial(env):
    env.submit(_spec("ej-rows", max_rows=4))
    assert env.wait("ej-rows")["state"] == "partial"
    files, _ = _untar(env.job("GET", "ej-rows", "/bundle")[1])
    manifest = json.loads(files["manifest.json"])
    assert manifest["complete"] is False and manifest["budget_used"]["records"] == 4
    assert sum(manifest["records"].values()) == 4 and manifest["coverage_pct"] == round(100 * 4 / 7, 1)
    assert "完整：否" in files["summary.md"].decode("utf-8")


def test_byte_budget_truncates(env):
    first = len(json.dumps(_records()[0], ensure_ascii=False).encode("utf-8"))
    env.submit(_spec("ej-bytes", max_bytes=first * 2))
    assert env.wait("ej-bytes")["state"] == "partial"
    manifest = json.loads(_untar(env.job("GET", "ej-bytes", "/bundle")[1])[0]["manifest.json"])
    assert manifest["budget_used"]["bytes"] <= first * 2 and manifest["budget_used"]["records"] < 7


def test_sidecar_partial_is_partial(env):
    env.sidecar.final = {"state": "partial"}
    env.submit(_spec("ej-part"))
    assert env.wait("ej-part")["state"] == "partial"


@pytest.mark.parametrize("case", ["prefix", "type", "hash", "duplicate", "json"])
def test_bad_records_fail_whole_job(env, tmp_path, case):
    records = _records()
    if case == "prefix":
        bad = {**records[1], "id": bin_anchor("other-source", "sym", "Order")}
        bad["hash"] = record_hash(bad)
        records[1] = bad
    elif case == "type":
        bad = {**records[1], "type": "widget"}
        bad["hash"] = record_hash(bad)
        records[1] = bad
    elif case == "hash":
        records[1] = {**records[1], "name": "Changed"}
    elif case == "duplicate":
        records.append(records[0])
    env.sidecar.records = records
    if case == "json":
        env.sidecar.lines = b'{"id": "bin:demo-client/mod:a"\n'
    env.submit(_spec(f"ej-bad-{case}"))
    final = env.wait(f"ej-bad-{case}")
    assert final["state"] == "failed" and final["error_code"] == "invalid_record"
    if case == "prefix":
        assert f"bin:{SOURCE}/" in final["error_message"]
    # 不留半份快照
    assert env.job("GET", f"ej-bad-{case}", "/bundle")[0] == 409
    assert not (tmp_path / "jobs" / f"ej-bad-{case}").exists()


def test_corrupt_raw_breaks_bundle_stream(env):
    env.submit(_spec("ej-raw"))
    env.wait("ej-raw")
    env.sidecar.raw["analyze-javascript-application.json"] = b"x" * len(RAW)
    with pytest.raises((http.client.IncompleteRead, ConnectionError)):
        env.job("GET", "ej-raw", "/bundle")


def test_tar_size_matches_tarfile():
    items = [bundle.Member("manifest.json", 1), bundle.Member("summary.md", 512),
             bundle.Member("records/entity.jsonl", 513), bundle.Member("blobs/" + "a" * 64, 0)]
    out = io.BytesIO()
    files = {item.name: b"z" * item.size for item in items}
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.USTAR_FORMAT) as tar:
        for item in items:
            info = tarfile.TarInfo(item.name)
            info.size = item.size
            tar.addfile(info, io.BytesIO(files[item.name]))
    assert bundle.tar_size(items) == len(out.getvalue())


# ---- 票据与请求核对 ----


def test_job_endpoints_check_ticket(env):
    env.submit(_spec("ej-t"))
    env.wait("ej-t")
    path = "/v1/jobs/ej-t"
    assert env.call("GET", path)[0] == 401
    # 操作不对：convert 的票据
    wrong_op = env.issuer.issue(job_id="ej-t", operation="convert", scope_hash=job_scope("ej-t"), budget={})
    assert env.call("GET", path, wrong_op)[0] == 401
    # 范围不对：另一个任务的票据
    assert env.call("GET", path, env.ticket("ej-other"))[0] == 401
    # 过期
    assert env.call("GET", path, env.ticket("ej-t", now=time.time() - 700))[0] == 401
    # 重放
    token = env.ticket("ej-t")
    assert env.call("GET", path, token)[0] == 200
    status, body, _ = env.call("GET", path, token)
    assert status == 401 and json.loads(body)["error"] == "ticket_invalid"
    for method, suffix in (("POST", "/cancel"), ("GET", "/bundle"), ("DELETE", "")):
        assert env.call(method, path + suffix, env.ticket("ej-other"))[0] == 401


def test_submit_checks_ticket(env):
    spec = _spec("ej-s")
    # 票据的 op 要和任务说明的 operation 一致
    assert env.submit(spec, operation="discover")[0] == 401
    # 票据绑定的是另一份任务说明（预算被改大）
    other = _spec("ej-s", max_rows=999999)
    status, body = env.submit(other, ticket_spec=spec)
    assert status == 401 and body["error"] == "ticket_invalid"
    assert "ej-s" not in env.sidecar.jobs
    # 没有票据
    status, _, _ = env.call("POST", "/v1/jobs", None, ARTIFACT, {"X-Onto-Job": encode_job_header(spec)})
    assert status == 401
    # 任务说明头坏了
    token = env.issuer.issue(job_id="ej-s", operation="snapshot", scope_hash=scope_hash(spec), budget={})
    assert env.call("POST", "/v1/jobs", token, ARTIFACT, {"X-Onto-Job": "!!"})[0] == 400


def test_submit_size_and_hash_checks(env):
    spec = _spec("ej-size")
    status, body = env.submit(spec, data=ARTIFACT[:-1])
    assert status == 400 and body["error"] == "size_mismatch"
    assert "ej-size" not in env.sidecar.jobs
    # 大小对、内容不对：转完发现哈希不符，旁路容器那边也清掉
    tampered = b"\xff" + ARTIFACT[1:]
    status, body = env.submit(_spec("ej-hash"), data=tampered)
    assert status == 400 and body["error"] == "artifact_mismatch"
    assert "ej-hash" in env.sidecar.deleted
    assert env.job("GET", "ej-hash")[0] == 404
    # 超过提供方上限
    env.rea.max_artifact_bytes = 1024
    status, body = env.submit(_spec("ej-big"))
    assert status == 413 and body["error"] == "too_large"


def test_submit_rejects_unknown_provider_and_duplicates(env):
    spec = _spec("ej-p")
    spec["source"]["provider"] = "db-mysql"
    status, body = env.submit(spec)
    assert status == 409 and body["error"] == "provider_not_installed"
    env.sidecar.healthy = False
    status, body = env.submit(_spec("ej-down"))
    assert status == 409 and body["error"] == "provider_not_installed"
    env.sidecar.healthy = True
    discover = {**_spec("ej-d"), "operation": "discover"}
    assert env.submit(discover)[1]["error"] == "operation_not_supported"
    assert env.submit(_spec("ej-dup"))[0] == 202
    status, body = env.submit(_spec("ej-dup"))
    assert status == 409 and body["error"] == "job_exists"


def test_at_most_two_active_jobs(env):
    env.sidecar.release.clear()
    assert env.submit(_spec("ej-a"))[0] == 202
    assert env.submit(_spec("ej-b"))[0] == 202
    status, body = env.submit(_spec("ej-c"))
    assert status == 503 and body["error"] == "busy"
    env.sidecar.release.set()
    env.wait("ej-a")
    env.wait("ej-b")
    assert env.submit(_spec("ej-c"))[0] == 202


def test_unknown_job_is_404(env):
    status, body, _ = env.job("GET", "ej-missing")
    assert status == 404 and body["error"] == "job_not_found"
    assert env.job("DELETE", "ej-missing")[0] == 404
    assert env.job("POST", "ej-missing", "/cancel")[0] == 404
    assert env.call("GET", "/v1/jobs/..%2Fx")[0] == 404


def test_delete_running_job_cancels_and_cleans(env, tmp_path):
    env.sidecar.release.clear()
    env.submit(_spec("ej-del"))
    env.wait("ej-del", states=("running",))
    assert env.job("DELETE", "ej-del")[0] == 200
    assert env.job("GET", "ej-del")[0] == 404
    deadline = time.time() + 5
    while time.time() < deadline and "ej-del" not in env.sidecar.deleted:
        time.sleep(0.05)
    assert "ej-del" in env.sidecar.cancelled and "ej-del" in env.sidecar.deleted
    assert not (tmp_path / "jobs" / "ej-del").exists()


def test_expired_jobs_are_swept(env, tmp_path):
    env.submit(_spec("ej-old"))
    env.wait("ej-old")
    env.table.ttl_s = 0
    time.sleep(0.01)
    assert env.job("GET", "ej-old")[0] == 404
    assert "ej-old" in env.sidecar.deleted and not (tmp_path / "jobs" / "ej-old").exists()


# ---- 提供方清单 ----


def test_providers_list_installed_flag(env):
    payload = json.loads(env.call("GET", "/v1/providers")[1])
    assert payload["items"] == []
    (info,) = payload["providers"]
    assert info["id"] == "rea-static" and info["installed"] is True and info["kind"] == "rea"
    assert info["self_service"] is True and info["input"] == "artifact" and info["runtime"] == "sidecar"
    assert info["evidence_types"] == ["entity", "field", "operation", "relation"]
    assert info["capabilities"] == ["snapshot"]
    assert info["cred_fields"] == [] and info["readonly_method"] == "静态分析，不运行目标程序，旁路容器断网"
    assert info["license_note"] == "逆向分析前要确认软件授权范围" and info["max_artifact_bytes"] > 0
    env.sidecar.healthy = False
    assert json.loads(env.call("GET", "/v1/providers")[1])["providers"][0]["installed"] is False


def test_provider_health_is_cached():
    sidecar = FakeSidecar()
    try:
        rea = ReaStatic(sidecar.url)
        assert rea.installed() and rea.installed()
        assert sidecar.health_calls == 1
    finally:
        sidecar.httpd.shutdown()
    # 连不上就是没装
    assert ReaStatic("http://127.0.0.1:9").installed() is False


def test_rea_static_listed_only_when_configured(monkeypatch):
    monkeypatch.delenv(REA_URL_ENV, raising=False)
    assert providers.load() == {}
    assert server.Gateway({}).providers() == {"items": [], "providers": []}
    monkeypatch.setenv(REA_URL_ENV, "http://rea-sidecar:8130")
    loaded = providers.load()
    rea = loaded["rea-static"]
    assert list(loaded) == ["rea-static"] and (rea.host, rea.port) == ("rea-sidecar", 8130)
