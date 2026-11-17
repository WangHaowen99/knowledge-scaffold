"""knowledge-provider/v1 routes backed by the DeepAnalyze knowledge pipeline.

The DA backend runs with ``DA_AUTH_MODE=none`` on a network only this adapter
joins, so requests to it carry no credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import httpx
from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from kb_provider_kit import CONTRACT, Ids, ProviderError, check_bearer, install_error_handler, not_found, segment
from kb_provider_kit import reading, textfiles
from kb_provider_kit.uploads import UploadStore

from . import __version__, backend_settings, jobs, mapping, offering
from .offering import EXPORTS, PREVIEW, REPORTS, SETTINGS

SUMMARY_CONCURRENCY = 8
# no content.L2: DA's L2 is its full-text markdown page (or docling's raw JSON only for some
# documents), not the contract's JSON locator data
FEATURES = [
    "kbs.write", "ingest.file", "ingest.text", "documents.delete", "documents.reprocess", "documents.file",
    "content.structure", "grep", "uploads",
]
# wiki.read and the contract section 9 features follow the routes the backend serves (see ``offering``)
_SAFE_NAME = re.compile(r"[^\w一-鿿.-]+")
# The platform keeps the full name and stores the file under a short name of its own (since the
# DeepAnalyze change of 2026-11-11; older platform builds answered a bare 500 for names over ~206 bytes):
# names are only kept to a sane length, the same as the WeKnora adapter
MAX_NAME_CHARS = 255
# each folder level becomes a directory under DA's data dir
MAX_FOLDER_BYTES = 255

# DA answers these only when the work is done (LLM calls, re-embedding)
ACTION_TIMEOUT_S = 600.0
REQUEST_JOB_TIMEOUT_S = 6 * 3600.0
SCHEMA_TTL_S = 60.0
SCHEMA_TIMEOUT_S = 5.0
# which optional DA routes the backend serves is checked again after this long
ROUTES_TTL_S = 120.0
PROBE_TIMEOUT_S = 5.0
# entity pages and links are listed through DA's graph route, which walks the whole knowledge base
GRAPH_TTL_S = 30.0
MAX_GRAPH_NODES = 20000
MAX_JOBS = 500
MAX_LISTED_JOBS = 50
MAX_LINKED_PAGES = 50
# per-document caches (anchors placed in the L1 text, items of the parser's JSON)
MAX_CACHED_DOCS = 128
# keyword hits come without text (DA's full-text search keeps none): the page is read for these many
MAX_FILLED_HITS = 40
SUMMARY_RESULT = {"regenerated": "已重新生成", "skipped_has_content": "已有摘要", "skipped_not_ready": "未处理完"}
_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$")


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
        # doc id -> size of the original file, for byte ranges (a document's file never changes)
        self.sizes: dict[str, int] = {}
        # raw job id -> what this process started (see ``jobs``); lost on restart by design
        self.jobs: dict[str, dict] = {}
        self._schema: tuple[float, dict] | None = None
        # (when, probe -> served) of the optional DA routes; see ``offering``
        self._routes: tuple[float, dict[str, bool]] | None = None
        # kb id -> (when, DA graph): entity pages and links, shared by the wiki routes for a short while
        self._graphs: dict[str, tuple[float, dict]] = {}
        # (when, parser signals) for the file types; see ``offering.file_types``
        self._parsers: tuple[float, set[str]] | None = None
        # (doc id, hash of the text) -> DA's anchors placed in that text
        self._located: dict[tuple[str, int], list[tuple[int, str, dict]]] = {}
        # (doc id, version) -> text items with page and box from DA's raw parser JSON
        self._items: dict[tuple[str, str], list[dict]] = {}

    @classmethod
    def from_settings(cls) -> "DaKb":
        s = get_settings()
        return cls(httpx.AsyncClient(base_url=s.base_url.rstrip("/"), timeout=s.timeout_s, trust_env=False))

    async def send(
        self, method: str, path: str, *, stream: bool = False, api: str = "/api/knowledge", **kwargs: Any,
    ) -> httpx.Response:
        request = self.client.build_request(method, f"{api}{path}", **kwargs)
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
            code = {400: "bad_request", 404: "not_found", 409: "conflict"}.get(status, "backend_error")
            raise ProviderError(status, code, f"DA 知识管线：{message}")
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

    async def page(self, kb_id: str, page_id: str, level: str | None = None, page_type: str | None = None) -> dict:
        params = {k: v for k, v in (("level", level), ("pageType", page_type)) if v}
        return await self.json("GET", f"/kbs/{segment(kb_id, '知识库')}/pages/{segment(page_id, '页面')}",
                               params=params or None)

    def forget_summaries(self, doc_ids: set[str]) -> None:
        """Drop cached L0 texts; regenerating an abstract does not change the document's ``updated_at``."""

        for key in [k for k in self._summaries if k[0] in doc_ids]:
            self._summaries.pop(key, None)

    async def doc_page_id(self, kb_id: str, doc_id: str) -> str | None:
        """Any of the document's DA pages, to reach its siblings by page type."""

        for level in ("L0", "L1"):
            found = await self.expand(kb_id, doc_id, level)
            if found and found.get("pageId"):
                return found["pageId"]
        return None

    async def overview(self, kb_id: str, doc_id: str, page_id: str | None = None) -> dict | None:
        """DA's LLM overview page of a document (plain documents only; structured ones have none).

        ``page_id`` is any page of the document when the caller already has one.
        """

        page_id = page_id or await self.doc_page_id(kb_id, doc_id)
        if not page_id:
            return None
        # without an overview DA hands back a structure page or the page itself
        page = await self.page(kb_id, page_id, page_type="overview")
        return page if page.get("pageType") == "overview" else None

    async def _probe(self, method: str, api: str, path: str) -> bool | None:
        try:
            response = await self.client.request(method, f"{api}{path}", timeout=PROBE_TIMEOUT_S)
        except httpx.HTTPError:
            return None
        return offering.served(response.status_code, response.headers.get("content-type", ""), response.text)

    async def routes(self) -> dict[str, bool]:
        """Which optional DA routes the backend serves, probed again every ``ROUTES_TTL_S``.

        A probe that gets no clear answer keeps the last known result; without
        one the route counts as missing and the next call probes again.
        """

        now = time.monotonic()
        if self._routes and now - self._routes[0] < ROUTES_TTL_S:
            return self._routes[1]
        answers = await asyncio.gather(*(self._probe(*spec) for spec in offering.PROBES.values()))
        previous = self._routes[1] if self._routes else {}
        found = {name: previous.get(name, False) if answer is None else answer
                 for name, answer in zip(offering.PROBES, answers, strict=True)}
        if self._routes or None not in answers:
            self._routes = (now, found)
        return found

    async def offer(self) -> dict:
        return offering.offer(await self.routes())

    async def parsers(self, found_routes: dict[str, bool]) -> set[str]:
        """Parser signals DA reports reliably (MinerU's health, the vision and transcription roles).

        ``found_routes`` is the probe result (``routes()``) the caller already has.
        """

        now = time.monotonic()
        if self._parsers and now - self._parsers[0] < ROUTES_TTL_S:
            return self._parsers[1]
        found: set[str] = set()
        known = True
        if found_routes.get("settings.providers"):
            try:
                found |= offering.role_parsers(await self.json("GET", "/providers", api=SETTINGS, timeout=PROBE_TIMEOUT_S))
            except ProviderError:
                known = False
        if found_routes.get("settings.mineru"):
            try:
                status = await self.json("GET", "/mineru-status", api=SETTINGS, timeout=PROBE_TIMEOUT_S) or {}
                if status.get("enabled") and status.get("connected"):
                    found.add("mineru")
            except ProviderError:
                known = False
        if not known and self._parsers:
            # an unclear answer keeps what was known
            found |= self._parsers[1]
        if known or self._parsers:
            self._parsers = (now, found)
        return found

    def _remember(self, cache: dict, key: tuple, value: Any) -> Any:
        cache[key] = value
        while len(cache) > MAX_CACHED_DOCS:
            cache.pop(next(iter(cache)))
        return value

    async def anchors_in(self, kb_id: str, doc_id: str, text: str, page_id: str | None = None) -> list[tuple[int, str, dict]]:
        """DA's anchors of a document placed in ``text`` (its structure page): [(offset, raw id, anchor)].

        Empty when DA keeps no anchors for the document (plain documents) or
        cannot list them. ``page_id`` keeps only the anchors of that structure page.
        """

        key = (f"{doc_id}/{page_id or ''}", hash(text))
        if key in self._located:
            return self._located[key]
        anchors: list[dict] = []
        if await self.serves("structure_map"):
            data = await self.json("GET", f"/kbs/{segment(kb_id)}/documents/{segment(doc_id)}/structure-map",
                                   api=PREVIEW) or {}
            for page in data.get("structureMap") or []:
                if page_id is None or page.get("id") == page_id:
                    anchors += [a for a in page.get("anchors") or [] if str(a.get("id") or "").startswith(f"{doc_id}:")]
        by_id = {a["id"]: a for a in anchors}
        located = [(at, raw_id, by_id[raw_id]) for at, raw_id in await asyncio.to_thread(mapping.locate_anchors, text, anchors)]
        return self._remember(self._located, key, located)

    async def raw_items(self, kb_id: str, doc_id: str, version: str) -> list[dict]:
        """Text items with page and box from the parser JSON DA keeps for a document (docling or MinerU)."""

        key = (doc_id, version)
        if key in self._items:
            return self._items[key]
        raw = None
        base = f"/kbs/{segment(kb_id)}/documents/{segment(doc_id)}"
        try:
            if await self.serves("preview_raw"):
                raw = ((await self.json("GET", f"{base}/preview/raw", api=PREVIEW)) or {}).get("content")
            elif await self.serves("export"):
                raw = await self.json("GET", f"{base}/export/raw-json")
        except ProviderError as exc:
            # a document without a parser JSON (plain text, legacy flow)
            if exc.status != 404:
                raise
        items = await asyncio.to_thread(mapping.raw_items, raw)
        return self._remember(self._items, key, items)

    async def serves(self, *probes: str) -> bool:
        found = await self.routes()
        return all(found.get(p) for p in probes)

    async def settings_schema(self, served: list[str]) -> dict | None:
        """The settings schema with DA's current model services and pipelines; ``None`` without settings routes.

        ``served`` are the settings groups whose DA routes the backend serves (``offer()["settings"]``).
        """

        if not served:
            return None
        now = time.monotonic()
        if self._schema and now - self._schema[0] < SCHEMA_TTL_S:
            return self._schema[1]
        raw: dict[str, Any] = {group: None for group in served}
        try:
            # only the services and the pipelines shape the schema; the other groups are fixed
            for group, path in (("providers", "/providers"), ("strategies", "/pipeline-strategies")):
                if group in raw:
                    raw[group] = await self.json("GET", path, api=SETTINGS, timeout=SCHEMA_TIMEOUT_S)
        except ProviderError:
            return self._schema[1] if self._schema else backend_settings.schema({g: None for g in served})
        if not isinstance(raw.get("strategies"), list):
            raw["strategies"] = None
        schema = backend_settings.schema(raw)
        self._schema = (now, schema)
        return schema

    async def graph(self, kb_id: str) -> dict:
        """DA's page graph of a knowledge base (pages with their types, wiki links); empty without the route."""

        now = time.monotonic()
        cached = self._graphs.get(kb_id)
        if cached and now - cached[0] < GRAPH_TTL_S:
            return cached[1]
        if not await self.serves("graph"):
            return {}
        graph = await self.json("GET", f"/graph/{segment(kb_id, '知识库')}", api=REPORTS,
                                params={"maxNodes": MAX_GRAPH_NODES}) or {}
        self._graphs = {k: v for k, v in self._graphs.items() if now - v[0] < GRAPH_TTL_S}
        self._graphs[kb_id] = (now, graph)
        return graph

    def remember(self, raw_job: str, record: dict) -> dict:
        self.jobs[raw_job] = record
        while len(self.jobs) > MAX_JOBS:
            self.jobs.pop(next(iter(self.jobs)))
        return record

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
    app.include_router(_wiki_router())
    app.include_router(_extended_router())
    return app


def _router() -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(authorized)])

    @router.get("/capabilities")
    async def capabilities() -> dict:
        s = get_settings()
        extra = [t.strip() for t in s.extra_file_types.split(",") if t.strip()]
        found = await da().routes()
        offer = offering.offer(found)
        return {
            "contract": CONTRACT, "role": "base",
            "provider": {"type": "deepanalyze", "version": __version__, "label": s.label},
            "features": FEATURES + offer["features"], "file_types": offering.file_types(await da().parsers(found), extra),
            "limits": {"max_upload_mb": s.max_file_mb, "max_top_k": 50},
            "config_schema": None,
            "actions": offer["actions"], "views": offer["views"], "export_formats": offer["export_formats"],
            "settings_schema": await da().settings_schema(offer["settings"]),
            "search_options_schema": mapping.SEARCH_OPTIONS_SCHEMA,
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
        kb_id: str, status: str | None = None, q: str | None = None, folder: str | None = None,
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    ) -> dict:
        raw_kb = ids().inp(kb_id, "知识库")
        docs = await da().documents(raw_kb)
        if mapping.norm_folder(folder):
            docs = [d for d in docs if mapping.in_folder(d, mapping.norm_folder(folder))]
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
        raw = ids().inp(doc_id, "文档")
        kb_id = await da().kb_of(raw)
        l1 = await da().l1(kb_id, raw)
        win = await reading.structure(l1, doc_id, offset=offset, limit=limit, heading=heading)
        located = await da().anchors_in(kb_id, raw, l1)
        if located:
            # the anchors handed out are DA's own, so evidence and DA's search name the same passages
            end = win["offset"] + len(win.get("content") or "")
            win["anchors"] = await asyncio.to_thread(mapping.window_anchors, l1, doc_id, located, win["offset"], end, ids())
        return win

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
        kb_id = await da().kb_of(raw)
        # every page re-reads the whole L1 (no cache across requests)
        l1 = await da().l1(kb_id, raw)
        pieces = await asyncio.to_thread(mapping.split_chunks, l1)
        located = [(at, ids().out(raw_id)) for at, raw_id, _ in await da().anchors_in(kb_id, raw, l1)]
        if not located:
            # no anchors of DA's own: those of content.structure, which evidence resolves as well
            located = await asyncio.to_thread(mapping.block_anchors, l1, doc_id)
        window = list(enumerate(pieces))[offset:offset + limit]
        items = [mapping.chunk_view(raw, seq, line, text, ids(), mapping.anchor_for(located, start, start + len(text)))
                 for seq, (line, start, text) in window]
        return {"items": items, "total": len(pieces), "offset": offset}

    @router.get("/documents/{doc_id}/file")
    async def get_file(doc_id: str, request: Request) -> Response:
        raw = ids().inp(doc_id, "文档")
        span = _byte_range(request.headers.get("range"))
        if span is not None and await da().serves("original"):
            return await _file_range(raw, *span)
        kb_id = await da().kb_of(raw)
        upstream = await da().send("GET", f"/kbs/{segment(kb_id)}/documents/{segment(raw)}/download", stream=True)
        return _relay(upstream, {"content-disposition"}, extra={"accept-ranges": "bytes"})

    async def _file_range(raw: str, start: int | None, end: int | None) -> Response:
        """Bytes of the original file (contract ``documents.file.range``).

        DA's ``original`` route answers ranges but trusts them: an end past the
        file or a start beyond it would give a wrong length, so the range is
        clamped to the document's size first.
        """

        size = da().sizes.get(raw)
        kb_id = da().doc_kb.get(raw)
        if size is None or kb_id is None:
            kb_id, doc = await da().locate(raw)
            size = mapping._int(doc.get("file_size"))
            if size is not None:
                da().sizes[raw] = size
        if size is None:
            if start is None:
                raise ProviderError(416, "range_not_satisfiable", "文件大小未知，不能按末尾取范围")
            last = "" if end is None else str(end)
        else:
            if start is None:
                start, end = max(0, size - (end or 0)), size - 1
            if start >= size or (end is not None and end < start):
                return JSONResponse({"error": {"code": "range_not_satisfiable", "message": "请求的范围超出文件大小"}},
                                    status_code=416, headers={"content-range": f"bytes */{size}"})
            last = str(size - 1 if end is None else min(end, size - 1))
        try:
            upstream = await da().send("GET", f"/kbs/{segment(kb_id)}/documents/{segment(raw)}/original", stream=True,
                                       headers={"range": f"bytes={start}-{last}"})
        except ProviderError as exc:
            if exc.status == 404:
                da().sizes.pop(raw, None)
            raise
        if upstream.status_code != 206:
            return _relay(upstream, set(), extra={"accept-ranges": "bytes"})
        return _relay(upstream, {"content-range", "accept-ranges"}, status=206)

    async def _upload(raw_kb: str, name: str, data: bytes, folder_path: str, content_type: str) -> dict:
        if any(len(part.encode()) > MAX_FOLDER_BYTES for part in folder_path.split("/")):
            raise ProviderError(400, "bad_request", f"文件夹名称过长：每一级最多 {MAX_FOLDER_BYTES} 字节")
        name = textfiles.fit_file_name(name, max_chars=MAX_NAME_CHARS)
        # DA reads GB18030 with four-byte characters as Latin-1 and marks the garbled text ready
        data = await asyncio.to_thread(textfiles.to_utf8, name, data)
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
        mode, levels = mapping.search_options(body.get("options"))
        params: dict[str, Any] = {"query": query, "topK": top_k * 3}
        if mode or levels:
            # DA applies the mode only on its per-level search; each level returns its own top k
            params.update(topK=top_k * 2, levels=",".join(levels or mapping.LEVELS), **({"mode": mode} if mode else {}))
        if kb_ids:
            params["kbIds"] = ",".join(kb_ids)
        results = mapping.leveled_results((await da().json("GET", "/search", params=params) or {}).get("results") or [])
        wiki = body.get("include_wiki") is not False

        def wanted(r: dict) -> bool:
            # the scope is the gateway's to enforce; do not rely on DA honouring kbIds
            if kb_ids and r.get("kbId") not in kb_ids:
                return False
            return bool(r.get("docId")) or (wiki and r.get("pageType") in mapping.WIKI_TYPE and bool(r.get("kbId")))

        results = [r for r in results if wanted(r)]
        await _fill_hits(results[:MAX_FILLED_HITS], query)
        filenames: dict[str, str] = {}
        for kb_id in {r["kbId"] for r in results if r.get("docId")}:
            for doc in await da().documents(kb_id):
                filenames[doc["id"]] = doc.get("filename") or ""
        # DA's score is a rank-fusion score, not a 0-1 relevance: relevance stays null and
        # min_score (defined on relevance) is not applied
        items = mapping.distinct_hits([mapping.hit_view(r, ids(), filenames) for r in results])[:top_k]
        return {"query": query, "items": items}

    return router


def _mentions(needle: str, *texts: str | None) -> bool:
    return any(needle in (t or "").lower() for t in texts)


def _needs_anchor(hit: dict) -> bool:
    """A hit with text but no anchor, on a document page DA can read again."""
    has_text = bool(str(hit.get("snippet") or "").strip())
    on_page = bool(hit.get("docId") and hit.get("pageId") and hit.get("kbId"))
    return has_text and on_page and not hit.get("anchorId")


async def _fill_hits(results: list[dict], query: str) -> None:
    """Give hits without text (DA's full-text search keeps none) a passage of their page around the query.

    DA then names the page's first anchor for want of a passage; the anchor
    is placed again at the passage, or dropped when DA keeps none there.
    Hits on a document's full-text page come with text but no anchor: they get
    the anchor at their passage, so they can be located as well.
    """

    empty = [r for r in results if not str(r.get("snippet") or "").strip() and r.get("pageId") and r.get("kbId")]
    anchorless = [r for r in results if _needs_anchor(r)]
    pages: dict[str, dict] = {}
    gate = asyncio.Semaphore(SUMMARY_CONCURRENCY)

    async def read(r: dict) -> None:
        if r["pageId"] in pages:
            return
        async with gate:
            try:
                pages[r["pageId"]] = await da().page(r["kbId"], r["pageId"])
            except ProviderError as exc:
                if exc.status != 404:
                    raise
                pages[r["pageId"]] = {}

    await asyncio.gather(*(read(r) for r in empty + anchorless))
    for r in anchorless:
        text = mapping.strip_doctags(pages[r["pageId"]].get("content") or "")
        at = mapping.passage_at(text, mapping.hit_text(r["snippet"]), query)
        if at is not None:
            located = await da().anchors_in(r["kbId"], r["docId"], text)
            before = [raw_id for offset, raw_id, _ in located if offset <= at]
            r["anchorId"] = before[-1] if before else None
    for r in empty:
        text = mapping.strip_doctags(pages[r["pageId"]].get("content") or "")
        r["snippet"], at = mapping.snippet_around(text, query)
        if r.get("anchorId") and r.get("docId"):
            located = await da().anchors_in(r["kbId"], r["docId"], text, r["pageId"])
            before = [raw_id for offset, raw_id, _ in located if at is not None and offset <= at]
            r["anchorId"] = before[-1] if before else None


def _byte_range(header: str | None) -> tuple[int | None, int | None] | None:
    """(start, end) of a single ``bytes=`` range; (None, n) for the last n bytes; ``None`` means the whole file."""

    match = _RANGE.match(header or "")
    if not match or not (match.group(1) or match.group(2)):
        return None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    return start, end


def _relay(upstream: httpx.Response, passed: set[str], *, status: int | None = None,
           extra: dict | None = None) -> StreamingResponse:
    # the body is relayed decoded, so a compressed upstream's content-length would be wrong
    keep = passed | ({"content-length"} if "content-encoding" not in upstream.headers else set())
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in keep}
    headers.update(extra or {})

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=status or upstream.status_code,
                             media_type=upstream.headers.get("content-type"), headers=headers)


# ---- wiki (contract 5.6) ------------------------------------------------------------------


async def _reports(raw_kb: str) -> list[dict]:
    """The agent reports DA keeps in a knowledge base (wiki pages of type ``report``)."""

    if not await da().serves("reports"):
        return []
    data = await da().json("GET", f"/reports/kb/{segment(raw_kb, '知识库')}", api=REPORTS) or {}
    return [r for r in data.get("reports") or [] if r.get("id")]


async def _graph_pages(raw_kb: str) -> list[dict]:
    """Entity and concept pages (from DA's page graph): pages of no document, made by entity extraction."""

    nodes = (await da().graph(raw_kb)).get("nodes") or []
    return [{"id": n["id"], "type": n["type"], "title": n.get("label") or ""}
            for n in nodes if n.get("id") and n.get("type") in ("entity", "concept")]


async def _page_ref(raw_kb: str, page_id: str, titles: dict[str, str]) -> dict | None:
    """Contract wiki reference ({slug, title, page_type}) of a DA page."""

    try:
        page = await da().page(raw_kb, page_id)
    except ProviderError as exc:
        if exc.status == 404:
            return None
        raise
    if page.get("docId"):
        doc_id = page["docId"]
        return {"slug": mapping.summary_slug(doc_id), "title": titles.get(doc_id) or doc_id, "page_type": "summary"}
    kind = page.get("pageType")
    if kind in mapping.WIKI_TYPE:
        return {"slug": mapping.page_slug(kind, page_id), "title": mapping._page_title(page.get("title") or ""),
                "page_type": mapping.WIKI_TYPE[kind]}
    return None


async def _links(raw_kb: str, page_ids: list[str], own_slug: str, titles: dict[str, str]) -> tuple[list, list]:
    """(links_to, linked_from) of DA pages from DA's wiki links (entity extraction makes them)."""

    outgoing, incoming = [], []
    for page_id in page_ids:
        try:
            page = await da().json("GET", f"/{segment(raw_kb, '知识库')}/wiki/{segment(page_id, '页面')}")
        except ProviderError as exc:
            if exc.status == 404:
                continue
            raise
        for link in (page or {}).get("links") or []:
            if link.get("sourcePageId") == page_id and link.get("targetPageId") not in page_ids:
                outgoing.append(link["targetPageId"])
            elif link.get("targetPageId") == page_id and link.get("sourcePageId") not in page_ids:
                incoming.append(link["sourcePageId"])

    async def refs(pids: list[str]) -> list[dict]:
        out: dict[str, dict] = {}
        for pid in list(dict.fromkeys(pids))[:MAX_LINKED_PAGES]:
            ref = await _page_ref(raw_kb, pid, titles)
            if ref and ref["slug"] != own_slug:
                out.setdefault(ref["slug"], ref)
        return list(out.values())

    return await refs(outgoing), await refs(incoming)


async def _wiki_kb(kb_id: str) -> str:
    if not await da().serves("wiki"):
        raise ProviderError(501, "unsupported", "这个 DA 知识管线不提供 Wiki")
    return ids().inp(kb_id, "知识库")


def _wiki_router() -> APIRouter:
    router = APIRouter(prefix="/v1/kbs/{kb_id}/wiki", dependencies=[Depends(authorized)])

    @router.get("/status")
    async def wiki_status(kb_id: str) -> dict:
        raw_kb = await _wiki_kb(kb_id)
        docs, reports, graph_pages = await asyncio.gather(da().documents(raw_kb), _reports(raw_kb), _graph_pages(raw_kb))
        statuses = [mapping.doc_status(d) for d in docs]
        ready = [d for d in docs if d.get("status") == "ready"]
        pages = {"summary": len(ready)}
        for kind, n in (("entity", sum(p["type"] == "entity" for p in graph_pages)),
                        ("concept", len(reports) + sum(p["type"] == "concept" for p in graph_pages))):
            if n:
                pages[kind] = n
        stamps = [str(d.get("updated_at")) for d in ready if d.get("updated_at")]
        stamps += [str(r.get("updatedAt")) for r in reports if r.get("updatedAt")]
        failed = [d for d in docs if mapping.doc_status(d) == "failed"]
        return {
            "enabled": True, "pages": pages, "updated_at": max(stamps, default=None),
            "documents": {s: statuses.count(s) for s in ("completed", "processing", "pending", "failed") if s in statuses},
            "pending_jobs": 0,
            "errors": [{"id": ids().out(d["id"]), "title": mapping.doc_title(d), "wiki_error": d.get("processing_error")}
                       for d in failed[:10]],
        }

    @router.get("/pages")
    async def wiki_pages(
        kb_id: str, type: str | None = Query(None, pattern="^(summary|entity|concept|index)$"), q: str | None = None,
        limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
    ) -> dict:
        raw_kb = await _wiki_kb(kb_id)
        ready = [d for d in await da().documents(raw_kb) if d.get("status") == "ready"] if type in (None, "summary") else []
        reports = await _reports(raw_kb) if type in (None, "concept") else []
        graph_pages = [p for p in await _graph_pages(raw_kb) if type in (None, mapping.WIKI_TYPE[p["type"]])] \
            if type in (None, "entity", "concept") else []
        ready.sort(key=mapping.doc_title)
        others = [("report", r, r.get("title") or "") for r in reports] + [(p["type"], p, p["title"]) for p in graph_pages]
        others.sort(key=lambda o: (o[0] != "entity", mapping._page_title(o[2])))
        summaries: dict[str, str | None] = {}
        if q:
            needle = q.lower()
            summaries = await da().summaries(raw_kb, ready)
            ready = [d for d in ready if _mentions(needle, mapping.doc_title(d), summaries.get(d["id"]))]
            others = [o for o in others if needle in o[2].lower()]
        rows: list[tuple[str, dict]] = [("summary", d) for d in ready] + [(kind, page) for kind, page, _ in others]
        window = rows[offset:offset + limit]
        if not q:
            summaries = await da().summaries(raw_kb, [d for kind, d in window if kind == "summary"])
        items = [mapping.summary_item(obj, ids(), summaries.get(obj["id"])) if kind == "summary"
                 else mapping.page_item(kind, obj, ids()) for kind, obj in window]
        return {"items": items, "total": len(rows)}

    @router.get("/page")
    async def wiki_page(kb_id: str, slug: str) -> dict:
        raw_kb = await _wiki_kb(kb_id)
        parsed = mapping.parse_slug(slug)
        if parsed is None:
            raise not_found("Wiki 页面")
        kind, item_id = parsed
        docs = await da().documents(raw_kb)
        titles = {d["id"]: mapping.doc_title(d) for d in docs}
        if kind == "summary":
            doc = next((d for d in docs if d["id"] == item_id and d.get("status") == "ready"), None)
            if doc is None:
                raise not_found("Wiki 页面")
            abstract = await da().expand(raw_kb, item_id, "L0")
            overview = await da().overview(raw_kb, item_id, (abstract or {}).get("pageId"))
            summary = (abstract or {}).get("content") or ""
            # entity extraction links a document's L1 page (overview or structure) to its entities
            l1 = await da().expand(raw_kb, item_id, "L1")
            candidates = ((abstract or {}).get("pageId"), (overview or {}).get("id"), (l1 or {}).get("pageId"))
            page_ids = list(dict.fromkeys(p for p in candidates if p))
            own = mapping.summary_slug(item_id)
            links_to, linked_from = await _links(raw_kb, page_ids, own, titles)
            sources = [{"id": ids().out(item_id), "title": titles[item_id], "file_name": doc.get("filename")}]
            item = mapping.summary_item(doc, ids(), summary)
            content = (overview or {}).get("content") or summary
            return mapping.wiki_page(item, raw_kb, content, ids(), links_to=links_to, linked_from=linked_from,
                                     sources=sources)
        try:
            page = await da().json("GET", f"/{segment(raw_kb, '知识库')}/wiki/{segment(item_id, 'Wiki 页面')}")
        except ProviderError as exc:
            if exc.status == 404:
                raise not_found("Wiki 页面") from exc
            raise
        # DA falls back to a page whose title contains the path; only the page itself counts
        if not page or page.get("id") != item_id or page.get("pageType") != kind:
            raise not_found("Wiki 页面")
        links_to, linked_from = await _links(raw_kb, [item_id], slug, titles)
        sources = []
        if kind == "report" and await da().serves("reports"):
            found = await da().json("GET", f"/reports/{segment(item_id)}/sources", api=REPORTS) or {}
            sources = [{"id": ids().out(s["docId"]), "title": titles.get(s["docId"]) or s.get("originalName") or s["docId"],
                        "file_name": s.get("originalName")} for s in found.get("sources") or [] if s.get("docId")]
        elif kind != "report":
            # an entity's sources are the documents that mention it
            by_id = {d["id"]: d for d in docs}
            mentioned = [r["slug"].split("/", 1)[1] for r in linked_from + links_to if r["page_type"] == "summary"]
            sources = [{"id": ids().out(doc_id), "title": titles[doc_id], "file_name": by_id[doc_id].get("filename")}
                       for doc_id in mentioned if doc_id in by_id]
        content = page.get("content") or ""
        item = mapping.page_item(kind, page, ids(), summary=mapping.plain_snippet(content), sources=len(sources))
        return mapping.wiki_page(item, raw_kb, content, ids(), links_to=links_to, linked_from=linked_from,
                                 sources=sources)

    @router.get("/graph")
    async def wiki_graph(kb_id: str, center: str | None = None, limit: int = Query(300, ge=1, le=2000)) -> dict:
        raw_kb = await _wiki_kb(kb_id)
        docs, reports, graph = await asyncio.gather(da().documents(raw_kb), _reports(raw_kb), da().graph(raw_kb))
        nodes = {mapping.summary_slug(d["id"]): {"title": mapping.doc_title(d), "page_type": "summary"}
                 for d in docs if d.get("status") == "ready"}
        nodes.update({mapping.page_slug("report", r["id"]): {"title": mapping._page_title(r.get("title") or ""),
                                                             "page_type": "concept"} for r in reports})
        kinds = {n.get("id"): n.get("type") for n in graph.get("nodes") or []}
        for n in graph.get("nodes") or []:
            if n.get("type") in ("entity", "concept"):
                nodes[mapping.page_slug(n["type"], n["id"])] = {"title": mapping._page_title(n.get("label") or ""),
                                                               "page_type": mapping.WIKI_TYPE[n["type"]]}
        titles = {d["id"]: mapping.doc_title(d) for d in docs}
        slugs: dict[str, str | None] = {}

        async def slug_of(page_id: str) -> str | None:
            if page_id not in slugs:
                kind = kinds.get(page_id)
                if kind in mapping.WIKI_TYPE:
                    slugs[page_id] = mapping.page_slug(kind, page_id)
                else:
                    # a document's page (abstract, overview, ...): its document is the node
                    ref = await _page_ref(raw_kb, page_id, titles) if len(slugs) < MAX_LINKED_PAGES * 4 else None
                    slugs[page_id] = ref["slug"] if ref else None
            return slugs[page_id]

        edges = []
        for e in graph.get("edges") or []:
            source, target = await slug_of(e.get("source")), await slug_of(e.get("target"))
            if source and target:
                edges.append((source, target))
        return mapping.graph_view(nodes, edges, center, limit)

    return router


# ---- contract section 9: actions, jobs, views, settings, evidence, export, folders ------------


async def _declared(key: str, entry_id: str, scope: str) -> dict:
    """An action or view the backend offers (its DA routes are served), else 501."""

    found = next((e for e in (await da().offer())[key] if e["id"] == entry_id and e["scope"] == scope), None)
    if found is None:
        kind = "操作" if key == "actions" else "视图"
        raise ProviderError(501, "unsupported", f"{get_settings().label}没有这个{kind}：{entry_id}")
    return found


async def _params(request: Request) -> dict:
    body = await request.body()
    try:
        params = ((await request.json()) or {}).get("params") if body else {}
    except (ValueError, AttributeError) as exc:
        raise ProviderError(400, "bad_request", "请求体不是 JSON 对象") from exc
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ProviderError(400, "bad_request", "params 必须是对象")
    return params


async def _run_request_job(record: dict, path: str) -> None:
    """A DA call that answers only when its work is done, tracked in ``record``."""

    try:
        result = await da().json("POST", path, timeout=REQUEST_JOB_TIMEOUT_S)
        record["outcome"] = {"status": "completed", "result": result or {}}
        if record["action"] == "regenerate_summaries":
            da().forget_summaries({r.get("docId") for r in (result or {}).get("results") or []})
    except ProviderError as exc:
        record["outcome"] = {"status": "failed", "message": exc.message}
    except Exception as exc:  # noqa: BLE001 - the job reports any failure instead of losing it
        record["outcome"] = {"status": "failed", "message": f"DA 知识管线：{exc}"}
    record["finished"] = datetime.now(timezone.utc)


def _request_outcome(action: str, record: dict | None) -> dict:
    if record is None:
        return {"status": "failed", "message": "适配服务重启过，结果未知，可重新执行"}
    outcome = record.get("outcome")
    if outcome is None:
        return {"status": "running", "message": None}
    if outcome["status"] != "completed":
        return {"status": "failed", "message": outcome.get("message"), "finished": record.get("finished")}
    result = outcome.get("result") or {}
    if action == "regenerate_summaries":
        rows = result.get("results") or []
        done = sum(r.get("status") == "regenerated" for r in rows)
        failed = sum(str(r.get("status") or "").startswith("failed") for r in rows)
        message = f"重新生成 {done} 篇" + (f"，{failed} 篇失败" if failed else "")
        report = {"items": [{"文档": r.get("filename"),
                             "结果": SUMMARY_RESULT.get(r.get("status"), str(r.get("status") or "").replace("failed:", "失败："))}
                            for r in rows]}
        return {"status": "completed", "message": message, "finished": record.get("finished"), "report": report}
    message = f"已用 {result.get('provider')}（{result.get('dimension')} 维）重建" if result.get("provider") else None
    return {"status": "completed", "message": message, "finished": record.get("finished")}


async def _job(raw_job: str, *, report: bool = False, cache: dict | None = None) -> dict:
    parsed = jobs.parse_job_id(raw_job)
    if parsed is None:
        raise not_found("任务")
    action, raw_kb, started = parsed
    cache = {} if cache is None else cache
    record = da().jobs.get(raw_job)
    common = {"raw_id": raw_job, "action": action, "kb_id": raw_kb, "started": started, "ids": ids()}
    if action in jobs.QUEUE_ACTIONS:
        if ("docs", raw_kb) not in cache:
            cache[("docs", raw_kb)] = await da().documents(raw_kb)
        state = jobs.queue_state(action, started, cache[("docs", raw_kb)])
        return jobs.view(**common, status=state["status"], progress=state["progress"], message=state["message"],
                         finished=state["finished"])
    if action == "preprocess":
        if not await da().serves("preprocess.status"):
            raise not_found("任务")
        if ("pre", raw_kb) not in cache:
            cache[("pre", raw_kb)] = await _preprocess_status(raw_kb)
        status, da_now = cache[("pre", raw_kb)]
        state = jobs.preprocess_state(started, status, da_now)
        if record and record.get("cancelled") and state["status"] in ("running", "queued", "failed"):
            state = {**state, "status": "cancelled", "message": None, "cancellable": False}
        out = jobs.view(**common, status=state["status"], message=state["message"], cancellable=state["cancellable"],
                        has_report=state["has_report"], finished=state["finished"])
        if report and state["has_report"]:
            found = await da().json("GET", f"/kbs/{segment(raw_kb, '知识库')}/preprocess/report") or {}
            out["report"] = {"media_type": "text/markdown", "content": found.get("report") or ""}
        return out
    state = _request_outcome(action, record)
    out = jobs.view(**common, status=state["status"], message=state.get("message"),
                    has_report="report" in state, finished=state.get("finished"))
    if report and "report" in state:
        out["report"] = {"media_type": "application/json", "content": state["report"]}
    return out


async def _preprocess_status(raw_kb: str) -> tuple[dict, datetime]:
    """DA's preprocess status and DA's time (the start of a run is on DA's clock too)."""

    response = await da().send("GET", f"/kbs/{segment(raw_kb, '知识库')}/preprocess/status")
    return (response.json() if response.content else {}) or {}, jobs.started_at(response.headers.get("date")) + timedelta(seconds=1)


async def _start_kb_action(action: str, raw_kb: str) -> str:
    """Start a job action on DA; the raw job id."""

    kb_seg = segment(raw_kb, "知识库")
    paths = {"process_pending": f"/kbs/{kb_seg}/process-all", "reindex": f"/kbs/{kb_seg}/reindex",
             "preprocess": f"/kbs/{kb_seg}/preprocess", "regenerate_summaries": f"/kbs/{kb_seg}/regenerate-abstracts",
             "rebuild_embeddings": f"/kbs/{kb_seg}/rebuild-embeddings"}
    if action in jobs.REQUEST_ACTIONS:
        # checked now, so an unknown knowledge base fails here rather than in the background
        response = await da().send("GET", f"/kbs/{kb_seg}")
    else:
        response = await da().send("POST", paths[action])
    started = jobs.started_at(response.headers.get("date"))
    raw_job = jobs.job_id(action, raw_kb, started)
    record = da().remember(raw_job, {"action": action, "kb": raw_kb, "started": started})
    if action in jobs.REQUEST_ACTIONS:
        record["task"] = asyncio.create_task(_run_request_job(record, paths[action]))
    return raw_job


def _job_raw(job_id: str) -> str:
    return ids().inp(job_id, "任务")


