"""内核调连接器网关做文档转换（ADR-0073；`app.evidence.gateway_client` 的转换这一部分）。

网关地址 ONTO_WEB_CONNECTOR_GATEWAY_URL；没配就是「未部署」，资料停在「等待转换」。每次转换签一张一次性票据，
写明任务、转换器、文件名和原文件的 sha256。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from ...evidence.tickets import Issuer, sha256_hex

_log = logging.getLogger(__name__)

URL_ENV = "ONTO_WEB_CONNECTOR_GATEWAY_URL"
HEALTH_TTL_S = 10
PROVIDERS_TTL_S = 30
REQUEST_TIMEOUT_S = 330          # 网关单个任务最长 300 秒，加上排队与传输
MAX_BYTES = 24 * 1024 * 1024
DEFAULT_DURATION_S = 120
KEY_FILE = "connector-ticket-key.pem"

# 网关在内部网络上，不走任何代理（http_proxy 之类的环境变量不生效）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_issuer: Issuer | None = None
_issuer_lock = threading.Lock()


class GatewayUnavailable(Exception):
    """网关不可达、繁忙、取不到公钥、没有这个转换器：资料记「等待转换」，恢复后补转。"""


def base_url() -> str:
    return (os.environ.get(URL_ENV) or "").rstrip("/")


def issuer() -> Issuer:
    global _issuer
    with _issuer_lock:
        if _issuer is None:
            from ...governance import secrets

            _issuer = Issuer(secrets.path(KEY_FILE))
        return _issuer


def reset() -> None:
    """测试与换数据目录时：丢掉缓存和签发器。"""
    global _issuer
    with _cache_lock:
        _cache.clear()
    with _issuer_lock:
        _issuer = None


def _get(path: str, timeout: float = 5) -> dict:
    url = base_url()
    if not url:
        raise GatewayUnavailable("未配置")
    try:
        with _OPENER.open(f"{url}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise GatewayUnavailable(type(exc).__name__) from None


def _cached(key: str, ttl: float, load, force: bool):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < ttl:
            return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def health(force: bool = False) -> dict:
    """{status: ok|down|not_configured, converters, detail, latency_ms}。"""
    def load():
        if not base_url():
            return {"status": "not_configured", "converters": 0, "detail": "未部署", "latency_ms": None}
        started = time.time()
        try:
            data = _get("/health", timeout=3)
        except GatewayUnavailable as exc:
            return {"status": "down", "converters": 0, "detail": f"不可达：{exc}", "latency_ms": None}
        return {"status": "ok", "converters": int(data.get("converters") or 0), "detail": None,
                "latency_ms": int((time.time() - started) * 1000)}

    return _cached("health", HEALTH_TTL_S, load, force)


def providers(force: bool = False) -> list[dict] | None:
    """网关里已加载的转换器（GET /v1/providers）；网关不可用时 None。"""
    def load():
        try:
            return list(_get("/v1/providers").get("items") or [])
        except GatewayUnavailable:
            return None

    value = _cached("providers", PROVIDERS_TTL_S, load, force)
    if value is None:
        with _cache_lock:
            _cache.pop("providers", None)  # 不可用的结果不缓存，恢复后下一次就能取到
    return value


def convert(data: bytes, filename: str, converter: str, job_id: str, *,
            max_duration_s: int = DEFAULT_DURATION_S) -> dict:
    """POST /v1/convert；返回网关的结果（state 为 converted / unsupported / failed）。"""
    url = base_url()
    if not url:
        raise GatewayUnavailable("未配置")
    token = issuer().issue(job_id=job_id, operation="convert", scope_hash=sha256_hex(data),
                           budget={"max_bytes": MAX_BYTES, "max_duration_s": max_duration_s, "max_rows": 0},
                           claims={"converter": converter, "filename": filename})
    request = urllib.request.Request(f"{url}/v1/convert", data=data, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"})
    try:
        with _OPENER.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        code = str(body.get("error") or "")
        if exc.code in (503, 404) or code in ("busy", "issuer_unavailable", "converter_not_found"):
            raise GatewayUnavailable(code or str(exc.code)) from None
        if exc.code == 413:
            return {"state": "failed", "error_code": "too_large"}
        _log.error("连接器网关拒绝了转换请求：%s %s", exc.code, code)
        return {"state": "failed", "error_code": "crashed"}
    except (OSError, ValueError) as exc:
        raise GatewayUnavailable(type(exc).__name__) from None
