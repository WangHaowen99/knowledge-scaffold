"""内核调连接器网关的采集接口（`design/evidence-collection.md` §4；ADR-0082）。

复用资料转换的网关客户端：同一个地址（ONTO_WEB_CONNECTOR_GATEWAY_URL）、同一把票据密钥（`gateway.issuer()`）、
不走代理。每个请求签一张一次性票据：建任务用 `snapshot` / `discover`，范围是任务说明的规范 JSON；查进度、取消、
取快照包、清理用 `job`，范围是 `{"job_id": …}`。连接型提供方（F16，spec §8）另有写、删凭据（`secret`）和测试连接（`test`）。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..services.material_conversion import gateway
from . import spec

CATALOG_TTL_S = 30
DOWN_TTL_S = 10              # 网关不通的结果也缓存一会儿：提供方列表在页面和节点判断里调得很频繁
STATUS_TIMEOUT_S = 10
SUBMIT_TIMEOUT_S = 600       # 目标程序可能有几百 MB，边收边转给旁路容器
BUNDLE_TIMEOUT_S = 300
TEST_TIMEOUT_S = 120         # 测试连接要连业务系统、做只读自检
_COPY_BYTES = 1024 * 1024
_catalog: tuple[str, float, list[dict] | None] | None = None
_catalog_lock = threading.Lock()

GatewayUnavailable = gateway.GatewayUnavailable


class GatewayError(Exception):
    """网关拒绝了请求：带 HTTP 状态和网关的错误码（`{"error", "message"}`）。"""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status = status
        self.code = code or f"http_{status}"
        self.message = message or self.code


def catalog(force: bool = False) -> list[dict] | None:
    """网关里的证据源提供方（`GET /v1/providers` 的 `providers`）；网关不可用时 None。"""
    global _catalog
    now, url = time.time(), gateway.base_url()
    with _catalog_lock:
        if not force and _catalog is not None and _catalog[0] == url:  # 地址改了（即时生效的配置）就重取
            _url, stamp, value = _catalog
            if now - stamp < (CATALOG_TTL_S if value is not None else DOWN_TTL_S):
                return value
    try:
        data = gateway._get("/v1/providers")
        value = [dict(item) for item in data.get("providers") or [] if isinstance(item, dict) and item.get("id")]
    except GatewayUnavailable:
        value = None
    with _catalog_lock:
        _catalog = (url, now, value)
    return value


def reset() -> None:
    global _catalog
    with _catalog_lock:
        _catalog = None


def provider(provider_id: str, force: bool = False) -> dict | None:
    return next((item for item in catalog(force) or [] if item.get("id") == provider_id), None)


def _ticket(job_id: str, operation: str, scope_hash: str, budget: dict) -> str:
    return gateway.issuer().issue(job_id=job_id, operation=operation, scope_hash=scope_hash, budget=budget)


def _job_ticket(job_id: str, budget: dict) -> str:
    return _ticket(job_id, "job", spec.job_scope(job_id), budget)


def _url(path: str) -> str:
    base = gateway.base_url()
    if not base:
        raise GatewayUnavailable("未配置")
    return f"{base}{path}"


def _error(exc: urllib.error.HTTPError) -> GatewayError:
    try:
        body = json.loads(exc.read().decode("utf-8"))
    except (ValueError, OSError):
        body = {}
    body = body if isinstance(body, dict) else {}
    return GatewayError(exc.code, str(body.get("error") or body.get("code") or ""), str(body.get("message") or ""))


def _call(request: urllib.request.Request, timeout: float) -> dict:
    try:
        with gateway._OPENER.open(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _error(exc) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise GatewayUnavailable(type(exc).__name__) from None
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise GatewayError(502, "bad_response", "网关回的不是 JSON") from None
    return data if isinstance(data, dict) else {}


def _connection(timeout: float) -> tuple[http.client.HTTPConnection, str]:
    parsed = urllib.parse.urlsplit(gateway.base_url())
    if not parsed.hostname:
        raise GatewayUnavailable("未配置")
    kind = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    return kind(parsed.hostname, parsed.port, timeout=timeout), parsed.path.rstrip("/")


def submit(job_spec: dict, artifact: Path | None) -> dict:
    """`POST /v1/jobs`：任务说明放 `X-Onto-Job` 头，请求体是目标程序（流式）；回 JobStatus。

    网关核对不过时可能不读完请求体就回错误并断开：发送中连接被重置时先试着读出响应里的错误码，读不到才算网关不可达。
    """
    token = _ticket(job_spec["job_id"], job_spec["operation"], spec.scope_hash(job_spec), job_spec["budget"])
    size = Path(artifact).stat().st_size if artifact is not None else 0
    connection, prefix = _connection(SUBMIT_TIMEOUT_S)
    try:
        try:
            connection.putrequest("POST", f"{prefix}/v1/jobs", skip_accept_encoding=True)
            for name, value in (("Authorization", f"Bearer {token}"),
                                ("X-Onto-Job", spec.encode_job_header(job_spec)),
                                ("Content-Type", "application/octet-stream"), ("Content-Length", str(size))):
                connection.putheader(name, value)
            connection.endheaders()
            if artifact is not None:
                with Path(artifact).open("rb") as handle:
                    while piece := handle.read(_COPY_BYTES):
                        connection.send(piece)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 下面读响应：网关多半已经回了错误码
        except OSError as exc:
            raise GatewayUnavailable(type(exc).__name__) from None
        try:
            response = connection.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise GatewayUnavailable(type(exc).__name__) from None
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        if response.status >= 400:
            raise GatewayError(response.status, str(data.get("error") or ""), str(data.get("message") or ""))
        return data
    finally:
        connection.close()


def status(job_id: str, budget: dict) -> dict:
    request = urllib.request.Request(_url(f"/v1/jobs/{job_id}"), method="GET",
                                     headers={"Authorization": f"Bearer {_job_ticket(job_id, budget)}"})
    return _call(request, STATUS_TIMEOUT_S)


def cancel(job_id: str, budget: dict) -> dict:
    request = urllib.request.Request(_url(f"/v1/jobs/{job_id}/cancel"), data=b"", method="POST",
                                     headers={"Authorization": f"Bearer {_job_ticket(job_id, budget)}"})
    return _call(request, STATUS_TIMEOUT_S)


def delete(job_id: str, budget: dict) -> None:
    request = urllib.request.Request(_url(f"/v1/jobs/{job_id}"), method="DELETE",
                                     headers={"Authorization": f"Bearer {_job_ticket(job_id, budget)}"})
    _call(request, STATUS_TIMEOUT_S)


# ---------------------------------------------------------------------------
# 凭据与测试连接（spec §8）。明文只在 put_secret 的请求体里经过一次：不记日志、不进异常信息
# ---------------------------------------------------------------------------


def _json_request(method: str, path: str, token: str, payload: dict | None) -> urllib.request.Request:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    return urllib.request.Request(_url(path), data=data, method=method, headers=headers)


def ticket_prefix(token: str) -> str:
    """票据 jti 的前 8 位（EV-08 的 `ticket.id_prefix`）；只解码载荷，不验签。"""
    try:
        import jwt

        return str(jwt.decode(token, options={"verify_signature": False}).get("jti") or "")[:8]
    except Exception:  # noqa: BLE001 - 只用于展示
        return ""


def put_secret(source_id: str, fields: dict[str, str]) -> dict:
    """`PUT /v1/secrets/{源}`：回 `{cred_ref, written_at}`。"""
    token = _ticket(source_id, "secret", spec.secret_scope(source_id, fields), {})
    request = _json_request("PUT", f"/v1/secrets/{urllib.parse.quote(source_id)}", token, {"fields": dict(fields)})
    return _call(request, STATUS_TIMEOUT_S)


def delete_secret(source_id: str) -> None:
    """`DELETE /v1/secrets/{源}`：删这个源的所有版本。"""
    token = _ticket(source_id, "secret", spec.secret_delete_scope(source_id), {})
    _call(_json_request("DELETE", f"/v1/secrets/{urllib.parse.quote(source_id)}", token, None), STATUS_TIMEOUT_S)


def test_source(test_spec: dict) -> tuple[dict, str]:
    """`POST /v1/sources/{源}/test`：回 (`{ok, checks}`, 票据 jti 前缀)。"""
    source_id = test_spec["source"]["id"]
    token = _ticket(source_id, "test", spec.scope_hash(test_spec), {})
    request = _json_request("POST", f"/v1/sources/{urllib.parse.quote(source_id)}/test", token, test_spec)
    return _call(request, TEST_TIMEOUT_S), ticket_prefix(token)


def bundle(job_id: str, budget: dict, target: Path, max_bytes: int) -> int:
    """`GET /v1/jobs/{id}/bundle`：tar 流写到 target，超过 max_bytes 就停（GatewayError bundle_too_large）。"""
    request = urllib.request.Request(_url(f"/v1/jobs/{job_id}/bundle"), method="GET",
                                     headers={"Authorization": f"Bearer {_job_ticket(job_id, budget)}"})
    written = 0
    try:
        with gateway._OPENER.open(request, timeout=BUNDLE_TIMEOUT_S) as response, Path(target).open("wb") as out:
            while piece := response.read(_COPY_BYTES):
                written += len(piece)
                if written > max_bytes:
                    raise GatewayError(413, "bundle_too_large", "快照包超过上限")
                out.write(piece)
            expected = response.headers.get("Content-Length")
            if expected is not None and expected.isdigit() and int(expected) != written:
                raise http.client.IncompleteRead(b"", int(expected) - written)
    except urllib.error.HTTPError as exc:
        raise _error(exc) from None
    except http.client.IncompleteRead:
        # 网关发现原始结果不对会中途断开：按 Content-Length 读不满，入库失败、不留半份快照
        raise GatewayError(502, "bundle_incomplete", "快照包没收全") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise GatewayUnavailable(type(exc).__name__) from None
    return written

