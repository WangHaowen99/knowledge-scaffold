"""Hybrid retrieval and document readers.

Query shapes follow WeKnora's ParadeDB retriever
(internal/application/repository/retriever/postgres/repository.go):
``content ||| query`` with ``paradedb.score`` for keywords and a cast to
``halfvec(dim)`` on both sides of ``<=>`` so the partial HNSW index is used.

Search flow (knobs in :mod:`kb_config` ``retrieval``):
  legs      chunk vectors, chunk BM25, generated-question vectors and BM25
            (resolved to their chunk; only knowledge bases that have
            questions), wiki page vectors and BM25 — run concurrently, each
            filtered by its knowledge base's threshold
  expand    keyword-only query variants when recall is low (optional)
  fuse      weighted RRF; ``base_score`` keeps a 0-1 relevance per hit
  rerank    model rerank with WeKnora's threshold ladder and composite score
  select    wiki boost, MMR (or plain top-k), wiki pages capped
  context   merge adjacent hits, grow short hits or return the parent section;
            a grown window stays in its section and a context header names
            every section the text comes from
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass, field

from . import db, kb_config, models_client, postprocess, storage, text_structure
from .chunking import parse_blocks
from .config import get_settings
from .pipeline import vector_literal

logger = logging.getLogger(__name__)

RERANK_PASSAGE_CHARS = 1500
WIKI_PASSAGE_CHARS = 1500
_CHUNK_COLUMNS = (
    "c.id, c.document_id, c.kb_id, c.seq, c.chunk_type, c.content, c.context_header,"
    " c.start_offset, c.end_offset, c.line_start, c.anchor_id, c.source_locators"
)
WIKI_TYPE_LABEL = {"summary": "文档摘要页", "entity": "实体", "concept": "概念", "index": "索引"}


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    kb_id: str
    seq: int
    chunk_type: str
    content: str
    context_header: str
    start_offset: int
    end_offset: int
    line_start: int
    anchor_id: str | None
    source_locators: list
    score: float = 0.0
    sources: set[str] = field(default_factory=set)
    title: str = ""
    file_name: str | None = None
    file_type: str | None = None
    l1_key: str | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    base_score: float = 0.0
    relevance: float = 0.0
    matched_question: str | None = None
    questions: list[str] = field(default_factory=list)
    merged_chunk_ids: list[str] = field(default_factory=list)
    expanded: bool = False
    # L1 offsets of the matched chunk once content was widened (merge / expand / parent)
    core_abs: tuple[int, int] | None = None
    wiki_slug: str | None = None
    wiki_type: str | None = None
    wiki_sources: list[str] = field(default_factory=list)

    @property
    def is_wiki(self) -> bool:
        return self.chunk_type == "wiki_page"

    @property
    def core(self) -> tuple[int, int] | None:
        """Matched chunk's span inside ``content`` (None when content is the chunk itself)."""

        if self.core_abs is None:
            return None
        return self.core_abs[0] - self.start_offset, self.core_abs[1] - self.start_offset

    def _extra(self) -> dict:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 6)

        return {
            "relevance": r(self.relevance),
            "scores": {
                "vector": r(self.vector_score),
                "keyword": r(self.keyword_score),
                "rerank": r(self.rerank_score),
                "base": r(self.base_score),
            },
        }

    def as_dict(self) -> dict:
        out = {
            "chunkId": self.chunk_id,
            "docId": self.document_id,
            "kbId": self.kb_id,
            "seq": self.seq,
            "chunkType": self.chunk_type,
            "title": self.title,
            "fileName": self.file_name,
            "contextHeader": self.context_header,
            "content": self.content,
            "score": round(self.score, 6),
            "lineStart": self.line_start,
            "anchorId": self.anchor_id,
            "locators": self.source_locators,
            "matchedBy": sorted(self.sources),
            **self._extra(),
        }
        if self.matched_question:
            out["matchedQuestion"] = self.matched_question
        if self.merged_chunk_ids:
            out["mergedChunkIds"] = self.merged_chunk_ids
        if self.core:
            out["core"] = list(self.core)
        if self.is_wiki:
            out.update(wikiSlug=self.wiki_slug, wikiType=self.wiki_type, wikiSources=self.wiki_sources)
        return out

    def as_snake(self) -> dict:
        out = {
            "chunk_id": self.chunk_id,
            "doc_id": self.document_id,
            "kb_id": self.kb_id,
            "seq": self.seq,
            "chunk_type": self.chunk_type,
            "title": self.title,
            "file_name": self.file_name,
            "context_header": self.context_header,
            "content": self.content,
            "score": round(self.score, 6),
            "line_start": self.line_start,
            "anchor_id": self.anchor_id,
            "locators": self.source_locators,
            "matched_by": sorted(self.sources),
            "matched_question": self.matched_question,
            "questions": self.questions,
            "merged_chunk_ids": self.merged_chunk_ids,
            "expanded": self.expanded,
            "core": list(self.core) if self.core else None,
            **self._extra(),
        }
        if self.is_wiki:
            out.update(wiki_slug=self.wiki_slug, wiki_type=self.wiki_type, wiki_sources=self.wiki_sources)
        return out


