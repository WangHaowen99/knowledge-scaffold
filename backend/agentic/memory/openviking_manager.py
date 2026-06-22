"""OpenViking-backed memory manager."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import get_logger
from .models import (
    MEMORY_TYPE_TO_DIR,
    MemoryHit,
    MemoryStoreResult,
    normalize_memory_scope,
    normalize_memory_type,
)
from .utils import (
    memory_priority,
    parse_frontmatter,
    render_memory_note,
    serialize_plain_value,
    utc_now_iso,
)

logger = get_logger("memory.manager")

USER_MEMORY_ROOT_URI = "viking://user/default/memories"
DEFAULT_RECALL_LIMIT = 3
OPENVIKING_MEMORY_VERSION = "openviking-v1"


class OpenVikingMemoryManager:
    """Thin wrapper around OpenViking for per-user memory storage."""

    memory_version = OPENVIKING_MEMORY_VERSION

    def __init__(
        self,
        *,
        user_id: str,
        workspace_dir: Path,
        client_cls: Any = None,
        recall_topk: int = DEFAULT_RECALL_LIMIT,
        recall_threshold: Optional[float] = None,
    ) -> None:
        self.user_id = user_id or "anonymous"
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._staging_dir = self.workspace_dir / ".staging"
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._client_cls = client_cls
        self._client = None
        self._recall_topk = max(1, int(recall_topk or DEFAULT_RECALL_LIMIT))
        self._recall_threshold = recall_threshold

    @property
    def client(self) -> Any:
        if self._client is not None:
            return self._client

        client_cls = self._client_cls
        if client_cls is None:
            try:
                from openviking import SyncOpenViking  # type: ignore
            except Exception as exc:  # pragma: no cover - exercised when dependency missing.
                raise RuntimeError(
                    "OpenViking is not installed. Install backend dependencies first."
                ) from exc
            client_cls = SyncOpenViking

        self._client = client_cls(path=str(self.workspace_dir))
        if hasattr(self._client, "initialize"):
            self._client.initialize()
        return self._client

    def ensure_session(
        self,
        *,
        app_session_id: str,
        existing_session_id: Optional[str] = None,
    ) -> str:
        existing = str(existing_session_id or "").strip()
        if existing:
            try:
                if bool(self.client.session_exists(existing)):
                    return existing
            except Exception:
                logger.warning("OpenViking session_exists failed for %s", existing, exc_info=True)

        created = self.client.create_session()
        if isinstance(created, dict):
            created_id = str(created.get("session_id") or "").strip()
            if created_id:
                return created_id

        raise RuntimeError("OpenViking create_session returned no session_id")

    def delete_session(self, session_id: Optional[str]) -> None:
        target = str(session_id or "").strip()
        if not target:
            return
        self.client.delete_session(target)

    def append_session_message(
        self,
        *,
        ov_session_id: str,
        role: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        text = self._serialize_message_content(content, metadata=metadata)
        if not text.strip():
            return
        self.client.add_message(ov_session_id, role, text)

    def append_turn_summary(
        self,
        *,
        ov_session_id: str,
        user_request: str,
        final_output: Any,
        status: str,
    ) -> None:
        payload = {
            "kind": "turn_summary",
            "status": status,
            "user_request": str(user_request or "").strip(),
            "final_output": self._serialize_plain_value(final_output),
        }
        self.append_session_message(
            ov_session_id=ov_session_id,
            role="system",
            content=json.dumps(payload, ensure_ascii=False),
        )

    def commit_session(self, ov_session_id: Optional[str]) -> None:
        target = str(ov_session_id or "").strip()
        if not target:
            return
        self.client.commit_session(target)

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

        target_uri = self._build_target_uri(
            memory_type=normalized_type,
            scope=normalized_scope,
            project_key=project_key,
            app_session_id=app_session_id,
        )
        self._ensure_uri_path(target_uri)

        fingerprint = hashlib.sha1(
            "||".join(
                [
                    normalized_type,
                    normalized_scope,
                    str(project_key or ""),
                    str(app_session_id or ""),
                    normalized_content,
                ]
            ).encode("utf-8")
        ).hexdigest()[:16]
        filename = "{}-{}.md".format(normalized_type, fingerprint)
        staged_path = self._staging_dir / filename
        now = utc_now_iso()
        staged_path.write_text(
            render_memory_note(
                user_id=self.user_id,
                content=normalized_content,
                memory_type=normalized_type,
                scope=normalized_scope,
                project_key=project_key,
                app_session_id=app_session_id,
                source=source,
                created_at=now,
                updated_at=now,
            ),
            encoding="utf-8",
        )

        self.client.add_resource(
            str(staged_path),
            to=target_uri,
            wait=True,
            summarize=True,
            build_index=True,
        )

        return MemoryStoreResult(
            stored=True,
            memory_id=fingerprint,
            memory_type=normalized_type,
            scope=normalized_scope,
            source_uri="{}/{}".format(target_uri, filename),
            project_key=None,
            created_at=now,
            updated_at=now,
        )

    def archive_memory(
        self,
        *,
        source_uri: str,
        reason: Optional[str] = None,
    ) -> bool:
        del reason
        uri = str(source_uri or "").strip()
        if not uri:
            return False
        try:
            raw = self.client.read(uri)
            metadata, body = parse_frontmatter(self._extract_text(raw))
            memory_type = str(metadata.get("memory_type") or self._infer_memory_type_from_uri(uri) or "").strip()
            scope = str(metadata.get("scope") or self._infer_scope_from_uri(uri) or "user").strip() or "user"
            if memory_type not in MEMORY_TYPE_TO_DIR:
                return False
            target_uri, _, filename = uri.rstrip("/").rpartition("/")
            if not target_uri or not filename:
                return False
            now = utc_now_iso()
            staged_path = self._staging_dir / filename
            staged_path.write_text(
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
            self.client.add_resource(
                str(staged_path),
                to=target_uri,
                wait=True,
                summarize=True,
                build_index=True,
            )
            return True
        except Exception:
            logger.warning("OpenViking memory archive failed for %s", uri, exc_info=True)
            return False

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
        normalized_scope = normalize_memory_scope(scope)
        normalized_type = normalize_memory_type(memory_type) if memory_type else None
        limit = max(1, int(k or self._recall_topk))

        target_uri = self._recall_target_uri(
            scope=normalized_scope,
            project_key=project_key,
            app_session_id=app_session_id,
            memory_type=normalized_type,
        )
        payload = self.client.search(
            query=str(query or "").strip(),
            target_uri=target_uri,
            session_id=str(ov_session_id or "").strip() or None,
            limit=limit,
            score_threshold=self._recall_threshold,
        )

        hits: List[MemoryHit] = []
        for item in self._iter_search_items(payload):
            hit = self._build_memory_hit(item)
            if hit.archived and not include_archived:
                continue
            if hit.memory_type not in MEMORY_TYPE_TO_DIR:
                continue
            if hit.scope != "user":
                continue
            if normalized_type and hit.memory_type != normalized_type:
                continue
            if hit.scope != normalized_scope:
                continue
            hits.append(hit)

        hits.sort(key=lambda item: item.score or 0.0, reverse=True)
        return hits[:limit]

    def build_memory_context(
        self,
        *,
        query: str,
        ov_session_id: Optional[str],
        limit: int = DEFAULT_RECALL_LIMIT,
    ) -> str:
        payload = self.client.search(
            query=str(query or "").strip(),
            target_uri=USER_MEMORY_ROOT_URI,
            session_id=str(ov_session_id or "").strip() or None,
            limit=max(limit * 4, limit),
            score_threshold=self._recall_threshold,
        )

        hits: List[MemoryHit] = []
        for item in self._iter_search_items(payload):
            hit = self._build_memory_hit(item)
            if hit.memory_type not in MEMORY_TYPE_TO_DIR:
                continue
            if hit.scope != "user":
                continue
            hits.append(hit)

        ranked = sorted(
            hits,
            key=lambda item: (memory_priority(item), -(item.score or 0.0)),
        )[: max(1, int(limit or DEFAULT_RECALL_LIMIT))]
        if not ranked:
            return ""

        lines = ["## Relevant Long-Term Memory"]
        for hit in ranked:
            label = hit.memory_type
            summary = hit.overview or hit.abstract or hit.content
            lines.append("- [{}] {}".format(label, summary.strip()))
        return "\n".join(lines)

    def _build_target_uri(
        self,
        *,
        memory_type: str,
        scope: str,
        project_key: Optional[str],
        app_session_id: Optional[str],
    ) -> str:
        dir_name = MEMORY_TYPE_TO_DIR[memory_type]
        del scope, project_key, app_session_id
        return "{}/{}".format(USER_MEMORY_ROOT_URI, dir_name)

    def _recall_target_uri(
        self,
        *,
        scope: str,
        project_key: Optional[str],
        app_session_id: Optional[str],
        memory_type: Optional[str] = None,
    ) -> str:
        del scope, project_key, app_session_id, memory_type
        return USER_MEMORY_ROOT_URI

    def _ensure_uri_path(self, target_uri: str) -> None:
        relative = target_uri.replace(USER_MEMORY_ROOT_URI, "").strip("/")
        if not relative:
            return
        current = USER_MEMORY_ROOT_URI
        for segment in [part for part in relative.split("/") if part]:
            current = "{}/{}".format(current, segment)
            try:
                self.client.mkdir(current)
            except Exception:
                continue

    def _build_memory_hit(self, item: Dict[str, Any]) -> MemoryHit:
        uri = str(item.get("uri") or "").strip()
        metadata, body = self._read_note_metadata(uri)
        overview = str(item.get("overview") or "").strip()
        abstract = str(item.get("abstract") or item.get("abstract_content") or "").strip()
        memory_type = str(metadata.get("memory_type") or self._infer_memory_type_from_uri(uri) or "").strip()
        scope = str(metadata.get("scope") or self._infer_scope_from_uri(uri) or "user")
        project_key = str(metadata.get("project_key") or self._infer_project_key(uri) or "").strip() or None
        created_at = str(metadata.get("created_at") or metadata.get("stored_at") or "").strip() or None
        updated_at = str(metadata.get("updated_at") or metadata.get("stored_at") or created_at or "").strip() or None
        if scope == "session":
            project_key = str(metadata.get("session_key") or self._infer_session_key(uri) or "").strip() or None

        return MemoryHit(
            source_uri=uri,
            score=float(item["score"]) if item.get("score") is not None else None,
            content=body or abstract or overview,
            overview=overview,
            abstract=abstract,
            memory_type=memory_type,
            scope=scope,
            project_key=project_key,
            hit_count=int(metadata.get("hit_count") or 0),
            archived=bool(metadata.get("archived") or False),
            created_at=created_at,
            updated_at=updated_at,
        )

    def _read_note_metadata(self, uri: str) -> tuple[Dict[str, Any], str]:
        if not uri:
            return {}, ""
        try:
            raw = self.client.read(uri)
        except Exception:
            return {}, ""
        text = self._extract_text(raw)
        return parse_frontmatter(text)

    @staticmethod
    def _extract_text(raw: Any) -> str:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for key in ("content", "text", "data"):
                value = raw.get(key)
                if isinstance(value, str):
                    return value
        if hasattr(raw, "to_dict"):
            return OpenVikingMemoryManager._extract_text(raw.to_dict())
        return str(raw or "")

    @staticmethod
    def _iter_search_items(payload: Any) -> List[Dict[str, Any]]:
        if hasattr(payload, "to_dict"):
            payload = payload.to_dict()
        if not isinstance(payload, dict):
            return []

        items: List[Dict[str, Any]] = []
        for key in ("memories", "resources", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                items.extend([item for item in value if isinstance(item, dict)])
        return items

    @staticmethod
    def _infer_memory_type_from_uri(uri: str) -> str:
        normalized = str(uri or "").lower()
        if "/preferences/" in normalized:
            return "preference"
        if "/profile/" in normalized:
            return "profile"
        return ""

    @staticmethod
    def _infer_scope_from_uri(uri: str) -> str:
        normalized = str(uri or "").lower()
        if "/sessions/" in normalized:
            return "session"
        if "/projects/" in normalized:
            return "project"
        return "user"

    @staticmethod
    def _infer_project_key(uri: str) -> Optional[str]:
        match = re.search(r"/projects/([^/]+)", str(uri or ""))
        return match.group(1) if match else None

    @staticmethod
    def _infer_session_key(uri: str) -> Optional[str]:
        match = re.search(r"/sessions/([^/]+)", str(uri or ""))
        return match.group(1) if match else None

    @staticmethod
    def _serialize_plain_value(value: Any) -> str:
        return serialize_plain_value(value)

    def _serialize_message_content(
        self,
        content: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        base = self._serialize_plain_value(content).strip()
        if not metadata:
            return base
        return "{}\n\n{}".format(
            base,
            json.dumps({"metadata": metadata}, ensure_ascii=False),
        ).strip()
