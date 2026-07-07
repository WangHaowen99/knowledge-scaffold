"""Protocol for interchangeable memory manager backends."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from .models import MemoryHit, MemoryStoreResult


class MemoryManager(Protocol):
    """Runtime protocol shared by all memory manager implementations."""

    user_id: str

    def ensure_session(
        self,
        *,
        app_session_id: str,
        existing_session_id: Optional[str] = None,
    ) -> Optional[str]:
        ...

    def delete_session(self, session_id: Optional[str]) -> None:
        ...

    def append_session_message(
        self,
        *,
        ov_session_id: str,
        role: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        ...

    def append_turn_summary(
        self,
        *,
        ov_session_id: str,
        user_request: str,
        final_output: Any,
        status: str,
    ) -> None:
        ...

    def commit_session(self, ov_session_id: Optional[str]) -> None:
        ...

    def store_memory(
        self,
        *,
        content: str,
        memory_type: str,
        scope: str = "user",
        project_key: Optional[str] = None,
        app_session_id: Optional[str] = None,
        source: str = "explicit",
    ) -> MemoryStoreResult:
        ...

    def archive_memory(
        self,
        *,
        source_uri: str,
        reason: Optional[str] = None,
    ) -> bool:
        ...

    def recall_memory(
        self,
        *,
        query: str,
        memory_type: Optional[str] = None,
        scope: str = "user",
        project_key: Optional[str] = None,
        app_session_id: Optional[str] = None,
        ov_session_id: Optional[str] = None,
        k: int = 5,
        include_archived: bool = False,
    ) -> List[MemoryHit]:
        ...

    def build_memory_context(
        self,
        *,
        query: str,
        ov_session_id: Optional[str],
        limit: int = 3,
    ) -> str:
        ...
