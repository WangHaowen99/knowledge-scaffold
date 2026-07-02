"""Workspace-aware store manager for chat and execution history."""

from __future__ import annotations

import re
import threading
from typing import Dict, Tuple

from .chat_store import ChatStore
from .execution_store import ExecutionStore
from ..workspaces import get_workspace_paths


class UserStoreManager:
    def __init__(self) -> None:
        self._chat_stores: Dict[Tuple[str, str], ChatStore] = {}
        self._execution_stores: Dict[Tuple[str, str], ExecutionStore] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _safe_user_dir(user_id: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", user_id or "user")
        return safe or "user"

    def get_chat_store(self, user_id: str, workspace_id: str) -> ChatStore:
        safe_user = self._safe_user_dir(user_id)
        key = (workspace_id, safe_user)
        with self._lock:
            store = self._chat_stores.get(key)
            if store is None:
                paths = get_workspace_paths(workspace_id)
                path = paths.users_runtime_dir / safe_user / "chats"
                store = ChatStore(str(path))
                self._chat_stores[key] = store
            return store

    def get_execution_store(self, user_id: str, workspace_id: str) -> ExecutionStore:
        safe_user = self._safe_user_dir(user_id)
        key = (workspace_id, safe_user)
        with self._lock:
            store = self._execution_stores.get(key)
            if store is None:
                paths = get_workspace_paths(workspace_id)
                path = paths.users_runtime_dir / safe_user / "executions"
                store = ExecutionStore(str(path))
                self._execution_stores[key] = store
            return store

    def prune_sessions(self, user_id: str, workspace_id: str, *, max_sessions: int) -> list[str]:
        """Age out old chat sessions and matching execution records for one user/workspace."""
        chat_store = self.get_chat_store(user_id, workspace_id)
        execution_store = self.get_execution_store(user_id, workspace_id)
        deleted_session_ids = chat_store.prune_old_sessions(max_sessions)
        for session_id in deleted_session_ids:
            execution_store.delete_session(session_id)
        return deleted_session_ids
