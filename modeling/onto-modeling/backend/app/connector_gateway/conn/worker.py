"""连接型提供方的子进程（证据采集接口 §8）：`python -m app.connector_gateway.conn.worker`，由 `conn.runner` 起。

标准输入：第一行 JSON 头（任务说明字段，外加运行器给的 `module`、`scheme`、`executables`、`limits`），第二行凭据 JSON，然后关闭。
标准输出：一行一个事件，只经 `conn.context.Context` 写；提供方直接 print 的东西进标准错误，不会混进事件流。

启动顺序：读完输入 → 设资源上限 → 解析出口白名单里的主机 → 装审计钩子 → 导入提供方模块 → 调 `<operation>(ctx)`。
审计钩子是纵深防御，不是沙箱：它拦 Python 里白名单外的连接、域名解析和子进程；白名单内的可执行文件（如 git）自己发起的
连接不经过它，由提供方用参数约束（只许 https、不跟重定向等）。
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import os
import shutil
import socket
import sys
from urllib.parse import quote

from .context import BudgetExceeded, ConnError, Context

MASK = "«已遮蔽»"


def _limit(limits: dict) -> None:
    try:
        import resource
    except ImportError:  # 非 POSIX
        return
    for name, value in (("RLIMIT_AS", limits.get("memory_bytes")), ("RLIMIT_CPU", limits.get("cpu_seconds")),
                        ("RLIMIT_FSIZE", limits.get("file_bytes")), ("RLIMIT_CORE", 0)):
        kind = getattr(resource, name, None)
        if kind is None or value is None:
            continue
        try:
            resource.setrlimit(kind, (int(value), int(value)))
        except (ValueError, OSError):  # macOS 不支持 RLIMIT_AS；容器里由 Linux 生效
            pass


def host_key(host) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    text = str(host or "").strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).compressed
    except ValueError:
        return text


def _port(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


class Guard:
    """审计钩子：出口白名单（主机、端口和启动时解析出的 IP）与可执行文件白名单。"""

    WATCHED = frozenset({
        "socket.getaddrinfo", "socket.connect", "socket.sendto", "socket.gethostbyname", "socket.gethostbyaddr",
        "socket.getnameinfo", "subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn", "os.system", "os.fork",
        "os.forkpty", "pty.spawn",
    })

    def __init__(self, egress: list[dict], executables: list[str], *, resolve=socket.getaddrinfo, which=shutil.which):
        self.pairs: set[tuple[str, int]] = set()
        self.hosts: set[str] = set()
        for item in egress:
            host, port = host_key(item.get("host")), _port(item.get("port"))
            if not host or port is None or port <= 0:
                continue
            self.hosts.add(host)
            self.pairs.add((host, port))
            try:
                infos = resolve(host, port, 0, socket.SOCK_STREAM)
            except (OSError, UnicodeError):
                continue
            for info in infos:
                address = host_key(info[4][0])
                self.hosts.add(address)
                self.pairs.add((address, port))
        self.executables = {os.path.realpath(path) for path in map(which, executables) if path}
        # (错误码, 被拦的是什么)：egress_denied 是网络，exec_denied 是子进程
        self.denied: list[tuple[str, str]] = []

    def _deny(self, what: str, code: str = "egress_denied"):
        self.denied.append((code, what[:200]))
        raise PermissionError(f"不在白名单里：{what[:200]}")

    def _check_address(self, address) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            self._deny("非 IP 套接字")
        host, port = host_key(address[0]), _port(address[1])
        if (host, port) not in self.pairs:
            self._deny(f"{host}:{port}")

    def _check_executable(self, executable, args) -> None:
        if executable is None and args:
            executable = args[0] if isinstance(args, (list, tuple)) else args
        try:
            name = os.fsdecode(executable)
        except TypeError:
            self._deny("子进程", "exec_denied")
        path = name if os.sep in name else (shutil.which(name) or name)
        if os.path.realpath(path) not in self.executables:
            self._deny(f"子进程 {os.path.basename(name)}", "exec_denied")

    def __call__(self, event: str, args: tuple) -> None:
        if event not in self.WATCHED:
            return
        if event == "socket.getaddrinfo":
            host, port = host_key(args[0]), _port(args[1])
            if host not in self.hosts or (port is not None and (host, port) not in self.pairs):
                self._deny(f"解析 {host}:{port}")
        elif event in ("socket.connect", "socket.sendto"):
            self._check_address(args[1])
        elif event == "socket.gethostbyname":
            if host_key(args[0]) not in self.hosts:
                self._deny(f"解析 {host_key(args[0])}")
        elif event in ("socket.gethostbyaddr", "socket.getnameinfo"):
            self._deny("反向解析")
        elif event == "subprocess.Popen":
            self._check_executable(args[0], args[1])
        elif event in ("os.exec", "os.posix_spawn"):
            self._check_executable(args[0], args[1])
        elif event == "os.spawn":
            self._check_executable(args[1], args[2])
        else:
            self._deny(event, "exec_denied")


def scrub(text: str, secret: dict, minimum: int = 3, *, json_text: bool = False) -> str:
    """把凭据取值（原文或 URL 编码写法）换成遮蔽标记。`json_text`：text 是一行 JSON，按 JSON 转义后的写法找，
    换完仍是合法 JSON。"""
    for value in secret.values():
        if not isinstance(value, str) or len(value) < minimum:
            continue
        written = {json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]} if json_text else {value}
        for variant in written | {quote(value, safe="")}:
            if variant:
                text = text.replace(variant, MASK)
    return text


def _coverage(ctx: Context) -> float:
    done, total = ctx.last_progress
    return round(100.0 * min(done, total) / total, 1) if total > 0 else 0.0


def _denied(ctx: Context, guard: Guard) -> None:
    code, what = guard.denied[0]
    ctx.error(code, f"不在白名单里：{what}")


def main() -> int:
    stream = sys.stdin.buffer
    head = json.loads(stream.readline().decode("utf-8"))
    secret_line = stream.readline()
    stream.close()
    secret = json.loads(secret_line.decode("utf-8")) if secret_line.strip() else {}
    del secret_line
    _limit(head.get("limits") or {})
    out = sys.stdout
    out.reconfigure(encoding="utf-8")
    # 提供方或驱动直接 print 的东西不能混进事件流
    sys.stdout = sys.stderr
    ctx = Context(head, secret, out, scheme=str(head.get("scheme") or ""))
    guard = Guard(ctx.egress, [str(item) for item in head.get("executables") or []])
    sys.addaudithook(guard)
    try:
        module = importlib.import_module(str(head["module"]))
        getattr(module, ctx.operation)(ctx)
        if guard.denied:
            # 提供方吞掉了被拦的异常也算失败：它试图连白名单外的地方或起白名单外的程序
            _denied(ctx, guard)
    except BudgetExceeded:
        ctx.result(complete=False, coverage_pct=_coverage(ctx))
    except ConnError as exc:
        if guard.denied:
            _denied(ctx, guard)
        else:
            ctx.error(exc.code, scrub(exc.message, secret))
    except MemoryError:
        ctx.error("memory", "超出内存上限")
    except Exception as exc:  # noqa: BLE001 异常原文可能带连接串、密码：只报类型
        if guard.denied:
            _denied(ctx, guard)
        else:
            ctx.error("provider_error", type(exc).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
