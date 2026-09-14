# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Natural-language ontology evidence retrieval and response contracts."""

from __future__ import annotations

import json
from typing import Any

from ontology.evidence.sqlite_index import SQLiteEvidenceIndex


MAX_QUERY_CHARS = 512
MAX_LIMIT = 10
DEFAULT_LIMIT = 5


class EvidenceSearchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class EvidenceSearchService:
    def __init__(
        self,
        index: SQLiteEvidenceIndex,
        *,
        workspace_id: str,
        graph_enabled: bool = False,
    ) -> None:
        self.index = index
        self.workspace_id = str(workspace_id or "").strip()
        if not self.workspace_id:
            raise EvidenceSearchError("INVALID_ARGUMENTS", "workspace_id is required")
        self.graph_enabled = bool(graph_enabled)

    def search(self, *, ontology_id: str, query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        if not normalized_query or len(normalized_query) > MAX_QUERY_CHARS:
            raise EvidenceSearchError("INVALID_ARGUMENTS", "query length must be between 1 and 512")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise EvidenceSearchError("INVALID_ARGUMENTS", "limit must be between 1 and 10")
        metadata = self.index.metadata()
        if str(metadata.get("workspace_id") or "") != self.workspace_id:
            raise EvidenceSearchError("ONTOLOGY_NOT_FOUND", "active index does not belong to workspace")
        if str(metadata["ontology_id"]) != str(ontology_id):
            raise EvidenceSearchError("ONTOLOGY_NOT_FOUND", "active index does not belong to ontology")

        raw_candidates = self.index.search(normalized_query, limit=max(limit * 4, 20))
        if self.graph_enabled and raw_candidates:
            raw_candidates = _with_graph_neighbors(
                self.index,
                raw_candidates,
                limit=max(limit * 4, 20),
            )
        for candidate in raw_candidates:
            candidate["ontology_id"] = metadata["ontology_id"]
        ranked = _rank_candidates(raw_candidates, normalized_query)
        selected = _select_diverse_hits(ranked, limit=limit)
        hits = [_public_hit(item) for item in selected]
        status, result_code = _result_status(ranked)
        response = {
            "contract": "OntologyEvidenceSearch/v1",
            "success": True,
            "status": status,
            "result_code": result_code,
            "ontology_id": metadata["ontology_id"],
            "ontology_version": metadata["ontology_version"],
            "ontology_epoch": metadata["ontology_epoch"],
            "index_generation": metadata["index_generation"],
            "query": normalized_query,
            "retrieval": {
                "mode": "lexical_graph" if self.graph_enabled else "lexical",
                "degraded": False,
                "degraded_channels": [],
                "ranker_version": "evidence-ranker-v1",
            },
            "hits": hits,
            "guidance": _guidance(status),
        }
        _enforce_response_budget(response)
        return response


def _rank_candidates(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    exact_query = query.strip()
    for item in items:
        exact_rank = item.get("exact_rank")
        lexical_rank = item.get("lexical_position")
        score = 0.0
        reasons: list[str] = []
        if exact_rank:
            score += 1.0 / (60 + int(exact_rank))
            if exact_query in {str(item.get("resource_key") or ""), str(item.get("canonical_key") or "")}:
                reasons.append("EXACT_KEY")
            if exact_query == str(item.get("title") or ""):
                reasons.append("EXACT_TITLE")
            if exact_query in {str(value) for value in decode_json_array(item.get("aliases_json"))}:
                reasons.append("EXACT_ALIAS")
        if lexical_rank:
            score += 1.0 / (60 + int(lexical_rank))
            reasons.append("LEXICAL_MATCH")
        graph_rank = item.get("graph_position")
        if graph_rank:
            score += 1.0 / (100 + int(graph_rank))
            reasons.append("GRAPH_NEIGHBOR")
        next_item = dict(item)
        next_item["relevance_score"] = round(min(1.0, score * 31), 6)
        next_item["reason_codes"] = reasons
        ranked.append(next_item)
    ranked.sort(
        key=lambda item: (
            -float(item["relevance_score"]),
            str(item["resource"]),
            str(item["resource_key"]),
            str(item["section_anchor"]),
            str(item["evidence_id"]),
        )
    )
    return ranked


def _select_diverse_hits(items: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    """Keep at most two sections for one semantic resource."""
    selected: list[dict[str, Any]] = []
    subject_counts: dict[tuple[str, str], int] = {}
    for item in items:
        subject = (str(item["resource"]), str(item["resource_key"]))
        if subject_counts.get(subject, 0) >= 2:
            continue
        selected.append(item)
        subject_counts[subject] = subject_counts.get(subject, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _result_status(items: list[dict[str, Any]]) -> tuple[str, str]:
    if not items:
        return "no_match", "NO_EVIDENCE_FOUND"
    exact_subjects = {
        (str(item["resource"]), str(item["resource_key"]))
        for item in items
        if item.get("exact_rank")
    }
    if len(exact_subjects) > 1:
        return "ambiguous", "AMBIGUOUS_EVIDENCE"
    if len(items) > 1 and float(items[0]["relevance_score"]) < float(items[1]["relevance_score"]) * 1.05:
        first = (items[0]["resource"], items[0]["resource_key"])
        second = (items[1]["resource"], items[1]["resource_key"])
        if first != second:
            return "ambiguous", "AMBIGUOUS_EVIDENCE"
    return "resolved", "EVIDENCE_FOUND"


def _public_hit(item: dict[str, Any]) -> dict[str, Any]:
    score = float(item["relevance_score"])
    next_arguments = {
        "ontology_id": item.get("ontology_id") or "",
        "resource": item["resource"],
        "key": item["resource_key"],
        "max_chars": 8000,
        "response_format": "structured",
    }
    if item["section_anchor"] != "_metadata":
        next_arguments["section"] = item["section_anchor"]
    return {
        "evidence_id": item["evidence_id"],
        "resource": item["resource"],
        "key": item["resource_key"],
        "title": item["title"],
        "section": item["section"],
        "section_anchor": item["section_anchor"],
        "chunk_type": item["chunk_type"],
        "snippet": str(item["snippet"])[:420],
        "source_path": item["source_path"],
        "content_hash": "sha256:" + item["content_hash"],
        "relevance_score": score,
        "confidence_band": "high" if score >= 0.8 else "medium" if score >= 0.45 else "low",
        "reason_codes": item["reason_codes"],
        "loaded_before": False,
        "next_action": {
            "tool": "ontology_context",
            "arguments": next_arguments,
        },
    }


def _with_graph_neighbors(
    index: SQLiteEvidenceIndex,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    result = list(candidates)
    seen = {int(item["chunk_rowid"]) for item in candidates}
    by_rowid = {int(item["chunk_rowid"]): item for item in result}
    position = 0
    subjects: set[tuple[str, str]] = set()
    for candidate in candidates[:5]:
        subject = (str(candidate["resource"]), str(candidate["resource_key"]))
        if subject in subjects:
            continue
        subjects.add(subject)
        for neighbor in index.linked_candidates(*subject, limit=10):
            rowid = int(neighbor["chunk_rowid"])
            if rowid in seen:
                position += 1
                by_rowid[rowid].setdefault("graph_position", position)
                continue
            position += 1
            item = dict(neighbor)
            item["graph_position"] = position
            result.append(item)
            seen.add(rowid)
            if len(result) >= limit:
                return result
    return result


def _guidance(status: str) -> dict[str, Any] | None:
    if status == "no_match":
        return {
            "action": "refine_query_or_load_root",
            "message": "未找到可靠本体证据；请收窄术语或重新加载当前知识包根上下文。",
        }
    if status == "ambiguous":
        return {
            "action": "select_candidate",
            "message": "存在多个接近候选；请结合 resource、key 和 section 选择后加载详情。",
        }
    return None


def _enforce_response_budget(response: dict[str, Any], *, max_chars: int = 6000) -> None:
    def size() -> int:
        return len(json.dumps(response, ensure_ascii=False, separators=(",", ":")))

    if size() <= max_chars:
        return
    for hit in response.get("hits") or []:
        hit["snippet"] = str(hit.get("snippet") or "")[:240]
    while size() > max_chars and len(response.get("hits") or []) > 1:
        response["hits"].pop()
    if size() > max_chars and response.get("hits"):
        response["hits"][0]["snippet"] = str(response["hits"][0].get("snippet") or "")[:120]


def decode_json_array(value: Any) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []
