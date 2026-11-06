"""Wiki management and browsing API for the Swarm plugin (pages addressed by slug query param)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from .. import auth, db, documents, jobs, kb_config
from ..wiki import jobs as wiki_jobs
from ..wiki import store
from .common import Caller, need

router = APIRouter(prefix="/v1/kbs/{kb_id}/wiki")

LIST_COLUMNS = (
    "id, slug, title, page_type, summary, aliases, cardinality(source_doc_ids) AS sources,"
    " cardinality(in_links) AS in_count, cardinality(out_links) AS out_count, version, last_edit_source,"
    " updated_at"
)


async def _page_or_404(kb_id: str, slug: str) -> dict:
    page = await store.get(kb_id, slug)
    if page is None:
        raise documents.DocumentError(404, "not_found", f"Wiki 页面不存在：{slug}")
    return page


@router.get("/status")
async def wiki_status(kb_id: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    await caller.check_kb(kb_id)
    row = await db.pool().fetchrow("SELECT config FROM knowledge_bases WHERE id = $1", kb_id)
    pages = await db.pool().fetch(
        "SELECT page_type, count(*) AS n, max(updated_at) AS updated FROM wiki_pages"
        " WHERE kb_id = $1 AND deleted_at IS NULL GROUP BY page_type",
        kb_id,
    )
    docs = await db.pool().fetch(
        "SELECT wiki_status, count(*) AS n FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND parse_status = 'completed' GROUP BY wiki_status",
        kb_id,
    )
    pending = await db.pool().fetchval(
        "SELECT count(*) FROM jobs WHERE kb_id = $1 AND status IN ('queued', 'running')"
        " AND kind = ANY($2::text[])",
        kb_id,
        [jobs.KIND_WIKI_INGEST, jobs.KIND_WIKI_RETRACT, jobs.KIND_WIKI_FINALIZE],
    )
    errors = await db.pool().fetch(
        "SELECT id, title, wiki_error FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND wiki_status = 'failed' ORDER BY updated_at DESC LIMIT 10",
        kb_id,
    )
    return {
        "enabled": kb_config.load(row["config"] if row else None).wiki.enabled,
        "pages": {r["page_type"]: int(r["n"]) for r in pages},
        "updated_at": max((r["updated"] for r in pages), default=None),
        "documents": {r["wiki_status"]: int(r["n"]) for r in docs},
        "pending_jobs": int(pending or 0),
        "errors": [dict(r) for r in errors],
    }


@router.get("/pages")
async def list_pages(
    kb_id: str,
    type: str | None = Query(None, pattern="^(summary|entity|concept|index)$"),
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    caller: Caller = Depends(need(auth.CAP_RETRIEVE)),
) -> dict:
    await caller.check_kb(kb_id)
    where = (
        " WHERE kb_id = $1 AND deleted_at IS NULL AND ($2::text IS NULL OR page_type = $2)"
        " AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%' OR summary ILIKE '%' || $3 || '%'"
        "  OR $3 = ANY(aliases))"
    )
    rows = await db.pool().fetch(
        f"SELECT {LIST_COLUMNS} FROM wiki_pages{where}"
        " ORDER BY CASE page_type WHEN 'index' THEN 0 WHEN 'summary' THEN 1 ELSE 2 END, title"
        " LIMIT $4 OFFSET $5",
        kb_id,
        type,
        q,
        limit,
        offset,
    )
    total = await db.pool().fetchval(f"SELECT count(*) FROM wiki_pages{where}", kb_id, type, q)
    return {"items": [dict(r) for r in rows], "total": int(total or 0)}


@router.get("/page")
async def get_page(kb_id: str, slug: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    await caller.check_kb(kb_id)
    page = await _page_or_404(kb_id, slug)
    related = await store.get_many(kb_id, list({*page["out_links"], *page["in_links"]}))
    docs = await db.pool().fetch(
        "SELECT id, title, file_name FROM documents WHERE id = ANY($1::text[]) AND deleted_at IS NULL",
        page["source_doc_ids"],
    )

    def ref(s: str) -> dict:
        p = related.get(s)
        return {"slug": s, "title": p["title"] if p else s, "page_type": p["page_type"] if p else None}

    page.pop("title_norm", None)
    page["links_to"] = [ref(s) for s in page["out_links"]]
    page["linked_from"] = [ref(s) for s in page["in_links"]]
    page["sources"] = [dict(d) for d in docs]
    page["revisions"] = await store.revisions(page["id"])
    return page


class PageEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = Field(default=None, max_length=2000)
    content: str | None = Field(default=None, max_length=200000)
    aliases: list[str] | None = None


@router.put("/page")
async def edit_page(kb_id: str, slug: str, body: PageEdit, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    await caller.check_kb(kb_id)
    page = await _page_or_404(kb_id, slug)
    fields = body.model_dump(exclude_none=True)
    try:
        updated = await store.update(page, edit_source="user", **fields)
    except store.VersionConflict as exc:
        raise documents.DocumentError(409, "conflict", "页面刚被更新，请刷新后再改") from exc
    await store.embed_pages([updated])
    await store.refresh_links(kb_id)
    return await get_page(kb_id, slug, caller)


@router.delete("/page", status_code=204)
async def delete_page(kb_id: str, slug: str, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> None:
    await caller.check_kb(kb_id)
    page = await _page_or_404(kb_id, slug)
    await store.soft_delete(page["id"])
    await store.refresh_links(kb_id)


@router.get("/graph")
async def graph(
    kb_id: str,
    center: str | None = None,
    limit: int = Query(300, ge=1, le=2000),
    caller: Caller = Depends(need(auth.CAP_RETRIEVE)),
) -> dict:
    """Pages as nodes and ``[[...]]`` links as edges; ``center`` limits to one page and its neighbours."""

    await caller.check_kb(kb_id)
    rows = await db.pool().fetch(
        "SELECT slug, title, page_type, out_links, cardinality(in_links) + cardinality(out_links) AS degree"
        " FROM wiki_pages WHERE kb_id = $1 AND deleted_at IS NULL AND page_type <> 'index'"
        " ORDER BY degree DESC LIMIT $2",
        kb_id,
        limit,
    )
    pages = {r["slug"]: r for r in rows}
    if center and center in pages:
        keep = {center, *pages[center]["out_links"]}
        keep |= {s for s, r in pages.items() if center in r["out_links"]}
        pages = {s: r for s, r in pages.items() if s in keep}
    nodes = [
        {"slug": s, "title": r["title"], "page_type": r["page_type"], "degree": int(r["degree"])}
        for s, r in pages.items()
    ]
    edges = []
    for slug, row in pages.items():
        edges.extend({"source": slug, "target": t} for t in row["out_links"] if t in pages and t != slug)
    return {"nodes": nodes, "edges": edges}


class Rebuild(BaseModel):
    reset: bool = False


@router.post("/rebuild")
async def rebuild(kb_id: str, body: Rebuild, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    await caller.check_kb(kb_id)
    return {"queued": await wiki_jobs.rebuild(kb_id, reset=body.reset)}


@router.post("/finalize")
async def finalize_now(kb_id: str, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    await caller.check_kb(kb_id)
    async with db.pool().acquire() as conn:
        await jobs.enqueue_kb_once(conn, jobs.KIND_WIKI_FINALIZE, kb_id, delay_s=0)
    return {"queued": 1}

