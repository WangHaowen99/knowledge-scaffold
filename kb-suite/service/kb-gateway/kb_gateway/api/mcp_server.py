"""MCP server (streamable HTTP) for the Swarm main agent and Loop Forest.

The seven tools and text formats of kb-service's MCP plus show_source, answered across all
backends. Callers authenticate with a retrieve key and may confine a
connection to some knowledge bases with ``X-KB-Scope``. Results carry the
gateway's notes (``kb_gateway.notes``): knowledge bases not searched because
their backend is unavailable, grep matches not returned, files without text.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import json
from importlib import resources
from typing import Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .. import notes, ops
from ..auth import CAP_RETRIEVE, AuthError, Principal, check_scope, extract_token, narrow, parse_scope_header
from ..deps import registry, state
from ..registry import UpstreamError

_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("kbgw_mcp_principal", default=None)

INSTRUCTIONS = (
    "知识库工具。先用 list_knowledge_bases 看有哪些知识库，用 search_knowledge 检索相关片段，"
    "需要上下文时用 read_document 按顺序阅读文档，精确查找原文用 grep_documents。"
    "开启了 Wiki 的知识库还有大模型整理好的实体、概念和文档摘要页：要概览或跨文档综合时先用 search_wiki，"
    "再用 read_wiki_page 读全文并顺着 [[链接]] 读相关页面；Wiki 是二次整理，结论要回到原文片段核对出处。"
    "回答时引用片段末尾的出处（文档标题、章节、页码）；用户要看某条出处的原文时用 show_source 展示出处卡片。"
    "回答里提到知识库个数时，直接引用 list_knowledge_bases 结果里的 total 和各后端的 count，不要自己数；"
    "用户要求列出知识库时，按后端分组逐个写出全部库名，不要只举例或只给个数。"
    "工具结果说某些知识库所在后端不可用、没有检索时，回答要告诉用户这些库没有查到；"
    "说文件没有可识别文字时，如实说明查不到，不要猜测。"
)
# MCP Apps (io.modelcontextprotocol/ui): hosts that support it render show_source's result as a card
SOURCE_CARD_URI = "ui://kb-service/source"
MCP_APP_MIME_TYPE = "text/html;profile=mcp-app"
CONTEXT_CHARS = 600
WIKI_PAGE_CHARS = 12000
# list_knowledge_bases: at most this many knowledge bases are listed (the counts always cover all of them),
# each description cut to this length; worst case about 200 x 400 characters
LIST_MAX_KBS = 200
LIST_DESCRIPTION_CHARS = 160
GREP_MAX_LINES = 100
# a tool call that outlives the MCP client's own timeout breaks some clients' sessions for good (Swarm 0.2.7
# does not reconnect, D44): every tool answers within this budget, with a note when it had to stop
TOOL_BUDGET_S = 25.0


def _within_budget(tool: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    @functools.wraps(tool)
    async def wrapper(*args, **kwargs) -> str:
        try:
            return await asyncio.wait_for(tool(*args, **kwargs), TOOL_BUDGET_S)
        except asyncio.TimeoutError:
            return (f"这次查询超过 {int(TOOL_BUDGET_S)} 秒没有完成，已停止，结果不完整。"
                    "可以缩小知识库范围或换更具体的查询再试；回答时要告诉用户这次没有查到。")

    return wrapper


def _make_server() -> FastMCP:
    kwargs = dict(name="kb-service", instructions=INSTRUCTIONS, stateless_http=True, json_response=True)
    try:
        from mcp.server.transport_security import TransportSecuritySettings

        kwargs["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    except ImportError:
        pass
    server = FastMCP(**kwargs)
    server.settings.streamable_http_path = "/"
    return server


mcp = _make_server()


def _caller() -> Principal:
    principal = _principal.get()
    if principal is None:
        raise AuthError(401, "missing principal")
    return principal


def _scope(kb_ids: list[str] | None) -> list[str]:
    """Requested ids (checked against the connection scope) or the connection scope ([] = all)."""

    principal = _caller()
    return check_scope(principal, kb_ids) if kb_ids else list(principal.kb_allowlist)


def _location(locators: list) -> str:
    parts = []
    for loc in (locators or [])[:3]:
        if loc.get("page"):
            parts.append(f"第{loc['page']}页")
        elif loc.get("sheet"):
            rows = f"{loc.get('row_start', '')}-{loc.get('row_end', '')}行" if loc.get("row_start") else ""
            parts.append(f"{loc['sheet']} {rows}".strip())
        elif loc.get("slide"):
            parts.append(f"第{loc['slide']}页幻灯片")
    return "、".join(dict.fromkeys(parts))


def _error(exc: UpstreamError, scope: list[str] | None = None) -> str:
    """The tool result for a failed call: why it failed (by error code), and which knowledge bases it concerns."""

    if exc.code in notes.PATTERN_CODES:
        return f"查找条件无效：{exc.message}"
    if scope and notes.reportable(exc):
        return notes.unavailable_message([notes.exc_ref(registry(), kb, exc) for kb in scope])
    backend = registry().by_id.get(exc.backend)
    label = backend.config.label if backend else "知识服务"
    if notes.is_down(exc):
        return f"{label} 当前不可用，这部分暂时查不到（不是不存在），回答时要告诉用户没有查到。（{exc.message}）"
    return f"{label} {notes.explain(exc.code, exc.message)}"


def _lead(failed: list[dict], warnings: list[str], scope: list[str]) -> str:
    """What goes in front of a search result: the backends' warnings and the knowledge bases not searched."""

    lines = [f"注意：{w}" for w in warnings]
    if failed:
        lines.append("（" + _missed(scope, failed) + "）")
    return "\n\n".join(lines) + ("\n\n" if lines else "")


