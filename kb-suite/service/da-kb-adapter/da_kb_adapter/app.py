"""knowledge-provider/v1 routes backed by the DeepAnalyze knowledge pipeline.

The DA backend runs with ``DA_AUTH_MODE=none`` on a network only this adapter
joins, so requests to it carry no credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from functools import lru_cache
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from kb_provider_kit import CONTRACT, Ids, ProviderError, check_bearer, install_error_handler, not_found, segment
from kb_provider_kit import reading
from kb_provider_kit.uploads import UploadStore

from . import __version__, mapping

SUMMARY_CONCURRENCY = 8
FILE_TYPES = ["txt", "md", "markdown", "html", "htm", "xlsx", "xls", "xlsm", "csv", "json", "xml", "yaml", "yml"]
# no content.L2: DA's L2 is its full-text markdown page (or docling's raw JSON only for some
# documents), not the contract's JSON locator data
FEATURES = [
    "kbs.write", "ingest.file", "ingest.text", "documents.delete", "documents.reprocess", "documents.file",
    "content.structure", "grep", "uploads",
]
_SAFE_NAME = re.compile(r"[^\w一-鿿.-]+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DA_KB_")

    # address of the DA backend (the da-kb compose service, port 21000)
    base_url: str = ""
    timeout_s: float = 120.0
    max_file_mb: int = 100
    # with docling or a MinerU service behind DA, pdf/docx/pptx parse too
    extra_file_types: str = ""
    adapter_key: str = ""
    id_prefix: str = "da_"
    label: str = "DA 知识管线"
    upload_dir: str = "/tmp/da-kb-adapter-uploads"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def ids() -> Ids:
    return Ids(get_settings().id_prefix)


class DaKb:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        # doc id -> kb id; DA has no "get document" route, documents are listed per knowledge base
        self.doc_kb: dict[str, str] = {}
        # (doc id, updated_at) -> L0; DA lists documents without their summaries
        self._summaries: dict[tuple[str, str], str | None] = {}
        self._gate = asyncio.Semaphore(SUMMARY_CONCURRENCY)

    @classmethod
    def from_settings(cls) -> "DaKb":
        s = get_settings()
        return cls(httpx.AsyncClient(base_url=s.base_url.rstrip("/"), timeout=s.timeout_s, trust_env=False))

    async def send(self, method: str, path: str, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        request = self.client.build_request(method, f"/api/knowledge{path}", **kwargs)
        try:
            response = await self.client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise ProviderError(502, "backend_unavailable", f"DA 知识管线不可用：{exc}") from exc
        if response.status_code >= 400:
            if stream:
                await response.aread()
                await response.aclose()
            try:
                body = response.json()
                message = str(body.get("error") or "") + (f"（{body['detail']}）" if body.get("detail") else "")
            except (ValueError, AttributeError):
                message = response.text[:300]
            status = response.status_code if response.status_code < 500 else 502
            raise ProviderError(status, "not_found" if status == 404 else "backend_error", f"DA 知识管线：{message}")
        return response

    async def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.send(method, path, **kwargs)
        return response.json() if response.content else None

    async def kbs(self) -> list[dict]:
        return (await self.json("GET", "/kbs") or {}).get("knowledgeBases") or []

    async def documents(self, kb_id: str) -> list[dict]:
        docs = (await self.json("GET", f"/kbs/{segment(kb_id, '知识库')}/documents") or {}).get("documents") or []
        for doc in docs:
            self.doc_kb[doc["id"]] = kb_id
        return docs

    async def _scan(self, doc_id: str) -> tuple[str, dict]:
        for kb in await self.kbs():
            for doc in await self.documents(kb["id"]):
                if doc["id"] == doc_id:
                    return kb["id"], doc
        raise not_found("文档")

    async def locate(self, doc_id: str) -> tuple[str, dict]:
        """(kb id, document row) for a raw doc id; lists the document's knowledge base once."""

        kb_id = self.doc_kb.get(doc_id)
        if kb_id:
            for doc in await self.documents(kb_id):
                if doc["id"] == doc_id:
                    return kb_id, doc
            self.doc_kb.pop(doc_id, None)
            raise not_found("文档")
        return await self._scan(doc_id)

    async def kb_of(self, doc_id: str) -> str:
        """The knowledge base of a raw doc id, checked with DA's single-document status route.

        Readers that only need the knowledge base use this instead of ``locate``,
        which would list the whole knowledge base on every call.
        """

        kb_id = self.doc_kb.get(doc_id)
        if not kb_id:
            return (await self._scan(doc_id))[0]
        try:
            await self.json("GET", f"/kbs/{segment(kb_id, '知识库')}/documents/{segment(doc_id, '文档')}/status")
        except ProviderError as exc:
            if exc.status == 404:
                # DA documents never move between knowledge bases: gone
                self.doc_kb.pop(doc_id, None)
                raise not_found("文档") from exc
            raise
        return kb_id

    async def expand(self, kb_id: str, doc_id: str, level: str) -> dict | None:
        try:
            return await self.json("POST", f"/{segment(kb_id, '知识库')}/expand",
                                   json={"docId": doc_id, "level": level, "format": "md"})
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise

    async def page(self, kb_id: str, page_id: str, level: str | None = None) -> dict:
        return await self.json("GET", f"/kbs/{segment(kb_id, '知识库')}/pages/{segment(page_id, '页面')}",
                               params={"level": level} if level else None)

    async def summary(self, kb_id: str, doc_id: str, version: str = "") -> str | None:
        key = (doc_id, version)
        if version and key in self._summaries:
            return self._summaries[key]
        async with self._gate:
            found = await self.expand(kb_id, doc_id, "L0")
        text = (found or {}).get("content") or None
        if version:
            self._summaries[key] = text
        return text

    async def summaries(self, kb_id: str, docs: list[dict]) -> dict[str, str | None]:
        ready = [d for d in docs if d.get("status") == "ready"]
        texts = await asyncio.gather(*(self.summary(kb_id, d["id"], str(d.get("updated_at") or "")) for d in ready))
        return {d["id"]: t for d, t in zip(ready, texts)}

    async def fulltext(self, kb_id: str, doc_id: str) -> str:
        """DA's ``fulltext`` page (expand may hand back docling's raw JSON instead, so it is read as a page)."""

        found = await self.expand(kb_id, doc_id, "L1") or await self.expand(kb_id, doc_id, "L0")
        if not found:
            return ""
        page = await self.page(kb_id, found["pageId"], "L2")
        # pages/:id silently returns the page itself when the level is missing
        return (page.get("content") or "") if page.get("level") == "L2" else ""

    async def l1(self, kb_id: str, doc_id: str) -> str:
        """The whole document as markdown: DA's structure page, else its full text.

        Without docling DA's L1 is an LLM-written overview; the contract's L1 is the
        full text, which DA keeps as L2 (``fulltext``) in that case.
        """

        found = await self.expand(kb_id, doc_id, "L1")
        if found:
            page = await self.page(kb_id, found["pageId"])
            if page.get("pageType") in mapping.L1_STRUCTURE:
                return found.get("content") or ""
        return await self.fulltext(kb_id, doc_id) or (found or {}).get("content") or ""

    async def health(self) -> bool:
        try:
            response = await self.client.get("/api/health", timeout=5)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def aclose(self) -> None:
        await self.client.aclose()


