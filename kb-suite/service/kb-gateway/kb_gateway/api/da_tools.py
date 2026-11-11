"""DA tool contract (``POST /v1/tools/*``) for DA-java's knowledge-service slot.

A call whose scope lies on one backend that implements the tools itself
(feature ``tools.da``, e.g. kb-service) is forwarded unchanged. Otherwise the
gateway answers from the knowledge-provider/v1 core API, in the same result
shapes (pageId ``{docId}:L0|L1|L2``, anchors ``{docId}:{type}:{index}``;
Wiki pages of non-native backends are ``wiki:{kbId}/{slug}``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import __version__, ops
from ..auth import CAP_REPROCESS, CAP_RETRIEVE, AuthError, parse_scope_header
from ..config import get_settings
from ..deps import caller, registry
from ..registry import Backend, UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tools")

REQUIRED_HEADERS = ("x-session-id", "x-tenant-id", "x-request-id")
L1_PAGE_CHARS = 30000
MAX_ANCHORS = 200
STRUCTURE_TYPES = {"structure", "structure_md", "structure_dt", "fulltext"}
WIKI_PAGE_TYPES = {"entity": "entity", "concept": "concept", "summary": "overview", "index": "overview"}
WIKI_DA_TYPES = {"entity", "concept", "overview"}
WIKI_PREFIX = "wiki:"
LEVEL_TYPES = {"L0": "abstract", "L1": "structure_md", "L2": "fulltext"}


def _tokens(text: str) -> int:
    return max(1, int(len(text) / 1.5)) if text else 0


def _context(request: Request, capability: str) -> list[str]:
    missing = [h for h in REQUIRED_HEADERS if not request.headers.get(h)]
    if missing:
        raise AuthError(400, f"missing required headers: {', '.join(missing)}")
    principal = caller(request, capability)
    scope = parse_scope_header(request.headers.get("x-kb-scope"))
    if not scope:
        raise AuthError(403, "knowledge base scope is empty")
    if principal.kb_allowlist:
        scope = [kb for kb in scope if kb in principal.kb_allowlist]
        if not scope:
            raise AuthError(403, "knowledge base scope is outside the key's allowlist")
    return scope


def _narrow(scope: list[str], requested: list[str] | None) -> tuple[list[str], str | None]:
    if not requested:
        return scope, None
    allowed = [kb for kb in requested if kb in scope]
    dropped = [kb for kb in requested if kb not in scope]
    return allowed, (f"已忽略会话范围外的知识库: {', '.join(dropped)}" if dropped else None)


def _split_page_id(page_id: str) -> tuple[str, str | None]:
    if ":" in page_id:
        doc_id, level = page_id.rsplit(":", 1)
        if level in {"L0", "L1", "L2"}:
            return doc_id, level
    return page_id, None


def _split_wiki_id(page_id: str) -> tuple[str, str] | None:
    """``wiki:{kbId}/{slug}`` (gateway form); ``None`` for a backend-native ``wiki:{id}``."""

    rest = page_id.removeprefix(WIKI_PREFIX)
    kb_id, sep, slug = rest.partition("/")
    if not (sep and slug) or not _safe_slug(slug):
        return None
    return kb_id, slug


def _safe_slug(slug: str) -> bool:
    """Slugs come from the model; a backend may build a path from them, so no dot segments or URL syntax."""

    return not any(c in slug for c in "?#%\\") and all(part not in {"", ".", ".."} for part in slug.split("/"))


def _subset(scope: list[str], backend: Backend) -> list[str]:
    reg = registry()
    return [kb for kb in scope if reg.route(kb) is backend]


async def _native(backend: Backend, request: Request, scope: list[str], body: dict) -> Response:
    headers = {h: request.headers[h] for h in ("x-session-id", "x-request-id") if h in request.headers}
    headers["X-Kb-Scope"] = ",".join(_subset(scope, backend))
    response = await backend.send("POST", request.url.path, tools=True, headers=headers, json=body)
    return Response(response.content, status_code=response.status_code, media_type="application/json")


def _native_ok(backend: Backend) -> bool:
    return backend.healthy and backend.has("tools.da")


async def _doc_in_scope(scope: list[str], doc_id: str) -> dict | None:
    doc = await ops.get_document(registry(), doc_id)
    if doc is None or doc.get("kb_id") not in scope:
        return None
    return doc


# ---- kb_search ------------------------------------------------------------


def _wiki_page_id(kb_id: str, slug: str) -> str:
    return f"{WIKI_PREFIX}{kb_id}/{slug}"


def _result(hit: dict) -> dict:
    if hit.get("chunk_type") == "wiki_page":
        return {
            "pageId": _wiki_page_id(hit["kb_id"], hit.get("wiki_slug") or ""),
            "kbId": hit["kb_id"], "docId": None, "title": hit.get("title"),
            "score": hit.get("score"), "relevance": hit.get("relevance"),
            "snippet": (hit.get("content") or "")[:800], "source": "fusion", "level": "wiki",
            "pageType": WIKI_PAGE_TYPES.get(hit.get("wiki_type") or "", "overview"),
            "wikiSlug": hit.get("wiki_slug"), "sourceDocIds": hit.get("wiki_sources") or [],
        }
    header = hit.get("context_header") or ""
    return {
        "pageId": f"{hit['doc_id']}:L1", "kbId": hit["kb_id"], "docId": hit["doc_id"], "title": hit.get("title"),
        "score": hit.get("score"), "relevance": hit.get("relevance"),
        "snippet": ((header + "\n") if header else "") + (hit.get("content") or "")[:800],
        "source": "fusion", "anchorId": hit.get("anchor_id"), "level": "L1", "pageType": "structure_md",
        "chunkId": hit.get("chunk_id"), "lineStart": hit.get("line_start"), "locators": hit.get("locators") or [],
    }


async def _linked_generic(scope: list[str], page_id: str, seen: set[str]) -> list[dict]:
    if not page_id.startswith(WIKI_PREFIX):
        return []
    parsed = _split_wiki_id(page_id)
    if parsed is None or parsed[0] not in scope:
        return []
    kb_id, slug = parsed
    backend = registry().route(kb_id)
    if not (backend.healthy and backend.has("wiki.read")):
        return []
    try:
        page = await backend.json("GET", f"/v1/kbs/{kb_id}/wiki/page", params={"slug": slug},
                                  timeout=get_settings().search_timeout_s)
    except UpstreamError as exc:
        # extra pages only: a dead link or a backend hiccup must not cost the search results
        logger.warning("linkedFrom %s failed: %s", page_id, exc.message)
        return []
    out = []
    for link in page.get("links_to") or []:
        pid = _wiki_page_id(kb_id, link["slug"])
        if pid in seen:
            continue
        seen.add(pid)
        out.append({
            "pageId": pid, "kbId": kb_id, "docId": None, "title": link.get("title"), "score": 0.0, "relevance": 0.0,
            "snippet": "", "source": "link", "level": "wiki",
            "pageType": WIKI_PAGE_TYPES.get(link.get("page_type") or "", "overview"), "wikiSlug": link["slug"],
        })
    return out


async def _search_group(backend: Backend, request: Request, scope: list[str], ids: list[str], body: dict) -> list[dict]:
    """One backend's part of a cross-backend kb_search: on the short timeout, so one slow backend cannot sink the call."""

    timeout = get_settings().search_timeout_s
    if _native_ok(backend):
        headers = {h: request.headers[h] for h in ("x-session-id", "x-request-id") if h in request.headers}
        headers["X-Kb-Scope"] = ",".join(_subset(scope, backend))
        data = await backend.json("POST", "/v1/tools/kb_search", tools=True, headers=headers, json={**body, "kbIds": ids},
                                  timeout=timeout)
        return data if isinstance(data, list) else (data or {}).get("results", [])
    page_types = set(body.get("pageTypes") or [])
    want_chunks = not page_types or bool(page_types & STRUCTURE_TYPES)
    want_wiki = (not page_types or bool(page_types & WIKI_DA_TYPES)) and backend.has("wiki.read")
    top_k = int(body.get("topK") or 20)
    hits, _, _ = await ops.search(
        registry(), ids,
        {"query": str(body.get("query") or ""), "min_score": body.get("minScore"), "include_wiki": want_wiki},
        max(1, min(top_k, ops.SEARCH_MAX_TOP_K)), fan_out=True,
    )
    results = []
    for hit in hits:
        is_wiki = hit.get("chunk_type") == "wiki_page"
        if not (want_wiki if is_wiki else want_chunks):
            continue
        result = _result(hit)
        if is_wiki and page_types and result["pageType"] not in page_types:
            continue
        results.append(result)
    return results