async def _preprocess_jobs(raw_kb: str, status: dict) -> list[str]:
    """Preprocess runs DA knows about that this process did not start (from DA's own page, or before a restart)."""

    if any(r["action"] == "preprocess" and r["kb"] == raw_kb for r in da().jobs.values()):
        return []
    running = jobs.parse_time(status.get("startedAt"))
    if status.get("running") and running:
        return [jobs.job_id("preprocess", raw_kb, running)]
    done = jobs.parse_time(status.get("lastPreprocessedAt"))
    if done and status.get("lastReport"):
        return [jobs.job_id("preprocess", raw_kb, done - timedelta(milliseconds=1))]
    return []


async def _view_kb(view: str, raw_kb: str) -> Response:
    kb_seg = segment(raw_kb, "知识库")
    if view == "quality_report":
        rows = mapping.quality_rows(await da().json("GET", f"/kbs/{kb_seg}/quality-report") or {})
        if not rows:
            raise ProviderError(404, "not_found", "还没有质量审计结果")
        return JSONResponse({"items": rows})
    data = await da().json("GET", f"/timeline/{kb_seg}", api=REPORTS, params={"maxEvents": 200}) or {}
    if not data.get("events"):
        raise ProviderError(404, "not_found", "没有找到带日期的事件")
    titles = {d["id"]: mapping.doc_title(d) for d in await da().documents(raw_kb)}
    return JSONResponse({"events": mapping.timeline_events(data, titles)})


async def _view_doc(view: str, raw_doc: str) -> Response:
    kb_id = await da().kb_of(raw_doc)
    if view == "overview":
        page = await da().overview(kb_id, raw_doc)
        if not page or not page.get("content"):
            raise ProviderError(404, "not_found", "这篇文档没有概览")
        return PlainTextResponse(page["content"], media_type="text/markdown; charset=utf-8")
    base = f"/kbs/{segment(kb_id)}/documents/{segment(raw_doc)}"
    if view == "structure_map":
        data = await da().json("GET", f"{base}/structure-map", api=PREVIEW) or {}
        rows = mapping.structure_rows(data.get("structureMap") or [], ids())
        if not rows:
            raise ProviderError(404, "not_found", "这篇文档没有结构信息")
        return JSONResponse({"items": rows})
    found = mapping.media_view(await da().json("GET", f"{base}/media-metadata") or {})
    if found is None:
        raise ProviderError(404, "not_found", "这篇文档不是音视频或图片")
    return JSONResponse(found)


async def _settings_raw() -> dict:
    """One entry per DA settings route the backend serves (see ``backend_settings``)."""

    served = (await da().offer())["settings"]
    if not served:
        raise ProviderError(501, "unsupported", f"{get_settings().label}没有后端设置")

    async def get(path: str) -> Any:
        try:
            return await da().json("GET", path, api=SETTINGS)
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise

    paths = {"providers": "/providers", "strategies": "/pipeline-strategies", "parsing_mode": "/parsing-priority-mode",
             "auto_process": "/key/auto_process", **{g: route for g, (_, route, _) in backend_settings.CONFIGS.items()}}
    found = dict(zip(served, await asyncio.gather(*(get(paths[g]) for g in served)), strict=True))
    if "auto_process" in found:
        found["auto_process"] = (found["auto_process"] or {}).get("value")
    if "strategies" in found and not isinstance(found["strategies"], list):
        found["strategies"] = None
    return found


async def _apply_settings(changes: dict, raw: dict) -> None:
    """Write the changed groups to DA: services before the roles that may point at them."""

    for pid, fields in (changes.get("providers") or {}).items():
        stored = await da().json("GET", f"/providers/{segment(pid, '模型服务')}", api=SETTINGS)
        await da().json("PUT", f"/providers/{segment(pid, '模型服务')}", api=SETTINGS,
                        json=backend_settings.provider_body(stored or {}, fields))
    if changes.get("models"):
        await da().json("PUT", "/defaults", api=SETTINGS, json=changes["models"])
    processing = changes.get("processing") or {}
    if "auto_process" in processing:
        await da().json("PUT", "/key/auto_process", api=SETTINGS, json={"value": processing["auto_process"]})
    if "parsing_mode" in processing:
        await da().json("PUT", "/parsing-priority-mode", api=SETTINGS, json={"mode": processing["parsing_mode"]})
    if changes.get("pipelines"):
        await da().json("PUT", "/pipeline-strategies", api=SETTINGS,
                        json=backend_settings.reorder(raw.get("strategies") or [], changes["pipelines"]))
    for group, (_, route, _) in backend_settings.CONFIGS.items():
        if changes.get(group):
            await da().json("PUT", route, api=SETTINGS, json=backend_settings.config_body(group, changes[group]))


