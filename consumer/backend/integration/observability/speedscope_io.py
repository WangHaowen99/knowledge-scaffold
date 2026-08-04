"""Shared I/O coordination for Speedscope session files."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import threading
from typing import Iterator, Optional

from utils.identifiers import safe_user_storage_key, validate_session_id
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


def speedscope_session_file_name(session_id: str) -> str:
    """Return the stable attachment name for one session trace."""
    normalized_session_id = validate_session_id(session_id)
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", normalized_session_id).strip("._-")
    normalized = (normalized or "session")[:64]
    digest = hashlib.sha256(normalized_session_id.encode("utf-8")).hexdigest()[:12]
    return "session-{}-{}.speedscope.json".format(normalized, digest)


def speedscope_session_path(
    output_dir: str | Path,
    session_id: str,
    *,
    user_id: str | None = None,
) -> Path:
    """Resolve a session file below the configured output directory.

    Production writers pass a user id so traces are isolated within the
    workspace. The root-level fallback keeps direct exporter usage compatible
    with older callers that do not attach an owner to their synthetic events.
    """
    root = Path(output_dir)
    if user_id:
        root = root / safe_user_storage_key(user_id)
    return root / speedscope_session_file_name(session_id)


def delete_speedscope_session_files(
    output_dir: str | Path,
    session_id: str,
    *,
    user_id: str | None = None,
) -> int:
    """Delete all matching session files and lock files, returning a count."""
    filename = speedscope_session_file_name(session_id)
    root = Path(output_dir)
    candidates = [root / filename]
    if user_id:
        candidates.append(root / safe_user_storage_key(user_id) / filename)
    elif root.is_dir():
        candidates.extend(root.glob("*/{}".format(filename)))

    deleted = 0
    for path in dict.fromkeys(candidates):
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            speedscope_session_lock_path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            continue
    return deleted


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
