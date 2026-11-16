"""Search without the embedding model, exact codes in the top results, no invented summaries, no stuck documents."""

from __future__ import annotations

import asyncio

import pytest

from kb_service import jobs, kb_config, models_client, pipeline, postprocess, questions, retrieval, storage, text_utils
from kb_service.retrieval import Hit


def chunk(cid: str, doc: str, content: str, **scores) -> Hit:
    hit = Hit(
        chunk_id=cid, document_id=doc, kb_id="kb_demo", seq=0, chunk_type="text", content=content,
        context_header="", start_offset=0, end_offset=len(content), line_start=1, anchor_id=None, source_locators=[],
    )
    for name, value in scores.items():
        setattr(hit, name, value)
    return hit


C18 = "电压数值表：3.2 V、2.1 V、1.3 V、4.2 V，额定 3.2.1 档"
C32 = "编号清单：LFP-2048X、v3.2.1-rc4、E-0x1F3A、KX_77_alpha"


@pytest.fixture
def legs(monkeypatch):
    """Database legs replaced by synthetic hits; returns a dict to fill per test."""

    found: dict = {"vector": [], "keyword": [], "exact": [], "vector_called": False}

    async def kb_configs(kb_ids):
        return {k: kb_config.defaults() for k in kb_ids}

    async def no_questions(ids):
        return {}

    async def attach(hits):
        for h in hits:
            h.title = h.document_id
        return hits

    async def vector(tenant_id, kb_ids, vec, limit):
        found["vector_called"] = True
        return [chunk(h.chunk_id, h.document_id, h.content, vector_score=h.vector_score) for h in found["vector"]]

    async def keywords(tenant_id, kb_ids, query, limit):
        return [chunk(h.chunk_id, h.document_id, h.content, keyword_score=h.keyword_score) for h in found["keyword"]]

    async def exact(tenant_id, kb_ids, terms, limit):
        return [chunk(h.chunk_id, h.document_id, h.content) for h in found["exact"]]

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(retrieval, "kb_configs", kb_configs)
    monkeypatch.setattr(questions, "questions_for_chunks", no_questions)
    monkeypatch.setattr(retrieval, "_attach_documents", attach)
    monkeypatch.setattr(retrieval, "_vector", vector)
    monkeypatch.setattr(retrieval, "_keywords", keywords)
    monkeypatch.setattr(retrieval, "_exact", exact)
    monkeypatch.setattr(retrieval, "_kbs_with_questions", empty)
    for name in ("_question_vector", "_question_keywords", "_wiki_vector", "_wiki_keywords"):
        monkeypatch.setattr(retrieval, name, empty)
    return found


def _embedding_fails(monkeypatch):
    async def embed_query(text):
        raise models_client.ModelError("HTTP 401: invalid credentials", retriable=False)

    monkeypatch.setattr(models_client, "embed_query", embed_query)


def test_search_falls_back_to_keywords_when_the_embedding_model_fails(legs, monkeypatch):
    _embedding_fails(monkeypatch)
    legs["keyword"] = [chunk("c1", "doc_a", "泵房巡检记录出口压力", keyword_score=3.0)]
    warnings: list[str] = []
    hits = asyncio.run(retrieval.search("tenant_demo", ["kb_demo"], "出口压力", warnings=warnings))
    assert [h.document_id for h in hits] == ["doc_a"] and not legs["vector_called"]
    assert warnings == [retrieval.VECTOR_DOWN_WARNING]


def test_search_without_any_usable_leg_is_unavailable(legs, monkeypatch):
    _embedding_fails(monkeypatch)
    with pytest.raises(retrieval.SearchUnavailable, match="向量模型"):
        asyncio.run(retrieval.search("tenant_demo", ["kb_demo"], "出口压力", options={"keyword_enabled": False}))


