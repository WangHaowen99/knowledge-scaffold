from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from da_kb_adapter import app as app_mod
from da_kb_adapter.app import DaKb, create_app
from da_kb_adapter.mapping import split_chunks

FULLTEXT = "# 泵房规程\n\n循环泵出口压力低于 0.3 MPa 时联锁停机。\n\n每班记录出口压力。"


class FakeDa:
    """DeepAnalyze /api/knowledge with one knowledge base (synthetic data).

    ``platform`` makes it the platform's knowledge service: no /api/settings, and
    501 for the routes the platform has not taken over. The routes beyond the
    core contract live in ``FakeDaExtras`` (below).
    """

    def __init__(self, structured: bool = False, platform: bool = False) -> None:
        self.structured = structured
        self.platform = platform
        self.extras = FakeDaExtras(self)
        self.calls: list[tuple[str, str, dict]] = []
        self.docs = [{"id": "d1", "kb_id": "k1", "filename": "泵房规程.md", "file_type": "md", "file_size": "120",
                      "folder_path": "", "status": "ready", "updated_at": "2026-01-01T00:00:00Z"},
                     {"id": "d2", "kb_id": "k1", "filename": "坏.pdf", "file_type": "pdf", "status": "error",
                      "processing_error": "Cancelled by user"}]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/knowledge")
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") and request.content else {}
        self.calls.append((request.method, path, {"body": body, "params": dict(request.url.params), "raw": request.content,
                                                  "headers": dict(request.headers)}))
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        handled = self.extras(request, path, body)
        if handled is not None:
            return handled
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
                return httpx.Response(200, json={"content": self.extras.l1_text, "level": "L1", "pageId": "p-l1"})
            if level == "L0":
                return httpx.Response(200, json={"content": self.extras.abstract, "level": "L0", "pageId": "p-l0"})
            return httpx.Response(404, json={"error": "No page found"})
        m = re.match(r"^/kbs/k1/pages/([^/]+)$", path)
        if m:
            if request.url.params.get("level") == "L2":
                return httpx.Response(200, json={"id": "p-l2", "level": "L2", "pageType": "fulltext", "content": FULLTEXT})
            page_type = "structure_md" if self.structured else "overview"
            return httpx.Response(200, json={"id": m.group(1), "level": "L1", "pageType": page_type, "docId": "d1"})
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


OVERVIEW = "# 结构化概览\n\n泵房规程规定了出口压力阈值。"
FILE_BYTES = bytes(range(120))
NOT_MIGRATED = re.compile(
    r"^/kbs/[^/]+/(regenerate-abstracts|quality-report|preprocess(/.*)?|"
    r"documents/[^/]+/(regenerate-abstract|quality-audit|export/[^/]+))$")


def _json(status: int, data, **headers) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


