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
        m = re.match(r"^/kbs/k1/documents/([^/]+)/status$", path)
        if m:
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
        m = re.match(r"^/kbs/k1/pages/([^/]+)$", path)
        if m:
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


# ---- uploads: names DA can store, text as UTF-8 ---------------------------------------


def _file_part(raw: bytes) -> tuple[str, bytes]:
    """(file name, content) of the multipart ``file`` field sent to DA."""

    found = re.search(rb'name="file"; filename="([^"]*)"\r\n(?:[^\r\n]+\r\n)*\r\n(.*?)\r\n--', raw, re.S)
    return found.group(1).decode(), found.group(2)


def _uploads(fake: FakeDa) -> list[dict]:
    return [c[2] for c in fake.calls if c[1] == "/kbs/k1/upload"]


def test_long_file_names_keep_up_to_255_characters(client, fake):
    # the platform stores the file under a short name of its own and keeps the full name
    name = "超长文件名_" + "储能电池运维知识" * 24 + ".md"  # 200 characters, about 600 bytes
    r = client.post("/v1/kbs/da_k1/documents", files={"file": (name, "标记 SAMPLE-LONG-0001".encode())})
    assert r.status_code == 201
    sent, content = _file_part(_uploads(fake)[-1]["raw"])
    assert sent == name
    assert content == "标记 SAMPLE-LONG-0001".encode()
    longer = "超长文件名_" + "储能电池运维知识" * 40 + ".md"
    client.post("/v1/kbs/da_k1/documents", files={"file": (longer, b"x")})
    sent = _file_part(_uploads(fake)[-1]["raw"])[0]
    assert len(sent) <= 255 and sent.startswith("超长文件名_储能") and sent.endswith("….md")
    # text documents are named after their title
    client.post("/v1/kbs/da_k1/documents/text", json={"title": "巡检记录" * 80, "text": "正文"})
    sent = _file_part(_uploads(fake)[-1]["raw"])[0]
    assert len(sent) <= 255 and sent.endswith(".md")
    # names that fit are left alone
    client.post("/v1/kbs/da_k1/documents", files={"file": ("泵房规程.md", b"# x")})
    assert _file_part(_uploads(fake)[-1]["raw"])[0] == "泵房规程.md"


def test_too_long_folder_names_are_refused_readably(client, fake):
    r = client.post("/v1/kbs/da_k1/documents", files={"file": ("a.md", b"# a")}, data={"folder_path": "手册/" + "层" * 100})
    assert r.status_code == 400 and "文件夹名称过长" in r.json()["error"]["message"]
    assert _uploads(fake) == []


def test_text_files_reach_da_as_utf8(client, fake):
    gb18030 = "GB18030 编码测试\n生僻字：𠀀𪚥龘；标记 SAMPLE-GB-0001。\n"
    utf16 = "UTF-16 编码测试，标记 SAMPLE-U16-0001。"
    gbk = "采购价格联动会议纪要（合成样例）：基准价上调，联动系数不变。"
    for name, data, text in (("c15.txt", gb18030.encode("gb18030"), gb18030),
                             ("c17.txt", utf16.encode("utf-16-le"), utf16),
                             ("a15.md", gbk.encode("gbk"), gbk)):
        assert client.post("/v1/kbs/da_k1/documents", files={"file": (name, data)}).status_code == 201, name
        assert _file_part(_uploads(fake)[-1]["raw"])[1] == text.encode("utf-8"), name
    bom_csv = "\ufeff日期,指标,数值\n2026-01-01,样例,1\n".encode("utf-8")
    client.post("/v1/kbs/da_k1/documents", files={"file": ("c16.csv", bom_csv)})
    assert _file_part(_uploads(fake)[-1]["raw"])[1] == bom_csv


def test_text_of_unknown_encoding_is_refused_not_garbled(client, fake, tmp_path, monkeypatch):
    r = client.post("/v1/kbs/da_k1/documents", files={"file": ("x.txt", bytes(range(256)) * 4)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_encoding"
    # chunked uploads go the same way
    monkeypatch.setenv("DA_KB_UPLOAD_DIR", str(tmp_path))
    app_mod.get_settings.cache_clear()
    data = "分块上传的 GBK 文本".encode("gbk")
    begin = client.post("/v1/uploads", json={"kb_id": "da_k1", "file_name": "y.txt", "file_size": len(data)}).json()
    client.put(f"/v1/uploads/{begin['upload_id']}/parts/0", content=data)
    assert client.post(f"/v1/uploads/{begin['upload_id']}/complete").status_code == 201
    assert [_file_part(u["raw"])[1] for u in _uploads(fake)] == ["分块上传的 GBK 文本".encode("utf-8")]


def test_search_keeps_one_whole_page_hit_per_document():
    from da_kb_adapter.mapping import distinct_hits

    hits = [{"doc_id": "da_a05", "anchor_id": None, "page_type": "structure_md"},
            {"doc_id": "da_a05", "anchor_id": None, "page_type": "fulltext"},
            {"doc_id": "da_a05", "anchor_id": None, "page_type": "abstract"},
            {"doc_id": "da_a04", "anchor_id": None, "page_type": "structure_md"},
            {"doc_id": "da_a05", "anchor_id": "da_a05:paragraph:2", "page_type": "structure_md"},
            {"doc_id": "da_a05", "anchor_id": "da_a05:paragraph:2", "page_type": "structure_md"}]
    kept = distinct_hits(hits)
    assert [(h["doc_id"], h["anchor_id"], h["page_type"]) for h in kept] == [
        ("da_a05", None, "structure_md"), ("da_a04", None, "structure_md"), ("da_a05", "da_a05:paragraph:2", "structure_md")]


def test_search_drops_the_same_text_of_a_document_shown_twice():
    from da_kb_adapter.mapping import distinct_hits

    same = "第十二条 实施日期"
    hits = [{"doc_id": "da_a05", "anchor_id": "da_a05:paragraph:1", "content": same},
            {"doc_id": "da_a05", "anchor_id": None, "content": " 第十二条  实施日期 "},
            {"doc_id": "da_a04", "anchor_id": None, "content": same}]
    assert [(h["doc_id"], h["anchor_id"]) for h in distinct_hits(hits)] == [("da_a05", "da_a05:paragraph:1"), ("da_a04", None)]


def test_search_puts_each_documents_best_hit_first():
    from da_kb_adapter.mapping import distinct_hits

    hits = [{"doc_id": "da_a05", "anchor_id": "da_a05:table:1", "content": "正文"},
            {"doc_id": "da_a05", "anchor_id": None, "content": "摘要甲"},
            {"doc_id": "da_a04", "anchor_id": "da_a04:paragraph:2", "content": "正文乙"},
            {"doc_id": "da_a04", "anchor_id": None, "content": "摘要乙"},
            {"doc_id": "da_a10", "anchor_id": None, "content": "摘要丙"}]
    assert [h["content"] for h in distinct_hits(hits)] == ["正文", "正文乙", "摘要丙", "摘要甲", "摘要乙"]
