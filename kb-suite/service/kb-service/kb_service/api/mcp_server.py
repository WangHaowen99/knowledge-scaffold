"""MCP server (streamable HTTP) for the Swarm main agent.

Authenticated with a retrieve-only service key (Authorization: Bearer ...),
bound to one tenant and optionally a knowledge-base allowlist.
"""

from __future__ import annotations

import contextvars
import json
import logging

from mcp.server.fastmcp import Context, FastMCP

from .. import auth, retrieval
from ..auth import AuthError, Principal

logger = logging.getLogger(__name__)

_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("kbs_mcp_principal", default=None)

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
    except ImportError:  # older SDKs have no host check
        pass
    server = FastMCP(**kwargs)
    server.settings.streamable_http_path = "/"
    return server


mcp = _make_server()


async def _caller(ctx: Context) -> tuple[Principal, str]:
    principal = _principal.get()
    if principal is None:
        request = getattr(getattr(ctx, "request_context", None), "request", None)
        headers = getattr(request, "headers", {}) if request is not None else {}
        principal = await auth.principal_from_token(auth.extract_token(headers))
    principal.require(auth.CAP_RETRIEVE)
    return principal, auth.resolve_tenant(principal, None)


def _location(locators: list) -> str:
    parts = []
    for loc in locators[:3]:
        if loc.get("page"):
            parts.append(f"第{loc['page']}页")
        elif loc.get("sheet"):
            rows = f"{loc.get('row_start', '')}-{loc.get('row_end', '')}行" if loc.get("row_start") else ""
            parts.append(f"{loc['sheet']} {rows}".strip())
        elif loc.get("slide"):
            parts.append(f"第{loc['slide']}页幻灯片")
    return "、".join(dict.fromkeys(parts))


@mcp.tool()
async def list_knowledge_bases(ctx: Context) -> str:
    """列出可用的知识库（id、名称、说明、文档数）。"""

    principal, tenant = await _caller(ctx)
    kb_ids = await auth.resolve_kb_scope(principal, tenant, None, allow_all_when_empty=True)
    from .. import db

    rows = await db.pool().fetch(
        "SELECT k.id, k.name, k.description,"
        " (SELECT count(*) FROM documents d WHERE d.kb_id = k.id AND d.deleted_at IS NULL"
        "  AND d.parse_status = 'completed') AS docs"
        " FROM knowledge_bases k WHERE k.id = ANY($1::text[]) ORDER BY k.created_at",
        kb_ids,
    )
    items = [
        {"id": r["id"], "name": r["name"], "description": r["description"], "documents": int(r["docs"])}
        for r in rows
    ]
    return json.dumps(items, ensure_ascii=False)


@mcp.tool()
async def search_knowledge(ctx: Context, query: str, kb_ids: list[str] | None = None, top_k: int = 8) -> str:
    """在知识库中混合检索（向量 + 关键词 + 重排），返回最相关的片段和出处。kb_ids 为空时检索全部可用知识库。"""

    principal, tenant = await _caller(ctx)
    scope = await auth.resolve_kb_scope(principal, tenant, kb_ids, allow_all_when_empty=True)
    hits = await retrieval.search(tenant, scope, query, top_k=max(1, min(int(top_k), 20)))
    if not hits:
        return "没有检索到相关内容。"
    blocks = []
    for i, h in enumerate(hits, start=1):
        if h.is_wiki:
            blocks.append(
                f"[{i}] Wiki 页面《{h.title}》（{h.context_header}，kb_id={h.kb_id}, slug={h.wiki_slug},"
                f" score={h.score:.3f}；整理自 {len(h.wiki_sources)} 篇文档，用 read_wiki_page 读全文）\n{h.content}"
            )
            continue
        where = " · ".join(x for x in (h.title, h.context_header, _location(h.source_locators)) if x)
        note = f"，命中问题：{h.matched_question}" if h.matched_question else ""
        blocks.append(
            f"[{i}] 出处：{where}\n(doc_id={h.document_id}, chunk_seq={h.seq}, score={h.score:.3f}{note})\n{h.content}"
        )
    return "\n\n---\n\n".join(blocks)


@mcp.tool()
async def search_wiki(ctx: Context, query: str, kb_ids: list[str] | None = None, limit: int = 5) -> str:
    """在开启了 Wiki 的知识库里检索 Wiki 页面（实体、概念、文档摘要页），返回标题、摘要和 slug。"""

    principal, tenant = await _caller(ctx)
    scope = await auth.resolve_kb_scope(principal, tenant, kb_ids, allow_all_when_empty=True)
    n = max(1, min(int(limit), 20))
    hits = await retrieval.search(tenant, scope, query, top_k=n, include_chunks=False, wiki_limit=n)
    if not hits:
        return "没有检索到 Wiki 页面（知识库可能没有开启 Wiki）。"
    items = [
        {
            "kb_id": h.kb_id,
            "slug": h.wiki_slug,
            "title": h.title,
            "type": h.wiki_type,
            "summary": h.content.split("\n\n", 1)[0][:300],
            "score": round(h.score, 3),
        }
        for h in hits
    ]
    return json.dumps(items, ensure_ascii=False)


