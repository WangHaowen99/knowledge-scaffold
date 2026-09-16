# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Filesystem-backed memory manager with zero external dependencies."""

from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone
from operator import getitem
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from agent.memory.models import (
    MEMORY_TYPE_TO_DIR,
    MemoryHit,
    MemoryStoreResult,
    normalize_for_dedupe,
    normalize_memory_scope,
    normalize_memory_type,
)
from agent.memory.utils import (
    parse_frontmatter,
    render_memory_note,
    utc_now_iso,
)
from utils.logging import get_logger

logger = get_logger("memory.filesystem")

DEFAULT_RECALL_LIMIT = 3
FILESYSTEM_MEMORY_VERSION = "filesystem-v1"


class FileSystemMemoryManager:
    """Persist memories as markdown files under a per-user workspace."""

    memory_version = FILESYSTEM_MEMORY_VERSION

    def __init__(
        self,
        *,
        user_id: str,
        workspace_dir: Path,
        recall_topk: int = DEFAULT_RECALL_LIMIT,
        recall_threshold: Optional[float] = None,
    ) -> None:
        self.user_id = user_id or "anonymous"
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._memories_dir = self.workspace_dir / "memories"
        self._memories_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.workspace_dir / "MEMORY.md"
        self._lock = threading.Lock()
        self._recall_topk = max(1, int(recall_topk or DEFAULT_RECALL_LIMIT))
        self._recall_threshold = recall_threshold

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
        normalized_content = str(content or "").strip()
        if len(normalized_content) < 8:
            raise ValueError("Memory content is too short. Include concrete context.")

        normalized_type = normalize_memory_type(memory_type)
        normalized_scope = normalize_memory_scope(scope)
        project_key = None
        app_session_id = None

        dedupe_content = normalize_for_dedupe(normalized_content)
        fingerprint = hashlib.sha256(
            "||".join(
                [
                    normalized_type,
                    normalized_scope,
                    str(project_key or ""),
                    str(app_session_id or ""),
                    dedupe_content,
                ]
            ).encode("utf-8")
        ).hexdigest()
        note_dir = self._resolve_storage_dir(
            memory_type=normalized_type,
            scope=normalized_scope,
            project_key=project_key,
            app_session_id=app_session_id,
        )
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = note_dir / "{}-{}.md".format(normalized_type, fingerprint)

        with self._lock:
            matching_path = self._find_matching_note_path(
                note_dir,
                memory_type=normalized_type,
                scope=normalized_scope,
                dedupe_content=dedupe_content,
                preferred_path=note_path,
            )
            if matching_path is not None:
                note_path = matching_path
                prefix = "{}-".format(normalized_type)
                if note_path.stem.startswith(prefix):
                    fingerprint = note_path.stem[len(prefix):]
            merged = False
            existing_hit_count = 0
            now = utc_now_iso()
            created_at = now
            if note_path.exists():
                existing_meta, _existing_body = parse_frontmatter(
                    note_path.read_text(encoding="utf-8")
                )
                merged = True
                existing_hit_count = int(existing_meta.get("hit_count") or 0)
                created_at = (
                    str(existing_meta.get("created_at") or existing_meta.get("stored_at") or "").strip()
                    or now
                )

            # Re-seeing a memory counts as an implicit "still relevant" hit.
            next_hit_count = existing_hit_count + (1 if merged else 0)
            note_path.write_text(
                render_memory_note(
                    user_id=self.user_id,
                    content=normalized_content,
                    memory_type=normalized_type,
                    scope=normalized_scope,
                    project_key=project_key,
                    app_session_id=app_session_id,
                    source=source,
                    hit_count=next_hit_count,
                    archived=False,
                    created_at=created_at,
                    updated_at=now,
                ),
                encoding="utf-8",
            )
            self._rebuild_index()

        return MemoryStoreResult(
            stored=not merged,
            memory_id=fingerprint,
            memory_type=normalized_type,
            scope=normalized_scope,
            source_uri=str(note_path),
            project_key=None,
            hit_count=next_hit_count,
            merged=merged,
            created_at=created_at,
            updated_at=now,
        )

    def archive_memory(
        self,
        *,
        source_uri: str,
        reason: Optional[str] = None,
    ) -> bool:
        del reason
        raw_path = str(source_uri or "").strip()
        if not raw_path:
            return False
        note_path = Path(raw_path)
        try:
            note_path = note_path.resolve()
            note_path.relative_to(self._memories_dir.resolve())
        except Exception:
            return False
        if not note_path.exists() or not note_path.is_file():
            return False

        with self._lock:
            metadata, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
            memory_type = str(metadata.get("memory_type") or "").strip()
            scope = str(metadata.get("scope") or "user").strip() or "user"
            if memory_type not in MEMORY_TYPE_TO_DIR:
                return False
            now = utc_now_iso()
            note_path.write_text(
                render_memory_note(
                    user_id=self.user_id,
                    content=body,
                    memory_type=memory_type,
                    scope=scope,
                    project_key=None,
                    app_session_id=None,
                    source=str(metadata.get("source") or "explicit"),
                    hit_count=int(metadata.get("hit_count") or 0),
                    archived=True,
                    created_at=str(metadata.get("created_at") or metadata.get("stored_at") or now),
                    updated_at=now,
                ),
                encoding="utf-8",
            )
            self._rebuild_index()
        return True

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
        del query
        normalized_scope = normalize_memory_scope(scope)
        normalized_type = normalize_memory_type(memory_type) if memory_type else None
        limit = max(1, int(k or self._recall_topk))

        hits: List[tuple[datetime, MemoryHit]] = []
        for note_path in self._iter_note_paths():
            metadata, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
            note_type = str(metadata.get("memory_type") or "").strip()
            note_scope = str(metadata.get("scope") or "user").strip() or "user"
            note_archived = bool(metadata.get("archived") or False)
            if note_archived and not include_archived:
                continue
            if note_type not in MEMORY_TYPE_TO_DIR:
                continue
            if note_scope != "user":
                continue
            if normalized_type and note_type != normalized_type:
                continue

            if note_scope != normalized_scope:
                continue

            created_at = self._metadata_time_iso(metadata, note_path, "created_at", "stored_at")
            updated_at = self._metadata_time_iso(metadata, note_path, "updated_at", "stored_at")
            hits.append(
                (
                    self._parse_time(updated_at, note_path),
                    MemoryHit(
                        source_uri=str(note_path),
                        score=None,
                        content=body,
                        memory_type=note_type,
                        scope=note_scope,
                        hit_count=int(metadata.get("hit_count") or 0),
                        archived=note_archived,
                        created_at=created_at,
                        updated_at=updated_at,
                    ),
                )
            )

        hits.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in hits[:limit]]

    def list_memories(
        self,
        *,
        memory_type: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> List[MemoryHit]:
        """List the current user's ordinary memories, newest first.

        Unlike ``recall_memory`` (which is query-driven), this enumerates every
        stored note so the user-memory management UI can show the full set.
        """
        normalized_scope = "user"
        normalized_type = normalize_memory_type(memory_type) if memory_type else None
        limit = max(1, int(limit or 100))

        hits: List[tuple[datetime, MemoryHit]] = []
        for note_path in self._iter_note_paths():
            metadata, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
            note_type = str(metadata.get("memory_type") or "").strip()
            note_scope = str(metadata.get("scope") or "user").strip() or "user"
            note_archived = bool(metadata.get("archived") or False)
            if note_archived and not include_archived:
                continue
            if note_type not in MEMORY_TYPE_TO_DIR:
                continue
            if note_scope != "user":
                continue
            if normalized_type and note_type != normalized_type:
                continue

            if note_scope != normalized_scope:
                continue

            created_at = self._metadata_time_iso(metadata, note_path, "created_at", "stored_at")
            updated_at = self._metadata_time_iso(metadata, note_path, "updated_at", "stored_at")
            hits.append(
                (
                    self._parse_time(updated_at, note_path),
                    MemoryHit(
                        source_uri=str(note_path),
                        score=None,
                        content=body,
                        memory_type=note_type,
                        scope=note_scope,
                        hit_count=int(metadata.get("hit_count") or 0),
                        archived=note_archived,
                        created_at=created_at,
                        updated_at=updated_at,
                    ),
                )
            )

        hits.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in hits[:limit]]

    def build_memory_context(
        self,
        *,
        query: str,
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> str:
        del query, limit
        if not self._index_path.exists():
            with self._lock:
                self._rebuild_index()
        if not self._index_path.exists():
            return ""

        body = self._index_path.read_text(encoding="utf-8").strip()
        if not body:
            return ""
        return "## Relevant Long-Term Memory\n{}".format(body)

    def _resolve_storage_dir(
        self,
        *,
        memory_type: str,
        scope: str,
        project_key: Optional[str],
        app_session_id: Optional[str],
    ) -> Path:
        dir_name = MEMORY_TYPE_TO_DIR[memory_type]
        del scope, project_key, app_session_id
        return self._memories_dir / dir_name

    def _iter_note_paths(self) -> List[Path]:
        return sorted(
            [
                path
                for path in self._memories_dir.rglob("*.md")
                if path.is_file()
            ]
        )

    @staticmethod
    def _find_matching_note_path(
        note_dir: Path,
        *,
        memory_type: str,
        scope: str,
        dedupe_content: str,
        preferred_path: Path,
    ) -> Optional[Path]:
        candidates = [preferred_path] if preferred_path.is_file() else []
        candidates.extend(
            path for path in sorted(note_dir.glob("*.md")) if path != preferred_path
        )
        for candidate in candidates:
            metadata, body = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            candidate_type = str(metadata.get("memory_type") or "").strip().lower()
            candidate_scope = str(metadata.get("scope") or "user").strip().lower() or "user"
            if (
                candidate_type == memory_type
                and candidate_scope == scope
                and normalize_for_dedupe(body) == dedupe_content
            ):
                return candidate
        return None

    def _rebuild_index(self) -> None:
        grouped: Dict[str, List[str]] = {}
        for note_path in self._iter_note_paths():
            metadata, body = parse_frontmatter(note_path.read_text(encoding="utf-8"))
            if bool(metadata.get("archived") or False):
                continue
            note_type = str(metadata.get("memory_type") or "").strip()
            note_scope = str(metadata.get("scope") or "user").strip() or "user"
            if note_type not in MEMORY_TYPE_TO_DIR:
                continue
            if note_scope != "user":
                continue

            grouped.setdefault(note_type, []).append(
                "- [user] {}".format(body.strip())
            )

        lines: List[str] = []
        for memory_type in sorted(grouped):
            if lines:
                lines.append("")
            lines.append("### {}".format(memory_type))
            lines.extend(getitem(grouped, memory_type))

        self._index_path.write_text("\n".join(lines).strip() + ("\n" if lines else ""), encoding="utf-8")

    @staticmethod
    def _metadata_time_iso(metadata: Dict[str, Any], note_path: Path, key: str, fallback_key: str) -> str:
        raw_value = metadata.get(key) or metadata.get(fallback_key)
        parsed = FileSystemMemoryManager._parse_time(raw_value, note_path)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_time(raw_value: Any, note_path: Path) -> datetime:
        text = str(raw_value or "").strip()
        if text:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.fromtimestamp(note_path.stat().st_mtime, tz=timezone.utc)