class FakeDaExtras:
    """DA routes beyond the core contract: actions, preprocessing, views, reports, settings, preview."""

    def __init__(self, fake: FakeDa) -> None:
        self.fake = fake
        self.now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.down = False
        self.abstract = "泵房规程摘要"
        # the structure page's text (DA's L1 of a structured document)
        self.l1_text = "LLM 概览"
        self.raw: dict | None = None
        self.raw_reads = 0
        self.mineru_status: dict = {"connected": False, "enabled": False}
        self.leveled: dict | None = None
        self.pre_status: dict = {"running": False, "lastReport": None}
        self.report_md: str | None = None
        self.audited = False
        self.media: dict = {"type": None}
        self.anchors: dict[str, dict] = {}
        self.structure_map: list = []
        self.events: list = []
        self.reports = [{"id": "r1", "title": "泵房分析报告", "updatedAt": "2026-01-01T08:00:00Z"}]
        self.graph_nodes: list = [{"id": "p-l0", "label": "Abstract: d1", "type": "document"},
                                  {"id": "r1", "label": "泵房分析报告", "type": "report"}]
        self.graph_edges: list = []
        self.wiki_pages = {
            "r1": {"id": "r1", "pageType": "report", "title": "泵房分析报告",
                   "content": "# 泵房分析报告\n\n> 生成于测试\n\n出口压力偏低的原因分析。", "links": []},
            "p-l0": {"id": "p-l0", "pageType": "abstract", "title": "Abstract: d1", "links": []},
            "p-ov": {"id": "p-ov", "pageType": "overview", "title": "Overview: d1", "links": []},
            "p-l1": {"id": "p-l1", "pageType": "overview", "title": "Overview: d1", "links": []},
        }
        self.pages = {"p-l0": {"pageType": "abstract", "docId": "d1"}, "p-ov": {"pageType": "overview", "docId": "d1"},
                      "e1": {"pageType": "entity", "docId": None, "title": "循环泵"},
                      "e2": {"pageType": "entity", "docId": None, "title": "冷却塔"}}
        self.settings = {
            "/providers": {
                "defaults": {"main": "main-llm", "summarizer": "main-llm", "embedding": "emb", "vlm": ""},
                "providers": [
                    {"id": "main-llm", "name": "主力模型", "type": "openai-compatible", "model": "qwen-plus",
                     "apiKey": "••••••••abcd", "enabled": True, "endpoint": "https://llm.example.com/v1", "maxTokens": 0},
                    {"id": "emb", "name": "emb", "type": "openai-compatible", "model": "text-embedding-v4", "apiKey": "",
                     "enabled": True, "endpoint": "https://emb.example.com/v1", "dimension": 1024}]},
            "/docling-config": {"ocr": True, "ocr_engine": "rapidocr", "table_mode": "accurate", "use_vlm": False,
                                "vlm_mode": "inline", "vlm_api_url": "", "parallelism": 5, "layout_model": "x"},
            "/mineru-config": {"apiUrl": "https://mineru.example.com", "defaultBackend": "pipeline", "defaultLang": "ch",
                               "formulaEnable": True, "tableEnable": True, "imageAnalysis": True, "timeout": 600,
                               "enabled": False},
            "/vlm-config": {"thinkingEnabled": False, "thinkingParamType": "chat_template_kwargs", "maxTokens": 0,
                            "timeoutMs": 300000},
            "/pipeline-strategies": [
                {"fileType": "pdf", "pipelines": [{"pipeline": "mineru", "priority": 1},
                                                  {"pipeline": "docling", "priority": 2}]},
                {"fileType": "spreadsheet", "pipelines": [{"pipeline": "native", "priority": 1}]}],
            "/parsing-priority-mode": {"mode": "quality", "config": {}},
            "auto_process": "true",
        }
        self.writes: list[tuple[str, object]] = []

    def dated(self, data) -> httpx.Response:
        return _json(200, data, date=format_datetime(self.now, usegmt=True))

    def __call__(self, request: httpx.Request, path: str, body: dict) -> httpx.Response | None:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        full, method, params = request.url.path, request.method, request.url.params
        if full.startswith("/api/settings"):
            if self.fake.platform:
                return _json(404, {"error": "Not found"})
            return self.setting(method, full.removeprefix("/api/settings"), body)
        if self.fake.platform and full.startswith("/api/knowledge") and NOT_MIGRATED.match(path):
            return _json(501, {"error": "本期未迁移到知识服务", "notMigrated": True})
        if "kb-adapter-probe" in full or "doc-adapter-probe" in full:
            if full.startswith("/api/preview/anchors/"):
                return _json(404, {"error": "Anchor not found"})
            return _json(404, {"error": "Knowledge base not found"})
        if full.startswith("/api/preview"):
            sub = full.removeprefix("/api/preview")
            if sub.startswith("/anchors/"):
                found = self.anchors.get(sub.removeprefix("/anchors/"))
                return _json(200, found) if found else _json(404, {"error": "Anchor not found"})
            if sub == "/kbs/k1/documents/d1/structure-map":
                return _json(200, {"structureMap": self.structure_map})
            if sub == "/kbs/k1/documents/d1/preview/raw":
                self.raw_reads += 1
                return _json(200, {"content": self.raw}) if self.raw else _json(404, {"error": "Raw data not found"})
            return None
        if full.startswith("/api/reports"):
            sub = full.removeprefix("/api/reports")
            if sub == "/reports/kb/k1":
                return _json(200, {"kbId": "k1", "reports": self.reports})
            if sub == "/reports/r1/sources":
                return _json(200, {"sources": [{"docId": "d1", "originalName": "泵房规程.md"}]})
            if sub == "/timeline/k1":
                return _json(200, {"kbId": "k1", "events": self.events, "totalCount": len(self.events)})
            if sub == "/graph/k1":
                return _json(200, {"kbId": "k1", "nodes": self.graph_nodes, "edges": self.graph_edges})
            return None
        return self.knowledge(method, path, params, body)

    def knowledge(self, method: str, path: str, params, body: dict) -> httpx.Response | None:
        docs = self.fake.docs
        if path == "/kbs/k1" and method == "GET":
            return self.dated({"id": "k1", "name": "规程库"})
        if path in ("/kbs/k1/process-all", "/kbs/k1/reindex") and method == "POST":
            return self.dated({"enqueued": len(docs)})
        if path == "/kbs/k1/preprocess" and method == "POST":
            if self.pre_status.get("running"):
                return _json(409, {"error": "该知识库已有预处理任务正在运行"})
            return self.dated({"message": "预处理已启动", "sessionId": "s1", "kbId": "k1"})
        if path == "/kbs/k1/preprocess/status":
            return self.dated(self.pre_status)
        if path == "/kbs/k1/preprocess/cancel":
            running = bool(self.pre_status.get("running"))
            self.pre_status = {**self.pre_status, "running": False, **({"lastStatus": "failed"} if running else {})}
            return _json(200, {"cancelled": running})
        if path == "/kbs/k1/preprocess/report":
            if self.report_md is None:
                return _json(404, {"error": "Report not generated yet"})
            return _json(200, {"report": self.report_md, "stats": None})
        if path == "/kbs/k1/regenerate-abstracts":
            self.abstract = "补全后的摘要"
            return _json(200, {"kbId": "k1", "total": 2, "results": [
                {"docId": "d1", "filename": "泵房规程.md", "status": "regenerated"},
                {"docId": "d2", "filename": "坏.pdf", "status": "skipped_not_ready"}]})
        if path == "/kbs/k1/rebuild-embeddings":
            return _json(200, {"kbId": "k1", "status": "completed", "provider": "emb", "dimension": 1024})
        if path == "/kbs/k1/documents/d1/regenerate-abstract":
            self.abstract = "重新生成的摘要"
            return _json(200, {"documentId": "d1", "status": "regenerated"})
        if path == "/kbs/k1/documents/d1/quality-audit":
            self.audited = True
            return _json(200, {"docId": "d1", "score": 62, "issues": ["表格缺表头"],
                               "reExtracted": params.get("reExtract") == "true", "modelUsed": "vlm-x"})
        if path == "/kbs/k1/quality-report":
            audit = {"score": 62, "issues": ["表格缺表头"], "auditedAt": "2026-01-01T09:00:00Z"} if self.audited else None
            return _json(200, {"kbId": "k1", "documents": [
                {"docId": "d1", "filename": "泵房规程.md", "qualityAudit": audit},
                {"docId": "d2", "filename": "坏.pdf", "qualityAudit": None}]})
        if path == "/kbs/k1/documents/d1/media-metadata":
            return _json(200, self.media)
        if path == "/kbs/k1/documents/d1/download":
            return httpx.Response(200, content=FILE_BYTES, headers={"content-type": "application/octet-stream",
                                                                    "content-disposition": 'attachment; filename="x.md"'})
        if path == "/kbs/k1/documents/d1/original":
            found = re.match(r"bytes=(\d+)-(\d*)", self.fake.calls[-1][2]["headers"].get("range", ""))
            if not found:
                return httpx.Response(200, content=FILE_BYTES, headers={"accept-ranges": "bytes"})
            start = int(found.group(1))
            end = int(found.group(2)) if found.group(2) else len(FILE_BYTES) - 1
            return httpx.Response(206, content=FILE_BYTES[start:end + 1], headers={
                "content-range": f"bytes {start}-{end}/{len(FILE_BYTES)}", "accept-ranges": "bytes",
                "content-type": "application/octet-stream"})
        m = re.match(r"^/kbs/k1/documents/d1/export/(.+)$", path)
        if m:
            if m.group(1) == "markdown" and self.fake.structured:
                return httpx.Response(200, text="## 泵房规程\n\n结构化正文", headers={"content-type": "text/markdown"})
            if m.group(1) == "structure-bundle":
                return _json(200, {"docId": "d1", "pages": [], "anchors": []})
            return _json(404, {"error": "No structure pages found"})
        if path == "/kbs/k1/folder" and method == "DELETE":
            return _json(200, {"kbId": "k1", "deleted": 1})
        m = re.match(r"^/k1/wiki/(.+)$", path)
        if m:
            found = self.wiki_pages.get(m.group(1))
            return _json(200, found) if found else _json(404, {"error": "Page not found"})
        m = re.match(r"^/kbs/k1/pages/([^/]+)$", path)
        if m and params.get("pageType") == "overview":
            if self.fake.structured:
                return _json(200, {"id": "p-l1", "level": "L1", "pageType": "structure_md", "docId": "d1"})
            return _json(200, {"id": "p-ov", "level": "L1", "pageType": "overview", "docId": "d1", "content": OVERVIEW})
        if m and m.group(1) in self.pages and not params.get("level"):
            page = self.pages[m.group(1)]
            return _json(200, {"id": m.group(1), "title": page.get("title") or m.group(1), **page})
        if path == "/search" and self.leveled is not None:
            return _json(200, {"results": self.leveled})
        if path == "/search" and params.get("levels"):
            return _json(200, {"results": {
                "L0": [{"pageId": "p-l0", "kbId": "k1", "docId": "d1", "title": "Abstract: d1", "score": 0.5,
                        "snippet": "摘要命中"}],
                "L2": [{"pageId": "p-l2", "kbId": "k1", "docId": "d1", "title": "Document: d1", "score": 0.9,
                        "snippet": "全文<mark>命中</mark>", "anchorId": "d1:paragraph:1"}]}})
        return None

    def setting(self, method: str, path: str, body):
        store = self.settings
        if method != "GET":
            self.writes.append((path, body))
        if path == "/providers":
            return _json(200, store["/providers"])
        if path == "/mineru-status":
            return _json(200, self.mineru_status)
        m = re.match(r"^/providers/([^/]+)$", path)
        if m:
            providers = store["/providers"]["providers"]
            found = next((p for p in providers if p["id"] == m.group(1)), None)
            if method == "GET":
                return _json(200, found) if found else _json(404, {"error": "Provider not found"})
            stored = dict(body)
            if str(stored.get("apiKey", "")).startswith("••••••••"):
                stored["apiKey"] = found["apiKey"]
            elif stored.get("apiKey"):
                stored["apiKey"] = "••••••••" + stored["apiKey"][-4:]
            providers[providers.index(found)] = stored
            return _json(200, {"success": True})
        if path == "/defaults" and method == "PUT":
            store["/providers"]["defaults"].update(body)
            return _json(200, {"success": True})
        if path == "/key/auto_process":
            if method == "PUT":
                store["auto_process"] = json.dumps(body["value"])
            return _json(200, {"key": "auto_process", "value": store["auto_process"]})
        if path in ("/docling-config", "/mineru-config", "/vlm-config"):
            if method == "PUT":
                store[path].update(body)
            return _json(200, store[path])
        if path == "/pipeline-strategies":
            if method == "PUT":
                store[path] = body
            return _json(200, store[path])
        if path == "/parsing-priority-mode":
            if method == "PUT":
                store[path] = {"mode": body["mode"], "config": {}}
            return _json(200, store[path])
        return None


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
    # DA has no JSON L2 locator data; its abstracts, overviews and reports are readable as a wiki
    assert "wiki.read" in caps["features"] and "content.L2" not in caps["features"]


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


