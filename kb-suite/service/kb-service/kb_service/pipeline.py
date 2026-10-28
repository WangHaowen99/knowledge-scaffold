"""Ingestion pipeline: parse -> chunk -> embed/index, then summary (L0).

State machine follows WeKnora's parse_status values. Every stage re-checks
``documents.attempt`` so a superseded run (reprocess, cancel, delete) exits
without touching the newer attempt; old chunks stay searchable until the new
attempt is swapped in inside one transaction.
"""

from __future__ import annotations

import json
import logging
import re
import uuid

from . import db, jobs, kb_config, models_client, storage
from .chunking import chunk_markdown
from .locators import LocatorIndex
from .parsing import ParseError, ParseResult, parse_file, parse_url

logger = logging.getLogger(__name__)

TERMINAL_STOP = {"cancelled", "deleting"}

SUMMARY_SYSTEM = "你是严谨的文档摘要助手，只依据给定内容写摘要，不编造信息。"
SUMMARY_PROMPT = (
    "请用{language}为下面的文档写一段不超过 200 字的摘要，说明文档主题、主要内容和用途。"
    "直接输出摘要正文，不要加标题或前缀。{extra}\n\n文档标题：{title}\n\n文档内容（节选）：\n{text}"
)
TABLE_FILE_TYPES = {"csv", "tsv", "xlsx", "xls"}


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6g}" for v in values) + "]"


def _safe_asset_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name.replace("\\", "/").split("/")[-1])
    return cleaned[:120] or uuid.uuid4().hex


def doc_config(doc: dict) -> kb_config.KbConfig:
    """Knowledge-base config with this document's upload-time overrides applied."""

    return kb_config.load(doc.get("kb_config"), doc.get("process_overrides"))


async def _load_current(doc_id: str, attempt: int) -> dict:
    row = await db.pool().fetchrow(
        "SELECT d.*, k.config AS kb_config FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id"
        " WHERE d.id = $1",
        doc_id,
    )
    if row is None or row["deleted_at"] is not None:
        raise jobs.Abandon("document no longer exists")
    if row["attempt"] != attempt:
        raise jobs.Abandon(f"attempt {attempt} superseded by {row['attempt']}")
    if row["parse_status"] in TERMINAL_STOP:
        raise jobs.Abandon(f"document is {row['parse_status']}")
    return dict(row)


async def _set_stage(doc_id: str, attempt: int, stage: str) -> None:
    await db.pool().execute(
        "UPDATE documents SET parse_status = 'processing', stage = $3, updated_at = now()"
        " WHERE id = $1 AND attempt = $2 AND parse_status NOT IN ('cancelled', 'deleting')",
        doc_id,
        attempt,
        stage,
    )


async def _fail(doc_id: str, attempt: int, code: str, message: str) -> None:
    await db.pool().execute(
        "UPDATE documents SET parse_status = 'failed', stage = NULL, error_code = $3,"
        " error_message = $4, updated_at = now()"
        " WHERE id = $1 AND attempt = $2 AND parse_status NOT IN ('cancelled', 'deleting')",
        doc_id,
        attempt,
        code,
        message[:2000],
    )
    await db.pool().execute(
        "DELETE FROM chunks WHERE document_id = $1 AND attempt = $2 AND NOT is_active", doc_id, attempt
    )


async def _parse(doc: dict, cfg: kb_config.KbConfig) -> ParseResult:
    source = doc["source_type"]
    if source == "file":
        data = storage.get_bytes(doc["storage_key"])
        return await parse_file(doc["file_name"], data, cfg.parsing.engine_rules)
    if source == "url":
        return await parse_url(doc["source_url"])
    if source == "text":
        text = storage.get_text(doc["storage_key"])
        return ParseResult(markdown=text, engine="text")
    raise ParseError("unsupported_source", f"unknown source type {source}")


