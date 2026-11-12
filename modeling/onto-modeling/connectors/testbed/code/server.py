"""代码仓库连接器的测试服务：git 智能 HTTP（`git http-backend` CGI）+ Basic 认证。全部是合成代码。

- 启动时把同目录下的三个合成项目各提交成一个裸仓库 `<名字>.git`（作者、时间固定，提交号稳定）；
- 用户名 `x-access-token` 或 `oauth2`，口令要等于环境变量 `GIT_TESTBED_TOKEN`；
- 只读：关掉 receive-pack；允许按提交号取（和常见托管平台一致）；
- `/moved/<路径>` 回 301 指到 `/<路径>`，测重定向不跟；
- 给了 `--tls-cert` / `--tls-key` 就走 https（提供方生产路径只认 https）。

用法：`GIT_TESTBED_TOKEN=<随机值> python server.py --root /tmp/git-repos --port 8080`
"""

from __future__ import annotations

import argparse
import base64
import hmac
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PROJECTS = ("orders-java", "orders-python", "orders-ts")
USERNAMES = ("x-access-token", "oauth2")
TOKEN_ENV = "GIT_TESTBED_TOKEN"
COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "testbed", "GIT_AUTHOR_EMAIL": "testbed@example.invalid",
    "GIT_COMMITTER_NAME": "testbed", "GIT_COMMITTER_EMAIL": "testbed@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def _emit(text="", stream=None):
    """结果写到标准输出（报错写到标准错误）：单独的输出通道，不受应用日志配置影响。"""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    output = logging.Logger("code-testbed.out", logging.INFO)
    output.propagate = False
    output.addHandler(handler)
    output.info(text)


def git(*args: str, cwd: Path | str | None = None, env: dict | None = None) -> str:
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cwd or "/tmp"), "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C", **COMMIT_ENV, **(env or {})}
    done = subprocess.run(  # NOSONAR(S4721) 测试台：固定的 git 参数列表，不经 shell
        ["git", *args], cwd=cwd, env=base, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败：{done.stderr.strip()}")
    return done.stdout.strip()


def publish(work: Path, bare: Path) -> None:
    """把一个已提交的工作目录推成只读裸仓库。"""
    if bare.exists():
        shutil.rmtree(bare)
    git("clone", "-q", "--bare", str(work), str(bare))
    git("config", "http.receivepack", "false", cwd=bare)
    git("config", "uploadpack.allowReachableSHA1InWant", "true", cwd=bare)
    git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=bare)


def commit_tree(source: Path, bare: Path, message: str = "初始提交") -> str:
    """把 `source` 目录的内容作为一个提交发布成 `bare`，回提交号。"""
    with tempfile.TemporaryDirectory(prefix="testbed-") as tmp:
        work = Path(tmp) / "work"
        shutil.copytree(source, work)
        git("init", "-q", "-b", "main", cwd=work)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", message, cwd=work)
        publish(work, bare)
        return git("rev-parse", "HEAD", cwd=work)


def init_projects(root: Path) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    return {name: commit_tree(HERE / name, root / f"{name}.git") for name in PROJECTS}


def _http_backend() -> list[str]:
    exec_path = git("--exec-path")
    backend = Path(exec_path) / "git-http-backend"
    return [str(backend)] if backend.exists() else ["git", "http-backend"]


class Handler(BaseHTTPRequestHandler):
    server_version = "testbed-git"
    root: Path
    token: str
    log: list
    backend: list[str]

    def log_message(self, fmt, *args):  # noqa: D401 - 测试里不刷屏
        pass

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        return user in USERNAMES and bool(self.token) and hmac.compare_digest(password, self.token)

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _handle(self) -> None:
        parts = urlsplit(self.path)
        authorized = self._authorized()
        self.log.append({"method": self.command, "path": parts.path, "authorized": authorized})
        if parts.path.startswith("/moved/"):
            self.send_response(301)
            self.send_header("Location", parts.path[len("/moved"):] + (f"?{parts.query}" if parts.query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not authorized:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="testbed"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self._body() if self.command == "POST" else b""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1", "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.root),
            "REQUEST_METHOD": self.command, "PATH_INFO": parts.path, "QUERY_STRING": parts.query,
            "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "reader", "REMOTE_ADDR": self.client_address[0],
            "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
        }
        done = subprocess.run(  # NOSONAR(S4721) 测试台：固定的 git http-backend 命令，不经 shell
            self.backend, input=body, env=env, capture_output=True, check=False)
        head, sep, payload = done.stdout.partition(b"\r\n\r\n")
        if not sep:
            head, sep, payload = done.stdout.partition(b"\n\n")
        status, headers = 200, []
        for line in head.decode("latin-1").splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                headers.append((name.strip(), value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


def make_server(root: Path, token: str, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """测试用：起在线程里，`server.log` 是收到的请求（方法、路径、是否通过认证）。"""
    handler = type("BoundHandler", (Handler,), {"root": Path(root), "token": token, "log": [],
                                                "backend": _http_backend()})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.log = handler.log
    return server


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default="/srv/git")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    args = parser.parse_args()
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        _emit(f"要先设置 {TOKEN_ENV}", sys.stderr)
        return 2
    for name, commit in init_projects(Path(args.root)).items():
        _emit(f"{name}.git {commit}")
    server = make_server(Path(args.root), token, args.host, args.port)
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = "https" if args.tls_cert else "http"
    _emit(f"listening on {scheme}://{args.host}:{server.server_address[1]}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
