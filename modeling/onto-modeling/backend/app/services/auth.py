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
AUDIENCE = os.environ.get("ONTO_WEB_AUTH_AUDIENCE", "knowledge-scaffold")

# 产品由 meta-wise 改名为 knowledge-scaffold：Consumer 升级会把 auth-server 签发的 audience
# 迁到新名（只迁仍等于旧默认值的部署），这里的配置却可能仍是旧名，两边一错开登录后所有请求都 401。
# 两个名字是同一产品的新旧称呼，配置为其中之一时都接受；其他自定义取值仍严格匹配。
_PRODUCT_AUDIENCES = ("knowledge-scaffold", "meta-wise")


def accepted_audiences(configured: str) -> list[str]:
    return list(_PRODUCT_AUDIENCES) if configured in _PRODUCT_AUDIENCES else [configured]


ACCEPTED_AUDIENCES = accepted_audiences(AUDIENCE)

ENABLED = bool(AUTH_URL)
_jwks_client = PyJWKClient(f"{AUTH_URL}/jwks.json") if ENABLED else None

# 签发方（auth-server）与本服务的时钟可能有偏差：分机部署、虚拟机校时回拨。
# iat/nbf/exp 不留容差时，刚签发的令牌会被判"尚未生效"，表现为刚登录就提示登录过期
CLOCK_SKEW_LEEWAY_SECONDS = 60

# 用户最近活跃时间（内存态，重启清零），供运维页"在线用户"用
LAST_SEEN: dict[str, float] = {}


def touch(username: str) -> None:
    if username:
        import time

        LAST_SEEN[username] = time.time()


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
            token,
            key,
            algorithms=["RS256"],
            audience=ACCEPTED_AUDIENCES,
            issuer=ISSUER,
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
        )
    except Exception:
        return None
    if claims.get("typ") != "access":
        return None
    return claims


def current_user(token: str, claims: dict) -> dict:
    """向 Auth 核对当前账号状态；JWT 中的角色和改密标记不能替代实时状态。"""
    if not AUTH_URL:
        raise AuthError("认证服务地址未配置", 503)
    request = urllib.request.Request(
        f"{AUTH_URL}/userinfo", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            user = json.loads(response.read(16385))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("未登录或登录已过期", 401) from exc
        raise AuthError("认证服务暂不可用", 503) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise AuthError("认证服务暂不可用", 503) from exc
    if not isinstance(user, dict):
        raise AuthError("账号状态无效", 401)
    if user.get("sub") != claims.get("sub"):
        raise AuthError("账号状态无效", 401)
    if user.get("role") not in ("admin", "user"):
        raise AuthError("账号状态无效", 401)
    if not isinstance(user.get("password_change_required"), bool):
        raise AuthError("账号状态无效", 401)
    return user


def register_request(payload: dict) -> tuple[int, dict]:
    """代理 auth-server /register；4xx/5xx 原样透传（含密码策略 violations）。"""
    if not ENABLED:
        raise AuthError("鉴权未启用", 400)
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"{AUTH_URL}/register",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {"error": "注册失败"}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError("认证服务不可达", 502) from exc


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


def resolve_hub_member(username: str) -> dict:
    """Resolve the current account contract: JWT subject is the directory username."""
    from .. import config
    if not AUTH_URL or not ISSUER or not config.AUTH_ADMIN_TOKEN:
        raise AuthError("未配置可信账号目录，无法同步 Hub 成员", 503)
    request = urllib.request.Request(
        f"{AUTH_URL}/admin/users", headers={"X-Admin-Token": config.AUTH_ADMIN_TOKEN})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            users = json.loads(response.read())["users"]
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        raise AuthError("账号目录查询失败", 502) from exc
    if not any(user.get("username") == username for user in users):
        raise AuthError("目标账号不存在，无法同步 Hub 授权", 422)
    return {"issuer": ISSUER, "sub": username}
