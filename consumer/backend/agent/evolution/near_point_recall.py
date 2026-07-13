"""Near-point recall attached to ontology-context reads.

Source of truth: the operation-evolution proposal buffer. Procedures, pitfalls
and schema corrections all live there now.
Passive reflection writes pass through the buffer and only land in the
operation `.md` once a patch is approved.

Design invariant: this module ONLY formats/appends extra text. It never raises
into the caller; tool output must remain usable even when memory is missing or
recall fails.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import List, Optional, Tuple

from utils.logging import get_logger
from agent.evolution.models import Aggregate
from agent.evolution.registry import get_proposal_buffer

logger = get_logger("evolution.near_point_recall")


_ONTOLOGY_PATH_RE = re.compile(
    r"(?:^|/)ontologies/(?P<ontology_id>[^/]+)/(?P<kind>operations|objects)/(?P<name>[^/]+)\.md$",
    re.IGNORECASE,
)


def parse_ontology_doc_path(file_path: str) -> Optional[Tuple[str, str, str]]:
    """Return (ontology_id, kind, key) if ``file_path`` is an ontology op/object doc.

    ``kind`` is ``"operations"`` or ``"objects"``.  ``key`` is the filename stem.
    Returns None for list pages like ``operations-list.md``.
    """
    if not file_path:
        return None
    normalized = str(file_path).replace("\\", "/")
    match = _ONTOLOGY_PATH_RE.search(normalized)
    if not match:
        return None
    ontology_id = match.group("ontology_id").strip()
    kind = match.group("kind").lower()
    raw_name = PurePosixPath(normalized).stem
    if not ontology_id or not raw_name:
        return None
    if raw_name.endswith("-list"):
        return None
    return ontology_id, kind, raw_name


def _format_aggregate(idx: int, agg: Aggregate) -> str:
    content = (agg.canonical_content or "").strip()
    if len(content) > 240:
        content = content[:240] + "…"
    meta_parts: List[str] = []
    if agg.applied_count:
        meta_parts.append("applied={}".format(agg.applied_count))
    if agg.active_endorsements:
        meta_parts.append("👍={}".format(agg.active_endorsements))
    if agg.status and agg.status != "pending":
        meta_parts.append(agg.status)
    meta = " ".join("[{}]".format(p) for p in meta_parts)
    return "{idx}. {meta} {content}".format(idx=idx, meta=meta, content=content).strip()


def _safe_recall(
    *,
    workspace_id: Optional[str] = None,
    ontology_id: str,
    operation_key: Optional[str],
    kind: str,
    entity_key: Optional[str] = None,
    k: int = 3,
) -> List[Aggregate]:
    if not ontology_id:
        return []
    if not operation_key and kind == "schema" and not entity_key:
        return []
    try:
        return list(
            get_proposal_buffer().recall(
                ontology_id=ontology_id,
                workspace_id=workspace_id,
                operation_key=operation_key,
                kind=kind,
                entity_key=entity_key,
                k=k,
            )
        )
    except Exception:
        logger.warning(
            "near-point recall failed kind=%s ont=%s op=%s entity=%s",
            kind, ontology_id, operation_key, entity_key,
            exc_info=True,
        )
        return []


def _render_section(title: str, aggs: List[Aggregate]) -> str:
    if not aggs:
        return ""
    lines = ["### {}".format(title)]
    for idx, agg in enumerate(aggs, 1):
        lines.append(_format_aggregate(idx, agg))
    return "\n".join(lines)


def build_near_point_block(
    *,
    workspace_id: Optional[str] = None,
    ontology_id: str,
    operation_key: Optional[str] = None,
    entity_key: Optional[str] = None,
    query: str = "",  # unused — buffer ranks by indicator counters, not query similarity
    k: int = 3,
) -> Tuple[str, List[str]]:
    """Recall buffered proposals and render a markdown block.

    Returns ``(text, fingerprints)``.  ``text`` is ``""`` when nothing useful
    is available — callers should only append the block when non-empty.
    ``fingerprints`` lists the aggregate fingerprints that were injected,
    which orchestrator uses at turn-end to call
    `buffer.record_outcome(used_fps, success=...)`.

    """
    del query  # buffer ranks by indicator counters, not query similarity

    if not ontology_id:
        return "", []

    sections: List[str] = []
    used: List[str] = []

    if operation_key:
        procedures = _safe_recall(
            workspace_id=workspace_id,
            ontology_id=ontology_id, operation_key=operation_key, kind="procedure", k=k,
        )
        pitfalls = _safe_recall(
            workspace_id=workspace_id,
            ontology_id=ontology_id, operation_key=operation_key, kind="pitfall", k=k,
        )
        proc_section = _render_section("成功模式", procedures)
        pit_section = _render_section("历史坑点", pitfalls)
        if proc_section:
            sections.append(proc_section)
            used.extend(a.fingerprint for a in procedures)
        if pit_section:
            sections.append(pit_section)
            used.extend(a.fingerprint for a in pitfalls)

    if entity_key:
        schemas = _safe_recall(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            operation_key=None,
            kind="schema",
            entity_key=entity_key,
            k=k,
        )
        if operation_key:
            schemas.extend(
                _safe_recall(
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    operation_key=operation_key,
                    kind="schema",
                    entity_key=entity_key,
                    k=k,
                )
            )
        schema_section = _render_section("实体字段修正", schemas)
        if schema_section:
            sections.append(schema_section)
            used.extend(a.fingerprint for a in schemas)

    if not sections:
        return "", []

    header = "## 历史经验"
    return "\n\n".join([header] + sections), used
