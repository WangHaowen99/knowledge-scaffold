"""连接器网关的一次性票据（详设 5.8.2「鉴权」；ADR-0073 第 5 条）。

内核用 Ed25519 私钥签 JWT，网关只拿公钥验：网关手里没有能签票据的东西。票据写明任务、操作、范围哈希（转换时是
原文件的 sha256）、预算，10 分钟过期；网关按 jti 记下用过的票据，同一张只认一次。

签发密钥在后端的密钥目录（0700，助手以独立用户运行读不到文件）。后端进程里能拿到这把密钥的缺口见详设 5.8.9。
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ISSUER = "onto-modeling"
AUDIENCE = "connector-gateway"
ALGORITHM = "EdDSA"
KEY_FILE = "connector-ticket-key.pem"
TICKET_TTL_S = 600  # 10 分钟
# 转换（ADR-0073）；插件交付、凭据、数据源测试与采集（ADR-0103）
OPERATIONS = ("convert", "plugin_install", "plugin_remove", "secret", "source_test", "source_collect")


class TicketError(ValueError):
    """票据不成立：签名、受众、过期、操作、重放、范围哈希任一项不对。"""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _kid(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


class Issuer:
    """内核这一侧：签发票据、给出公钥。"""

    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        self._lock = threading.Lock()
        self._key: Ed25519PrivateKey | None = None

    def _private(self) -> Ed25519PrivateKey:
        with self._lock:
            if self._key is None:
                if self.key_path.is_file():
                    self._key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
                else:
                    self._key = Ed25519PrivateKey.generate()
                    self.key_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.key_path.with_suffix(".tmp")
                    tmp.write_bytes(self._key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                            serialization.NoEncryption()))
                    tmp.chmod(0o600)
                    tmp.replace(self.key_path)
            return self._key

    def public_jwk(self) -> dict:
        public = self._private().public_key()
        raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"kty": "OKP", "crv": "Ed25519", "x": _b64(raw), "kid": _kid(public), "alg": ALGORITHM, "use": "sig"}

    def issue(self, *, job_id: str, operation: str, scope_hash: str, budget: dict, claims: dict | None = None,
              now: float | None = None) -> str:
        if operation not in OPERATIONS:
            raise TicketError(f"未知的操作：{operation}")
        issued = int(now if now is not None else time.time())
        payload = {
            **(claims or {}),
            "iss": ISSUER, "aud": AUDIENCE, "jti": uuid.uuid4().hex, "job": job_id, "op": operation,
            "scope": scope_hash, "budget": dict(budget), "iat": issued, "exp": issued + TICKET_TTL_S,
        }
        key = self._private()
        return jwt.encode(payload, key, algorithm=ALGORITHM, headers={"kid": _kid(key.public_key())})


class Verifier:
    """网关这一侧：用内核的公钥验签，按 jti 防重放。"""

    def __init__(self, jwk: dict):
        self._key = jwt.PyJWK(dict(jwk), algorithm=ALGORITHM).key
        self.kid = str(jwk.get("kid") or "")
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, token: str, *, operation: str, scope_hash: str, now: float | None = None) -> dict:
        try:
            claims = jwt.decode(token, self._key, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
                                options={"require": ["exp", "iat", "jti", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            raise TicketError(f"票据无效：{type(exc).__name__}") from None
        current = now if now is not None else time.time()
        if claims.get("exp", 0) - claims.get("iat", 0) > TICKET_TTL_S:
            raise TicketError("票据有效期超过上限")
        if claims.get("op") != operation:
            raise TicketError("票据的操作不符")
        if claims.get("scope") != scope_hash:
            raise TicketError("票据的范围哈希不符")
        with self._lock:
            for jti, expires in list(self._used.items()):
                if expires < current:
                    del self._used[jti]
            if claims["jti"] in self._used:
                raise TicketError("票据已用过")
            self._used[claims["jti"]] = float(claims["exp"])
        return claims


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
