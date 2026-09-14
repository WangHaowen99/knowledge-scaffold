# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""RS256 密钥对管理 + JWKS。

- 私钥 PEM 首启自动生成（RSA 2048），存 <DATA_DIR>/keys/private.pem，0600 原子写
  （写临时文件 + chmod + os.link，并发安全，写法参考 consumer secrets.py）。
- kid 取公钥 DER(SubjectPublicKeyInfo) SHA-256 指纹前 12 位。
- JWKS 汇集 keys/ 目录下全部 *.pem（私钥取其公钥，公钥直接收录），
  支持多把 key 以备轮换：换新 private.pem 后把旧公钥留在目录里即可继续验旧 token。
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from authlib.jose import JsonWebKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

RSA_KEY_SIZE = 2048
ACTIVE_KEY_FILENAME = "private.pem"


def compute_kid(public_key: Any) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()[:12]


class KeyManager:
    def __init__(self, keys_dir: str | Path) -> None:
        self.keys_dir = Path(keys_dir)
        self._private_pem, self._kid = self._load_or_create_active_key()

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def private_pem(self) -> bytes:
        return self._private_pem

    def _load_or_create_active_key(self) -> tuple[bytes, str]:
        path = self.keys_dir / ACTIVE_KEY_FILENAME
        if not path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            self._atomic_write_if_absent(path, pem)
        pem = path.read_bytes()
        private_key = serialization.load_pem_private_key(pem, password=None)
        return pem, compute_kid(private_key.public_key())

    def _atomic_write_if_absent(self, path: Path, data: bytes) -> None:
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.keys_dir.chmod(0o700)
        except OSError:
            pass
        temp_path = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
        try:
            temp_path.write_bytes(data)
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            try:
                os.link(temp_path, path)
            except FileExistsError:
                # 并发首启：另一进程已写入，读它的即可
                pass
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _public_key_from_pem(pem: bytes) -> Optional[Any]:
        try:
            return serialization.load_pem_private_key(pem, password=None).public_key()
        except (ValueError, TypeError):
            pass
        try:
            return serialization.load_pem_public_key(pem)
        except (ValueError, TypeError):
            return None

    def jwks(self) -> Dict[str, Any]:
        keys = []
        seen: set[str] = set()
        for path in sorted(self.keys_dir.glob("*.pem")):
            public_key = self._public_key_from_pem(path.read_bytes())
            if public_key is None:
                continue
            kid = compute_kid(public_key)
            if kid in seen:
                continue
            seen.add(kid)
            public_pem = public_key.public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            jwk = JsonWebKey.import_key(
                public_pem, {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid}
            )
            keys.append(jwk.as_dict())
        return {"keys": keys}
