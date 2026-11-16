"""DA tool contract (``POST /v1/tools/*``) for DA-java's knowledge-service slot.

A call whose scope lies on one backend that implements the tools itself
(feature ``tools.da``, e.g. kb-service) is forwarded unchanged. Otherwise the
gateway answers from the knowledge-provider/v1 core API, in the same result
shapes (pageId ``{docId}:L0|L1|L2``, anchors ``{docId}:{type}:{index}``;
Wiki pages of non-native backends are ``wiki:{kbId}/{slug}``).

Either way the gateway may add notes (``kb_gateway.notes``): knowledge bases of
the session that were not searched because their backend is unavailable
(``unavailableKbs`` and a ``message``), grep matches that were not returned and
documents without any readable text (``noText``), and document text that carries
instructions for the AI reading it (``embeddedInstructions``). Notes only add fields.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import __version__, notes, ops
from ..auth import CAP_REPROCESS, CAP_RETRIEVE, AuthError, parse_scope_header
from ..config import get_settings
from ..deps import caller, registry
from ..registry import Backend, UpstreamError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/tools")

REQUIRED_HEADERS = ("x-session-id", "x-tenant-id", "x-request-id")
L1_PAGE_CHARS = 30000
MAX_ANCHORS = 200
DEFAULT_GREP_LINES = 10
DEFAULT_GREP_RESULTS = 50
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


Annotate = Callable[[Any], Awaitable[Any]]
Handler = Callable[[Request], Awaitable[Response]]


def _flags_instructions(handler: Handler) -> Handler:
    """Point out document text with embedded instructions in the handler's JSON result (notes.flag_instructions)."""

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        response = await handler(request)
        if response.status_code != 200 or not response.body:
            return response
        try:
            data = json.loads(response.body)
        except ValueError:
            return response
        flagged = notes.flag_instructions(data)
        return response if flagged is None else JSONResponse(flagged)

    return wrapper


async def _native(backend: Backend, request: Request, scope: list[str], body: dict,
                  annotate: Annotate | None = None) -> Response:
    """Forward to the backend's own tool; ``annotate`` may return a replacement for the JSON result (None: as is)."""

    headers = {h: request.headers[h] for h in ("x-session-id", "x-request-id") if h in request.headers}
    headers["X-Kb-Scope"] = ",".join(_subset(scope, backend))
    response = await backend.send("POST", request.url.path, tools=True, headers=headers, json=body)
    if annotate is not None and response.content:
        try:
            data = response.json()
        except ValueError:
            data = None
        changed = await annotate(data) if data is not None else None
        if changed is not None:
            return JSONResponse(changed)
    return Response(response.content, status_code=response.status_code, media_type="application/json")


def _native_ok(backend: Backend) -> bool:
    return backend.healthy and backend.has("tools.da")


def _with_down_kbs(result: Any, scope: list[str], missed: list[dict] | None = None) -> Any:
    """Name the session's knowledge bases that were not searched: those of ``missed`` and those on unavailable backends.

    A bare list (kb_search) becomes ``{results, total, ...}``, the shape DA's contract has for results with a message.
    Returns None when there is nothing to add.
    """

    down = notes.merge_refs(missed or [], notes.down_in_scope(registry(), scope))
    if not down:
        return None
    if isinstance(result, list):
        result = {"results": result, "total": len(result)}
    if not isinstance(result, dict):
        return None
    result["unavailableKbs"] = notes.merge_refs(result.get("unavailableKbs") or [], down)
    notes.add_message(result, notes.unavailable_message(result["unavailableKbs"]))
    return result


def _tool_error(exc: UpstreamError, what: str, kb_ids: list[str] | None = None) -> dict:
    """A tool result for a call the backend did not answer: unavailable (which is not "not found"), or why it failed."""

    backend = registry().by_id.get(exc.backend)
    label = backend.config.label if backend else "知识后端"
    if notes.is_down(exc):
        error = f"{what}所在的知识后端 {label} 当前不可用，暂时无法读取（不是不存在）"
    else:
        error = f"{what}没有读到（{label} {notes.explain(exc.code, exc.message)}）"
    out: dict[str, Any] = {"error": error + "。回答时要告诉用户这部分没有查到", "code": exc.code}
    if notes.is_down(exc):
        out["backendUnavailable"] = label
    if kb_ids:
        out["unavailableKbs"] = [notes.exc_ref(registry(), kb, exc) for kb in kb_ids]
    return out


