"""测试替身：假的 jiuwenbox 管理接口，以及跑在本机端口上的真实脚本验证网关。

假 jiuwenbox 收到 exec 时把沙箱里的 /usr/bin/python3 换成本机解释器真实执行，模拟脚本在沙箱里的运行；
网关用的是 app.sandbox_gateway 的真实实现，只把 jiuwenbox 地址指向假 jiuwenbox。
"""

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import sandbox_gateway


class FakeJiuwenbox:
    """jiuwenbox 管理接口替身：exec 时把沙箱里的 /usr/bin/python3 换成本机解释器真实执行。"""

    def __init__(self, *, create_status: int = 200, exec_status: int = 200):
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, payload=None):
                body = b"" if status == 204 else json.dumps(payload or {}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.calls.append(("GET", self.path))
                self._reply(200, {"status": "ok"})

            def do_DELETE(self):
                fake.calls.append(("DELETE", self.path))
                self._reply(204)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(("POST", self.path))
                fake.bodies.append(body)
                if self.path == "/api/v1/sandboxes":
                    self._reply(create_status, {"error": "sandbox quota exceeded"} if create_status >= 400 else {})
                    return
                if exec_status != 200:
                    self._reply(exec_status, {"error": "exec failed"})
                    return
                command = [sys.executable, *body["command"][1:]]
                # 与真实 jiuwenbox 一样只给请求里的环境变量；SYSTEMROOT 等是 Windows 开发机启动进程所需
                env = {**body.get("env", {}), **{
                    key: os.environ[key] for key in ("SYSTEMROOT", "TEMP", "TMP") if key in os.environ
                }}
                # 安全审查：argv 是本机解释器（替换沙箱里的 /usr/bin/python3）加网关写死的执行器参数，
                # 以序列传入、不经 shell 解析；只在测试里模拟 jiuwenbox 的 exec。
                done = subprocess.run(  # NOSONAR(S4721)
                    command, capture_output=True, text=True, encoding="utf-8",
                    timeout=body["timeout_seconds"], env=env, shell=False,
                )
                self._reply(200, {"exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class RunningGateway:
    """真实的脚本验证网关，监听本机随机端口，jiuwenbox 地址指向 socket_url。"""

    def __init__(self, socket_url: str, **options):
        self.gateway = sandbox_gateway.Gateway(socket_url=socket_url, **options)
        self.server = sandbox_gateway.serve(port=0, host="127.0.0.1", gateway=self.gateway)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def unused_port() -> int:
    """一个当前没人监听的本机端口：连过去立即被拒绝。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
