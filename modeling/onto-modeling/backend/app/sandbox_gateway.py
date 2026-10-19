"""脚本验证网关：唯一持有 jiuwenbox 管理 socket 的进程，跑在单独的无特权容器里。

jiuwenbox 的管理接口没有鉴权，谁连上 socket 谁就能决定沙箱怎么建（挂哪些目录、能否出网）。
onto-modeling 主容器里跑着能执行任意命令的建模 agent，所以 socket 不挂进主容器，只挂给本网关；
主容器经本网关的 HTTP 接口运行脚本验证（ONTO_WEB_SANDBOX_GATEWAY_URL）。

网关只做一件事：把调用方打包好的脚本和用例，交给 jiuwenbox_client.run_bundle 按写死的策略、
解释器、执行器和超时在一次性沙箱里运行。调用方决定不了这些参数，也拿不到 socket。网关自己
从不执行脚本：jiuwenbox 不可用时返回 503，由调用方回退到它自己的容器内执行。

接口（只在 onto-modeling 的内部网络上监听，不映射到宿主）：
- GET  /health      网关存活（容器健康检查），不碰 jiuwenbox
- GET  /v1/status   jiuwenbox 是否可用与沙箱 Python 环境（配置中心「沙箱」）
- POST /v1/verify   {"bundle", "entry", "cases", "case_count"} → {"results": [...]}

同时最多 MAX_CONCURRENT 个沙箱在跑，另有 MAX_WAITING 个请求可排队；再多直接返回 503。
启动：python -m app.sandbox_gateway（与主服务同一镜像，不读 onto-modeling 的数据卷与 .env）。
仅依赖标准库。
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

_log = logging.getLogger(__name__)

PORT_ENV = "ONTO_WEB_SANDBOX_GATEWAY_PORT"
DEFAULT_PORT = 8110
MAX_CONCURRENT = 2
MAX_WAITING = 8
MAX_BODY_BYTES = 256 * 1024
MAX_CASES = 16
# 读请求的套接字超时：慢速发送的连接不能一直占着处理线程
READ_TIMEOUT_SECONDS = 30
ENTRY_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\.py")


def _load_client():
    path = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts" / "jiuwenbox_client.py"
    spec = importlib.util.spec_from_file_location("onto_sandbox_gateway_jiuwenbox_client", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


client = _load_client()


class RequestRejected(ValueError):
    """请求体不合法：返回 400，不会送进沙箱。"""


def parse_verify_request(body) -> tuple[str, str, str, int]:
    """校验运行验证请求：只接受打包好的脚本、入口文件名与用例，不接受任何执行参数。"""
    if not isinstance(body, dict) or set(body) != {"bundle", "entry", "cases", "case_count"}:
        raise RequestRejected("请求必须且只能包含 bundle、entry、cases、case_count")
    bundle, entry, cases, count = body["bundle"], body["entry"], body["cases"], body["case_count"]
    if not all(isinstance(value, str) and value for value in (bundle, entry, cases)):
        raise RequestRejected("bundle、entry、cases 必须是非空字符串")
    if not ENTRY_PATTERN.fullmatch(entry):
        raise RequestRejected("entry 必须是脚本文件名（如 refund_check.py）")
    if type(count) is not int or not 1 <= count <= MAX_CASES:
        raise RequestRejected(f"case_count 必须是 1~{MAX_CASES} 的整数")
    for name, value in (("bundle", bundle), ("cases", cases)):
        if len(value) > client.MAX_ARGUMENT_BYTES:
            raise RequestRejected(f"{name} 超过沙箱参数上限 {client.MAX_ARGUMENT_BYTES // 1024}KB")
    return bundle, entry, cases, count


class Gateway:
    def __init__(
        self,
        *,
        socket_url: str | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        max_waiting: int = MAX_WAITING,
        wait_seconds: float | None = None,
    ):
        self.socket_url = socket_url or client.default_url()
        self._running = threading.BoundedSemaphore(max_concurrent)
        self._admitted = threading.BoundedSemaphore(max_concurrent + max_waiting)
        self._wait_seconds = client.GATEWAY_WAIT_SECONDS if wait_seconds is None else wait_seconds

    @contextmanager
    def slot(self):
        """拿到执行位时产出 True；排队已满或等不到执行位时产出 False。"""
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

    def status(self) -> tuple[int, dict]:
        with self.slot() as acquired:
            if not acquired:
                return 503, {"error": "脚本验证网关繁忙，稍后重新检测"}
            return 200, client.probe(self.socket_url)

    def verify(self, body) -> tuple[int, dict]:
        try:
            bundle, entry, cases, count = parse_verify_request(body)
        except RequestRejected as exc:
            return 400, {"error": str(exc)}
        with self.slot() as acquired:
            if not acquired:
                return 503, {"error": "脚本验证网关繁忙，本次改在 onto-modeling 容器内执行"}
            try:
                results = client.run_bundle(bundle, entry, cases, count, url=self.socket_url)
            except (client.SandboxUnavailable, client.SandboxError) as exc:
                return 503, {"error": str(exc)}
        return 200, {"results": results}


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "onto-sandbox-gateway"
        timeout = READ_TIMEOUT_SECONDS

        def log_message(self, format, *args):
            _log.info("%s %s", self.address_string(), format % args)

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
            elif self.path == "/v1/status":
                self._reply(*gateway.status())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/verify":
                self._reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY_BYTES:
                self._reply(413, {"error": "请求体过大"})
                return
            if length <= 0:
                self._reply(400, {"error": "请求体为空"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:  # 含 UnicodeDecodeError 与 JSONDecodeError
                self._reply(400, {"error": "请求体不是 JSON"})
                return
            self._reply(*gateway.verify(body))

    return Handler


def serve(port: int | None = None, gateway: Gateway | None = None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    port = port if port is not None else int(os.environ.get(PORT_ENV) or DEFAULT_PORT)
    server = ThreadingHTTPServer((host, port), make_handler(gateway or Gateway()))
    server.daemon_threads = True
    return server


def _configure_logging() -> None:
    """只给本模块的 logger 接上 stderr：网关独立运行，没有 uvicorn 替它配置日志，不动根 logger。"""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.handlers.clear()
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def main() -> None:
    _configure_logging()
    server = serve()
    _log.info("脚本验证网关监听 :%s，jiuwenbox socket %s", server.server_address[1], client.default_url())
    server.serve_forever()


if __name__ == "__main__":
    main()
