"""连接器网关的 HTTP 接口（详设 5.8.2；ADR-0073）。只在内部网络上监听，不映射到宿主。

- GET  /health        存活与已加载的转换器、数据源连接器数（容器健康检查、内核的健康探测）
- GET  /v1/providers  已加载的转换器（items）、数据源连接器（sources）、插件（plugins）
- POST /v1/convert    请求体是原文件字节，`Authorization: Bearer <票据>`；回 Markdown 与位置对照
- PUT/DELETE /v1/plugins/{名}/{版本}、PUT/DELETE /v1/secrets/{项目}/{引用}、POST /v1/sources/test|collect（ADR-0104，
  见 design/data-sources.md 第 5 节）

票据由内核签发（`app.evidence.tickets`），网关从内核取公钥验签（ONTO_CONNECTOR_ISSUER_KEY_URL），核对操作、原文件的
sha256、转换器，用过即作废。同时最多 MAX_CONCURRENT 个转换在跑，另有 MAX_WAITING 个可排队，再多返回 503。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

from ..evidence.convert import ConvertLimits
from ..evidence.tickets import TicketError, Verifier, sha256_hex
from . import egress as egress_check
from . import runner
from .errors import GatewayError
from .plugins import Store
from .source import render as source_render
from .source import runner as source_runner
from .vault import Vault

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
MAX_JSON_BYTES = 1024 * 1024
SOURCE_TIMEOUT_S = int(os.environ.get("ONTO_CONNECTOR_SOURCE_TIMEOUT_S") or 600)
TEST_TIMEOUT_S = 30
READ_TIMEOUT_SECONDS = 60
KEY_REFRESH_SECONDS = 10
# 内核在内部网络上，不走任何代理
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
            except (OSError, ValueError, TypeError, jwt.PyJWTError) as exc:  # OSError 含 URLError
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
                 store: Store | None = None, vault: Vault | None = None,
                 source_limits: source_runner.SourceLimits | None = None):
        # converters 只给测试用：固定一组转换器，不读插件仓库
        self._static_converters = converters
        self.store = store if store is not None else (Store() if converters is None else None)
        self.vault = vault or Vault()
        self.source_limits = source_limits or source_runner.SourceLimits(timeout_s=SOURCE_TIMEOUT_S,
                                                                         memory_bytes=JOB_MEMORY_BYTES)
        self.keys = keys or KeySource()
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

    @property
    def converters(self) -> dict:
        if self._static_converters is not None:
            return self._static_converters
        return self.store.converters() if self.store is not None else {}

    @property
    def sources(self) -> dict:
        return self.store.sources() if self.store is not None else {}

    def health(self) -> dict:
        return {"status": "ok", "converters": len(self.converters), "sources": len(self.sources)}

    def providers(self) -> dict:
        plugins = self.store.plugins() if self.store is not None else []
        return {"items": [item.info.to_dict() for item in self.converters.values()],
                "sources": [item.info.to_dict() for item in self.sources.values()],
                "plugins": [item.to_dict() for item in plugins]}

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


    # ---- 插件（ADR-0104）----

    def _claims(self, token: str, operation: str, scope: str) -> dict:
        return self.verify(token, operation, scope)

    def install_plugin(self, token: str, name: str, version: str, data: bytes) -> tuple[int, dict]:
        try:
            if self.store is None:
                raise GatewayError(503, "plugin_store_unavailable", "网关没有插件仓库")
            digest = sha256_hex(data)
            claims = self._claims(token, "plugin_install", digest)
            if claims.get("plugin") != f"{name}@{version}":
                raise GatewayError(401, "ticket_invalid", "票据不是给这个插件的")
            plugin = self.store.install(name, version, data, digest)
        except GatewayError as exc:
            return exc.status, exc.payload()
        return 200, {"plugin": plugin.to_dict()}

    def remove_plugin(self, token: str, name: str, version: str) -> tuple[int, dict]:
        try:
            if self.store is None:
                raise GatewayError(503, "plugin_store_unavailable", "网关没有插件仓库")
            self._claims(token, "plugin_remove", sha256_hex(f"{name}@{version}".encode("utf-8")))
            removed = self.store.remove(name, version)
        except GatewayError as exc:
            return exc.status, exc.payload()
        return 200, {"removed": removed}

    # ---- 凭据 ----

    def put_secret(self, token: str, project: str, ref: str, data: bytes) -> tuple[int, dict]:
        try:
            claims = self._claims(token, "secret", sha256_hex(data))
            if claims.get("project") != project:
                raise GatewayError(401, "ticket_invalid", "票据不是给这个项目的")
            body = _json_body(data)
            fields = self.vault.put(project, ref, body.get("fields"))
        except GatewayError as exc:
            return exc.status, exc.payload()
        return 200, {"secret_fields": fields}

    def delete_secret(self, token: str, project: str, ref: str) -> tuple[int, dict]:
        try:
            claims = self._claims(token, "secret", sha256_hex(f"delete:{project}/{ref}".encode("utf-8")))
            if claims.get("project") != project:
                raise GatewayError(401, "ticket_invalid", "票据不是给这个项目的")
            deleted = self.vault.delete(project, ref)
        except GatewayError as exc:
            return exc.status, exc.payload()
        return 200, {"deleted": deleted}

    # ---- 数据源 ----

    def _prepare(self, token: str, operation: str, data: bytes):
        claims = self._claims(token, operation, sha256_hex(data))
        body = _json_body(data)
        project = str(body.get("project") or "")
        if not project or claims.get("project") != project:
            raise GatewayError(401, "ticket_invalid", "票据不是给这个项目的")
        connector = self.sources.get(str(body.get("connector") or ""))
        if connector is None:
            raise GatewayError(404, "connector_not_found", "网关里没有这个连接器")
        from ..evidence.sources import coerce_config, egress

        stored = ()
        secret: dict = {}
        ref = str(body.get("secret") or "")
        if ref:
            found = self.vault.read(project, ref)
            if found is None and not body.get("secrets"):
                # 引用在这个项目下没有凭据（导入、复制来的数据源资料，或凭据被删了）
                raise GatewayError(409, "secret_missing", "凭据没有存，要重新填写")
            secret = dict(found or {})
            stored = tuple(secret)
        config, given, errors = coerce_config(connector.info, body.get("config"), body.get("secrets"),
                                              stored_secrets=stored)
        if errors:
            raise GatewayError(400, "config_invalid", "、".join(f"{key}:{why}" for key, why in sorted(errors.items())))
        if operation == "source_collect" and given:
            raise GatewayError(400, "config_invalid", "采集不收这次填的凭据，先写进凭据库")
        missing = [key for key in connector.info.secret_keys if key not in secret and key not in given
                   and any(item.key == key and item.required for item in connector.info.fields)]
        if missing:
            raise GatewayError(409, "secret_missing", "凭据没有存，要重新填写")
        secret.update(given)
        allowed = egress(connector.info, config)
        egress_check.ensure_allowed(allowed)
        return project, connector, config, secret, allowed

    def _run_source(self, operation: str, token: str, data: bytes) -> tuple[int, dict]:
        try:
            project, connector, config, secret, allowed = self._prepare(token, f"source_{operation}", data)
        except GatewayError as exc:
            return exc.status, exc.payload()
        timeout = TEST_TIMEOUT_S if operation == "test" else self.source_limits.timeout_s
        limits = source_runner.SourceLimits(timeout_s=timeout, memory_bytes=self.source_limits.memory_bytes,
                                            max_event_bytes=self.source_limits.max_event_bytes,
                                            qps=self.source_limits.qps,
                                            query_timeout_s=self.source_limits.query_timeout_s)
        with self.slot() as acquired:
            if not acquired:
                return 503, {"error": "busy", "message": "连接器网关繁忙"}
            started = time.time()
            outcome = source_runner.run(connector.entry_path, connector.vendor_path, operation, config, secret,
                                        allowed, limits)
        identity = {"id": connector.info.id, "version": connector.info.version}
        _log.info("数据源 %s project=%s connector=%s@%s ok=%s error=%s %.1fs", operation, project, connector.info.id,
                  connector.info.version, outcome.ok, outcome.error_code or "-", time.time() - started)
        error = {"error_code": outcome.error_code, "error_message": outcome.error_message} if not outcome.ok else {}
        if operation == "test":
            ok = outcome.ok and bool(outcome.checks) and all(item["ok"] for item in outcome.checks)
            return 200, {"ok": ok, "checks": outcome.checks, "connector": identity, **error}
        if not outcome.ok:
            return 200, {"state": "failed", "connector": identity, **error}
        markdown, stats, limitations = source_render.render(outcome.collected, self.vault.mask_salt(project))
        return 200, {"state": "converted", "markdown": markdown, "stats": stats, "limitations": limitations,
                     "connector": identity}

    def source_test(self, token: str, data: bytes) -> tuple[int, dict]:
        return self._run_source("test", token, data)

    def source_collect(self, token: str, data: bytes) -> tuple[int, dict]:
        return self._run_source("collect", token, data)


def _json_body(data: bytes) -> dict:
    try:
        body = json.loads(data.decode("utf-8"))
    except ValueError:  # 含 UnicodeDecodeError
        raise GatewayError(400, "bad_request", "请求体不是 JSON") from None
    if not isinstance(body, dict):
        raise GatewayError(400, "bad_request", "请求体要是对象")
    return body


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

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, gateway.health())
            elif self.path == "/v1/providers":
                self._reply(200, gateway.providers())
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self):
            if self.path == "/v1/convert":
                self._convert()
            elif self.path == "/v1/sources/test":
                self._with_body(MAX_JSON_BYTES, gateway.source_test)
            elif self.path == "/v1/sources/collect":
                self._with_body(MAX_JSON_BYTES, gateway.source_collect)
            else:
                self._reply(404, {"error": "not_found"})

        def do_PUT(self):
            parts = self._parts()
            if len(parts) == 4 and parts[1] == "plugins":
                self._with_body(_PLUGIN_MAX, lambda token, data: gateway.install_plugin(token, parts[2], parts[3], data))
            elif len(parts) == 4 and parts[1] == "secrets":
                self._with_body(MAX_JSON_BYTES, lambda token, data: gateway.put_secret(token, parts[2], parts[3], data))
            else:
                self._reply(404, {"error": "not_found"})

        def do_DELETE(self):
            parts = self._parts()
            token = self._token()
            if token is None:
                return
            if len(parts) == 4 and parts[1] == "plugins":
                self._reply(*gateway.remove_plugin(token, parts[2], parts[3]))
            elif len(parts) == 4 and parts[1] == "secrets":
                self._reply(*gateway.delete_secret(token, parts[2], parts[3]))
            else:
                self._reply(404, {"error": "not_found"})

        def _parts(self) -> list[str]:
            # /v1/<资源>/<a>/<b>：不解码百分号，名字、版本、项目、引用都只用安全字符
            path = self.path.split("?", 1)[0]
            parts = path.strip("/").split("/")
            return parts if parts and parts[0] == "v1" else []

        def _token(self) -> str | None:
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("Bearer "):
                self._reply(401, {"error": "ticket_invalid", "message": "缺少票据"})
                return None
            return auth[len("Bearer "):].strip()

        def _with_body(self, limit: int, handler) -> None:
            token = self._token()
            if token is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > limit:
                self._reply(413, {"error": "too_large", "message": "请求体超过上限"})
                return
            if length <= 0:
                self._reply(400, {"error": "empty", "message": "请求体为空"})
                return
            self._reply(*handler(token, self.rfile.read(length)))

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

    return Handler


_PLUGIN_MAX = 32 * 1024 * 1024


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
    # 启动时先取一次内核公钥：第一张票据到的时候内核可能正忙着等网关回话，这时再回调取钥就要等它
    threading.Thread(target=gateway.keys.verifier, args=("",), daemon=True, name="issuer-key-prefetch").start()
    server = serve(gateway=gateway)
    _log.info("连接器网关监听 :%s，转换器 %s，数据源连接器 %s", server.server_address[1],
              ", ".join(f"{item.info.id}@{item.info.version}" for item in gateway.converters.values()) or "无",
              ", ".join(f"{item.info.id}@{item.info.version}" for item in gateway.sources.values()) or "无")
    server.serve_forever()
