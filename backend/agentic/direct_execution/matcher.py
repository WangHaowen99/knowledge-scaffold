"""Deterministic operation matching for the direct execution branch.

Phase 1 uses only deterministic rules — no LLM preflight — to decide whether
the user request clearly matches a script-type operation in the selected
ontology.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..utils.logging import get_logger
from ..utils.ontology_catalog import (
    get_ontology_manifest,
    load_script_catalog,
)

logger = get_logger("direct_execution.matcher")

_KEYWORD_SPLIT_PATTERN = re.compile(r"[\s,，、/／:：;；\-—]+")
# CJK Unified Ideographs range
_CJK = r"[一-鿿]"
# Compiled pattern to detect strings consisting entirely of CJK characters
_CJK_ONLY_RE = re.compile(r"^[一-鿿]+$")


def _tokenize(text: str) -> set[str]:
    """Split text into normalized keywords.

    Inserts separators at CJK ↔ non-CJK boundaries so that Chinese text
    without spaces is split into words properly.
    """
    # Insert space between CJK and non-CJK characters
    text = re.sub(r"({})([a-zA-Z0-9])".format(_CJK), r"\1 \2", text)
    text = re.sub(r"([a-zA-Z0-9])({})".format(_CJK), r"\1 \2", text)
    raw = _KEYWORD_SPLIT_PATTERN.split(text.strip())
    return {t.lower() for t in raw if len(t) >= 2}


def _get_script_catalog(
    ontology_id: str,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Load and parse the script catalog for an ontology."""
    ontology, error = get_ontology_manifest(ontology_id)
    if error or ontology is None:
        return None, error or "Ontology not found"

    ontology_dir = Path(ontology.ontology_dir).resolve()
    catalog = [
        entry
        for entry in load_script_catalog(ontology_dir)
        if entry.get("_is_bound_operation")
        and str(entry.get("operation_key") or entry.get("script_key") or "").strip()
    ]
    if not catalog:
        return None, "No script catalog found for ontology '{}'".format(ontology_id)
    return catalog, None


def _is_pure_cjk(text: str) -> bool:
    """Return True when the string consists entirely of CJK characters."""
    return bool(_CJK_ONLY_RE.match(text))


def _calc_confidence(
    user_tokens: set[str],
    op_name: str,
    op_tokens: set[str],
    user_request: str = "",
) -> float:
    """Calculate match confidence between user query and operation name.

    Applies two strategies based on token character type:

    * **Non-CJK or mixed tokens** (e.g. "target_item", "供应商编号123"): substring
      matching — checks whether the op token appears within any user token.
      This handles cases like "的供应商编号" containing "供应商编号".

    * **Pure CJK tokens** (e.g. "指定车系车主人群特征分析"): strict exact
      match — the op token must equal a user token exactly. No substring
      matching to avoid false prefix matches.

    Hard requirement: the full operation name must appear verbatim in the
    user request. This ensures only literal/exact ("绝对匹配") triggers
    direct script execution.
    """
    if not user_tokens or not op_tokens:
        return 0.0

    matched_credit = 0.0
    for op_token in op_tokens:
        if _is_pure_cjk(op_token):
            # Strict exact match for pure CJK tokens (no substring)
            for user_token in user_tokens:
                if op_token == user_token:
                    matched_credit += 1.0
                    break
        else:
            # Substring matching for non-CJK / mixed tokens
            for user_token in user_tokens:
                if op_token in user_token:
                    matched_credit += 1.0
                    break

    if matched_credit == 0.0:
        return 0.0

    coverage = matched_credit / len(op_tokens)

    # Hard requirement: operation name must appear verbatim in the user request.
    # This ensures only literal/exact ("绝对匹配") triggers direct script execution.
    if op_name.lower() not in user_request.lower():
        return 0.0

    return min(1.0, coverage)


def _normalized_phrase(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-鿿]+", "", str(text or "").lower())