async def handle_ingest(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    attempt = int(job.payload["attempt"])
    doc = await _load_current(doc_id, attempt)
    cfg = doc_config(doc)
    prefix = storage.doc_prefix(doc["tenant_id"], doc["kb_id"], doc_id)

    # 1. parse
    await _set_stage(doc_id, attempt, "parse")
    try:
        result = await _parse(doc, cfg)
    except ParseError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(exc.message) from exc
        await _fail(doc_id, attempt, exc.code, exc.message)
        return
    doc = await _load_current(doc_id, attempt)

    l1_key = f"{prefix}/l1.{attempt}.md"
    l2_key = f"{prefix}/l2.{attempt}.json"
    storage.put_text(l1_key, result.markdown)
    storage.put_text(
        l2_key,
        json.dumps(
            {
                "engine": result.engine,
                "metadata": result.metadata,
                "source_blocks": [
                    {"start": b.start, "end": b.end, "locator": b.locator} for b in result.source_blocks
                ],
            },
            ensure_ascii=False,
        ),
    )
    for asset in result.assets:
        key = f"{prefix}/assets/{_safe_asset_name(asset.name)}"
        storage.put_bytes(key, asset.data)
        await db.pool().execute(
            "INSERT INTO document_assets (document_id, name, storage_key, mime_type) VALUES ($1, $2, $3, $4)"
            " ON CONFLICT (document_id, name) DO UPDATE SET storage_key = EXCLUDED.storage_key,"
            " mime_type = EXCLUDED.mime_type",
            doc_id,
            asset.name,
            key,
            asset.mime_type,
        )
    parse_meta = {
        "engine": result.engine,
        "source_blocks": len(result.source_blocks),
        "assets": len(result.assets),
        **{k: v for k, v in result.metadata.items() if isinstance(v, str) and len(v) < 200},
    }

    # 2. chunk
    await _set_stage(doc_id, attempt, "chunk")
    chunking = cfg.chunking
    chunks, _blocks = chunk_markdown(
        result.markdown,
        doc_id=doc_id,
        chunk_size=chunking.chunk_size,
        overlap=chunking.chunk_overlap,
        separators=chunking.separators,
        strategy=chunking.strategy,
    )
    if not chunks:
        await _fail(doc_id, attempt, "empty_content", "切分后没有可索引的内容")
        return
    index = LocatorIndex(result.source_blocks)
    await db.pool().execute(
        "DELETE FROM chunks WHERE document_id = $1 AND attempt = $2 AND NOT is_active", doc_id, attempt
    )
    rows = [
        (
            str(uuid.uuid4()),
            doc["tenant_id"],
            doc["kb_id"],
            doc_id,
            attempt,
            c.seq,
            c.chunk_type,
            c.content,
            c.context_header,
            c.start,
            c.end,
            c.line_start,
            c.anchor_id,
            index.for_range(c.start, c.end),
        )
        for c in chunks
    ]
    await db.pool().executemany(
        "INSERT INTO chunks (id, tenant_id, kb_id, document_id, attempt, seq, chunk_type, content,"
        " context_header, start_offset, end_offset, line_start, anchor_id, source_locators)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
        rows,
    )

    # 3. embed + index (skipped for wiki-only knowledge bases)
    rag = cfg.indexing.rag_enabled
    if rag:
        await _set_stage(doc_id, attempt, "embed")
        texts = [(f"{c.context_header}\n\n" if c.context_header else "") + c.content for c in chunks]
        try:
            vectors = await models_client.embed(texts)
        except models_client.ModelError as exc:
            if exc.retriable and job.attempts < job.max_attempts:
                raise jobs.RetryLater(str(exc)) from exc
            await _fail(doc_id, attempt, "embedding_failed", str(exc))
            return
        await db.pool().executemany(
            "UPDATE chunks SET embedding = $2::halfvec, dimension = $3 WHERE id = $1",
            [(row[0], vector_literal(vec), len(vec)) for row, vec in zip(rows, vectors)],
        )
    questions_on = rag and cfg.questions.enabled

    # 4. swap in atomically
    old_keys: list[str] = []
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT attempt, parse_status, l1_key FROM documents WHERE id = $1 FOR UPDATE", doc_id
            )
            if current is None or current["attempt"] != attempt or current["parse_status"] in TERMINAL_STOP:
                raise jobs.Abandon("superseded before swap")
            if current["l1_key"] and current["l1_key"] != l1_key:
                old_keys.append(current["l1_key"])
                old_keys.append(current["l1_key"].replace("/l1.", "/l2.").replace(".md", ".json"))
            await conn.execute("DELETE FROM chunks WHERE document_id = $1 AND attempt <> $2", doc_id, attempt)
            await conn.execute(
                "UPDATE chunks SET is_active = true WHERE document_id = $1 AND attempt = $2", doc_id, attempt
            )
            await conn.execute(
                "UPDATE documents SET parse_status = 'completed', stage = NULL, active_attempt = $2,"
                " chunk_count = $3, l1_key = $4, l1_chars = $5, parse_meta = $6,"
                " error_code = NULL, error_message = NULL, summary_status = $8,"
                " question_status = $9, question_count = 0, wiki_status = $10, wiki_error = NULL,"
                " title = CASE WHEN source_type = 'url' AND $7 <> '' THEN $7 ELSE title END,"
                " processed_at = now(), updated_at = now() WHERE id = $1",
                doc_id,
                attempt,
                len(chunks),
                l1_key,
                len(result.markdown),
                parse_meta,
                (result.title or "")[:300],
                "pending" if cfg.summary.enabled else "none",
                "pending" if questions_on else "none",
                "pending" if cfg.wiki.enabled else "none",
            )
            payload = {"attempt": attempt}
            if cfg.summary.enabled:
                await jobs.enqueue(conn, jobs.KIND_SUMMARY, document_id=doc_id, payload=payload)
            else:
                await enqueue_after_summary(conn, doc["kb_id"], doc_id, attempt, cfg)
            if questions_on:
                await jobs.enqueue(conn, jobs.KIND_QUESTIONS, document_id=doc_id, payload=payload)
    for key in old_keys:
        try:
            storage.delete_prefix(key)
        except (OSError, ValueError):
            logger.warning("failed to remove old artifact %s", key)


