"""LLM wiki pipeline (WeKnora wiki ingest, simplified to one document per job).

Per document (``wiki_ingest``, queued once its summary settles):
  1. candidates   entities / concepts with slugs, reusing this document's
                  previous slugs for continuity
  2. identity     exact title / alias match reuses a page; otherwise
                  pg_trgm-similar pages go through an LLM dedup pass
  3. summary page ``summary/<doc_id>`` linking the candidates
  4. citations    chunks (``c000`` handles, batches of ~12k chars) mapped to
                  the slugs they substantively discuss
  5. reduce       each page is created or merged from the verbatim cited
                  chunks; writes are versioned and retried on conflict
  6. stale        slugs this document no longer supports are retracted
Deleting a document runs ``wiki_retract``; ``wiki_finalize`` (debounced per
knowledge base) cleans dead links, auto-links titles, refreshes back-links
and rebuilds the index page.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from .. import db, jobs, kb_config, models_client, storage, text_utils
from ..pipeline import doc_config
from . import links, prompts, store

logger = logging.getLogger(__name__)

CONTENT_CHARS = 32768
CITATION_BATCH_CHARS = 12000
NEW_INFO_CHARS = 12000
SOURCE_CONTEXT_CHARS = 600
MIN_TEXT_CHARS = 10
WRITE_RETRIES = 2
INDEX_SLUG = "index"
FINALIZE_DELAY_S = 20


class WikiError(Exception):
    pass


@contextlib.asynccontextmanager
async def kb_lock(kb_id: str):
    """One wiki writer per knowledge base, so each document sees the pages the
    previous one created (dedup cannot see pages being created concurrently).
    A busy lock defers the job instead of blocking a worker slot."""

    key = f"wiki-kb:{kb_id}"
    async with db.pool().acquire() as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", key):
            raise jobs.Defer(15)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", key)


async def _llm_json(prompt: str, cfg: kb_config.KbConfig, *, max_tokens: int = 4096) -> dict:
    text = await models_client.chat(
        prompt, model=cfg.wiki.model, max_tokens=max_tokens, temperature=0.3, json_mode=True
    )
    try:
        return text_utils.parse_json_object(text)
    except ValueError as exc:
        raise WikiError(f"模型返回的不是有效 JSON：{exc}") from exc


def _extra(text: str, title: str) -> str:
    text = (text or "").strip()
    return f"\n### {title}\n{text}\n" if text else ""


# ---- candidates & identity ----------------------------------------------------


def _clean_items(data: dict, max_items: int) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page_type, key in (("entity", "entities"), ("concept", "concepts")):
        for raw in data.get(key) or []:
            if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
                continue
            name = str(raw["name"]).strip()[:120]
            slug = links.clean_slug(page_type, str(raw.get("slug") or ""), name)
            norm = links.normalize_title(name)
            if slug in seen or not norm:
                continue
            seen.add(slug)
            items.append(
                {
                    "type": page_type,
                    "name": name,
                    "slug": slug,
                    "aliases": [str(a).strip()[:120] for a in raw.get("aliases") or [] if str(a).strip()][:10],
                    "description": str(raw.get("description") or "").strip()[:500],
                    "details": str(raw.get("details") or "").strip()[:600],
                }
            )
    return items[:max_items] if max_items else items


def _unique_slugs(items: list[dict]) -> list[dict]:
    """Identity resolution can point two items at one page; keep the first."""

    seen: dict[str, dict] = {}
    for item in items:
        seen.setdefault(item["slug"], item)
    return list(seen.values())


async def _resolve_identity(kb_id: str, items: list[dict], cfg: kb_config.KbConfig) -> None:
    """Point items at existing pages: same slug, exact title/alias match, or LLM-confirmed duplicate."""

    existing = await store.all_titles(kb_id)
    by_slug = {p["slug"]: p for p in existing}
    by_norm: dict[str, dict] = {}
    for page in existing:
        if page["page_type"] not in links.PAGE_TYPES:
            continue
        for name in (page["title"], *(page["aliases"] or [])):
            by_norm.setdefault(links.normalize_title(name), page)
    pending: list[tuple[dict, list[dict]]] = []
    for item in items:
        if item["slug"] in by_slug and by_slug[item["slug"]]["page_type"] == item["type"]:
            continue
        match = next(
            (by_norm[n] for n in (links.normalize_title(x) for x in (item["name"], *item["aliases"])) if n in by_norm),
            None,
        )
        if match is not None:
            item["slug"] = match["slug"]
            continue
        similar = await store.similar(kb_id, [item["name"], *item["aliases"]], item["type"])
        if similar:
            pending.append((item, similar))
        elif item["slug"] in by_slug:
            # slug taken by a page of another type or name: keep the new page separate
            item["slug"] = f"{item['slug']}-{item['type'][0]}"
    if not pending:
        return
    blocks = []
    for item, candidates in pending:
        cands = "\n".join(
            f'    <page slug="{c["slug"]}" type="{c["page_type"]}"><name>{c["title"]}</name>'
            + "".join(f"<alias>{a}</alias>" for a in c["aliases"] or [])
            + f"<summary>{c['summary'][:200]}</summary></page>"
            for c in candidates
        )
        blocks.append(
            f'<item slug="{item["slug"]}" type="{item["type"]}"><name>{item["name"]}</name>'
            + "".join(f"<alias>{a}</alias>" for a in item["aliases"])
            + f"<description>{item['description']}</description>\n  <candidates>\n{cands}\n  </candidates>\n</item>"
        )
    try:
        data = await _llm_json(prompts.render(prompts.DEDUP, Items="\n".join(blocks)), cfg, max_tokens=1024)
    except (WikiError, models_client.ModelError) as exc:
        logger.warning("wiki dedup skipped: %s", exc)
        return
    merges = data.get("merges") or {}
    for item, candidates in pending:
        target = merges.get(item["slug"])
        allowed = {c["slug"] for c in candidates if c["page_type"] == item["type"]}
        if target in allowed:
            item["slug"] = target


# ---- citations --------------------------------------------------------------------


def _handle(i: int) -> str:
    return f"c{i:03d}"


async def _cite(chunks: list[dict], items: list[dict], cfg: kb_config.KbConfig) -> tuple[dict[str, set[str]], list[dict]]:
    """Returns ({slug: chunk ids}, new items found while citing)."""

    handle_to_id = {_handle(i): c["id"] for i, c in enumerate(chunks)}
    candidate_list = "\n".join(f'- {it["slug"]} = {it["name"]}: {it["description"]}' for it in items)
    batches: list[list[tuple[str, dict]]] = [[]]
    size = 0
    for i, chunk in enumerate(chunks):
        if chunk["chunk_type"] not in {"text", "table"}:
            continue
        length = len(chunk["content"])
        if batches[-1] and size + length > CITATION_BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append((_handle(i), chunk))
        size += length
    batches = [b for b in batches if b]

    async def one(batch: list[tuple[str, dict]]) -> dict:
        xml = "\n".join(f'<c id="{h}">{c["content"]}</c>' for h, c in batch)
        prompt = prompts.render(prompts.CITATIONS, Language=cfg.language, CandidateSlugs=candidate_list, Chunks=xml)
        try:
            return await _llm_json(prompt, cfg)
        except WikiError as exc:
            logger.warning("citation batch skipped: %s", exc)
            return {}

    results = await asyncio.gather(*(one(b) for b in batches))
    cited: dict[str, set[str]] = {}
    new_items: list[dict] = []
    for data in results:
        for slug, handles in (data.get("citations") or {}).items():
            ids = {handle_to_id[h] for h in handles or [] if h in handle_to_id}
            if ids:
                cited.setdefault(str(slug), set()).update(ids)
        for raw in data.get("new_slugs") or []:
            if not isinstance(raw, dict):
                continue
            page_type = "concept" if raw.get("type") == "concept" else "entity"
            cleaned = _clean_items({"entities" if page_type == "entity" else "concepts": [raw]}, 0)
            if not cleaned:
                continue
            item = cleaned[0]
            item["type"] = page_type
            ids = {handle_to_id[h] for h in raw.get("source_chunks") or [] if h in handle_to_id}
            if ids:
                cited.setdefault(item["slug"], set()).update(ids)
            new_items.append(item)
    return cited, new_items


# ---- page writes ----------------------------------------------------------------


def _valid_links(titles: list[dict], extra: list[dict]) -> str:
    lines = [f"[[{p['slug']}]] = {p['title']}" for p in titles if p["page_type"] in links.PAGE_TYPES]
    lines.extend(f"[[{it['slug']}]] = {it['name']}" for it in extra)
    return "\n".join(dict.fromkeys(lines))[:6000]


def _modify_prompt(
    page_meta: dict,
    existing: str,
    cfg: kb_config.KbConfig,
    valid_links: str,
    *,
    shared_context: str = "",
    new_content: str = "",
    deleted: str = "",
    remaining: str = "",
) -> list[dict]:
    title = page_meta["title"]
    user = prompts.render(
        prompts.MODIFY_USER,
        SharedContext=f"<shared_source_contexts>\n{shared_context}\n</shared_source_contexts>\n\n" if shared_context else "",
        Slug=page_meta["slug"],
        Title=title,
        Type=page_meta["page_type"],
        Aliases=", ".join(page_meta.get("aliases") or []),
        Existing=existing or "(new page, no existing content)",
        Additions=prompts.render(prompts.ADDITIONS, NewContent=new_content) if new_content else "",
        Retractions=prompts.render(prompts.RETRACTIONS, Deleted=deleted, Remaining=remaining or "(none)")
        if deleted
        else "",
        ValidLinks=valid_links or "(none)",
        RetractRule=prompts.render(prompts.RETRACT_RULE, Title=title) if deleted else "",
        AddRule=prompts.render(prompts.ADD_RULE, Title=title) if new_content else "",
        Language=cfg.language,
        ExtraInstructions=_extra(cfg.wiki.content_instructions, "Business Instructions (lower priority than the rules above)"),
    )
    return [{"role": "system", "content": prompts.MODIFY_SYSTEM}, {"role": "user", "content": user}]


async def _rewrite(messages: list[dict], cfg: kb_config.KbConfig) -> tuple[str, str] | None:
    text, complete = await models_client.chat_long(messages, model=cfg.wiki.model, max_tokens=4096)
    if not complete:
        logger.warning("wiki page rewrite still truncated, not written")
        return None
    summary, body = text_utils.split_summary_line(text)
    return summary, body


async def _merge_item(
    doc: dict,
    item: dict,
    chunk_ids: set[str],
    chunk_text: dict[str, str],
    cfg: kb_config.KbConfig,
    valid_links: str,
    valid_slugs: set[str],
    shared_context: str,
) -> dict | None:
    """Create or merge one entity/concept page from this document's cited chunks."""

    if chunk_ids:
        ordered = [cid for cid in chunk_text if cid in chunk_ids]
        new_content = "\n\n---\n\n".join(chunk_text[c] for c in ordered)[:NEW_INFO_CHARS]
    else:
        new_content = "\n".join(x for x in (item["description"], item["details"]) if x)
    for _ in range(WRITE_RETRIES + 1):
        page = await store.get(doc["kb_id"], item["slug"])
        meta = page or {"slug": item["slug"], "title": item["name"], "page_type": item["type"], "aliases": item["aliases"]}
        result = await _rewrite(
            _modify_prompt(
                meta, page["content"] if page else "", cfg, valid_links,
                shared_context=shared_context, new_content=new_content,
            ),
            cfg,
        )
        if result is None:
            return None
        summary, body = result
        body = links.strip_dead_links(body, valid_slugs, item["slug"])
        refs = dict(page["chunk_refs"]) if page else {}
        refs[doc["id"]] = sorted(chunk_ids)
        if page is None:
            try:
                created = await store.create(
                    tenant_id=doc["tenant_id"],
                    kb_id=doc["kb_id"],
                    slug=item["slug"],
                    title=item["name"],
                    page_type=item["type"],
                    summary=summary or item["description"],
                    content=body,
                    aliases=item["aliases"],
                    source_doc_ids=[doc["id"]],
                    chunk_refs=refs,
                )
            except store.TitleTaken as exc:
                item["slug"] = exc.slug  # same thing under another slug: merge into that page
                continue
            if created is not None:
                return created
            continue  # slug created concurrently: merge into it
        aliases = list(dict.fromkeys([*(page["aliases"] or []), *item["aliases"]]))[:20]
        sources = list(dict.fromkeys([*page["source_doc_ids"], doc["id"]]))
        try:
            return await store.update(
                page, summary=summary or page["summary"], content=body, aliases=aliases,
                source_doc_ids=sources, chunk_refs=refs,
            )
        except store.VersionConflict:
            continue
    logger.warning("wiki page %s not written after retries", item["slug"])
    return None


