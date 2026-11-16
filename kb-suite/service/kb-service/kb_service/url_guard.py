"""URL imports: refuse internal addresses (SSRF) and keep internal addresses and host names out of error messages."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import re
import socket
import threading
from urllib.parse import urlsplit

RESOLVE_TIMEOUT_S = 5.0
BLOCKED_MESSAGE = "不允许导入内部网络地址：该网址指向本机、内网或保留地址"
FETCH_BLOCKED_MESSAGE = "网址指向内部网络地址，已拒绝抓取"
UNRESOLVABLE_MESSAGE = "无法解析网址里的域名（域名不存在，或服务器当前无法解析外部域名），请检查网址后重试"
HIDDEN_ADDRESS = "<内部地址>"
HIDDEN_HOST = "<内部主机>"
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
# Characters some parsers read differently from urlsplit: "http://127.0.0.1\@x/" is host x
# here but 127.0.0.1 to a fetcher that takes the backslash for a slash.
_AMBIGUOUS = re.compile(r"[\\\x00-\x20\x7f]")
# Numeric host forms that inet_aton accepts besides dotted quads (127.1, 2130706433, 0x7f.0.0.1).
_NUMERIC_HOST = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.IGNORECASE)
# IPv6 ranges that carry an IPv4 address in their last 32 bits (NAT64, IPv4-compatible),
# and the local-use NAT64 prefix, which only ever leads inside.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_LOCAL_NAT64 = ipaddress.ip_network("64:ff9b:1::/48")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Za-z:])")
_FETCH_BLOCKED = re.compile(r"restricted ip|private ip|ssrf|loopback|link-local|internal ip|reserved ip", re.IGNORECASE)
# Where fetch errors name a host: host='x', host name "x", Failed to resolve 'x', lookup x, x:8080.
_HOST_MENTIONS = (
    re.compile(r"(?P<pre>\bhost(?:\s*name)?\s*[=:]\s*['\"]?)(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    re.compile(r"(?P<pre>\bhost(?:\s*name)?\s+['\"])(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    re.compile(r"(?P<pre>\b(?:resolve|resolving|lookup)\s+['\"]?)(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)", re.IGNORECASE),
    re.compile(r"(?P<pre>(?<![\w.-]))(?P<host>[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*)(?=:\d{2,5}(?!\d))"),
)

_dns_lock = threading.Lock()
_dns_pool: concurrent.futures.ThreadPoolExecutor | None = None


class UrlRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address leads to: mapped, 6to4, Teredo (server and client), NAT64, compatible."""

    found = [v4 for v4 in (ip.ipv4_mapped, ip.sixtofour) if v4 is not None]
    if ip.teredo:
        found.extend(ip.teredo)
    if ip in _NAT64 or ip in _IPV4_COMPATIBLE:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def _reachable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not ip.is_multicast


def is_public_address(value: str) -> bool:
    """A globally reachable unicast address, and so is every IPv4 address it embeds."""

    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    candidates: list = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _LOCAL_NAT64:
            return False
        candidates.extend(_embedded_ipv4(ip))
    return all(_reachable(c) for c in candidates)


def _literal_address(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.split("%", 1)[0]))
    except ValueError:
        pass
    if _NUMERIC_HOST.match(host):
        try:
            return socket.inet_ntoa(socket.inet_aton(host))
        except OSError:
            return None
    return None


def _resolver() -> concurrent.futures.ThreadPoolExecutor:
    # own threads: the loop's default pool can be busy with file reads
    global _dns_pool
    with _dns_lock:
        if _dns_pool is None:
            _dns_pool = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="kb-dns")
        return _dns_pool


async def _resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    lookup = loop.run_in_executor(_resolver(), socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    infos = await asyncio.wait_for(lookup, RESOLVE_TIMEOUT_S)
    return [info[4][0] for info in infos]


async def check_public_url(url: str) -> None:
    """Raise UrlRejected unless ``url`` is a plain http(s) URL whose host resolves to public addresses only.

    The message never names an address. A name that cannot be resolved is
    refused too: whether it leads inside cannot be told.
    """

    if _AMBIGUOUS.search(url):
        raise UrlRejected("bad_url", "网址里不能有反斜杠、空格或控制字符")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UrlRejected("bad_url", "只支持 http/https 地址")
    if "@" in parts.netloc:
        raise UrlRejected("bad_url", "网址里不能带用户名或密码（@）")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UrlRejected("bad_url", "网址缺少主机名")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlRejected("bad_url", "网址里的端口无效") from exc
    if host in _LOCAL_NAMES or host.endswith(".localhost"):
        raise UrlRejected("url_not_allowed", BLOCKED_MESSAGE)
    literal = _literal_address(host)
    if literal is not None:
        if not is_public_address(literal):
            raise UrlRejected("url_not_allowed", BLOCKED_MESSAGE)
        return
    try:
        addresses = await _resolve(host, port)
    except (OSError, UnicodeError, asyncio.TimeoutError) as exc:
        raise UrlRejected("url_unresolvable", UNRESOLVABLE_MESSAGE) from exc
    if not addresses or any(not is_public_address(address) for address in addresses):
        raise UrlRejected("url_not_allowed", BLOCKED_MESSAGE)


def safe_fetch_error(message: str, url: str = "") -> str:
    """A fetch error fit for users: no internal addresses or host names, no SSRF filter details.

    The host of ``url`` itself, which the user typed, is kept.
    """

    if _FETCH_BLOCKED.search(message):
        return FETCH_BLOCKED_MESSAGE
    own_host = (urlsplit(url).hostname or "").rstrip(".").lower() if url else ""

    def hide_address(match: re.Match) -> str:
        text = match.group(0)
        try:
            ipaddress.ip_address(text)
        except ValueError:
            return text
        return text if is_public_address(text) else HIDDEN_ADDRESS

    def hide_host(match: re.Match) -> str:
        host = match.group("host")
        if host.rstrip(".").lower() == own_host or _literal_address(host) is not None:
            return match.group(0)
        return match.group("pre") + HIDDEN_HOST

    message = _IPV6.sub(hide_address, _IPV4.sub(hide_address, message))
    for pattern in _HOST_MENTIONS:
        message = pattern.sub(hide_host, message)
    return message
