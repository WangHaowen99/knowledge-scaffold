"""MCP server (streamable HTTP) for the Swarm main agent and Loop Forest.

Same seven tools and text formats as kb-service's MCP, answered across all
backends. Callers authenticate with a retrieve key and may confine a
connection to some knowledge bases with ``X-KB-Scope``.
"""

from __future__ import annotations

import contextvars
import json

from mcp.server.fastmcp import FastMCP

from .. import ops
from ..auth import CAP_RETRIEVE, AuthError, Principal, check_scope, extract_token, narrow, parse_scope_header
from ..deps import registry, state
from ..registry import UpstreamError

_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("kbgw_mcp_principal", default=None)

INSTRUCTIONS = (
    "知识库工具。先用 list_knowledge_bases 看有哪些知识库，用 search_knowledge 检索相关片段，"
    "需要上下文时用 read_document 按顺序阅读文档，精确查找原文用 grep_documents。"
    "开启了 Wiki 的知识库还有大模型整理好的实体、概念和文档摘要页：要概览或跨文档综合时先用 search_wiki，"
    "再用 read_wiki_page 读全文并顺着 [[链接]] 读相关页面；Wiki 是二次整理，结论要回到原文片段核对出处。"
    "回答时引用片段末尾的出处（文档标题、章节、页码）。"
)
WIKI_PAGE_CHARS = 12000


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


def _error(exc: UpstreamError) -> str:
    return f"知识服务出错：{exc.message}"


@mcp.tool()
async def list_knowledge_bases() -> str:
    """列出可用的知识库（id、名称、说明、文档数、所在后端）。"""

    principal = _caller()
    items, _ = await ops.list_kbs(registry())
    if principal.kb_allowlist:
        items = [kb for kb in items if kb["id"] in principal.kb_allowlist]
    return json.dumps(
        [
            {"id": kb["id"], "name": kb.get("name"), "description": kb.get("description"),
             "documents": int((kb.get("stats") or {}).get("completed") or 0), "backend": kb["backend"]["label"]}
            for kb in items
        ],
        ensure_ascii=False,
    )


@mcp.tool()
async def search_knowledge(query: str, kb_ids: list[str] | None = None, top_k: int = 8) -> str:
    """在知识库中混合检索（向量 + 关键词 + 重排），返回最相关的片段和出处。kb_ids 为空时检索全部可用知识库。"""

    try:
        hits, failed, _ = await ops.search(registry(), _scope(kb_ids), {"query": query}, max(1, min(int(top_k), 20)))
    except UpstreamError as exc:
        return _error(exc)
    if not hits:
        return "没有检索到相关内容。"
    blocks = []
    for i, h in enumerate(hits, start=1):
        if h.get("chunk_type") == "wiki_page":
            blocks.append(
                f"[{i}] Wiki 页面《{h.get('title')}》（{h.get('context_header') or ''}，kb_id={h['kb_id']}, "
                f"slug={h.get('wiki_slug')}；整理自 {len(h.get('wiki_sources') or [])} 篇文档，用 read_wiki_page 读全文）\n"
                f"{h.get('content')}"
            )
            continue
        where = " · ".join(x for x in (h.get("title"), h.get("context_header"), _location(h.get("locators"))) if x)
        note = f"，命中问题：{h['matched_question']}" if h.get("matched_question") else ""
        blocks.append(
            f"[{i}] 出处：{where}\n(doc_id={h['doc_id']}, chunk_seq={h.get('seq')}, score={float(h.get('score') or 0):.3f}{note})\n"
            f"{h.get('content')}"
        )
    tail = ""
    if failed:
        tail = "\n\n（以下知识后端本次检索失败：" + "、".join(f["label"] for f in failed) + "）"
    return "\n\n---\n\n".join(blocks) + tail


@mcp.tool()
async def search_wiki(query: str, kb_ids: list[str] | None = None, limit: int = 5) -> str:
    """在开启了 Wiki 的知识库里检索 Wiki 页面（实体、概念、文档摘要页），返回标题、摘要和 slug。"""

    n = max(1, min(int(limit), 20))
    try:
        # over-fetch so enough wiki pages survive the filter; the contract caps top_k at 50
        hits, _, _ = await ops.search(registry(), _scope(kb_ids), {"query": query, "include_wiki": True},
                                      min(n * 3, ops.SEARCH_MAX_TOP_K))
    except UpstreamError as exc:
        return _error(exc)
    items = [
        {"kb_id": h["kb_id"], "slug": h.get("wiki_slug"), "title": h.get("title"), "type": h.get("wiki_type"),
         "summary": (h.get("content") or "").split("\n\n", 1)[0][:300], "score": round(float(h.get("score") or 0), 3)}
        for h in hits if h.get("chunk_type") == "wiki_page"
    ][:n]
    if not items:
        return "没有检索到 Wiki 页面（知识库可能没有开启 Wiki）。"
    return json.dumps(items, ensure_ascii=False)


@mcp.tool()
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
        return _error(exc)
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
async def list_documents(kb_id: str, limit: int = 50) -> str:
    """列出某个知识库的文档（标题、类型、状态、摘要）。"""

    _scope([kb_id])
    try:
        docs = await ops.list_documents(registry(), kb_id, limit=max(1, min(int(limit), 200)))
    except UpstreamError as exc:
        return _error(exc)
    items = [
        {"doc_id": d["id"], "title": d.get("title"), "file_name": d.get("file_name"),
         "status": d.get("parse_status"), "summary": d.get("summary")}
        for d in docs
    ]
    return json.dumps(items, ensure_ascii=False, default=str)


@mcp.tool()
async def read_document(doc_id: str, offset: int = 0, limit: int = 10) -> str:
    """按阅读顺序读取文档的分块（offset 从 0 开始，limit 最多 30），用于查看检索命中处的上下文。"""

    doc = await ops.get_document(registry(), doc_id)
    principal = _caller()
    if doc is None or (principal.kb_allowlist and doc.get("kb_id") not in principal.kb_allowlist):
        return f"文档不存在或无权访问：{doc_id}"
    offset = max(0, int(offset))
    try:
        page = await ops.read_chunks(registry(), doc_id, offset, max(1, min(int(limit), 30)))
    except UpstreamError as exc:
        return _error(exc)
    chunks, total = page.get("items") or [], int(page.get("total") or 0)
    head = f"《{doc.get('title')}》 共 {total} 个分块，当前 {offset}–{offset + len(chunks) - 1}"
    if doc.get("summary") and offset == 0:
        head += f"\n摘要：{doc['summary']}"
    body = "\n\n".join(
        f"[seq {c.get('seq')}] {c['context_header']}\n{c.get('content')}" if c.get("context_header")
        else f"[seq {c.get('seq')}]\n{c.get('content')}"
        for c in chunks
    )
    more = f"\n\n（还有更多，继续用 offset={offset + len(chunks)}）" if offset + len(chunks) < total else ""
    return f"{head}\n\n{body}{more}"


@mcp.tool()
async def grep_documents(pattern: str, kb_ids: list[str] | None = None, max_results: int = 20) -> str:
    """在知识库正文中逐行查找正则或字面量（穷尽扫描），返回命中行和行号。"""

    try:
        result = ops.grep_view(await ops.grep(registry(), _scope(kb_ids), pattern, max_results=max(1, min(int(max_results), 50))))
    except UpstreamError as exc:
        return _error(exc)
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
