"""access/refresh JWT 签发与校验（RS256，基于 Authlib，不手写 JOSE）。"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

from .config import AuthServerConfig
from .keys import KeyManager
from .users import UserView

_jwt = JsonWebToken(["RS256"])


class TokenService:
    def __init__(self, config: AuthServerConfig, key_manager: KeyManager) -> None:
        self._config = config
        self._keys = key_manager

    def issue_pair(self, user: UserView) -> Dict[str, Any]:
        """签发一对 access/refresh token（/token 端点的成功响应体）。"""
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": self._keys.kid}
        access_claims = {
            "iss": self._config.issuer,
            "aud": self._config.audience,
            "sub": user.username,
            "role": user.role,
            "pwd_cr": bool(user.password_change_required),
            "ver": user.token_version,
            "typ": "access",
            "iat": now,
            "exp": now + self._config.access_ttl_seconds,
        }
        refresh_claims = {
            "iss": self._config.issuer,
            "aud": self._config.audience,
            "sub": user.username,
            "ver": user.token_version,
            "typ": "refresh",
            "jti": secrets.token_hex(16),
            "iat": now,
            "exp": now + self._config.refresh_ttl_seconds,
        }
        return {
            "access_token": _jwt.encode(header, access_claims, self._keys.private_pem).decode("ascii"),
            "refresh_token": _jwt.encode(header, refresh_claims, self._keys.private_pem).decode("ascii"),
            "token_type": "Bearer",
            "expires_in": self._config.access_ttl_seconds,
        }

    def decode(self, token: str, expected_typ: str) -> Optional[Dict[str, Any]]:
        """验签 + iss/aud/exp + typ 校验；ver 与用户当前 token_version 的比对由调用方完成。"""
        if not token:
            return None
        try:
            key_set = JsonWebKey.import_key_set(self._keys.jwks())
            claims = _jwt.decode(
                token,
                key_set,
                claims_options={
                    "iss": {"essential": True, "values": [self._config.issuer]},
                    "aud": {"essential": True, "values": [self._config.audience]},
                    "exp": {"essential": True},
                },
            )
            claims.validate()
        except (JoseError, ValueError, TypeError):
            return None
        if claims.get("typ") != expected_typ:
            return None
        return dict(claims)
