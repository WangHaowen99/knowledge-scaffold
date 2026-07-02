"""Encryption helpers for secrets that must be recovered at runtime."""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SECRET_ENCRYPTION_KEY_ENV = "SECRET_ENCRYPTION_KEY"
SECRET_ENCRYPTION_KEY_ID_ENV = "SECRET_ENCRYPTION_KEY_ID"
ENCRYPTED_SECRET_PREFIX = "enc:v1:"

def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "platform").is_dir() and (candidate / "runtime").is_dir():
            return candidate
    return start.parents[2]


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
SERVER_ROOT = REPO_ROOT / "platform" / "server"
BACKEND_ROOT = SERVER_ROOT
PROJECT_ROOT_ENV = "PROJECT_ROOT"


class SecretEncryptionError(ValueError):
    """Raised when an encrypted secret cannot be decoded or decrypted."""


def _resolve_default_runtime_root() -> Path:
    return REPO_ROOT / ".onto-runtime"


def _resolve_runtime_root() -> Path:
    configured_root = os.getenv(PROJECT_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser()
    return _resolve_default_runtime_root()


def _key_file_path() -> Path:
    return _resolve_runtime_root() / "config" / "secret.key"


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(str(value or "").strip().encode("ascii"), validate=True)
    except Exception as exc:
        raise SecretEncryptionError("{} 必须是 base64 编码的 32 字节密钥".format(SECRET_ENCRYPTION_KEY_ENV)) from exc
    if len(key) != 32:
        raise SecretEncryptionError("{} 必须解码为 32 字节".format(SECRET_ENCRYPTION_KEY_ENV))
    return key


def _load_key() -> bytes:
    env_value = os.getenv(SECRET_ENCRYPTION_KEY_ENV)
    if env_value:
        return _decode_key(env_value)

    path = _key_file_path()
    if path.exists():
        return _decode_key(path.read_text(encoding="utf-8"))

    key = secrets.token_bytes(32)
    encoded_key = base64.b64encode(key).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(".{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        temp_path.write_text(encoded_key, encoding="utf-8")
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        try:
            os.link(temp_path, path)
        except FileExistsError:
            return _decode_key(path.read_text(encoding="utf-8"))
    except FileExistsError:
        return _decode_key(path.read_text(encoding="utf-8"))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return key


def _key_id() -> str:
    return str(os.getenv(SECRET_ENCRYPTION_KEY_ID_ENV) or "default").strip() or "default"


def _aad(context: str) -> bytes:
    return str(context or "").encode("utf-8")


def is_encrypted_secret(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_SECRET_PREFIX)


def encrypt_secret(plaintext: str, *, context: str) -> str:
    value = str(plaintext or "")
    if not value:
        return ""
    if is_encrypted_secret(value):
        return value

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_load_key()).encrypt(nonce, value.encode("utf-8"), _aad(context))
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
    return "{}{}:{}:{}".format(ENCRYPTED_SECRET_PREFIX, _key_id(), nonce_b64, ciphertext_b64)


def decrypt_secret(value: str, *, context: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    if not is_encrypted_secret(raw):
        return raw

    parts = raw.split(":", 4)
    if len(parts) != 5 or parts[0] != "enc" or parts[1] != "v1":
        raise SecretEncryptionError("secret 密文格式无效")
    _, _, _key_id_value, nonce_b64, ciphertext_b64 = parts
    try:
        nonce = base64.b64decode(nonce_b64.encode("ascii"), validate=True)
        ciphertext = base64.b64decode(ciphertext_b64.encode("ascii"), validate=True)
        plaintext = AESGCM(_load_key()).decrypt(nonce, ciphertext, _aad(context))
    except Exception as exc:
        raise SecretEncryptionError("secret 解密失败") from exc
    return plaintext.decode("utf-8")


def encrypt_secret_fields(payload: dict[str, Any], secret_fields: set[str], *, context: str) -> dict[str, Any]:
    result = dict(payload or {})
    for field_name in secret_fields:
        if field_name in result:
            result[field_name] = encrypt_secret(result[field_name], context="{}.{}".format(context, field_name))
    return result


def decrypt_secret_fields(payload: dict[str, Any], secret_fields: set[str], *, context: str) -> dict[str, Any]:
    result = dict(payload or {})
    for field_name in secret_fields:
        if field_name in result:
            result[field_name] = decrypt_secret(result[field_name], context="{}.{}".format(context, field_name))
    return result