def _hit(row) -> Hit:
    return Hit(
        chunk_id=row["id"],
        document_id=row["document_id"],
        kb_id=row["kb_id"],
        seq=row["seq"],
        chunk_type=row["chunk_type"],
        content=row["content"],
        context_header=row["context_header"],
        start_offset=row["start_offset"],
        end_offset=row["end_offset"],
        line_start=row["line_start"],
        anchor_id=row["anchor_id"],
        source_locators=row["source_locators"] or [],
    )


def wiki_hit(row) -> Hit:
    from .wiki.links import plain

    summary = plain(row["summary"])
    return Hit(
        chunk_id=f"wiki:{row['id']}",
        document_id="",
        kb_id=row["kb_id"],
        seq=0,
        chunk_type="wiki_page",
        content=((summary + "\n\n") if summary else "") + plain(row["content"][:WIKI_PASSAGE_CHARS]),
        context_header=f"Wiki · {WIKI_TYPE_LABEL.get(row['page_type'], row['page_type'])}",
        start_offset=0,
        end_offset=0,
        line_start=0,
        anchor_id=None,
        source_locators=[],
        title=row["title"],
        wiki_slug=row["slug"],
        wiki_type=row["page_type"],
        wiki_sources=list(row["source_doc_ids"] or []),
    )


# ---- legs -----------------------------------------------------------------


async def _knn(sql: str, *args) -> list:
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL hnsw.ef_search = 100")
            await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            return await conn.fetch(sql, *args)


async def _vector(tenant_id: str, kb_ids: list[str], vec: list[float], limit: int) -> list[Hit]:
    dim = len(vec)
    # Literal dimension: a bind parameter would stop the planner from
    # matching the partial HNSW index predicate in a generic plan.
    rows = await _knn(
        f"SELECT {_CHUNK_COLUMNS}, (c.embedding::halfvec({dim}) <=> $1::halfvec({dim})) AS distance"
        f" FROM chunks c WHERE c.dimension = {int(dim)} AND c.is_active"
        " AND c.kb_id = ANY($2::text[]) AND c.tenant_id = $3"
        f" ORDER BY c.embedding::halfvec({dim}) <=> $1::halfvec({dim}) LIMIT $4",
        vector_literal(vec),
        kb_ids,
        tenant_id,
        limit,
    )
    hits = []
    for row in rows:
        hit = _hit(row)
        hit.vector_score = 1.0 - float(row["distance"])
        hits.append(hit)
    return hits


async def _keywords(tenant_id: str, kb_ids: list[str], query: str, limit: int) -> list[Hit]:
    sql = (
        f"SELECT {_CHUNK_COLUMNS}, paradedb.score(c.id) AS bm25"
        " FROM chunks c WHERE c.content ||| $1 AND c.is_active"
        " AND c.kb_id = ANY($2::text[]) AND c.tenant_id = $3"
        " ORDER BY bm25 DESC LIMIT $4"
    )
    try:
        rows = await db.pool().fetch(sql, query, kb_ids, tenant_id, limit)
    except Exception:  # noqa: BLE001 - keyword leg is best effort
        logger.exception("bm25 query failed")
        return []
    hits = []
    for row in rows:
        hit = _hit(row)
        hit.keyword_score = float(row["bm25"] or 0.0)
        hits.append(hit)
    return hits