def _missed(scope: list[str], failed: list[dict]) -> str:
    """The note for backends that failed in a call over ``scope`` ([] = every knowledge base)."""

    if not failed:
        return ""
    refs = notes.on_failed(registry(), scope, failed) if scope else []
    return notes.unavailable_message(refs) if refs else notes.backends_message(registry(), failed)


def _kb_label(kb_id: str) -> str:
    """"知识库名（kb_id，后端）": which knowledge base and backend a hit came from, so answers name it right."""

    backend = registry().route(kb_id)
    name = backend.kb_names.get(kb_id)
    return f"{name}（{kb_id}，{backend.config.label}）" if name else f"{kb_id}（{backend.config.label}）"


def _kb_entry(kb: dict) -> dict:
    description = kb.get("description") or ""
    if len(description) > LIST_DESCRIPTION_CHARS:
        description = description[:LIST_DESCRIPTION_CHARS] + "…"
    return {"id": kb["id"], "name": kb.get("name"), "description": description,
            "documents": int((kb.get("stats") or {}).get("completed") or 0)}


@mcp.tool()
@_within_budget
async def list_knowledge_bases() -> str:
    """列出可用的知识库，按所在知识后端分组（id、名称、说明、已完成的文档数）。

    结果开头的 summary、total 和每个后端的 count 是准确的个数：回答里提到知识库个数时直接引用这些数字，不要自己数。
    用户要求列出知识库时，按后端分组逐个写出全部库名（说明可省略），不要只举例、用“等”省略或只给个数。
    unavailable_backends 里的知识后端当前不可用，它们的知识库不在列表里、也检索不到，回答时要说明。
    """

    principal = _caller()
    items, unavailable = await ops.list_kbs(registry())
    if principal.kb_allowlist:
        items = [kb for kb in items if kb["id"] in principal.kb_allowlist]
    groups: dict[str, list[dict]] = {}
    for kb in items:
        groups.setdefault(kb["backend"]["label"], []).append(kb)
    counts = "、".join(f"{label} {len(kbs)} 个" for label, kbs in groups.items())
    summary = f"共 {len(items)} 个可用的知识库" + (f"：{counts}" if counts else "") + "。"
    room = LIST_MAX_KBS
    backends = []
    for label, kbs in groups.items():
        listed = [_kb_entry(kb) for kb in kbs[:room]]
        room -= len(listed)
        entry = {"backend": label, "count": len(kbs), "knowledge_bases": listed}
        if len(listed) < len(kbs):
            entry["not_listed"] = len(kbs) - len(listed)
        backends.append(entry)
    if any("not_listed" in b for b in backends):
        summary += f"列表只列出前 {LIST_MAX_KBS} 个，个数仍按全部统计。"
    down = []
    for u in unavailable:
        backend = registry().by_id.get(u["id"])
        known = dict(backend.kb_names) if backend else {}
        if principal.kb_allowlist:
            if backend is None or not any(registry().route(kb) is backend for kb in principal.kb_allowlist):
                continue  # this connection cannot see that backend's knowledge bases anyway
            known = {k: v for k, v in known.items() if k in principal.kb_allowlist}
        down.append({"backend": u["label"], "last_seen_knowledge_bases": list(known.values())[:LIST_MAX_KBS]})
    if down:
        summary += ("另有知识后端当前不可用，其上的知识库没有计入、也检索不到："
                    + "、".join(d["backend"] for d in down) + "。")
    if items:
        summary += "用户要求列出知识库时，按后端逐个写出全部库名。"
    out: dict = {"summary": summary, "total": len(items), "backends": backends}
    if down:
        out["unavailable_backends"] = down
    return json.dumps(out, ensure_ascii=False)