async def _doc_titles(doc_ids: list[str]) -> dict[str, dict]:
    if not doc_ids:
        return {}
    rows = await db.pool().fetch(
        "SELECT id, title, summary FROM documents WHERE id = ANY($1::text[]) AND deleted_at IS NULL", doc_ids
    )
    return {r["id"]: dict(r) for r in rows}


async def retract_doc(page: dict, doc_id: str, doc_title: str, doc_summary: str, cfg: kb_config.KbConfig) -> dict | None:
    """Remove one document's contribution from a page; delete the page when nothing is left."""

    remaining_ids = [d for d in page["source_doc_ids"] if d != doc_id]
    if not remaining_ids or page["page_type"] == "summary":
        await store.soft_delete(page["id"])
        return None
    remaining = await _doc_titles(remaining_ids)
    titles = await store.all_titles(page["kb_id"])
    refs = {k: v for k, v in (page["chunk_refs"] or {}).items() if k != doc_id}
    for _ in range(WRITE_RETRIES + 1):
        result = await _rewrite(
            _modify_prompt(
                page, page["content"], cfg, _valid_links(titles, []),
                deleted=f"- {doc_title}: {doc_summary or ''}"[:SOURCE_CONTEXT_CHARS],
                remaining="\n".join(f"- {d['title']}: {(d['summary'] or '')[:200]}" for d in remaining.values()),
            ),
            cfg,
        )
        try:
            if result is None:
                return await store.update(page, source_doc_ids=remaining_ids, chunk_refs=refs)
            summary, body = result
            return await store.update(
                page, summary=summary or page["summary"], content=body, source_doc_ids=remaining_ids, chunk_refs=refs
            )
        except store.VersionConflict:
            fresh = await store.get_by_id(page["id"])
            if fresh is None:
                return None
            page = fresh
    return None