def _like_pattern(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _exact(tenant_id: str, kb_ids: list[str], terms: list[str], limit: int) -> list[Hit]:
    """Chunks containing one of ``terms`` verbatim (case-insensitive); the tokenizer splits codes apart."""

    sql = (
        f"SELECT {_CHUNK_COLUMNS} FROM chunks c WHERE c.is_active"
        " AND c.kb_id = ANY($2::text[]) AND c.tenant_id = $3 AND c.content ILIKE ANY($1::text[]) LIMIT $4"
    )
    try:
        rows = await db.pool().fetch(sql, [_like_pattern(t) for t in terms], kb_ids, tenant_id, limit)
    except Exception:  # noqa: BLE001 - best effort, like the keyword leg
        logger.exception("exact identifier query failed")
        return []
    return [_hit(row) for row in rows]


def merge_exact(keyword_hits: list[Hit], exact_hits: list[Hit], terms: list[str]) -> list[Hit]:
    """Keyword leg with verbatim identifier matches first, at the top (normalised) keyword score."""

    by_id = {h.chunk_id: h for h in keyword_hits}
    for hit in exact_hits:
        by_id.setdefault(hit.chunk_id, hit)
    found = {cid: postprocess.contains_terms(h, terms) for cid, h in by_id.items()}
    for cid, hit in by_id.items():
        if found[cid]:
            hit.keyword_score = 1.0
            hit.sources.add("exact")
    order = {h.chunk_id: i for i, h in enumerate(keyword_hits)}
    return sorted(by_id.values(), key=lambda h: (-found[h.chunk_id], order.get(h.chunk_id, len(order))))


async def _question_vector(tenant_id: str, kb_ids: list[str], vec: list[float], limit: int) -> list[tuple]:
    dim = len(vec)
    rows = await _knn(
        f"SELECT q.chunk_id, q.question, (q.embedding::halfvec({dim}) <=> $1::halfvec({dim})) AS distance"
        f" FROM chunk_questions q WHERE q.dimension = {int(dim)}"
        " AND q.kb_id = ANY($2::text[]) AND q.tenant_id = $3"
        f" ORDER BY q.embedding::halfvec({dim}) <=> $1::halfvec({dim}) LIMIT $4",
        vector_literal(vec),
        kb_ids,
        tenant_id,
        limit,
    )
    return [(r["chunk_id"], r["question"], 1.0 - float(r["distance"])) for r in rows]


async def _question_keywords(tenant_id: str, kb_ids: list[str], query: str, limit: int) -> list[tuple]:
    try:
        rows = await db.pool().fetch(
            "SELECT q.chunk_id, q.question, paradedb.score(q.id) AS bm25 FROM chunk_questions q"
            " WHERE q.question ||| $1 AND q.kb_id = ANY($2::text[]) AND q.tenant_id = $3"
            " ORDER BY bm25 DESC LIMIT $4",
            query,
            kb_ids,
            tenant_id,
            limit,
        )
    except Exception:  # noqa: BLE001
        logger.exception("question bm25 query failed")
        return []
    return [(r["chunk_id"], r["question"], float(r["bm25"] or 0.0)) for r in rows]


_WIKI_COLUMNS = "w.id, w.kb_id, w.slug, w.title, w.page_type, w.summary, w.content, w.source_doc_ids"


async def _wiki_vector(tenant_id: str, kb_ids: list[str], vec: list[float], limit: int) -> list[Hit]:
    dim = len(vec)
    rows = await _knn(
        f"SELECT {_WIKI_COLUMNS}, (w.embedding::halfvec({dim}) <=> $1::halfvec({dim})) AS distance"
        f" FROM wiki_pages w WHERE w.dimension = {int(dim)} AND w.deleted_at IS NULL"
        " AND w.kb_id = ANY($2::text[]) AND w.tenant_id = $3"
        f" ORDER BY w.embedding::halfvec({dim}) <=> $1::halfvec({dim}) LIMIT $4",
        vector_literal(vec),
        kb_ids,
        tenant_id,
        limit,
    )
    hits = []
    for row in rows:
        hit = wiki_hit(row)
        hit.vector_score = 1.0 - float(row["distance"])
        hits.append(hit)
    return hits


async def _wiki_keywords(tenant_id: str, kb_ids: list[str], query: str, limit: int) -> list[Hit]:
    try:
        rows = await db.pool().fetch(
            f"SELECT {_WIKI_COLUMNS}, paradedb.score(w.id) AS bm25 FROM wiki_pages w"
            " WHERE (w.title ||| $1 OR w.summary ||| $1 OR w.content ||| $1) AND w.deleted_at IS NULL"
            " AND w.kb_id = ANY($2::text[]) AND w.tenant_id = $3 ORDER BY bm25 DESC LIMIT $4",
            query,
            kb_ids,
            tenant_id,
            limit,
        )
    except Exception:  # noqa: BLE001
        logger.exception("wiki bm25 query failed")
        return []
    hits = []
    for row in rows:
        hit = wiki_hit(row)
        hit.keyword_score = float(row["bm25"] or 0.0)
        hits.append(hit)
    return hits


async def _load_chunks(chunk_ids: list[str]) -> dict[str, Hit]:
    if not chunk_ids:
        return {}
    rows = await db.pool().fetch(
        f"SELECT {_CHUNK_COLUMNS} FROM chunks c WHERE c.id = ANY($1::text[]) AND c.is_active", chunk_ids
    )
    return {r["id"]: _hit(r) for r in rows}


async def _attach_documents(hits: list[Hit]) -> list[Hit]:
    doc_ids = list({h.document_id for h in hits if not h.is_wiki})
    docs = {}
    if doc_ids:
        rows = await db.pool().fetch(
            "SELECT id, title, file_name, file_type, l1_key FROM documents"
            " WHERE id = ANY($1::text[]) AND deleted_at IS NULL",
            doc_ids,
        )
        docs = {r["id"]: r for r in rows}
    kept = []
    for hit in hits:
        if hit.is_wiki:
            kept.append(hit)
            continue
        doc = docs.get(hit.document_id)
        if doc is None:
            continue
        hit.title, hit.file_name, hit.file_type, hit.l1_key = (
            doc["title"],
            doc["file_name"],
            doc["file_type"],
            doc["l1_key"],
        )
        kept.append(hit)
    return kept


@functools.lru_cache(maxsize=32)
def _l1_text(key: str) -> str:
    return storage.get_text(key) if storage.exists(key) else ""


@functools.lru_cache(maxsize=32)
def _l1_blocks(key: str) -> list:
    return parse_blocks(_l1_text(key))


async def kb_configs(kb_ids: list[str]) -> dict[str, kb_config.KbConfig]:
    if not kb_ids:
        return {}
    rows = await db.pool().fetch("SELECT id, config FROM knowledge_bases WHERE id = ANY($1::text[])", kb_ids)
    return {r["id"]: kb_config.load(r["config"]) for r in rows}


# ---- fusion helpers ----------------------------------------------------------


def _normalize_keyword(hits: list[Hit]) -> None:
    top = max((h.keyword_score or 0.0 for h in hits), default=0.0)
    for hit in hits:
        hit.keyword_score = (hit.keyword_score or 0.0) / top if top > 0 else 0.0


def _fold_questions(
    leg: list[Hit], questions: list[tuple], loaded: dict[str, Hit], attr: str
) -> list[Hit]:
    """Resolve question hits to their chunk; the chunk keeps its best score on ``attr``."""

    by_id = {h.chunk_id: h for h in leg}
    for chunk_id, question, score in questions:
        hit = by_id.get(chunk_id)
        if hit is None:
            base = loaded.get(chunk_id)
            if base is None:
                continue
            hit = Hit(**{k: v for k, v in base.__dict__.items() if k != "sources"})
            by_id[chunk_id] = hit
        current = getattr(hit, attr)
        if current is None or score > current:
            setattr(hit, attr, score)
            hit.matched_question = question
            hit.sources.add("question")
    return sorted(by_id.values(), key=lambda h: getattr(h, attr) or 0.0, reverse=True)


def _rrf(
    vector: list[Hit], keyword: list[Hit], rc: kb_config.RetrievalConfig
) -> list[Hit]:
    fused: dict[str, Hit] = {}
    rrf: dict[str, float] = {}
    for leg, hits, weight in (("vector", vector, rc.vector_weight), ("keyword", keyword, rc.keyword_weight)):
        for rank, hit in enumerate(hits):
            entry = fused.get(hit.chunk_id)
            if entry is None:
                entry = fused[hit.chunk_id] = hit
            else:
                entry.sources |= hit.sources
                entry.vector_score = entry.vector_score if hit.vector_score is None else hit.vector_score
                entry.keyword_score = entry.keyword_score if hit.keyword_score is None else hit.keyword_score
                entry.matched_question = entry.matched_question or hit.matched_question
            entry.sources.add(leg)
            rrf[hit.chunk_id] = rrf.get(hit.chunk_id, 0.0) + weight / (rc.rrf_k + rank + 1)
    for chunk_id, hit in fused.items():
        hit.score = rrf[chunk_id]
        keyword_rel = 0.5 + 0.5 * hit.keyword_score if hit.keyword_score is not None else 0.0
        hit.base_score = max(hit.vector_score or 0.0, keyword_rel)
        hit.relevance = hit.base_score
    return sorted(fused.values(), key=lambda h: h.score, reverse=True)


# ---- search -----------------------------------------------------------------


class SearchUnavailable(Exception):
    """No retrieval leg can run: the query embedding failed and keyword search is off."""


VECTOR_DOWN_WARNING = "向量检索暂不可用（调用向量模型失败，例如模型密钥无效或模型服务不可用），本次结果只来自关键词检索"


async def _query_vector(query: str, rc: kb_config.RetrievalConfig, warnings: list[str] | None) -> list[float] | None:
    """Query embedding, or None (keyword-only search) when the model fails and keywords can answer."""

    if not rc.vector_enabled:
        return None
    try:
        return await models_client.embed_query(query)
    except models_client.ModelError as exc:
        logger.warning("query embedding failed: %s", exc)
        if not rc.keyword_enabled:
            raise SearchUnavailable(
                "检索暂不可用：调用向量模型失败（例如模型密钥无效或模型服务不可用），且所选知识库没有开启关键词检索"
            ) from exc
        if warnings is not None:
            warnings.append(VECTOR_DOWN_WARNING)
        return None


async def _kbs_with_questions(kb_ids: list[str]) -> list[str]:
    """Knowledge bases holding generated questions; the question legs search only these.

    Each question leg costs a vector and a BM25 query over the whole question
    index even when it can find nothing, which under concurrent searches
    showed up as hundreds of milliseconds per search.
    """

    if not kb_ids:
        return []
    rows = await db.pool().fetch(
        "SELECT DISTINCT kb_id FROM documents WHERE kb_id = ANY($1::text[]) AND deleted_at IS NULL"
        " AND question_count > 0",
        kb_ids,
    )
    return [r["kb_id"] for r in rows]


async def search(
    tenant_id: str,
    kb_ids: list[str],
    query: str,
    *,
    top_k: int = 10,
    use_rerank: bool | None = None,
    min_score: float | None = None,
    options: dict | None = None,
    include_chunks: bool = True,
    include_wiki: bool = True,
    wiki_limit: int | None = None,
    trace: dict | None = None,
    warnings: list[str] | None = None,
) -> list[Hit]:
    """Hybrid search. ``min_score`` filters on ``relevance`` (0-1); ``options``
    overrides the knowledge bases' retrieval settings for this call.
    ``include_chunks=False`` searches wiki pages only (every wiki-enabled
    knowledge base, ``wiki_limit`` pages). When the embedding model fails the
    search falls back to keywords and appends a note to ``warnings``; it raises
    SearchUnavailable when keywords are off too."""

    started = time.perf_counter()
    if not kb_ids or not query.strip():
        return []
    configs = await kb_configs(kb_ids)
    if use_rerank is not None:
        options = {**(options or {}), "rerank_enabled": bool(use_rerank)}
    rc = kb_config.merge_retrieval(list(configs.values()), options)
    overrides = kb_config.retrieval_overrides(options)

    def cfg_of(kb_id: str) -> kb_config.KbConfig:
        return configs.get(kb_id) or kb_config.defaults()

    def knob(hit: Hit, name: str):
        return overrides.get(name, getattr(cfg_of(hit.kb_id).retrieval, name))

    chunk_kbs = [k for k in kb_ids if include_chunks and cfg_of(k).indexing.rag_enabled]
    def wiki_searchable(kb_id: str) -> bool:
        wiki = cfg_of(kb_id).wiki
        if not (include_wiki and wiki.enabled):
            return False
        return not include_chunks or (wiki.include_in_search and wiki.max_search_results > 0)

    wiki_kbs = [k for k in kb_ids if wiki_searchable(k)]
    wiki_cap = wiki_limit if wiki_limit is not None else max(
        (cfg_of(k).wiki.max_search_results for k in wiki_kbs), default=0
    )
    if not chunk_kbs and not wiki_kbs:
        return []

    async def none() -> list:
        return []

    stage_ms: dict[str, float] = {}
    mark = time.perf_counter()
    # which knowledge bases have generated questions is looked up while the query is embedded
    vec, question_kbs = await asyncio.gather(
        _query_vector(query, rc, warnings),
        _kbs_with_questions(chunk_kbs) if rc.use_questions and chunk_kbs else none(),
    )
    stage_ms["embed"], mark = round((time.perf_counter() - mark) * 1000, 1), time.perf_counter()

    terms = postprocess.identifier_terms(query) if rc.keyword_enabled and chunk_kbs else []
    legs = await asyncio.gather(
        _vector(tenant_id, chunk_kbs, vec, rc.vector_top_k) if vec and chunk_kbs else none(),
        _keywords(tenant_id, chunk_kbs, query, rc.keyword_top_k) if rc.keyword_enabled and chunk_kbs else none(),
        _question_vector(tenant_id, question_kbs, vec, rc.vector_top_k) if vec and question_kbs else none(),
        _question_keywords(tenant_id, question_kbs, query, rc.keyword_top_k)
        if rc.keyword_enabled and question_kbs
        else none(),
        _wiki_vector(tenant_id, wiki_kbs, vec, max(10, wiki_cap * 3)) if vec and wiki_kbs else none(),
        _wiki_keywords(tenant_id, wiki_kbs, query, max(10, wiki_cap * 3))
        if rc.keyword_enabled and wiki_kbs
        else none(),
        _exact(tenant_id, chunk_kbs, terms, rc.keyword_top_k) if terms else none(),
    )
    stage_ms["legs"], mark = round((time.perf_counter() - mark) * 1000, 1), time.perf_counter()
    vector_hits, keyword_hits, q_vector, q_keyword, wiki_vec, wiki_kw, exact_hits = legs
    counts = {
        "vector": len(vector_hits),
        "keyword": len(keyword_hits),
        "question_vector": len(q_vector),
        "question_keyword": len(q_keyword),
        "wiki_vector": len(wiki_vec),
        "wiki_keyword": len(wiki_kw),
        "exact": len(exact_hits),
    }

    # question hits -> parent chunks
    if q_vector or q_keyword:
        known = {h.chunk_id for h in vector_hits} | {h.chunk_id for h in keyword_hits}
        loaded = await _load_chunks(list({c for c, *_ in (*q_vector, *q_keyword)} - known))
        loaded.update({h.chunk_id: h for h in (*vector_hits, *keyword_hits)})
        top_q = max((s for *_, s in q_keyword), default=0.0)
        q_keyword = [(c, q, s / top_q if top_q > 0 else 0.0) for c, q, s in q_keyword]
        _normalize_keyword(keyword_hits)
        vector_hits = _fold_questions(vector_hits, q_vector, loaded, "vector_score")
        keyword_hits = _fold_questions(keyword_hits, q_keyword, loaded, "keyword_score")
    else:
        _normalize_keyword(keyword_hits)
    _normalize_keyword(wiki_kw)
    if terms:
        keyword_hits = merge_exact(keyword_hits, exact_hits, terms)

    vector_hits = [h for h in vector_hits if (h.vector_score or 0.0) >= knob(h, "vector_threshold")]
    keyword_hits = [h for h in keyword_hits if (h.keyword_score or 0.0) >= knob(h, "keyword_threshold")]
    wiki_vec = [h for h in wiki_vec if (h.vector_score or 0.0) >= knob(h, "vector_threshold")]
    wiki_kw = [h for h in wiki_kw if (h.keyword_score or 0.0) >= knob(h, "keyword_threshold")]

    # optional keyword-only query variants when recall is thin
    variants: list[str] = []
    if rc.query_expansion and rc.keyword_enabled and chunk_kbs:
        distinct = {h.chunk_id for h in vector_hits} | {h.chunk_id for h in keyword_hits}
        if len(distinct) < rc.vector_top_k:
            variants = postprocess.expand_query(query)
            extra = await asyncio.gather(*(_keywords(tenant_id, chunk_kbs, v, rc.keyword_top_k) for v in variants))
            have = {h.chunk_id: h for h in keyword_hits}
            for found in extra:
                _normalize_keyword(found)
                for hit in found:
                    if (hit.keyword_score or 0.0) < knob(hit, "keyword_threshold") * 0.8:
                        continue
                    hit.sources.add("expansion")
                    current = have.get(hit.chunk_id)
                    if current is None or (hit.keyword_score or 0) > (current.keyword_score or 0):
                        have[hit.chunk_id] = hit
            keyword_hits = sorted(have.values(), key=lambda h: h.keyword_score or 0.0, reverse=True)
            counts["expansion_variants"] = len(variants)

    chunks_fused = _rrf(vector_hits, keyword_hits, rc)
    wiki_fused = _rrf(wiki_vec, wiki_kw, rc)
    counts["fused"] = len(chunks_fused)
    counts["wiki_fused"] = len(wiki_fused)

    pool = chunks_fused[: rc.rerank_candidates] + wiki_fused[: max(wiki_cap * 2, 0)]
    from .questions import questions_for_chunks

    qmap = await questions_for_chunks([h.chunk_id for h in pool if not h.is_wiki]) if question_kbs else {}
    for hit in pool:
        hit.questions = qmap.get(hit.chunk_id, [])

    reranked = False
    if rc.rerank_enabled and pool and get_settings().rerank_url:
        passages = []
        for h in pool:
            head = f"{h.title}\n" if h.is_wiki else (f"{h.context_header}\n" if h.context_header else "")
            passages.append(head + postprocess.passage(h)[:RERANK_PASSAGE_CHARS])
        try:
            ranked = await models_client.rerank(query, passages, top_n=len(pool))
        except models_client.ModelError:
            logger.warning("rerank failed, using fused order")
            ranked = []
        if ranked:
            kept = postprocess.apply_rerank(
                pool, ranked, lambda h: float(knob(h, "rerank_threshold")), composite=rc.composite_score
            )
            if terms:  # a verbatim code match stays a candidate even below the rerank threshold
                kept_ids = {id(h) for h in kept}
                kept += [h for h in pool if id(h) not in kept_ids and postprocess.contains_terms(h, terms)]
            pool = kept
            reranked = True
    if not reranked:
        pool.sort(key=lambda h: h.score, reverse=True)
    counts["after_rerank"] = len(pool)
    stage_ms["rerank"], mark = round((time.perf_counter() - mark) * 1000, 1), time.perf_counter()

    for hit in pool:
        if hit.is_wiki:
            hit.score *= cfg_of(hit.kb_id).wiki.boost
            hit.sources.add("wiki")
    postprocess.boost_exact(pool, terms)
    pool.sort(key=lambda h: h.score, reverse=True)

    if min_score is not None:
        pool = [h for h in pool if h.relevance >= float(min_score)]

    # Order the whole pool, drop near-duplicates, then cut: duplicates must not
    # take result slots that other candidates could fill.
    if rc.mmr_enabled:
        pool = postprocess.select_mmr(pool, len(pool), rc.mmr_lambda, max_wiki=wiki_cap)
    else:
        pool = postprocess.select_top(pool, len(pool), max_wiki=wiki_cap)
    deduped = postprocess.dedupe_ordered(pool)
    counts["duplicates"] = len(pool) - len(deduped)
    # Spare candidates: merging and overlap removal below can shrink the list,
    # and the spares fill those slots (or merge into a better hit).
    pool = deduped[: top_k * 2]
    counts["selected"] = len(pool)

    hits = await _attach_documents(pool)
    l1_keys = {h.document_id: h.l1_key for h in hits if not h.is_wiki and h.l1_key}

    def l1_of(doc_id: str) -> str | None:
        key = l1_keys.get(doc_id)
        return _l1_text(key) if key else None

    def blocks_of(doc_id: str) -> list | None:
        key = l1_keys.get(doc_id)
        return _l1_blocks(key) if key else None

    if rc.parent_child:
        for hit in hits:
            key = l1_keys.get(hit.document_id)
            if key:
                postprocess.parent_window(hit, _l1_text(key), _l1_blocks(key), rc.parent_chunk_size)
        hits = postprocess.merge_adjacent(hits, l1_of, blocks_of) if rc.merge_adjacent else hits
    else:
        if rc.merge_adjacent:
            hits = postprocess.merge_adjacent(hits, l1_of, blocks_of)
        if rc.expand_enabled:
            for hit in hits:
                text = l1_of(hit.document_id) if not hit.is_wiki else None
                if text and hit.end_offset - hit.start_offset < rc.expand_min_chars:
                    postprocess.expand_short(
                        hit, text, rc.expand_min_chars, rc.expand_max_chars, blocks_of(hit.document_id)
                    )
            # expansion can make windows of one document overlap: merge them again
            hits = postprocess.merge_adjacent(hits, l1_of, blocks_of)
    hits = postprocess.remove_overlaps(hits)
    hits.sort(key=lambda h: (-h.score, h.kb_id, h.document_id, h.seq, h.chunk_id))
    hits = hits[:top_k]
    counts["returned"] = len(hits)
    stage_ms["context"] = round((time.perf_counter() - mark) * 1000, 1)
    if trace is not None:
        trace.update(
            counts=counts,
            reranked=reranked,
            variants=variants,
            identifiers=terms,
            warnings=list(warnings or []),
            settings=rc.model_dump(),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            # embed: query embedding; legs: the database queries; rerank: fusion,
            # question lookup and the rerank model; context: documents, windows, merging
            timings_ms=stage_ms,
        )
    return hits


async def doc_titles(doc_ids: list[str]) -> dict[str, str]:
    if not doc_ids:
        return {}
    rows = await db.pool().fetch(
        "SELECT id, title FROM documents WHERE id = ANY($1::text[]) AND deleted_at IS NULL", doc_ids
    )
    return {r["id"]: r["title"] for r in rows}


async def list_documents(tenant_id: str, kb_id: str, *, limit: int = 500, offset: int = 0) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT id, kb_id, title, file_name, file_type, file_size, folder_path, source_type, source_url,"
        " parse_status, stage, error_code, error_message, summary, summary_status, chunk_count,"
        " l1_chars, created_at, processed_at FROM documents"
        " WHERE tenant_id = $1 AND kb_id = $2 AND deleted_at IS NULL"
        " ORDER BY created_at DESC LIMIT $3 OFFSET $4",
        tenant_id,
        kb_id,
        limit,
        offset,
    )
    return [dict(r) for r in rows]


