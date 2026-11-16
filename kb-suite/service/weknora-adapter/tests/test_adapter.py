from __future__ import annotations

import asyncio
import json
import re
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from weknora_adapter import client as client_mod
from weknora_adapter.app import create_app
from weknora_adapter.client import WeKnora
from weknora_adapter.mapping import stitch

TEXT = "第一节 循环泵巡检。\n\n第二节 出口压力低于 0.3 MPa 联锁停机。\n\n第三节 每班记录。"


def _chunks():
    # three chunks of TEXT with a 4-character overlap, offsets in characters (synthetic)
    spans = [(0, 20), (16, 44), (40, len(TEXT))]
    return [{"id": f"c{i}", "chunk_index": i, "content": TEXT[s:e], "start_at": s, "end_at": e,
             "source_locators": [{"type": "pdf", "page": i + 1}]} for i, (s, e) in enumerate(spans)]


def ok(data, **extra) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data, **extra})


class FakeWeKnora:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.failed = 1
        self.wiki_pages: list[dict] = []
        self.kb = {"id": "kb1", "name": "测试库", "description": "", "type": "document", "knowledge_count": 2,
                   "processing_count": 1, "chunk_count": 3, "chunking_config": {"chunk_size": 500, "chunk_overlap": 50},
                   "indexing_strategy": {"vector_enabled": True, "wiki_enabled": True}}
        self.doc = {"id": "k1", "knowledge_base_id": "kb1", "type": "file", "title": "泵手册", "file_name": "pump.pdf",
                    "file_type": "PDF", "parse_status": "finalizing", "description": "泵的巡检要求"}
        self.docs = [self.doc]
        self.hits = [{"id": "c1", "knowledge_id": "k1", "knowledge_base_id": "kb1", "chunk_index": 1,
                      "knowledge_title": "泵手册", "content": "出口压力低于 0.3 MPa", "score": 0.7,
                      "matched_content": "出口压力低于 0.3 MPa", "source_locators": [{"page": 2, "type": "pdf"}]}]
        # chunk-list totals of documents other than k1
        self.chunk_totals: dict[str, int] = {}
        # full chunk lists of documents other than k1
        self.extra_chunks: dict[str, list[dict]] = {}
        self.url_answer: httpx.Response | None = None
        self.download_disposition = 'attachment; filename="pump.pdf"'

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") and request.content else {}
        self.calls.append((request.method, path, {"body": body, "params": dict(request.url.params), "raw": request.content,
                                                  "raw_path": request.url.raw_path}))
        params = request.url.params
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/knowledge-bases" and request.method == "GET":
            return ok([self.kb, {"id": "faq1", "type": "faq"}])
        if path == "/knowledge-bases" and request.method == "POST":
            return httpx.Response(201, json={"success": True, "data": {**self.kb, "id": "kb2", "name": body["name"]}})
        m = re.match(r"^/knowledge-bases/([^/]+)$", path)
        if m:
            if request.method == "PUT":
                return ok(self.kb)
            return ok({**self.kb, "id": m.group(1)})
        if re.match(r"^/knowledge-bases/[^/]+/knowledge$", path):
            status = params.get("parse_status")
            if status == "failed":
                return ok([], total=self.failed, page=1, page_size=1)
            docs = [d for d in self.docs if not status or d["parse_status"] == status]
            page, size = int(params.get("page", 1)), int(params.get("page_size", 20))
            return ok(docs[(page - 1) * size: page * size], total=len(docs), page=page, page_size=size)
        if path.endswith("/knowledge/file"):
            # WeKnora titles a file after the fileName field, without its folder
            found = re.search(rb'name="fileName"\r\n\r\n(.*?)\r\n', request.content)
            name = found.group(1).decode().rsplit("/", 1)[-1] if found else self.doc["file_name"]
            return ok({**self.doc, "id": "k9", "parse_status": "pending", "title": name, "file_name": name,
                       "file_type": name.rsplit(".", 1)[-1]})
        if path.endswith("/knowledge/url"):
            return self.url_answer or ok({**self.doc, "id": "k7", "type": "url", "source": body["url"]})
        if path.endswith("/knowledge/manual"):
            return ok({**self.doc, "id": "k8", "type": "manual", "title": body["title"], "file_type": ""})
        if path == "/knowledge/k1":
            return ok(self.doc)
        if path == "/knowledge/missing":
            return httpx.Response(404, json={"success": False, "error": {"code": 1003, "message": "not found"}})
        if path == "/chunks/k1":
            page, size = int(request.url.params["page"]), int(request.url.params["page_size"])
            chunks = _chunks()
            return ok(chunks[(page - 1) * size: page * size], total=len(chunks), page=page, page_size=size)
        m = re.match(r"^/chunks/([^/]+)$", path)
        if m and m.group(1) in self.extra_chunks:
            chunks = self.extra_chunks[m.group(1)]
            page, size = int(params.get("page", 1)), int(params.get("page_size", 20))
            return ok(chunks[(page - 1) * size: page * size], total=len(chunks), page=page, page_size=size)
        if m and m.group(1) in self.chunk_totals:
            return ok([], total=self.chunk_totals[m.group(1)], page=1, page_size=1)
        if path == "/knowledge-search":
            return ok(self.hits)
        if path == "/wiki-search":
            return ok([{"id": "w1", "knowledge_base_id": "kb1", "slug": "entity/pump", "title": "循环泵",
                        "page_type": "entity", "summary": "泵"}])
        if path == "/knowledgebase/kb1/wiki/pages/entity/pump":
            return httpx.Response(200, json={"id": "w1", "slug": "entity/pump", "title": "循环泵", "page_type": "entity",
                                             "content": "正文", "out_links": ["concept/pressure"], "in_links": ["index"],
                                             "in_link_titles": {"index": "索引"}, "source_refs": ["k1|泵手册"]})
        if path.startswith("/knowledgebase/kb2/wiki/"):
            return httpx.Response(400, json={"error": "Wiki feature is not enabled"})
        if path == "/knowledgebase/kb1/wiki/pages":
            pages = [w for w in self.wiki_pages if not params.get("page_type") or w["page_type"] == params["page_type"]]
            page, size = int(params.get("page", 1)), int(params.get("page_size", 20))
            return httpx.Response(200, json={"pages": pages[(page - 1) * size: page * size], "total": len(pages)})
        if path == "/knowledge/k1/download":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf",
                                                                 "content-disposition": self.download_disposition})
        return httpx.Response(404, json={"success": False, "error": {"code": 1003, "message": path}})


