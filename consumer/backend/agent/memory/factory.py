"""Per-user factory for the filesystem memory manager backend."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional

from config.system_config import ONTO_RUNTIME_ROOT, load_system_config
from utils.logging import get_logger
from utils.identifiers import safe_user_storage_key
from agent.memory.filesystem_manager import FileSystemMemoryManager
from agent.memory.protocol import MemoryManager


logger = get_logger("memory.factory")


class MemoryFactory:
    """Create and cache one filesystem memory manager per user."""

    def __init__(
        self,
        workspace_root: Optional[str | Path] = None,
        *,
        recall_topk: int = 3,
        recall_threshold: Optional[float] = None,
    ) -> None:
        system_config = load_system_config()
        memory_config = dict((system_config.get("memory") or {}))
        self._workspace_root = self._resolve_workspace_root(
            workspace_root=workspace_root,
            memory_config=memory_config,
            system_config=system_config,
        )
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._recall_topk = int(memory_config.get("recall_topk") or recall_topk or 3)
        configured_threshold = memory_config.get("recall_threshold")
        self._recall_threshold = (
            float(configured_threshold)
            if configured_threshold not in {None, ""}
            else recall_threshold
        )
        self._lock = threading.Lock()
        self._cache: Dict[str, MemoryManager] = {}

    @staticmethod
    def _resolve_workspace_root(
        *,
        workspace_root: Optional[str | Path],
        memory_config: Dict[str, Any],
        system_config: Dict[str, Dict[str, Any]],
    ) -> Path:
        if workspace_root:
            return Path(workspace_root)

        configured_root = str(memory_config.get("workspace_root") or "").strip()
        if configured_root:
            return Path(configured_root)

        storage_config = dict((system_config.get("storage") or {}))
        project_root = str(storage_config.get("project_root") or "").strip()
        if project_root:
            return Path(project_root) / "memory"

        return ONTO_RUNTIME_ROOT / "memory"

    def for_user(self, user_id: Optional[str]) -> FileSystemMemoryManager:
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

    @staticmethod
    def _safe_user_dir(user_id: str) -> str:
        return safe_user_storage_key(user_id)