async def enqueue_after_summary(conn, kb_id: str, doc_id: str, attempt: int, cfg: kb_config.KbConfig) -> None:
    """Wiki and profile build on the summary, so they are queued once it settles."""

    if cfg.wiki.enabled:
        await jobs.enqueue(
            conn, jobs.KIND_WIKI_INGEST, document_id=doc_id, kb_id=kb_id, payload={"attempt": attempt}, delay_s=5
        )
    if cfg.profile.enabled:
        await jobs.enqueue_kb_once(conn, jobs.KIND_PROFILE, kb_id, delay_s=30)


def summary_prompt(row: dict, cfg: kb_config.KbConfig, text: str) -> str:
    extra = cfg.summary.instructions.strip()
    if (row.get("file_type") or "").lower() in TABLE_FILE_TYPES and cfg.summary.table_instructions.strip():
        extra = "\n".join(x for x in (extra, "表格说明：" + cfg.summary.table_instructions.strip()) if x)
    return SUMMARY_PROMPT.format(
        language=cfg.language,
        extra=f"\n补充要求：{extra}" if extra else "",
        title=row["title"],
        text=text,
    )


def is_current_parse(row, attempt: int) -> bool:
    """The document still exists and ``attempt`` is its latest completed parse."""
    if row is None or row["deleted_at"]:
        return False
    return row["attempt"] == attempt and row["parse_status"] == "completed"


async def handle_summary(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    attempt = int(job.payload["attempt"])
    row = await db.pool().fetchrow(
        "SELECT d.title, d.l1_key, d.attempt, d.parse_status, d.deleted_at, d.kb_id, d.file_type,"
        " d.process_overrides, k.config AS kb_config"
        " FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id WHERE d.id = $1",
        doc_id,
    )
    if not is_current_parse(row, attempt):
        raise jobs.Abandon("document changed")
    row = dict(row)
    cfg = doc_config(row)
    await db.pool().execute(
        "UPDATE documents SET summary_status = 'processing' WHERE id = $1 AND attempt = $2", doc_id, attempt
    )
    text = storage.get_text(row["l1_key"])[: cfg.summary.input_chars]
    try:
        summary = await models_client.chat(
            summary_prompt(row, cfg, text), system=SUMMARY_SYSTEM, max_tokens=500, model=cfg.summary.model
        )
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        async with db.pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE documents SET summary_status = 'failed' WHERE id = $1 AND attempt = $2",
                    doc_id,
                    attempt,
                )
                await enqueue_after_summary(conn, row["kb_id"], doc_id, attempt, cfg)
        return
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE documents SET summary = $3, summary_status = 'completed', updated_at = now()"
                " WHERE id = $1 AND attempt = $2",
                doc_id,
                attempt,
                summary,
            )
            await enqueue_after_summary(conn, row["kb_id"], doc_id, attempt, cfg)


async def handle_delete(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    row = await db.pool().fetchrow(
        "SELECT d.tenant_id, d.kb_id, d.title, k.config AS kb_config"
        " FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id WHERE d.id = $1",
        doc_id,
    )
    if row is None:
        return
    cfg = kb_config.load(row["kb_config"])
    has_pages = await db.pool().fetchval(
        "SELECT EXISTS (SELECT 1 FROM wiki_pages WHERE kb_id = $1 AND $2 = ANY(source_doc_ids)"
        " AND deleted_at IS NULL)",
        row["kb_id"],
        doc_id,
    )
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            if has_pages:
                await jobs.enqueue(
                    conn,
                    jobs.KIND_WIKI_RETRACT,
                    document_id=doc_id,
                    kb_id=row["kb_id"],
                    payload={"title": row["title"]},
                )
            if cfg.profile.enabled:
                await jobs.enqueue_kb_once(conn, jobs.KIND_PROFILE, row["kb_id"], delay_s=30)
    await db.pool().execute("DELETE FROM chunks WHERE document_id = $1", doc_id)
    await db.pool().execute("DELETE FROM document_assets WHERE document_id = $1", doc_id)
    storage.delete_prefix(storage.doc_prefix(row["tenant_id"], row["kb_id"], doc_id))
    await db.pool().execute(
        "UPDATE documents SET parse_status = 'deleted', stage = NULL, chunk_count = 0, updated_at = now()"
        " WHERE id = $1",
        doc_id,
    )


def handlers() -> dict:
    from . import profile, questions
    from .wiki import jobs as wiki_jobs

    return {
        jobs.KIND_INGEST: handle_ingest,
        jobs.KIND_SUMMARY: handle_summary,
        jobs.KIND_DELETE: handle_delete,
        jobs.KIND_QUESTIONS: questions.handle_questions,
        jobs.KIND_WIKI_INGEST: wiki_jobs.handle_ingest,
        jobs.KIND_WIKI_RETRACT: wiki_jobs.handle_retract,
        jobs.KIND_WIKI_FINALIZE: wiki_jobs.handle_finalize,
        jobs.KIND_PROFILE: profile.handle_profile,
    }
