"""Generated knowledge-base description (WeKnora ``profile_config``).

Aggregates document titles and summaries into a short description that tells
agents what the knowledge base covers. Runs debounced after documents are
summarised or deleted; the model is called only when the inputs changed.
"""

from __future__ import annotations

import hashlib
import logging

from . import db, jobs, kb_config, models_client
from .wiki import prompts

logger = logging.getLogger(__name__)

MAX_DOCS = 80


async def generate(kb_id: str, *, force: bool = False) -> str | None:
    row = await db.pool().fetchrow(
        "SELECT config, profile, profile_status, profile_digest, deleted_at FROM knowledge_bases WHERE id = $1",
        kb_id,
    )
    if row is None or row["deleted_at"]:
        return None
    cfg = kb_config.load(row["config"])
    docs = await db.pool().fetch(
        "SELECT title, summary FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND parse_status = 'completed' ORDER BY created_at LIMIT $2",
        kb_id,
        MAX_DOCS,
    )
    if not docs:
        await db.pool().execute(
            "UPDATE knowledge_bases SET profile = NULL, profile_status = 'none' WHERE id = $1", kb_id
        )
        return None
    listing = "\n".join(f"- {d['title']}：{(d['summary'] or '')[:300]}" for d in docs)
    digest = hashlib.sha1((listing + cfg.profile.instructions + cfg.language).encode("utf-8")).hexdigest()[:16]
    if not force and row["profile_digest"] == digest and row["profile"]:
        return row["profile"]
    await db.pool().execute("UPDATE knowledge_bases SET profile_status = 'processing' WHERE id = $1", kb_id)
    extra = cfg.profile.instructions.strip()
    text = await models_client.chat(
        prompts.render(
            prompts.PROFILE,
            Documents=listing,
            Language=cfg.language,
            ExtraInstructions=f"4. 补充要求：{extra}" if extra else "",
        ),
        model=cfg.profile.model,
        max_tokens=400,
    )
    await db.pool().execute(
        "UPDATE knowledge_bases SET profile = $2, profile_status = 'completed', profile_digest = $3,"
        " profile_updated_at = now() WHERE id = $1",
        kb_id,
        text.strip(),
        digest,
    )
    return text.strip()


async def handle_profile(job: jobs.Job) -> None:
    kb_id = job.kb_id or ""
    row = await db.pool().fetchrow("SELECT config FROM knowledge_bases WHERE id = $1", kb_id)
    if row is None or not kb_config.load(row["config"]).profile.enabled:
        return
    try:
        await generate(kb_id)
    except models_client.ModelError as exc:
        if exc.retriable and job.attempts < job.max_attempts:
            raise jobs.RetryLater(str(exc)) from exc
        await db.pool().execute("UPDATE knowledge_bases SET profile_status = 'failed' WHERE id = $1", kb_id)
