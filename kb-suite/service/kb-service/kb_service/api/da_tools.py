"""DA tool contract (K7): POST /v1/tools/{kb_search,expand,doc_grep,wiki_browse,reprocess_document}.

DA-java mounts these through its ``knowledge-service`` OpenAPI slot and sends
X-Session-Id / X-Tenant-Id / X-Kb-Scope (comma separated) / X-Request-Id.
Result shapes mirror DeepAnalyze's original tools (tool-setup.ts) so DA's
prompts keep working: L0 = document summary, L1 = full structured Markdown
with anchors ``{docId}:{type}:{index}``, L2 = parser source blocks.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__, auth, db, documents, retrieval
from ..auth import AuthError
from .. import text_structure
from ..wiki import links as wiki_links
from ..wiki import store as wiki_store
from .common import Caller, caller_from_request, error_body

router = APIRouter(prefix="/v1/tools")

REQUIRED_HEADERS = ("x-session-id", "x-tenant-id", "x-request-id")
L1_PAGE_CHARS = 30000
MAX_ANCHORS = 200
STRUCTURE_TYPES = {"structure", "structure_md", "structure_dt", "fulltext"}
# kb-service wiki page types -> DA page types
WIKI_PAGE_TYPES = {"entity": "entity", "concept": "concept", "summary": "overview", "index": "overview"}
WIKI_DA_TYPES = {"entity", "concept", "overview"}
WIKI_PREFIX = "wiki:"


def _tokens(text: str) -> int:
    return max(1, int(len(text) / 1.5)) if text else 0


async def _context(request: Request, capability: str) -> tuple[Caller, list[str]]:
    missing = [h for h in REQUIRED_HEADERS if not request.headers.get(h)]
    if missing:
        raise AuthError(400, f"missing required headers: {', '.join(missing)}")
    caller = await caller_from_request(request, capability)
    scope = auth.parse_scope_header(request.headers.get("x-kb-scope"))
    kb_ids = await caller.kb_scope(scope, allow_all_when_empty=False)
    return caller, kb_ids


def _narrow(scope: list[str], requested: list[str] | None) -> tuple[list[str], str | None]:
    if not requested:
        return scope, None
    allowed = [kb for kb in requested if kb in scope]
    dropped = [kb for kb in requested if kb not in scope]
    warning = f"已忽略会话范围外的知识库: {', '.join(dropped)}" if dropped else None
    return allowed, warning


def _page_id(doc_id: str, level: str) -> str:
    return f"{doc_id}:{level}"


def _wiki_page_id(page: dict) -> str:
    return f"{WIKI_PREFIX}{page['id']}"


def _split_page_id(page_id: str) -> tuple[str, str | None]:
    if ":" in page_id:
        doc_id, level = page_id.rsplit(":", 1)
        if level in {"L0", "L1", "L2"}:
            return doc_id, level
    return page_id, None


async def _doc_in_scope(caller: Caller, scope: list[str], doc_id: str) -> dict | None:
    return await retrieval.get_document(caller.tenant_id, doc_id, scope)


def _wiki_result(h: retrieval.Hit, source: str = "fusion") -> dict:
    return {
        "pageId": f"{WIKI_PREFIX}{h.chunk_id.removeprefix(WIKI_PREFIX)}",
        "kbId": h.kb_id,
        "docId": None,
        "title": h.title,
        "score": round(h.score, 6),
        "relevance": round(h.relevance, 4),
        "snippet": h.content[:800],
        "source": source,
        "level": "wiki",
        "pageType": WIKI_PAGE_TYPES.get(h.wiki_type or "", "overview"),
        "wikiSlug": h.wiki_slug,
        "sourceDocIds": h.wiki_sources,
    }


async def _linked(caller: Caller, scope: list[str], page_id: str, seen: set[str]) -> list[dict]:
    """``linkedFrom``: pages a wiki page links to, or wiki pages built from a document."""

    if page_id.startswith(WIKI_PREFIX):
        page = await wiki_store.get_by_id(page_id.removeprefix(WIKI_PREFIX))
        if page is None or page["kb_id"] not in scope:
            return []
        targets = await wiki_store.get_many(page["kb_id"], page["out_links"])
        pages = list(targets.values())
    else:
        doc_id, _ = _split_page_id(page_id)
        doc = await _doc_in_scope(caller, scope, doc_id)
        if doc is None:
            return []
        pages = await wiki_store.pages_for_doc(doc["kb_id"], doc_id)
    out = []
    for page in pages:
        pid = _wiki_page_id(page)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(
            {
                "pageId": pid,
                "kbId": page["kb_id"],
                "docId": None,
                "title": page["title"],
                "score": 0.0,
                "relevance": 0.0,
                "snippet": wiki_links.plain(((page["summary"] + "\n") if page["summary"] else "") + page["content"][:600]),
                "source": "link",
                "level": "wiki",
                "pageType": WIKI_PAGE_TYPES.get(page["page_type"], "overview"),
                "wikiSlug": page["slug"],
                "sourceDocIds": page["source_doc_ids"],
            }
        )
    return out


@router.post("/kb_search")
async def kb_search(request: Request) -> JSONResponse:
    caller, scope = await _context(request, auth.CAP_RETRIEVE)
    body = await request.json()
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"results": [], "total": 0, "message": warning or "所选知识库都不在会话范围内"})
    page_types = set(body.get("pageTypes") or [])
    want_chunks = not page_types or bool(page_types & STRUCTURE_TYPES)
    want_wiki = not page_types or bool(page_types & WIKI_DA_TYPES)
    if not (want_chunks or want_wiki):
        return JSONResponse(
            {
                "results": [],
                "total": 0,
                "message": "当前知识服务索引 L1 结构化正文（structure_md）和 Wiki 页面（entity、concept、overview）",
            }
        )
    top_k = int(body.get("topK") or 20)
    hits = await retrieval.search(
        caller.tenant_id,
        kb_ids,
        str(body.get("query", "")),
        top_k=top_k,
        min_score=body.get("minScore"),
        include_chunks=want_chunks,
        include_wiki=want_wiki,
        wiki_limit=None if want_chunks else top_k,
    )
    results = []
    for h in hits:
        if h.is_wiki:
            if not page_types or WIKI_PAGE_TYPES.get(h.wiki_type or "", "overview") in page_types:
                results.append(_wiki_result(h))
            continue
        results.append(
            {
                "pageId": _page_id(h.document_id, "L1"),
                "kbId": h.kb_id,
                "docId": h.document_id,
                "title": h.title,
                "score": round(h.score, 6),
                "relevance": round(h.relevance, 4),
                "snippet": ((h.context_header + "\n") if h.context_header else "") + h.content[:800],
                "source": "fusion",
                "anchorId": h.anchor_id,
                "level": "L1",
                "pageType": "structure_md",
                "chunkId": h.chunk_id,
                "lineStart": h.line_start,
                "locators": h.source_locators,
            }
        )
    if body.get("linkedFrom"):
        seen = {r["pageId"] for r in results}
        results.extend(await _linked(caller, kb_ids, str(body["linkedFrom"]), seen))
    if warning:
        return JSONResponse({"results": results, "total": len(results), "message": warning})
    return JSONResponse(results)


def _da_anchor(anchor: dict) -> dict:
    return {"id": anchor["id"], "type": anchor["type"], "lineStart": anchor["line_start"],
            "preview": anchor["preview"], "sectionTitle": anchor["section_title"]}


@router.post("/expand")
async def expand(request: Request) -> JSONResponse:
    caller, scope = await _context(request, auth.CAP_RETRIEVE)
    body = await request.json()

    doc_ids = body.get("docIds")
    if doc_ids:
        results = []
        for doc_id in list(doc_ids)[:20]:
            doc = await _doc_in_scope(caller, scope, doc_id)
            if doc is None:
                results.append({"docId": doc_id, "error": "文档不存在或不在会话范围内"})
                continue
            l1 = retrieval.read_l1(doc)
            results.append(
                {
                    "docId": doc_id,
                    "filename": doc.get("file_name"),
                    "title": doc["title"],
                    "level": "L1",
                    "pageId": _page_id(doc_id, "L1"),
                    "summary": doc.get("summary"),
                    "outline": text_structure.outline(l1),
                    "tokenCount": _tokens(l1),
                }
            )
        return JSONResponse({"mode": "batch", "totalDocs": len(doc_ids), "results": results})

    level = body.get("targetLevel")
    doc_id = body.get("docId")
    if body.get("pageId"):
        doc_id, page_level = _split_page_id(str(body["pageId"]))
        level = level or page_level
    if not doc_id:
        return JSONResponse({"error": "需要 pageId、docId 或 docIds"})
    level = level if level in {"L0", "L1", "L2"} else "L1"
    doc = await _doc_in_scope(caller, scope, doc_id)
    if doc is None:
        return JSONResponse({"error": f"文档不存在或不在会话范围内: {doc_id}"})
    if doc["parse_status"] != "completed" and not doc.get("l1_key"):
        return JSONResponse({"error": f"文档尚未处理完成（状态：{doc['parse_status']}）"})

    base = {
        "docId": doc_id,
        "filename": doc.get("file_name"),
        "title": doc["title"],
        "level": level,
        "pageId": _page_id(doc_id, level),
        "childPages": [
            {"pageId": _page_id(doc_id, lv), "level": lv, "title": doc["title"]} for lv in ("L0", "L1", "L2") if lv != level
        ],
    }
    if level == "L0":
        content = doc.get("summary") or ""
        if not content:
            content = "[摘要尚未生成，以下为正文开头]\n" + retrieval.read_l1(doc)[:800]
        return JSONResponse({**base, "content": content, "tokenCount": _tokens(content)})
    if level == "L2":
        content = retrieval.read_l2(doc)
        return JSONResponse({**base, "content": content, "tokenCount": _tokens(content), "format": "json"})

    markdown = retrieval.read_l1(doc)
    heading = str(body["heading"]) if body.get("heading") else None
    limit = L1_PAGE_CHARS
    if body.get("tokenBudget"):
        limit = max(1000, int(float(body["tokenBudget"]) * 1.5))
    win = text_structure.window(markdown, doc_id, offset=int(body.get("offset") or 0), limit=limit, heading=heading)
    if win is None:
        return JSONResponse({"error": f'Section "{heading}" not found in page {base["pageId"]}'})
    content = win["content"]
    anchors = [_da_anchor(a) for a in win["anchors"]]
    payload = {**base, "content": content[:limit], "tokenCount": _tokens(content[:limit]), "anchors": anchors}
    if heading and len(content) > limit:
        # a heading returns its whole section; cap it like an offset window, with the anchors that point into it
        last_line = (anchors[0].get("lineStart") or 0) + content[:limit].count("\n") if anchors else 0
        payload["anchors"] = [a for a in anchors if (a.get("lineStart") or 0) <= last_line][:MAX_ANCHORS]
        payload["truncated"] = True
        payload["message"] = f"该节超过 {limit} 字，已截断；可去掉 heading 按 offset 分页读取"
    if not heading:
        payload.update(totalChars=win["total_chars"], offset=win["offset"], hasMore=win["has_more"])
        if "next_offset" in win:
            payload["nextOffset"] = win["next_offset"]
    return JSONResponse(payload)


def _file_type_label(ext: str) -> str:
    ext = (ext or "").lower()
    groups = {
        "PDF": {"pdf"},
        "Spreadsheet": {"xlsx", "xls", "csv", "tsv"},
        "Word": {"doc", "docx"},
        "Presentation": {"ppt", "pptx"},
        "Text": {"txt", "md", "markdown"},
        "Data": {"json", "xml", "yaml", "yml"},
        "Web": {"html", "htm", "mhtml"},
    }
    for label, exts in groups.items():
        if ext in exts:
            return label
    return ext.upper() or "Unknown"


@router.post("/wiki_browse")
async def wiki_browse(request: Request) -> JSONResponse:
    caller, scope = await _context(request, auth.CAP_RETRIEVE)
    body = await request.json()

    if str(body.get("pageId") or "").startswith(WIKI_PREFIX):
        page = await wiki_store.get_by_id(str(body["pageId"]).removeprefix(WIKI_PREFIX))
        if page is None or page["kb_id"] not in scope:
            return JSONResponse({"error": f"Page not found: {body['pageId']}"})
        related = await wiki_store.get_many(page["kb_id"], list({*page["out_links"], *page["in_links"]}))
        content = page["content"]
        return JSONResponse(
            {
                "id": _wiki_page_id(page),
                "kbId": page["kb_id"],
                "docId": None,
                "pageType": WIKI_PAGE_TYPES.get(page["page_type"], "overview"),
                "title": page["title"],
                "summary": page["summary"],
                "slug": page["slug"],
                "tokenCount": _tokens(content),
                "content": content[:L1_PAGE_CHARS],
                "truncated": len(content) > L1_PAGE_CHARS,
                "sourceDocIds": page["source_doc_ids"],
                "linksTo": [
                    {"pageId": _wiki_page_id(related[x]), "title": related[x]["title"]}
                    for x in page["out_links"]
                    if x in related
                ],
                "linkedFrom": [
                    {"pageId": _wiki_page_id(related[x]), "title": related[x]["title"]}
                    for x in page["in_links"]
                    if x in related
                ],
            }
        )

    if body.get("pageId"):
        doc_id, level = _split_page_id(str(body["pageId"]))
        doc = await _doc_in_scope(caller, scope, doc_id)
        if doc is None:
            return JSONResponse({"error": f"Page not found: {body['pageId']}"})
        level = level or "L1"
        if level == "L0":
            content = doc.get("summary") or ""
        elif level == "L2":
            content = retrieval.read_l2(doc)
        else:
            content = retrieval.read_l1(doc)
        return JSONResponse(
            {
                "id": _page_id(doc_id, level),
                "kbId": doc["kb_id"],
                "docId": doc_id,
                "filename": doc.get("file_name"),
                "pageType": {"L0": "abstract", "L1": "structure_md", "L2": "fulltext"}[level],
                "title": doc["title"],
                "tokenCount": _tokens(content),
                "content": content[:L1_PAGE_CHARS],
                "truncated": len(content) > L1_PAGE_CHARS,
            }
        )

    kb_id = body.get("kbId")
    if not kb_id:
        return JSONResponse({"error": "需要 kbId 或 pageId"})
    if kb_id not in scope:
        return JSONResponse({"error": f"知识库不在会话范围内: {kb_id}"})
    docs = await retrieval.list_documents(caller.tenant_id, kb_id, limit=2000)

    if body.get("listDocuments"):
        categories: dict[str, dict] = {}
        for d in docs:
            folder = (d.get("folder_path") or "").replace("\\", "/").strip("/")
            if folder:
                parts = [p for p in folder.split("/") if p]
                cat = "/".join(parts[-2:])
            else:
                cat = _file_type_label(d.get("file_type") or "")
            entry = categories.setdefault(cat, {"type": cat, "count": 0, "sampleFiles": []})
            entry["count"] += 1
            if len(entry["sampleFiles"]) < 3:
                entry["sampleFiles"].append(d.get("file_name") or d["title"])
        return JSONResponse(
            {
                "kbId": kb_id,
                "totalDocuments": len(docs),
                "categories": list(categories.values()),
                "documents": [
                    {
                        "docId": d["id"],
                        "filename": d.get("file_name") or d["title"],
                        "title": d["title"],
                        "folderPath": d.get("folder_path") or None,
                        "fileType": d.get("file_type"),
                        "status": d["parse_status"],
                        "abstractPageId": _page_id(d["id"], "L0") if d.get("summary") else None,
                        "abstract": d.get("summary"),
                    }
                    for d in docs
                ],
            }
        )

    page_type = body.get("pageType")
    pages = []
    for d in docs:
        if d["parse_status"] != "completed":
            continue
        for level, ptype, tokens in (
            ("L0", "abstract", _tokens(d.get("summary") or "")),
            ("L1", "structure_md", _tokens("x" * int(d.get("l1_chars") or 0))),
        ):
            if page_type and page_type != ptype:
                continue
            pages.append(
                {"id": _page_id(d["id"], level), "docId": d["id"], "pageType": ptype, "title": d["title"], "tokenCount": tokens}
            )
    if not page_type or page_type in WIKI_DA_TYPES:
        rows = await db.pool().fetch(
            "SELECT id, slug, title, page_type, summary, length(content) AS chars FROM wiki_pages"
            " WHERE kb_id = $1 AND deleted_at IS NULL ORDER BY page_type, title LIMIT 2000",
            kb_id,
        )
        for row in rows:
            ptype = WIKI_PAGE_TYPES.get(row["page_type"], "overview")
            if page_type and page_type != ptype:
                continue
            pages.append(
                {
                    "id": f"{WIKI_PREFIX}{row['id']}",
                    "docId": None,
                    "pageType": ptype,
                    "title": row["title"],
                    "summary": row["summary"],
                    "slug": row["slug"],
                    "tokenCount": _tokens("x" * int(row["chars"] or 0)),
                }
            )
    return JSONResponse({"kbId": kb_id, "total": len(pages), "pages": pages})


@router.post("/doc_grep")
async def doc_grep(request: Request) -> JSONResponse:
    caller, scope = await _context(request, auth.CAP_RETRIEVE)
    body = await request.json()
    pattern = str(body.get("pattern") or "")
    if not pattern:
        return JSONResponse({"error": "pattern 不能为空"})
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"pattern": pattern, "total": 0, "matches": [], "message": warning})
    max_lines = max(1, min(int(body.get("maxLinesPerPage") or 10), 100))
    try:
        result = await retrieval.grep(
            caller.tenant_id,
            kb_ids,
            pattern,
            max_results=max(1, min(int(body.get("maxResults") or 50), 200)),
            max_lines_per_doc=max_lines,
        )
    except text_structure.PatternError as exc:
        return JSONResponse({"error": f"查找条件无效：{exc}"})
    result.update(
        {
            "totalPages": result["totalDocuments"],
            "maxLinesPerPage": max_lines,
            "total": result["totalMatches"],
        }
    )
    if warning:
        result["message"] = warning
    return JSONResponse(result)


@router.post("/reprocess_document")
async def reprocess_document(request: Request) -> JSONResponse:
    caller, scope = await _context(request, auth.CAP_REPROCESS)
    body = await request.json()
    doc_id = str(body.get("docId") or "")
    doc = await _doc_in_scope(caller, scope, doc_id)
    if doc is None:
        return JSONResponse({"error": f"文档不存在或不在会话范围内: {doc_id}"})
    try:
        row = await documents.reprocess(caller.tenant_id, doc_id, scope)
    except documents.DocumentError as exc:
        return JSONResponse(error_body(exc.code, exc.message))
    return JSONResponse(
        {"kbId": row["kb_id"], "docId": doc_id, "status": "queued", "attempt": row["attempt"], "message": "已重新排队处理"}
    )


def _op(name: str, summary: str, properties: dict, required: list[str]) -> dict:
    headers = [
        {"name": h, "in": "header", "required": h != "X-Kb-Scope", "schema": {"type": "string"}}
        for h in ("X-Session-Id", "X-Tenant-Id", "X-Kb-Scope", "X-Request-Id")
    ]
    return {
        "post": {
            "operationId": name,
            "summary": summary,
            "description": summary,
            "parameters": headers,
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": properties, "required": required}
                    }
                },
            },
            "responses": {"200": {"description": "tool result", "content": {"application/json": {"schema": {}}}}},
            "x-max-duration": {"defaultMs": 120000, "maxMs": 120000},
        }
    }


def tool_spec(base_url: str = "") -> dict:
    s, n, b, a = {"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}
    paths = {
        "/v1/tools/kb_search": _op(
            "kb_search",
            "语义搜索知识库（向量 + BM25 + 生成问题混合检索并重排）。返回 L1 结构化正文命中（含 pageId/docId/anchorId"
            " 与原文定位）和 Wiki 页面（pageType 为 entity/concept/overview）；minScore 与 relevance（0-1）比较；"
            "linkedFrom 追加某页面链接到的 Wiki 页面。",
            {"query": s, "kbIds": a, "topK": n, "linkedFrom": s, "pageTypes": a, "minScore": n},
            ["query"],
        ),
        "/v1/tools/expand": _op(
            "expand",
            "逐层展开文档内容（L0 摘要 / L1 结构化 Markdown / L2 解析定位数据）。L1 返回 anchors，锚点 ID 须逐字复制。",
            {"pageId": s, "docId": s, "docIds": a, "kbId": s, "targetLevel": s, "format": s, "heading": s, "tokenBudget": n, "offset": n},
            [],
        ),
        "/v1/tools/doc_grep": _op(
            "doc_grep",
            "在知识库文档的 L1 正文中按正则或字面量逐行查找，穷尽扫描。",
            {"pattern": s, "kbIds": a, "pageTypes": a, "maxResults": n, "maxLinesPerPage": n},
            ["pattern"],
        ),
        "/v1/tools/wiki_browse": _op(
            "wiki_browse",
            "浏览知识库：listDocuments=true 返回全部文档与 L0 摘要；pageId 返回页面内容（wiki: 开头的是 Wiki 页面，"
            "带链接关系）；仅 kbId 列出页面，可按 pageType（abstract/structure_md/entity/concept/overview）过滤。",
            {"pageId": s, "kbId": s, "pageType": s, "listDocuments": b},
            [],
        ),
        "/v1/tools/reprocess_document": _op(
            "reprocess_document",
            "重新解析并索引一个文档（异步排队）。",
            {"kbId": s, "docId": s, "processor": s, "force": b},
            ["docId"],
        ),
    }
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "kb-service DA tools", "version": __version__},
        "paths": paths,
    }
    if base_url:
        spec["servers"] = [{"url": base_url}]
    return spec


@router.get("/openapi.json")
async def openapi_spec() -> dict:
    return tool_spec()
