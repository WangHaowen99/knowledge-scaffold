"""Typed models and helpers for memory storage (filesystem & OpenViking)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

MEMORY_TYPE_TO_DIR = {
    "preference": "preferences",
    "profile": "profile",
}

MEMORY_SCOPE_VALUES = {"user"}

@dataclass
class MemoryStoreResult:
    stored: bool
    memory_type: str
    scope: str
    source_uri: str
    project_key: Optional[str] = None
    memory_id: Optional[str] = None
    hit_count: int = 0
    merged: bool = False
    error: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "stored": self.stored,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "source_uri": self.source_uri,
            "hit_count": self.hit_count,
            "merged": self.merged,
        }
        if self.project_key:
            payload["project_key"] = self.project_key
        if self.memory_id:
            payload["memory_id"] = self.memory_id
        if self.created_at:
            payload["created_at"] = self.created_at
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class MemoryHit:
    source_uri: str
    score: Optional[float]
    content: str
    overview: str = ""
    abstract: str = ""
    memory_type: str = "preference"
    scope: str = "user"
    project_key: Optional[str] = None
    hit_count: int = 0
    archived: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source_uri": self.source_uri,
            "score": self.score,
            "content": self.content,
            "overview": self.overview,
            "abstract": self.abstract,
            "memory_type": self.memory_type,
            "scope": self.scope,
            "hit_count": self.hit_count,
        }
        if self.project_key:
            payload["project_key"] = self.project_key
        if self.created_at:
            payload["created_at"] = self.created_at
        if self.updated_at:
            payload["updated_at"] = self.updated_at
        if self.archived:
            payload["archived"] = True
        return payload


def normalize_memory_type(value: Optional[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in MEMORY_TYPE_TO_DIR:
        raise ValueError(
            "Unsupported memory_type '{}'. Expected one of: {}.".format(
                value,
                ", ".join(sorted(MEMORY_TYPE_TO_DIR)),
            )
        )
    return normalized


def normalize_memory_scope(value: Optional[str]) -> str:
    normalized = str(value or "user").strip().lower()
    if normalized not in MEMORY_SCOPE_VALUES:
        raise ValueError(
            "Unsupported scope '{}'. Expected one of: user.".format(value)
        )
    return normalized


def normalize_for_dedupe(text: str) -> str:
    """Return a lowercased, whitespace/punctuation-stripped form suitable for
    dedupe fingerprinting.

    Aggressive on purpose: the goal is that small punctuation/spacing changes
    produce identical fingerprints. Semantic dedupe (paraphrase) is out of
    scope.
    """
    import re as _re

    normalized = str(text or "").lower()
    # Drop punctuation and whitespace entirely.  CJK characters and alnum
    # survive; the remaining byte sequence is the dedupe key.
    return _re.sub(r"[\s\W_]+", "", normalized, flags=_re.UNICODE)


def normalize_confidence(value: Any, default: float = 1.0) -> float:
    try:
        confidence = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if confidence < 0.0:
        return 0.0
    if confidence > 1.0:
        return 1.0
    return confidence
