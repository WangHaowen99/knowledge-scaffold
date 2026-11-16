"""Document lifecycle operations shared by the HTTP APIs."""

from __future__ import annotations

import hashlib
import uuid

import asyncpg

from . import db, jobs, storage, url_guard
from .parsing import SUPPORTED_TYPES, file_type_of

MAX_FILE_NAME_CHARS = 255
# Storage file names stay well under the common 255-byte limit (CJK takes 3 bytes, ".tmp" is appended).
MAX_STORED_NAME_BYTES = 180
_MAX_EXTENSION = 16


class DocumentError(Exception):
    def __init__(self, status: int, code: str, message: str, *, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def _split_extension(name: str) -> tuple[str, str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or not ext or len(ext) > _MAX_EXTENSION:
        return name, ""
    return stem, "." + ext


def clean_file_name(name: str) -> str:
    """The name a user sees: last path segment, no control characters, at most 255 characters.

    ``../../evil.md``, ``..\\..\\evil.md`` and ``/etc/evil.md`` all become
    ``evil.md``; a longer name is cut before its extension.
    """

    base = name.replace("\\", "/").split("/")[-1]
    base = "".join(ch for ch in base if ch >= " " and ch != "\x7f").strip()
    if not base.strip("."):
        return "file"
    if len(base) > MAX_FILE_NAME_CHARS:
        stem, ext = _split_extension(base)
        base = stem[: MAX_FILE_NAME_CHARS - len(ext)] + ext
    return base


def _title_from_name(name: str) -> str:
    base = clean_file_name(name)
    return base.rsplit(".", 1)[0] if "." in base else base


def _duplicate_message(source_type: str, existing: dict | None) -> str:
    message = "知识库里已有相同网址的文档" if source_type == "url" else "知识库里已有相同内容的文档"
    if existing and existing["parse_status"] == "failed":
        message += "（那篇文档解析失败了，可以在列表里重新解析，或删除后再导入）"
    return message


async def _insert(
    conn: asyncpg.Connection,
    *,
    tenant_id: str,
    kb_id: str,
    source_type: str,
    title: str,
    content_hash: str | None,
    folder_path: str,
    file_name: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    source_url: str | None = None,
    overrides: dict | None = None,
) -> dict:
    doc_id = str(uuid.uuid4())
    prefix = storage.doc_prefix(tenant_id, kb_id, doc_id)
    storage_key = None
    if source_type == "file":
        storage_key = f"{prefix}/source/{_safe_name(file_name or 'file')}"
    elif source_type == "text":
        storage_key = f"{prefix}/source/text.md"
    try:
        # A savepoint: a duplicate must leave the caller's transaction usable for the lookup below.
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO documents (id, tenant_id, kb_id, source_type, file_name, file_type, file_size,"
                " storage_key, source_url, content_hash, folder_path, title, process_overrides)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *",
                doc_id,
                tenant_id,
                kb_id,
                source_type,
                file_name,
                file_type,
                file_size,
                storage_key,
                source_url,
                content_hash,
                folder_path,
                title[:300],
                overrides or {},
            )
    except asyncpg.UniqueViolationError as exc:
        existing = await conn.fetchrow(
            "SELECT id, parse_status FROM documents WHERE kb_id = $1 AND content_hash = $2 AND deleted_at IS NULL",
            kb_id,
            content_hash,
        )
        raise DocumentError(
            409,
            "duplicate",
            _duplicate_message(source_type, existing),
            extra={
                "existingDocId": existing["id"] if existing else None,
                "existingStatus": existing["parse_status"] if existing else None,
            },
        ) from exc
    return dict(row)


def _safe_name(name: str) -> str:
    """Storage file name: letters, digits and ``._-`` of the cleaned name, at most 180 UTF-8 bytes."""

    base = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in clean_file_name(name)).lstrip(".")
    stem, ext = _split_extension(base or "file")
    budget = MAX_STORED_NAME_BYTES - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore") or "file"
    return stem + ext


async def create_file_document(
    tenant_id: str, kb_id: str, file_name: str, data: bytes, *, folder_path: str = "", overrides: dict | None = None
) -> dict:
    file_name = clean_file_name(file_name)
    file_type = file_type_of(file_name)
    if file_type not in SUPPORTED_TYPES:
        raise DocumentError(415, "unsupported_type", f"不支持的文件类型: .{file_type or '?'}")
    if not data:
        raise DocumentError(400, "empty_file", "文件为空")
    digest = hashlib.sha256(data).hexdigest()
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            doc = await _insert(
                conn,
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_type="file",
                title=_title_from_name(file_name),
                content_hash=digest,
                folder_path=folder_path,
                overrides=overrides,
                file_name=file_name,
                file_type=file_type,
                file_size=len(data),
            )
            storage.put_bytes(doc["storage_key"], data)
            await jobs.enqueue(conn, jobs.KIND_INGEST, document_id=doc["id"], payload={"attempt": 1})
    return doc


async def create_text_document(
    tenant_id: str, kb_id: str, title: str, text: str, *, folder_path: str = "", overrides: dict | None = None
) -> dict:
    if not text.strip():
        raise DocumentError(400, "empty_text", "文本为空")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            doc = await _insert(
                conn,
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_type="text",
                title=title or "文本",
                content_hash=digest,
                folder_path=folder_path,
                overrides=overrides,
                file_name=None,
                file_type="md",
                file_size=len(text.encode("utf-8")),
            )
            storage.put_text(doc["storage_key"], text)
            await jobs.enqueue(conn, jobs.KIND_INGEST, document_id=doc["id"], payload={"attempt": 1})
    return doc


async def create_url_document(
    tenant_id: str, kb_id: str, url: str, *, folder_path: str = "", overrides: dict | None = None
) -> dict:
    try:
        await url_guard.check_public_url(url)
    except url_guard.UrlRejected as exc:
        raise DocumentError(400, exc.code, exc.message) from exc
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            doc = await _insert(
                conn,
                tenant_id=tenant_id,
                kb_id=kb_id,
                source_type="url",
                title=url[:300],
                content_hash=digest,
                folder_path=folder_path,
                overrides=overrides,
                file_type="html",
                source_url=url,
            )
            await jobs.enqueue(conn, jobs.KIND_INGEST, document_id=doc["id"], payload={"attempt": 1})
    return doc


async def reprocess(tenant_id: str, doc_id: str, kb_ids: list[str] | None = None) -> dict:
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE documents SET attempt = attempt + 1, parse_status = 'pending', stage = NULL,"
                " error_code = NULL, error_message = NULL, updated_at = now()"
                " WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
                " AND ($3::text[] IS NULL OR kb_id = ANY($3::text[]))"
                " RETURNING id, kb_id, attempt",
                doc_id,
                tenant_id,
                kb_ids,
            )
            if row is None:
                raise DocumentError(404, "not_found", "文档不存在")
            await jobs.enqueue(conn, jobs.KIND_INGEST, document_id=doc_id, payload={"attempt": row["attempt"]})
    return dict(row)


async def cancel(tenant_id: str, doc_id: str) -> dict:
    row = await db.pool().fetchrow(
        "UPDATE documents SET parse_status = 'cancelled', stage = NULL, updated_at = now()"
        " WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL"
        " AND parse_status IN ('pending', 'processing') RETURNING id, parse_status",
        doc_id,
        tenant_id,
    )
    if row is None:
        raise DocumentError(409, "not_cancellable", "文档不在处理中")
    return dict(row)


async def delete(tenant_id: str, doc_id: str) -> None:
    async with db.pool().acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "UPDATE documents SET parse_status = 'deleting', deleted_at = now(), updated_at = now()"
                " WHERE id = $1 AND tenant_id = $2 AND deleted_at IS NULL RETURNING id",
                doc_id,
                tenant_id,
            )
            if row is None:
                raise DocumentError(404, "not_found", "文档不存在")
            await conn.execute("UPDATE chunks SET is_active = false WHERE document_id = $1", doc_id)
            await jobs.enqueue(conn, jobs.KIND_DELETE, document_id=doc_id)
