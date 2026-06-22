"""Per-user factory for pluggable memory manager backends."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from ..config.system_config import ONTO_RUNTIME_ROOT, load_system_config
from ..utils.logging import get_logger
from .filesystem_manager import FileSystemMemoryManager
from .openviking_manager import OpenVikingMemoryManager
from .protocol import MemoryManager


logger = get_logger("memory.factory")

_SUPPORTED_MEMORY_MODES = {"filesystem", "openviking"}


class MemoryFactory:
    """Create and cache one memory manager per user for the active backend."""

    def __init__(
        self,
        workspace_root: Optional[str | Path] = None,
        *,
        client_cls: Any = None,
        recall_topk: int = 3,
        recall_threshold: Optional[float] = None,
    ) -> None:
        system_config = load_system_config()
        memory_config = dict((system_config.get("memory") or {}))
        self._mode = self._resolve_mode(memory_config)
        self._workspace_root = self._resolve_workspace_root(
            workspace_root=workspace_root,
            memory_config=memory_config,
            system_config=system_config,
            mode=self._mode,
        )
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._client_cls = client_cls
        self._recall_topk = int(memory_config.get("recall_topk") or recall_topk or 3)
        configured_threshold = memory_config.get("recall_threshold")
        self._recall_threshold = (
            float(configured_threshold)
            if configured_threshold not in {None, ""}
            else recall_threshold
        )
        self._lock = threading.Lock()
        self._cache: Dict[str, MemoryManager] = {}
        self._disabled_reason: Optional[str] = None

    @staticmethod
    def _resolve_mode(memory_config: Dict[str, Any]) -> str:
        configured = os.getenv("MEMORY_MODE")
        if configured in {None, ""}:
            configured = memory_config.get("mode")
        normalized = str(configured or "filesystem").strip().lower()
        if normalized not in _SUPPORTED_MEMORY_MODES:
            logger.warning("Unsupported MEMORY_MODE %s; falling back to filesystem", normalized)
            return "filesystem"
        return normalized

    @staticmethod
    def _resolve_workspace_root(
        *,
        workspace_root: Optional[str | Path],
        memory_config: Dict[str, Any],
        system_config: Dict[str, Dict[str, Any]],
        mode: str,
    ) -> Path:
        if workspace_root:
            return Path(workspace_root)

        configured_root = str(memory_config.get("workspace_root") or "").strip()
        if configured_root:
            return Path(configured_root)

        if mode == "openviking":
            legacy_root = os.getenv("OPENVIKING_WORKSPACE_ROOT")
            if legacy_root:
                return Path(legacy_root)
            return Path(__file__).resolve().parents[2] / "data" / "openviking"

        storage_config = dict((system_config.get("storage") or {}))
        project_root = str(storage_config.get("project_root") or "").strip()
        if project_root:
            return Path(project_root) / "memory"

        return ONTO_RUNTIME_ROOT / "memory"

    def for_user(self, user_id: Optional[str]) -> Optional[MemoryManager]:
        if self._mode == "openviking":
            return self._get_openviking_manager(user_id)
        return self._get_filesystem_manager(user_id)

    def _get_filesystem_manager(self, user_id: Optional[str]) -> FileSystemMemoryManager:
        key = self._safe_user_dir(user_id or "anonymous")
        with self._lock:
            manager = self._cache.get(key)
            if not isinstance(manager, FileSystemMemoryManager):
                manager = FileSystemMemoryManager(
                    user_id=user_id or "anonymous",
                    workspace_dir=self._workspace_root / key,
                    recall_topk=self._recall_topk,
                    recall_threshold=self._recall_threshold,
                )
                self._cache[key] = manager
            return manager

    def _get_openviking_manager(self, user_id: Optional[str]) -> Optional[OpenVikingMemoryManager]:
        key = self._safe_user_dir(user_id or "anonymous")
        with self._lock:
            if self._disabled_reason is not None:
                return None
            manager = self._cache.get(key)
            if not isinstance(manager, OpenVikingMemoryManager):
                manager = OpenVikingMemoryManager(
                    user_id=user_id or "anonymous",
                    workspace_dir=self._workspace_root / key,
                    client_cls=self._client_cls,
                    recall_topk=self._recall_topk,
                    recall_threshold=self._recall_threshold,
                )
                try:
                    manager.client
                except Exception as exc:
                    if self._should_disable(exc):
                        self._disabled_reason = str(exc) or exc.__class__.__name__
                        logger.warning("OpenViking memory disabled: %s", self._disabled_reason)
                        return None
                    raise
                self._cache[key] = manager
            return manager

    @staticmethod
    def _safe_user_dir(user_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id or "user")
        return safe or "user"

    @staticmethod
    def _should_disable(exc: Exception) -> bool:
        if isinstance(exc, FileNotFoundError):
            return True

        message = str(exc).lower()
        return (
            "configuration file not found" in message
            or "openviking is not installed" in message
        )
