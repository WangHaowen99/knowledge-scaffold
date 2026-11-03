from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from da_kb_adapter import app as app_mod
from da_kb_adapter.app import DaKb, create_app
from da_kb_adapter.mapping import split_chunks

FULLTEXT = "# 泵房规程\n\n循环泵出口压力低于 0.3 MPa 时联锁停机。\n\n每班记录出口压力。"


class FakeDa:
    """DeepAnalyze /api/knowledge with one knowledge base (synthetic data)."""

    def __init__(self, structured: bool = False) -> None:
        self.structured = structured
        self.calls: list[tuple[str, str, dict]] = []
        self.docs = [{"id": "d1", "kb_id": "k1", "filename": "泵房规程.md", "file_type": "md", "file_size": "120",
                      "folder_path": "", "status": "ready", "updated_at": "2026-01-01T00:00:00Z"},
                     {"id": "d2", "kb_id": "k1", "filename": "坏.pdf", "file_type": "pdf", "status": "error",
                      "processing_error": "Cancelled by user"}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/knowledge")
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") and request.content else {}
        self.calls.append((request.method, path, {"body": body, "params": dict(request.url.params), "raw": request.content}))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/kbs" and request.method == "GET":
            return httpx.Response(200, json={"knowledgeBases": [{"id": "k1", "name": "规程库", "description": None}]})
        if path == "/kbs" and request.method == "POST":
            return httpx.Response(201, json={"id": "k2", "name": body["name"]})
        if path == "/kbs/k1/documents":
            return httpx.Response(200, json={"documents": self.docs})
        if m := re.match(r"^/kbs/k1/documents/([^/]+)/status$", path):
            if any(d["id"] == m.group(1) for d in self.docs):
                return httpx.Response(200, json={"docId": m.group(1), "status": "ready"})
            return httpx.Response(404, json={"error": "Document not found"})
        if path == "/k1/expand":
            level = body["level"]
            if level == "L1":
                return httpx.Response(200, json={"content": "LLM 概览", "level": "L1", "pageId": "p-l1"})
            if level == "L0":
                return httpx.Response(200, json={"content": "泵房规程摘要", "level": "L0", "pageId": "p-l0"})
            return httpx.Response(404, json={"error": "No page found"})
        if m := re.match(r"^/kbs/k1/pages/([^/]+)$", path):
            if request.url.params.get("level") == "L2":
                return httpx.Response(200, json={"id": "p-l2", "level": "L2", "pageType": "fulltext", "content": FULLTEXT})
            page_type = "structure_md" if self.structured else "overview"
            return httpx.Response(200, json={"id": m.group(1), "level": "L1", "pageType": page_type})
        if path == "/search":
            results = [
                {"pageId": "p-l1", "kbId": "k1", "docId": "d1", "pageType": "overview", "title": "Overview: d1",
                 "score": 0.03, "snippet": "<mark>出口压力</mark>低于", "anchorId": "d1:paragraph:3"},
                {"pageId": "r1", "kbId": "k1", "docId": None, "pageType": "report", "title": "报告", "score": 0.02},
            ]
            if request.url.params.get("kbIds"):
                # as if DA ignored kbIds: a hit outside the requested knowledge bases must not get through
                results.append({"pageId": "p-x", "kbId": "k2", "docId": "d7", "pageType": "overview",
                                "title": "Overview: d7", "score": 0.01, "snippet": "范围外"})
            return httpx.Response(200, json={"results": results})
        if path == "/kbs/k1/upload":
            return httpx.Response(201, json={"id": "d9", "filename": "x.md", "fileType": "md", "status": "uploaded"})
        if path.startswith("/kbs/k1/process/"):
            return httpx.Response(200, json={"status": "queued"})
        if path == "/kbs/k1/documents/d1" and request.method == "DELETE":
            return httpx.Response(200, json={"id": "d1", "deleted": True})
        return httpx.Response(404, json={"error": "Not found", "detail": path})


def _client(fake: FakeDa, monkeypatch) -> TestClient:
    monkeypatch.setenv("DA_KB_ADAPTER_KEY", "adapter-key")
    app_mod.get_settings.cache_clear()
    backend = DaKb(httpx.AsyncClient(base_url="https://da.example.com", transport=httpx.MockTransport(fake)))
    return TestClient(create_app(backend), headers={"Authorization": "Bearer adapter-key"})


@pytest.fixture
def fake():
    return FakeDa()


@pytest.fixture
def client(fake, monkeypatch):
    with _client(fake, monkeypatch) as c:
        yield c
    app_mod.get_settings.cache_clear()


def test_capabilities(client):
    caps = client.get("/v1/capabilities").json()
    assert caps["provider"]["type"] == "deepanalyze" and "pdf" not in caps["file_types"]
    # DA has no JSON L2 locator data
    assert "wiki.read" not in caps["features"] and "content.L2" not in caps["features"]


def test_kbs_count_statuses(client):
    kb = client.get("/v1/kbs").json()["items"][0]
    assert kb["id"] == "da_k1"
    assert kb["stats"] == {"documents": 2, "completed": 1, "processing": 0, "failed": 0, "cancelled": 1}


def test_documents_map_status_and_size(client):
    docs = client.get("/v1/kbs/da_k1/documents").json()["items"]
    assert [(d["id"], d["parse_status"]) for d in docs] == [("da_d1", "completed"), ("da_d2", "cancelled")]
    assert docs[0]["file_size"] == 120 and docs[0]["title"] == "泵房规程"
    filtered = client.get("/v1/kbs/da_k1/documents", params={"status": "completed"}).json()
    assert filtered["total"] == 1


def test_document_list_carries_cached_summaries(client, fake):
    assert client.get("/v1/kbs/da_k1/documents").json()["items"][0]["summary"] == "泵房规程摘要"
    calls = len([c for c in fake.calls if c[1] == "/k1/expand"])
    client.get("/v1/kbs/da_k1/documents")
    assert len([c for c in fake.calls if c[1] == "/k1/expand"]) == calls


def test_document_lookup_finds_the_kb_and_summary(client):
    doc = client.get("/v1/documents/da_d1").json()
    assert doc["kb_id"] == "da_k1" and doc["summary"] == "泵房规程摘要"
    assert client.get("/v1/documents/da_nope").status_code == 404


def test_l1_is_full_text_when_da_only_has_an_overview(client):
    assert client.get("/v1/documents/da_d1/content", params={"level": "L1"}).text == FULLTEXT
    r = client.get("/v1/documents/da_d1/content", params={"level": "L2"})
    assert r.status_code == 501 and r.json()["error"]["code"] == "unsupported"


def test_l1_is_the_structure_page_when_present(monkeypatch):
    with _client(FakeDa(structured=True), monkeypatch) as c:
        assert c.get("/v1/documents/da_d1/content", params={"level": "L1"}).text == "LLM 概览"


def test_chunks_split_l1(client):
    page = client.get("/v1/documents/da_d1/chunks").json()
    assert page["total"] == 1 and page["items"][0]["id"] == "da_d1.0" and page["items"][0]["line_start"] == 1


def test_split_chunks_respects_paragraphs():
    text = ("段落一。" * 100) + "\n\n" + ("段落二。" * 100)
    pieces = split_chunks(text, size=500)
    assert len(pieces) == 2 and pieces[1][0] == 3 and pieces[1][2].startswith("段落二")


def test_search_drops_pages_without_documents_and_prefixes_anchors(client):
    data = client.post("/v1/search", json={"query": "出口压力", "kb_ids": ["da_k1"], "top_k": 5}).json()
    (hit,) = data["items"]
    assert hit["doc_id"] == "da_d1" and hit["anchor_id"] == "da_d1:paragraph:3"
    assert hit["content"] == "出口压力低于" and hit["title"] == "泵房规程.md"


def test_text_becomes_a_markdown_upload(client, fake):
    doc = client.post("/v1/kbs/da_k1/documents/text", json={"title": "巡检/记录", "text": "正文"}).json()
    assert doc["id"] == "da_d9" and doc["parse_status"] == "pending"
    assert 'filename="巡检_记录.md"'.encode() in fake.calls[-1][2]["raw"]


def test_reprocess_forces_a_rerun(client, fake):
    client.post("/v1/documents/da_d1/reprocess")
    assert fake.calls[-1][1] == "/kbs/k1/process/d1" and fake.calls[-1][2]["params"] == {"force": "true"}


def test_backend_errors_keep_status(client):
    r = client.get("/v1/kbs/da_k9")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_structure_and_grep_use_the_full_text(client):
    win = client.get("/v1/documents/da_d1/structure", params={"heading": "泵房规程"}).json()
    assert "联锁停机" in win["content"] and win["anchors"][0]["id"].startswith("da_d1:")
    data = client.post("/v1/grep", json={"pattern": "出口压力"}).json()
    assert [m["doc_id"] for m in data["matches"]] == ["da_d1"] and data["total_matches"] == 2


def test_chunked_upload_ingests_through_da(client, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("DA_KB_UPLOAD_DIR", str(tmp_path))
    app_mod.get_settings.cache_clear()
    begin = client.post("/v1/uploads", json={"kb_id": "da_k1", "file_name": "x.md", "file_size": 2}).json()
    client.put(f"/v1/uploads/{begin['upload_id']}/parts/0", content=b"ab")
    doc = client.post(f"/v1/uploads/{begin['upload_id']}/complete").json()
    assert doc["id"] == "da_d9" and fake.calls[-1][1] == "/kbs/k1/upload"
    assert client.post("/v1/uploads", json={"kb_id": "k1", "file_name": "x", "file_size": 1}).status_code == 404


# ---- ids never steer the upstream URL ----------------------------------------------


def test_ids_that_were_never_issued_do_not_reach_da(client, fake):
    for path in ("/v1/kbs/da_..", "/v1/kbs/da_a%3Fb", "/v1/documents/da_%2E%2E/content", "/v1/documents/da_a%2Cb"):
        assert client.get(path).status_code == 404, path
    assert fake.calls == []


def test_page_ids_from_da_are_encoded(monkeypatch):
    fake = FakeDa()
    sent: list[bytes] = []

    def odd_page(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.raw_path)
        if request.url.path == "/api/knowledge/k1/expand" and json.loads(request.content)["level"] == "L1":
            return httpx.Response(200, json={"content": "x", "level": "L1", "pageId": "../../../k2"})
        return fake(request)

    with _client(odd_page, monkeypatch) as c:
        c.get("/v1/kbs/da_k1/documents")
        c.get("/v1/documents/da_d1/content", params={"level": "L1"})
    assert b"/api/knowledge/k2" not in sent
    assert b"/api/knowledge/kbs/k1/pages/..%2F..%2F..%2Fk2" in sent


# ---- search ------------------------------------------------------------------------


def test_min_score_does_not_filter_on_the_fusion_score(client):
    items = client.post("/v1/search", json={"query": "出口压力", "top_k": 5, "min_score": 0.2}).json()["items"]
    assert len(items) == 1 and items[0]["relevance"] is None


# ---- lookups -------------------------------------------------------------------------


def test_grep_lists_each_knowledge_base_once(client, fake):
    fake.docs = [{**fake.docs[0], "id": f"d{i}", "filename": f"f{i}.md"} for i in range(40)]
    assert client.post("/v1/grep", json={"pattern": "出口压力", "kb_ids": ["da_k1"]}).json()["total_documents"] == 40
    assert len([c for c in fake.calls if c[1] == "/kbs/k1/documents"]) == 1


def test_readers_check_one_document_not_the_whole_list(client, fake):
    client.get("/v1/kbs/da_k1/documents")
    before = len([c for c in fake.calls if c[1] == "/kbs/k1/documents"])
    client.get("/v1/documents/da_d1/structure")
    client.get("/v1/documents/da_d1/chunks")
    assert len([c for c in fake.calls if c[1] == "/kbs/k1/documents"]) == before
    fake.docs = fake.docs[1:]
    assert client.get("/v1/documents/da_d1/structure").status_code == 404


def test_split_chunks_line_numbers_match_a_full_count():
    text = "\n".join(f"第 {i} 行" + "。" * (i % 50) for i in range(2000))
    for line, first, _ in split_chunks(text, size=300):
        assert line == text.count("\n", 0, first) + 1


def test_search_keeps_only_the_requested_knowledge_bases(client):
    data = client.post("/v1/search", json={"query": "出口压力", "kb_ids": ["da_k1"], "top_k": 5}).json()
    assert {h["kb_id"] for h in data["items"]} == {"da_k1"}
