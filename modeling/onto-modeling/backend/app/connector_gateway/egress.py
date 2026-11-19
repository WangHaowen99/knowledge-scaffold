"""数据源的出口检查（ADR-0103；design/data-sources.md 第 5 节）：连接目标不能是网关自己、内核或内部网络里的服务。

业务库通常在内网，私有网段照常放行；拒绝的是回环、链路本地（含云主机元数据地址）、未指定、组播，以及内部服务名
和它们解析出的地址。内部服务名默认是部署里的几个服务，`ONTO_CONNECTOR_EGRESS_DENY`（逗号分隔）可追加；
内核的主机名取自取公钥的地址。解析不了的主机放行：连不上会由插件报连接失败。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from .errors import GatewayError

DENY_ENV = "ONTO_CONNECTOR_EGRESS_DENY"
INTERNAL_SERVICES = ("onto-modeling", "connector-gateway", "sandbox-gateway", "auth-server", "localhost")


def _internal_names() -> set[str]:
    names = {name.lower() for name in INTERNAL_SERVICES}
    names |= {item.strip().lower() for item in (os.environ.get(DENY_ENV) or "").split(",") if item.strip()}
    issuer = urlparse(os.environ.get("ONTO_CONNECTOR_ISSUER_KEY_URL") or "").hostname
    if issuer:
        names.add(issuer.lower())
    try:
        names.add(socket.gethostname().lower())
    except OSError:
        pass
    return names


def _addresses(host: str) -> set[str]:
    try:
        return {info[4][0].split("%", 1)[0] for info in socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)}
    except (OSError, UnicodeError):
        return set()


def _forbidden_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast


def ensure_allowed(egress: list[dict], *, resolve=None) -> None:
    resolve = resolve or _addresses
    internal = _internal_names()
    internal_ips: set[str] | None = None
    for item in egress:
        host = str(item.get("host") or "").strip().strip("[]").lower().rstrip(".")
        if not host:
            continue
        if host in internal or _forbidden_ip(host):
            raise GatewayError(400, "egress_forbidden", "连接地址不能是平台内部服务")
        resolved = resolve(host)
        if any(_forbidden_ip(address) for address in resolved):
            raise GatewayError(400, "egress_forbidden", "连接地址不能是平台内部服务")
        if internal_ips is None:
            internal_ips = set().union(*(resolve(name) for name in internal)) if internal else set()
        if resolved & internal_ips:
            raise GatewayError(400, "egress_forbidden", "连接地址不能是平台内部服务")
