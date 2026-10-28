"""Retrieval post-processing, ported from WeKnora's chat pipeline.

Pure functions over hits so they can be tested without a database:

- rerank threshold with WeKnora's fallback ladder and the composite score
  (internal/reranking/rerank.go);
- MMR diversity selection (internal/reranking/mmr.go), Jaccard over tokens;
- merging adjacent hits, short-hit neighbour expansion (merge_expand.go:
  below 350 chars grow to about 850, half before / half after) and a
  parent-section window, all computed on L1 offsets because chunks overlap;
- near-duplicate removal (search.go removePartialOverlaps);
- keyword-only query variants (query_expansion.go), no LLM.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Callable, Iterable

from . import text_utils

if TYPE_CHECKING:
    from .chunking import Block
    from .retrieval import Hit

FALLBACK_MIN_SCORE = 0.15
DEGRADE_FLOOR = 0.3
MAX_LOCATORS = 8
_BOUNDARY = "\n。！？；!?;"


# ---- rerank ---------------------------------------------------------------


def apply_rerank(
    pool: list["Hit"],
    ranked: list[tuple[int, float]],
    threshold_of: Callable[["Hit"], float],
    *,
    composite: bool = True,
) -> list["Hit"]:
    """Attach rerank scores, filter by each hit's threshold, rescore and sort.

    Ladder (per WeKnora): keep hits at or above the threshold; if none pass,
    retry once at ``max(threshold * 0.7, 0.3)`` for thresholds above 0.3;
    otherwise keep the single best hit when it scores at least 0.15.
    """

    scored: list[Hit] = []
    for index, score in ranked:
        if 0 <= index < len(pool):
            hit = pool[index]
            hit.rerank_score = float(score)
            hit.sources.add("rerank")
            scored.append(hit)
    if not scored:
        return []
    passed = [h for h in scored if h.rerank_score >= threshold_of(h)]
    if not passed:
        passed = [
            h
            for h in scored
            if threshold_of(h) > DEGRADE_FLOOR and h.rerank_score >= max(threshold_of(h) * 0.7, DEGRADE_FLOOR)
        ]
    if not passed:
        best = max(scored, key=lambda h: h.rerank_score)
        passed = [best] if best.rerank_score >= FALLBACK_MIN_SCORE else []
    for hit in passed:
        model = min(max(hit.rerank_score, 0.0), 1.0)
        hit.relevance = model
        hit.score = min(1.0, 0.6 * model + 0.3 * hit.base_score + 0.1) if composite else hit.rerank_score
    passed.sort(key=lambda h: h.score, reverse=True)
    return passed


# ---- MMR ------------------------------------------------------------------


def passage(hit: "Hit") -> str:
    text = hit.content
    if hit.questions:
        text += "\n" + "; ".join(hit.questions)
    return text


def select_mmr(hits: list["Hit"], k: int, lam: float = 0.7, *, max_wiki: int | None = None) -> list["Hit"]:
    """Greedy MMR: ``lam * relevance - (1 - lam) * max Jaccard to selected``.

    Scores are normalised to [0, 1] first so RRF-scale scores (~0.03) are not
    swamped by the redundancy term. At most ``max_wiki`` wiki pages are
    picked; ties go to the earlier hit.
    """

    if k <= 0 or not hits:
        return []
    top = max((h.score for h in hits), default=0.0)
    norm = [(h.score / top) if top > 0 else 0.0 for h in hits]
    toks = [text_utils.tokens(passage(h)) for h in hits]
    chosen: list[int] = []
    remaining = list(range(len(hits)))
    wiki = 0
    while remaining and len(chosen) < k:
        best, best_val = -1, float("-inf")
        for i in remaining:
            if max_wiki is not None and hits[i].is_wiki and wiki >= max_wiki:
                continue
            redundancy = max((text_utils.jaccard(toks[i], toks[j]) for j in chosen), default=0.0)
            value = lam * norm[i] - (1 - lam) * redundancy
            if value > best_val:
                best, best_val = i, value
        if best < 0:
            break
        chosen.append(best)
        remaining.remove(best)
        wiki += 1 if hits[best].is_wiki else 0
    return [hits[i] for i in chosen]


def select_top(hits: list["Hit"], k: int, *, max_wiki: int | None = None) -> list["Hit"]:
    out: list[Hit] = []
    wiki = 0
    for hit in hits:
        if len(out) >= k:
            break
        if max_wiki is not None and hit.is_wiki:
            if wiki >= max_wiki:
                continue
            wiki += 1
        out.append(hit)
    return out


# ---- context: merge / expand / parent --------------------------------------


def _union_locators(groups: Iterable[list]) -> list:
    out: list = []
    seen: set[str] = set()
    for locs in groups:
        for loc in locs or []:
            key = repr(sorted(loc.items())) if isinstance(loc, dict) else repr(loc)
            if key not in seen:
                seen.add(key)
                out.append(loc)
    return out[:MAX_LOCATORS]


def merge_adjacent(hits: list["Hit"], l1_of: Callable[[str], str | None]) -> list["Hit"]:
    """Merge hits of one document whose L1 ranges touch or overlap.

    The merged hit keeps the best score, the earliest citation fields and
    records every chunk it absorbed; its content is the L1 span.
    """

    by_doc: dict[str, list[Hit]] = {}
    order: list[Hit] = []
    for hit in hits:
        if hit.is_wiki:
            order.append(hit)
            continue
        by_doc.setdefault(hit.document_id, []).append(hit)
    merged_of: dict[int, Hit] = {}
    for doc_id, group in by_doc.items():
        l1 = l1_of(doc_id)
        group.sort(key=lambda h: (h.start_offset, h.end_offset))
        runs: list[list[Hit]] = []
        for hit in group:
            if runs and l1 is not None and hit.start_offset <= max(h.end_offset for h in runs[-1]) + 2:
                runs[-1].append(hit)
            elif runs and l1 is None and hit.seq == runs[-1][-1].seq + 1:
                runs[-1].append(hit)
            else:
                runs.append([hit])
        for run in runs:
            head = max(run, key=lambda h: h.score)
            if len(run) > 1:
                first = run[0]
                start = min(h.start_offset for h in run)
                end = max(h.end_offset for h in run)
                if l1 is not None:
                    head.core_abs = head.core_abs or (head.start_offset, head.end_offset)
                    head.content = l1[start:end]
                else:
                    head.content = "\n".join(h.content for h in run)
                head.start_offset, head.end_offset = start, end
                head.line_start = first.line_start
                head.anchor_id = first.anchor_id or head.anchor_id
                head.context_header = first.context_header or head.context_header
                head.source_locators = _union_locators(h.source_locators for h in run)
                head.merged_chunk_ids = list(dict.fromkeys(c for h in run for c in (h.merged_chunk_ids or [h.chunk_id])))
                head.sources = set().union(*(h.sources for h in run))
                head.sources.add("merged")
            for h in run:
                merged_of[id(h)] = head
    out: list[Hit] = []
    seen: set[int] = set()
    for hit in hits:
        target = hit if hit.is_wiki else merged_of.get(id(hit), hit)
        if id(target) not in seen:
            seen.add(id(target))
            out.append(target)
    return out


def _snap_start(l1: str, start: int, limit: int) -> int:
    """Move a window start forward to just after a boundary within ``limit`` chars."""

    if start <= 0:
        return 0
    window = l1[start : start + limit]
    for i, ch in enumerate(window):
        if ch in _BOUNDARY:
            return start + i + 1
    return start


def _snap_end(l1: str, end: int, limit: int) -> int:
    if end >= len(l1):
        return len(l1)
    window = l1[max(0, end - limit) : end]
    for i in range(len(window) - 1, -1, -1):
        if window[i] in _BOUNDARY:
            return end - (len(window) - 1 - i)
    return end


def expand_short(hit: "Hit", l1: str, min_chars: int, max_chars: int) -> None:
    """Grow a short text hit with surrounding L1 text (same document only).

    The budget ``max_chars - len`` goes half before and half after; a side
    that hits the document edge passes its unused share to the other side.
    Each extension is then trimmed back (by at most a third) to a sentence
    or line boundary.
    """

    if hit.is_wiki or hit.chunk_type != "text" or not l1:
        return
    start, end = hit.start_offset, hit.end_offset
    if end - start >= min_chars:
        return
    budget = max_chars - (end - start)
    before, after = budget // 2, budget - budget // 2
    if before > start:
        after += before - start
        before = start
    if after > len(l1) - end:
        before = min(start, before + after - (len(l1) - end))
        after = len(l1) - end
    if before == 0 and after == 0:
        return
    new_start = _snap_start(l1, start - before, before // 3) if before else start
    new_end = _snap_end(l1, end + after, after // 3) if after else end
    hit.core_abs = hit.core_abs or (start, end)
    hit.content = l1[new_start:new_end]
    hit.start_offset, hit.end_offset = new_start, new_end
    hit.expanded = True
    hit.sources.add("expanded")


def parent_window(hit: "Hit", l1: str, blocks: list["Block"], parent_size: int) -> None:
    """Return the enclosing section (capped at ``parent_size``) instead of the chunk.

    WeKnora's parent-child chunking matches on small child chunks and hands
    the parent to the LLM; here the parent is derived at query time from the
    heading structure of L1, so it needs no extra index.
    """

    if hit.is_wiki or not l1 or not blocks:
        return
    start, end = hit.start_offset, hit.end_offset
    idx = 0
    for i, block in enumerate(blocks):
        if block.start <= start < block.end or block.start >= start:
            idx = i
            break
    path = blocks[idx].header_path
    lo = idx
    while lo > 0 and blocks[lo - 1].header_path[: len(path)] == path:
        lo -= 1
    hi = idx
    while hi + 1 < len(blocks) and blocks[hi + 1].header_path[: len(path)] == path:
        hi += 1
    sec_start, sec_end = min(blocks[lo].start, start), max(blocks[hi].end, end)
    if sec_end - sec_start > parent_size:
        room = max(0, parent_size - (end - start))
        left = room // 2
        new_start = max(sec_start, start - left)
        new_end = min(sec_end, new_start + max(parent_size, end - new_start))
        new_start = max(sec_start, min(new_start, new_end - parent_size))
        sec_start, sec_end = new_start, new_end
    if (sec_start, sec_end) == (start, end):
        return
    hit.core_abs = hit.core_abs or (start, end)
    hit.content = l1[sec_start:sec_end]
    hit.start_offset, hit.end_offset = sec_start, sec_end
    hit.expanded = True
    hit.sources.add("parent")


def _near_duplicate(norm: str, tok: set[str], kept: list[tuple[str, set[str]]]) -> bool:
    for other_norm, other_tok in kept:
        if norm and norm in other_norm:
            return True
        small, large = sorted((len(tok), len(other_tok)))
        if small and large <= small * 3 and text_utils.overlap_min(tok, other_tok) >= 0.85:
            return True
    return False


def dedupe_ordered(hits: list["Hit"]) -> list["Hit"]:
    """Keep the first of near-identical hits (containment or >= 0.85 token overlap), preserving order.

    Run before the top-k cut so the same text stored in several documents
    (e.g. one manual uploaded as PDF and Word) does not use up result slots.
    """

    kept: list[tuple[str, set[str]]] = []
    out: list[Hit] = []
    for hit in hits:
        norm = text_utils.normalize(hit.content)
        tok = text_utils.tokens(hit.content)
        if _near_duplicate(norm, tok, kept):
            continue
        kept.append((norm, tok))
        out.append(hit)
    return out


def remove_overlaps(hits: list["Hit"]) -> list["Hit"]:
    """Drop the weaker of two near-identical hits, keeping the input order otherwise."""

    keep = dedupe_ordered(sorted(hits, key=lambda h: (-h.score, -len(h.content))))
    kept = {id(h) for h in keep}
    return [h for h in hits if id(h) in kept]


# ---- query expansion -------------------------------------------------------

_QUESTION_WORDS = re.compile(
    r"(请问|请告诉我|请说明|是什么|是多少|有哪些|有什么|为什么|怎么样|怎么办|怎么|如何|怎样|哪些|哪个|"
    r"什么|多少|是否|能否|可以吗|吗|呢|吧)"
)
_SPLIT = re.compile(r"[\s,，。.!！?？;；:：、()（）\[\]【】]+")
_QUOTED = re.compile(r"[\"“「『]([^\"”」』]{2,})[\"”」』]")


def expand_query(query: str, limit: int = 5) -> list[str]:
    """Keyword-only variants: quoted phrases, question words stripped, segments."""

    variants: list[str] = []
    variants.extend(m.strip() for m in _QUOTED.findall(query))
    stripped = _SPLIT.sub(" ", _QUESTION_WORDS.sub(" ", query)).strip()
    if stripped:
        variants.append(stripped)
    parts = [p for p in _SPLIT.split(_QUESTION_WORDS.sub(" ", query)) if len(p.strip()) >= 2]
    if len(parts) > 1:
        variants.extend(parts)
    out: list[str] = []
    base = text_utils.normalize(query)
    for v in variants:
        v = v.strip()
        if v and text_utils.normalize(v) != base and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out