@mcp.tool()
@_within_budget
async def search_knowledge(query: str, kb_ids: list[str] | None = None, top_k: int = 8) -> str:
    """在知识库中混合检索（向量 + 关键词 + 重排），返回最相关的片段和出处。kb_ids 为空时检索全部可用知识库。"""

    scope = _scope(kb_ids)
    try:
        hits, failed, _, warnings = await ops.search(registry(), scope, {"query": query}, max(1, min(int(top_k), 20)))
    except UpstreamError as exc:
        return _error(exc, scope)
    lead = _lead(failed, warnings, scope)
    if not hits:
        return lead + "没有检索到相关内容。"
    blocks = []
    for i, h in enumerate(hits, start=1):
        if h.get("chunk_type") == "wiki_page":
            blocks.append(
                f"[{i}] Wiki 页面《{h.get('title')}》（{h.get('context_header') or ''}，kb_id={h['kb_id']}, "
                f"slug={h.get('wiki_slug')}；整理自 {len(h.get('wiki_sources') or [])} 篇文档，用 read_wiki_page 读全文）\n"
                f"知识库：{_kb_label(h['kb_id'])}\n"
                f"{h.get('content')}"
            )
            continue
        where = " · ".join(x for x in (h.get("title"), h.get("context_header"), _location(h.get("locators"))) if x)
        note = f"，命中问题：{h['matched_question']}" if h.get("matched_question") else ""
        anchor = f", anchor_id={h['anchor_id']}" if h.get("anchor_id") else ""
        blocks.append(
            f"[{i}] 出处：{where}\n知识库：{_kb_label(h['kb_id'])}\n"
            f"(doc_id={h['doc_id']}, chunk_seq={h.get('seq')}{anchor}, score={float(h.get('score') or 0):.3f}{note})\n"
            + notes.instruction_line(h.get("content"), h.get("title"))
            + f"{h.get('content')}"
        )
    return lead + "\n\n---\n\n".join(blocks)


@mcp.tool()
@_within_budget
async def search_wiki(query: str, kb_ids: list[str] | None = None, limit: int = 5) -> str:
    """在开启了 Wiki 的知识库里检索 Wiki 页面（实体、概念、文档摘要页），返回标题、摘要和 slug。"""

    n = max(1, min(int(limit), 20))
    scope = _scope(kb_ids)
    try:
        # over-fetch so enough wiki pages survive the filter; the contract caps top_k at 50
        hits, failed, _, warnings = await ops.search(registry(), scope, {"query": query, "include_wiki": True},
                                                     min(n * 3, ops.SEARCH_MAX_TOP_K))
    except UpstreamError as exc:
        return _error(exc, scope)
    items = [
        {"kb_id": h["kb_id"], "slug": h.get("wiki_slug"), "title": h.get("title"), "type": h.get("wiki_type"),
         "summary": (h.get("content") or "").split("\n\n", 1)[0][:300], "score": round(float(h.get("score") or 0), 3)}
        for h in hits if h.get("chunk_type") == "wiki_page"
    ][:n]
    lead = _lead(failed, warnings, scope)
    if not items:
        return lead + "没有检索到 Wiki 页面（知识库可能没有开启 Wiki）。"
    return lead + json.dumps(items, ensure_ascii=False)