@pytest.fixture
def fake():
    return FakeWeKnora()


@pytest.fixture
def client(fake, monkeypatch):
    monkeypatch.setenv("WEKNORA_ADAPTER_KEY", "adapter-key")
    monkeypatch.setenv("WEKNORA_EMBEDDING_MODEL_ID", "emb")
    monkeypatch.setenv("WEKNORA_CHAT_MODEL_ID", "chat")
    client_mod.get_settings.cache_clear()
    weknora = WeKnora(httpx.AsyncClient(base_url="https://wk.example.com/api/v1", transport=httpx.MockTransport(fake)))
    with TestClient(create_app(weknora), headers={"Authorization": "Bearer adapter-key"}) as c:
        yield c
    client_mod.get_settings.cache_clear()


def test_key_is_required(client):
    assert client.get("/v1/capabilities", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_capabilities_declare_contract_and_schema(client):
    caps = client.get("/v1/capabilities").json()
    assert caps["contract"] == "knowledge-provider/v1" and caps["role"] == "base"
    assert "wiki.read" in caps["features"] and "tools.da" not in caps["features"]
    assert set(caps["config_schema"]["properties"]) == {"chunk_size", "chunk_overlap", "wiki_enabled"}


def test_kbs_are_prefixed_and_faq_bases_hidden(client):
    items = client.get("/v1/kbs").json()["items"]
    assert [kb["id"] for kb in items] == ["wk_kb1"]
    # documents is the document list's total (1 here), not WeKnora's knowledge_count (2)
    assert items[0]["stats"] == {"documents": 1, "completed": 0, "processing": 1, "failed": 1, "cancelled": 0, "chunks": 3}
    assert items[0]["config"] == {"chunk_size": 500, "chunk_overlap": 50, "wiki_enabled": True}


def test_unprefixed_ids_are_not_found(client):
    assert client.get("/v1/kbs/kb1").status_code == 404
    assert client.get("/v1/documents/wk_missing").json()["error"]["code"] == "1003"


def test_create_kb_uses_configured_models(client, fake):
    kb = client.post("/v1/kbs", json={"name": "新库", "config": {"chunk_size": 800, "wiki_enabled": True}}).json()
    assert kb["id"] == "wk_kb2"
    sent = next(c for c in fake.calls if c[:2] == ("POST", "/knowledge-bases"))[2]["body"]
    assert sent["embedding_model_id"] == "emb" and sent["wiki_config"] == {"synthesis_model_id": "chat"}
    assert sent["chunking_config"]["chunk_size"] == 800 and sent["indexing_strategy"]["wiki_enabled"] is True


def test_documents_map_status_and_summary(client):
    docs = client.get("/v1/kbs/wk_kb1/documents").json()
    doc = docs["items"][0]
    assert doc["id"] == "wk_k1" and doc["kb_id"] == "wk_kb1" and doc["file_type"] == "pdf"
    # finalizing is already searchable
    assert doc["parse_status"] == "completed" and doc["summary"] == "泵的巡检要求"


def test_text_documents_are_published(client, fake):
    client.post("/v1/kbs/wk_kb1/documents/text", json={"title": "", "text": "标题行\n正文"})
    sent = fake.calls[-1][2]["body"]
    assert sent["status"] == "publish" and sent["title"] == "标题行"


def test_upload_keeps_folder_in_file_name(client, fake):
    doc = client.post("/v1/kbs/wk_kb1/documents", files={"file": ("a.md", b"# x")}, data={"folder_path": "手册/泵"}).json()
    assert doc["id"] == "wk_k9"
    raw = fake.calls[-1][2]["raw"]
    assert "手册/泵/a.md".encode() in raw and b'name="channel"' in raw


def test_chunks_window_spans_pages(client, monkeypatch):
    from weknora_adapter import app as app_mod

    monkeypatch.setattr(app_mod, "CHUNK_PAGE", 2)
    page = client.get("/v1/documents/wk_k1/chunks?offset=1&limit=2").json()
    assert [c["seq"] for c in page["items"]] == [1, 2] and page["total"] == 3
    assert page["items"][0]["id"] == "wk_c1" and page["items"][0]["source_locators"] == [{"page": 2}]


def test_l1_is_stitched_without_overlap(client):
    assert client.get("/v1/documents/wk_k1/content?level=L1").text == TEXT
    assert client.get("/v1/documents/wk_k1/content?level=L0").text == "泵的巡检要求"


def test_stitch_falls_back_to_blank_lines():
    chunks = [{"chunk_index": 0, "content": "甲"}, {"chunk_index": 1, "content": "乙", "start_at": 5, "end_at": 9}]
    assert stitch(chunks) == "甲\n\n乙"


def test_search_maps_hits_and_adds_wiki(client, fake):
    data = client.post("/v1/search", json={"query": "压力", "kb_ids": ["wk_kb1"], "top_k": 5}).json()
    hit, wiki = data["items"]
    assert hit["doc_id"] == "wk_k1" and hit["kb_id"] == "wk_kb1" and hit["locators"] == [{"page": 2}]
    assert wiki["chunk_type"] == "wiki_page" and wiki["wiki_slug"] == "entity/pump"
    sent = next(c for c in fake.calls if c[1] == "/knowledge-search")[2]["body"]
    assert sent["knowledge_base_ids"] == ["kb1"]


def test_wiki_page_and_disabled_wiki(client):
    page = client.get("/v1/kbs/wk_kb1/wiki/page", params={"slug": "entity/pump"}).json()
    assert page["sources"] == [{"id": "wk_k1", "title": "泵手册"}] and page["linked_from"][0]["title"] == "索引"
    assert client.get("/v1/kbs/wk_kb2/wiki/status").json()["enabled"] is False


def test_file_download_streams(client):
    r = client.get("/v1/documents/wk_k1/file")
    assert r.content == b"%PDF" and "pump.pdf" in r.headers["content-disposition"]


def test_structure_reads_the_stitched_text(client):
    win = client.get("/v1/documents/wk_k1/structure", params={"limit": 15}).json()
    assert win["total_chars"] == len(TEXT) and win["has_more"] and win["anchors"][0]["id"].startswith("wk_k1:")
    assert client.get("/v1/documents/wk_k1/structure", params={"heading": "没有"}).status_code == 404


def test_grep_over_documents(client):
    data = client.post("/v1/grep", json={"pattern": "出口压力", "kb_ids": ["wk_kb1"]}).json()
    assert data["total_documents"] == 1 and data["matches"][0]["doc_id"] == "wk_k1"
    assert data["matches"][0]["lines"][0]["line_number"] == 3
    assert client.post("/v1/grep", json={"pattern": ""}).status_code == 400


def test_chunked_upload_ingests_through_weknora(client, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("WEKNORA_UPLOAD_DIR", str(tmp_path))
    client_mod.get_settings.cache_clear()
    begin = client.post("/v1/uploads", json={"kb_id": "wk_kb1", "file_name": "a.md", "file_size": 3}).json()
    assert begin["upload_id"].startswith("wk_")
    client.put(f"/v1/uploads/{begin['upload_id']}/parts/0", content=b"# x")
    doc = client.post(f"/v1/uploads/{begin['upload_id']}/complete")
    assert doc.status_code == 201 and doc.json()["id"] == "wk_k9"
    assert fake.calls[-1][1] == "/knowledge-bases/kb1/knowledge/file" and b"# x" in fake.calls[-1][2]["raw"]
    bad = client.post("/v1/uploads", json={"kb_id": "wk_kb1", "file_name": "a.md", "file_size": 3, "sha256": "0" * 64}).json()
    client.put(f"/v1/uploads/{bad['upload_id']}/parts/0", content=b"# x")
    assert client.post(f"/v1/uploads/{bad['upload_id']}/complete").json()["error"]["code"] == "checksum_mismatch"


# ---- ids and names never steer the upstream URL ------------------------------


def test_wiki_slug_cannot_leave_the_knowledge_base(client, fake):
    r = client.get("/v1/kbs/wk_kb1/wiki/page", params={"slug": "../../../kbB/wiki/pages/secret"})
    assert r.status_code == 404 and not any("kbB" in c[1] for c in fake.calls)
    client.get("/v1/kbs/wk_kb1/wiki/page", params={"slug": "entity/a?b#c"})
    assert fake.calls[-1][2]["raw_path"] == b"/api/v1/knowledgebase/kb1/wiki/pages/entity/a%3Fb%23c"


def test_ids_that_were_never_issued_do_not_reach_weknora(client, fake):
    for path in ("/v1/kbs/wk_..", "/v1/documents/wk_a%3Fb", "/v1/documents/wk_%2E%2E/chunks", "/v1/kbs/wk_a%2Cb"):
        assert client.get(path).status_code == 404, path
    assert fake.calls == []


# ---- paging and filters ----------------------------------------------------------


def _docs(n: int, status: str = "completed") -> list[dict]:
    return [{"id": f"k{i}", "knowledge_base_id": "kb1", "parse_status": status, "created_at": f"2026-01-01T00:{99 - i:02d}"}
            for i in range(n)]


def test_document_window_with_an_unaligned_offset(client, fake):
    fake.docs = _docs(100)
    page = client.get("/v1/kbs/wk_kb1/documents", params={"offset": 30, "limit": 20}).json()
    assert [d["id"] for d in page["items"]] == [f"wk_k{i}" for i in range(30, 50)] and page["total"] == 100


def test_status_filter_covers_every_weknora_status_behind_it(client, fake):
    fake.docs = [{**d, "parse_status": "finalizing" if i % 2 else "completed"} for i, d in enumerate(_docs(6))]
    page = client.get("/v1/kbs/wk_kb1/documents", params={"status": "completed", "offset": 1, "limit": 4}).json()
    assert [d["id"] for d in page["items"]] == ["wk_k1", "wk_k2", "wk_k3", "wk_k4"] and page["total"] == 6
    sent = {c[2]["params"].get("parse_status") for c in fake.calls if c[1] == "/knowledge-bases/kb1/knowledge"}
    assert sent == {"completed", "finalizing"}
    assert client.get("/v1/kbs/wk_kb1/documents", params={"status": "nonsense"}).json() == {"items": [], "total": 0}


def test_no_non_contract_status_and_cancelled_is_not_completed(client, fake):
    fake.docs = [{**fake.doc, "parse_status": "deleting"}, {**fake.doc, "id": "k2", "parse_status": "cancelled"}]
    fake.kb = {**fake.kb, "knowledge_count": 5}
    assert [d["parse_status"] for d in client.get("/v1/kbs/wk_kb1/documents").json()["items"]] == ["processing", "cancelled"]
    stats = client.get("/v1/kbs/wk_kb1").json()["stats"]
    assert stats["cancelled"] == 1 and stats["documents"] == 2 and stats["completed"] == 0


def test_document_total_follows_the_list_not_the_lagging_count(client, fake):
    # WeKnora deletes in the background and its knowledge_count stays stale for minutes
    fake.docs = [{**fake.doc, "parse_status": "completed"}, {**fake.doc, "id": "k2", "parse_status": "completed"},
                 {**fake.doc, "id": "k3", "parse_status": "completed"}]
    fake.kb = {**fake.kb, "knowledge_count": 7, "processing_count": 0}
    fake.failed = 0
    stats = client.get("/v1/kbs/wk_kb1").json()["stats"]
    assert stats["documents"] == 3 and stats["completed"] == 3


def test_wiki_pages_concept_filter_and_window(client, fake):
    fake.wiki_pages = [{"id": f"w{i}", "slug": f"p{i}", "title": f"页{i}", "page_type": t}
                       for i, t in enumerate(["concept", "synthesis", "entity", "comparison", "concept"])]
    concept = client.get("/v1/kbs/wk_kb1/wiki/pages", params={"type": "concept"}).json()
    assert sorted(p["slug"] for p in concept["items"]) == ["p0", "p1", "p3", "p4"] and concept["total"] == 4
    assert {p["page_type"] for p in concept["items"]} == {"concept"}
    assert all(p["id"].startswith("wk_") for p in concept["items"])
    page = client.get("/v1/kbs/wk_kb1/wiki/pages", params={"offset": 3, "limit": 2}).json()
    assert [p["slug"] for p in page["items"]] == ["p3", "p4"]


def test_wiki_pages_and_graph_are_empty_when_wiki_is_off(client):
    assert client.get("/v1/kbs/wk_kb2/wiki/pages").json() == {"items": [], "total": 0}
    assert client.get("/v1/kbs/wk_kb2/wiki/graph").json() == {"nodes": [], "edges": []}


# ---- search ------------------------------------------------------------------------


def test_min_score_does_not_filter_on_the_weknora_score(client):
    data = client.post("/v1/search", json={"query": "压力", "kb_ids": ["wk_kb1"], "min_score": 0.99,
                                           "include_wiki": False}).json()
    assert len(data["items"]) == 1 and data["items"][0]["relevance"] is None


def test_matched_content_is_not_reported_as_a_question(client):
    hit = client.post("/v1/search", json={"query": "压力", "kb_ids": ["wk_kb1"], "include_wiki": False}).json()["items"][0]
    assert "matched_question" not in hit


def test_search_is_capped_and_ordered_with_wiki_hits(client, fake):
    fake.hits = [{**fake.hits[0], "id": f"c{i}", "score": s} for i, s in enumerate([0.2, 0.9, 0.5])]
    plain = client.post("/v1/search", json={"query": "压力", "kb_ids": ["wk_kb1"], "top_k": 2, "include_wiki": False}).json()
    assert [h["chunk_id"] for h in plain["items"]] == ["wk_c1", "wk_c2"]
    mixed = client.post("/v1/search", json={"query": "压力", "kb_ids": ["wk_kb1"], "top_k": 2}).json()["items"]
    scores = [h["score"] for h in mixed]
    assert len(mixed) == 2 and scores == sorted(scores, reverse=True)
    assert mixed[0]["chunk_id"] == "wk_c1" and mixed[0]["weknora_score"] == 0.9


def test_l2_is_unsupported(client):
    r = client.get("/v1/documents/wk_k1/content", params={"level": "L2"})
    assert r.status_code == 501 and r.json()["error"]["code"] == "unsupported"


# ---- knowledge-base config ---------------------------------------------------------


def test_cleared_numbers_mean_not_set(client, fake):
    assert client.post("/v1/kbs", json={"name": "新库", "config": {"chunk_size": None, "chunk_overlap": None}}).status_code == 201
    sent = next(c for c in fake.calls if c[:2] == ("POST", "/knowledge-bases"))[2]["body"]["chunking_config"]
    assert (sent["chunk_size"], sent["chunk_overlap"]) == (1000, 200)
    assert client.patch("/v1/kbs/wk_kb1", json={"config": {"chunk_overlap": None, "wiki_enabled": None}}).status_code == 200
    put = next(c for c in fake.calls if c[:2] == ("PUT", "/knowledge-bases/kb1"))[2]["body"]["config"]
    assert put["chunking_config"]["chunk_overlap"] == 50 and put["indexing_strategy"]["wiki_enabled"] is True


def test_bad_numbers_are_a_400(client):
    for config in ({"chunk_size": "abc"}, {"chunk_size": 50}, {"chunk_overlap": 2.5}, {"chunk_size": True}):
        r = client.post("/v1/kbs", json={"name": "新库", "config": config})
        assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request" and "整数" in r.json()["error"]["message"]
    r = client.patch("/v1/kbs/wk_kb1", json={"config": {"chunk_overlap": 600}})
    assert r.status_code == 400 and "分块重叠" in r.json()["error"]["message"]


def test_a_single_page_of_a_disabled_wiki_is_not_found(client):
    r = client.get("/v1/kbs/wk_kb2/wiki/page", params={"slug": "entity/pump"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ---- knowledge-base chunk statistics ---------------------------------------------------


def _chunk_calls(fake: FakeWeKnora) -> list[str]:
    return sorted(c[1] for c in fake.calls if c[1].startswith("/chunks/"))


def test_chunk_stats_add_up_the_documents_when_weknora_counts_none(client, fake):
    # WeKnora fills chunk_count for FAQ knowledge bases only
    fake.kb = {**fake.kb, "chunk_count": 0, "knowledge_count": 3, "processing_count": 1}
    fake.docs = [{**fake.doc, "updated_at": "2026-01-01T00:00:00Z"},
                 {**fake.doc, "id": "k2", "parse_status": "completed", "updated_at": "2026-01-01T00:00:00Z"},
                 {**fake.doc, "id": "k3", "parse_status": "pending"}]
    fake.chunk_totals = {"k2": 2, "k3": 99}
    assert client.get("/v1/kbs/wk_kb1").json()["stats"]["chunks"] == 3 + 2
    assert client.get("/v1/kbs").json()["items"][0]["stats"]["chunks"] == 3 + 2
    # computed once within its TTL; a pending document has no chunks to ask about
    assert _chunk_calls(fake) == ["/chunks/k1", "/chunks/k2"]
    # a new document refreshes the total at once, asking only about that document
    fake.docs.append({**fake.doc, "id": "k4", "parse_status": "completed", "updated_at": "2026-01-02T00:00:00Z"})
    fake.chunk_totals["k4"] = 4
    fake.kb = {**fake.kb, "knowledge_count": 4}
    assert client.get("/v1/kbs").json()["items"][0]["stats"]["chunks"] == 3 + 2 + 4
    assert _chunk_calls(fake) == ["/chunks/k1", "/chunks/k2", "/chunks/k4"]


def test_chunk_stats_read_every_page_of_documents(client, fake, monkeypatch):
    from weknora_adapter import app as app_mod

    monkeypatch.setattr(app_mod, "DOC_PAGE", 2)
    fake.kb = {**fake.kb, "chunk_count": 0, "knowledge_count": 6}
    fake.docs = _docs(6)
    # k1 has the three chunks of _chunks(); k5 is gone by the time it is counted
    fake.chunk_totals = {"k0": 1, "k2": 3, "k3": 4, "k4": 5}
    assert client.get("/v1/kbs/wk_kb1").json()["stats"]["chunks"] == 1 + 3 + 3 + 4 + 5


def test_listing_does_not_wait_for_a_slow_chunk_count(fake, monkeypatch):
    from weknora_adapter import app as app_mod

    monkeypatch.setenv("WEKNORA_ADAPTER_KEY", "adapter-key")
    client_mod.get_settings.cache_clear()
    monkeypatch.setattr(app_mod, "CHUNK_WAIT_S", 0.05)
    fake.kb = {**fake.kb, "chunk_count": 0, "knowledge_count": 16}
    fake.docs = _docs(16)
    fake.chunk_totals = {f"k{i}": 1 for i in range(16) if i != 1}

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/chunks/"):
            await asyncio.sleep(0.3)
        return fake(request)

    weknora = WeKnora(httpx.AsyncClient(base_url="https://wk.example.com/api/v1", transport=httpx.MockTransport(slow)))
    with TestClient(create_app(weknora), headers={"Authorization": "Bearer adapter-key"}) as c:
        # 16 documents, 8 at a time: the total takes 0.6 s, the listing answers without it
        first = c.get("/v1/kbs").json()["items"][0]["stats"]
        assert "chunks" not in first and first["documents"] == 16
        deadline = time.monotonic() + 10
        stats = first
        while "chunks" not in stats and time.monotonic() < deadline:
            time.sleep(0.05)
            stats = c.get("/v1/kbs").json()["items"][0]["stats"]
        assert stats["chunks"] == 15 + 3
    client_mod.get_settings.cache_clear()


# ---- uploads: encodings, JSON, long names ---------------------------------------------


def _file_part(raw: bytes) -> tuple[str, bytes]:
    """(file name, content) of the multipart ``file`` field sent to WeKnora."""

    found = re.search(rb'name="file"; filename="([^"]*)"\r\n(?:[^\r\n]+\r\n)*\r\n(.*?)\r\n--', raw, re.S)
    return found.group(1).decode(), found.group(2)


def _form_field(raw: bytes, name: str) -> str:
    return re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n', raw).group(1).decode()


def test_text_files_reach_weknora_as_utf8(client, fake):
    text = "编码测试，标记 SAMPLE-MARK-0017。\n生僻字：𠀀𪚥\n"
    table = "日期,指标\n2026-01-01,样例\n"
    for name, data, expected in (
        ("a.txt", text.encode("utf-16-le"), text),  # no byte order mark
        ("b.txt", text.encode("utf-16"), text),
        ("c.md", text.encode("gb18030"), text),
        ("d.csv", table.encode("gbk"), table),
    ):
        assert client.post("/v1/kbs/wk_kb1/documents", files={"file": (name, data)}).status_code == 201, name
        assert _file_part(fake.calls[-1][2]["raw"])[1] == expected.encode("utf-8"), name
    # UTF-8, byte order mark included, goes through byte for byte
    bom = ("\ufeff" + table).encode("utf-8")
    client.post("/v1/kbs/wk_kb1/documents", files={"file": ("e.csv", bom)})
    assert _file_part(fake.calls[-1][2]["raw"])[1] == bom


def test_text_of_unknown_encoding_is_refused_not_garbled(client, fake):
    r = client.post("/v1/kbs/wk_kb1/documents", files={"file": ("x.txt", bytes(range(256)) * 4)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_encoding" and "UTF-8" in r.json()["error"]["message"]
    assert not any(c[1].endswith("/knowledge/file") for c in fake.calls)
    # other types are WeKnora's to read
    assert client.post("/v1/kbs/wk_kb1/documents", files={"file": ("x.pdf", bytes(range(256)))}).status_code == 201


def test_json_reaches_weknora_as_text_and_shows_as_json(client, fake):
    valid = '[{"编号": "SAMPLE-JSON-0001"}, {"编号": "SAMPLE-JSON-0002"}]'.encode()
    broken = b'{"note": "broken", "mark": "SAMPLE-JSON-0003", '
    for name, data in (("list.json", valid), ("broken.json", broken)):
        doc = client.post("/v1/kbs/wk_kb1/documents", files={"file": (name, data)}, data={"folder_path": "数据"}).json()
        sent = fake.calls[-1][2]["raw"]
        # the original bytes, under a name WeKnora reads as plain text
        assert _file_part(sent) == (name + ".as-text.txt", data)
        assert _form_field(sent, "fileName") == f"数据/{name}.as-text.txt"
        assert (doc["title"], doc["file_name"], doc["file_type"]) == (name, name, "json")
    client.post("/v1/kbs/wk_kb1/documents", files={"file": ("notes.txt", b"plain")})
    assert _file_part(fake.calls[-1][2]["raw"])[0] == "notes.txt"

    fake.hits = [{**fake.hits[0], "knowledge_title": "list.JSON.as-text.txt", "knowledge_filename": "list.JSON.as-text.txt"}]
    hit = client.post("/v1/search", json={"query": "SAMPLE", "kb_ids": ["wk_kb1"], "include_wiki": False}).json()["items"][0]
    assert (hit["title"], hit["file_name"]) == ("list.JSON", "list.JSON")
    fake.download_disposition = "attachment; filename*=UTF-8''%E6%95%B0%E6%8D%AE.json.as-text.txt"
    assert client.get("/v1/documents/wk_k1/file").headers["content-disposition"] == \
        "attachment; filename*=UTF-8''%E6%95%B0%E6%8D%AE.json"


def test_users_own_json_txt_files_keep_their_name(client, fake):
    doc = client.post("/v1/kbs/wk_kb1/documents", files={"file": ("导出.json.txt", b'{"a": 1}')}).json()
    assert _file_part(fake.calls[-1][2]["raw"])[0] == "导出.json.txt"
    assert (doc["title"], doc["file_name"], doc["file_type"]) == ("导出.json.txt", "导出.json.txt", "txt")
    fake.hits = [{**fake.hits[0], "knowledge_title": "导出.json.txt", "knowledge_filename": "导出.json.txt"}]
    hit = client.post("/v1/search", json={"query": "a", "kb_ids": ["wk_kb1"], "include_wiki": False}).json()["items"][0]
    assert hit["title"] == "导出.json.txt"
    fake.download_disposition = 'attachment; filename="export.json.txt"'
    assert client.get("/v1/documents/wk_k1/file").headers["content-disposition"] == 'attachment; filename="export.json.txt"'


def test_json_can_stay_with_weknoras_json_reader(client, fake, monkeypatch):
    monkeypatch.setenv("WEKNORA_JSON_AS_TEXT", "false")
    client_mod.get_settings.cache_clear()
    client.post("/v1/kbs/wk_kb1/documents", files={"file": ("a.json", b"{}")})
    assert _file_part(fake.calls[-1][2]["raw"])[0] == "a.json"


def test_long_file_names_fit_weknoras_column(client, fake):
    client.post("/v1/kbs/wk_kb1/documents", files={"file": ("名" * 300 + ".md", b"# x")})
    sent = _form_field(fake.calls[-1][2]["raw"], "fileName")
    assert len(sent) == 255 and sent.endswith("….md") and sent.startswith("名" * 200)
    client.post("/v1/kbs/wk_kb1/documents", files={"file": ("名" * 300 + ".json", b"{}")})
    assert len(_form_field(fake.calls[-1][2]["raw"], "fileName")) == 255


def test_only_the_base_name_is_fitted_under_a_folder(client, fake):
    # WeKnora splits fileName at its last '/': the folder goes to folder_path (varchar(1024),
    # fitted by WeKnora itself), the base name to file_name and title (varchar(255))
    folder = "/".join(["很长的文件夹名" * 10] * 3)
    doc = client.post("/v1/kbs/wk_kb1/documents", files={"file": ("名" * 300 + ".md", b"# x")},
                      data={"folder_path": folder}).json()
    sent_folder, _, sent_name = _form_field(fake.calls[-1][2]["raw"], "fileName").rpartition("/")
    assert sent_folder == folder and len(sent_name) == 255 and sent_name.endswith("….md")
    assert len(doc["title"]) == 255 and doc["title"] == sent_name


def test_long_titles_are_cut_to_weknoras_column(client, fake):
    r = client.post("/v1/kbs/wk_kb1/documents/text", json={"title": "标" * 301, "text": "正文"})
    sent = fake.calls[-1][2]["body"]["title"]
    # stored again as "<title>.md" in file_name, also varchar(255)
    assert r.status_code == 201 and sent == "标" * 251 + "…" and len(sent + ".md") == 255
    assert r.json()["title"] == sent
    client.post("/v1/kbs/wk_kb1/documents/text", json={"title": "题" * 252, "text": "正文"})
    assert fake.calls[-1][2]["body"]["title"] == "题" * 252


# ---- URL import: refused addresses ---------------------------------------------------------

# the shape of WeKnora's SSRF answer (FormatSSRFError), synthetic addresses
SSRF_RESOLVED = ("URL 未通过安全校验：SSRF validation failed: hostname intranet.example.com resolves to restricted IP "
                 "192.0.2.15: private IP。如该地址确实可信，请联系运维在服务端环境变量 SSRF_WHITELIST_EXTRA 中加入该主机，"
                 "示例：SSRF_WHITELIST_EXTRA=intranet.example.com,*.example.com,192.0.2.0/24")
SSRF_DIRECT_IP = ("URL 未通过安全校验：SSRF validation failed: direct IP address access is not allowed, use domain name "
                  "or add to SSRF_WHITELIST。如该地址确实可信，请联系运维在服务端环境变量 SSRF_WHITELIST_EXTRA 中加入该主机")


def _weknora_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"success": False, "error": {"code": 1000, "message": message}})


def test_refused_internal_urls_do_not_reveal_the_network(client, fake):
    for message in (SSRF_RESOLVED, SSRF_DIRECT_IP):
        fake.url_answer = _weknora_error(400, message)
        r = client.post("/v1/kbs/wk_kb1/documents/url", json={"url": "http://127.0.0.1:8080/healthz"})
        assert r.status_code == 400
        assert r.json()["error"] == {"code": "url_not_allowed", "message": "不允许导入内部或私有地址"}
    # WeKnora's own service-level check answers a bare 500
    fake.url_answer = _weknora_error(500, "invalid URL")
    r = client.post("/v1/kbs/wk_kb1/documents/url", json={"url": "https://intranet.example.com/"})
    assert r.status_code == 400 and "不允许导入内部或私有地址" in r.json()["error"]["message"]
    fake.url_answer = _weknora_error(400, "URL 未通过安全校验：SSRF validation failed: DNS resolution failed for hostname "
                                          "nowhere.example.com: cannot verify if it resolves to safe IP")
    r = client.post("/v1/kbs/wk_kb1/documents/url", json={"url": "https://nowhere.example.com/"})
    assert r.status_code == 400 and r.json()["error"]["message"] == "无法解析网址中的域名，请检查网址"


def test_url_import_still_works_and_other_errors_lose_addresses(client, fake):
    doc = client.post("/v1/kbs/wk_kb1/documents/url", json={"url": " https://www.example.com/a "}).json()
    assert doc["id"] == "wk_k7" and fake.calls[-1][2]["body"]["url"] == "https://www.example.com/a"
    fake.url_answer = _weknora_error(503, "upstream 192.0.2.15:8080 unavailable")
    r = client.post("/v1/kbs/wk_kb1/documents/url", json={"url": "https://www.example.com/b"})
    assert r.status_code == 502 and "192.0.2.15" not in r.text  # NOSONAR(S1313) RFC 5737 documentation address


def test_failed_url_documents_hide_the_address_they_tried(client, fake):
    fake.docs = [{**fake.doc, "type": "url", "source": "https://www.example.com/", "parse_status": "failed",
                  "error_message": "2026-01-01 10:20:30 fetch failed: dial tcp 192.0.2.15:443, [2001:db8::15]:443: "
                                   "connection refused"}]
    error = client.get("/v1/kbs/wk_kb1/documents").json()["items"][0]["error_message"]
    assert "192.0.2.15" not in error and "2001:db8" not in error  # NOSONAR(S1313) RFC 5737 / RFC 3849 addresses
    assert error.startswith("2026-01-01 10:20:30 fetch failed") and "connection refused" in error


def test_stitch_drops_the_table_header_repeated_on_continued_chunks():
    head = "| 日期 | 价格 |\n| --- | --- |\n"
    rows1 = "| 2021-01-01 | 1 |\n| 2021-01-02 | 2 |\n"
    rows2 = "| 2021-01-03 | 3 |\n"
    text = head + rows1 + rows2
    a = len(head + rows1)
    chunks = [{"chunk_index": 0, "content": head + rows1, "start_at": 0, "end_at": a},
              {"chunk_index": 1, "content": head + rows2, "start_at": a, "end_at": len(text)}]
    assert stitch(chunks) == text
    # overlapping continuation: the header and the overlap both go
    overlap = [{"chunk_index": 0, "content": head + rows1, "start_at": 0, "end_at": a},
               {"chunk_index": 1, "content": head + "| 2021-01-02 | 2 |\n" + rows2,
                "start_at": a - len("| 2021-01-02 | 2 |\n"), "end_at": len(text)}]
    assert stitch(overlap) == text
    # extra text that is not table rows is kept (joined as before)
    odd = [{"chunk_index": 0, "content": "甲\n", "start_at": 0, "end_at": 2},
           {"chunk_index": 1, "content": "前言\n乙", "start_at": 2, "end_at": 3}]
    assert stitch(odd) == "甲\n\n\n前言\n乙"


def test_search_puts_passages_with_the_identifier_first(client, fake):
    # WeKnora's own ranking misses the identifier; the adapter scans the parsed text for it
    text = "# 合成异常记录\n\n第 1 行无关。\n错误码 E-0x1F3A 表示电池容量衰减超限。\n结尾。"
    fake.docs = [fake.doc, {**fake.doc, "id": "k5", "title": "异常记录", "file_name": "c32.md", "parse_status": "completed"}]
    fake.extra_chunks["k5"] = [{"id": "c50", "chunk_index": 0, "content": text, "start_at": 0, "end_at": len(text)}]
    data = client.post("/v1/search", json={"query": "E-0x1F3A 是什么错误", "kb_ids": ["wk_kb1"], "top_k": 5,
                                           "include_wiki": False}).json()
    first = data["items"][0]
    assert first["doc_id"] == "wk_k5" and "E-0x1F3A" in first["content"] and first["matched_by"] == ["exact"]
    assert first["line_start"] == 1 and first["content"].startswith("# 合成异常记录") and data["items"][1]["doc_id"] == "wk_k1"
    scores = [h["score"] for h in data["items"]]
    assert scores == sorted(scores, reverse=True)  # best-first even after the exact matches moved up
    # a query without an identifier is left as WeKnora ranked it
    plain = client.post("/v1/search", json={"query": "出口压力", "kb_ids": ["wk_kb1"], "include_wiki": False}).json()
    assert [h["doc_id"] for h in plain["items"]] == ["wk_k1"] and "matched_by" not in plain["items"][0]


def test_documents_carry_their_chunk_count_once_counted(client, fake):
    # real WeKnora knowledge bases carry no chunk total: the knowledge-base view starts the background
    # count per document, and the document list then has it
    fake.kb = {**fake.kb, "chunk_count": 0, "processing_count": 0}
    fake.doc = {**fake.doc, "parse_status": "completed", "updated_at": "2026-01-01T00:00:00Z"}
    fake.docs = [fake.doc]
    client.get("/v1/kbs/wk_kb1")
    import time
    for _ in range(50):
        items = client.get("/v1/kbs/wk_kb1/documents").json()["items"]
        if items[0]["chunk_count"] is not None:
            break
        time.sleep(0.05)
    assert items[0]["chunk_count"] == 3
    assert client.get("/v1/documents/wk_k1").json()["chunk_count"] == 3
