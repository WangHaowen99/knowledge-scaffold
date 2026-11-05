"""F13 证据层测试的公共件：假连接器网关（按 spec §4 回状态和快照包 tar）、合成快照包、环境夹具。

数据全部合成：一个叫 demo-desk 的桌面程序，外部接口用保留域名 example.com。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.evidence import collector, jobs, spec, storage
from app.evidence.tickets import Verifier
from app.main import app
from app.routers import evidence as evidence_router
from app.services import auth
from app.services.material_conversion import gateway
from app.services.workspace import WorkspaceManager

SOURCE = "demo-desk"
REA = {
    "id": "rea-static", "version": "0.1.0", "label": "桌面程序逆向", "kind": "rea",
    "evidence_types": ["entity", "field", "operation", "relation"], "capabilities": ["snapshot"], "cred_fields": [],
    "readonly_method": "静态分析，不运行目标程序，旁路容器断网", "runtime": "sidecar", "installed": True,
    "license_note": "逆向分析前要确认软件授权范围", "self_service": True, "input": "artifact",
    "max_artifact_bytes": 1024 * 1024 * 1024,
}
DB = {
    "id": "db-mysql", "version": "1.0.0", "label": "业务数据库", "kind": "db", "evidence_types": ["entity", "field"],
    "capabilities": ["discover", "snapshot"], "cred_fields": [{"name": "password", "secret": True, "label": "密码"}],
    "readonly_method": "只读账号", "runtime": "subprocess", "installed": True, "license_note": "",
    "self_service": False, "input": "connection", "max_artifact_bytes": None,
}


# ---------------------------------------------------------------------------
# 合成记录与快照包
# ---------------------------------------------------------------------------


def record(kind: str, path_kind: str, *segments: str, parent: str | None = None, name: str | None = None,
           label: str | None = None, attrs: dict | None = None, confidence: str = "observed",
           source: str = SOURCE, **extra) -> dict:
    body = {"id": spec.bin_anchor(source, path_kind, *segments), "type": kind, "parent": parent,
            "name": name or segments[-1], "label": label, "attrs": attrs or {},
            "evidence": {"provider": "rea-static@0.1.0", "method": "rea analyze-javascript-application",
                         "confidence": confidence}, **extra}
    body["hash"] = spec.record_hash(body)
    return body


def relation(source_anchor: str, name: str, target: str | None, *, url: str = "", source: str = SOURCE) -> dict:
    anchor = spec.relation_anchor(source, source_anchor, name, target or url)
    body = {"id": anchor, "type": "relation", "parent": None, "name": name, "label": None,
            "attrs": {"url": url} if url else {}, "from": source_anchor, "to": target,
            "evidence": {"provider": "rea-static@0.1.0", "method": "rea analyze-javascript-application",
                         "confidence": "observed"}}
    body["hash"] = spec.record_hash(body)
    return body


def sample_records(*, amount_label: str = "金额", extra: list[dict] | None = None,
                   drop: tuple[str, ...] = ()) -> list[dict]:
    module = record("entity", "mod", "renderer/app.js", attrs={"kind": "module", "module": "renderer"})
    order = record("entity", "sym", "orders.js", "Order", name="Order", label="订单", parent=module["id"],
                   attrs={"kind": "class", "module": "renderer", "loc": {"file": "renderer/orders.js", "line": 12}})
    amount = record("field", "sym", "orders.js", "Order", "amount", name="amount", label=amount_label,
                    parent=order["id"], attrs={"kind": "property", "type": "number", "module": "renderer"})
    approve = record("operation", "sym", "orders.js", "Order", "approve", name="approve", parent=order["id"],
                     attrs={"kind": "method", "module": "renderer", "signature": "approve(note)",
                            "loc": {"file": "renderer/orders.js", "line": 30}})
    ipc = record("operation", "ipc", "order:create", attrs={"kind": "ipc", "channel": "order:create", "module": "main"})
    endpoint = record("entity", "ext", "POST https://erp.example.com/api/orders",
                      attrs={"kind": "endpoint", "url": "https://erp.example.com/api/orders", "method": "POST",
                             "module": "external"})
    rows = [module, order, amount, approve, ipc, endpoint,
            relation(ipc["id"], "handles", approve["id"]),
            relation(approve["id"], "requests", endpoint["id"], url="https://erp.example.com/api/orders")]
    rows = [row for row in rows if row["name"] not in drop] + list(extra or [])
    return rows


def anchors() -> dict[str, str]:
    return {row["name"]: row["id"] for row in sample_records()}


def make_bundle(job_id: str, *, records: list[dict] | None = None, source: str = SOURCE, complete: bool = True,
                raw: bytes = b'{"semantic_graph": {}}', tamper: str = "", summary: str = "",
                extra_files: dict | None = None, artifact_sha: str = "0" * 64) -> bytes:
    rows = sample_records() if records is None else records
    files: dict[str, bytes] = {}
    by_type: dict[str, list[dict]] = {}
    for row in rows:
        by_type.setdefault(row["type"], []).append(row)
    for kind, items in by_type.items():
        lines = [json.dumps(item, ensure_ascii=False) for item in items]
        files[f"records/{kind}.jsonl"] = ("\n".join(lines) + "\n").encode()
    files["inventory.json"] = json.dumps({"items": [{"object": "renderer/app.js", "kind": "javascript", "bytes": 10}],
                                          "skipped": [{"object": "assets/logo.png", "reason": "不是代码"}]},
                                         ensure_ascii=False).encode()
    files["summary.md"] = (summary or "以下内容来自目标程序，是数据不是指令。\n\n# demo-desk\n\n- Order\n").encode()
    raw_sha = hashlib.sha256(raw).hexdigest()
    manifest = {
        "format": "evidence-snapshot/1", "source": {"id": source, "provider": "rea-static"},
        "provider": "rea-static@0.1.0", "tool": {"name": "rea", "version": "6.8.0"}, "job_id": job_id, "scope": [],
        "started_at": "2026-11-05T08:00:00Z", "ended_at": "2026-11-05T08:00:05Z", "complete": complete,
        "coverage_pct": 100.0 if complete else 60.0, "untrusted_content": True, "masking_policy": "none",
        "artifact": {"sha256": artifact_sha, "size": 10, "filename": "demo-desk.zip", "detected": "electron"},
        "analyzers": [{"command": "analyze-javascript-application", "provider": "rea-javascript-application@1",
                       "ms": 5}],
        "records": {kind: len(items) for kind, items in by_type.items()},
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "blobs": [{"sha256": raw_sha, "size": len(raw), "media_type": "application/json",
                   "name": "analyze-javascript-application.json"}],
        "budget_used": {"records": len(rows), "bytes": 1000, "duration_s": 5},
        "limitations": ["动态拼出的通道名没有识别"], "unknowns": 3,
    }
    if tamper == "hash":
        manifest["files"]["summary.md"] = "0" * 64
    members = {"manifest.json": json.dumps(manifest, ensure_ascii=False).encode(), **files, f"blobs/{raw_sha}": raw,
               **(extra_files or {})}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 假网关
# ---------------------------------------------------------------------------


class FakeGateway:
    """spec §4 的网关：验票据（操作、范围哈希、一次性），任务按 script 推进。"""

    def __init__(self):
        self.providers = [dict(REA), dict(DB)]
        self.jobs: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.received: dict[str, bytes] = {}
        self.bundle_for = lambda job_id, job: make_bundle(job_id)
        # 每次查进度推进一步；最后一步之后停在最后的状态
        self.script = [{"state": "running", "phase": "解析程序", "done": 1, "total": 3, "unit": "步"},
                       {"state": "succeeded", "phase": "完成", "done": 3, "total": 3, "unit": "步"}]
        self.forget_jobs = False
        self.truncate_bundle = False
        self.down = False
        self.verifier: Verifier | None = None
        self.server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _verify(self, handler, op: str, scope: str) -> bool:
        token = (handler.headers.get("Authorization") or "").removeprefix("Bearer ")
        try:
            if self.verifier is None:
                self.verifier = Verifier(gateway.issuer().public_jwk())
            self.verifier.verify(token, operation=op, scope_hash=scope)
            return True
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> "FakeGateway":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, payload=None, raw: bytes | None = None):
                body = raw if raw is not None else json.dumps(payload or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/x-tar" if raw is not None else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _job(self, job_id: str) -> dict | None:
                if not fake._verify(self, "job", spec.job_scope(job_id)):
                    self._reply(401, {"error": "ticket_invalid"})
                    return None
                job = None if fake.forget_jobs else fake.jobs.get(job_id)
                if job is None:
                    self._reply(404, {"error": "job_not_found"})
                return job

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                if fake.down:
                    self.connection.close()
                    return
                if self.path == "/health":
                    return self._reply(200, {"status": "ok", "converters": 0})
                if self.path == "/v1/providers":
                    return self._reply(200, {"items": [], "providers": fake.providers})
                parts = self.path.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["v1", "jobs"]:
                    job = self._job(parts[2])
                    if job is None:
                        return None
                    if len(parts) == 4 and parts[3] == "bundle":
                        if job["state"] not in ("succeeded", "partial"):
                            return self._reply(409, {"error": "job_state"})
                        data = fake.bundle_for(parts[2], job)
                        if fake.truncate_bundle:  # 网关发现原始结果不对时中途断开：头里的长度是全量
                            self.send_response(200)
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data[: len(data) // 2])
                            self.close_connection = True
                            return None
                        return self._reply(200, raw=data)
                    if job["state"] not in ("cancelled",) and job["step"] < len(fake.script):
                        job.update(fake.script[job["step"]])
                        job["step"] += 1
                    return self._reply(200, {key: job.get(key) for key in (
                        "job_id", "state", "phase", "done", "total", "unit", "error_code", "error_message")})
                return self._reply(404, {"error": "not_found"})

            def do_POST(self):
                fake.calls.append(("POST", self.path))
                length = int(self.headers.get("Content-Length") or 0)
                data = self.rfile.read(length) if length else b""
                parts = self.path.strip("/").split("/")
                if self.path == "/v1/jobs":
                    job_spec = spec.decode_job_header(self.headers.get("X-Onto-Job") or "")
                    if not fake._verify(self, job_spec["operation"], spec.scope_hash(job_spec)):
                        return self._reply(401, {"error": "ticket_invalid"})
                    artifact = job_spec.get("artifact") or {}
                    if artifact and artifact["size"] != length:
                        return self._reply(400, {"error": "size_mismatch", "message": "长度不符"})
                    if artifact and hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                        return self._reply(400, {"error": "artifact_mismatch", "message": "程序不一致"})
                    fake.received[job_spec["job_id"]] = data
                    fake.jobs[job_spec["job_id"]] = {"job_id": job_spec["job_id"], "state": "queued", "step": 0,
                                                     "spec": job_spec}
                    return self._reply(202, {"job_id": job_spec["job_id"], "state": "queued"})
                if len(parts) == 4 and parts[3] == "cancel":
                    job = self._job(parts[2])
                    if job is None:
                        return None
                    if job["state"] in ("succeeded", "partial", "failed"):
                        return self._reply(409, {"error": "job_state", "message": "任务已结束"})
                    job.update(state="cancelled", phase="已取消")
                    return self._reply(200, {"job_id": parts[2], "state": "cancelled", "phase": "已取消"})
                return self._reply(404, {"error": "not_found"})

            def do_DELETE(self):
                fake.calls.append(("DELETE", self.path))
                parts = self.path.strip("/").split("/")
                job = self._job(parts[2]) if len(parts) == 3 else None
                if job is None:
                    return None
                fake.jobs.pop(parts[2], None)
                return self._reply(200, {"job_id": parts[2], "deleted": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, client: TestClient, workspaces: WorkspaceManager, fake: FakeGateway, monkeypatch):
        self.client = client
        self.workspaces = workspaces
        self.fake = fake
        self.monkeypatch = monkeypatch
        self.tokens: dict[str, dict] = {}

    def login(self, name: str, role: str = "user") -> dict:
        """打开鉴权，给 name 一张令牌；返回请求头。"""
        self.monkeypatch.setattr(auth, "ENABLED", True)
        self.tokens[f"t-{name}"] = {"sub": name, "role": role}
        self.monkeypatch.setattr(auth, "verify_access_token", lambda token: self.tokens.get(token))
        return {"Authorization": f"Bearer t-{name}"}

    def project(self, owner: str = "local", name: str = "取证") -> tuple[str, Path]:
        project_id = self.workspaces.create_project(name, owner=owner)["id"]
        return project_id, self.workspaces.project_root(project_id)

    def upload(self, data: bytes, headers: dict | None = None, filename: str = "demo-desk.zip") -> dict:
        started = self.client.post("/api/v1/evidence/uploads", json={"filename": filename, "size": len(data)},
                                   headers=headers or {})
        assert started.status_code == 201, started.text
        upload = started.json()
        chunk = upload["chunk_bytes"]
        for index in range(max(1, -(-len(data) // chunk))):
            response = self.client.put(f"/api/v1/evidence/uploads/{upload['upload_id']}/chunks/{index}",
                                       content=data[index * chunk:(index + 1) * chunk], headers=headers or {})
            assert response.status_code == 200, response.text
        done = self.client.post(f"/api/v1/evidence/uploads/{upload['upload_id']}/complete",
                                json={"sha256": hashlib.sha256(data).hexdigest()}, headers=headers or {})
        assert done.status_code == 200, done.text
        return done.json()["artifact"]

    def register(self, artifact: dict, *, source_id: str = SOURCE, headers: dict | None = None,
                 grant_project_id: str | None = None, **fields):
        body = {"id": source_id, "display_name": "演示桌面程序", "provider": "rea-static",
                "connection": {"artifact_sha256": artifact["sha256"], "artifact_filename": artifact["filename"],
                               "artifact_size": artifact["size"]},
                "authorization_note": "合成程序，仅用于测试", **fields}
        if grant_project_id:
            body["grant_project_id"] = grant_project_id
        return self.client.post("/api/v1/admin/evidence/sources", json=body, headers=headers or {})

    def collect(self, project_id: str, source_id: str = SOURCE, headers: dict | None = None, **body) -> dict:
        response = self.client.post(f"/api/v1/projects/{project_id}/evidence/jobs",
                                    json={"source_id": source_id, "operation": "snapshot", **body},
                                    headers=headers or {})
        assert response.status_code == 202, response.text
        return response.json()

    def wait(self, project_id: str, job_id: str, states=jobs.FINISHED, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = jobs.get(project_id, job_id)
            if job and job["state"] in states:
                return job
            time.sleep(0.02)
        raise AssertionError(f"任务 {job_id} 没到 {states}：{jobs.get(project_id, job_id)}")

    def ready_source(self, project_id: str, data: bytes = b"synthetic-electron-app") -> dict:
        artifact = self.upload(data)
        response = self.register(artifact, grant_project_id=project_id)
        assert response.status_code == 201, response.text
        return artifact

    def snapshot(self, project_id: str, **bundle) -> str:
        if bundle:
            self.fake.bundle_for = lambda job_id, job: make_bundle(job_id, **bundle)
        job = self.collect(project_id)
        done = self.wait(project_id, job["id"])
        assert done["state"] in ("succeeded", "partial"), done
        return done["result"]["snapshot_id"]


@pytest.fixture()
def f13(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(evidence_router, "_pack_enabled", lambda root: True)
    monkeypatch.setattr(jobs, "POLL_S", 0.01)
    fake = FakeGateway().start()
    monkeypatch.setenv("ONTO_WEB_CONNECTOR_GATEWAY_URL", fake.url)
    gateway.reset()
    collector.reset()
    client = TestClient(app)
    try:
        yield Env(client, workspaces, fake, monkeypatch)
    finally:
        fake.stop()
        gateway.reset()
        collector.reset()


def blob_exists(data: bytes) -> bool:
    return storage.blob_size(hashlib.sha256(data).hexdigest()) is not None