async def get_document(tenant_id: str, doc_id: str, kb_ids: list[str] | None = None) -> dict | None:
    row = await db.pool().fetchrow(
        "SELECT * FROM documents WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL", doc_id, tenant_id
    )
    if row is None:
        return None
    if kb_ids is not None and row["kb_id"] not in kb_ids:
        return None
    return dict(row)


async def read_chunks(doc_id: str, *, offset: int = 0, limit: int = 20) -> tuple[list[dict], int]:
    total = await db.pool().fetchval(
        "SELECT count(*) FROM chunks WHERE document_id = $1 AND is_active", doc_id
    )
    rows = await db.pool().fetch(
        "SELECT id, seq, chunk_type, content, context_header, line_start, anchor_id, source_locators"
        " FROM chunks WHERE document_id = $1 AND is_active ORDER BY seq OFFSET $2 LIMIT $3",
        doc_id,
        offset,
        limit,
    )
    return [dict(r) for r in rows], int(total or 0)


def read_l1(doc: dict) -> str:
    if not doc.get("l1_key"):
        return ""
    return storage.get_text(doc["l1_key"])


def read_l2(doc: dict) -> str:
    if not doc.get("l1_key"):
        return ""
    key = doc["l1_key"].replace("/l1.", "/l2.").replace(".md", ".json")
    return storage.get_text(key) if storage.exists(key) else ""


