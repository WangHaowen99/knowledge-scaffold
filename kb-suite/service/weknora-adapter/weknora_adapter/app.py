"""knowledge-provider/v1 routes backed by WeKnora."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from kb_provider_kit import (
    CONTRACT, Ids, ProviderError, check_bearer, install_error_handler, not_found, page_span, segment, segments,
)
from kb_provider_kit import reading, textfiles
from kb_provider_kit.uploads import UploadStore
from kb_service import postprocess

from . import __version__, mapping
from .chunk_stats import ChunkStats
from .client import WeKnora, get_settings

logger = logging.getLogger(__name__)

CHUNK_PAGE = 100
MAX_L1_CHUNKS = 5000
FILE_TYPES = ["pdf", "docx", "doc", "txt", "md", "markdown", "xlsx", "xls", "csv", "pptx", "ppt", "html", "htm", "json"]
FEATURES = [
    "kbs.write", "ingest.file", "ingest.text", "ingest.url", "documents.delete", "documents.reprocess",
    "documents.cancel", "documents.file", "wiki.read", "content.structure", "grep", "uploads",
]
# WeKnora's largest page_size for knowledge lists
DOC_PAGE = 1000
# exact identifier recall on search: documents scanned, parallel reads, time budget
EXACT_MAX_DOCS = 300
EXACT_CONCURRENCY = 8
EXACT_BUDGET_S = 8.0
# a knowledge base's chunk total is recomputed in the background once it is this old,
# or as soon as its document or processing count changes
CHUNK_TTL_S = 10.0
# longest a knowledge-base view waits for a chunk total being computed; without one
# the view leaves "chunks" out and the next view has it
CHUNK_WAIT_S = 2.0

_client: WeKnora | None = None
_chunks: ChunkStats | None = None


class WikiDisabled(ProviderError):
    """WeKnora's answer for a knowledge base without a wiki (a 404 ``not_found`` to the gateway)."""

    def __init__(self) -> None:
        super().__init__(404, "not_found", "这个知识库没有开启 Wiki")


# (upstream filter value, page, page_size) -> (items, total)
Fetch = Callable[[str | None, int, int], Awaitable[tuple[list[dict], int]]]


async def _pages(fetch: Fetch, value: str | None, offset: int, limit: int, page_size: int) -> tuple[list[dict], int]:
    items: list[dict] = []
    total = 0
    for page, start, stop in page_span(offset, limit, page_size):
        got, total = await fetch(value, page, page_size)
        items.extend(got[start:stop])
        if len(got) < page_size:
            break
    return items, total


async def window(
    fetch: Fetch, values: list[str | None], offset: int, limit: int, *, key: Callable[[dict], Any], reverse: bool,
) -> tuple[list[dict], int]:
    """``offset``/``limit`` over WeKnora's page/page_size lists.

    WeKnora filters on one exact value; a contract value that stands for several
    WeKnora values (status "completed" is completed + finalizing) reads the head
    of each list and merges them in WeKnora's own sort order.
    """

    if len(values) == 1:
        return await _pages(fetch, values[0], offset, limit, limit)
    parts = await asyncio.gather(*(_pages(fetch, v, 0, offset + limit, DOC_PAGE) for v in values))
    merged = sorted((item for items, _ in parts for item in items), key=key, reverse=reverse)
    return merged[offset:offset + limit], sum(total for _, total in parts)


def wk() -> WeKnora:
    if _client is None:
        raise RuntimeError("adapter not started")
    return _client


def chunk_stats() -> ChunkStats:
    if _chunks is None:
        raise RuntimeError("adapter not started")
    return _chunks


