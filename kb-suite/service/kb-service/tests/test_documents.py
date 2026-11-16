"""Uploads and renames answer with readable errors instead of 500 (synthetic data, fake database)."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from kb_service import documents, jobs, storage
from kb_service.api import management

LONG_CJK_NAME = "超长文件名测试" * 28 + "LONG-NAME-1203.md"  # 213 characters, mostly 3-byte UTF-8


def _row_from_insert(args: tuple) -> dict:
    keys = ("id", "tenant_id", "kb_id", "source_type", "file_name", "file_type", "file_size", "storage_key",
            "source_url", "content_hash", "folder_path", "title", "process_overrides")
    return {**dict(zip(keys, args)), "parse_status": "pending"}


@pytest.fixture
def stored(monkeypatch, tmp_path):
    async def no_job(*args, **kwargs):
        return 1

    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    monkeypatch.setattr(jobs, "enqueue", no_job)
    return tmp_path


def test_clean_file_name_keeps_only_the_last_segment():
    for raw in ("../../evil.md", "..\\..\\evil.md", "/etc/evil.md", " evil.md\x07 "):
        assert documents.clean_file_name(raw) == "evil.md"
    assert documents.clean_file_name("../..") == "file"
    long_name = documents.clean_file_name("长" * 400 + ".docx")
    assert len(long_name) == documents.MAX_FILE_NAME_CHARS and long_name.endswith(".docx")


def test_storage_name_fits_the_file_system_limit():
    name = documents._safe_name(LONG_CJK_NAME)
    assert len(name.encode("utf-8")) <= documents.MAX_STORED_NAME_BYTES and name.endswith(".md")
    assert documents._safe_name("../../evil.md") == "evil.md"
    assert documents._safe_name("..") == "file"


def test_long_cjk_file_name_uploads(fake_pool, stored):
    fake_pool(lambda kind, sql, args: _row_from_insert(args) if sql.startswith("INSERT INTO documents") else None)
    doc = asyncio.run(documents.create_file_document("tenant_demo", "kb_demo", LONG_CJK_NAME, b"# LONG-NAME-1203\n"))
    assert doc["file_name"] == LONG_CJK_NAME and doc["title"] == LONG_CJK_NAME[:-3]
    assert storage.get_bytes(doc["storage_key"]) == b"# LONG-NAME-1203\n"


def test_path_in_file_name_is_dropped_on_upload(fake_pool, stored):
    fake_pool(lambda kind, sql, args: _row_from_insert(args) if sql.startswith("INSERT INTO documents") else None)
    doc = asyncio.run(documents.create_file_document("tenant_demo", "kb_demo", "../../evil.md", b"synthetic"))
    assert doc["file_name"] == "evil.md" and doc["storage_key"].endswith("/source/evil.md")
    assert (stored / doc["storage_key"]).is_file()


def _duplicate_db(existing_status: str):
    def handler(kind, sql, args):
        if sql.startswith("INSERT INTO documents"):
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        if sql.startswith("SELECT id, parse_status FROM documents"):
            return {"id": "doc_existing", "parse_status": existing_status}
        return None

    return handler


def test_duplicate_content_is_a_409_with_the_existing_document(fake_pool, stored):
    pool = fake_pool(_duplicate_db("completed"))
    with pytest.raises(documents.DocumentError) as info:
        asyncio.run(documents.create_file_document("tenant_demo", "kb_demo", "C11_乙.md", b"same text"))
    error = info.value
    assert (error.status, error.code, error.message) == (409, "duplicate", "知识库里已有相同内容的文档")
    assert error.extra == {"existingDocId": "doc_existing", "existingStatus": "completed"}
    assert not pool.aborted


def test_duplicate_url_mentions_a_failed_earlier_import(fake_pool, monkeypatch):
    async def public(url):
        return None

    monkeypatch.setattr(documents.url_guard, "check_public_url", public)
    fake_pool(_duplicate_db("failed"))
    with pytest.raises(documents.DocumentError) as info:
        asyncio.run(documents.create_url_document("tenant_demo", "kb_demo", "https://example.com/"))
    assert info.value.status == 409 and info.value.message.startswith("知识库里已有相同网址的文档")
    assert "解析失败" in info.value.message and info.value.extra["existingStatus"] == "failed"


def test_upload_endpoint_returns_409_for_duplicate_content(api_client, fake_pool, stored):
    fake_pool(_duplicate_db("completed"))
    r = api_client.post("/v1/kbs/kb_demo/documents", files={"file": ("C11_乙.md", b"same text", "text/markdown")})
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "duplicate", "message": "知识库里已有相同内容的文档",
        "existingDocId": "doc_existing", "existingStatus": "completed",
    }


def test_renaming_to_an_existing_name_is_a_409(api_client, fake_pool):
    def handler(kind, sql, args):
        if sql.startswith("SELECT * FROM knowledge_bases"):
            return {"id": "kb_demo", "config": {}}
        if sql.startswith("UPDATE knowledge_bases"):
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        return None

    fake_pool(handler)
    r = api_client.patch("/v1/kbs/kb_demo", json={"name": "库B"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "duplicate_name" and "已存在同名知识库" in r.json()["error"]["message"]


def test_download_header_and_list_view_carry_only_the_file_name(api_client, monkeypatch, tmp_path):
    (tmp_path / "evil.md").write_bytes(b"synthetic")
    doc = {"id": "doc_demo", "kb_id": "kb_demo", "source_type": "file", "storage_key": "evil.md",
           "file_name": "../../evil.md"}

    async def get_document(tenant_id, doc_id, kb_ids=None):
        return doc

    monkeypatch.setattr(management.retrieval, "get_document", get_document)
    monkeypatch.setattr(storage, "_root", lambda: tmp_path)
    r = api_client.get("/v1/documents/doc_demo/file")
    assert r.status_code == 200 and r.content == b"synthetic"
    disposition = r.headers["content-disposition"]
    assert 'filename="evil.md"' in disposition and ".." not in disposition and "/" not in disposition
    assert management._doc_view(doc)["file_name"] == "evil.md"
