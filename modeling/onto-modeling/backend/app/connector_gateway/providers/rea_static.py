"""提供方 rea-static：旁路容器 rea-sidecar 的客户端（证据采集接口 §4、§5；ADR-0082）。

旁路容器只在 `rea` 内部网络上，那个网络上只有网关，所以请求不带票据。目标程序边收边转（每次 1 MiB），网关不落盘；
REA 原始结果只在取快照包时从旁路容器流式转进 tar。网关不解析目标程序，也不跑 REA。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from typing import BinaryIO, Iterator
from urllib.parse import quote, urlencode, urlsplit

from ...evidence.spi import ProviderInfo
from ..errors import GatewayError

REA_URL_ENV = "ONTO_CONNECTOR_REA_URL"
MAX_ARTIFACT_ENV = "ONTO_CONNECTOR_REA_MAX_ARTIFACT_MB"
VERSION = "0.1.0"
CHUNK_BYTES = 1 << 20
HEALTH_TTL_S = 10.0
REQUEST_TIMEOUT_S = 30.0
# 收完请求体后旁路容器还要落盘、算哈希才回 202
UPLOAD_TIMEOUT_S = 120.0
STATUS_LIMIT = 64 * 1024
RESULT_LIMIT = 32 * 1024 * 1024


class ReaStatic:
    id = "rea-static"
    version = VERSION
    input = "artifact"
    capabilities = ("snapshot",)

    def __init__(self, url: str, *, max_artifact_bytes: int | None = None, health_ttl_s: float = HEALTH_TTL_S,
                 timeout_s: float = REQUEST_TIMEOUT_S):
        parts = urlsplit(url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"{REA_URL_ENV} 要是 http://主机:端口")
        self.host, self.port = parts.hostname, parts.port or 80
        if max_artifact_bytes is None:
            max_artifact_bytes = int(os.environ.get(MAX_ARTIFACT_ENV) or 2048) * 1024 * 1024
        self.max_artifact_bytes = max_artifact_bytes
        self._timeout = timeout_s
        self._health_ttl = health_ttl_s
        self._health: dict | None = None
        self._health_at = float("-inf")
        self._lock = threading.Lock()

    # ---- 清单与健康 ----

    def health(self) -> dict | None:
        """旁路容器的 /health，缓存 10 秒（成功失败都缓存，免得每次列清单都去探）。"""
        with self._lock:
            if time.monotonic() - self._health_at < self._health_ttl:
                return self._health
            try:
                data = self._json("GET", "/health", timeout=3, limit=STATUS_LIMIT)
                self._health = data if data.get("status") == "ok" else None
            except GatewayError:
                self._health = None
            self._health_at = time.monotonic()
            return self._health

    def installed(self) -> bool:
        return self.health() is not None

    def tool(self) -> dict:
        return {"name": "rea", "version": str((self.health() or {}).get("rea_version") or "")}

    def describe(self) -> dict:
        info = ProviderInfo(
            id=self.id, version=self.version, label="REA 静态分析", kind="rea",
            evidence_types=("entity", "field", "operation", "relation"), capabilities=self.capabilities,
            cred_fields=(), readonly_method="静态分析，不运行目标程序，旁路容器断网",
            anchor_rule="bin:<源>/<路径类型>:<名字>，按名字生成，不带版本号、偏移和哈希", runtime="sidecar",
            license_note="逆向分析前要确认软件授权范围")
        return {**asdict(info), "installed": self.installed(), "self_service": True, "input": self.input,
                "max_artifact_bytes": self.max_artifact_bytes}

    # ---- 任务 ----

    def submit(self, spec: dict, budget: dict, body: BinaryIO, length: int) -> str:
        """把请求体原样转给旁路容器，回转发内容的 sha256。内核那条连接断了回 400，旁路容器的错误照转。"""
        artifact = spec.get("artifact") or {}
        query = urlencode({
            "id": spec["job_id"], "source": spec["source"]["id"], "filename": artifact.get("filename") or "",
            "sha256": artifact.get("sha256") or "", "max_seconds": budget["max_duration_s"],
            "max_records": budget["max_rows"], "max_bytes": self.max_artifact_bytes,
        })
        digest = hashlib.sha256()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=UPLOAD_TIMEOUT_S)
        try:
            try:
                conn.putrequest("POST", f"/v1/jobs?{query}", skip_accept_encoding=True)
                conn.putheader("Content-Type", "application/octet-stream")
                conn.putheader("Content-Length", str(length))
                conn.endheaders()
            except (OSError, http.client.HTTPException):
                raise _unavailable() from None
            remaining = length
            while remaining:
                try:
                    chunk = body.read(min(CHUNK_BYTES, remaining))
                except OSError:
                    chunk = b""
                if not chunk:
                    raise GatewayError(400, "body_incomplete", "请求体没收全")
                digest.update(chunk)
                try:
                    conn.send(chunk)
                except OSError:
                    # 旁路容器可能已经回了错误（比如超过上限）再断开，先看看它说了什么
                    self._raise_early_reply(conn)
                    raise _unavailable() from None
                remaining -= len(chunk)
            try:
                response = conn.getresponse()
                status, data = response.status, _parse(response.read(STATUS_LIMIT + 1), STATUS_LIMIT)
            except (OSError, http.client.HTTPException):
                raise _unavailable() from None
        finally:
            conn.close()
        if status >= 400:
            raise _sidecar_error(status, data)
        return digest.hexdigest()

    def status(self, job_id: str) -> dict:
        return self._json("GET", f"/v1/jobs/{quote(job_id)}", limit=STATUS_LIMIT)

    def cancel(self, job_id: str) -> None:
        self._json("POST", f"/v1/jobs/{quote(job_id)}/cancel", limit=STATUS_LIMIT)

    def result(self, job_id: str) -> dict:
        return self._json("GET", f"/v1/jobs/{quote(job_id)}/result", limit=RESULT_LIMIT)

    def delete(self, job_id: str) -> None:
        try:
            self._json("DELETE", f"/v1/jobs/{quote(job_id)}", limit=STATUS_LIMIT)
        except GatewayError as exc:
            if exc.status != 404:
                raise

    @contextmanager
    def records(self, job_id: str) -> Iterator[BinaryIO]:
        """记录 JSONL 流；调用方逐行读，读多少由网关的预算决定。"""
        with self._stream(f"/v1/jobs/{quote(job_id)}/records") as response:
            yield response

    @contextmanager
    def raw(self, job_id: str, name: str) -> Iterator[BinaryIO]:
        with self._stream(f"/v1/jobs/{quote(job_id)}/raw/{quote(name, safe='')}") as response:
            yield response

    # ---- HTTP ----

    def _json(self, method: str, path: str, *, timeout: float | None = None, limit: int) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self._timeout)
        try:
            conn.request(method, path, headers={"Accept": "application/json"})
            response = conn.getresponse()
            status, data = response.status, _parse(response.read(limit + 1), limit)
        except (OSError, http.client.HTTPException):
            raise _unavailable() from None
        finally:
            conn.close()
        if status >= 400:
            raise _sidecar_error(status, data)
        if data is None:
            raise GatewayError(502, "provider_error", "旁路容器的回复不是 JSON 对象")
        return data

    @contextmanager
    def _stream(self, path: str) -> Iterator[BinaryIO]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self._timeout)
        try:
            try:
                conn.request("GET", path)
                response = conn.getresponse()
            except (OSError, http.client.HTTPException):
                raise _unavailable() from None
            if response.status != 200:
                raise _sidecar_error(response.status, _parse(response.read(STATUS_LIMIT + 1), STATUS_LIMIT))
            yield response
        finally:
            conn.close()

    @staticmethod
    def _raise_early_reply(conn: http.client.HTTPConnection) -> None:
        try:
            response = conn.getresponse()
            status, data = response.status, _parse(response.read(STATUS_LIMIT + 1), STATUS_LIMIT)
        except (OSError, http.client.HTTPException):
            return
        if status >= 400:
            raise _sidecar_error(status, data)


def _parse(raw: bytes, limit: int) -> dict | None:
    if len(raw) > limit:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _unavailable() -> GatewayError:
    return GatewayError(503, "provider_unavailable", "连不上旁路容器")


def _sidecar_error(status: int, data: dict | None) -> GatewayError:
    data = data or {}
    code = str(data.get("error") or "provider_error")[:64]
    message = str(data.get("message") or code)[:300]
    if status == 503:
        return GatewayError(503, "busy", "旁路容器繁忙")
    if status == 404:
        return GatewayError(404, "provider_job_missing", "旁路容器里没有这个任务")
    return GatewayError(400 if status < 500 else 502, code, message)
