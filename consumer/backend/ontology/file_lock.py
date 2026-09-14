# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Small cross-process advisory locks for workspace-local coordination."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator

try:  # Production deployments are POSIX; retain thread safety elsewhere.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None  # type: ignore


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize threads and cooperating processes on one trusted lock file.

    ``flock`` supplies the cross-process boundary while the process-local
    ``RLock`` keeps the semantics deterministic across platforms where locks
    acquired through separate descriptors in one process may not contend.
    The caller decides the critical-section size; no SQLite transaction is
    held by this helper.
    """

    lock_path = path.resolve(strict=False)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock_for(lock_path)
    with process_lock:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _process_lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[path] = lock
        return lock
