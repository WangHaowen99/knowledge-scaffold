"""对接 auth-server（OAuth2 密码模式子集）的资源服务器侧实现。

ONTO_WEB_AUTH_URL 未配置时鉴权整体关闭（本地开发默认）。
access token 为 RS256 JWT，经 JWKS 验签并校验 iss/aud/typ。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import jwt
from jwt import PyJWKClient

AUTH_URL = os.environ.get("ONTO_WEB_AUTH_URL", "").rstrip("/")
ISSUER = os.environ.get("ONTO_WEB_AUTH_ISSUER", AUTH_URL)
AUDIENCE = os.environ.get("ONTO_WEB_AUTH_AUDIENCE", "meta-wise")

ENABLED = bool(AUTH_URL)
_jwks_client = PyJWKClient(f"{AUTH_URL}/jwks.json") if ENABLED else None


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def verify_access_token(token: str) -> dict | None:
    """验签通过返回 claims，否则 None。"""
    if not ENABLED:
        return {}
    try:
        key = _jwks_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=AUDIENCE, issuer=ISSUER
        )
    except Exception:
        return None
    if claims.get("typ") != "access":
        return None
    return claims


def token_request(payload: dict) -> dict:
    """代理到 auth-server /token（登录与刷新共用）。"""
    if not ENABLED:
        raise AuthError("鉴权未启用", 400)
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(f"{AUTH_URL}/token", data=data)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            raise AuthError("用户名或密码错误，或登录已失效") from exc
        raise AuthError("认证服务异常", 502) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError("认证服务不可达", 502) from exc
