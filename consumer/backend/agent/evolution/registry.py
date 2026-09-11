"""Process-wide singleton accessor for the proposal buffer.

Multiple subsystems share one ProposalBuffer instance so its in-process thread
locks remain meaningful. Storage path is derived from `AppConfig.project_root`
once on first access.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Optional

from agent.evolution.buffer import ProposalBuffer
from agent.evolution.patcher import OperationPatcher, PatcherConfig
from utils.logging import get_logger

logger = get_logger("agent.evolution.registry")

_INSTANCE: Optional[ProposalBuffer] = None
_PATCHER: Optional[OperationPatcher] = None
_INSTANCE_LOCK = Lock()
_PATCHER_LOCK = Lock()


def get_proposal_buffer(project_root: Optional[str] = None) -> ProposalBuffer:
    """Return the shared ProposalBuffer.

    First call resolves storage to `<project_root>/operations-evolution`.
    Subsequent calls ignore `project_root` — the singleton is bound to the
    first path it sees. Tests that need a fresh instance should call
    `reset_proposal_buffer()` first.
    """
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is not None:
            return _INSTANCE
        if not project_root:
            from config.settings import AppConfig

            project_root = AppConfig.from_env().project_root
        root = Path(project_root).expanduser() / "operations-evolution"
        _INSTANCE = ProposalBuffer(str(root))
        return _INSTANCE


def reset_proposal_buffer() -> None:
    """Drop the cached singleton. Test-only."""
    global _INSTANCE
    with _INSTANCE_LOCK:
        _INSTANCE = None


def init_operation_patcher(
    *,
    config: Optional[PatcherConfig] = None,
    project_root: Optional[str] = None,
) -> OperationPatcher:
    """Initialize the process-wide operation patcher and bind it to the buffer."""
    global _PATCHER
    with _PATCHER_LOCK:
        resolved_config = config or PatcherConfig()
        if _PATCHER is not None:
            _PATCHER.update_config(resolved_config)
            return _PATCHER
        buffer = get_proposal_buffer(project_root)
        patcher = OperationPatcher(
            buffer=buffer,
            config=resolved_config,
        )
        buffer.set_change_listener(patcher.on_buffer_change)
        _PATCHER = patcher
        return patcher


def get_operation_patcher() -> Optional[OperationPatcher]:
    return _PATCHER


def reset_operation_patcher() -> None:
    """Stop and drop the cached operation patcher. Test-only."""
    global _PATCHER
    with _PATCHER_LOCK:
        patcher = _PATCHER
        _PATCHER = None
    if patcher is not None:
        try:
            patcher.cancel_nowait()
        finally:
            try:
                get_proposal_buffer().set_change_listener(None)
            except Exception as exc:
                logger.warning("proposal buffer listener cleanup failed: %s", exc, exc_info=True)