def test_search_endpoint_reports_degraded_and_unavailable_search(api_client, monkeypatch):
    async def degraded(*args, warnings=None, **kwargs):
        warnings.append(retrieval.VECTOR_DOWN_WARNING)
        return []

    monkeypatch.setattr(retrieval, "search", degraded)
    r = api_client.post("/v1/search", json={"query": "出口压力"})
    assert r.status_code == 200 and r.json()["warnings"] == [retrieval.VECTOR_DOWN_WARNING]

    async def unavailable(*args, **kwargs):
        raise retrieval.SearchUnavailable("检索暂不可用：调用向量模型失败")

    monkeypatch.setattr(retrieval, "search", unavailable)
    r = api_client.post("/v1/search", json={"query": "出口压力"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "search_unavailable"


def test_identifier_terms():
    assert postprocess.identifier_terms("v3.2.1-rc4") == ["v3.2.1-rc4"]
    assert postprocess.identifier_terms("LFP-2048X 与 E-0x1F3A、KX_77_alpha 的关系") == ["LFP-2048X", "E-0x1F3A", "KX_77_alpha"]
    assert postprocess.identifier_terms("2026 年电压 3.2 V 是多少") == []


def test_exact_code_outranks_number_heavy_chunks(legs, monkeypatch):
    async def embed_query(text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(models_client, "embed_query", embed_query)
    legs["vector"] = [chunk("c18", "C18", C18, vector_score=0.82), chunk("c32", "C32", C32, vector_score=0.55)]
    legs["keyword"] = [chunk("c18", "C18", C18, keyword_score=9.0), chunk("c32", "C32", C32, keyword_score=2.0)]
    legs["exact"] = [chunk("c32", "C32", C32)]

    with monkeypatch.context() as m:
        m.setattr(postprocess, "identifier_terms", lambda q: [])
        before = asyncio.run(retrieval.search("tenant_demo", ["kb_demo"], "v3.2.1-rc4", top_k=3))
    assert before[0].document_id == "C18"  # the reported ranking without exact matching

    after = asyncio.run(retrieval.search("tenant_demo", ["kb_demo"], "v3.2.1-rc4", top_k=3))
    assert after[0].document_id == "C32" and "exact" in after[0].sources


def test_merge_exact_puts_verbatim_matches_first():
    keyword = [chunk("c18", "C18", C18, keyword_score=1.0), chunk("c32", "C32", C32, keyword_score=0.2)]
    merged = retrieval.merge_exact(keyword, [chunk("c99", "C99", "另一份 v3.2.1-rc4 说明")], ["v3.2.1-rc4"])
    assert [h.chunk_id for h in merged] == ["c32", "c99", "c18"]
    assert merged[0].keyword_score == 1.0 and "exact" in merged[1].sources


def test_summary_is_not_written_for_a_body_without_text():
    scanned = "# \n\n![](images/page-1.png)\n\n![第2页](images/page-2.png)\n\n<!-- image -->\n"
    assert text_utils.text_content(scanned) == (0, 2)
    assert pipeline.summary_without_text(scanned) == pipeline.NO_TEXT_SUMMARY_IMAGES
    assert pipeline.summary_without_text("   \n\n") == pipeline.NO_TEXT_SUMMARY
    assert pipeline.summary_without_text("泵房巡检规程：每班记录出口压力，低于 0.3 MPa 联锁停泵，并通知值班人员。") is None


def test_handle_summary_skips_the_model_for_a_scanned_document(fake_pool, monkeypatch):
    saved: list[str] = []

    def handler(kind, sql, args):
        if sql.startswith("SELECT d.title"):
            return {"title": "扫描件示例", "l1_key": "l1.md", "attempt": 1, "parse_status": "completed",
                    "deleted_at": None, "kb_id": "kb_demo", "file_type": "pdf", "process_overrides": {},
                    "kb_config": {}}
        if sql.startswith("UPDATE documents SET summary = $3"):
            saved.append(args[2])
        return None

    async def model_must_not_run(*args, **kwargs):
        raise AssertionError("no text: the model must not be asked for a summary")

    async def no_job(*args, **kwargs):
        return 1

    fake_pool(handler)
    monkeypatch.setattr(storage, "get_text", lambda key: "![](images/page-1.png)\n")
    monkeypatch.setattr(models_client, "chat", model_must_not_run)
    monkeypatch.setattr(jobs, "enqueue", no_job)
    monkeypatch.setattr(jobs, "enqueue_kb_once", no_job)
    job = jobs.Job(id=1, kind=jobs.KIND_SUMMARY, document_id="doc_demo", payload={"attempt": 1}, attempts=1,
                   max_attempts=3)
    asyncio.run(pipeline.handle_summary(job))
    assert saved == [pipeline.NO_TEXT_SUMMARY_IMAGES]


def test_unexpected_ingest_error_marks_the_document_failed(monkeypatch):
    failed: list[tuple] = []

    async def broken(job, doc_id, attempt):
        raise RuntimeError("synthetic failure")

    async def fail(doc_id, attempt, code, message):
        failed.append((doc_id, attempt, code, message))

    monkeypatch.setattr(pipeline, "_ingest", broken)
    monkeypatch.setattr(pipeline, "_fail", fail)
    job = jobs.Job(id=1, kind=jobs.KIND_INGEST, document_id="doc_demo", payload={"attempt": 2}, attempts=1,
                   max_attempts=3)
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.handle_ingest(job))
    assert failed and failed[0][:3] == ("doc_demo", 2, "internal_error") and "RuntimeError" in failed[0][3]


def test_superseded_ingest_is_not_marked_failed(monkeypatch):
    async def superseded(job, doc_id, attempt):
        raise jobs.Abandon("attempt superseded")

    async def fail(*args):
        raise AssertionError("an abandoned job must not fail the document")

    monkeypatch.setattr(pipeline, "_ingest", superseded)
    monkeypatch.setattr(pipeline, "_fail", fail)
    job = jobs.Job(id=1, kind=jobs.KIND_INGEST, document_id="doc_demo", payload={"attempt": 1}, attempts=1,
                   max_attempts=3)
    with pytest.raises(jobs.Abandon):
        asyncio.run(pipeline.handle_ingest(job))
