"""AI question generation per chunk (WeKnora ``questionGeneration``).

Each text chunk gets a few questions it answers well; every question is
embedded and BM25-indexed as its own row in ``chunk_questions`` and a hit on
it resolves to the parent chunk. Prompt adapted from WeKnora
config/prompt_templates/generate_questions.yaml (MIT License, Copyright (C)
2026 Tencent).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from . import db, jobs, models_client
from .kb_config import KbConfig
from .pipeline import doc_config, vector_literal

logger = logging.getLogger(__name__)

MIN_CHUNK_CHARS = 20
CONTEXT_CHARS = 600
QUESTION_TYPES = {"text", "table"}

PROMPT = """You are a question generation assistant optimizing for search retrieval. Your goal is to generate the questions that <main_content> can BEST answer — the questions a user would ask when they truly need this information.
Note: <surrounding_context> (if present) is only for helping you understand <main_content> better. Generate questions ONLY about <main_content>.

{context}
<main_content>
Document name: {doc_name}

{content}
</main_content>

## Question Quality Rules
- Focus on questions where this content provides a COMPLETE or SUBSTANTIAL answer, not just a passing mention
- Prioritize questions about the main theme, key concepts, how-tos, and conclusions — NOT trivial details or isolated facts
- Each question must be self-contained: NO pronouns or references (e.g. "it", "this", "该文档"); use specific names
- Questions should reflect real user search intent, within 30 words each

## What NOT to Generate
- Questions about minor details that are only briefly mentioned
- Questions answerable with a single word or number copied from the text
- Questions too broad to be meaningfully answered by this specific content
{instructions}
## Output
Generate {count} questions, one per line, no numbering or prefixes. Write the questions in {language}."""

_PREFIX_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)、．]|[（(]?\d+[）)]|Q\d*[:：]|问题\s*\d*[:：])\s*")


def parse_questions(text: str, count: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        q = _PREFIX_RE.sub("", line).strip().strip('"“”')
        if len(q.encode("utf-8")) <= 5 or q.startswith("#"):
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q[:300])
        if len(out) >= count:
            break
    return out


def build_prompt(title: str, chunk: dict, prev: dict | None, nxt: dict | None, cfg: KbConfig) -> str:
    context_parts = []
    if prev:
        context_parts.append(f"[前文]\n{prev['content'][-CONTEXT_CHARS:]}")
    if nxt:
        context_parts.append(f"[后文]\n{nxt['content'][:CONTEXT_CHARS]}")
    context = (
        "<surrounding_context>\n" + "\n\n".join(context_parts) + "\n</surrounding_context>" if context_parts else ""
    )
    body = (f"{chunk['context_header']}\n\n" if chunk["context_header"] else "") + chunk["content"]
    extra = cfg.questions.instructions.strip()
    return PROMPT.format(
        context=context,
        doc_name=title,
        content=body,
        instructions=f"\n## Additional Requirements\n{extra}\n" if extra else "",
        count=cfg.questions.count,
        language=cfg.language,
    )


async def _load(doc_id: str, attempt: int) -> dict:
    row = await db.pool().fetchrow(
        "SELECT d.id, d.tenant_id, d.kb_id, d.title, d.attempt, d.parse_status, d.deleted_at,"
        " d.process_overrides, k.config AS kb_config"
        " FROM documents d JOIN knowledge_bases k ON k.id = d.kb_id WHERE d.id = $1",
        doc_id,
    )
    if row is None or row["deleted_at"] or row["attempt"] != attempt or row["parse_status"] != "completed":
        raise jobs.Abandon("document changed")
    return dict(row)


async def generate_for_chunks(
    title: str, chunks: list[dict], cfg: KbConfig
) -> tuple[dict[str, list[str]], int, bool]:
    """Returns ({chunk_id: questions}, failures, any_retriable_failure)."""

    by_seq = {c["seq"]: c for c in chunks}
    targets = [c for c in chunks if c["chunk_type"] in QUESTION_TYPES and len(c["content"].strip()) >= MIN_CHUNK_CHARS]
    failures = 0
    retriable = False

    async def one(chunk: dict) -> tuple[str, list[str]]:
        nonlocal failures, retriable
        prompt = build_prompt(title, chunk, by_seq.get(chunk["seq"] - 1), by_seq.get(chunk["seq"] + 1), cfg)
        try:
            text = await models_client.chat(prompt, max_tokens=512, model=cfg.questions.model, temperature=0.7)
        except models_client.ModelError as exc:
            failures += 1
            retriable = retriable or exc.retriable
            logger.warning("question generation failed for chunk %s: %s", chunk["id"], exc)
            return chunk["id"], []
        return chunk["id"], parse_questions(text, cfg.questions.count)

    results = await asyncio.gather(*(one(c) for c in targets))
    return {cid: qs for cid, qs in results if qs}, failures, retriable


async def store_questions(doc: dict, questions: dict[str, list[str]], *, replace_doc: bool = True) -> int:
    """Embed and write questions; ``replace_doc`` drops the document's old rows first."""

    rows: list[tuple] = []
    for chunk_id, qs in questions.items():
        for seq, q in enumerate(qs):
            rows.append((str(uuid.uuid4()), chunk_id, seq, q))
    vectors = await models_client.embed([f"{doc['title']}\n{q}" for *_, q in rows]) if rows else []
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            if replace_doc:
                await conn.execute("DELETE FROM chunk_questions WHERE document_id = $1", doc["id"])
            else:
                await conn.execute(
                    "DELETE FROM chunk_questions WHERE chunk_id = ANY($1::text[])", list(questions)
                )
            await conn.executemany(
                "INSERT INTO chunk_questions (id, tenant_id, kb_id, document_id, chunk_id, seq, question,"
                " embedding, dimension)"
                " SELECT $1, $2, $3, $4, $5, $6, $7, $8::halfvec, $9"
                " WHERE EXISTS (SELECT 1 FROM chunks WHERE id = $5)",
                [
                    (qid, doc["tenant_id"], doc["kb_id"], doc["id"], chunk_id, seq, q, vector_literal(vec), len(vec))
                    for (qid, chunk_id, seq, q), vec in zip(rows, vectors)
                ],
            )
            total = await conn.fetchval("SELECT count(*) FROM chunk_questions WHERE document_id = $1", doc["id"])
            await conn.execute(
                "UPDATE documents SET question_count = $2, updated_at = now() WHERE id = $1", doc["id"], int(total)
            )
    return int(total)