@mcp.tool()
@_within_budget
async def read_wiki_page(kb_id: str, slug: str) -> str:
    """读取一个 Wiki 页面全文，附带它链接到的页面、链接到它的页面和来源文档。slug 形如 entity/xxx、summary/<doc_id>、index。"""

    _scope([kb_id])
    backend = registry().route(kb_id)
    if not backend.has("wiki.read"):
        return f"{backend.config.label} 上的知识库没有 Wiki 页面。"
    try:
        page = await backend.json("GET", f"/v1/kbs/{kb_id}/wiki/page", params={"slug": slug})
    except UpstreamError as exc:
        if exc.status == 404:
            return f"Wiki 页面不存在：{slug}（可以先用 search_wiki 或读 slug=index 的索引页）"
        return _error(exc, [kb_id])
    content = page.get("content") or ""
    more = ""
    if len(content) > WIKI_PAGE_CHARS:
        content, more = content[:WIKI_PAGE_CHARS], f"\n\n（页面较长，已截断到前 {WIKI_PAGE_CHARS} 字）"

    def rel(refs: list[dict]) -> str:
        return "、".join(f"{r.get('title')}({r['slug']})" for r in refs or []) or "无"

    sources = "、".join(f"{d.get('title')}(doc_id={d.get('id')})" for d in page.get("sources") or []) or "无"
    return (
        f"# {page.get('title')}（{page.get('page_type')}，slug={slug}）\n摘要：{page.get('summary')}\n"
        f"来源文档：{sources}\n链接到：{rel(page.get('links_to'))}\n被链接自：{rel(page.get('linked_from'))}\n\n{content}{more}"
    )


@mcp.tool()
@_within_budget
async def list_documents(kb_id: str, limit: int = 50) -> str:
    """列出某个知识库的文档（标题、类型、状态、摘要）。no_text=true 的文件没有可识别的文字（扫描件或图片），内容查不到。"""

    _scope([kb_id])
    try:
        docs = await ops.list_documents(registry(), kb_id, limit=max(1, min(int(limit), 200)))
    except UpstreamError as exc:
        return _error(exc, [kb_id])
    items = [
        {"doc_id": d["id"], "title": d.get("title"), "file_name": d.get("file_name"),
         "status": d.get("parse_status"), "summary": d.get("summary"),
         **({"no_text": True} if notes.doc_has_no_text(d) else {})}
        for d in docs
    ]
    return json.dumps(items, ensure_ascii=False, default=str)


@mcp.tool()
@_within_budget
async def read_document(doc_id: str, offset: int = 0, limit: int = 10) -> str:
    """按阅读顺序读取文档的分块（offset 从 0 开始，limit 最多 30），用于查看检索命中处的上下文。"""

    principal = _caller()
    try:
        doc = await ops.get_document(registry(), doc_id)
    except UpstreamError as exc:
        return _error(exc)
    if doc is None or (principal.kb_allowlist and doc.get("kb_id") not in principal.kb_allowlist):
        return f"文档不存在或无权访问：{doc_id}"
    offset = max(0, int(offset))
    try:
        page = await ops.read_chunks(registry(), doc_id, offset, max(1, min(int(limit), 30)))
    except UpstreamError as exc:
        return _error(exc)
    chunks, total = page.get("items") or [], int(page.get("total") or 0)
    if notes.doc_has_no_text(doc) or (total == 0 and doc.get("parse_status") == "completed"):
        return f"《{doc.get('title')}》（{doc.get('file_name') or doc_id}）：{notes.NO_TEXT}"
    head = f"《{doc.get('title')}》 共 {total} 个分块，当前 {offset}–{offset + len(chunks) - 1}"
    if doc.get("summary") and offset == 0:
        head += f"\n摘要：{doc['summary']}"
    body = "\n\n".join(
        f"[seq {c.get('seq')}] {c['context_header']}\n{c.get('content')}" if c.get("context_header")
        else f"[seq {c.get('seq')}]\n{c.get('content')}"
        for c in chunks
    )
    more = f"\n\n（还有更多，继续用 offset={offset + len(chunks)}）" if offset + len(chunks) < total else ""
    warning = notes.instruction_line(body + "\n" + (doc.get("summary") or ""), doc.get("title"))
    return f"{warning}{head}\n\n{body}{more}"


@mcp.resource(SOURCE_CARD_URI, name="kb-source-card", title="出处", mime_type=MCP_APP_MIME_TYPE,
              meta={"ui": {"prefersBorder": False}})
def source_card() -> str:
    return resources.files(__package__).joinpath("source_card.html").read_text(encoding="utf-8")


def _source_text(source: dict) -> str:
    where = " · ".join(x for x in (source.get("title"), source.get("section_title"), _location(source.get("locators"))) if x)
    return f"出处：{where}\n{source.get('text') or ''}"


async def _evidence_source(anchor_id: str) -> dict | None:
    backend = registry().route(anchor_id.split(":", 1)[0])
    if not backend.healthy or not backend.has("evidence"):
        return None
    try:
        evidence = await backend.json("GET", f"/v1/evidence/{anchor_id}")
    except UpstreamError as exc:
        if exc.status == 404:
            return None
        raise
    return evidence if isinstance(evidence, dict) else None