@router.post("/kb_search")
async def kb_search(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"results": [], "total": 0, "message": warning or "所选知识库都不在会话范围内"})
    groups = [(registry().by_id[bid], ids) for bid, ids in registry().group(kb_ids).items()]
    if len(groups) == 1 and _native_ok(groups[0][0]) and not warning:
        return await _native(groups[0][0], request, scope, body)
    top_k = int(body.get("topK") or 20)
    outcomes = await asyncio.gather(
        *(_search_group(b, request, scope, ids, body) for b, ids in groups), return_exceptions=True
    )
    lists, notes = [], [warning] if warning else []
    for (backend, _), outcome in zip(groups, outcomes):
        if isinstance(outcome, Exception):
            notes.append(f"{backend.config.label} 检索失败：{getattr(outcome, 'message', outcome)}")
        else:
            lists.append(outcome)
    results = ops.rrf_merge(lists, top_k) if lists else []
    if body.get("linkedFrom"):
        seen = {r["pageId"] for r in results}
        results.extend(await _linked_generic(scope, str(body["linkedFrom"]), seen))
    if notes:
        return JSONResponse({"results": results, "total": len(results), "message": "；".join(n for n in notes if n)})
    return JSONResponse(results)


# ---- expand ---------------------------------------------------------------


def _da_anchors(win: dict) -> list[dict]:
    return [{"id": a["id"], "type": a["type"], "lineStart": a["line_start"], "preview": a["preview"],
             "sectionTitle": a["section_title"]} for a in win.get("anchors") or []]