_da: DaKb | None = None


def da() -> DaKb:
    if _da is None:
        raise RuntimeError("adapter not started")
    return _da


def authorized(request: Request) -> None:
    check_bearer(request, get_settings().adapter_key)


def create_app(backend: DaKb | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        global _da
        _da = backend or DaKb.from_settings()
        yield
        await _da.aclose()
        _da = None

    app = FastAPI(title="da-kb-adapter", version=__version__, lifespan=lifespan)
    install_error_handler(app)

    @app.get("/healthz")
    async def healthz() -> dict:
        if not await da().health():
            raise ProviderError(503, "backend_unavailable", "DA 知识管线不可用")
        return {"status": "ok"}

    app.include_router(_router())
    return app


def _router() -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(authorized)])

    @router.get("/capabilities")
    async def capabilities() -> dict:
        s = get_settings()
        extra = [t.strip() for t in s.extra_file_types.split(",") if t.strip()]
        return {
            "contract": CONTRACT, "role": "base",
            "provider": {"type": "deepanalyze", "version": __version__, "label": s.label},
            "features": FEATURES, "file_types": FILE_TYPES + extra,
            "limits": {"max_upload_mb": s.max_file_mb, "max_top_k": 50},
            "config_schema": None,
        }

    # ---- knowledge bases ------------------------------------------------

    async def _kb_view(kb: dict) -> dict:
        return mapping.kb_view(kb, await da().documents(kb["id"]), ids())

    @router.get("/kbs")
    async def list_kbs() -> dict:
        return {"items": list(await asyncio.gather(*(_kb_view(kb) for kb in await da().kbs())))}

    @router.get("/kbs/{kb_id}")
    async def get_kb(kb_id: str) -> dict:
        return await _kb_view(await da().json("GET", f"/kbs/{segment(ids().inp(kb_id, '知识库'))}"))

    @router.post("/kbs", status_code=201)
    async def create_kb(request: Request) -> dict:
        body = await request.json()
        name = str(body.get("name") or "").strip()
        if not name:
            raise ProviderError(400, "bad_request", "知识库名称不能为空")
        kb = await da().json("POST", "/kbs", json={"name": name, "description": body.get("description") or ""})
        return mapping.kb_view(kb, [], ids())

    @router.patch("/kbs/{kb_id}")
    async def patch_kb(kb_id: str, request: Request) -> dict:
        body = await request.json()
        patch = {k: body[k] for k in ("name", "description") if k in body}
        kb = await da().json("PUT", f"/kbs/{segment(ids().inp(kb_id, '知识库'))}", json=patch)
        return await _kb_view(kb)

    @router.delete("/kbs/{kb_id}", status_code=204)
    async def delete_kb(kb_id: str) -> Response:
        await da().json("DELETE", f"/kbs/{segment(ids().inp(kb_id, '知识库'))}")
        return Response(status_code=204)

    # ---- documents --------------------------------------------------------

    @router.get("/kbs/{kb_id}/documents")
    async def list_docs(
        kb_id: str, status: str | None = None, q: str | None = None,
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    ) -> dict:
        raw_kb = ids().inp(kb_id, "知识库")
        docs = await da().documents(raw_kb)
        if status:
            docs = [d for d in docs if mapping.doc_status(d) == status]
        if q:
            needle = q.lower()
            docs = [d for d in docs if needle in (d.get("filename") or "").lower()]
        window = docs[offset:offset + limit]
        summaries = await da().summaries(raw_kb, window)
        items = [mapping.doc_view(d, raw_kb, ids(), summary=summaries.get(d["id"])) for d in window]
        return {"items": items, "total": len(docs)}

    @router.get("/documents/{doc_id}")
    async def get_doc(doc_id: str) -> dict:
        raw = ids().inp(doc_id, "文档")
        kb_id, doc = await da().locate(raw)
        summary = await da().summary(kb_id, raw, str(doc.get("updated_at") or "")) if doc.get("status") == "ready" else None
        return mapping.doc_view(doc, kb_id, ids(), summary=summary)

    @router.get("/documents/{doc_id}/content")
    async def get_content(doc_id: str, level: str = Query("L1", pattern="^L[012]$")) -> PlainTextResponse:
        if level == "L2":
            raise ProviderError(501, "unsupported", "DA 知识管线不提供 L2 解析数据")
        raw = ids().inp(doc_id, "文档")
        kb_id = await da().kb_of(raw)
        if level == "L0":
            text = await da().summary(kb_id, raw) or ""
        else:
            text = await da().l1(kb_id, raw)
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    async def _l1_of(doc_id: str) -> str:
        raw = ids().inp(doc_id, "文档")
        return await da().l1(await da().kb_of(raw), raw)

    @router.get("/documents/{doc_id}/structure")
    async def get_structure(
        doc_id: str, offset: int = Query(0, ge=0), limit: int = Query(30000, ge=0, le=200000), heading: str | None = None,
    ) -> dict:
        return await reading.structure(await _l1_of(doc_id), doc_id, offset=offset, limit=limit, heading=heading)

    @router.post("/grep")
    async def grep(request: Request) -> dict:
        body = await request.json()
        wanted = [ids().inp(k, "知识库") for k in body.get("kb_ids") or []] or [kb["id"] for kb in await da().kbs()]
        docs = [mapping.doc_view(d, kb, ids()) for kb in wanted for d in await da().documents(kb)]

        async def read_l1(doc: dict) -> str:
            # the listing above already gave each document's knowledge base
            return await da().l1(ids().inp(doc["kb_id"]), ids().inp(doc["id"]))

        return await reading.grep(docs, read_l1, str(body.get("pattern") or ""),
                                  max_results=int(body.get("max_results") or 50),
                                  max_lines_per_doc=int(body.get("max_lines_per_doc") or 10))

    @router.get("/documents/{doc_id}/chunks")
    async def get_chunks(doc_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)) -> dict:
        raw = ids().inp(doc_id, "文档")
        # every page re-reads the whole L1 (no cache across requests)
        pieces = await asyncio.to_thread(mapping.split_chunks, await da().l1(await da().kb_of(raw), raw))
        items = [mapping.chunk_view(raw, seq, line, text, ids())
                 for seq, (line, _, text) in enumerate(pieces)][offset:offset + limit]
        return {"items": items, "total": len(pieces), "offset": offset}

    @router.get("/documents/{doc_id}/file")
    async def get_file(doc_id: str) -> StreamingResponse:
        raw = ids().inp(doc_id, "文档")
        kb_id = await da().kb_of(raw)
        upstream = await da().send("GET", f"/kbs/{segment(kb_id)}/documents/{segment(raw)}/download", stream=True)
        # the body is relayed decoded, so a compressed upstream's content-length would be wrong
        passed = {"content-disposition"} if "content-encoding" in upstream.headers else {"content-disposition", "content-length"}
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in passed}

        async def body():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(body(), media_type=upstream.headers.get("content-type"), headers=headers)

    async def _upload(raw_kb: str, name: str, data: bytes, folder_path: str, content_type: str) -> dict:
        form = {"folderPath": folder_path} if folder_path.strip("/") else {}
        created = await da().json("POST", f"/kbs/{segment(raw_kb, '知识库')}/upload",
                                  files={"file": (name, data, content_type)}, data=form, timeout=600)
        da().doc_kb[created["id"]] = raw_kb
        row = {"id": created["id"], "filename": created.get("filename") or name, "file_type": created.get("fileType"),
               "file_size": created.get("fileSize"), "folder_path": created.get("folderPath") or "",
               "status": created.get("status") or "uploaded", "created_at": created.get("createdAt")}
        return mapping.doc_view(row, raw_kb, ids())

    @router.post("/kbs/{kb_id}/documents", status_code=201)
    async def upload(
        kb_id: str, file: UploadFile = File(...), folder_path: str = Form(""), process_overrides: str = Form(""),
    ) -> dict:
        data = await file.read()
        return await _upload(ids().inp(kb_id, "知识库"), file.filename or "upload", data, folder_path,
                             file.content_type or "application/octet-stream")

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
        doc = await _upload(ids().inp(meta["kb_id"], "知识库"), meta["file_name"], data, meta["folder_path"],
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
        name = (_SAFE_NAME.sub("_", title).strip("._") or "text") + ".md"
        return await _upload(ids().inp(kb_id, "知识库"), name, text.encode("utf-8"), str(body.get("folder_path") or ""),
                             "text/markdown")

    @router.delete("/documents/{doc_id}", status_code=204)
    async def delete_doc(doc_id: str) -> Response:
        raw = ids().inp(doc_id, "文档")
        kb_id = await da().kb_of(raw)
        await da().json("DELETE", f"/kbs/{segment(kb_id)}/documents/{segment(raw)}")
        da().doc_kb.pop(raw, None)
        return Response(status_code=204)

    @router.post("/documents/{doc_id}/reprocess")
    async def reprocess(doc_id: str) -> dict:
        raw = ids().inp(doc_id, "文档")
        kb_id, doc = await da().locate(raw)
        await da().json("POST", f"/kbs/{segment(kb_id)}/process/{segment(raw)}", params={"force": "true"})
        return mapping.doc_view({**doc, "status": "uploaded"}, kb_id, ids())

    # ---- search ------------------------------------------------------------

    @router.post("/search")
    async def search(request: Request) -> dict:
        body = await request.json()
        query = str(body.get("query") or "").strip()
        if not query:
            raise ProviderError(400, "bad_request", "query 不能为空")
        top_k = max(1, min(int(body.get("top_k") or 8), 50))
        kb_ids = [ids().inp(k, "知识库") for k in body.get("kb_ids") or []]
        params: dict[str, Any] = {"query": query, "topK": top_k * 3}
        if kb_ids:
            params["kbIds"] = ",".join(kb_ids)
        results = (await da().json("GET", "/search", params=params) or {}).get("results") or []
        # the scope is the gateway's to enforce; do not rely on DA honouring kbIds
        results = [r for r in results if r.get("docId") and (not kb_ids or r.get("kbId") in kb_ids)]
        filenames: dict[str, str] = {}
        for kb_id in {r["kbId"] for r in results}:
            for doc in await da().documents(kb_id):
                filenames[doc["id"]] = doc.get("filename") or ""
        # DA's score is a rank-fusion score, not a 0-1 relevance: relevance stays null and
        # min_score (defined on relevance) is not applied
        items = [mapping.hit_view(r, ids(), filenames) for r in results][:top_k]
        return {"query": query, "items": items}

    return router
