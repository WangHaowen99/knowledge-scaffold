# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Narrow deterministic capability for publishing text artifacts."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path


def write_artifact(name: str, content: str) -> str:
    """Write one UTF-8 artifact inside the run-owned output directory."""
    normalized_name = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not normalized_name or normalized_name in {".", ".."}:
        raise ValueError("artifact name must be a plain file name")
    if Path(normalized_name).name != normalized_name:
        raise ValueError("artifact name must be a plain file name")
    if "/" in normalized_name or "\\" in normalized_name:
        raise ValueError("artifact name must be a plain file name")
    if not isinstance(content, str):
        raise TypeError("artifact content must be text")

    root_value = str(os.environ.get("RUN_ARTIFACT_DIR") or "").strip()
    if not root_value:
        raise RuntimeError("artifact capability is unavailable")
    root = Path(root_value)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("artifact capability root is invalid")

    payload = content.encode("utf-8")
    try:
        max_bytes = max(1, int(os.environ.get("RUN_ARTIFACT_MAX_FILE_BYTES") or 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("artifact capability limit is invalid") from exc
    if len(payload) > max_bytes:
        raise ValueError("artifact content exceeds the per-file limit")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(root / normalized_name, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("artifact write did not make progress")
            view = view[written:]
    finally:
        os.close(descriptor)
    return normalized_name