async def _expand_batch(scope: list[str], doc_ids: list[str]) -> dict:
    async def one(doc_id: str) -> dict:
        try:
            return await _expand_outline(scope, doc_id)
        except UpstreamError as exc:
            return {"docId": doc_id, "error": exc.message}

    results = await asyncio.gather(*(one(d) for d in doc_ids[:20]))
    return {"mode": "batch", "totalDocs": len(doc_ids), "results": list(results)}


async def _expand_outline(scope: list[str], doc_id: str) -> dict:
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return {"docId": doc_id, "error": "文档不存在或不在会话范围内"}
    win = await ops.structure(registry(), doc_id, limit=0)
    return {
        "docId": doc_id, "filename": doc.get("file_name"), "title": doc.get("title"), "level": "L1",
        "pageId": f"{doc_id}:L1", "summary": doc.get("summary"), "outline": win["outline"],
        "tokenCount": _tokens("x" * int(win["total_chars"])),
    }


async def _expand_one(scope: list[str], body: dict) -> dict:
    level = body.get("targetLevel")
    doc_id = body.get("docId")
    if body.get("pageId"):
        doc_id, page_level = _split_page_id(str(body["pageId"]))
        level = level or page_level
    if not doc_id:
        return {"error": "需要 pageId、docId 或 docIds"}
    level = level if level in LEVEL_TYPES else "L1"
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return {"error": f"文档不存在或不在会话范围内: {doc_id}"}
    if doc.get("parse_status") != "completed":
        return {"error": f"文档尚未处理完成（状态：{doc.get('parse_status')}）"}
    backend = registry().route(doc_id)
    levels = [lv for lv in LEVEL_TYPES if lv != "L2" or backend.has("content.L2")]
    base = {
        "docId": doc_id, "filename": doc.get("file_name"), "title": doc.get("title"), "level": level,
        "pageId": f"{doc_id}:{level}",
        "childPages": [{"pageId": f"{doc_id}:{lv}", "level": lv, "title": doc.get("title")} for lv in levels if lv != level],
    }
    if level == "L0":
        content = doc.get("summary") or ""
        if not content:
            head = await ops.structure(registry(), doc_id, limit=800)
            content = "[摘要尚未生成，以下为正文开头]\n" + head["content"]
        return {**base, "content": content, "tokenCount": _tokens(content)}
    if level == "L2":
        if not backend.has("content.L2"):
            return {"error": f"{backend.config.label} 不提供 L2 解析数据，请改用 L1"}
        content = await ops.read_content(registry(), doc_id, "L2")
        return {**base, "content": content, "tokenCount": _tokens(content), "format": "json"}
    if not backend.has("content.structure"):
        return {"error": f"{backend.config.label} 不支持分段读取正文"}
    heading = str(body["heading"]) if body.get("heading") else None
    limit = L1_PAGE_CHARS
    if body.get("tokenBudget"):
        limit = max(1000, min(int(float(body["tokenBudget"]) * 1.5), ops.STRUCTURE_MAX_CHARS - 1))
    # one extra char tells whether a heading section was cut, whether or not the backend applies limit to it
    win = await ops.structure(registry(), doc_id, offset=int(body.get("offset") or 0),
                              limit=limit + 1 if heading else limit, heading=heading)
    if win is None:
        return {"error": f'Section "{heading}" not found in page {base["pageId"]}'}
    content = win["content"]
    anchors = _da_anchors(win)
    payload = {**base, "content": content[:limit], "tokenCount": _tokens(content[:limit]), "anchors": anchors}
    if heading and len(content) > limit:
        # keep only the anchors that point into the text the caller got
        last_line = (anchors[0].get("lineStart") or 0) + content[:limit].count("\n") if anchors else 0
        payload["anchors"] = [a for a in anchors if (a.get("lineStart") or 0) <= last_line][:MAX_ANCHORS]
        payload["truncated"] = True
        payload["message"] = f"该节超过 {limit} 字，已截断；可去掉 heading 按 offset 分页读取"
    if not heading:
        payload.update(totalChars=win["total_chars"], offset=win["offset"], hasMore=win["has_more"])
        if "next_offset" in win:
            payload["nextOffset"] = win["next_offset"]
    return payload


