"""Semantic write path for operation-evolution proposals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from llm import LLMPurpose, get_llm_router
from utils.json_utils import _loads_json_object_loose
from utils.logging import get_logger

logger = get_logger("evolution.proposal_writer")


_OPERATION_RELATION_SYSTEM_PROMPT = """\
Compare one new operation-evolution memory item with existing memories in the same scope.

Return a single JSON object only.

Schema:
{
  "decisions": [
    {
      "fingerprint": "string",
      "relation": "duplicate" | "supersedes" | "refines" | "conflicts" | "unrelated",
      "reason": "string"
    }
  ]
}

Relation rules:
- duplicate: the new item says the same durable instruction or schema fact as the old item.
- supersedes: the new item replaces or corrects the old item.
- refines: the new item is a more precise version that should replace the old item in recall.
- conflicts: both items cannot safely be true at the same time.
- unrelated: both can coexist.

Return one decision for every provided candidate fingerprint.
"""


@dataclass
class ProposalWriteResult:
    aggregate: Any
    relation: Optional[str] = None
    matched_fingerprint: Optional[str] = None
    replaced_fingerprints: List[str] = field(default_factory=list)
    relations: Dict[str, str] = field(default_factory=dict)


def append_operation_proposal_with_relations(
    *,
    buffer: Any,
    workspace_id: Optional[str],
    ontology_id: str,
    operation_key: Optional[str],
    kind: str,
    content: str,
    source: str,
    entity_key: Optional[str] = None,
    rationale: Optional[str] = None,
    run_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> ProposalWriteResult:
    """Append an operation proposal after semantic relation judgment.

    Duplicate writes reuse the existing canonical content so they merge into the
    existing fingerprint. Superseding/refining/conflicting candidates are marked
    rejected before the new proposal is appended.
    """
    kind = _safe_str(kind).lower()
    ontology_id = _safe_str(ontology_id)
    operation_key = _safe_str(operation_key) or None
    entity_key = _safe_str(entity_key) or None
    content = _safe_str(content)

    candidates = operation_relation_candidates(
        buffer=buffer,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        operation_key=operation_key,
        kind=kind,
        entity_key=entity_key,
    )
    relation_decisions = judge_operation_relations(
        new_item={
            "content": content,
            "kind": kind,
            "ontology_id": ontology_id,
            "operation_key": operation_key,
            "entity_key": entity_key,
            "source": source,
        },
        candidates=candidates,
    )

    duplicate_candidate = next(
        (
            candidate
            for candidate in candidates
            if relation_decisions.get(candidate.fingerprint, {}).get("relation") == "duplicate"
        ),
        None,
    )
    replaced_fingerprints: List[str] = []
    relation_summary: Dict[str, str] = {}
    for candidate in candidates:
        decision = relation_decisions.get(candidate.fingerprint) or {}
        relation = decision.get("relation")
        if relation in {"supersedes", "refines", "conflicts"}:
            buffer.reject(
                ontology_id,
                candidate.operation_key,
                candidate.fingerprint,
                workspace_id=workspace_id,
            )
            replaced_fingerprints.append(candidate.fingerprint)
            relation_summary[candidate.fingerprint] = relation

    append_content = (
        duplicate_candidate.canonical_content
        if duplicate_candidate is not None
        else content
    )
    aggregate = buffer.append(
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        operation_key=operation_key,
        kind=kind,
        content=append_content,
        source=source,
        entity_key=entity_key,
        rationale=rationale,
        run_id=run_id,
        user_id=user_id,
    )
    return ProposalWriteResult(
        aggregate=aggregate,
        relation="duplicate" if duplicate_candidate is not None else None,
        matched_fingerprint=(
            duplicate_candidate.fingerprint if duplicate_candidate is not None else None
        ),
        replaced_fingerprints=replaced_fingerprints,
        relations=relation_summary,
    )


def operation_relation_candidates(
    *,
    buffer: Any,
    workspace_id: Optional[str],
    ontology_id: str,
    operation_key: Optional[str],
    kind: str,
    entity_key: Optional[str],
) -> List[Any]:
    statuses = ("pending", "promoted", "applied")
    if kind == "schema":
        if not entity_key:
            return []
        return buffer.recall(
            ontology_id,
            workspace_id=workspace_id,
            operation_key=operation_key,
            kind="schema",
            entity_key=entity_key,
            k=20,
            statuses=statuses,
        )
    if not operation_key:
        return []
    return buffer.recall(
        ontology_id,
        workspace_id=workspace_id,
        operation_key=operation_key,
        kind=kind,
        k=20,
        statuses=statuses,
    )


def judge_operation_relations(
    *,
    new_item: Dict[str, Any],
    candidates: List[Any],
) -> Dict[str, Dict[str, str]]:
    if not candidates:
        return {}
    payload = {
        "new_item": {
            "content": _safe_str(new_item.get("content")),
            "kind": _safe_str(new_item.get("kind")).lower(),
            "ontology_id": _safe_str(new_item.get("ontology_id")),
            "operation_key": _safe_str(new_item.get("operation_key")) or None,
            "entity_key": _safe_str(new_item.get("entity_key")) or None,
            "source": _safe_str(new_item.get("source")) or None,
        },
        "candidates": [
            {
                "fingerprint": candidate.fingerprint,
                "content": candidate.canonical_content,
                "kind": candidate.kind,
                "ontology_id": candidate.ontology_id,
                "operation_key": candidate.operation_key,
                "entity_key": candidate.entity_key,
                "status": candidate.status,
                "sources": dict(candidate.sources or {}),
            }
            for candidate in candidates
        ],
    }
    for item, candidate in zip(payload["candidates"], candidates):
        candidate_payload = candidate.to_dict()
        item["created_at"] = candidate_payload.get("created_at")
        item["updated_at"] = candidate_payload.get("updated_at")
    try:
        decision_payload = _call_memory_llm(
            system_prompt=_OPERATION_RELATION_SYSTEM_PROMPT,
            payload=payload,
            max_tokens=900,
            request_label="memory_relation_operation",
        )
    except Exception:
        logger.warning("operation memory relation judgment failed; appending as new", exc_info=True)
        return {}

    decisions: Dict[str, Dict[str, str]] = {}
    raw_decisions = decision_payload.get("decisions")
    if not isinstance(raw_decisions, list):
        return decisions
    valid = {"duplicate", "supersedes", "refines", "conflicts", "unrelated"}
    for item in raw_decisions:
        if not isinstance(item, dict):
            continue
        fingerprint = _safe_str(item.get("fingerprint"))
        relation = _safe_str(item.get("relation")).lower()
        if not fingerprint or relation not in valid:
            continue
        decisions[fingerprint] = {
            "relation": relation,
            "reason": _safe_str(item.get("reason")),
        }
    return decisions


def _call_memory_llm(
    *,
    system_prompt: str,
    payload: Any,
    max_tokens: int,
    request_label: str,
) -> Dict[str, Any]:
    user_content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    response = get_llm_router().call(
        purpose=LLMPurpose.MEMORY,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        tools=[],
        stream=False,
        temperature=0,
        max_tokens=max_tokens,
        enable_reasoning=False,
        run_id=None,
        request_label=request_label,
    )
    return _parse_memory_payload(response.content)


def _parse_memory_payload(raw: str) -> Dict[str, Any]:
    text = _strip_json_fence(raw)
    parsed = _loads_json_object_loose(text)
    if parsed is not None:
        return parsed
    return json.loads(text)


def _strip_json_fence(text: str) -> str:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    return candidate.strip()


def _safe_str(value: Any) -> str:
    return str(value or "").strip()
