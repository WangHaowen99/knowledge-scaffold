"""Typed models for operation-evolution proposals and aggregates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

ProposalKind = Literal["procedure", "pitfall", "schema"]
ProposalSource = Literal["active_chat", "passive"]
AggregateStatus = Literal["pending", "promoted", "rejected", "applied"]

PROPOSAL_KINDS: set = {"procedure", "pitfall", "schema"}
PROPOSAL_SOURCES: set = {"active_chat", "passive"}
AGGREGATE_STATUSES: set = {"pending", "promoted", "rejected", "applied"}


def _epoch_to_iso(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_for_fingerprint(text: str) -> str:
    """Lowercase + strip all whitespace/punctuation. Mirrors
    `agentic.memory.models.normalize_for_dedupe` so identical content
    fingerprints to the same key in both subsystems."""
    normalized = str(text or "").lower()
    return re.sub(r"[\s\W_]+", "", normalized, flags=re.UNICODE)


def compute_fingerprint(
    *,
    kind: str,
    ontology_id: str,
    operation_key: Optional[str],
    entity_key: Optional[str],
    content: str,
    workspace_id: Optional[str] = None,
) -> str:
    """SHA-256 of (kind + ontology + op/entity + normalized content).

    Same content under different operations should NOT collide.
    """
    parts = [
        str(kind or "").strip().lower(),
        str(workspace_id or "").strip(),
        str(ontology_id or "").strip(),
        str(operation_key or "").strip(),
        str(entity_key or "").strip(),
        _normalize_for_fingerprint(content),
    ]
    raw = "\u0001".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Proposal:
    id: str
    fingerprint: str
    workspace_id: Optional[str]
    ontology_id: str
    operation_key: Optional[str]
    kind: str
    content: str
    source: str
    ts: float
    entity_key: Optional[str] = None
    rationale: Optional[str] = None
    run_id: Optional[str] = None
    user_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = _epoch_to_iso(self.ts)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Proposal":
        return cls(
            id=str(data["id"]),
            fingerprint=str(data["fingerprint"]),
            workspace_id=data.get("workspace_id"),
            ontology_id=str(data["ontology_id"]),
            operation_key=str(data.get("operation_key") or "").strip() or None,
            kind=str(data["kind"]),
            content=str(data["content"]),
            source=str(data["source"]),
            ts=float(data["ts"]),
            entity_key=data.get("entity_key"),
            rationale=data.get("rationale"),
            run_id=data.get("run_id"),
            user_id=data.get("user_id"),
        )


@dataclass
class Aggregate:
    fingerprint: str
    workspace_id: Optional[str]
    ontology_id: str
    operation_key: Optional[str]
    kind: str
    canonical_content: str
    occurrences: int
    active_endorsements: int
    applied_count: int
    rejection_count: int
    first_seen: float
    last_seen: float
    status: str = "pending"
    entity_key: Optional[str] = None
    sources: Dict[str, int] = field(default_factory=dict)
    proposal_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["created_at"] = _epoch_to_iso(self.first_seen)
        payload["updated_at"] = _epoch_to_iso(self.last_seen)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Aggregate":
        return cls(
            fingerprint=str(data["fingerprint"]),
            workspace_id=data.get("workspace_id"),
            ontology_id=str(data["ontology_id"]),
            operation_key=str(data.get("operation_key") or "").strip() or None,
            kind=str(data["kind"]),
            canonical_content=str(data["canonical_content"]),
            occurrences=int(data["occurrences"]),
            active_endorsements=int(data.get("active_endorsements", 0)),
            applied_count=int(data.get("applied_count", 0)),
            rejection_count=int(data.get("rejection_count", 0)),
            first_seen=float(data["first_seen"]),
            last_seen=float(data["last_seen"]),
            status=str(data.get("status", "pending")),
            entity_key=data.get("entity_key"),
            sources=dict(data.get("sources") or {}),
            proposal_ids=list(data.get("proposal_ids") or []),
        )

    @classmethod
    def seed_from(cls, p: Proposal) -> "Aggregate":
        return cls(
            fingerprint=p.fingerprint,
            workspace_id=p.workspace_id,
            ontology_id=p.ontology_id,
            operation_key=p.operation_key,
            kind=p.kind,
            canonical_content=p.content,
            occurrences=1,
            active_endorsements=1 if p.source == "active_chat" else 0,
            applied_count=0,
            rejection_count=0,
            first_seen=p.ts,
            last_seen=p.ts,
            status="pending",
            entity_key=p.entity_key,
            sources={p.source: 1},
            proposal_ids=[p.id],
        )

    def merge(self, p: Proposal) -> None:
        self.occurrences += 1
        if p.source == "active_chat":
            self.active_endorsements += 1
            # active_chat outranks passive — keep its phrasing as canonical
            self.canonical_content = p.content
        elif not self.canonical_content:
            self.canonical_content = p.content
        self.last_seen = max(self.last_seen, p.ts)
        self.sources[p.source] = self.sources.get(p.source, 0) + 1
        if p.id not in self.proposal_ids:
            self.proposal_ids.append(p.id)
