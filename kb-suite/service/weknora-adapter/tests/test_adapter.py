from __future__ import annotations

import json
import re

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
        if m := re.match(r"^/knowledge-bases/([^/]+)$", path):
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
            return ok({**self.doc, "id": "k9", "parse_status": "pending"})
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
                                                                 "content-disposition": 'attachment; filename="pump.pdf"'})
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
    assert items[0]["stats"] == {"documents": 2, "completed": 0, "processing": 1, "failed": 1, "cancelled": 0, "chunks": 3}
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
    assert stats["cancelled"] == 1 and stats["completed"] == 5 - 1 - 1 - 1


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