def _pattern_error(pattern: str, exc: UpstreamError) -> dict:
    return {"pattern": pattern, "total": 0, "matches": [], "error": f"查找条件无效：{exc.message}", "code": exc.code}


async def _mark_no_text(items: list[dict]) -> bool:
    """Mark the items (expand results) whose document is processed; True when any was marked.

    A document still processing or failed is empty too, so the state is read first; one whose state
    cannot be read is left alone.
    """

    async def completed(doc_id: str) -> bool:
        try:
            doc = await ops.get_document(registry(), doc_id)
        except UpstreamError:
            return False
        return bool(doc) and doc.get("parse_status") == "completed"

    done = await asyncio.gather(*(completed(str(item["docId"])) for item in items))
    for item, ok in zip(items, done):
        if ok:
            notes.mark_no_text(item)
    return any(done)


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


async def _search_group(backend: Backend, request: Request, scope: list[str], ids: list[str],
                        body: dict) -> tuple[list[dict], list[str]]:
    """One backend's part of a cross-backend kb_search, with what the backend says about it (e.g. a degraded leg).

    On the short timeout, so one slow backend cannot sink the call.
    """

    timeout = get_settings().search_timeout_s
    if _native_ok(backend):
        headers = {h: request.headers[h] for h in ("x-session-id", "x-request-id") if h in request.headers}
        headers["X-Kb-Scope"] = ",".join(_subset(scope, backend))
        data = await backend.json("POST", "/v1/tools/kb_search", tools=True, headers=headers, json={**body, "kbIds": ids},
                                  timeout=timeout)
        if isinstance(data, list):
            return data, []
        data = data or {}
        if data.get("error"):
            # kb-service answers a search it cannot run (e.g. no embedding model) with 200 {error, code}
            raise UpstreamError(503, str(data.get("code") or "backend_error"), str(data["error"]), backend.id)
        return data.get("results", []), [str(data["message"])] if data.get("message") else []
    page_types = set(body.get("pageTypes") or [])
    want_chunks = not page_types or bool(page_types & STRUCTURE_TYPES)
    want_wiki = (not page_types or bool(page_types & WIKI_DA_TYPES)) and backend.has("wiki.read")
    top_k = int(body.get("topK") or 20)
    outcome = await ops.search(
        registry(), ids,
        {"query": str(body.get("query") or ""), "min_score": body.get("minScore"), "include_wiki": want_wiki},
        max(1, min(top_k, ops.SEARCH_MAX_TOP_K)), fan_out=True,
    )
    results = []
    for hit in outcome.hits:
        is_wiki = hit.get("chunk_type") == "wiki_page"
        if not (want_wiki if is_wiki else want_chunks):
            continue
        result = _result(hit)
        if is_wiki and page_types and result["pageType"] not in page_types:
            continue
        results.append(result)
    return results, outcome.warnings


@router.post("/kb_search")
@_flags_instructions
async def kb_search(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"results": [], "total": 0, "message": warning or "所选知识库都不在会话范围内"})
    groups = [(registry().by_id[bid], ids) for bid, ids in registry().group(kb_ids).items()]
    if len(groups) == 1 and _native_ok(groups[0][0]) and not warning:
        backend, ids = groups[0]

        async def down_note(data: Any) -> Any:
            return _with_down_kbs(data, scope)

        try:
            return await _native(backend, request, scope, body, annotate=down_note)
        except UpstreamError as exc:
            if not notes.reportable(exc):
                raise
            return JSONResponse(_with_down_kbs([], scope, [notes.exc_ref(registry(), kb, exc) for kb in ids]))
    top_k = int(body.get("topK") or 20)
    outcomes = await asyncio.gather(
        *(_search_group(b, request, scope, ids, body) for b, ids in groups), return_exceptions=True
    )
    lists, missed, said = [], [], [warning] if warning else []
    for (backend, ids), outcome in zip(groups, outcomes):
        if isinstance(outcome, Exception):
            logger.warning("kb_search on %s failed: %s", backend.id, getattr(outcome, "message", outcome))
            missed.extend(notes.exc_ref(registry(), kb, outcome) for kb in ids)
        else:
            lists.append(outcome[0])
            said.extend(f"{backend.config.label}：{w}" if len(groups) > 1 else w for w in outcome[1])
    results = ops.rrf_merge(lists, top_k) if lists else []
    if body.get("linkedFrom"):
        seen = {r["pageId"] for r in results}
        results.extend(await _linked_generic(scope, str(body["linkedFrom"]), seen))
    payload: Any = {"results": results, "total": len(results), "message": "；".join(said)} if said else results
    annotated = _with_down_kbs(payload, scope, missed)
    return JSONResponse(payload if annotated is None else annotated)


