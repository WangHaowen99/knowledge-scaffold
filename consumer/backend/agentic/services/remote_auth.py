"""RemoteAuthBackend：AuthBackend 协议的远程实现（对接独立 auth server）。

施工步骤 4（docs/decisions/2026-07-09-decoupled-auth-architecture.md D2/D4 + 方案 a）：

- authenticate / refresh_tokens 走 auth server 的 OAuth2 ``/token`` 端点
  （password / refresh_token 两种 grant）；
- verify_token 用 JWKS 做本地 RS256 验签，不逐请求回查 auth server——吊销语义
  按 D2 显式放宽为“≤ access TTL 内失效”（改密后旧 access 在 TTL 窗口内仍可验签，
  旧 refresh 立即失效）；
- 用户管理走 ``/admin/*``（X-Admin-Token 头），上游错误翻译为 ValueError（中文消息）
  与 PasswordPolicyError(violations)，routes/auth.py 现有异常处理零改动。

验签库选 PyJWT 而非 authlib：authlib 对 consumer 是新的重依赖，PyJWT 更轻，
且 RS256 所需的 cryptography 已在 requirements.txt 中。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import jwt
import requests

from .auth import AuthUser, PasswordPolicyError

# 连接 3s / 读取 10s：与注册 BFF 代理保持一致
_TIMEOUT = (3, 10)

_UNAVAILABLE_MESSAGE = "认证服务暂不可用"

DEFAULT_AUDIENCE = "meta-wise"
DEFAULT_JWKS_CACHE_TTL_SECONDS = 300


class RemoteAuthBackend:
    """通过 HTTP 对接独立 auth server 的认证后端（consumer 唯一认证路径）。"""

    # authenticate 成功后 token 三元组的暂存时长：只需覆盖同一次 login 请求内
    # 紧随其后的 issue_token / pop_login_tokens 调用
    LOGIN_TOKENS_TTL_SECONDS = 30

    def __init__(
        self,
        base_url: str,
        admin_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        jwks_cache_ttl_seconds: int = DEFAULT_JWKS_CACHE_TTL_SECONDS,
        audience: str = DEFAULT_AUDIENCE,
        issuer: str = "",
    ) -> None:
        self._base_url = str(base_url or "").strip().rstrip("/")
        if not self._base_url:
            raise ValueError("RemoteAuthBackend 需要非空的 auth server 地址")
        self._admin_token = str(admin_token or "").strip()
        self._session = session or requests.Session()
        self._jwks_cache_ttl_seconds = max(1, int(jwks_cache_ttl_seconds))
        self._audience = str(audience or "").strip() or DEFAULT_AUDIENCE
        # issuer 未显式配置时默认等于 base_url（预置 auth server 的部署约定）
        self._issuer = str(issuer or "").strip() or self._base_url

        # JWKS 缓存：kid -> RSA 公钥
        self._jwks_lock = threading.Lock()
        self._jwks_keys: Dict[str, Any] = {}
        self._jwks_fetched_at = 0.0

        # 登录短时缓存：username -> (stored_at, {access_token, refresh_token, expires_in})
        self._login_lock = threading.Lock()
        self._login_tokens: Dict[str, tuple] = {}

    # ------------------------------------------------------------ 认证与 token

    def authenticate(self, username: str, password: str) -> Optional[AuthUser]:
        if not username or not password:
            return None
        response = self._post_token(
            {"grant_type": "password", "username": username, "password": password}
        )
        if response.status_code != 200:
            # 预置 server 对无效凭据返回 400 invalid_grant；401 同义处理
            if response.status_code in (400, 401):
                return None
            raise ValueError(_UNAVAILABLE_MESSAGE)
        payload = self._json_or_unavailable(response)
        access_token = str(payload.get("access_token") or "")
        # token 刚从可信源拿到，但仍走本地验签同一条代码路径（顺带预热 JWKS 缓存），
        # 避免“签发即不可验”的配置错误（iss/aud 不匹配）拖到首个业务请求才暴露
        user = self._verify_access_token_locally(access_token)
        if user is None:
            return None
        self._store_login_tokens(
            user.username,
            {
                "access_token": access_token,
                "refresh_token": str(payload.get("refresh_token") or ""),
                "expires_in": int(payload.get("expires_in") or 0),
            },
        )
        return user

    def issue_token(self, user: AuthUser) -> str:
        tokens = self._peek_login_tokens(user.username)
        if not tokens:
            raise RuntimeError(
                "RemoteAuthBackend.issue_token 只能在 authenticate 成功后 "
                f"{self.LOGIN_TOKENS_TTL_SECONDS}s 内调用（登录路由的调用顺序）"
            )
        return str(tokens["access_token"])

    def pop_login_tokens(self, username: str) -> Optional[Dict[str, Any]]:
        """取出并清除 authenticate 暂存的 token 三元组（login 路由随响应下发）。"""
        with self._login_lock:
            entry = self._login_tokens.pop(username, None)
        if entry is None:
            return None
        stored_at, tokens = entry
        if time.time() - stored_at > self.LOGIN_TOKENS_TTL_SECONDS:
            return None
        return dict(tokens)

    def refresh_tokens(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """用 refresh_token 换新 token 对；无效/已吊销 → None（路由据此返回 401）。"""
        if not refresh_token:
            return None
        response = self._post_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        if response.status_code != 200:
            if response.status_code in (400, 401):
                return None
            raise ValueError(_UNAVAILABLE_MESSAGE)
        payload = self._json_or_unavailable(response)
        return {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "expires_in": int(payload.get("expires_in") or 0),
        }

    def verify_token(self, token: str) -> Optional[AuthUser]:
        return self._verify_access_token_locally(token)

    # ------------------------------------------------------------ 用户管理

    def get_user(self, username: str) -> Optional[AuthUser]:
        username = str(username or "").strip()
        if not username:
            return None
        for entry in self.list_users():
            if entry.get("username") == username:
                return AuthUser(
                    username=username,
                    role=str(entry.get("role") or "user"),
                    password_change_required=bool(entry.get("password_change_required")),
                )
        return None

    def list_users(self) -> list:
        response = self._admin_request("GET", "/admin/users")
        if response.status_code != 200:
            self._raise_admin_error(response)
        return list(self._json_or_unavailable(response).get("users") or [])

    def create_user(self, username: str, password: str, role: str = "user") -> AuthUser:
        response = self._admin_request(
            "POST",
            "/admin/users",
            json={"username": username, "password": password, "role": role},
        )
        if response.status_code != 201:
            self._raise_admin_error(response)
        return self._auth_user_from_payload(self._json_or_unavailable(response))

    def update_user(
        self,
        username: str,
        *,
        password: Optional[str] = None,
        role: Optional[str] = None,
    ) -> AuthUser:
        body: Dict[str, Any] = {}
        if password is not None:
            body["password"] = password
        if role is not None:
            body["role"] = role
        response = self._admin_request(
            "PUT", "/admin/users/" + quote(str(username), safe=""), json=body
        )
        if response.status_code != 200:
            self._raise_admin_error(response)
        return self._auth_user_from_payload(self._json_or_unavailable(response))

    def delete_user(self, username: str) -> None:
        response = self._admin_request(
            "DELETE", "/admin/users/" + quote(str(username), safe="")
        )
        if response.status_code != 200:
            self._raise_admin_error(response)

    def change_password(self, username: str, old_password: str, new_password: str) -> AuthUser:
        response = self._admin_request(
            "PUT",
            "/admin/change-password",
            json={
                "username": username,
                "old_password": old_password,
                "new_password": new_password,
            },
        )
        if response.status_code != 200:
            self._raise_admin_error(response)
        # 上游成功响应只有 {"success": true}：回查一次拿最新用户状态；
        # 改密成功后 must_change_password 必为 False，回查失败时按此构造
        user = self.get_user(username)
        return user or AuthUser(username=str(username).strip(), role="user", password_change_required=False)

    # ------------------------------------------------------------ HTTP 辅助

    def _post_token(self, payload: Dict[str, Any]) -> requests.Response:
        try:
            return self._session.post(
                self._base_url + "/token", json=payload, timeout=_TIMEOUT
            )
        except requests.RequestException as exc:
            raise ValueError(_UNAVAILABLE_MESSAGE) from exc

    def _admin_request(
        self, method: str, path: str, json: Optional[Dict[str, Any]] = None
    ) -> requests.Response:
        if not self._admin_token:
            raise ValueError("未配置 AUTH_SERVER_ADMIN_TOKEN，无法管理用户")
        try:
            return self._session.request(
                method,
                self._base_url + path,
                json=json,
                headers={"X-Admin-Token": self._admin_token},
                timeout=_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ValueError(_UNAVAILABLE_MESSAGE) from exc

    @staticmethod
    def _json_or_unavailable(response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(_UNAVAILABLE_MESSAGE) from exc
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _raise_admin_error(response: requests.Response) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code == 400:
            if payload.get("code") == "PASSWORD_POLICY_FAILED":
                raise PasswordPolicyError(
                    [str(v) for v in (payload.get("violations") or [])]
                )
            # 上游中文 error 消息原样透传，routes/auth.py 的 ValueError→400 翻译零改动
            raise ValueError(str(payload.get("error") or "请求无效"))
        raise ValueError(str(payload.get("error") or _UNAVAILABLE_MESSAGE))

    @staticmethod
    def _auth_user_from_payload(payload: Dict[str, Any]) -> AuthUser:
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return AuthUser(
            username=str(user.get("username") or ""),
            role=str(user.get("role") or "user"),
            password_change_required=bool(user.get("password_change_required")),
        )

    # ------------------------------------------------------------ 本地验签（JWKS）

    def _verify_access_token_locally(self, token: str) -> Optional[AuthUser]:
        if not token:
            return None
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            # 非 JWT（无法解析头部）→ 验签失败
            return None
        if header.get("alg") != "RS256":
            return None
        key = self._signing_key(header.get("kid"))
        if key is None:
            return None
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError:
            return None
        # 只接受 access token：refresh token 不能当访问凭据使用
        if claims.get("typ") != "access":
            return None
        username = str(claims.get("sub") or "").strip()
        if not username:
            return None
        return AuthUser(
            username=username,
            role=str(claims.get("role") or "user"),
            password_change_required=bool(claims.get("pwd_cr")),
        )

    def _signing_key(self, kid: Optional[str]) -> Optional[Any]:
        if not kid:
            return None
        now = time.time()
        with self._jwks_lock:
            fresh = (now - self._jwks_fetched_at) < self._jwks_cache_ttl_seconds
            key = self._jwks_keys.get(kid)
        if key is not None and fresh:
            return key
        # 缓存过期，或 TTL 内遇到未知 kid（可能刚轮换密钥）→ 主动刷新一次
        self._refresh_jwks()
        with self._jwks_lock:
            # 刷新失败（网络故障）时沿用旧缓存里的 key（stale 优于验不了）
            return self._jwks_keys.get(kid) or key

    def _refresh_jwks(self) -> None:
        """拉取 JWKS 重建 kid→公钥映射；网络失败静默沿用旧缓存（verify 失败返回 None，不抛错）。"""
        try:
            response = self._session.get(
                self._base_url + "/jwks.json", timeout=_TIMEOUT
            )
            if response.status_code != 200:
                return
            payload = response.json()
        except (requests.RequestException, ValueError):
            return
        if not isinstance(payload, dict):
            return
        keys: Dict[str, Any] = {}
        for jwk_data in payload.get("keys") or []:
            if not isinstance(jwk_data, dict):
                continue
            kid = str(jwk_data.get("kid") or "")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(jwk_data).key
            except (jwt.PyJWTError, ValueError, TypeError):
                continue
        with self._jwks_lock:
            self._jwks_keys = keys
            self._jwks_fetched_at = time.time()

    # ------------------------------------------------------------ 登录短时缓存

    def _store_login_tokens(self, username: str, tokens: Dict[str, Any]) -> None:
        now = time.time()
        with self._login_lock:
            # 顺带清理过期条目，避免缓存无界增长
            expired = [
                name
                for name, (stored_at, _tokens) in self._login_tokens.items()
                if now - stored_at > self.LOGIN_TOKENS_TTL_SECONDS
            ]
            for name in expired:
                self._login_tokens.pop(name, None)
            self._login_tokens[username] = (now, dict(tokens))

    def _peek_login_tokens(self, username: str) -> Optional[Dict[str, Any]]:
        with self._login_lock:
            entry = self._login_tokens.get(username)
            if entry is None:
                return None
            stored_at, tokens = entry
            if time.time() - stored_at > self.LOGIN_TOKENS_TTL_SECONDS:
                self._login_tokens.pop(username, None)
                return None
            return dict(tokens)
