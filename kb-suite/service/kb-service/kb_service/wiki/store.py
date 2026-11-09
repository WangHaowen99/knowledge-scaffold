"""Wiki page persistence: create / versioned update / retract / link bookkeeping."""

from __future__ import annotations

import uuid

from .. import db, models_client
from ..pipeline import vector_literal
from . import links

PAGE_COLUMNS = (
    "id, tenant_id, kb_id, slug, title, title_norm, page_type, summary, content, aliases, source_doc_ids,"
    " chunk_refs, out_links, in_links, version, last_edit_source, created_at, updated_at"
)
EMBED_CHARS = 2000
MAX_REVISIONS = 50


class VersionConflict(Exception):
    pass


class TitleTaken(Exception):
    """Another page of the same type already has this (normalised) title."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


async def get(kb_id: str, slug: str) -> dict | None:
    row = await db.pool().fetchrow(
        f"SELECT {PAGE_COLUMNS} FROM wiki_pages WHERE kb_id = $1 AND slug = $2 AND deleted_at IS NULL", kb_id, slug
    )
    return dict(row) if row else None


async def get_by_id(page_id: str) -> dict | None:
    row = await db.pool().fetchrow(
        f"SELECT {PAGE_COLUMNS} FROM wiki_pages WHERE id = $1 AND deleted_at IS NULL", page_id
    )
    return dict(row) if row else None


async def get_many(kb_id: str, slugs: list[str]) -> dict[str, dict]:
    if not slugs:
        return {}
    rows = await db.pool().fetch(
        f"SELECT {PAGE_COLUMNS} FROM wiki_pages WHERE kb_id = $1 AND slug = ANY($2::text[]) AND deleted_at IS NULL",
        kb_id,
        slugs,
    )
    return {r["slug"]: dict(r) for r in rows}


async def all_titles(kb_id: str) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT slug, title, title_norm, page_type, aliases FROM wiki_pages"
        " WHERE kb_id = $1 AND deleted_at IS NULL",
        kb_id,
    )
    return [dict(r) for r in rows]


async def pages_for_doc(kb_id: str, doc_id: str) -> list[dict]:
    rows = await db.pool().fetch(
        f"SELECT {PAGE_COLUMNS} FROM wiki_pages WHERE kb_id = $1 AND $2 = ANY(source_doc_ids)"
        " AND deleted_at IS NULL",
        kb_id,
        doc_id,
    )
    return [dict(r) for r in rows]


async def similar(kb_id: str, names: list[str], page_type: str, limit: int = 5) -> list[dict]:
    """Surface-similar existing pages for dedup (pg_trgm on normalised titles)."""

    norms = [n for n in {links.normalize_title(x) for x in names} if n]
    if not norms:
        return []
    rows = await db.pool().fetch(
        "SELECT slug, title, page_type, aliases, summary, max(similarity(title_norm, n)) AS sim"
        " FROM wiki_pages, unnest($2::text[]) AS n"
        " WHERE kb_id = $1 AND deleted_at IS NULL AND page_type = $3 AND similarity(title_norm, n) > 0.3"
        " GROUP BY slug, title, page_type, aliases, summary ORDER BY sim DESC LIMIT $4",
        kb_id,
        norms,
        page_type,
        limit,
    )
    return [dict(r) for r in rows]


async def create(
    *,
    tenant_id: str,
    kb_id: str,
    slug: str,
    title: str,
    page_type: str,
    summary: str,
    content: str,
    aliases: list[str],
    source_doc_ids: list[str],
    chunk_refs: dict,
    edit_source: str = "pipeline",
) -> dict | None:
    """Insert a page; returns None when the slug was taken concurrently.

    Entity and concept pages are unique per normalised title too (checked
    under an advisory lock), raising :class:`TitleTaken` with the existing slug.
    """

    title_norm = links.normalize_title(title)
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            if page_type in links.PAGE_TYPES:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"wiki-title:{kb_id}:{title_norm}")
                existing = await conn.fetchval(
                    "SELECT slug FROM wiki_pages WHERE kb_id = $1 AND title_norm = $2 AND page_type = $3"
                    " AND deleted_at IS NULL AND slug <> $4",
                    kb_id,
                    title_norm,
                    page_type,
                    slug,
                )
                if existing:
                    raise TitleTaken(existing)
            row = await conn.fetchrow(
                "INSERT INTO wiki_pages (id, tenant_id, kb_id, slug, title, title_norm, page_type, summary, content,"
                " aliases, source_doc_ids, chunk_refs, out_links, last_edit_source)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)"
                " ON CONFLICT (kb_id, slug) WHERE deleted_at IS NULL DO NOTHING"
                f" RETURNING {PAGE_COLUMNS}",
                str(uuid.uuid4()),
                tenant_id,
                kb_id,
                slug,
                title[:300],
                title_norm,
                page_type,
                links.plain(summary),
                content,
                aliases,
                source_doc_ids,
                chunk_refs,
                links.parse_links(content),
                edit_source,
            )
    return dict(row) if row else None


async def update(page: dict, *, edit_source: str = "pipeline", **fields) -> dict:
    """Versioned update: snapshots the old version, fails on a concurrent write."""

    allowed = {"title", "summary", "content", "aliases", "source_doc_ids", "chunk_refs", "page_type"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if "title" in changes:
        changes["title"] = changes["title"][:300]
        changes["title_norm"] = links.normalize_title(changes["title"])
    if "content" in changes:
        changes["out_links"] = links.parse_links(changes["content"])
    if "summary" in changes:
        changes["summary"] = links.plain(changes["summary"])
    sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(changes))
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO wiki_page_revisions (page_id, version, title, summary, content, edit_source)"
                " VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                page["id"],
                page["version"],
                page["title"],
                page["summary"],
                page["content"],
                page["last_edit_source"],
            )
            row = await conn.fetchrow(
                f"UPDATE wiki_pages SET {sets + ', ' if sets else ''}version = version + 1,"
                " last_edit_source = $3, updated_at = now()"
                f" WHERE id = $1 AND version = $2 AND deleted_at IS NULL RETURNING {PAGE_COLUMNS}",
                page["id"],
                page["version"],
                edit_source,
                *changes.values(),
            )
            if row is None:
                raise VersionConflict(page["slug"])
            await conn.execute(
                "DELETE FROM wiki_page_revisions WHERE page_id = $1 AND version <= $2",
                page["id"],
                page["version"] - MAX_REVISIONS,
            )
    return dict(row)


async def soft_delete(page_id: str) -> None:
    await db.pool().execute(
        "UPDATE wiki_pages SET deleted_at = now(), updated_at = now() WHERE id = $1 AND deleted_at IS NULL", page_id
    )


async def embed_pages(pages: list[dict]) -> None:
    pages = [p for p in pages if p]
    if not pages:
        return
    texts = [f"{p['title']}\n{p['summary']}\n{p['content'][:EMBED_CHARS]}" for p in pages]
    vectors = await models_client.embed(texts)
    await db.pool().executemany(
        "UPDATE wiki_pages SET embedding = $2::halfvec, dimension = $3 WHERE id = $1",
        [(p["id"], vector_literal(v), len(v)) for p, v in zip(pages, vectors)],
    )


async def refresh_links(kb_id: str) -> None:
    """Recompute out_links (live targets only) and in_links for the whole knowledge base."""

    rows = await db.pool().fetch(
        "SELECT id, slug, content FROM wiki_pages WHERE kb_id = $1 AND deleted_at IS NULL", kb_id
    )
    live = {r["slug"] for r in rows}
    outs = {r["slug"]: [s for s in links.parse_links(r["content"]) if s in live and s != r["slug"]] for r in rows}
    ins: dict[str, list[str]] = {slug: [] for slug in live}
    for src, targets in outs.items():
        for target in targets:
            ins[target].append(src)
    await db.pool().executemany(
        "UPDATE wiki_pages SET out_links = $2, in_links = $3 WHERE id = $1",
        [(r["id"], outs[r["slug"]], sorted(ins[r["slug"]])) for r in rows],
    )


async def revisions(page_id: str, limit: int = 20) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT version, title, summary, edit_source, created_at, length(content) AS chars"
        " FROM wiki_page_revisions WHERE page_id = $1 ORDER BY version DESC LIMIT $2",
        page_id,
        limit,
    )
    return [dict(r) for r in rows]