# ---- expand ---------------------------------------------------------------


def _da_anchors(win: dict) -> list[dict]:
    return [{"id": a["id"], "type": a["type"], "lineStart": a["line_start"], "preview": a["preview"],
             "sectionTitle": a["section_title"]} for a in win.get("anchors") or []]


async def _expand_batch(scope: list[str], doc_ids: list[str]) -> dict:
    async def one(doc_id: str) -> dict:
        try:
            return await _expand_outline(scope, doc_id)
        except UpstreamError as exc:
            if notes.reportable(exc):
                return {"docId": doc_id, **_tool_error(exc, f"文档 {doc_id} ")}
            return {"docId": doc_id, "error": exc.message}

    results = await asyncio.gather(*(one(d) for d in doc_ids[:20]))
    return {"mode": "batch", "totalDocs": len(doc_ids), "results": list(results)}


async def _expand_outline(scope: list[str], doc_id: str) -> dict:
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return {"docId": doc_id, "error": "文档不存在或不在会话范围内"}
    win = await ops.structure(registry(), doc_id, limit=0)
    out = {
        "docId": doc_id, "filename": doc.get("file_name"), "title": doc.get("title"), "level": "L1",
        "pageId": f"{doc_id}:L1", "summary": doc.get("summary"), "outline": win["outline"],
        "tokenCount": _tokens("x" * int(win["total_chars"])),
    }
    if notes.doc_has_no_text(doc):
        notes.mark_no_text(out)
    return out


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
            content = notes.L0_HEAD_MARK + "\n" + head["content"]
        payload = {**base, "content": content, "tokenCount": _tokens(content)}
        if notes.doc_has_no_text(doc):
            # a summary written from an empty body describes nothing that is in the file
            notes.mark_no_text(payload)
        return payload
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
        if notes.doc_has_no_text(doc):
            notes.mark_no_text(payload)
    return payload


