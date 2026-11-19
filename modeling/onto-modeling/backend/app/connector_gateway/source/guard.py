"""子进程的审计钩子与凭据遮蔽（ADR-0086、ADR-0104；design/data-sources.md 第 8 节）。

审计钩子是纵深防御，不是沙箱：它拦 Python 里白名单外的连接、域名解析和子进程；原生扩展、ctypes 绕得过去。
信任插件的人要对它的代码负责（ADR-0104「信任方式的代价」）。
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
from urllib.parse import quote

MASK = "«已遮蔽»"




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