async def _chunk_source(doc: dict, seq: int) -> dict | None:
    """Any backend: the chunk itself plus its neighbours as context (core chunks API)."""

    page = await ops.read_chunks(registry(), doc["id"], max(0, seq - 1), 3)
    by_seq = {c.get("seq"): c for c in page.get("items") or []}
    current = by_seq.get(seq)
    if current is None:
        return None
    line = [{"line_start": current["line_start"]}] if current.get("line_start") else []
    return {
        "anchor_id": current.get("anchor_id"), "doc_id": doc["id"], "kb_id": doc.get("kb_id"),
        "title": doc.get("title"), "file_name": doc.get("file_name"),
        "section_title": current.get("context_header") or None, "text": current.get("content") or "",
        "context_before": str((by_seq.get(seq - 1) or {}).get("content") or "")[-CONTEXT_CHARS:],
        "context_after": str((by_seq.get(seq + 1) or {}).get("content") or "")[:CONTEXT_CHARS],
        "locators": current.get("source_locators") or line,
    }


def _plain(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(title="查看出处", annotations=ToolAnnotations(readOnlyHint=True),
          meta={"ui": {"resourceUri": SOURCE_CARD_URI}}, structured_output=False)
@_within_budget  # on timeout the result is the budget's plain-text note, which FastMCP passes on as text
async def show_source(doc_id: str, chunk_seq: int | None = None, anchor_id: str | None = None) -> CallToolResult:
    """给用户展示一条出处的原文（文档、章节、页码和前后文）。参数取自 search_knowledge 结果里的 doc_id、chunk_seq，有 anchor_id 时一并传入。"""

    doc = await ops.get_document(registry(), doc_id)
    principal = _caller()
    if doc is None or (principal.kb_allowlist and doc.get("kb_id") not in principal.kb_allowlist):
        return _plain(f"文档不存在或无权访问：{doc_id}")
    try:
        source = None
        # an anchor is "{docId}:{type}:{index}"; only one of this document is followed
        if anchor_id and anchor_id.split(":", 1)[0] == doc_id:
            source = await _evidence_source(anchor_id)
        if source is None and chunk_seq is not None:
            source = await _chunk_source(doc, int(chunk_seq))
    except UpstreamError as exc:
        return _plain(_error(exc))
    if source is None:
        return _plain("没有找到这条出处：先用 search_knowledge 检索，再传入结果里的 doc_id 和 chunk_seq。")
    source.setdefault("title", doc.get("title"))
    source.setdefault("file_name", doc.get("file_name"))
    return CallToolResult(content=[TextContent(type="text", text=_source_text(source))],
                          structuredContent={"source": source})


@mcp.tool()
@_within_budget
async def grep_documents(pattern: str, kb_ids: list[str] | None = None, max_results: int = 20,
                         max_lines_per_doc: int = 10) -> str:
    """在知识库正文中逐行查找正则或字面量（穷尽扫描），返回命中行和行号。

    每篇最多返回 max_lines_per_doc 行（最大 100）；某篇被截断时该篇 truncated=true，message 写明还有多少处没有返回。
    """

    scope = _scope(kb_ids)
    max_results = max(1, min(int(max_results), 50))
    max_lines = max(1, min(int(max_lines_per_doc), GREP_MAX_LINES))
    try:
        result = ops.grep_view(await ops.grep(registry(), scope, pattern, max_results=max_results,
                                              max_lines_per_doc=max_lines))
    except UpstreamError as exc:
        return _error(exc, scope)
    notes.annotate_grep(result, max_lines=max_lines, max_results=max_results, lines_cap=GREP_MAX_LINES, results_cap=50)
    if result.get("failed_backends"):
        notes.add_message(result, _missed(scope, result["failed_backends"]))
    result = notes.flag_instructions(result) or result
    return json.dumps(result, ensure_ascii=False)


class AuthenticatedMcpApp:
    """ASGI wrapper: reject unauthenticated calls before they reach MCP."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            principal = state().keys.authenticate(extract_token(headers))
            principal.require(CAP_RETRIEVE)
            principal = narrow(principal, parse_scope_header(headers.get("x-kb-scope")))
        except AuthError as exc:
            body = json.dumps({"error": exc.message}).encode()
            await send({"type": "http.response.start", "status": exc.status,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        token = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token)