@router.post("/expand")
async def expand(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    doc_ids = [str(d) for d in body.get("docIds") or []]
    targets = doc_ids or [_split_page_id(str(body.get("pageId") or body.get("docId") or ""))[0]]
    owners = {registry().route(d).id for d in targets if d}
    if len(owners) == 1:
        backend = registry().by_id[owners.pop()]
        if _native_ok(backend):
            return await _native(backend, request, scope, body)
    if doc_ids:
        return JSONResponse(await _expand_batch(scope, doc_ids))
    return JSONResponse(await _expand_one(scope, body))


# ---- doc_grep -------------------------------------------------------------


@router.post("/doc_grep")
async def doc_grep(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    pattern = str(body.get("pattern") or "")
    if not pattern:
        return JSONResponse({"error": "pattern 不能为空"})
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"pattern": pattern, "total": 0, "matches": [], "message": warning})
    max_lines = max(1, min(int(body.get("maxLinesPerPage") or 10), ops.GREP_MAX_LINES_PER_DOC))
    max_results = max(1, min(int(body.get("maxResults") or 50), ops.GREP_MAX_RESULTS))
    owners = {registry().route(kb).id for kb in kb_ids}
    if len(owners) == 1 and not warning:
        backend = registry().by_id[next(iter(owners))]
        if _native_ok(backend):
            return await _native(backend, request, scope, {**body, "maxResults": max_results, "maxLinesPerPage": max_lines})
    result = ops.grep_view(await ops.grep(registry(), kb_ids, pattern, max_results=max_results,
                                          max_lines_per_doc=max_lines))
    result.update({"totalPages": result["totalDocuments"], "maxLinesPerPage": max_lines, "total": result["totalMatches"]})
    if warning:
        result["message"] = warning
    return JSONResponse(result)


# ---- wiki_browse ----------------------------------------------------------


def _file_type_label(ext: str) -> str:
    ext = (ext or "").lower()
    groups = {
        "PDF": {"pdf"}, "Spreadsheet": {"xlsx", "xls", "csv", "tsv"}, "Word": {"doc", "docx"},
        "Presentation": {"ppt", "pptx"}, "Text": {"txt", "md", "markdown"}, "Data": {"json", "xml", "yaml", "yml"},
        "Web": {"html", "htm", "mhtml"},
    }
    for label, exts in groups.items():
        if ext in exts:
            return label
    return ext.upper() or "Unknown"


async def _wiki_page(scope: list[str], page_id: str) -> dict:
    parsed = _split_wiki_id(page_id)
    if parsed is None or parsed[0] not in scope:
        return {"error": f"Page not found: {page_id}"}
    kb_id, slug = parsed
    backend = registry().route(kb_id)
    if not backend.has("wiki.read"):
        return {"error": f"{backend.config.label} 没有 Wiki 页面"}
    try:
        page = await backend.json("GET", f"/v1/kbs/{kb_id}/wiki/page", params={"slug": slug})
    except UpstreamError as exc:
        if exc.status == 404:
            return {"error": f"Page not found: {page_id}"}
        raise
    content = page.get("content") or ""
    return {
        "id": page_id, "kbId": kb_id, "docId": None,
        "pageType": WIKI_PAGE_TYPES.get(page.get("page_type") or "", "overview"),
        "title": page.get("title"), "summary": page.get("summary"), "slug": slug,
        "tokenCount": _tokens(content), "content": content[:L1_PAGE_CHARS], "truncated": len(content) > L1_PAGE_CHARS,
        "sourceDocIds": page.get("source_doc_ids") or [],
        "linksTo": [{"pageId": _wiki_page_id(kb_id, x["slug"]), "title": x.get("title")} for x in page.get("links_to") or []],
        "linkedFrom": [{"pageId": _wiki_page_id(kb_id, x["slug"]), "title": x.get("title")} for x in page.get("linked_from") or []],
    }


