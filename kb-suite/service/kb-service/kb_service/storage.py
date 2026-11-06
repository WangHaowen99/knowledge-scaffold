"""File storage on a local volume (S3/MinIO backend can replace it later)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import get_settings


def _root() -> Path:
    return Path(get_settings().files_dir)


def _resolve(key: str) -> Path:
    root = _root().resolve()
    target = (root / key).resolve()
    if root not in target.parents and target != root:
        raise ValueError(f"storage key escapes root: {key}")
    return target


def put_bytes(key: str, data: bytes) -> str:
    path = _resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return key


def put_text(key: str, text: str) -> str:
    return put_bytes(key, text.encode("utf-8"))


def get_bytes(key: str) -> bytes:
    return _resolve(key).read_bytes()


def get_text(key: str) -> str:
    return get_bytes(key).decode("utf-8")


def exists(key: str) -> bool:
    return _resolve(key).exists()


def path_of(key: str) -> Path:
    return _resolve(key)


def delete_prefix(prefix: str) -> None:
    path = _resolve(prefix)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def doc_prefix(tenant_id: str, kb_id: str, doc_id: str) -> str:
    return f"{tenant_id}/{kb_id}/{doc_id}"
