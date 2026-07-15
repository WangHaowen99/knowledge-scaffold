"""Shared I/O coordination for Speedscope session files."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Linux deployments use fcntl.
    fcntl = None

_FILE_LOCK = threading.RLock()


@contextmanager
def speedscope_file_lock(path: Optional[str | Path] = None) -> Iterator[None]:
    """Serialize read-merge-write cycles for session Speedscope documents."""
    with _FILE_LOCK:
        if path is None or fcntl is None:
            yield
            return

        lock_path = Path(path).with_name("{}.lock".format(Path(path).name))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