async def _doc_page(scope: list[str], page_id: str) -> dict:
    doc_id, level = _split_page_id(page_id)
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return {"error": f"Page not found: {page_id}"}
    level = level or "L1"
    if level == "L0":
        content = doc.get("summary") or ""
    elif level == "L2" and not registry().route(doc_id).has("content.L2"):
        return {"error": f"Page not found: {page_id}"}
    else:
        content = await ops.read_content(registry(), doc_id, level)
    return {
        "id": f"{doc_id}:{level}", "kbId": doc["kb_id"], "docId": doc_id, "filename": doc.get("file_name"),
        "pageType": LEVEL_TYPES[level], "title": doc.get("title"), "tokenCount": _tokens(content),
        "content": content[:L1_PAGE_CHARS], "truncated": len(content) > L1_PAGE_CHARS,
    }


def _categories(docs: list[dict]) -> list[dict]:
    categories: dict[str, dict] = {}
    for d in docs:
        folder = (d.get("folder_path") or "").replace("\\", "/").strip("/")
        cat = "/".join([p for p in folder.split("/") if p][-2:]) if folder else _file_type_label(d.get("file_type") or "")
        entry = categories.setdefault(cat, {"type": cat, "count": 0, "sampleFiles": []})
        entry["count"] += 1
        if len(entry["sampleFiles"]) < 3:
            entry["sampleFiles"].append(d.get("file_name") or d.get("title"))
    return list(categories.values())


async def _kb_pages(kb_id: str, page_type: str | None) -> dict:
    docs = await ops.list_documents(registry(), kb_id)
    pages: list[dict[str, Any]] = []
    for d in docs:
        if d.get("parse_status") != "completed":
            continue
        for level, ptype, tokens in (
            ("L0", "abstract", _tokens(d.get("summary") or "")),
            ("L1", "structure_md", _tokens("x" * int(d.get("l1_chars") or 0))),
        ):
            if not page_type or page_type == ptype:
                pages.append({"id": f"{d['id']}:{level}", "docId": d["id"], "pageType": ptype, "title": d.get("title"),
                              "tokenCount": tokens})
    backend = registry().route(kb_id)
    if (not page_type or page_type in WIKI_DA_TYPES) and backend.has("wiki.read"):
        data = await backend.json("GET", f"/v1/kbs/{kb_id}/wiki/pages", params={"limit": 1000})
        for row in (data or {}).get("items", []):
            ptype = WIKI_PAGE_TYPES.get(row.get("page_type") or "", "overview")
            if not page_type or page_type == ptype:
                pages.append({"id": _wiki_page_id(kb_id, row["slug"]), "docId": None, "pageType": ptype,
                              "title": row.get("title"), "summary": row.get("summary"), "slug": row["slug"]})
    return {"kbId": kb_id, "total": len(pages), "pages": pages}