def _title_core(text: str) -> str:
    normalized = _normalized_phrase(text)
    for suffix in ("脚本", "查询", "分析", "计算", "流程"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
    return normalized


def _calc_entry_confidence(
    *,
    entry: Dict[str, Any],
    user_tokens: set[str],
    user_request: str,
) -> float:
    operation_key = str(entry.get("operation_key") or "").strip()
    best = _calc_confidence(user_tokens, operation_key, _tokenize(operation_key), user_request=user_request)

    normalized_request = _normalized_phrase(user_request)
    candidates = [
        str(entry.get("title") or "").strip(),
        str(entry.get("script_key") or "").strip(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        core = _title_core(candidate)
        if core and len(core) >= 6 and core in normalized_request:
            best = max(best, 1.0)
            continue
        candidate_tokens = _tokenize(candidate)
        if candidate_tokens:
            best = max(
                best,
                _calc_confidence(
                    user_tokens,
                    candidate,
                    candidate_tokens,
                    user_request=user_request,
                ),
            )
    return best


def find_best_script_operation(
    user_request: str,
    ontology_id: str,
    *,
    confidence_threshold: float = 0.85,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
    """Find the best matching script-based operation for a user request.

    Phase 1 uses deterministic keyword matching only. Returns the script entry
    (from the catalog), the operation_key, and a confidence score. When no
    candidate meets the threshold, returns (None, None, 0.0).
    """
    catalog, error = _get_script_catalog(ontology_id)
    if error or catalog is None:
        logger.debug("No script catalog for ontology %s: %s", ontology_id, error)
        return None, None, 0.0

    if not catalog:
        return None, None, 0.0

    user_tokens = _tokenize(user_request)
    if not user_tokens:
        return None, None, 0.0

    best_entry: Optional[Dict[str, Any]] = None
    best_key: Optional[str] = None
    best_score = 0.0

    for entry in catalog:
        op_key = str(entry.get("operation_key") or "").strip()
        if not op_key:
            continue

        score = _calc_entry_confidence(
            entry=entry,
            user_tokens=user_tokens,
            user_request=user_request,
        )

        if best_key is None or score > best_score or (score == best_score and len(op_key) > len(best_key)):
            best_score = score
            best_entry = entry
            best_key = op_key

    if best_score < confidence_threshold:
        logger.debug(
            "Best candidate '%s' score=%.3f below threshold=%.3f",
            best_key,
            best_score,
            confidence_threshold,
        )
        return None, None, 0.0

    logger.info(
        "Matched operation '%s' confidence=%.3f for ontology=%s",
        best_key,
        best_score,
        ontology_id,
    )
    return best_entry, best_key, best_score


def llm_match_operation(
    user_request: str,
    ontology_id: str,
    task_llm,
    *,
    confidence_threshold: float = 0.85,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], float]:
    """Phase 2: LLM-based operation matching.

    Called when deterministic matching (find_best_script_operation) fails.
    Uses the lightweight task_llm to semantically match the user request
    against available script operations.
    """
    catalog, error = _get_script_catalog(ontology_id)
    if error or catalog is None:
        logger.debug("LLM preflight: no catalog for ontology %s", ontology_id)
        return None, None, 0.0
    if not catalog:
        return None, None, 0.0

    # Build operation list for the prompt
    op_lines = []
    for entry in catalog:
        op_key = str(entry.get("operation_key") or "").strip()
        args = entry.get("arguments") or []
        # Extract business parameter descriptions
        param_desc = []
        for a in args:
            a_str = str(a)
            if a_str.startswith("<") and a_str.endswith(">") and not a_str.startswith("<runtime."):
                param_desc.append(a_str[1:-1])
        details = "({})".format("，".join(param_desc)) if param_desc else ""
        op_lines.append("  - {} {}".format(op_key, details))

    prompt = (
        "你是一个操作匹配助手。从以下操作列表中选出最匹配用户请求的一个。\n\n"
        "用户请求：{}\n\n"
        "可用操作：\n{}\n\n"
        "只返回 JSON，格式：{{\"operation_key\": \"操作名\", \"confidence\": 0.95}}\n"
        "confidence 范围 0-1，低于 0.5 表示不匹配。"
    ).format(user_request, "\n".join(op_lines))

    try:
        response = task_llm.call(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=500,
            enable_reasoning=False,
            request_label="direct_execution:match_operation",
        )
    except Exception as exc:
        logger.warning("LLM preflight call failed: %s", exc)
        return None, None, 0.0

    raw = (response.content or "").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM preflight: invalid JSON response: %s", raw[:200])
        return None, None, 0.0

    matched_key = str(data.get("operation_key") or "").strip()
    llm_confidence = float(data.get("confidence") or 0.0)

    if not matched_key or llm_confidence < confidence_threshold:
        logger.info(
            "LLM preflight: matched '%s' confidence=%.3f below threshold=%.3f",
            matched_key,
            llm_confidence,
            confidence_threshold,
        )
        return None, None, 0.0

    # Find the matching catalog entry
    for entry in catalog:
        if str(entry.get("operation_key") or "").strip() == matched_key:
            logger.info(
                "LLM preflight matched: operation='%s' confidence=%.3f (ontology=%s)",
                matched_key,
                llm_confidence,
                ontology_id,
            )
            return entry, matched_key, llm_confidence

    logger.warning("LLM preflight: returned unknown operation '%s'", matched_key)
    return None, None, 0.0
