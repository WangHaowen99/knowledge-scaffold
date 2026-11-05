"""分块上传目标程序（契约 EV-31～EV-33；ADR-0082 约束 5）。

`DATA_DIR/evidence/uploads/<上传ID>/`：`meta.json` 和每块一个 `<序号>.part`。完成时按序拼进 blob 仓、边写边算 sha256；
主容器只拼块、算哈希，不看内容。每人同时最多 2 个没完成的上传，24 小时没完成的清掉。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..api.errors import ApiError
from . import storage

CHUNK_BYTES = 8 * 1024 * 1024
PENDING_MAX = 2
EXPIRE_S = 24 * 3600
MAX_ENV = "ONTO_WEB_EVIDENCE_MAX_ARTIFACT_MB"
FILENAME_MAX = 200
_COPY_BYTES = 1024 * 1024
_lock = threading.Lock()


def max_bytes() -> int:
    from ..governance import config_registry

    return int(config_registry.env_value(MAX_ENV)) * 1024 * 1024


def _dir() -> Path:
    return storage.root() / "uploads"


def _meta_path(upload_id: str) -> Path:
    return _dir() / upload_id / "meta.json"


def _chunk_count(size: int, chunk: int) -> int:
    return max(1, -(-size // chunk))


def _expected(meta: dict, index: int) -> int:
    count = _chunk_count(meta["size"], meta["chunk_bytes"])
    return meta["size"] - meta["chunk_bytes"] * (count - 1) if index == count - 1 else meta["chunk_bytes"]


def cleanup(now: float | None = None) -> int:
    """清掉超过 24 小时没完成的上传，返回清掉的个数。"""
    current = now if now is not None else time.time()
    removed = 0
    folder = _dir()
    if not folder.is_dir():
        return 0
    for entry in folder.iterdir():
        meta = storage.read_json(entry / "meta.json", None) if entry.is_dir() else None
        created = (meta or {}).get("created_at")
        if not isinstance(created, (int, float)):
            created = entry.stat().st_mtime
        if current - created > EXPIRE_S:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def _pending(owner: str) -> int:
    folder = _dir()
    if not folder.is_dir():
        return 0
    return sum(1 for entry in folder.iterdir()
               if entry.is_dir() and (storage.read_json(entry / "meta.json", {}) or {}).get("owner") == owner)


def start(owner: str, filename, size) -> dict:
    """EV-31。"""
    name = Path(str(filename or "")).name.strip()
    if not name or len(name) > FILENAME_MAX or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ApiError("invalid_argument", "要给出文件名和大于 0 的字节数", 400,
                       {"fields": {"filename": "required", "size": "positive_int"}})
    limit = max_bytes()
    if size > limit:
        raise ApiError("upload_too_large", "程序超过单文件上限", 413, {"max_bytes": limit})
    with _lock:
        cleanup()
        used = _pending(owner)
        if used >= PENDING_MAX:
            raise ApiError("quota_exceeded", "没完成的上传太多，先完成或等它过期", 429,
                           {"quota": "evidence_uploads_pending", "scope": "user", "limit": PENDING_MAX, "used": used,
                            "reset_at": None})
        upload_id = f"up-{uuid.uuid4().hex[:16]}"
        storage.write_json(_meta_path(upload_id), {"id": upload_id, "owner": owner, "filename": name, "size": size,
                                                   "chunk_bytes": CHUNK_BYTES, "created_at": time.time()})
    return {"upload_id": upload_id, "chunk_bytes": CHUNK_BYTES, "max_bytes": limit}


def _meta(upload_id: str, owner: str) -> dict:
    meta = storage.read_json(_meta_path(upload_id), None) if upload_id.startswith("up-") and "/" not in upload_id \
        and ".." not in upload_id else None
    if not isinstance(meta, dict) or meta.get("owner") != owner or time.time() - meta.get("created_at", 0) > EXPIRE_S:
        raise ApiError("upload_not_found", "上传不存在、已过期或不是本人的", 404)
    return meta


def _received(meta: dict) -> tuple[int, list[int]]:
    folder = _dir() / meta["id"]
    total, chunks = 0, []
    for index in range(_chunk_count(meta["size"], meta["chunk_bytes"])):
        part = folder / f"{index}.part"
        if part.is_file():
            total += part.stat().st_size
            chunks.append(index)
    return total, chunks


async def put_chunk(upload_id: str, index: int, owner: str, stream) -> dict:
    """EV-32：请求体边收边写，超出这一块的长度立即停。同一块重传以最后一次为准。"""
    meta = _meta(upload_id, owner)
    count = _chunk_count(meta["size"], meta["chunk_bytes"])
    if not 0 <= index < count:
        raise ApiError("invalid_argument", "块序号越界", 400, {"fields": {"index": f"0..{count - 1}"}})
    expected = _expected(meta, index)
    folder = _dir() / upload_id
    temporary = folder / f".{index}.{uuid.uuid4().hex[:8]}.tmp"
    written = 0
    try:
        with temporary.open("wb") as handle:
            async for piece in stream:
                written += len(piece)
                if written > expected:
                    break
                handle.write(piece)
        if written != expected:
            raise ApiError("invalid_argument", f"这一块应为 {expected} 字节", 400, {"fields": {"body": "chunk_size"}})
        os.replace(temporary, folder / f"{index}.part")
    finally:
        temporary.unlink(missing_ok=True)
    total, chunks = _received(meta)
    return {"received_bytes": total, "received_chunks": len(chunks)}


def complete(upload_id: str, owner: str, sha256) -> dict:
    """EV-33：按序拼块、边写边算哈希；对上了收进 blob 仓、删掉上传目录。"""
    meta = _meta(upload_id, owner)
    declared = str(sha256 or "").lower()
    if not storage.SHA256.match(declared):
        raise ApiError("invalid_argument", "sha256 要是 64 位十六进制", 400, {"fields": {"sha256": "sha256"}})
    count = _chunk_count(meta["size"], meta["chunk_bytes"])
    folder = _dir() / upload_id
    missing = [index for index in range(count) if not (folder / f"{index}.part").is_file()]
    if missing:
        raise ApiError("upload_incomplete", "还有块没传", 409, {"missing": missing})
    artifact = {"sha256": declared, "size": meta["size"], "filename": meta["filename"]}
    # 同一内容已经在 blob 仓里也照样拼一遍核对哈希（adopt_blob 再丢掉副本），免得错块被当成已有的程序
    digest = hashlib.sha256()
    temporary = storage.tmp_dir() / f"{upload_id}.{uuid.uuid4().hex[:8]}"
    try:
        with temporary.open("wb") as out:
            for index in range(count):
                with (folder / f"{index}.part").open("rb") as part:
                    while piece := part.read(_COPY_BYTES):
                        digest.update(piece)
                        out.write(piece)
        if digest.hexdigest() != declared:
            raise ApiError("upload_hash_mismatch", "拼好的文件和声明的 sha256 不一致", 422)
        storage.adopt_blob(temporary, declared)
    finally:
        temporary.unlink(missing_ok=True)
    shutil.rmtree(folder, ignore_errors=True)
    return {"artifact": artifact}