async def grep(
    tenant_id: str,
    kb_ids: list[str],
    pattern: str,
    *,
    max_results: int = 50,
    max_lines_per_doc: int = 10,
    max_docs: int = 300,
) -> dict:
    regex, mode = text_structure.compile_pattern(pattern)
    rows = await db.pool().fetch(
        "SELECT id, kb_id, title, file_name, l1_key FROM documents WHERE tenant_id = $1"
        " AND kb_id = ANY($2::text[]) AND deleted_at IS NULL AND parse_status = 'completed'"
        " ORDER BY created_at DESC LIMIT $3",
        tenant_id,
        kb_ids,
        max_docs,
    )
    # file reads and a caller-supplied regex over every document: keep them off the event loop
    # and off its default thread pool (see text_structure.grep_executor)
    return await asyncio.get_running_loop().run_in_executor(
        text_structure.grep_executor(), _grep_rows, rows, regex, mode, pattern, max_results, max_lines_per_doc
    )


def _grep_rows(rows: list, regex: re.Pattern[str], mode: str, pattern: str, max_results: int, max_lines_per_doc: int) -> dict:
    matches = []
    total = 0
    docs_hit = 0
    has_more = False
    for row in rows:
        if not row["l1_key"]:
            continue
        hits, found = text_structure.grep_lines(storage.get_text(row["l1_key"]), regex, max_lines_per_doc)
        total += hits
        lines_hit = [{"lineNumber": x["line_number"], "text": x["text"]} for x in found]
        if lines_hit:
            docs_hit += 1
            if len(matches) < max_results:
                matches.append(
                    {
                        "docId": row["id"],
                        "kbId": row["kb_id"],
                        "title": row["title"],
                        "fileName": row["file_name"],
                        "pageId": f"{row['id']}:L1",
                        "pageType": "structure_md",
                        "lines": lines_hit,
                    }
                )
            else:
                has_more = True
    return {
        "pattern": pattern,
        "searchMode": mode,
        "totalMatches": total,
        "totalDocuments": docs_hit,
        "hasMore": has_more,
        "matches": matches,
    }