@router.post("/expand")
@_flags_instructions
async def expand(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    doc_ids = [str(d) for d in body.get("docIds") or []]
    targets = doc_ids or [_split_page_id(str(body.get("pageId") or body.get("docId") or ""))[0]]
    owners = {registry().route(d).id for d in targets if d}
    heading = bool(body.get("heading"))

    async def no_text(data: Any) -> Any:
        return data if await _mark_no_text(notes.expand_candidates(data, heading=heading)) else None

    try:
        if len(owners) == 1:
            backend = registry().by_id[owners.pop()]
            if _native_ok(backend):
                return await _native(backend, request, scope, body, annotate=no_text)
        result = await (_expand_batch(scope, doc_ids) if doc_ids else _expand_one(scope, body))
    except UpstreamError as exc:
        if not notes.reportable(exc):
            raise
        return JSONResponse(_tool_error(exc, "文档" if len(targets) != 1 else f"文档 {targets[0]} "))
    await no_text(result)
    return JSONResponse(result)


# ---- doc_grep -------------------------------------------------------------


async def _grep_notes(result: Any, scope: list[str], max_lines: int, max_results: int) -> Any:
    """Truncation notes and the session's knowledge bases on unavailable backends; None when nothing was added."""

    if not isinstance(result, dict):
        return None
    changed = notes.annotate_grep(result, max_lines=max_lines, max_results=max_results,
                                  lines_cap=ops.GREP_MAX_LINES_PER_DOC, results_cap=ops.GREP_MAX_RESULTS)
    down = _with_down_kbs(result, scope) if "error" not in result else None
    return result if changed or down is not None else None


def _grep_failed(pattern: str, scope: list[str], kb_ids: list[str], exc: UpstreamError) -> dict:
    """No backend answered the grep: a refused pattern, or the knowledge bases that were not searched and why."""

    if exc.code in notes.PATTERN_CODES:
        return _pattern_error(pattern, exc)
    if not notes.reportable(exc):
        raise exc
    missed = [notes.exc_ref(registry(), kb, exc) for kb in kb_ids]
    return _with_down_kbs({"pattern": pattern, "total": 0, "matches": []}, scope, missed)


@router.post("/doc_grep")
@_flags_instructions
async def doc_grep(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    pattern = str(body.get("pattern") or "")
    if not pattern:
        return JSONResponse({"error": "pattern 不能为空"})
    kb_ids, warning = _narrow(scope, body.get("kbIds"))
    if not kb_ids:
        return JSONResponse({"pattern": pattern, "total": 0, "matches": [], "message": warning})
    max_lines = max(1, min(int(body.get("maxLinesPerPage") or DEFAULT_GREP_LINES), ops.GREP_MAX_LINES_PER_DOC))
    max_results = max(1, min(int(body.get("maxResults") or DEFAULT_GREP_RESULTS), ops.GREP_MAX_RESULTS))
    owners = {registry().route(kb).id for kb in kb_ids}
    if len(owners) == 1 and not warning:
        backend = registry().by_id[next(iter(owners))]
        if _native_ok(backend):
            try:
                return await _native(backend, request, scope, {**body, "maxResults": max_results, "maxLinesPerPage": max_lines},
                                     annotate=lambda data: _grep_notes(data, scope, max_lines, max_results))
            except UpstreamError as exc:
                return JSONResponse(_grep_failed(pattern, scope, kb_ids, exc))
    try:
        result = ops.grep_view(await ops.grep(registry(), kb_ids, pattern, max_results=max_results,
                                              max_lines_per_doc=max_lines))
    except UpstreamError as exc:
        return JSONResponse(_grep_failed(pattern, scope, kb_ids, exc))
    result.update({"totalPages": result["totalDocuments"], "maxLinesPerPage": max_lines, "total": result["totalMatches"]})
    if warning:
        result["message"] = warning
    unsupported = notes.unsupported_message(registry(), kb_ids, result.get("skipped_backends") or [], "逐行查找")
    if unsupported:
        notes.add_message(result, unsupported)
    notes.annotate_grep(result, max_lines=max_lines, max_results=max_results,
                        lines_cap=ops.GREP_MAX_LINES_PER_DOC, results_cap=ops.GREP_MAX_RESULTS)
    _with_down_kbs(result, scope, notes.on_failed(registry(), kb_ids, result.get("failed_backends") or []))
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
    page = {
        "id": f"{doc_id}:{level}", "kbId": doc["kb_id"], "docId": doc_id, "filename": doc.get("file_name"),
        "pageType": LEVEL_TYPES[level], "title": doc.get("title"), "tokenCount": _tokens(content),
        "content": content[:L1_PAGE_CHARS], "truncated": len(content) > L1_PAGE_CHARS,
    }
    if doc.get("parse_status") == "completed" and notes.page_candidate(page):
        notes.mark_no_text(page)
    return page


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


def _documents_view(kb_id: str, docs: list[dict]) -> dict:
    out: dict[str, Any] = {
        "kbId": kb_id, "totalDocuments": len(docs), "categories": _categories(docs),
        "documents": [
            {"docId": d["id"], "filename": d.get("file_name") or d.get("title"), "title": d.get("title"),
             "folderPath": d.get("folder_path") or None, "fileType": d.get("file_type"),
             "status": d.get("parse_status"), "abstractPageId": f"{d['id']}:L0" if d.get("summary") else None,
             "abstract": d.get("summary"), **({"noText": True} if notes.doc_has_no_text(d) else {})}
            for d in docs
        ],
    }
    empty = [d.get("file_name") or d.get("title") or d["id"] for d in docs if notes.doc_has_no_text(d)]
    if empty:
        names = "、".join(empty[:notes.MAX_NAMED]) + (f" 等 {len(empty)} 个" if len(empty) > notes.MAX_NAMED else "")
        notes.add_message(out, f"以下文档没有可识别的文字内容（扫描件或纯图片，没有做文字识别），检索、doc_grep 和 expand 都查不到"
                               f"它们的内容：{names}。问到这些文件时如实说明查不到，不要根据文件名或摘要猜测")
    return out


async def _browse(request: Request, scope: list[str], body: dict) -> Response:
    page_id = str(body.get("pageId") or "")
    if page_id:
        target = page_id.removeprefix(WIKI_PREFIX)
        parsed = _split_wiki_id(page_id) if page_id.startswith(WIKI_PREFIX) else None
        owner = registry().route(parsed[0] if parsed else _split_page_id(target)[0])
        if _native_ok(owner) and parsed is None:

            async def no_text(data: Any) -> Any:
                page = notes.page_candidate(data)
                return data if page is not None and await _mark_no_text([page]) else None

            return await _native(owner, request, scope, body, annotate=no_text)
        return JSONResponse(await (_wiki_page(scope, page_id) if parsed else _doc_page(scope, page_id)))
    kb_id = body.get("kbId")
    if not kb_id:
        return JSONResponse({"error": "需要 kbId 或 pageId"})
    if kb_id not in scope:
        return JSONResponse({"error": f"知识库不在会话范围内: {kb_id}"})
    owner = registry().route(kb_id)
    if _native_ok(owner):

        async def down_note(data: Any) -> Any:
            return _with_down_kbs(data, scope)

        return await _native(owner, request, scope, body, annotate=down_note)
    if body.get("listDocuments"):
        result = _documents_view(kb_id, await ops.list_documents(registry(), kb_id))
    else:
        result = await _kb_pages(kb_id, body.get("pageType"))
    _with_down_kbs(result, scope)
    return JSONResponse(result)


@router.post("/wiki_browse")
@_flags_instructions
async def wiki_browse(request: Request) -> Response:
    scope = _context(request, CAP_RETRIEVE)
    body = await request.json()
    try:
        return await _browse(request, scope, body)
    except UpstreamError as exc:
        if not notes.reportable(exc):
            raise
        kb_id = None if body.get("pageId") else body.get("kbId")
        if kb_id:
            name = registry().route(str(kb_id)).kb_names.get(str(kb_id))
            return JSONResponse(_tool_error(exc, f"知识库 {name or kb_id} ", [str(kb_id)]))
        return JSONResponse(_tool_error(exc, f"页面 {body.get('pageId')} "))


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
    note = _processor_note(backend, body)
    if _native_ok(backend):

        async def say_processor(data: Any) -> Any:
            if note and isinstance(data, dict) and "error" not in data:
                notes.add_message(data, note)
                return data
            return None

        return await _native(backend, request, scope, body, annotate=say_processor)
    doc = await _doc_in_scope(scope, doc_id)
    if doc is None:
        return JSONResponse({"error": f"文档不存在或不在会话范围内: {doc_id}"})
    try:
        await backend.json("POST", f"/v1/documents/{doc_id}/reprocess")
    except UpstreamError as exc:
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}})
    message = "已重新排队处理" + (f"；{note}" if note else "")
    return JSONResponse({"kbId": doc["kb_id"], "docId": doc_id, "status": "queued", "message": message})