@mcp.tool()
async def read_wiki_page(ctx: Context, kb_id: str, slug: str) -> str:
    """读取一个 Wiki 页面全文，附带它链接到的页面、链接到它的页面和来源文档。slug 形如 entity/xxx、summary/<doc_id>、index。"""

    principal, tenant = await _caller(ctx)
    await auth.resolve_kb_scope(principal, tenant, [kb_id], allow_all_when_empty=False)
    from ..wiki import store

    page = await store.get(kb_id, slug)
    if page is None:
        return f"Wiki 页面不存在：{slug}（可以先用 search_wiki 或读 slug=index 的索引页）"
    related = await store.get_many(kb_id, list({*page["out_links"], *page["in_links"]}))
    docs = await retrieval.doc_titles(page["source_doc_ids"])
    content = page["content"]
    more = ""
    if len(content) > WIKI_PAGE_CHARS:
        content, more = content[:WIKI_PAGE_CHARS], f"\n\n（页面较长，已截断到前 {WIKI_PAGE_CHARS} 字）"

    def rel(slugs: list[str]) -> str:
        return "、".join(f"{related[s]['title']}({s})" for s in slugs if s in related) or "无"

    return (
        f"# {page['title']}（{page['page_type']}，slug={slug}）\n摘要：{page['summary']}\n"
        f"来源文档：{'、'.join(f'{t}(doc_id={d})' for d, t in docs.items()) or '无'}\n"
        f"链接到：{rel(page['out_links'])}\n被链接自：{rel(page['in_links'])}\n\n{content}{more}"
    )


@mcp.tool()
async def list_documents(ctx: Context, kb_id: str, limit: int = 50) -> str:
    """列出某个知识库的文档（标题、类型、状态、摘要）。"""

    principal, tenant = await _caller(ctx)
    await auth.resolve_kb_scope(principal, tenant, [kb_id], allow_all_when_empty=False)
    docs = await retrieval.list_documents(tenant, kb_id, limit=max(1, min(int(limit), 200)))
    items = [
        {
            "doc_id": d["id"],
            "title": d["title"],
            "file_name": d.get("file_name"),
            "status": d["parse_status"],
            "summary": d.get("summary"),
        }
        for d in docs
    ]
    return json.dumps(items, ensure_ascii=False, default=str)


@mcp.tool()
async def read_document(ctx: Context, doc_id: str, offset: int = 0, limit: int = 10) -> str:
    """按阅读顺序读取文档的分块（offset 从 0 开始，limit 最多 30），用于查看检索命中处的上下文。"""

    principal, tenant = await _caller(ctx)
    scope = await auth.resolve_kb_scope(principal, tenant, None, allow_all_when_empty=True)
    doc = await retrieval.get_document(tenant, doc_id, scope)
    if doc is None:
        return f"文档不存在或无权访问：{doc_id}"
    chunks, total = await retrieval.read_chunks(doc_id, offset=max(0, int(offset)), limit=max(1, min(int(limit), 30)))
    head = f"《{doc['title']}》 共 {total} 个分块，当前 {offset}–{offset + len(chunks) - 1}"
    if doc.get("summary") and offset == 0:
        head += f"\n摘要：{doc['summary']}"
    body = "\n\n".join(
        f"[seq {c['seq']}] {c['context_header']}\n{c['content']}" if c["context_header"] else f"[seq {c['seq']}]\n{c['content']}"
        for c in chunks
    )
    more = f"\n\n（还有更多，继续用 offset={offset + len(chunks)}）" if offset + len(chunks) < total else ""
    return f"{head}\n\n{body}{more}"


@mcp.tool()
async def grep_documents(ctx: Context, pattern: str, kb_ids: list[str] | None = None, max_results: int = 20) -> str:
    """在知识库正文中逐行查找正则或字面量（穷尽扫描），返回命中行和行号。"""

    principal, tenant = await _caller(ctx)
    scope = await auth.resolve_kb_scope(principal, tenant, kb_ids, allow_all_when_empty=True)
    result = await retrieval.grep(tenant, scope, pattern, max_results=max(1, min(int(max_results), 50)))
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
            principal = await auth.principal_from_token(auth.extract_token(headers))
            principal.require(auth.CAP_RETRIEVE)
            # a caller may confine this connection to some knowledge bases (e.g. the
            # Loop Forest service, scoped per run like DA's tool calls)
            principal = auth.narrow_to_scope(principal, auth.parse_scope_header(headers.get("x-kb-scope")))
        except AuthError as exc:
            body = json.dumps({"error": exc.message}).encode()
            await send({"type": "http.response.start", "status": exc.status, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        token = _principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _principal.reset(token)
