"""Shared I/O coordination for Speedscope session files."""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator

_FILE_LOCK = threading.RLock()


@contextmanager
def speedscope_file_lock() -> Iterator[None]:
    """Serialize read-merge-write cycles for session Speedscope documents."""
    with _FILE_LOCK:
        yield
