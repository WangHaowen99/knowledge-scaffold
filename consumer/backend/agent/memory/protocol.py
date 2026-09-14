# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Protocol for memory manager capabilities."""

from __future__ import annotations

from typing import List, Optional, Protocol

from agent.memory.models import MemoryHit, MemoryStoreResult


class MemoryManager(Protocol):
    """Runtime protocol for durable user memory."""

    user_id: str

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
        k: int = 5,
        include_archived: bool = False,
    ) -> List[MemoryHit]:
        ...

    def list_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> List[MemoryHit]:
        ...

    def build_memory_context(
        self,
        *,
        query: str,
        limit: int = 3,
    ) -> str:
        ...
