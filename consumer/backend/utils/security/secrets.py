"""Encryption helpers for secrets that must be recovered at runtime."""

from __future__ import annotations

import base64
import os
import secrets
import stat
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


SECRET_ENCRYPTION_KEY_ENV = "SECRET_ENCRYPTION_KEY"
SECRET_ENCRYPTION_KEY_ID_ENV = "SECRET_ENCRYPTION_KEY_ID"
ENCRYPTED_SECRET_PREFIX = "enc:v1:"

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
PROJECT_ROOT_ENV = "PROJECT_ROOT"


class SecretEncryptionError(ValueError):
    """Raised when an encrypted secret cannot be decoded or decrypted."""


class SecretEncryptionKeyUnavailableError(SecretEncryptionError):
    """Raised when callers forbid creating a missing encryption key."""


def _resolve_default_runtime_root() -> Path:
    if REPO_ROOT == Path("/"):
        return BACKEND_ROOT / ".onto-runtime"
    return REPO_ROOT / ".onto-runtime"


def _resolve_runtime_root() -> Path:
    configured_root = os.getenv(PROJECT_ROOT_ENV)
    if configured_root:
        return Path(configured_root).expanduser()
    return _resolve_default_runtime_root()


def _key_file_path(runtime_root: Path | None = None) -> Path:
    resolved_root = Path(runtime_root).expanduser() if runtime_root is not None else _resolve_runtime_root()
    return resolved_root / "config" / "secret.key"


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(str(value or "").strip().encode("ascii"), validate=True)
    except Exception as exc:
        raise SecretEncryptionError("{} 必须是 base64 编码的 32 字节密钥".format(SECRET_ENCRYPTION_KEY_ENV)) from exc
    if len(key) != 32:
        raise SecretEncryptionError("{} 必须解码为 32 字节".format(SECRET_ENCRYPTION_KEY_ENV))
    return key


def load_key_for_test() -> bytes:
    """Load the configured key through the production synchronization path."""
    return _load_key()


def _read_key_file(path: Path) -> str:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        mode=0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SecretEncryptionError("secret 加密密钥文件无效")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_key(
    *,
    allow_creation: bool = True,
    runtime_root: Path | None = None,
) -> bytes:
    env_value = os.getenv(SECRET_ENCRYPTION_KEY_ENV)
    if env_value:
        return _decode_key(env_value)

    path = _key_file_path(runtime_root)
    if path.is_symlink():
        raise SecretEncryptionError("secret 加密密钥文件无效")
    if path.exists():
        if not path.is_file():
            raise SecretEncryptionError("secret 加密密钥文件无效")
        return _decode_key(_read_key_file(path))

    if not allow_creation:
        raise SecretEncryptionKeyUnavailableError("secret 加密密钥不可用")

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
            return _decode_key(_read_key_file(path))
    except FileExistsError:
        return _decode_key(_read_key_file(path))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
    return key


def validate_secret_encryption_key(
    *,
    runtime_root: Path | None = None,
) -> None:
    """Validate an existing key without creating local key material."""
    _load_key(allow_creation=False, runtime_root=runtime_root)


def _key_id() -> str:
    return str(os.getenv(SECRET_ENCRYPTION_KEY_ID_ENV) or "default").strip() or "default"


def _aad(context: str) -> bytes:
    return str(context or "").encode("utf-8")


def is_encrypted_secret(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENCRYPTED_SECRET_PREFIX)


def encrypt_secret(
    plaintext: str,
    *,
    context: str,
    allow_key_creation: bool = True,
    runtime_root: Path | None = None,
) -> str:
    value = str(plaintext or "")
    if not value:
        return ""
    if is_encrypted_secret(value):
        return value

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(
        _load_key(allow_creation=allow_key_creation, runtime_root=runtime_root)
    ).encrypt(nonce, value.encode("utf-8"), _aad(context))
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
    return "{}{}:{}:{}".format(ENCRYPTED_SECRET_PREFIX, _key_id(), nonce_b64, ciphertext_b64)


def decrypt_secret(
    value: str,
    *,
    context: str,
    allow_key_creation: bool = True,
    runtime_root: Path | None = None,
) -> str:
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
        plaintext = AESGCM(
            _load_key(allow_creation=allow_key_creation, runtime_root=runtime_root)
        ).decrypt(nonce, ciphertext, _aad(context))
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise SecretEncryptionError("secret 解密失败") from exc


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