async def _list_knowledge(raw_kb: str) -> list[dict]:
    """Every document of a knowledge base, page by page."""

    path = f"/knowledge-bases/{segment(raw_kb, '知识库')}/knowledge"
    first = await wk().call("GET", path, params={"page": 1, "page_size": DOC_PAGE}) or {}
    docs = list(first.get("data") or [])
    pages = -(-int(first.get("total") or 0) // DOC_PAGE)
    rest = await asyncio.gather(*(wk().call("GET", path, params={"page": p, "page_size": DOC_PAGE})
                                  for p in range(2, pages + 1)))
    for body in rest:
        docs.extend((body or {}).get("data") or [])
    return docs


async def _count_chunks(raw_doc: str) -> int:
    """The total of a document's chunk list (text chunks, as the document's chunks page shows)."""

    body = await wk().call("GET", f"/chunks/{segment(raw_doc, '文档')}", params={"page": 1, "page_size": 1}) or {}
    return int(body.get("total") or 0)


def ids() -> Ids:
    return Ids(get_settings().id_prefix)


def authorized(request: Request) -> None:
    check_bearer(request, get_settings().adapter_key)


def create_app(client: WeKnora | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        global _client, _chunks
        _client = client or WeKnora.from_settings()
        _chunks = ChunkStats(_list_knowledge, _count_chunks, ttl_s=CHUNK_TTL_S)
        yield
        await _chunks.aclose()
        await _client.aclose()
        _client = _chunks = None

    app = FastAPI(title="weknora-adapter", version=__version__, lifespan=lifespan)
    install_error_handler(app)

    @app.get("/healthz")
    async def healthz() -> dict:
        if not await wk().health():
            raise ProviderError(503, "backend_unavailable", "WeKnora 不可用")
        return {"status": "ok"}

    app.include_router(_router())
    return app


def _router():
    from fastapi import APIRouter

    router = APIRouter(prefix="/v1", dependencies=[Depends(authorized)])

    @router.get("/capabilities")
    async def capabilities() -> dict:
        s = get_settings()
        return {
            "contract": CONTRACT, "role": "base",
            "provider": {"type": "weknora", "version": __version__, "label": s.label},
            "features": FEATURES, "file_types": FILE_TYPES,
            "limits": {"max_upload_mb": s.max_file_mb, "max_top_k": 50},
            "config_schema": mapping.CONFIG_SCHEMA,
        }

    # ---- knowledge bases ------------------------------------------------

    async def _count(kb_id: str, status: str | None) -> int:
        params: dict[str, Any] = {"page": 1, "page_size": 1}
        if status:
            params["parse_status"] = status
        body = await wk().call("GET", f"/knowledge-bases/{segment(kb_id, '知识库')}/knowledge", params=params)
        return int((body or {}).get("total") or 0)

    async def _kb(kb_id: str) -> dict:
        raw = await wk().data("GET", f"/knowledge-bases/{segment(kb_id, '知识库')}")
        if not raw:
            raise not_found("知识库")
        return raw

    async def _kb_view(raw: dict) -> dict:
        # starts a background refresh of the chunk total when it is due, waited for only briefly
        chunk_stats().peek(raw)
        # the document total comes from the document list: WeKnora's knowledge_count lags behind a
        # background delete by minutes
        documents, failed, cancelled, _ = await asyncio.gather(
            _count(raw["id"], None), _count(raw["id"], "failed"), _count(raw["id"], "cancelled"),
            chunk_stats().settle([raw["id"]], CHUNK_WAIT_S))
        return mapping.kb_view(raw, ids(), failed=failed, cancelled=cancelled, chunks=chunk_stats().peek(raw),
                               documents=documents)

    @router.get("/kbs")
    async def list_kbs() -> dict:
        listed = await wk().data("GET", "/knowledge-bases") or []
        details = await asyncio.gather(*(_kb(k["id"]) for k in listed if k.get("type", "document") == "document"))
        return {"items": list(await asyncio.gather(*(_kb_view(k) for k in details)))}

    @router.get("/kbs/{kb_id}")
    async def get_kb(kb_id: str) -> dict:
        return await _kb_view(await _kb(ids().inp(kb_id, "知识库")))

    @router.post("/kbs", status_code=201)
    async def create_kb(request: Request) -> dict:
        s = get_settings()
        body = await request.json()
        if not str(body.get("name") or "").strip():
            raise ProviderError(400, "bad_request", "知识库名称不能为空")
        if not s.embedding_model_id:
            raise ProviderError(500, "misconfigured", "适配器未配置 WEKNORA_EMBEDDING_MODEL_ID")
        created = await wk().data("POST", "/knowledge-bases", json=mapping.create_body(
            body, embedding_model_id=s.embedding_model_id, chat_model_id=s.chat_model_id))
        return await _kb_view(await _kb(created["id"]))

    @router.patch("/kbs/{kb_id}")
    async def patch_kb(kb_id: str, request: Request) -> dict:
        raw_id = ids().inp(kb_id, "知识库")
        current = await _kb(raw_id)
        body = mapping.update_body(current, await request.json(), chat_model_id=get_settings().chat_model_id)
        await wk().data("PUT", f"/knowledge-bases/{segment(raw_id)}", json=body)
        return await _kb_view(await _kb(raw_id))

    @router.delete("/kbs/{kb_id}", status_code=204)
    async def delete_kb(kb_id: str) -> Response:
        await wk().call("DELETE", f"/knowledge-bases/{segment(ids().inp(kb_id, '知识库'))}")
        return Response(status_code=204)

    # ---- documents --------------------------------------------------------

    @router.get("/kbs/{kb_id}/documents")
    async def list_docs(
        kb_id: str,
        status: str | None = None,
        q: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        path = f"/knowledge-bases/{segment(ids().inp(kb_id, '知识库'))}/knowledge"
        statuses: list[str | None] = mapping.upstream_values(mapping.PARSE_STATUS, status) if status else [None]
        if not statuses:
            return {"items": [], "total": 0}

        async def fetch(value: str | None, page: int, page_size: int) -> tuple[list[dict], int]:
            params: dict[str, Any] = {"page": page, "page_size": page_size}
            if q:
                params["keyword"] = q
            if value:
                params["parse_status"] = value
            body = await wk().call("GET", path, params=params) or {}
            return body.get("data") or [], int(body.get("total") or 0)

        # WeKnora lists newest first
        items, total = await window(fetch, statuses, offset, limit, key=lambda k: str(k.get("created_at") or ""),
                                    reverse=True)
        return {"items": [mapping.doc_view(k, ids(), chunk_stats().doc_count(k)) for k in items], "total": total}

    async def _doc(doc_id: str) -> dict:
        raw = await wk().data("GET", f"/knowledge/{segment(ids().inp(doc_id, '文档'))}")
        if not raw:
            raise not_found("文档")
        return raw

    @router.get("/documents/{doc_id}")
    async def get_doc(doc_id: str) -> dict:
        raw = await _doc(doc_id)
        return mapping.doc_view(raw, ids(), chunk_stats().doc_count(raw))

    async def _chunk_page(raw_id: str, page: int) -> dict:
        params = {"page": page, "page_size": CHUNK_PAGE}
        return await wk().call("GET", f"/chunks/{segment(raw_id)}", params=params) or {}

    @router.get("/documents/{doc_id}/chunks")
    async def get_chunks(doc_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)) -> dict:
        raw_id = ids().inp(doc_id, "文档")
        items: list[dict] = []
        total = 0
        for page, start, stop in page_span(offset, limit, CHUNK_PAGE):
            body = await _chunk_page(raw_id, page)
            total = int(body.get("total") or 0)
            items.extend((body.get("data") or [])[start:stop])
        return {"items": [mapping.chunk_view(c, ids()) for c in items], "total": total, "offset": offset}

    async def _all_chunks(raw_id: str) -> list[dict]:
        first = await _chunk_page(raw_id, 1)
        chunks = list(first.get("data") or [])
        pages = min(-(-int(first.get("total") or 0) // CHUNK_PAGE), MAX_L1_CHUNKS // CHUNK_PAGE)
        rest = await asyncio.gather(*(_chunk_page(raw_id, p) for p in range(2, pages + 1)))
        for body in rest:
            chunks.extend(body.get("data") or [])
        return chunks

    @router.get("/documents/{doc_id}/content")
    async def get_content(doc_id: str, level: str = Query("L1", pattern="^L[012]$")) -> PlainTextResponse:
        if level == "L2":
            raise ProviderError(501, "unsupported", "WeKnora 不提供 L2 解析数据")
        if level == "L0":
            return PlainTextResponse((await _doc(doc_id)).get("description") or "", media_type="text/markdown; charset=utf-8")
        return PlainTextResponse(await _l1(doc_id), media_type="text/markdown; charset=utf-8")

    async def _l1(doc_id: str) -> str:
        chunks = await _all_chunks(ids().inp(doc_id, "文档"))
        return await asyncio.to_thread(mapping.stitch, chunks)

    @router.get("/documents/{doc_id}/structure")
    async def get_structure(
        doc_id: str, offset: int = Query(0, ge=0), limit: int = Query(30000, ge=0, le=200000), heading: str | None = None,
    ) -> dict:
        return await reading.structure(await _l1(doc_id), doc_id, offset=offset, limit=limit, heading=heading)

    async def _kb_docs(raw_kb: str) -> list[dict]:
        body = await wk().call("GET", f"/knowledge-bases/{segment(raw_kb)}/knowledge",
                               params={"page": 1, "page_size": DOC_PAGE}) or {}
        return [mapping.doc_view(k, ids()) for k in body.get("data") or []]

    @router.post("/grep")
    async def grep(request: Request) -> dict:
        body = await request.json()
        wanted = [ids().inp(k, "知识库") for k in body.get("kb_ids") or []]
        if not wanted:
            wanted = [k["id"] for k in await wk().data("GET", "/knowledge-bases") or [] if k.get("type", "document") == "document"]
        docs = [d for kb in wanted for d in await _kb_docs(kb)]
        return await reading.grep(docs, lambda d: _l1(d["id"]), str(body.get("pattern") or ""),
                                  max_results=int(body.get("max_results") or 50),
                                  max_lines_per_doc=int(body.get("max_lines_per_doc") or 10))

    @router.get("/documents/{doc_id}/file")
    async def get_file(doc_id: str) -> StreamingResponse:
        upstream = await wk().send("GET", f"/knowledge/{segment(ids().inp(doc_id, '文档'))}/download", stream=True)
        # the body is relayed decoded, so a compressed upstream's content-length would be wrong
        passed = {"content-disposition"} if "content-encoding" in upstream.headers else {"content-disposition", "content-length"}
        headers = {k.lower(): v for k, v in upstream.headers.items() if k.lower() in passed}
        if "content-disposition" in headers:
            headers["content-disposition"] = mapping.display_name(headers["content-disposition"])

        async def body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(body(), media_type=upstream.headers.get("content-type"), headers=headers)

    @router.post("/kbs/{kb_id}/documents", status_code=201)
    async def upload(
        kb_id: str, file: UploadFile = File(...), folder_path: str = Form(""), process_overrides: str = Form(""),
    ) -> dict:
        return await _ingest_file(ids().inp(kb_id, "知识库"), file.filename or "upload", await file.read(), folder_path,
                                  file.content_type or "application/octet-stream")

    async def _ingest_file(raw_kb: str, name: str, data: bytes, folder_path: str, content_type: str) -> dict:
        # WeKnora reads UTF-16 without a byte order mark byte by byte and marks the result parsed
        data = await asyncio.to_thread(textfiles.to_utf8, name, data)
        as_text = get_settings().json_as_text and textfiles.file_type(name) == "json"
        name = mapping.upload_name(name, as_text=as_text)
        if as_text:
            content_type = "text/plain; charset=utf-8"
        display = f"{folder_path.strip('/')}/{name}" if folder_path.strip("/") else name
        doc = await wk().data(
            "POST", f"/knowledge-bases/{segment(raw_kb)}/knowledge/file",
            files={"file": (name, data, content_type)}, data={"fileName": display, "channel": "api"}, timeout=600,
        )
        return mapping.doc_view(doc, ids())

    def _uploads() -> UploadStore:
        return UploadStore(get_settings().upload_dir, ids())

    @router.post("/uploads", status_code=201)
    async def upload_begin(request: Request) -> dict:
        body = await request.json()
        ids().inp(str(body.get("kb_id") or ""), "知识库")
        return await asyncio.to_thread(_uploads().begin, body, get_settings().max_file_mb * 1024 * 1024)

    @router.put("/uploads/{upload_id}/parts/{index}")
    async def upload_part(upload_id: str, index: int, request: Request) -> dict:
        return await asyncio.to_thread(_uploads().put, upload_id, index, await request.body())

    @router.post("/uploads/{upload_id}/complete", status_code=201)
    async def upload_complete(upload_id: str) -> dict:
        store = _uploads()
        meta, data = await asyncio.to_thread(store.assemble, upload_id)
        doc = await _ingest_file(ids().inp(meta["kb_id"], "知识库"), meta["file_name"], data, meta["folder_path"],
                                 "application/octet-stream")
        await asyncio.to_thread(store.drop, upload_id)
        return doc

    @router.post("/kbs/{kb_id}/documents/text", status_code=201)
    async def text_doc(kb_id: str, request: Request) -> dict:
        body = await request.json()
        text = str(body.get("text") or "")
        if not text.strip():
            raise ProviderError(400, "bad_request", "正文不能为空")
        title = str(body.get("title") or "").strip() or text.strip().split("\n", 1)[0][:60]
        # a title over WeKnora's column would only come back as a raw database error
        title = textfiles.fit_text(title, max_chars=mapping.MAX_TITLE_CHARS)
        doc = await wk().data("POST", f"/knowledge-bases/{segment(ids().inp(kb_id, '知识库'))}/knowledge/manual",
                              json={"title": title, "content": text, "status": "publish", "channel": "api"})
        return mapping.doc_view(doc, ids())

    @router.post("/kbs/{kb_id}/documents/url", status_code=201)
    async def url_doc(kb_id: str, request: Request) -> dict:
        body = await request.json()
        try:
            doc = await wk().data("POST", f"/knowledge-bases/{segment(ids().inp(kb_id, '知识库'))}/knowledge/url",
                                  json={"url": str(body.get("url") or "").strip(), "channel": "api"})
        except ProviderError as exc:
            raise mapping.url_import_error(exc) from exc
        return mapping.doc_view(doc, ids())

    @router.delete("/documents/{doc_id}", status_code=204)
    async def delete_doc(doc_id: str) -> Response:
        await wk().call("DELETE", f"/knowledge/{segment(ids().inp(doc_id, '文档'))}")
        return Response(status_code=204)

    @router.post("/documents/{doc_id}/reprocess")
    async def reprocess(doc_id: str) -> dict:
        await wk().call("POST", f"/knowledge/{segment(ids().inp(doc_id, '文档'))}/reparse", json={})
        return mapping.doc_view(await _doc(doc_id), ids())

    @router.post("/documents/{doc_id}/cancel")
    async def cancel(doc_id: str) -> dict:
        await wk().call("POST", f"/knowledge/{segment(ids().inp(doc_id, '文档'))}/cancel-parse")
        return mapping.doc_view(await _doc(doc_id), ids())

    # ---- search ------------------------------------------------------------

    @router.post("/search")
    async def search(request: Request) -> dict:
        body = await request.json()
        query = str(body.get("query") or "").strip()
        if not query:
            raise ProviderError(400, "bad_request", "query 不能为空")
        top_k = max(1, min(int(body.get("top_k") or 8), 50))
        kbs = await wk().data("GET", "/knowledge-bases") or []
        by_id = {k["id"]: k for k in kbs if k.get("type", "document") == "document"}
        wanted = [ids().inp(k, "知识库") for k in body.get("kb_ids") or []] or list(by_id)
        wanted = [k for k in wanted if k in by_id]
        if not wanted:
            return {"query": query, "items": []}
        results = await wk().data("POST", "/knowledge-search",
                                  json={"query": query, "knowledge_base_ids": wanted, "match_count": top_k}) or []
        # WeKnora's score mixes vector, keyword and rerank scales: it is not a 0-1 relevance,
        # so relevance stays null and min_score (defined on relevance) is not applied
        hits = sorted((mapping.hit_view(r, ids()) for r in results), key=lambda h: -h["score"])
        terms = postprocess.identifier_terms(query)
        if terms:
            hits = await _with_exact(wanted, terms, hits)
        wiki_kbs = [k for k in wanted if (by_id[k].get("indexing_strategy") or {}).get("wiki_enabled")]
        pages: list[dict] = []
        if body.get("include_wiki", True) and wiki_kbs:
            pages = await wk().data("POST", "/wiki-search",
                                    json={"query": query, "knowledge_base_ids": wiki_kbs, "limit": 3}) or []
        if pages:
            items = mapping.fuse(hits, [mapping.wiki_hit_view(p, ids()) for p in pages], top_k)
        else:
            items = hits[:top_k]
        return {"query": query, "items": items}

    async def _with_exact(wanted: list[str], terms: list[str], hits: list[dict]) -> list[dict]:
        """Put passages that contain an identifier of the query (E-0x1F3A, v3.2.1-rc4) first.

        WeKnora's keyword leg rejects such tokens ("Unsupported query shape") and the vector
        leg does not match them literally. The parsed text of the knowledge bases' documents
        is scanned for the identifier (at most EXACT_MAX_DOCS documents, EXACT_BUDGET_S seconds;
        documents not read in time are left out).
        """

        lowered = [t.lower() for t in terms]
        containing = [h for h in hits if any(t in (h.get("content") or "").lower() for t in lowered)]
        covered = {h["doc_id"] for h in containing}
        gate = asyncio.Semaphore(EXACT_CONCURRENCY)

        async def scan(doc: dict) -> dict | None:
            async with gate:
                text = await _l1(doc["id"])
            low = text.lower()
            pos = min((p for p in (low.find(t) for t in lowered) if p >= 0), default=-1)
            return None if pos < 0 else mapping.exact_hit(doc, text, pos)

        docs: list[dict] = []
        try:
            for kb in wanted:
                docs.extend(d for d in await _kb_docs(kb)
                            if d.get("parse_status") == "completed" and d["id"] not in covered)
        except ProviderError as exc:
            logger.warning("exact identifier scan skipped: %s", exc)
            docs = []
        docs = docs[:EXACT_MAX_DOCS]
        tasks = [asyncio.create_task(scan(d)) for d in docs]
        extra: list[dict] = []
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=EXACT_BUDGET_S)
            for task in pending:
                task.cancel()
            if pending:
                logger.warning("exact identifier scan: %d of %d documents not read in time", len(pending), len(tasks))
            # documents keep their listing order, so the result does not depend on read timing
            extra = [t.result() for t in tasks if t in done and not t.exception() and t.result() is not None]
        for h in containing:
            h["matched_by"] = ["exact"]
        rest = [h for h in hits if h not in containing]
        # the exact matches go first: give them the top score, so the list still reads best-first
        top = max((float(h.get("score") or 0.0) for h in hits), default=1.0) or 1.0
        for h in containing + extra:
            h["score"] = top
        return containing + extra + rest

    # ---- wiki ----------------------------------------------------------------

    async def _wiki(raw_kb: str, path: str, **kwargs: Any) -> Any:
        """``path`` is already made of safe segments."""

        try:
            return await wk().call("GET", f"/knowledgebase/{segment(raw_kb, '知识库')}/wiki/{path}", **kwargs)
        except ProviderError as exc:
            # WeKnora answers 400 when the knowledge base has no wiki
            if exc.status == 400:
                raise WikiDisabled() from exc
            raise

    @router.get("/kbs/{kb_id}/wiki/status")
    async def wiki_status(kb_id: str) -> dict:
        raw_kb = ids().inp(kb_id, "知识库")
        try:
            stats = await _wiki(raw_kb, "stats")
        except WikiDisabled:
            return {"enabled": False, "pages": {}, "updated_at": None, "documents": {}, "pending_jobs": 0, "errors": []}
        pages: dict[str, int] = {}
        for page_type, n in (stats.get("pages_by_type") or {}).items():
            key = mapping.WIKI_TYPE.get(page_type, "concept")
            pages[key] = pages.get(key, 0) + int(n)
        return {"enabled": True, "pages": pages, "updated_at": None, "documents": {},
                "pending_jobs": int(stats.get("pending_tasks") or 0), "errors": []}

    @router.get("/kbs/{kb_id}/wiki/pages")
    async def wiki_pages(
        kb_id: str, type: str | None = None, q: str | None = None,
        limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    ) -> dict:
        raw_kb = ids().inp(kb_id, "知识库")
        types: list[str | None] = mapping.upstream_values(mapping.WIKI_TYPE, type) if type else [None]
        if not types:
            return {"items": [], "total": 0}

        async def fetch(value: str | None, page: int, page_size: int) -> tuple[list[dict], int]:
            params: dict[str, Any] = {"page": page, "page_size": page_size, "sort_by": "title", "sort_order": "asc"}
            if value:
                params["page_type"] = value
            if q:
                params["query"] = q
            body = await _wiki(raw_kb, "pages", params=params) or {}
            return body.get("pages") or [], int(body.get("total") or 0)

        try:
            pages, total = await window(fetch, types, offset, limit, key=lambda p: str(p.get("title") or ""),
                                        reverse=False)
        except WikiDisabled:
            # the knowledge-base page shows its own "Wiki is off" state from wiki/status
            return {"items": [], "total": 0}
        return {"items": [mapping.wiki_item(p, ids()) for p in pages], "total": total}

    @router.get("/kbs/{kb_id}/wiki/page")
    async def wiki_page(kb_id: str, slug: str) -> dict:
        # slugs may hold '/' (entity/pump); each part is encoded and '..' is refused
        page = await _wiki(ids().inp(kb_id, "知识库"), "pages/" + segments(slug, "Wiki 页面"))
        return mapping.wiki_page(page, ids())

    @router.get("/kbs/{kb_id}/wiki/graph")
    async def wiki_graph(kb_id: str, center: str | None = None, limit: int = Query(300, ge=1, le=2000)) -> dict:
        params: dict[str, Any] = {"limit": limit, "mode": "ego" if center else "overview"}
        if center:
            params["center"] = center
        try:
            body = await _wiki(ids().inp(kb_id, "知识库"), "graph", params=params) or {}
        except WikiDisabled:
            body = {}
        nodes = [{"slug": n.get("slug"), "title": n.get("title"), "page_type": mapping.WIKI_TYPE.get(n.get("page_type") or "", "concept"),
                  "degree": int(n.get("link_count") or 0)} for n in body.get("nodes") or []]
        return {"nodes": nodes, "edges": [{"source": e.get("source"), "target": e.get("target")} for e in body.get("edges") or []]}

    return router