async def handle_questions(job: jobs.Job) -> None:
    doc_id = job.document_id or ""
    attempt = int(job.payload["attempt"])
    doc = await _load(doc_id, attempt)
    cfg = doc_config(doc)
    if not (cfg.questions.enabled and cfg.indexing.rag_enabled):
        await db.pool().execute(
            "UPDATE documents SET question_status = 'none' WHERE id = $1 AND attempt = $2", doc_id, attempt
        )
        return
    await db.pool().execute(
        "UPDATE documents SET question_status = 'processing' WHERE id = $1 AND attempt = $2", doc_id, attempt
    )
    chunks = [
        dict(r)
        for r in await db.pool().fetch(
            "SELECT id, seq, chunk_type, content, context_header FROM chunks"
            " WHERE document_id = $1 AND attempt = $2 AND is_active ORDER BY seq",
            doc_id,
            attempt,
        )
    ]
    questions, failures, retriable = await generate_for_chunks(doc["title"], chunks, cfg)
    if not questions and failures:
        if retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(f"question generation failed for {failures} chunks")
        await db.pool().execute(
            "UPDATE documents SET question_status = 'failed' WHERE id = $1 AND attempt = $2", doc_id, attempt
        )
        return
    await _load(doc_id, attempt)
    try:
        await store_questions(doc, questions)
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        await db.pool().execute(
            "UPDATE documents SET question_status = 'failed' WHERE id = $1 AND attempt = $2", doc_id, attempt
        )
        return
    await db.pool().execute(
        "UPDATE documents SET question_status = $3 WHERE id = $1 AND attempt = $2",
        doc_id,
        attempt,
        "partial" if failures else "completed",
    )


async def replace_chunk_questions(doc: dict, chunk_id: str, questions: list[str]) -> list[str]:
    cleaned = [q.strip()[:300] for q in questions if q.strip()]
    await store_questions(doc, {chunk_id: cleaned}, replace_doc=False)
    return cleaned


async def questions_for_chunks(chunk_ids: list[str]) -> dict[str, list[str]]:
    if not chunk_ids:
        return {}
    rows = await db.pool().fetch(
        "SELECT chunk_id, question FROM chunk_questions WHERE chunk_id = ANY($1::text[]) ORDER BY chunk_id, seq",
        chunk_ids,
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["chunk_id"], []).append(r["question"])
    return out
