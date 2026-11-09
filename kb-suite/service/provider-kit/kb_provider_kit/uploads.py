"""Chunked upload sessions for adapters (contract feature ``uploads``).

Parts are kept on the adapter's local disk; ``assemble`` checks size and
sha256 and returns the whole file, which the adapter then ingests the way its
backend accepts files.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path

from . import Ids, ProviderError

_RAW_ID = re.compile(r"^[0-9a-f]{32}$")
SESSION_TTL_S = 24 * 3600


class UploadStore:
    def __init__(self, root: str | Path, ids: Ids, part_mb: int = 8) -> None:
        self.root = Path(root)
        self.ids = ids
        self.part_size = part_mb * 1024 * 1024

    def _dir(self, upload_id: str) -> Path:
        raw = self.ids.inp(upload_id, "上传会话")
        if not _RAW_ID.match(raw):
            raise ProviderError(404, "not_found", "上传会话不存在")
        return self.root / raw

    def _meta(self, upload_id: str) -> dict:
        meta = self._dir(upload_id) / "meta.json"
        if not meta.exists():
            raise ProviderError(404, "not_found", "上传会话不存在或已过期")
        return json.loads(meta.read_text(encoding="utf-8"))

    def _sweep(self) -> None:
        if not self.root.exists():
            return
        cutoff = time.time() - SESSION_TTL_S
        for entry in self.root.iterdir():
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)

    def begin(self, body: dict, max_bytes: int) -> dict:
        file_name = Path(str(body.get("file_name") or "")).name
        file_size = int(body.get("file_size") or 0)
        if not body.get("kb_id") or not file_name or file_size <= 0:
            raise ProviderError(400, "bad_request", "需要 kb_id、file_name 和正数 file_size")
        if file_size > max_bytes:
            raise ProviderError(413, "too_large", f"文件超过 {max_bytes // (1024 * 1024)}MB 上限")
        self._sweep()
        raw = uuid.uuid4().hex
        folder = self.root / raw
        folder.mkdir(parents=True)
        meta = {
            "kb_id": str(body["kb_id"]), "file_name": file_name, "file_size": file_size,
            "sha256": str(body.get("sha256") or "").lower(), "folder_path": str(body.get("folder_path") or ""),
            "part_count": max(1, math.ceil(file_size / self.part_size)),
        }
        (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return {"upload_id": self.ids.out(raw), "part_size": self.part_size, "part_count": meta["part_count"]}

    def put(self, upload_id: str, index: int, data: bytes) -> dict:
        meta = self._meta(upload_id)
        if not 0 <= index < meta["part_count"]:
            raise ProviderError(400, "bad_part", f"分块序号越界：{index}")
        if len(data) > self.part_size:
            raise ProviderError(400, "bad_part", "分块超过约定大小")
        (self._dir(upload_id) / f"{index:06d}").write_bytes(data)
        return {"upload_id": upload_id, "index": index, "size": len(data)}

    def assemble(self, upload_id: str) -> tuple[dict, bytes]:
        """(session meta, file bytes); call ``drop`` once the file is ingested."""

        meta = self._meta(upload_id)
        folder = self._dir(upload_id)
        parts = []
        for index in range(meta["part_count"]):
            part = folder / f"{index:06d}"
            if not part.exists():
                raise ProviderError(400, "missing_part", f"缺少分块 {index}")
            parts.append(part.read_bytes())
        data = b"".join(parts)
        if len(data) != meta["file_size"] or (meta["sha256"] and hashlib.sha256(data).hexdigest() != meta["sha256"]):
            raise ProviderError(400, "checksum_mismatch", "文件大小或校验值不匹配")
        return meta, data

    def drop(self, upload_id: str) -> None:
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)