# ---- ingest ---------------------------------------------------------------------


async def _load_doc(doc_id: str, attempt: int | None) -> dict:
    row = await db.pool().fetchrow(
        "SELECT d.id, d.tenant_id, d.kb_id, d.title, d.summary, d.l1_key, d.attempt, d.parse_status,"
        " d.deleted_at, d.process_overrides, k.config AS kb_config"
        " FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id WHERE d.id = $1",
        doc_id,
    )
    if row is None or row["deleted_at"] or row["parse_status"] != "completed":
        raise jobs.Abandon("document not available")
    if attempt is not None and row["attempt"] != attempt:
        raise jobs.Abandon("attempt superseded")
    return dict(row)


async def ingest_document(doc: dict, cfg: kb_config.KbConfig) -> dict:
    kb_id = doc["kb_id"]
    chunks = [
        dict(r)
        for r in await db.pool().fetch(
            "SELECT id, seq, chunk_type, content FROM chunks WHERE document_id = $1 AND is_active ORDER BY seq",
            doc["id"],
        )
    ]
    content = storage.get_text(doc["l1_key"])[:CONTENT_CHARS] if doc.get("l1_key") else ""
    previous = await store.pages_for_doc(kb_id, doc["id"])
    prev_items = [p for p in previous if p["page_type"] in links.PAGE_TYPES]

    items: list[dict] = []
    if len(content.strip()) >= MIN_TEXT_CHARS:
        data = await _llm_json(
            prompts.render(
                prompts.CANDIDATES,
                Content=content,
                PreviousSlugs="\n".join(f"{p['slug']} = {p['title']}" for p in prev_items) or "(none)",
                Language=cfg.language,
                Granularity=cfg.wiki.granularity,
                GranularityGuidance=prompts.GRANULARITY[cfg.wiki.granularity],
                ExtraInstructions=_extra(cfg.wiki.extraction_instructions, "Extraction Focus"),
            ),
            cfg,
        )
        items = _clean_items(data, cfg.wiki.max_pages_per_doc)
        await _resolve_identity(kb_id, items, cfg)
        items = _unique_slugs(items)

    cited, new_items = await _cite(chunks, items, cfg) if items else ({}, [])
    known = {it["slug"] for it in items}
    for item in new_items:
        if cfg.wiki.max_pages_per_doc and len(items) >= cfg.wiki.max_pages_per_doc:
            break
        if item["slug"] not in known:
            items.append(item)
            known.add(item["slug"])
    added = [it for it in items if any(it is n for n in new_items)]
    if added:
        before = [it["slug"] for it in added]
        await _resolve_identity(kb_id, added, cfg)
        for old, item in zip(before, added):
            if item["slug"] != old and old in cited:
                cited.setdefault(item["slug"], set()).update(cited.pop(old))
        items = _unique_slugs(items)

    titles = await store.all_titles(kb_id)
    valid = _valid_links(titles, items)
    valid_slugs = {p["slug"] for p in titles if p["page_type"] in links.PAGE_TYPES} | {it["slug"] for it in items}
    chunk_text = {c["id"]: c["content"] for c in chunks}
    shared = f"<source><title>{doc['title']}</title><summary>{(doc.get('summary') or '')[:SOURCE_CONTEXT_CHARS]}</summary></source>"

    # summary page (one per document, wholesale overwrite)
    touched: list[dict] = []
    summary_slug = f"summary/{doc['id']}"
    if len(content.strip()) >= MIN_TEXT_CHARS:
        slug_list = "\n".join(
            f"[[{it['slug']}]] = {it['name']}" + (f" (Aliases: {', '.join(it['aliases'])})" if it["aliases"] else "")
            for it in items
        )
        text, complete = await models_client.chat_long(
            [
                {
                    "role": "user",
                    "content": prompts.render(
                        prompts.SUMMARY,
                        Content=content,
                        Slugs=slug_list or "(none)",
                        Language=cfg.language,
                        ExtraInstructions=_extra(cfg.wiki.content_instructions, "Business Instructions"),
                    ),
                }
            ],
            model=cfg.wiki.model,
            max_tokens=4096,
        )
        if complete:
            summary, body = text_utils.split_summary_line(text)
            body = links.strip_dead_links(body, valid_slugs, summary_slug)
            page = None
            for _ in range(WRITE_RETRIES + 1):
                existing = await store.get(kb_id, summary_slug)
                try:
                    if existing is None:
                        page = await store.create(
                            tenant_id=doc["tenant_id"], kb_id=kb_id, slug=summary_slug, title=doc["title"],
                            page_type="summary", summary=summary, content=body, aliases=[],
                            source_doc_ids=[doc["id"]], chunk_refs={},
                        )
                    else:
                        page = await store.update(existing, title=doc["title"], summary=summary, content=body)
                except store.VersionConflict:
                    continue
                if page is not None:
                    break
            if page is not None:
                touched.append(page)

    # entity / concept pages
    results = await asyncio.gather(
        *(
            _merge_item(doc, item, cited.get(item["slug"], set()), chunk_text, cfg, valid, valid_slugs, shared)
            for item in items
        )
    )
    touched.extend(r for r in results if r)

    # pages this document no longer supports
    current = {it["slug"] for it in items} | {summary_slug}
    stale = [p for p in previous if p["slug"] not in current]
    for page in stale:
        await retract_doc(page, doc["id"], doc["title"], doc.get("summary") or "", cfg)

    await store.embed_pages([p for p in touched if p])
    return {"pages": len(touched), "items": len(items), "retracted": len(stale)}