def _processor_note(backend: Backend, body: dict) -> str | None:
    """The DA tool offers a processor choice; no backend behind the gateway takes one per request: say so."""

    processor = str(body.get("processor") or "").strip()
    if not processor:
        return None
    return (f"{backend.config.label} 不能按请求指定处理器（{processor}），本次按该知识库配置的默认方式重新处理；"
            f"回答里不要说用了 {processor}")


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
            "和 Wiki 页面（pageType 为 entity/concept/overview）；linkedFrom 追加某 Wiki 页面链接到的页面。"
            "有知识库因所在后端不可用没有检索时，返回 {results, total, message, unavailableKbs}，回答要告诉用户这些库没有查到。",
            {"query": s, "kbIds": a, "topK": n, "linkedFrom": s, "pageTypes": a, "minScore": n}, ["query"]),
        "/v1/tools/expand": _op(
            "expand", "逐层展开文档内容（L0 摘要 / L1 结构化 Markdown / L2 解析定位数据）。L1 返回 anchors，锚点 ID 须逐字复制。"
            "noText=true 表示文件没有可识别的文字（扫描件或图片），内容查不到，如实说明。",
            {"pageId": s, "docId": s, "docIds": a, "kbId": s, "targetLevel": s, "format": s, "heading": s,
             "tokenBudget": n, "offset": n}, []),
        "/v1/tools/doc_grep": _op(
            "doc_grep", "在知识库文档的 L1 正文中按正则或字面量逐行查找，穷尽扫描。每篇最多返回 maxLinesPerPage 行（默认 10，"
            "最大 100）；某篇被截断时该篇 truncated=true，message 写明还有多少处没有返回，按提示调大参数或缩小范围再查。",
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
