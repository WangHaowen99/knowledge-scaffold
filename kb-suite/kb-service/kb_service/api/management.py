"""Management API used by the Swarm plugin backend (I2)."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .. import auth, db, documents, jobs, kb_config, profile, questions, retrieval, storage
from ..config import get_settings
from ..parsing import SUPPORTED_TYPES, docreader_client
from ..wiki import jobs as wiki_jobs
from .common import Caller, need

router = APIRouter(prefix="/v1")

DOC_FIELDS = (
    "id", "kb_id", "title", "file_name", "file_type", "file_size", "folder_path", "source_type",
    "source_url", "parse_status", "stage", "error_code", "error_message", "summary", "summary_status",
    "chunk_count", "l1_chars", "created_at", "processed_at", "question_status", "question_count",
    "wiki_status", "wiki_error", "process_overrides",
)


def _config_error(exc: kb_config.ConfigError) -> documents.DocumentError:
    return documents.DocumentError(400, "invalid_config", str(exc))


def _overrides(raw: dict | None) -> dict:
    try:
        return kb_config.validate_overrides(raw)
    except kb_config.ConfigError as exc:
        raise _config_error(exc) from exc


def _doc_view(doc: dict) -> dict:
    view = {k: doc.get(k) for k in DOC_FIELDS}
    view["engine"] = (doc.get("parse_meta") or {}).get("engine")
    return view


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    config: dict | None = None


class KbPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    config: dict | None = None


def _config_patch(body: KbCreate | KbPatch) -> dict:
    patch = dict(body.config or {})
    flat = {k: v for k, v in (("chunk_size", body.chunk_size), ("chunk_overlap", body.chunk_overlap)) if v is not None}
    if flat:
        patch["chunking"] = {**flat, **(patch.get("chunking") or {})}
    return patch


async def _kb_view(row) -> dict:
    stats = await db.pool().fetchrow(
        "SELECT count(*) FILTER (WHERE deleted_at IS NULL) AS documents,"
        " count(*) FILTER (WHERE deleted_at IS NULL AND parse_status = 'completed') AS completed,"
        " count(*) FILTER (WHERE deleted_at IS NULL AND parse_status IN ('pending','processing')) AS processing,"
        " count(*) FILTER (WHERE deleted_at IS NULL AND parse_status = 'failed') AS failed,"
        " coalesce(sum(chunk_count) FILTER (WHERE deleted_at IS NULL), 0) AS chunks,"
        " coalesce(sum(question_count) FILTER (WHERE deleted_at IS NULL), 0) AS questions,"
        " count(*) FILTER (WHERE deleted_at IS NULL AND (summary_status IN ('pending','processing')"
        "   OR question_status IN ('pending','processing') OR wiki_status IN ('pending','processing'))) AS enriching"
        " FROM documents WHERE kb_id = $1",
        row["id"],
    )
    wiki_pages = await db.pool().fetchval(
        "SELECT count(*) FROM wiki_pages WHERE kb_id = $1 AND deleted_at IS NULL", row["id"]
    )
    config = kb_config.load(row["config"])
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "embed_model": row["embed_model"],
        "embed_dim": row["embed_dim"],
        "chunk_size": config.chunking.chunk_size,
        "chunk_overlap": config.chunking.chunk_overlap,
        "config": config.model_dump(),
        "profile": row["profile"],
        "profile_status": row["profile_status"],
        "profile_updated_at": row["profile_updated_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "stats": {**{k: int(v) for k, v in dict(stats).items()}, "wiki_pages": int(wiki_pages or 0)},
    }


@router.get("/config/meta")
async def config_meta(caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    meta = kb_config.describe()
    meta["parser_engines"] = await docreader_client.list_engines()
    meta["file_types"] = sorted(SUPPORTED_TYPES)
    return meta


@router.get("/me")
async def me(caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    p = caller.principal
    return {"key": p.name, "tenant_id": caller.tenant_id, "capabilities": sorted(p.capabilities)}


@router.get("/kbs")
async def list_kbs(caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    kb_ids = await caller.kb_scope(None)
    rows = await db.pool().fetch(
        "SELECT * FROM knowledge_bases WHERE id = ANY($1::text[]) ORDER BY created_at", kb_ids
    )
    return {"items": [await _kb_view(r) for r in rows]}


@router.post("/kbs", status_code=201)
async def create_kb(body: KbCreate, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    settings = get_settings()
    try:
        config = kb_config.apply_patch({}, _config_patch(body))
    except kb_config.ConfigError as exc:
        raise _config_error(exc) from exc
    try:
        row = await db.pool().fetchrow(
            "INSERT INTO knowledge_bases (id, tenant_id, name, description, config, embed_model, embed_dim)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            str(uuid.uuid4()),
            caller.tenant_id,
            body.name.strip(),
            body.description,
            config,
            settings.embed_model,
            settings.embed_dim,
        )
    except Exception as exc:  # noqa: BLE001
        if "knowledge_bases_tenant_name" in str(exc):
            raise documents.DocumentError(409, "duplicate_name", "已存在同名知识库") from exc
        raise
    return await _kb_view(row)


@router.get("/kbs/{kb_id}")
async def get_kb(kb_id: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    await caller.check_kb(kb_id)
    row = await db.pool().fetchrow("SELECT * FROM knowledge_bases WHERE id = $1", kb_id)
    return await _kb_view(row)


@router.patch("/kbs/{kb_id}")
async def patch_kb(kb_id: str, body: KbPatch, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    await caller.check_kb(kb_id)
    row = await db.pool().fetchrow("SELECT * FROM knowledge_bases WHERE id = $1", kb_id)
    try:
        config = kb_config.apply_patch(row["config"], _config_patch(body))
    except kb_config.ConfigError as exc:
        raise _config_error(exc) from exc
    row = await db.pool().fetchrow(
        "UPDATE knowledge_bases SET name = coalesce($2, name), description = coalesce($3, description),"
        " config = $4, updated_at = now() WHERE id = $1 RETURNING *",
        kb_id,
        body.name.strip() if body.name else None,
        body.description,
        config,
    )
    return await _kb_view(row)


@router.delete("/kbs/{kb_id}", status_code=204)
async def delete_kb(kb_id: str, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> None:
    await caller.check_kb(kb_id)
    doc_ids = [
        r["id"]
        for r in await db.pool().fetch(
            "SELECT id FROM documents WHERE kb_id = $1 AND deleted_at IS NULL", kb_id
        )
    ]
    for doc_id in doc_ids:
        await documents.delete(caller.tenant_id, doc_id)
    await db.pool().execute(
        "UPDATE knowledge_bases SET deleted_at = now(), updated_at = now() WHERE id = $1", kb_id
    )


class KbReprocess(BaseModel):
    target: Literal["all", "summary", "questions", "wiki", "profile"]
    reset_wiki: bool = False


@router.post("/kbs/{kb_id}/reprocess")
async def reprocess_kb(kb_id: str, body: KbReprocess, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    """Apply changed settings to existing documents."""

    await caller.check_kb(kb_id)
    if body.target == "wiki":
        return {"target": "wiki", "queued": await wiki_jobs.rebuild(kb_id, reset=body.reset_wiki)}
    if body.target == "profile":
        async with db.pool().acquire() as conn:
            await jobs.enqueue_kb_once(conn, jobs.KIND_PROFILE, kb_id, delay_s=0)
        return {"target": "profile", "queued": 1}
    rows = await db.pool().fetch(
        "SELECT id, attempt FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND parse_status = ANY($2::text[]) ORDER BY created_at",
        kb_id,
        ["completed", "failed"] if body.target == "all" else ["completed"],
    )
    if body.target == "all":
        for row in rows:
            await documents.reprocess(caller.tenant_id, row["id"])
        return {"target": "all", "queued": len(rows)}
    kind, column = (
        (jobs.KIND_SUMMARY, "summary_status") if body.target == "summary" else (jobs.KIND_QUESTIONS, "question_status")
    )
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            for i, row in enumerate(rows):
                await jobs.enqueue(
                    conn, kind, document_id=row["id"], payload={"attempt": row["attempt"]}, delay_s=i * 0.5
                )
            await conn.execute(
                f"UPDATE documents SET {column} = 'pending' WHERE id = ANY($1::text[])", [r["id"] for r in rows]
            )
    return {"target": body.target, "queued": len(rows)}


@router.post("/kbs/{kb_id}/profile/generate")
async def generate_profile(kb_id: str, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    await caller.check_kb(kb_id)
    text = await profile.generate(kb_id, force=True)
    return {"profile": text}


@router.post("/kbs/{kb_id}/profile/apply")
async def apply_profile(kb_id: str, caller: Caller = Depends(need(auth.CAP_MANAGE))) -> dict:
    """Use the generated profile as the knowledge base description."""

    await caller.check_kb(kb_id)
    row = await db.pool().fetchrow(
        "UPDATE knowledge_bases SET description = coalesce(profile, description), updated_at = now()"
        " WHERE id = $1 RETURNING *",
        kb_id,
    )
    return await _kb_view(row)


@router.get("/kbs/{kb_id}/documents")
async def list_docs(
    kb_id: str,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    caller: Caller = Depends(need(auth.CAP_RETRIEVE)),
) -> dict:
    await caller.check_kb(kb_id)
    rows = await db.pool().fetch(
        "SELECT * FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND ($2::text IS NULL OR parse_status = $2)"
        " AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%' OR file_name ILIKE '%' || $3 || '%')"
        " ORDER BY created_at DESC LIMIT $4 OFFSET $5",
        kb_id,
        status,
        q,
        limit,
        offset,
    )
    total = await db.pool().fetchval(
        "SELECT count(*) FROM documents WHERE kb_id = $1 AND deleted_at IS NULL"
        " AND ($2::text IS NULL OR parse_status = $2)"
        " AND ($3::text IS NULL OR title ILIKE '%' || $3 || '%' OR file_name ILIKE '%' || $3 || '%')",
        kb_id,
        status,
        q,
    )
    return {"items": [_doc_view(dict(r)) for r in rows], "total": int(total)}


def _check_size(size: int) -> None:
    limit = get_settings().max_upload_mb * 1024 * 1024
    if size > limit:
        raise documents.DocumentError(413, "too_large", f"文件超过 {get_settings().max_upload_mb}MB 上限")


@router.post("/kbs/{kb_id}/documents", status_code=201)
async def upload_doc(
    kb_id: str,
    file: UploadFile = File(...),
    folder_path: str = Form(""),
    process_overrides: str = Form(""),
    caller: Caller = Depends(need(auth.CAP_INGEST)),
) -> dict:
    await caller.check_kb(kb_id)
    try:
        overrides = _overrides(json.loads(process_overrides) if process_overrides else None)
    except ValueError as exc:
        raise documents.DocumentError(400, "invalid_config", "process_overrides 不是合法 JSON") from exc
    data = await file.read()
    _check_size(len(data))
    doc = await documents.create_file_document(
        caller.tenant_id, kb_id, file.filename or "file", data, folder_path=folder_path, overrides=overrides
    )
    return _doc_view(doc)


class TextDoc(BaseModel):
    title: str = ""
    text: str
    folder_path: str = ""
    process_overrides: dict | None = None


@router.post("/kbs/{kb_id}/documents/text", status_code=201)
async def create_text_doc(kb_id: str, body: TextDoc, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    await caller.check_kb(kb_id)
    doc = await documents.create_text_document(
        caller.tenant_id, kb_id, body.title, body.text, folder_path=body.folder_path,
        overrides=_overrides(body.process_overrides),
    )
    return _doc_view(doc)


class UrlDoc(BaseModel):
    url: str
    folder_path: str = ""
    process_overrides: dict | None = None


@router.post("/kbs/{kb_id}/documents/url", status_code=201)
async def create_url_doc(kb_id: str, body: UrlDoc, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    await caller.check_kb(kb_id)
    doc = await documents.create_url_document(
        caller.tenant_id, kb_id, body.url.strip(), folder_path=body.folder_path,
        overrides=_overrides(body.process_overrides),
    )
    return _doc_view(doc)


async def _doc_for(caller: Caller, doc_id: str) -> dict:
    doc = await retrieval.get_document(caller.tenant_id, doc_id)
    if doc is None:
        raise documents.DocumentError(404, "not_found", "文档不存在")
    await caller.check_kb(doc["kb_id"])
    return doc


@router.get("/documents/{doc_id}")
async def get_doc(doc_id: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    return _doc_view(await _doc_for(caller, doc_id))


@router.get("/documents/{doc_id}/chunks")
async def get_chunks(
    doc_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=200),
    caller: Caller = Depends(need(auth.CAP_RETRIEVE)),
) -> dict:
    await _doc_for(caller, doc_id)
    items, total = await retrieval.read_chunks(doc_id, offset=offset, limit=limit)
    qmap = await questions.questions_for_chunks([c["id"] for c in items])
    for item in items:
        item["questions"] = qmap.get(item["id"], [])
    return {"items": items, "total": total, "offset": offset}


@router.post("/documents/{doc_id}/questions/regenerate")
async def regenerate_questions(doc_id: str, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    doc = await _doc_for(caller, doc_id)
    if doc["parse_status"] != "completed":
        raise documents.DocumentError(409, "not_ready", "文档还没有解析完成")
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("UPDATE documents SET question_status = 'pending' WHERE id = $1", doc_id)
            await jobs.enqueue(conn, jobs.KIND_QUESTIONS, document_id=doc_id, payload={"attempt": doc["attempt"]})
    return {"doc_id": doc_id, "question_status": "pending"}


class ChunkQuestions(BaseModel):
    questions: list[str] = Field(default_factory=list, max_length=20)


@router.put("/chunks/{chunk_id}/questions")
async def put_chunk_questions(
    chunk_id: str, body: ChunkQuestions, caller: Caller = Depends(need(auth.CAP_INGEST))
) -> dict:
    doc_id = await db.pool().fetchval("SELECT document_id FROM chunks WHERE id = $1 AND is_active", chunk_id)
    if doc_id is None:
        raise documents.DocumentError(404, "not_found", "分块不存在")
    doc = await _doc_for(caller, doc_id)
    saved = await questions.replace_chunk_questions(doc, chunk_id, body.questions)
    return {"chunk_id": chunk_id, "questions": saved}


@router.get("/documents/{doc_id}/content")
async def get_content(
    doc_id: str, level: str = Query("L1", pattern="^L[012]$"), caller: Caller = Depends(need(auth.CAP_RETRIEVE))
) -> PlainTextResponse:
    doc = await _doc_for(caller, doc_id)
    if level == "L0":
        text = doc.get("summary") or ""
    elif level == "L1":
        text = retrieval.read_l1(doc)
    else:
        text = retrieval.read_l2(doc)
    media = "application/json" if level == "L2" else "text/markdown"
    return PlainTextResponse(text, media_type=f"{media}; charset=utf-8")


@router.get("/documents/{doc_id}/file")
async def get_file(doc_id: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> FileResponse:
    doc = await _doc_for(caller, doc_id)
    if doc["source_type"] != "file" or not doc.get("storage_key"):
        raise documents.DocumentError(404, "no_file", "该文档没有原始文件")
    return FileResponse(storage.path_of(doc["storage_key"]), filename=doc.get("file_name") or "file")


@router.get("/documents/{doc_id}/assets/{name:path}")
async def get_asset(doc_id: str, name: str, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> FileResponse:
    await _doc_for(caller, doc_id)
    row = await db.pool().fetchrow(
        "SELECT storage_key, mime_type FROM document_assets WHERE document_id = $1 AND name = $2", doc_id, name
    )
    if row is None:
        raise documents.DocumentError(404, "not_found", "资源不存在")
    return FileResponse(storage.path_of(row["storage_key"]), media_type=row["mime_type"] or None)


@router.post("/documents/{doc_id}/reprocess")
async def reprocess_doc(doc_id: str, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    await _doc_for(caller, doc_id)
    return await documents.reprocess(caller.tenant_id, doc_id)


@router.post("/documents/{doc_id}/cancel")
async def cancel_doc(doc_id: str, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    await _doc_for(caller, doc_id)
    return await documents.cancel(caller.tenant_id, doc_id)


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_doc(doc_id: str, caller: Caller = Depends(need(auth.CAP_INGEST))) -> None:
    await _doc_for(caller, doc_id)
    await documents.delete(caller.tenant_id, doc_id)


# ---- chunked uploads (large files from the Swarm plugin) -----------------


class UploadInit(BaseModel):
    kb_id: str
    file_name: str
    file_size: int = Field(gt=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    folder_path: str = ""
    process_overrides: dict | None = None


def _upload_dir(upload_id: str) -> Path:
    return Path(get_settings().files_dir) / "_uploads" / upload_id


@router.post("/uploads", status_code=201)
async def init_upload(body: UploadInit, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    await caller.check_kb(body.kb_id)
    _check_size(body.file_size)
    part_size = get_settings().upload_part_mb * 1024 * 1024
    part_count = max(1, math.ceil(body.file_size / part_size))
    upload_id = str(uuid.uuid4())
    await db.pool().execute(
        "INSERT INTO upload_sessions (id, tenant_id, kb_id, file_name, file_size, sha256, folder_path,"
        " part_size, part_count, expires_at, process_overrides)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now() + interval '1 day', $10)",
        upload_id,
        caller.tenant_id,
        body.kb_id,
        body.file_name,
        body.file_size,
        (body.sha256 or "").lower(),
        body.folder_path,
        part_size,
        part_count,
        _overrides(body.process_overrides),
    )
    _upload_dir(upload_id).mkdir(parents=True, exist_ok=True)
    return {"upload_id": upload_id, "part_size": part_size, "part_count": part_count}


async def _session(caller: Caller, upload_id: str) -> dict:
    row = await db.pool().fetchrow(
        "SELECT * FROM upload_sessions WHERE id = $1 AND tenant_id = $2 AND status = 'open' AND expires_at > now()",
        upload_id,
        caller.tenant_id,
    )
    if row is None:
        raise documents.DocumentError(404, "upload_not_found", "上传会话不存在或已过期")
    return dict(row)


@router.put("/uploads/{upload_id}/parts/{index}")
async def put_part(upload_id: str, index: int, request: Request, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    session = await _session(caller, upload_id)
    if not 0 <= index < session["part_count"]:
        raise documents.DocumentError(400, "bad_part", "分块序号越界")
    data = await request.body()
    if len(data) > session["part_size"]:
        raise documents.DocumentError(400, "bad_part", "分块过大")
    (_upload_dir(upload_id) / f"{index:06d}").write_bytes(data)
    await db.pool().execute(
        "UPDATE upload_sessions SET parts_received = array(SELECT DISTINCT unnest(parts_received || $2::int))"
        " WHERE id = $1",
        upload_id,
        index,
    )
    return {"upload_id": upload_id, "index": index, "size": len(data)}


@router.post("/uploads/{upload_id}/complete", status_code=201)
async def complete_upload(upload_id: str, caller: Caller = Depends(need(auth.CAP_INGEST))) -> dict:
    session = await _session(caller, upload_id)
    folder = _upload_dir(upload_id)
    digest = hashlib.sha256()
    chunks = []
    for index in range(session["part_count"]):
        part = folder / f"{index:06d}"
        if not part.exists():
            raise documents.DocumentError(400, "missing_part", f"缺少分块 {index}")
        data = part.read_bytes()
        digest.update(data)
        chunks.append(data)
    data = b"".join(chunks)
    if len(data) != session["file_size"] or (session["sha256"] and digest.hexdigest() != session["sha256"]):
        raise documents.DocumentError(400, "checksum_mismatch", "文件大小或校验值不匹配")
    doc = await documents.create_file_document(
        caller.tenant_id, session["kb_id"], session["file_name"], data, folder_path=session["folder_path"],
        overrides=session.get("process_overrides") or {},
    )
    await db.pool().execute("UPDATE upload_sessions SET status = 'completed' WHERE id = $1", upload_id)
    shutil.rmtree(folder, ignore_errors=True)
    return _doc_view(doc)


# ---- search test ----------------------------------------------------------


class SearchBody(BaseModel):
    query: str = Field(min_length=1)
    kb_ids: list[str] | None = None
    top_k: int = Field(default=8, ge=1, le=50)
    rerank: bool | None = None
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    include_wiki: bool = True
    # Per-request overrides of the knowledge bases' retrieval settings.
    options: dict | None = None
    trace: bool = False


@router.post("/search")
async def search(body: SearchBody, caller: Caller = Depends(need(auth.CAP_RETRIEVE))) -> dict:
    kb_ids = await caller.kb_scope(body.kb_ids)
    trace: dict | None = {} if body.trace else None
    try:
        hits = await retrieval.search(
            caller.tenant_id,
            kb_ids,
            body.query,
            top_k=body.top_k,
            use_rerank=body.rerank,
            min_score=body.min_score,
            options=body.options,
            include_wiki=body.include_wiki,
            trace=trace,
        )
    except kb_config.ConfigError as exc:
        raise _config_error(exc) from exc
    out = {"query": body.query, "items": [h.as_snake() for h in hits]}
    if trace is not None:
        out["trace"] = trace
    return out