def test_search_prefixes_anchors_and_keeps_reports_as_wiki_hits(client):
    data = client.post("/v1/search", json={"query": "出口压力", "kb_ids": ["da_k1"], "top_k": 5}).json()
    hit, report = data["items"]
    assert hit["doc_id"] == "da_d1" and hit["anchor_id"] == "da_d1:paragraph:3"
    assert hit["content"] == "出口压力低于" and hit["title"] == "泵房规程.md"
    # an agent report has no document: it is a wiki page
    assert report["chunk_type"] == "wiki_page" and report["doc_id"] is None
    assert report["wiki_slug"] == "report/r1" and report["wiki_type"] == "concept" and report["kb_id"] == "da_k1"
    # without the wiki only document hits remain
    data = client.post("/v1/search", json={"query": "出口压力", "kb_ids": ["da_k1"], "include_wiki": False}).json()
    assert [h["doc_id"] for h in data["items"]] == ["da_d1"]


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
    assert len(items) == 2 and {i["relevance"] for i in items} == {None}


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


# ---- v1.1: what is declared follows what the backend serves ---------------------------------------

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts" / "knowledge-provider" / "v1"
SECTION_9 = {"actions", "jobs", "views", "settings", "search.options", "evidence", "documents.file.range",
             "documents.export", "folders", "folders.delete"}


def _probes(fake: FakeDa) -> int:
    return len([c for c in fake.calls if "adapter-probe" in c[1]])


def _platform(monkeypatch):
    return _client(FakeDa(platform=True), monkeypatch)


def test_capabilities_declare_what_da_serves(client):
    caps = client.get("/v1/capabilities").json()
    assert SECTION_9 | {"wiki.read"} <= set(caps["features"])
    assert [a["id"] for a in caps["actions"]] == ["process_pending", "regenerate_summaries", "reindex",
                                                   "rebuild_embeddings", "preprocess", "regenerate_summary",
                                                   "quality_audit"]
    assert all(a["confirm"] for a in caps["actions"] if a["id"] in {"reindex", "rebuild_embeddings", "preprocess"})
    assert [v["id"] for v in caps["views"]] == ["overview", "structure_map", "media", "quality_report", "timeline"]
    assert caps["export_formats"] == ["md", "json", "doctags", "manifest"]
    props = caps["settings_schema"]["properties"]
    assert set(props) == {"models", "providers", "processing", "pipelines", "docling", "mineru", "vlm"}
    assert props["models"]["properties"]["main"]["enum"] == ["", "main-llm", "emb"]
    assert "重新生成向量" in props["models"]["properties"]["embedding"]["description"]
    assert props["providers"]["properties"]["main-llm"]["properties"]["api_key"]["x-secret"] is True
    # a choice only where DA has more than one pipeline
    assert list(props["pipelines"]["properties"]) == ["pdf"]
    assert set(caps["search_options_schema"]["properties"]) == {"mode", "levels"}


