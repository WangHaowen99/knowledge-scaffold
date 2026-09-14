# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""JWT HS256 helpers for MCP tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Mapping


class MCPTokenError(ValueError):
    """Raised when an MCP token cannot be encoded or verified."""


def strip_token_prefix(token: str) -> str:
    value = str(token or "").strip()
    if value.startswith("mcp_"):
        return value[4:]
    return value


def hash_token(token: str) -> str:
    digest = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
    return "sha256:{}".format(digest)


def mask_token(token: str) -> str:
    value = str(token or "").strip()
    if not value:
        return ""
    if len(value) <= 18:
        return "{}...{}".format(value[:6], value[-4:])
    return "{}...{}".format(value[:12], value[-8:])


def encode_jwt(payload: Mapping[str, Any], *, secret: str, algorithm: str = "HS256") -> str:
    if str(algorithm or "").upper() != "HS256":
        raise MCPTokenError("P0 仅支持 HS256")
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(
        json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    )
    signing_input = "{}.{}".format(header_b64, payload_b64).encode("utf-8")
    signature = hmac.new(str(secret or "").encode("utf-8"), signing_input, hashlib.sha256).digest()
    return "{}.{}.{}".format(header_b64, payload_b64, _b64url_encode(signature))


def decode_jwt(
    token: str,
    *,
    secret: str,
    algorithm: str = "HS256",
    issuer: str = "",
    audience: str = "",
) -> dict[str, Any]:
    if str(algorithm or "").upper() != "HS256":
        raise MCPTokenError("P0 仅支持 HS256")
    raw_token = strip_token_prefix(token)
    parts = raw_token.split(".")
    if len(parts) != 3:
        raise MCPTokenError("JWT 结构无效")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise MCPTokenError("JWT 内容不是合法 JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise MCPTokenError("JWT header/payload 必须是对象")
    if str(header.get("alg") or "").upper() != "HS256":
        raise MCPTokenError("JWT 签名算法不匹配")
    signing_input = "{}.{}".format(header_b64, payload_b64).encode("utf-8")
    expected = hmac.new(str(secret or "").encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual = _b64url_decode(signature_b64)
    if not hmac.compare_digest(expected, actual):
        raise MCPTokenError("JWT 签名校验失败")
    _validate_time_claims(payload)
    if issuer and str(payload.get("iss") or "") != issuer:
        raise MCPTokenError("JWT issuer 不匹配")
    if audience:
        aud = payload.get("aud")
        if isinstance(aud, list):
            if audience not in [str(item) for item in aud]:
                raise MCPTokenError("JWT audience 不匹配")
        elif str(aud or "") != audience:
            raise MCPTokenError("JWT audience 不匹配")
    return payload


def _validate_time_claims(payload: Mapping[str, Any]) -> None:
    now = int(time.time())
    exp = payload.get("exp")
    if exp is not None:
        try:
            exp_ts = int(exp)
        except Exception as exc:
            raise MCPTokenError("JWT exp 字段无效") from exc
        if exp_ts < now:
            raise MCPTokenError("JWT 已过期")
    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            nbf_ts = int(nbf)
        except Exception as exc:
            raise MCPTokenError("JWT nbf 字段无效") from exc
        if nbf_ts > now:
            raise MCPTokenError("JWT 尚未生效")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(str(value or "")) % 4)
    return base64.urlsafe_b64decode(str(value or "") + padding)
