"""连接器网关的 HTTP 接口（详设 5.8.2；ADR-0073）。只在内部网络上监听，不映射到宿主。

- GET  /health        存活与已加载的转换器数（容器健康检查、内核的健康探测）
- GET  /v1/providers  已加载的转换器：ID、版本、文件类型、许可证
- POST /v1/convert    请求体是原文件字节，`Authorization: Bearer <票据>`；回 Markdown 与位置对照
- /v1/jobs…           采集任务：建任务（请求体是目标程序，边收边转给提供方）、查进度、取消、取快照包、清理（`jobs`；
                      证据采集接口 §4）。`GET /v1/providers` 同时列出证据源提供方
- PUT/DELETE /v1/secrets/{源}   连接型提供方的凭据：只写、只删，没有读出明文的接口（`secrets`；§8）
- POST /v1/sources/{源}/test    测试连接：同步跑提供方子进程的 test，30 秒上限（§8）

票据由内核签发（`app.evidence.tickets`），网关从内核取公钥验签（ONTO_CONNECTOR_ISSUER_KEY_URL），核对操作、原文件的
sha256、转换器，用过即作废。同时最多 MAX_CONCURRENT 个转换在跑，另有 MAX_WAITING 个可排队，再多返回 503。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

from ..evidence.convert import ConvertLimits
from ..evidence.spec import decode_job_header, job_scope, scope_hash
from ..evidence.tickets import TicketError, Verifier, sha256_hex
from . import bundle, jobs, providers, registry, runner
from .errors import GatewayError
from .secrets import SecretStore

_log = logging.getLogger(__name__)

PORT_ENV = "ONTO_CONNECTOR_GATEWAY_PORT"
ISSUER_KEY_URL_ENV = "ONTO_CONNECTOR_ISSUER_KEY_URL"
DEFAULT_PORT = 8120
MAX_CONCURRENT = int(os.environ.get("ONTO_CONNECTOR_MAX_CONCURRENT") or 2)
MAX_WAITING = 8
MAX_INPUT_BYTES = 24 * 1024 * 1024
MAX_TIMEOUT_S = 300
JOB_TIMEOUT_S = min(int(os.environ.get("ONTO_CONNECTOR_JOB_TIMEOUT_S") or 120), MAX_TIMEOUT_S)
JOB_MEMORY_BYTES = int(os.environ.get("ONTO_CONNECTOR_JOB_MEMORY_MB") or 768) * 1024 * 1024
WAIT_SECONDS = 150
READ_TIMEOUT_SECONDS = 60
KEY_REFRESH_SECONDS = 10
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024
# 内核在内部网络上，不走任何代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_JOB_PATH = re.compile(r"^/v1/jobs/([A-Za-z0-9][A-Za-z0-9_-]{0,79})(/cancel|/bundle)?$")
_SECRET_PATH = re.compile(r"^/v1/secrets/([a-z0-9][a-z0-9-]{1,40})$")
_TEST_PATH = re.compile(r"^/v1/sources/([a-z0-9][a-z0-9-]{1,40})/test$")
MAX_JSON_BODY = 64 * 1024
MAX_TESTS = 2


class KeySource:
    """内核的公钥：第一次用时取，票据的 kid 对不上时重取（内核换了密钥），最多每 10 秒一次。"""

    def __init__(self, url: str | None = None, fetch=None):
        self.url = url if url is not None else os.environ.get(ISSUER_KEY_URL_ENV, "")
        self._fetch = fetch or self._http_fetch
        self._verifier: Verifier | None = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def _http_fetch(self) -> dict:
        with _OPENER.open(self.url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def verifier(self, kid: str) -> Verifier | None:
        with self._lock:
            current = self._verifier
            if current is not None and (current.kid == kid or time.time() - self._fetched_at < KEY_REFRESH_SECONDS):
                return current
            if not self.url and self._fetch == self._http_fetch:
                return None
            try:
                fresh = Verifier(self._fetch())
            except (urllib.error.URLError, OSError, ValueError, TypeError, jwt.PyJWTError) as exc:
                _log.warning("取不到内核的票据公钥：%s", type(exc).__name__)
                return current
            # 换公钥时沿用已用过的 jti，不让旧票据在换钥后重放
            if current is not None:
                fresh._used.update(current._used)
            self._verifier, self._fetched_at = fresh, time.time()
            return fresh


class Gateway:
    def __init__(self, converters: dict | None = None, keys: KeySource | None = None, *,
                 max_concurrent: int = MAX_CONCURRENT, max_waiting: int = MAX_WAITING,
                 job_limits: runner.JobLimits | None = None, wait_seconds: float = WAIT_SECONDS,
                 evidence_providers: dict | None = None, job_table: jobs.Jobs | None = None,
                 secrets: SecretStore | None = None):
        self.converters = registry.load() if converters is None else converters
        self.keys = keys or KeySource()
        self.secrets = secrets or SecretStore()
        self.providers_by_id = providers.load(self.secrets) if evidence_providers is None else evidence_providers
        self._tests = threading.BoundedSemaphore(MAX_TESTS)
        self.jobs = job_table or jobs.Jobs(self.providers_by_id)
        self.job_limits = job_limits or runner.JobLimits(timeout_s=JOB_TIMEOUT_S, memory_bytes=JOB_MEMORY_BYTES)
        self._running = threading.BoundedSemaphore(max_concurrent)
        self._admitted = threading.BoundedSemaphore(max_concurrent + max_waiting)
        self._wait_seconds = wait_seconds

    @contextmanager
    def slot(self):
        if not self._admitted.acquire(blocking=False):
            yield False
            return
        try:
            running = self._running.acquire(timeout=self._wait_seconds)
            try:
                yield running
            finally:
                if running:
                    self._running.release()
        finally:
            self._admitted.release()

    def health(self) -> dict:
        return {"status": "ok", "converters": len(self.converters)}

    def providers(self) -> dict:
        return {"items": [item.info.to_dict() for item in self.converters.values()],
                "providers": [item.describe() for item in self.providers_by_id.values()]}

    def verify(self, token: str, operation: str, scope: str) -> dict:
        try:
            kid = str(jwt.get_unverified_header(token).get("kid") or "")
        except jwt.PyJWTError:
            raise GatewayError(401, "ticket_invalid", "票据格式不对") from None
        verifier = self.keys.verifier(kid)
        if verifier is None:
            raise GatewayError(503, "issuer_unavailable", "取不到内核的票据公钥")
        try:
            return verifier.verify(token, operation=operation, scope_hash=scope)
        except TicketError as exc:
            raise GatewayError(401, "ticket_invalid", str(exc)) from None

    # ---- 采集任务（证据采集接口 §4）----

    def submit_job(self, token: str, header: str, body, length: int) -> tuple[int, dict]:
        """票据的 op 是任务说明里的 operation，scope 是任务说明的规范 JSON 哈希。"""
        try:
            spec = decode_job_header(header)
        except (ValueError, UnicodeError):
            raise GatewayError(400, "bad_request", "X-Onto-Job 头不对") from None
        operation = spec.get("operation")
        if operation not in ("snapshot", "discover", "sample"):
            raise GatewayError(400, "bad_request", "operation 要是 snapshot、discover 或 sample")
        claims = self.verify(token, operation, scope_hash(spec))
        status = self.jobs.create(spec, body, length)
        _log.info("采集任务 %s 票据 job=%s", spec.get("job_id"), claims.get("job"))
        return 202, status

    def job_status(self, token: str, job_id: str) -> tuple[int, dict]:
        self.verify(token, "job", job_scope(job_id))
        return 200, self.jobs.get(job_id).status()

    def cancel_job(self, token: str, job_id: str) -> tuple[int, dict]:
        self.verify(token, "job", job_scope(job_id))
        return 200, self.jobs.cancel(job_id)

    def delete_job(self, token: str, job_id: str) -> tuple[int, dict]:
        self.verify(token, "job", job_scope(job_id))
        return 200, self.jobs.delete(job_id)

    def job_bundle(self, token: str, job_id: str):
        """回 (包里的文件, tar 总字节数, 任务)；调用方先发头再 `bundle.write_tar`。"""
        self.verify(token, "job", job_scope(job_id))
        return self.jobs.bundle(job_id)

    # ---- 凭据与测试连接（证据采集接口 §8）----

    def put_secret(self, token: str, source_id: str, body: dict) -> tuple[int, dict]:
        fields = body.get("fields")
        if not isinstance(fields, dict):
            raise GatewayError(400, "bad_request", "请求体要是 {\"fields\": {…}}")
        self.verify(token, "secret", scope_hash({"source": source_id, "fields": sorted(fields)}))
        return 200, self.secrets.put(source_id, fields)

    def delete_secret(self, token: str, source_id: str) -> tuple[int, dict]:
        self.verify(token, "secret", scope_hash({"source": source_id, "delete": True}))
        if not self.secrets.ready():
            raise GatewayError(503, "secrets_unavailable", "网关没有配置主密钥")
        return 200, self.secrets.delete(source_id)

    def test_source(self, token: str, source_id: str, spec: dict) -> tuple[int, dict]:
        """请求体是 TestSpec `{source: {id, provider, connection}, cred_ref, egress, rate_limit}`，票据范围是它的哈希。"""
        self.verify(token, "test", scope_hash(spec))
        source = spec.get("source")
        if not isinstance(source, dict) or source.get("id") != source_id:
            raise GatewayError(400, "bad_request", "source.id 和路径里的数据源不一致")
        provider = self.providers_by_id.get(str(source.get("provider") or ""))
        if provider is None or getattr(provider, "input", "") != "connection":
            raise GatewayError(409, "provider_not_installed", "网关里没有这个连接型提供方")
        if not self.secrets.ready():
            raise GatewayError(503, "secrets_unavailable", "网关没有配置主密钥")
        if not provider.installed():
            raise GatewayError(409, "provider_not_installed", "网关里没有装这个提供方的驱动")
        jobs.check_connection(provider, {**spec, "operation": "test"})
        if not self._tests.acquire(blocking=False):
            raise GatewayError(503, "busy", "连接器网关繁忙")
        try:
            started = time.time()
            reply = provider.test(spec)
        finally:
            self._tests.release()
        _log.info("测试连接 source=%s provider=%s ok=%s %.1fs", source_id, provider.id, reply["ok"],
                  time.time() - started)
        return 200, reply

    def convert(self, token: str, data: bytes) -> tuple[int, dict]:
        try:
            claims = self.verify(token, "convert", sha256_hex(data))
        except GatewayError as exc:
            return exc.status, exc.payload()
        converter = self.converters.get(str(claims.get("converter") or ""))
        if converter is None:
            return 404, {"error": "converter_not_found", "message": "网关里没有这个转换器"}
        budget = claims.get("budget") if isinstance(claims.get("budget"), dict) else {}
        if len(data) > min(int(budget.get("max_bytes") or MAX_INPUT_BYTES), MAX_INPUT_BYTES):
            return 413, {"error": "too_large", "message": "文件超过上限"}
        timeout = min(float(budget.get("max_duration_s") or self.job_limits.timeout_s), self.job_limits.timeout_s)
        limits = runner.JobLimits(timeout_s=timeout, memory_bytes=self.job_limits.memory_bytes,
                                  max_output_bytes=self.job_limits.max_output_bytes)
        with self.slot() as acquired:
            if not acquired:
                return 503, {"error": "busy", "message": "连接器网关繁忙"}
            started = time.time()
            result = runner.run(converter, data, str(claims.get("filename") or ""), ConvertLimits(), limits)
        _log.info("转换 job=%s converter=%s@%s bytes=%d state=%s %.1fs", claims.get("job"), converter.info.id,
                  converter.info.version, len(data), result.get("state"), time.time() - started)
        return 200, {**result, "job": claims.get("job"),
                     "converter": {"id": converter.info.id, "version": converter.info.version}}


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "onto-connector-gateway"
        timeout = READ_TIMEOUT_SECONDS

        def log_message(self, format, *args):
            _log.debug("%s %s", self.address_string(), format % args)

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _token(self) -> str:
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                raise GatewayError(401, "ticket_invalid", "缺少票据")
            return auth[len("Bearer "):].strip()

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, gateway.health())
            elif self.path == "/v1/providers":
                self._reply(200, gateway.providers())
            else:
                self._jobs("GET")

        def do_POST(self):
            test = _TEST_PATH.match(self.path)
            if self.path == "/v1/convert":
                self._convert()
            elif test:
                self._json_route(lambda token, body: gateway.test_source(token, test.group(1), body))
            else:
                self._jobs("POST")

        def do_DELETE(self):
            match = _SECRET_PATH.match(self.path)
            if match:
                self._json_route(lambda token, _body: gateway.delete_secret(token, match.group(1)), body=False)
            else:
                self._jobs("DELETE")

        def do_PUT(self):
            match = _SECRET_PATH.match(self.path)
            if match:
                self._json_route(lambda token, body: gateway.put_secret(token, match.group(1), body))
            else:
                self._reply(404, {"error": "not_found"})

        def _json_route(self, action, *, body: bool = True):
            try:
                token = self._token()
                payload = self._json_body() if body else {}
                self._reply(*action(token, payload))
            except GatewayError as exc:
                self._reply(exc.status, exc.payload())

        def _json_body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_JSON_BODY:
                raise GatewayError(400, "bad_request", "请求体要是不超过 64 KiB 的 JSON 对象")
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise GatewayError(400, "bad_request", "请求体不是 JSON") from None
            if not isinstance(data, dict):
                raise GatewayError(400, "bad_request", "请求体要是 JSON 对象")
            return data

        def _convert(self):
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                self._reply(401, {"error": "ticket_invalid", "message": "缺少票据"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_INPUT_BYTES:
                self._reply(413, {"error": "too_large", "message": "文件超过上限"})
                return
            if length <= 0:
                self._reply(400, {"error": "empty", "message": "请求体为空"})
                return
            data = self.rfile.read(length)
            self._reply(*gateway.convert(auth[len("Bearer "):].strip(), data))

        def _jobs(self, method: str):
            match = _JOB_PATH.match(self.path)
            action = match.group(2) if match else None
            routes = {("GET", None): gateway.job_status, ("POST", "/cancel"): gateway.cancel_job,
                      ("DELETE", None): gateway.delete_job}
            try:
                if self.path == "/v1/jobs" and method == "POST":
                    self._submit()
                elif match and (method, action) == ("GET", "/bundle"):
                    self._bundle(match.group(1))
                elif match and (method, action) in routes:
                    self._reply(*routes[(method, action)](self._token(), match.group(1)))
                else:
                    self._reply(404, {"error": "not_found"})
            except GatewayError as exc:
                self._reply(exc.status, exc.payload())

        def _submit(self):
            try:
                length = int(self.headers.get("Content-Length") or -1)
            except ValueError:
                length = -1
            if length < 0:
                raise GatewayError(400, "size_mismatch", "缺少 Content-Length")
            body = _CountingReader(self.rfile)
            try:
                self._reply(*gateway.submit_job(self._token(), self.headers.get("X-Onto-Job") or "", body, length))
            except GatewayError:
                # 没读完请求体就回错误时，对方还在发，直接关连接它多半只看到连接被重置；不大的先读掉再回
                body.drain(length - body.consumed)
                raise

        def _bundle(self, job_id: str):
            items, size, job = gateway.job_bundle(self._token(), job_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            try:
                bundle.write_tar(self.wfile, items, lambda name: job.provider.raw(job.id, name),
                                 int(job.ended or time.time()))
            except Exception as exc:  # noqa: BLE001 头已发出：只能断开，内核按 Content-Length 发现包不完整
                _log.warning("快照包 %s 没发完：%s", job_id, exc)
                self.close_connection = True

    return Handler


class _CountingReader:
    def __init__(self, stream):
        self._stream = stream
        self.consumed = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.consumed += len(data)
        return data

    def drain(self, remaining: int) -> None:
        if not 0 < remaining <= DRAIN_LIMIT_BYTES:
            return
        try:
            while remaining > 0:
                data = self._stream.read(min(1 << 20, remaining))
                if not data:
                    return
                remaining -= len(data)
        except OSError:
            return


def serve(port: int | None = None, gateway: Gateway | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    port = port if port is not None else int(os.environ.get(PORT_ENV) or DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), make_handler(gateway or Gateway()))
    server.daemon_threads = True
    return server


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in ("app.connector_gateway", "app.evidence"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def main() -> None:
    _configure_logging()
    gateway = Gateway()
    server = serve(gateway=gateway)
    _log.info("连接器网关监听 :%s，转换器 %s", server.server_address[1],
              ", ".join(f"{item.info.id}@{item.info.version}" for item in gateway.converters.values()) or "无")
    server.serve_forever()