def _extended_router() -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(authorized)])

    # ---- actions and jobs ----

    @router.post("/kbs/{kb_id}/actions/{action}")
    async def kb_action(kb_id: str, action: str, request: Request) -> dict:
        await _declared("actions", action, "kb")
        await _params(request)
        raw_job = await _start_kb_action(action, ids().inp(kb_id, "知识库"))
        return {"job": await _job(raw_job)}

    @router.post("/documents/{doc_id}/actions/{action}")
    async def document_action(doc_id: str, action: str, request: Request) -> dict:
        await _declared("actions", action, "document")
        params = await _params(request)
        raw = ids().inp(doc_id, "文档")
        kb_id = await da().kb_of(raw)
        base = f"/kbs/{segment(kb_id)}/documents/{segment(raw)}"
        if action == "regenerate_summary":
            await da().json("POST", f"{base}/regenerate-abstract", timeout=ACTION_TIMEOUT_S)
            da().forget_summaries({raw})
            return {"result": await da().summary(kb_id, raw) or ""}
        query = {"reExtract": "true"} if params.get("re_extract") is True else None
        audit = await da().json("POST", f"{base}/quality-audit", params=query, timeout=ACTION_TIMEOUT_S) or {}
        return {"result": {"评分": audit.get("score"), "原评分": audit.get("originalScore"),
                           "问题": audit.get("issues") or [], "已重新提取": bool(audit.get("reExtracted")),
                           "模型": audit.get("modelUsed")}}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str, report: bool = False) -> dict:
        return await _job(_job_raw(job_id), report=report)

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict:
        raw_job = _job_raw(job_id)
        current = await _job(raw_job)
        if not current["cancellable"]:
            raise ProviderError(409, "not_cancellable", "这个任务不能取消" if current["status"] in ("queued", "running")
                                else "任务已经结束")
        _, raw_kb, started = jobs.parse_job_id(raw_job)
        result = await da().json("POST", f"/kbs/{segment(raw_kb, '知识库')}/preprocess/cancel") or {}
        if not result.get("cancelled"):
            raise ProviderError(409, "not_cancellable", "没有正在运行的预处理")
        record = da().jobs.get(raw_job) or da().remember(raw_job, {"action": "preprocess", "kb": raw_kb, "started": started})
        record["cancelled"] = True
        return await _job(raw_job)

    @router.get("/kbs/{kb_id}/jobs")
    async def list_jobs(kb_id: str, status: str | None = Query(None, pattern="^(queued|running|completed|failed|cancelled)$")) -> dict:
        raw_kb = ids().inp(kb_id, "知识库")
        if "jobs" not in (await da().offer())["features"]:
            raise ProviderError(501, "unsupported", f"{get_settings().label}没有任务")
        cache: dict = {}
        raw_ids = [raw for raw, r in da().jobs.items() if r["kb"] == raw_kb]
        if await da().serves("preprocess.status"):
            cache[("pre", raw_kb)] = await _preprocess_status(raw_kb)
            raw_ids += await _preprocess_jobs(raw_kb, cache[("pre", raw_kb)][0])
        items = [await _job(raw, cache=cache) for raw in raw_ids]
        items = [j for j in items if not status or j["status"] == status]
        items.sort(key=lambda j: j["started_at"] or "", reverse=True)
        return {"items": items[:MAX_LISTED_JOBS]}

    # ---- views ----

    @router.get("/kbs/{kb_id}/views/{view}")
    async def kb_view(kb_id: str, view: str) -> Response:
        await _declared("views", view, "kb")
        return await _view_kb(view, ids().inp(kb_id, "知识库"))

    @router.get("/documents/{doc_id}/views/{view}")
    async def document_view(doc_id: str, view: str) -> Response:
        await _declared("views", view, "document")
        return await _view_doc(view, ids().inp(doc_id, "文档"))

    # ---- backend settings ----

    @router.get("/settings")
    async def get_backend_settings() -> dict:
        return backend_settings.values(await _settings_raw())

    @router.put("/settings")
    async def put_backend_settings(request: Request) -> dict:
        try:
            patch = await request.json()
        except ValueError as exc:
            raise ProviderError(400, "bad_request", "设置必须是 JSON 对象") from exc
        raw = await _settings_raw()
        schema = backend_settings.schema(raw)
        changes = backend_settings.changes(schema, backend_settings.values(raw), patch)
        if changes:
            await _apply_settings(changes, raw)
            da()._schema = None
            raw = await _settings_raw()
        return backend_settings.values(raw)

    # ---- evidence ----

    @router.get("/evidence/{anchor_id}")
    async def evidence(anchor_id: str) -> dict:
        prefix = ids().prefix
        raw_anchor = anchor_id[len(prefix):] if anchor_id.startswith(prefix) else ""
        raw_doc, sep, rest = raw_anchor.partition(":")
        if not sep or not rest or any(c in rest for c in "/?#%\\"):
            raise not_found("锚点")
        ids().inp(prefix + raw_doc, "锚点")
        kb_id, doc = await da().locate(raw_doc)
        anchor = raw_context = None
        try:
            if await da().serves("anchors"):
                found = await da().json("GET", f"/anchors/{segment(raw_anchor, '锚点')}", api=PREVIEW) or {}
                anchor, raw_context = found.get("anchor"), found.get("rawContext")
        except ProviderError as exc:
            if exc.status != 404:
                raise
        if not isinstance(anchor, dict) or anchor.get("doc_id") != raw_doc or anchor.get("kb_id") != kb_id:
            anchor, raw_context = None, None
        l1 = await da().l1(kb_id, raw_doc)
        located = await da().anchors_in(kb_id, raw_doc, l1)
        at = next((offset for offset, raw_id, _ in located if raw_id == raw_anchor), None)
        if anchor is None and at is not None:
            # DA's anchor list knows it even where the anchor detail route is missing
            listed = next(a for offset, raw_id, a in located if raw_id == raw_anchor)
            anchor = {"element_type": listed.get("type"), "content_preview": listed.get("preview"),
                      "section_title": listed.get("sectionTitle")}
        items = await da().raw_items(kb_id, raw_doc, str(doc.get("updated_at") or ""))
        found = await asyncio.to_thread(mapping.evidence_view, anchor_id, doc, kb_id, l1, ids(), anchor=anchor,
                                        raw_context=raw_context, items=items, at=at, own_anchors=bool(located))
        if found is None:
            raise not_found("锚点")
        return found

    # ---- export ----

    @router.get("/documents/{doc_id}/export/{fmt}")
    async def export(doc_id: str, fmt: str) -> Response:
        if fmt not in (await da().offer())["export_formats"]:
            raise ProviderError(501, "unsupported", f"{get_settings().label}不能导出为 {fmt}")
        da_format, suffix = EXPORTS[fmt]
        raw = ids().inp(doc_id, "文档")
        kb_id, doc = await da().locate(raw)
        disposition = mapping.attachment(mapping.doc_title(doc) + suffix)
        try:
            if not await da().serves("export"):
                raise ProviderError(404, "not_found", "没有导出接口")
            upstream = await da().send("GET", f"/kbs/{segment(kb_id)}/documents/{segment(raw)}/export/{da_format}",
                                       stream=True)
        except ProviderError as exc:
            if exc.status != 404:
                raise
            # DA exports markdown only from structure pages; a plain document's markdown is its L1
            text = await da().l1(kb_id, raw) if fmt == "md" else ""
            if not text:
                raise ProviderError(404, "not_found", f"这篇文档没有可导出的 {fmt}") from exc
            return Response(text, media_type="text/markdown; charset=utf-8", headers={"content-disposition": disposition})
        return _relay(upstream, set(), extra={"content-disposition": disposition})

    # ---- folders ----

    @router.get("/kbs/{kb_id}/folders")
    async def list_folders(kb_id: str) -> dict:
        return {"items": mapping.folders(await da().documents(ids().inp(kb_id, "知识库")))}

    @router.delete("/kbs/{kb_id}/folders", status_code=204)
    async def delete_folder(kb_id: str, path: str = "") -> Response:
        if not await da().serves("folder_delete"):
            raise ProviderError(501, "unsupported", f"{get_settings().label}不能删除文件夹")
        raw_kb = ids().inp(kb_id, "知识库")
        folder = mapping.norm_folder(path)
        if not folder or any(part in (".", "..") for part in folder.split("/")):
            raise ProviderError(400, "bad_request", "需要文件夹路径")
        inside = [d for d in await da().documents(raw_kb) if mapping.in_folder(d, folder)]
        if not inside:
            raise not_found("文件夹")
        await da().json("DELETE", f"/kbs/{segment(raw_kb)}/folder", params={"path": folder})
        for doc in inside:
            da().doc_kb.pop(doc["id"], None)
            da().sizes.pop(doc["id"], None)
        return Response(status_code=204)

    return router