async def _finish(doc_id: str, status: str, error: str | None = None) -> None:
    await db.pool().execute(
        "UPDATE documents SET wiki_status = $2, wiki_error = $3, updated_at = now() WHERE id = $1",
        doc_id,
        status,
        error[:1000] if error else None,
    )


async def _queue_finalize(kb_id: str) -> None:
    async with db.pool().acquire() as conn:
        await jobs.enqueue_kb_once(conn, jobs.KIND_WIKI_FINALIZE, kb_id, delay_s=FINALIZE_DELAY_S)


async def handle_ingest(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    attempt = job.payload.get("attempt")
    doc = await _load_doc(doc_id, int(attempt) if attempt is not None else None)
    cfg = doc_config(doc)
    if not cfg.wiki.enabled:
        await _finish(doc_id, "none")
        return
    try:
        async with kb_lock(doc["kb_id"]):
            await _finish(doc_id, "processing")
            stats = await ingest_document(doc, cfg)
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        await _finish(doc_id, "failed", str(exc))
        return
    except WikiError as exc:
        if job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        await _finish(doc_id, "failed", str(exc))
        return
    logger.info("wiki ingest %s: %s", doc_id, stats)
    await _finish(doc_id, "completed")
    await _queue_finalize(doc["kb_id"])


async def handle_retract(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    kb_id = job.kb_id or job.payload.get("kb_id") or ""
    row = await db.pool().fetchrow("SELECT config FROM knowledge_bases WHERE id = $1", kb_id)
    if row is None:
        return
    cfg = kb_config.load(row["config"])
    title = job.payload.get("title") or ""
    summary = await db.pool().fetchval("SELECT summary FROM documents WHERE id = $1", doc_id) or ""
    try:
        async with kb_lock(kb_id):
            for page in await store.pages_for_doc(kb_id, doc_id):
                updated = await retract_doc(page, doc_id, title, summary, cfg)
                if updated:
                    await store.embed_pages([updated])
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        raise
    await _queue_finalize(kb_id)


# ---- finalize -------------------------------------------------------------------


def _directory(pages: list[dict]) -> str:
    groups = (("summary", "文档"), ("entity", "实体"), ("concept", "概念"))
    parts = []
    for page_type, label in groups:
        items = sorted((p for p in pages if p["page_type"] == page_type), key=lambda p: p["title"])
        if not items:
            continue
        parts.append(f"## {label}（{len(items)}）\n")
        parts.extend(f"- [[{p['slug']}|{p['title']}]]：{p['summary']}" for p in items)
        parts.append("")
    return "\n".join(parts)


async def finalize(kb_id: str, cfg: kb_config.KbConfig, tenant_id: str) -> dict:
    rows = await db.pool().fetch(
        f"SELECT {store.PAGE_COLUMNS} FROM wiki_pages WHERE kb_id = $1 AND deleted_at IS NULL", kb_id
    )
    pages = [dict(r) for r in rows]
    live = {p["slug"] for p in pages}
    targets = [
        (name, p["slug"])
        for p in pages
        if p["page_type"] in links.PAGE_TYPES
        for name in (p["title"], *(p["aliases"] or []))
    ]
    changed: list[dict] = []
    for page in pages:
        if page["page_type"] == "index":
            continue
        content = links.strip_dead_links(page["content"], live, page["slug"])
        content = links.linkify(content, targets, page["slug"])
        if content != page["content"]:
            try:
                changed.append(await store.update(page, content=content))
            except store.VersionConflict:
                continue

    content_pages = [p for p in pages if p["page_type"] != "index"]
    index = next((p for p in pages if p["slug"] == INDEX_SLUG), None)
    if content_pages:
        summaries = await db.pool().fetch(
            "SELECT title, summary FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
            " AND parse_status = 'completed' ORDER BY created_at LIMIT 60",
            kb_id,
        )
        intro = ""
        if index is None or not index["summary"]:
            intro = await models_client.chat(
                prompts.render(
                    prompts.INDEX_INTRO,
                    Summaries="\n".join(f"- {r['title']}: {(r['summary'] or '')[:300]}" for r in summaries),
                    Language=cfg.language,
                ),
                model=cfg.wiki.model,
                max_tokens=600,
            )
        else:
            intro = index["summary"]
        heading, _, rest = intro.strip().partition("\n")
        title = heading.lstrip("# ").strip() or "Wiki"
        body = f"# {title}\n\n{rest.strip()}\n\n{_directory(content_pages)}"
        if index is None:
            index = await store.create(
                tenant_id=tenant_id, kb_id=kb_id, slug=INDEX_SLUG, title=title, page_type="index",
                summary=intro.strip(), content=body, aliases=[], source_doc_ids=[], chunk_refs={},
            )
            changed.append(index)
        elif body != index["content"]:
            changed.append(await store.update(index, title=title, content=body))
    elif index is not None:
        await store.soft_delete(index["id"])

    await store.refresh_links(kb_id)
    await store.embed_pages([p for p in changed if p])
    return {"pages": len(pages), "changed": len(changed)}


async def handle_finalize(job: jobs.Job) -> None:
    kb_id = job.kb_id or ""
    row = await db.pool().fetchrow("SELECT tenant_id, config, deleted_at FROM knowledge_bases WHERE id = $1", kb_id)
    if row is None or row["deleted_at"]:
        return
    cfg = kb_config.load(row["config"])
    try:
        async with kb_lock(kb_id):
            stats = await finalize(kb_id, cfg, row["tenant_id"])
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        raise
    logger.info("wiki finalize %s: %s", kb_id, stats)


async def rebuild(kb_id: str, *, reset: bool = False) -> int:
    """Queue every completed document of the knowledge base for wiki ingest."""

    async with db.pool().acquire() as conn:
        async with conn.transaction():
            if reset:
                await conn.execute(
                    "UPDATE wiki_pages SET deleted_at = now(), updated_at = now()"
                    " WHERE kb_id = $1 AND deleted_at IS NULL",
                    kb_id,
                )
            rows = await conn.fetch(
                "SELECT id FROM documents WHERE kb_id = $1 AND deleted_at IS NULL AND parse_status = 'completed'"
                " ORDER BY created_at",
                kb_id,
            )
            for i, row in enumerate(rows):
                await jobs.enqueue(
                    conn, jobs.KIND_WIKI_INGEST, document_id=row["id"], kb_id=kb_id, payload={}, delay_s=i * 2
                )
            await conn.execute(
                "UPDATE documents SET wiki_status = 'pending', wiki_error = NULL"
                " WHERE kb_id = $1 AND deleted_at IS NULL AND parse_status = 'completed'",
                kb_id,
            )
    return len(rows)