def test_capabilities_match_the_contract_schema(monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    if not (CONTRACT_DIR / "capabilities.schema.json").exists():
        pytest.skip("the contract is not next to the tests")
    schema = json.loads((CONTRACT_DIR / "capabilities.schema.json").read_text(encoding="utf-8"))
    for fake in (FakeDa(), FakeDa(platform=True)):
        with _client(fake, monkeypatch) as c:
            jsonschema.validate(c.get("/v1/capabilities").json(), schema)


def test_platform_without_da_ts_routes_drops_those_features(monkeypatch):
    with _platform(monkeypatch) as c:
        caps = c.get("/v1/capabilities").json()
        assert "settings" not in caps["features"] and caps["settings_schema"] is None
        assert [a["id"] for a in caps["actions"]] == ["process_pending", "reindex", "rebuild_embeddings"]
        assert [v["id"] for v in caps["views"]] == ["overview", "structure_map", "media", "timeline"]
        # the platform cannot export; markdown still comes from the L1 text
        assert caps["export_formats"] == ["md"]
        assert {"wiki.read", "jobs", "evidence", "folders.delete", "documents.file.range"} <= set(caps["features"])
        # what is not declared answers 501, it does not fail later
        for method, path in (("POST", "/v1/kbs/da_k1/actions/preprocess"), ("GET", "/v1/settings"),
                             ("PUT", "/v1/settings"), ("GET", "/v1/kbs/da_k1/views/quality_report"),
                             ("POST", "/v1/documents/da_d1/actions/quality_audit"),
                             ("GET", "/v1/documents/da_d1/export/json")):
            r = c.request(method, path, json={})
            assert r.status_code == 501 and r.json()["error"]["code"] == "unsupported", path
        r = c.get("/v1/documents/da_d1/export/md")
        assert r.status_code == 200 and r.text == FULLTEXT


def test_routes_are_probed_once_and_again_after_a_while(client, fake, monkeypatch):
    client.get("/v1/capabilities")
    first = _probes(fake)
    assert first > 0
    client.get("/v1/capabilities")
    client.get("/v1/kbs/da_k1/folders")
    assert _probes(fake) == first
    monkeypatch.setattr(app_mod, "ROUTES_TTL_S", 0.0)
    client.get("/v1/capabilities")
    assert _probes(fake) == 2 * first


def test_an_outage_hides_nothing_it_knew_and_is_probed_again(client, fake, monkeypatch):
    fake.extras.down = True
    # nothing known yet: only the core features, and nothing is cached
    caps = client.get("/v1/capabilities")
    assert caps.status_code == 200 and "actions" not in caps.json()["features"]
    fake.extras.down = False
    assert "actions" in client.get("/v1/capabilities").json()["features"]
    # once known, an outage keeps the last answer
    monkeypatch.setattr(app_mod, "ROUTES_TTL_S", 0.0)
    fake.extras.down = True
    assert "actions" in client.get("/v1/capabilities").json()["features"]


def test_probe_answers_are_read_strictly():
    from da_kb_adapter.offering import served

    js = "application/json"
    assert served(404, js, '{"error": "Knowledge base not found"}') is True
    assert served(404, js, '{"error": "Not found"}') is False
    assert served(404, "text/plain", "404 Not Found") is False
    assert served(501, js, '{"error": "本期未迁移到知识服务", "notMigrated": true}') is False
    assert served(200, "text/html", "<html></html>") is False
    assert served(200, js, "{}") is True and served(405, "", "") is True
    assert served(503, js, "{}") is None


# ---- folders and the original file -----------------------------------------------------------


def test_folders_are_derived_and_filter_the_list(client, fake):
    fake.docs = [{**fake.docs[0], "id": "a1", "folder_path": "规程/泵房"}, {**fake.docs[0], "id": "a2", "folder_path": "规程"},
                 {**fake.docs[0], "id": "a3", "folder_path": ""}]
    assert client.get("/v1/kbs/da_k1/folders").json() == {"items": [{"path": "规程", "documents": 2},
                                                                   {"path": "规程/泵房", "documents": 1}]}
    listed = client.get("/v1/kbs/da_k1/documents", params={"folder": "/规程/"}).json()
    assert listed["total"] == 2 and {d["id"] for d in listed["items"]} == {"da_a1", "da_a2"}
    assert client.get("/v1/kbs/da_k1/documents", params={"folder": "规程/泵房"}).json()["total"] == 1


def test_folder_delete_goes_to_da_after_checking_the_path(client, fake):
    fake.docs = [{**fake.docs[0], "id": "a1", "folder_path": "规程/泵房"}]
    assert client.delete("/v1/kbs/da_k1/folders", params={"path": "规程"}).status_code == 204
    (call,) = [c for c in fake.calls if c[1] == "/kbs/k1/folder"]
    assert call[0] == "DELETE" and call[2]["params"] == {"path": "规程"}
    assert client.delete("/v1/kbs/da_k1/folders", params={"path": "规程/../.."}).status_code == 400
    assert client.delete("/v1/kbs/da_k1/folders", params={"path": "/"}).status_code == 400
    assert client.delete("/v1/kbs/da_k1/folders", params={"path": "别处"}).status_code == 404
    assert len([c for c in fake.calls if c[1] == "/kbs/k1/folder"]) == 1


def test_file_ranges_are_clamped_to_the_file(client, fake):
    r = client.get("/v1/documents/da_d1/file", headers={"Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == bytes([2, 3, 4, 5])
    assert r.headers["content-range"] == "bytes 2-5/120" and r.headers["accept-ranges"] == "bytes"
    # an end past the file is cut to its last byte before DA sees it
    r = client.get("/v1/documents/da_d1/file", headers={"Range": "bytes=100-999"})
    assert r.headers["content-range"] == "bytes 100-119/120" and len(r.content) == 20
    assert fake.calls[-1][2]["headers"]["range"] == "bytes=100-119"
    r = client.get("/v1/documents/da_d1/file", headers={"Range": "bytes=-10"})
    assert r.headers["content-range"] == "bytes 110-119/120"
    r = client.get("/v1/documents/da_d1/file", headers={"Range": "bytes=500-"})
    assert r.status_code == 416 and r.headers["content-range"] == "bytes */120"
    whole = client.get("/v1/documents/da_d1/file")
    assert whole.status_code == 200 and whole.headers["accept-ranges"] == "bytes" and len(whole.content) == 120


# ---- search options ------------------------------------------------------------------------


def test_search_options_use_da_levels_and_mode(client, fake):
    body = {"query": "命中", "kb_ids": ["da_k1"], "options": {"mode": "keyword", "levels": {"L0": True, "L1": False}}}
    items = client.post("/v1/search", json=body).json()["items"]
    params = [c for c in fake.calls if c[1] == "/search"][-1][2]["params"]
    assert params["levels"] == "L0,L2" and params["mode"] == "keyword" and params["kbIds"] == "k1"
    # one list by score; the abstract keeps its summary type
    assert [(h["chunk_type"], h["anchor_id"]) for h in items] == [("text", "da_d1:paragraph:1"), ("summary", None)]
    assert items[0]["content"] == "全文命中" and items[0]["level"] == "L2" and items[0]["page_type"] == "fulltext"


def test_search_with_the_default_options_stays_a_fusion_search(client, fake):
    defaults = {"mode": "hybrid", "levels": {"L0": True, "L1": True, "L2": True}}
    client.post("/v1/search", json={"query": "出口压力", "options": defaults})
    params = [c for c in fake.calls if c[1] == "/search"][-1][2]["params"]
    assert "levels" not in params and "mode" not in params


# ---- actions and jobs --------------------------------------------------------------------------


def _job(client, job_id: str, **params) -> dict:
    return client.get(f"/v1/jobs/{job_id}", params=params).json()


def test_queue_actions_are_jobs_read_back_from_document_statuses(client, fake):
    job = client.post("/v1/kbs/da_k1/actions/process_pending", json={"params": {}}).json()["job"]
    assert re.fullmatch(r"da_process_pending\.k1\.\d+", job["id"])
    assert job["kb_id"] == "da_k1" and job["action"] == "process_pending" and not job["cancellable"]
    # d2 failed before: DA queued it again and has not picked it up
    assert job["status"] == "queued" and job["progress"] == 0
    fake.docs[1] = {**fake.docs[1], "status": "parsing", "updated_at": "2026-01-02T00:00:05Z"}
    running = _job(client, job["id"])
    assert running["status"] == "running"
    fake.docs[1] = {**fake.docs[1], "status": "ready", "updated_at": "2026-01-02T00:01:00Z"}
    done = _job(client, job["id"])
    assert done["status"] == "completed" and done["progress"] == 1.0 and done["finished_at"] == "2026-01-02T00:01:00Z"
    assert client.post(f"/v1/jobs/{job['id']}/cancel").json()["error"]["code"] == "not_cancellable"


def test_job_ids_hold_their_state_across_a_restart(fake, monkeypatch):
    with _client(fake, monkeypatch) as c:
        job = c.post("/v1/kbs/da_k1/actions/reindex").json()["job"]
        assert job["status"] == "queued"
    fake.docs[0] = {**fake.docs[0], "updated_at": "2026-01-02T00:03:00Z"}
    with _client(fake, monkeypatch) as fresh:
        again = _job(fresh, job["id"])
        assert again["status"] == "completed" and again["message"] == "已处理 1 篇"
        listed = fresh.get("/v1/kbs/da_k1/jobs").json()["items"]
        assert job["id"] not in [j["id"] for j in listed]


def _settle(client, job_id: str) -> dict:
    for _ in range(100):
        job = _job(client, job_id)
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_request_actions_run_in_the_background_with_a_report(client, fake):
    assert client.get("/v1/kbs/da_k1/documents").json()["items"][0]["summary"] == "泵房规程摘要"
    job = client.post("/v1/kbs/da_k1/actions/regenerate_summaries").json()["job"]
    done = _settle(client, job["id"])
    assert done["status"] == "completed" and done["message"] == "重新生成 1 篇" and done["has_report"]
    report = _job(client, job["id"], report="true")["report"]
    assert report["media_type"] == "application/json"
    assert report["content"]["items"] == [{"文档": "泵房规程.md", "结果": "已重新生成"},
                                          {"文档": "坏.pdf", "结果": "未处理完"}]
    # the summary cache does not hold on to the old abstract
    assert client.get("/v1/kbs/da_k1/documents").json()["items"][0]["summary"] == "补全后的摘要"
    rebuilt = _settle(client, client.post("/v1/kbs/da_k1/actions/rebuild_embeddings").json()["job"]["id"])
    assert rebuilt["status"] == "completed" and "emb" in rebuilt["message"]
    listed = client.get("/v1/kbs/da_k1/jobs").json()["items"]
    assert {j["action"] for j in listed} == {"regenerate_summaries", "rebuild_embeddings"}
    assert [j["id"] for j in client.get("/v1/kbs/da_k1/jobs", params={"status": "failed"}).json()["items"]] == []


def test_a_request_job_from_before_a_restart_reports_an_unknown_outcome(client):
    job = _job(client, "da_rebuild_embeddings.k1.1767225600000")
    assert job["status"] == "failed" and "重启" in job["message"]


def test_preprocess_runs_cancels_and_reports(client, fake):
    job = client.post("/v1/kbs/da_k1/actions/preprocess").json()["job"]
    assert job["status"] == "queued"
    fake.extras.pre_status = {"running": True, "startedAt": "2026-01-02T00:00:00Z", "currentActivity": "合并跨页表格"}
    running = _job(client, job["id"])
    assert running["status"] == "running" and running["cancellable"] and running["message"] == "合并跨页表格"
    # DA refuses a second run while one is going
    assert client.post("/v1/kbs/da_k1/actions/preprocess").status_code == 409
    cancelled = client.post(f"/v1/jobs/{job['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and not cancelled["cancellable"]
    assert [c for c in fake.calls if c[1] == "/kbs/k1/preprocess/cancel"]
    # a later run that finished leaves a report
    fake.extras.pre_status = {"running": False, "lastStatus": "success", "lastPreprocessedAt": "2026-01-03T00:00:00Z",
                              "lastReport": {"content": "# 预处理报告"}}
    fake.extras.report_md = "# 预处理报告\n\n合并了 2 张表"
    fake.extras.now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    second = client.post("/v1/kbs/da_k1/actions/preprocess").json()["job"]
    assert second["status"] == "completed" and second["has_report"]
    assert _job(client, second["id"], report="true")["report"] == {"media_type": "text/markdown",
                                                                    "content": "# 预处理报告\n\n合并了 2 张表"}


def test_jobs_list_shows_preprocess_runs_started_in_da_itself(client, fake):
    fake.extras.pre_status = {"running": True, "startedAt": "2026-01-02T03:00:00Z", "currentActivity": None}
    (job,) = client.get("/v1/kbs/da_k1/jobs").json()["items"]
    assert job["action"] == "preprocess" and job["status"] == "running" and job["started_at"] == "2026-01-02T03:00:00Z"


def test_undeclared_actions_and_scopes_are_unsupported(client, fake):
    for path in ("/v1/kbs/da_k1/actions/regenerate_summary", "/v1/documents/da_d1/actions/reindex",
                 "/v1/kbs/da_k1/actions/drop_all", "/v1/kbs/da_k1/actions/Bad-Id"):
        r = client.post(path, json={})
        assert r.status_code == 501 and r.json()["error"]["code"] == "unsupported", path
    assert client.post("/v1/documents/da_d1/actions/quality_audit", json={"params": "x"}).status_code == 400


def test_document_actions_answer_with_their_result(client, fake):
    r = client.post("/v1/documents/da_d1/actions/regenerate_summary", json={"params": {}}).json()
    assert r == {"result": "重新生成的摘要"}
    audit = client.post("/v1/documents/da_d1/actions/quality_audit", json={"params": {"re_extract": True}}).json()
    assert audit["result"]["评分"] == 62 and audit["result"]["已重新提取"] is True
    assert [c for c in fake.calls if c[1] == "/kbs/k1/documents/d1/quality-audit"][-1][2]["params"] == {"reExtract": "true"}


def test_job_ids_never_reach_da_unchecked(client, fake):
    for job_id in ("da_nope", "da_reindex.k1.abc", "nope_reindex.k1.1", "da_drop.k1.1", "da_reindex..1"):
        assert client.get(f"/v1/jobs/{job_id}").status_code == 404, job_id
    assert client.get("/v1/jobs/da_reindex.k1%2Fx.1").status_code == 404


# ---- views ---------------------------------------------------------------------------------------


def test_document_views(client, fake):
    r = client.get("/v1/documents/da_d1/views/overview")
    assert r.status_code == 200 and r.text == OVERVIEW and r.headers["content-type"].startswith("text/markdown")
    assert client.get("/v1/documents/da_d1/views/structure_map").status_code == 404
    fake.extras.structure_map = [{"id": "s1", "title": "泵房规程", "anchors": [
        {"id": "d1:heading:0", "type": "heading", "preview": "泵房规程", "lineStart": 0}]}]
    rows = client.get("/v1/documents/da_d1/views/structure_map").json()["items"]
    assert rows == [{"章节": "泵房规程", "类型": "标题", "内容": "泵房规程", "锚点": "da_d1:heading:0"}]
    assert client.get("/v1/documents/da_d1/views/media").status_code == 404
    fake.extras.media = {"type": "audio", "audio": {"duration": 30, "speakers": ["甲"],
                                                    "turns": [{"speaker": "甲", "text": "压力正常", "start": 1, "end": 3}]}}
    media = client.get("/v1/documents/da_d1/views/media").json()
    assert media["类型"] == "音频" and media["对话"] == [{"开始": 1, "结束": 3, "说话人": "甲", "内容": "压力正常"}]


def test_overview_view_is_empty_for_structured_documents(monkeypatch):
    with _client(FakeDa(structured=True), monkeypatch) as c:
        assert c.get("/v1/documents/da_d1/views/overview").status_code == 404


def test_kb_views(client, fake):
    assert client.get("/v1/kbs/da_k1/views/quality_report").status_code == 404
    fake.extras.audited = True
    rows = client.get("/v1/kbs/da_k1/views/quality_report").json()["items"]
    assert [(r["文档"], r["评分"], r["问题"]) for r in rows] == [("泵房规程.md", 62, "表格缺表头")]
    assert client.get("/v1/kbs/da_k1/views/timeline").status_code == 404
    fake.extras.events = [{"date": "2026-01-01", "title": "联锁停机", "description": "压力低于 0.3 MPa",
                           "sourcePageId": "p-ov", "sourceTitle": "Overview: d1"}]
    events = client.get("/v1/kbs/da_k1/views/timeline").json()["events"]
    assert events == [{"date": "2026-01-01", "title": "联锁停机", "description": "压力低于 0.3 MPa", "source": "泵房规程"}]
    assert client.get("/v1/kbs/da_k1/views/overview").status_code == 501


# ---- backend settings -------------------------------------------------------------------------------


def test_settings_map_da_groups_and_mask_keys(client):
    values = client.get("/v1/settings").json()
    assert values["models"]["embedding"] == "emb" and values["models"]["vlm"] == ""
    assert values["providers"]["main-llm"]["api_key"] == "******" and values["providers"]["emb"]["api_key"] is None
    assert values["providers"]["emb"]["dimension"] == 1024 and "dimension" not in values["providers"]["main-llm"]
    assert values["processing"] == {"auto_process": True, "parsing_mode": "quality"}
    assert values["pipelines"] == {"pdf": "mineru"}
    assert values["vlm"]["timeout"] == 300 and values["mineru"]["api_url"] == "https://mineru.example.com"
    assert "layout_model" not in values["docling"]


def test_settings_put_writes_only_what_changed(client, fake):
    current = client.get("/v1/settings").json()
    patch = {**current, "models": {**current["models"], "summarizer": "emb"},
             "providers": {"main-llm": {**current["providers"]["main-llm"], "model": "qwen-max"}},
             "processing": {"auto_process": False, "parsing_mode": "quality"},
             "pipelines": {"pdf": "docling"}, "docling": {**current["docling"], "parallelism": 8},
             "vlm": {**current["vlm"], "timeout": 60}}
    after = client.put("/v1/settings", json=patch).json()
    writes = dict(fake.extras.writes)
    assert set(writes) == {"/providers/main-llm", "/defaults", "/key/auto_process", "/pipeline-strategies",
                           "/docling-config", "/vlm-config"}
    # an unchanged key goes back masked, so DA keeps it
    assert writes["/providers/main-llm"]["apiKey"] == "••••••••abcd" and writes["/providers/main-llm"]["model"] == "qwen-max"
    assert writes["/defaults"] == {"summarizer": "emb"} and writes["/key/auto_process"] == {"value": False}
    assert writes["/docling-config"] == {"parallelism": 8} and writes["/vlm-config"] == {"timeoutMs": 60000}
    assert [p["pipeline"] for p in writes["/pipeline-strategies"][0]["pipelines"]] == ["docling", "mineru"]
    assert after["pipelines"] == {"pdf": "docling"} and after["processing"]["auto_process"] is False
    assert after["providers"]["main-llm"]["api_key"] == "******"


def test_settings_secrets_can_be_replaced_or_cleared(client, fake):
    new_key = secrets.token_hex(8)
    client.put("/v1/settings", json={"providers": {"emb": {"api_key": new_key}}})
    assert fake.extras.writes[-1][1]["apiKey"] == new_key
    assert client.get("/v1/settings").json()["providers"]["emb"]["api_key"] == "******"
    client.put("/v1/settings", json={"providers": {"emb": {"api_key": ""}}})
    assert fake.extras.writes[-1][1]["apiKey"] == ""
    before = len(fake.extras.writes)
    client.put("/v1/settings", json={"providers": {"emb": {"api_key": "******"}}, "models": {"main": "main-llm"}})
    assert len(fake.extras.writes) == before


def test_settings_put_refuses_bad_values(client, fake):
    for patch in ({"nope": {}}, {"docling": {"table_mode": "slow"}}, {"docling": {"parallelism": "8"}},
                  {"docling": {"parallelism": 99}}, {"providers": {"ghost": {"model": "x"}}},
                  {"models": {"main": "ghost"}}, {"processing": {"auto_process": "no"}}, ["x"]):
        r = client.put("/v1/settings", json=patch)
        assert r.status_code == 400, patch
    assert fake.extras.writes == []


# ---- evidence ----------------------------------------------------------------------------------------


def test_evidence_from_a_da_anchor_with_page_and_box(client, fake):
    fake.extras.anchors["d1:paragraph:1"] = {
        "anchor": {"id": "d1:paragraph:1", "doc_id": "d1", "kb_id": "k1", "element_type": "paragraph",
                   "page_number": 2, "line_start": 2, "content_preview": "每班记录出口压力。",
                   "metadata": {"source": "mineru"}},
        "rawContext": {"type": "text", "bbox": [72, 140.5, 520, 188]},
    }
    ev = client.get("/v1/evidence/da_d1:paragraph:1").json()
    assert ev["doc_id"] == "da_d1" and ev["kb_id"] == "da_k1" and ev["title"] == "泵房规程"
    assert ev["text"] == "每班记录出口压力。" and ev["section_title"] == "泵房规程"
    # DA's line of a paragraph is not trusted: the line comes from the text
    assert ev["locators"] == [{"page": 3, "bbox": [72.0, 140.5, 520.0, 188.0], "line_start": 5, "line_end": 5}]
    assert "联锁停机" in ev["context_before"]


def test_evidence_of_a_media_turn_has_its_time(client, fake):
    fake.extras.anchors["d1:turn:0"] = {"anchor": {
        "id": "d1:turn:0", "doc_id": "d1", "kb_id": "k1", "element_type": "turn", "page_number": 12,
        "content_preview": "每班记录出口压力。", "section_title": "说话人甲",
        "metadata": {"startTime": 12.5, "endTime": 20}}}
    ev = client.get("/v1/evidence/da_d1:turn:0").json()
    assert ev["locators"] == [{"time_start": 12.5, "time_end": 20, "line_start": 5, "line_end": 5}]
    assert ev["section_title"] == "说话人甲"


def test_evidence_falls_back_to_the_structure_anchors(client, fake):
    anchors = client.get("/v1/documents/da_d1/structure").json()["anchors"]
    paragraph = next(a for a in anchors if a["type"] == "paragraph")
    ev = client.get(f"/v1/evidence/{paragraph['id']}").json()
    assert ev["text"].startswith(paragraph["preview"][:10]) and ev["locators"][0]["line_start"] == paragraph["line_start"]


def test_evidence_of_unknown_or_foreign_anchors_is_not_found(client, fake):
    fake.extras.anchors["d1:paragraph:9"] = {"anchor": {"id": "d1:paragraph:9", "doc_id": "d7", "kb_id": "k2"}}
    for anchor in ("da_d1:paragraph:9", "da_d1:paragraph:99", "da_nope:paragraph:0", "x_d1:paragraph:0", "da_d1"):
        assert client.get(f"/v1/evidence/{anchor}").status_code == 404, anchor


# ---- export -----------------------------------------------------------------------------------------


def test_export_relays_da_and_names_the_file(monkeypatch):
    with _client(FakeDa(structured=True), monkeypatch) as c:
        r = c.get("/v1/documents/da_d1/export/md")
        assert r.status_code == 200 and r.text == "## 泵房规程\n\n结构化正文"
        assert "filename*=UTF-8''%E6%B3%B5%E6%88%BF%E8%A7%84%E7%A8%8B.md" in r.headers["content-disposition"]
        assert c.get("/v1/documents/da_d1/export/manifest").json()["docId"] == "d1"


def test_export_falls_back_to_l1_for_markdown_only(client):
    assert client.get("/v1/documents/da_d1/export/md").text == FULLTEXT
    r = client.get("/v1/documents/da_d1/export/json")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert client.get("/v1/documents/da_d1/export/pdf").status_code == 501


# ---- wiki --------------------------------------------------------------------------------------------


def test_wiki_lists_document_summaries_and_reports(client):
    status = client.get("/v1/kbs/da_k1/wiki/status").json()
    assert status["enabled"] and status["pages"] == {"summary": 1, "concept": 1}
    assert status["documents"] == {"completed": 1} and status["errors"] == []
    pages = client.get("/v1/kbs/da_k1/wiki/pages").json()
    assert pages["total"] == 2
    assert [(p["slug"], p["page_type"], p["title"], p["summary"]) for p in pages["items"]] == [
        ("summary/d1", "summary", "泵房规程", "泵房规程摘要"), ("report/r1", "concept", "泵房分析报告", "")]
    assert client.get("/v1/kbs/da_k1/wiki/pages", params={"type": "concept"}).json()["total"] == 1
    assert client.get("/v1/kbs/da_k1/wiki/pages", params={"q": "摘要"}).json()["items"][0]["slug"] == "summary/d1"


def test_wiki_pages_read_overview_and_reports(client):
    page = client.get("/v1/kbs/da_k1/wiki/page", params={"slug": "summary/d1"}).json()
    assert page["content"] == OVERVIEW and page["summary"] == "泵房规程摘要"
    assert page["sources"] == [{"id": "da_d1", "title": "泵房规程", "file_name": "泵房规程.md"}]
    assert page["source_doc_ids"] == ["da_d1"] and page["revisions"] == [] and page["aliases"] == []
    report = client.get("/v1/kbs/da_k1/wiki/page", params={"slug": "report/r1"}).json()
    assert report["page_type"] == "concept" and report["summary"] == "出口压力偏低的原因分析。"
    assert report["source_doc_ids"] == ["da_d1"]
    for slug in ("report/nope", "summary/d2", "entity/r1", "summary/../x", "index/x", "report/a/b"):
        assert client.get("/v1/kbs/da_k1/wiki/page", params={"slug": slug}).status_code == 404, slug


def test_platform_entities_and_links_come_through_the_wiki(monkeypatch):
    fake = FakeDa(platform=True)
    x = fake.extras
    x.graph_nodes += [{"id": "p-l1", "label": "Overview: d1", "type": "document"},
                      {"id": "e1", "label": "循环泵", "type": "entity"}, {"id": "e2", "label": "冷却塔", "type": "entity"}]
    x.graph_edges = [{"source": "p-l1", "target": "e1", "type": "entity_ref"},
                     {"source": "e1", "target": "e2", "type": "forward"}]
    x.wiki_pages["p-l1"]["links"] = [{"sourcePageId": "p-l1", "targetPageId": "e1", "linkType": "entity_ref"}]
    x.wiki_pages["e1"] = {"id": "e1", "pageType": "entity", "title": "循环泵", "content": "# 循环泵\n\n类型: 设备", "links": [
        {"sourcePageId": "p-l1", "targetPageId": "e1", "linkType": "entity_ref"},
        {"sourcePageId": "e1", "targetPageId": "e2", "linkType": "forward"}]}
    with _client(fake, monkeypatch) as c:
        assert c.get("/v1/kbs/da_k1/wiki/status").json()["pages"] == {"summary": 1, "entity": 2, "concept": 1}
        entities = c.get("/v1/kbs/da_k1/wiki/pages", params={"type": "entity"}).json()
        assert [p["slug"] for p in entities["items"]] == ["entity/e2", "entity/e1"]
        page = c.get("/v1/kbs/da_k1/wiki/page", params={"slug": "entity/e1"}).json()
        assert page["page_type"] == "entity" and page["summary"] == "类型: 设备"
        assert [r["slug"] for r in page["linked_from"]] == ["summary/d1"] and [r["slug"] for r in page["links_to"]] == ["entity/e2"]
        assert page["source_doc_ids"] == ["da_d1"]
        doc_page = c.get("/v1/kbs/da_k1/wiki/page", params={"slug": "summary/d1"}).json()
        assert [r["slug"] for r in doc_page["links_to"]] == ["entity/e1"]
        graph = c.get("/v1/kbs/da_k1/wiki/graph").json()
        assert {(e["source"], e["target"]) for e in graph["edges"]} == {("summary/d1", "entity/e1"), ("entity/e1", "entity/e2")}
        assert {n["slug"]: n["degree"] for n in graph["nodes"]}["entity/e1"] == 2
        centered = c.get("/v1/kbs/da_k1/wiki/graph", params={"center": "entity/e2"}).json()
        assert {n["slug"] for n in centered["nodes"]} == {"entity/e1", "entity/e2"}
        # the graph is walked once for the wiki tab, not once per request
        assert len([call for call in fake.calls if call[1] == "/api/reports/graph/k1"]) == 1
        hits = c.post("/v1/search", json={"query": "出口压力"}).json()["items"]
        assert any(h["chunk_type"] == "wiki_page" for h in hits)


def test_wiki_search_hits_are_not_merged_with_each_other():
    from da_kb_adapter.mapping import distinct_hits

    hits = [{"doc_id": None, "chunk_id": "da_e1", "anchor_id": None, "content": "循环泵"},
            {"doc_id": None, "chunk_id": "da_e2", "anchor_id": None, "content": "冷却塔"},
            {"doc_id": "da_d1", "chunk_id": "da_p", "anchor_id": None, "content": "正文"}]
    assert [h["chunk_id"] for h in distinct_hits(hits)] == ["da_e1", "da_e2", "da_p"]


# ---- what DA keeps of a parsed PDF: anchors, docling JSON, DocTags ------------------------------------

STRUCTURED = ("## 示例换热站运行规程\n\n第一章 适用范围\n\n本规程适用于一号机组。\n\n"
              "- 一次网供水温度控制在 95 摄氏度以下。\n\n循环水泵轴承温度超过 75 摄氏度时应立即停泵检查。\n")
# DA's anchors of STRUCTURED: every paragraph claims line 2 (DA's markdown anchors share the first one's line)
DA_ANCHORS = [
    {"id": "d1:paragraph:0", "type": "paragraph", "preview": "第一章 适用范围", "lineStart": 2},
    {"id": "d1:heading:0", "type": "heading", "preview": "示例换热站运行规程", "lineStart": 0},
    {"id": "d1:paragraph:1", "type": "paragraph", "preview": "本规程适用于一号机组。", "lineStart": 2},
    {"id": "d1:paragraph:2", "type": "paragraph", "preview": "- 一次网供水温度控制在 95 摄氏度以下。", "lineStart": 2},
    {"id": "d1:paragraph:3", "type": "paragraph", "preview": "循环水泵轴承温度超过 75 摄氏度时应立即停泵检查。", "lineStart": 2},
]
DOCLING = {
    "schema_name": "DoclingDocument", "pages": {"1": {"size": {"width": 595.2756, "height": 841.8898}}},
    "texts": [
        {"label": "section_header", "text": "示例换热站运行规程",
         "prov": [{"page_no": 1, "bbox": {"l": 72, "t": 774.2648, "r": 378, "b": 759.8648, "coord_origin": "BOTTOMLEFT"}}]},
        {"label": "text", "text": "循环水泵轴承温度超过 75 摄氏度时应立即停泵检查。",
         "prov": [{"page_no": 1, "bbox": {"l": 72, "t": 620.1398, "r": 360, "b": 610.5398, "coord_origin": "BOTTOMLEFT"}}]},
    ],
}


def _structured(monkeypatch, fake: FakeDa | None = None):
    fake = fake or FakeDa(structured=True)
    fake.extras.l1_text = STRUCTURED
    fake.extras.structure_map = [{"id": "p-md", "title": "示例换热站运行规程", "anchors": DA_ANCHORS}]
    fake.extras.pages["p-md"] = {"pageType": "structure_md", "docId": "d1", "content": STRUCTURED}
    return fake, _client(fake, monkeypatch)


def test_keyword_hits_get_their_text_and_the_anchor_at_it(monkeypatch):
    fake, client = _structured(monkeypatch)
    # DA's full-text search keeps no text, and then names the page's first anchor
    fake.extras.leveled = {"L1": [{"pageId": "p-md", "kbId": "k1", "docId": "d1", "title": "示例", "snippet": "",
                                   "score": 0.7, "anchorId": "d1:paragraph:0"}]}
    with client as c:
        body = {"query": "循环水泵轴承温度", "options": {"mode": "keyword"}}
        (hit,) = c.post("/v1/search", json=body).json()["items"]
    assert "循环水泵轴承温度超过 75 摄氏度" in hit["content"]
    assert hit["anchor_id"] == "da_d1:paragraph:3"


def test_full_text_hits_get_the_anchor_at_their_passage(monkeypatch):
    fake, client = _structured(monkeypatch)
    # DA's default (hybrid) search hits the full-text page (L2): text, no anchor
    fake.extras.pages["p-ft"] = {"pageType": "fulltext", "docId": "d1", "content": STRUCTURED}
    fake.extras.leveled = {"L2": [{"pageId": "p-ft", "kbId": "k1", "docId": "d1", "title": "示例", "score": 0.03,
                                   "snippet": "...一号机组。\n\n- 一次网供水温度控制在 95 摄氏度以下。\n\n循环水泵轴承温度超过"}]}
    with client as c:
        (hit,) = c.post("/v1/search", json={"query": "循环水泵轴承温度"}).json()["items"]
        assert hit["anchor_id"] == "da_d1:paragraph:3"
        # a passage the page does not hold names no anchor
        fake.extras.leveled["L2"][0]["snippet"] = "冷却塔风机"
        (hit,) = c.post("/v1/search", json={"query": "冷却塔"}).json()["items"]
        assert hit["anchor_id"] is None


def test_a_keyword_hit_whose_passage_has_no_anchor_names_none(monkeypatch):
    fake, client = _structured(monkeypatch)
    fake.extras.structure_map = []
    fake.extras.leveled = {"L1": [{"pageId": "p-md", "kbId": "k1", "docId": "d1", "title": "示例", "snippet": "",
                                   "score": 0.7, "anchorId": "d1:paragraph:0"}]}
    with client as c:
        (hit,) = c.post("/v1/search", json={"query": "轴承", "options": {"mode": "keyword"}}).json()["items"]
    assert "轴承" in hit["content"] and hit["anchor_id"] is None


def test_doctags_never_reach_hit_text(client, fake):
    fake.extras.leveled = {"L1": [{"pageId": "p-dt", "kbId": "k1", "docId": "d1", "title": "示例", "score": 0.6,
                                   "snippet": "<doctag><text><loc_119><loc_302><loc_124><loc_310>板式换热器"
                                              "二次侧压力保持在 0.6 至 0.8 兆帕之间。</text>"}]}
    (hit,) = client.post("/v1/search", json={"query": "板式换热器", "options": {"mode": "vector"}}).json()["items"]
    assert hit["content"] == "板式换热器二次侧压力保持在 0.6 至 0.8 兆帕之间。"


def test_tags_cut_by_the_snippet_are_dropped(client, fake):
    fake.extras.leveled = {"L1": [{"pageId": "p-dt", "kbId": "k1", "docId": "d1", "title": "示例", "score": 0.6,
                                   "snippet": "...119><loc_302>板式换热器二次侧压力。\n循环水泵<loc_1"}]}
    (hit,) = client.post("/v1/search", json={"query": "板式换热器", "options": {"mode": "vector"}}).json()["items"]
    assert hit["content"] == "...板式换热器二次侧压力。\n循环水泵"


def test_strip_doctags_keeps_tables_readable_and_leaves_html_alone():
    from da_kb_adapter.mapping import strip_doctags

    tags = "<otsl><loc_1><loc_2><loc_3><loc_4><ched>项目<ched>上限<nl><fcel>压力<fcel>0.8<nl></otsl>"
    assert strip_doctags(tags) == "项目 | 上限\n压力 | 0.8"
    html = "<table><tr><td>压力</td></tr></table>"
    assert strip_doctags(html) == html
    assert strip_doctags("") == ""


def test_snippets_find_the_longest_piece_of_the_query():
    from da_kb_adapter.mapping import snippet_around

    text = "前言。" * 60 + "水泵轴承温度超过 75 度时停泵。" + "后记。" * 200
    snippet, at = snippet_around(text, "循环水泵轴承温度")
    assert at == text.index("水泵轴承温度") and "水泵轴承温度超过" in snippet
    assert snippet_around("无关内容", "轴承") == ("无关内容", None)


def test_evidence_gets_page_and_box_from_the_docling_json(monkeypatch):
    fake, client = _structured(monkeypatch)
    fake.extras.raw = DOCLING
    # DA's anchors made from docling's markdown have no page and no JSON path
    fake.extras.anchors["d1:paragraph:3"] = {"anchor": {
        "id": "d1:paragraph:3", "doc_id": "d1", "kb_id": "k1", "element_type": "paragraph", "page_number": None,
        "raw_json_path": "", "line_start": 2, "content_preview": "循环水泵轴承温度超过 75 摄氏度时应立即停泵检查。", "metadata": {}}}
    with client as c:
        ev = c.get("/v1/evidence/da_d1:paragraph:3").json()
        # top-left origin: y = page height - docling's bottom-left y
        assert ev["locators"] == [{"page": 1, "bbox": [72.0, 221.75, 360.0, 231.35], "line_start": 9, "line_end": 9}]
        c.get("/v1/evidence/da_d1:paragraph:3")
    # the JSON is read once per document version
    assert fake.extras.raw_reads == 1


def test_evidence_without_a_matching_item_keeps_its_lines(monkeypatch):
    fake, client = _structured(monkeypatch)
    fake.extras.raw = {**DOCLING, "texts": DOCLING["texts"][:1]}
    with client as c:
        anchors = c.get("/v1/documents/da_d1/structure").json()["anchors"]
        list_anchor = next(a for a in anchors if a["id"] == "da_d1:paragraph:2")
        ev = c.get(f"/v1/evidence/{list_anchor['id']}")
    assert ev.status_code == 200 and ev.json()["locators"] == [{"line_start": 7, "line_end": 7}]


def test_parser_items_from_mineru_and_docling():
    from da_kb_adapter.mapping import match_item, raw_items

    mineru = {"pdf_info": [{"para_blocks": []}, {"para_blocks": [
        {"type": "text", "bbox": [50, 60, 300, 80], "lines": [{"spans": [{"content": "补水量异常"}, {"content": "增大"}]}]}]}]}
    assert match_item(raw_items(mineru), "补水量异常增大时排查") == {"page": 2, "bbox": [50.0, 60.0, 300.0, 80.0]}
    items = raw_items(DOCLING)
    # a passage holding several items gets the box around them
    merged = match_item(items, "示例换热站运行规程\n循环水泵轴承温度超过 75 摄氏度时应立即停泵检查。")
    assert merged == {"page": 1, "bbox": [72.0, 67.62, 378.0, 231.35]}
    assert match_item(items, "完全无关") is None and raw_items(None) == []


def test_structure_windows_name_das_own_anchors(monkeypatch):
    fake, client = _structured(monkeypatch)
    with client as c:
        anchors = c.get("/v1/documents/da_d1/structure").json()["anchors"]
    assert [(a["id"], a["line_start"]) for a in anchors] == [
        ("da_d1:heading:0", 1), ("da_d1:paragraph:0", 3), ("da_d1:paragraph:1", 5), ("da_d1:paragraph:2", 7),
        ("da_d1:paragraph:3", 9)]
    assert anchors[4]["section_title"] == "示例换热站运行规程" and anchors[4]["preview"].startswith("循环水泵轴承")


def test_chunks_carry_the_anchor_that_starts_in_them(monkeypatch):
    fake, client = _structured(monkeypatch)
    with client as c:
        chunk = c.get("/v1/documents/da_d1/chunks").json()["items"][0]
    assert chunk["anchor_id"] == "da_d1:heading:0"


def test_chunk_anchors_follow_the_text_not_das_line_numbers():
    from da_kb_adapter.mapping import anchor_for, locate_anchors, split_chunks

    located = locate_anchors(STRUCTURED, DA_ANCHORS)
    assert [a for _, a in located] == ["d1:heading:0", "d1:paragraph:0", "d1:paragraph:1", "d1:paragraph:2",
                                       "d1:paragraph:3"]
    pieces = split_chunks(STRUCTURED, size=40)
    found = [anchor_for(located, start, start + len(text)) for _, start, text in pieces]
    assert found[0] == "d1:heading:0" and found[-1] == "d1:paragraph:3"
    # repeated text is placed in order
    twice = "甲\n\n重复段落\n\n乙\n\n重复段落\n"
    again = locate_anchors(twice, [{"id": "x:paragraph:1", "type": "paragraph", "preview": "重复段落"},
                                   {"id": "x:paragraph:3", "type": "paragraph", "preview": "重复段落"}])
    assert again == [(3, "x:paragraph:1"), (12, "x:paragraph:3")]


def test_chunks_of_plain_documents_carry_structure_anchors(client):
    chunk = client.get("/v1/documents/da_d1/chunks").json()["items"][0]
    anchors = client.get("/v1/documents/da_d1/structure").json()["anchors"]
    assert chunk["anchor_id"] == anchors[0]["id"]
    assert client.get(f"/v1/evidence/{chunk['anchor_id']}").status_code == 200


# ---- file types: what DA can parse --------------------------------------------------------------------


def test_file_types_follow_das_parsers(fake, monkeypatch):
    with _client(fake, monkeypatch) as c:
        types = c.get("/v1/capabilities").json()["file_types"]
    assert "pdf" not in types and "png" not in types and "mp3" not in types and "md" in types
    fake.extras.mineru_status = {"enabled": True, "connected": True}
    fake.extras.settings["/providers"]["defaults"].update(vlm="main-llm", audio_transcribe="emb")
    monkeypatch.setenv("DA_KB_EXTRA_FILE_TYPES", "doc, .PPT")
    with _client(fake, monkeypatch) as c:
        types = c.get("/v1/capabilities").json()["file_types"]
    assert {"pdf", "docx", "pptx", "png", "jpg", "mp3", "mp4", "doc", "ppt"} <= set(types)
    assert len(types) == len(set(types))


def test_a_role_on_a_disabled_service_opens_nothing():
    from da_kb_adapter.offering import role_parsers

    settings = {"defaults": {"vlm": "v", "audio_transcribe": "gone"}, "providers": [{"id": "v", "enabled": False}]}
    assert role_parsers(settings) == set() and role_parsers(None) == set()


def test_platform_file_types_are_the_base_ones_plus_the_override(monkeypatch):
    monkeypatch.setenv("DA_KB_EXTRA_FILE_TYPES", "pdf")
    with _platform(monkeypatch) as c:
        types = c.get("/v1/capabilities").json()["file_types"]
    assert "pdf" in types and "png" not in types


def test_evidence_does_not_mix_block_numbering_with_das_anchors(monkeypatch):
    fake, client = _structured(monkeypatch)
    with client as c:
        # DA numbers the list item paragraph:2; the block numbering would call the next paragraph that
        assert "一次网供水温度" in c.get("/v1/evidence/da_d1:paragraph:2").json()["text"]
        # a block anchor DA does not have is not answered with another block
        assert c.get("/v1/evidence/da_d1:list:0").status_code == 404
