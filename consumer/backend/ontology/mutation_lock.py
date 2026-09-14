# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Cross-thread and cross-process serialization for ontology mutations."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

try:  # Production deployments are POSIX; retain thread safety elsewhere.
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None  # type: ignore


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_STATE = threading.local()


@contextmanager
def ontology_mutation_lock(ontology_dir: Path):
    """Serialize all filesystem mutations for one ontology.

    Thread-local ownership makes nested helpers reentrant without opening a
    second flock descriptor, which could otherwise self-deadlock.
    """
    ontology_dir = Path(ontology_dir).resolve()
    lock_path = ontology_dir.parent / ".{}.install.lock".format(ontology_dir.name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(lock_key, threading.RLock())
    with thread_lock:
        held_keys = getattr(_LOCK_STATE, "held_keys", None)
        if held_keys is None:
            held_keys = set()
            _LOCK_STATE.held_keys = held_keys
        if lock_key in held_keys:
            yield
            return
        stream = lock_path.open("a+")
        try:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            held_keys.add(lock_key)
            yield
        finally:
            held_keys.discard(lock_key)
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