@router.post("/wiki_browse")
async def wiki_browse(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    page_id = str(body.get("pageId") or "")
    if page_id:
        target = page_id.removeprefix(WIKI_PREFIX)
        parsed = _split_wiki_id(page_id) if page_id.startswith(WIKI_PREFIX) else None
        owner = registry().route(parsed[0] if parsed else _split_page_id(target)[0])
        if _native_ok(owner) and parsed is None:
            return await _native(owner, request, scope, body)
        return JSONResponse(await (_wiki_page(scope, page_id) if parsed else _doc_page(scope, page_id)))
    kb_id = body.get("kbId")
    if not kb_id:
        return JSONResponse({"error": "需要 kbId 或 pageId"})
    if kb_id not in scope:
        return JSONResponse({"error": f"知识库不在会话范围内: {kb_id}"})
    owner = registry().route(kb_id)
    if _native_ok(owner):
        return await _native(owner, request, scope, body)
    if body.get("listDocuments"):
        docs = await ops.list_documents(registry(), kb_id)
        return JSONResponse({
            "kbId": kb_id, "totalDocuments": len(docs), "categories": _categories(docs),
            "documents": [
                {"docId": d["id"], "filename": d.get("file_name") or d.get("title"), "title": d.get("title"),
                 "folderPath": d.get("folder_path") or None, "fileType": d.get("file_type"),
                 "status": d.get("parse_status"), "abstractPageId": f"{d['id']}:L0" if d.get("summary") else None,
                 "abstract": d.get("summary")}
                for d in docs
            ],
        })
    return JSONResponse(await _kb_pages(kb_id, body.get("pageType")))


# ---- reprocess_document ----------------------------------------------------


@router.post("/reprocess_document")
async def reprocess_document(request: Request) -> Response:
    scope = _context(request, CAP_REPROCESS)
    body = await request.json()
    doc_id = str(body.get("docId") or "")
    backend = registry().route(doc_id)
    # checked before forwarding: a readonly backend keeps tools.da but loses documents.reprocess
    if not backend.has("documents.reprocess"):
        return JSONResponse({"error": f"{backend.config.label} 不支持重新处理文档"})
    if _native_ok(backend):
        return await _native(backend, request, scope, body)
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return JSONResponse({"error": f"文档不存在或不在会话范围内: {doc_id}"})
    try:
        await backend.json("POST", f"/v1/documents/{doc_id}/reprocess")
    except UpstreamError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}})
    return JSONResponse({"kbId": doc["kb_id"], "docId": doc_id, "status": "queued", "message": "已重新排队处理"})


# ---- tool spec ------------------------------------------------------------


def _op(name: str, summary: str, properties: dict, required: list[str]) -> dict:
    headers = [
        {"name": h, "in": "header", "required": h != "X-Kb-Scope", "schema": {"type": "string"}}
        for h in ("X-Session-Id", "X-Tenant-Id", "X-Kb-Scope", "X-Request-Id")
    ]
    return {"post": {
        "operationId": name, "summary": summary, "description": summary, "parameters": headers,
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": {"type": "object", "properties": properties, "required": required}}}},
        "responses": {"200": {"description": "tool result", "content": {"application/json": {"schema": {}}}}},
        "x-max-duration": {"defaultMs": 120000, "maxMs": 120000},
    }}


def tool_spec(base_url: str = "") -> dict:
    s, n, b, a = {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}
    paths = {
        "/v1/tools/kb_search": _op(
            "kb_search",
            "语义搜索知识库（可跨多个知识后端，按排名融合）。返回 L1 结构化正文命中（含 pageId/docId/anchorId 与原文定位）"
            "和 Wiki 页面（pageType 为 entity/concept/overview）；linkedFrom 追加某 Wiki 页面链接到的页面。",
            {"query": s, "kbIds": a, "topK": n, "linkedFrom": s, "pageTypes": a, "minScore": n}, ["query"]),
        "/v1/tools/expand": _op(
            "expand", "逐层展开文档内容（L0 摘要 / L1 结构化 Markdown / L2 解析定位数据）。L1 返回 anchors，锚点 ID 须逐字复制。",
            {"pageId": s, "docId": s, "docIds": a, "kbId": s, "targetLevel": s, "format": s, "heading": s,
             "tokenBudget": n, "offset": n}, []),
        "/v1/tools/doc_grep": _op(
            "doc_grep", "在知识库文档的 L1 正文中按正则或字面量逐行查找，穷尽扫描。",
            {"pattern": s, "kbIds": a, "pageTypes": a, "maxResults": n, "maxLinesPerPage": n}, ["pattern"]),
        "/v1/tools/wiki_browse": _op(
            "wiki_browse",
            "浏览知识库：listDocuments=true 返回全部文档与 L0 摘要；pageId 返回页面内容（wiki: 开头的是 Wiki 页面，带链接关系）；"
            "仅 kbId 列出页面，可按 pageType（abstract/structure_md/entity/concept/overview）过滤。",
            {"pageId": s, "kbId": s, "pageType": s, "listDocuments": b}, []),
        "/v1/tools/reprocess_document": _op(
            "reprocess_document", "重新解析并索引一个文档（异步排队）。",
            {"kbId": s, "docId": s, "processor": s, "force": b}, ["docId"]),
    }
    spec = {"openapi": "3.0.3", "info": {"title": "kb-gateway DA tools", "version": __version__}, "paths": paths}
    if base_url:
        spec["servers"] = [{"url": base_url}]
    return spec


@router.get("/openapi.json")
async def openapi_spec() -> dict:
    return tool_spec()
