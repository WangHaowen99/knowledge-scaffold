"""Concurrent searches: query embeddings, question legs and the database pool (no database or model needed)."""

from __future__ import annotations

import asyncio
import secrets
from collections import OrderedDict

import pytest

from kb_service import config, db, kb_config, models_client, questions, retrieval


def _settings(**overrides) -> config.Settings:
    return config.Settings(llm_api_key=secrets.token_hex(8), llm_base_url="https://model.example.com/v1", **overrides)


@pytest.fixture
def model_calls(monkeypatch):
    """Fake embedding endpoint that records how many calls run at once."""

    state = {"running": 0, "peak": 0, "calls": 0, "fail": False}

    async def post_json(url, body, *, attempts=4):
        state["calls"] += 1
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.02)
        state["running"] -= 1
        if state["fail"]:
            raise models_client.ModelError("HTTP 503: busy", retriable=True)
        return {"data": [{"index": i, "embedding": [0.1, 0.2]} for i, _ in enumerate(body["input"])]}

    settings = _settings(embed_concurrency=1, query_embed_concurrency=8, embed_batch=1)
    monkeypatch.setattr(models_client, "get_settings", lambda: settings)
    monkeypatch.setattr(models_client, "_post_json", post_json)
    monkeypatch.setattr(models_client, "_embed_sem", None)
    monkeypatch.setattr(models_client, "_query_sem_obj", None)
    monkeypatch.setattr(models_client, "_query_cache", OrderedDict())
    monkeypatch.setattr(models_client, "_query_inflight", {})
    return state


def test_query_embeddings_do_not_queue_behind_the_ingestion_limit(model_calls):
    async def run():
        return await asyncio.gather(*(models_client.embed_query(f"查询 {i}") for i in range(8)))

    vectors = asyncio.run(run())
    assert vectors == [[0.1, 0.2]] * 8
    assert model_calls["peak"] == 8


def test_concurrent_searches_for_the_same_text_share_one_model_call(model_calls):
    async def run():
        return await asyncio.gather(*(models_client.embed_query("针刺试验 表面温度") for _ in range(20)))

    assert asyncio.run(run()) == [[0.1, 0.2]] * 20 and model_calls["calls"] == 1
    asyncio.run(models_client.embed_query("针刺试验 表面温度"))
    assert model_calls["calls"] == 1  # kept for the next search


def test_a_failed_query_embedding_reaches_every_waiting_search_and_is_not_kept(model_calls):
    model_calls["fail"] = True

    async def run():
        return await asyncio.gather(*(models_client.embed_query("出口压力") for _ in range(5)), return_exceptions=True)

    results = asyncio.run(run())
    assert all(isinstance(r, models_client.ModelError) for r in results) and model_calls["calls"] == 1
    model_calls["fail"] = False
    assert asyncio.run(models_client.embed_query("出口压力")) == [0.1, 0.2] and model_calls["calls"] == 2


def test_query_cache_is_bounded(model_calls, monkeypatch):
    monkeypatch.setattr(models_client, "QUERY_CACHE_SIZE", 3)

    async def run():
        for i in range(5):
            await models_client.embed_query(f"查询 {i}")

    asyncio.run(run())
    assert [k[2] for k in models_client._query_cache] == ["查询 2", "查询 3", "查询 4"]


def test_ingestion_embeddings_keep_their_own_limit(model_calls):
    async def run():
        return await asyncio.gather(*(models_client.embed([f"段落 {i}"]) for i in range(4)))

    asyncio.run(run())
    assert model_calls["peak"] == 1


@pytest.fixture
def search_legs(monkeypatch):
    calls: dict = {"question_kbs": [], "questions_for_chunks": 0}

    async def kb_configs(kb_ids):
        return {k: kb_config.defaults() for k in kb_ids}

    async def embed_query(text):
        return [0.1, 0.2]

    async def empty(*args, **kwargs):
        return []

    async def question_leg(tenant_id, kb_ids, *args):
        calls["question_kbs"].append(list(kb_ids))
        return []

    async def lookup(ids):
        calls["questions_for_chunks"] += 1
        return {}

    monkeypatch.setattr(retrieval, "kb_configs", kb_configs)
    monkeypatch.setattr(models_client, "embed_query", embed_query)
    for name in ("_vector", "_keywords", "_wiki_vector", "_wiki_keywords", "_exact"):
        monkeypatch.setattr(retrieval, name, empty)
    monkeypatch.setattr(retrieval, "_question_vector", question_leg)
    monkeypatch.setattr(retrieval, "_question_keywords", question_leg)
    monkeypatch.setattr(questions, "questions_for_chunks", lookup)
    monkeypatch.setattr(retrieval, "_attach_documents", empty)
    return calls


def test_question_legs_are_skipped_for_knowledge_bases_without_questions(search_legs, monkeypatch):
    async def none_have_questions(kb_ids):
        return []

    monkeypatch.setattr(retrieval, "_kbs_with_questions", none_have_questions)
    trace: dict = {}
    asyncio.run(retrieval.search("tenant_demo", ["kb_a", "kb_b"], "针刺试验", trace=trace))
    assert search_legs["question_kbs"] == [] and search_legs["questions_for_chunks"] == 0
    assert set(trace["timings_ms"]) == {"embed", "legs", "rerank", "context"}


def test_question_legs_search_only_knowledge_bases_with_questions(search_legs, monkeypatch):
    async def some_have_questions(kb_ids):
        return ["kb_b"]

    monkeypatch.setattr(retrieval, "_kbs_with_questions", some_have_questions)
    asyncio.run(retrieval.search("tenant_demo", ["kb_a", "kb_b"], "针刺试验"))
    assert search_legs["question_kbs"] == [["kb_b"], ["kb_b"]]


def test_kbs_with_questions_reads_document_counts(fake_pool):
    seen = []

    def handler(kind, sql, args):
        seen.append(sql)
        return [{"kb_id": "kb_b"}]

    fake_pool(handler)
    assert asyncio.run(retrieval._kbs_with_questions(["kb_a", "kb_b"])) == ["kb_b"]
    assert "question_count > 0" in seen[0] and "deleted_at IS NULL" in seen[0]


def test_api_pool_is_opened_warm_without_parallel_workers(monkeypatch):
    created = []

    async def create_pool(dsn, **kwargs):
        created.append(kwargs)
        return object()

    monkeypatch.setattr(db, "get_settings", lambda: _settings(db_pool_size=20, db_parallel_workers=0))
    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    for kwargs in ({}, {"max_size": 10}):
        monkeypatch.setattr(db, "_pool", None)
        asyncio.run(db.connect("postgresql://db.example.com/kbs", **kwargs))
    api, worker = created
    assert (api["min_size"], api["max_size"]) == (20, 20)
    assert (worker["min_size"], worker["max_size"]) == (1, 10)
    for kwargs in created:
        assert kwargs["server_settings"] == {"max_parallel_workers_per_gather": "0"}
        assert kwargs["max_inactive_connection_lifetime"] == 0
