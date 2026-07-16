"""Shared I/O coordination for Speedscope session files."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
from typing import Iterator, Optional

from utils.logging import LOG_DIR

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux deployments use fcntl.
    fcntl = None

_FILE_LOCK = threading.RLock()
LOG_DIR_ENV = "LOG_DIR"
PROJECT_ROOT_ENV = "PROJECT_ROOT"
DIRECTORY_LOCK_FILE = ".speedscope.write.lock"


def resolve_speedscope_output_dir(output_dir: Optional[str | Path] = None) -> Path:
    """Return the runtime Speedscope directory shared by all profile writers."""
    if output_dir is not None:
        return Path(output_dir)
    configured_log_dir = str(os.getenv(LOG_DIR_ENV) or "").strip()
    if configured_log_dir:
        return Path(configured_log_dir) / "speedscope"
    configured_project_root = str(os.getenv(PROJECT_ROOT_ENV) or "").strip()
    if configured_project_root:
        return Path(configured_project_root) / "logs" / "speedscope"
    return Path(LOG_DIR) / "speedscope"


def speedscope_directory_lock_path(path: str | Path) -> Path:
    return Path(path).parent / DIRECTORY_LOCK_FILE


def speedscope_session_lock_path(path: str | Path) -> Path:
    target = Path(path)
    return target.with_name("{}.lock".format(target.name))


@contextmanager
def speedscope_file_lock(path: Optional[str | Path] = None) -> Iterator[None]:
    """Serialize read-merge-write cycles for session Speedscope documents."""
    with _FILE_LOCK:
        if path is None or fcntl is None:
            yield
            return

        lock_path = speedscope_directory_lock_path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
