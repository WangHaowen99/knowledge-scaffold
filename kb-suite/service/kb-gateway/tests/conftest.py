from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from kb_gateway.api.app import create_app
from kb_gateway.auth import KeyRing, parse_keys
from kb_gateway.config import parse_backends
from kb_gateway.deps import State
from kb_gateway.registry import Registry

KEY_ENV = {"PLUGIN": "plugin-key", "MCP": "mcp-key", "DA": "da-key"}
KEYS = "plugin=PLUGIN:manage,ingest,retrieve;mcp=MCP:retrieve;da=DA:retrieve,reprocess"

L1_KBS = "# 设备手册\n\n冷却塔风机电流上限为 30A。\n\n## 维护\n\n每月检查一次皮带。\n"
L1_WK = "# 离心泵\n\n循环泵出口压力低于 0.3 MPa 时联锁停机。\n\n## 巡检\n\n每班记录出口压力。\n"


class FakeProvider:
    """A knowledge-provider/v1 backend held in memory (synthetic data)."""

    def __init__(self, provider_type: str, prefix: str, features: list[str], *, role: str = "base") -> None:
        p = prefix
        self.type = provider_type
        self.features = features
        self.role = role
        self.calls: list[tuple[str, str, dict]] = []
        self.fail_search = False
        self.down = False
        self.kbs = {f"{p}kb1": {"id": f"{p}kb1", "name": f"{provider_type} 库", "description": "", "config": {},
                                "stats": {"documents": 1, "completed": 1, "processing": 0, "failed": 0}}}
        l1 = L1_KBS if not prefix else L1_WK
        self.docs = {f"{p}d1": {"id": f"{p}d1", "kb_id": f"{p}kb1", "title": f"{provider_type} 文档", "file_name": "a.md",
                                "file_type": "md", "folder_path": "", "parse_status": "completed", "summary": "摘要",
                                "chunk_count": 2, "l1_chars": len(l1), "_l1": l1}}
        self.uploads: list[dict] = []
        self.prefix = prefix
        self.parts: dict[int, bytes] = {}

    def _json(self, data, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    def _doc(self, doc: dict) -> dict:
        return {k: v for k, v in doc.items() if not k.startswith("_")}

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503, text="down")
        path = request.url.path
        body = {}
        if request.headers.get("content-type", "").startswith("application/json") and request.content:
            body = json.loads(request.content)
        self.calls.append((request.method, path, {"body": body, "headers": dict(request.headers),
                                                   "params": dict(request.url.params)}))
        doc_match = re.match(r"^/v1/documents/([^/]+)(/.*)?$", path)
        if path == "/v1/capabilities":
            return self._json({"contract": "knowledge-provider/v1", "role": self.role,
                               "provider": {"type": self.type, "version": "1"}, "features": self.features,
                               "limits": {"max_upload_mb": 5}, "config_schema": None})
        if path == "/v1/kbs" and request.method == "GET":
            return self._json({"items": list(self.kbs.values())})
        if path == "/v1/kbs" and request.method == "POST":
            kb = {"id": f"{next(iter(self.kbs))[:-3]}kb9", "name": body["name"], "description": "", "config": {}, "stats": {}}
            return self._json(kb, 201)
        if m := re.match(r"^/v1/kbs/([^/]+)$", path):
            return self._json(self.kbs[m.group(1)]) if m.group(1) in self.kbs else self._json({"error": {"code": "not_found", "message": "no"}}, 404)
        if m := re.match(r"^/v1/kbs/([^/]+)/documents$", path):
            if request.method == "POST":
                self.uploads.append({"kb": m.group(1), "content": request.content})
                return self._json({"id": "new", "kb_id": m.group(1), "parse_status": "pending"}, 201)
            items = [self._doc(d) for d in self.docs.values() if d["kb_id"] == m.group(1)]
            return self._json({"items": items, "total": len(items)})
        if doc_match:
            doc = self.docs.get(doc_match.group(1))
            rest = doc_match.group(2) or ""
            if doc is None:
                return self._json({"error": {"code": "not_found", "message": "no doc"}}, 404)
            if rest == "":
                return self._json(self._doc(doc))
            if rest == "/content":
                level = request.url.params.get("level")
                return httpx.Response(200, text=doc["summary"] if level == "L0" else doc["_l1"])
            if rest == "/chunks":
                items = [{"id": f"{doc['id']}-c{i}", "seq": i, "chunk_type": "text", "content": part,
                          "context_header": "", "line_start": 1, "anchor_id": None, "source_locators": [{"page": 1}]}
                         for i, part in enumerate(doc["_l1"].split("\n\n"))]
                return self._json({"items": items, "total": len(items), "offset": 0})
            if rest == "/file":
                async def body_stream():
                    yield b"%PDF-"
                    yield b"synthetic"

                return httpx.Response(200, content=body_stream(),
                                      headers={"content-type": "application/pdf",
                                               "content-disposition": 'attachment; filename="a.pdf"'})
            if rest == "/reprocess":
                return self._json({"id": doc["id"], "parse_status": "pending"})
            if rest == "/structure":
                l1, heading = doc["_l1"], request.url.params.get("heading")
                content = l1
                if heading:
                    start = l1.find(f"## {heading}")
                    if start == -1:
                        return self._json({"error": {"code": "not_found", "message": "no section"}}, 404)
                    content = l1[start:]
                limit = int(request.url.params.get("limit", 30000))
                anchors = [] if limit == 0 else [{"id": f"{doc['id']}:paragraph:0", "type": "paragraph", "line_start": 3,
                                                  "preview": content[:20], "section_title": None}]
                return self._json({"content": content[:limit], "offset": 0, "total_chars": len(l1), "has_more": False,
                                   "outline": [line.lstrip("# ") for line in l1.split("\n") if line.startswith("#")],
                                   "anchors": anchors})
        if path == "/v1/grep":
            matches = []
            for d in self.docs.values():
                lines = [{"line_number": i, "text": t} for i, t in enumerate(d["_l1"].split("\n"), 1) if body["pattern"] in t]
                if lines:
                    matches.append({"doc_id": d["id"], "kb_id": d["kb_id"], "title": d["title"], "file_name": d["file_name"],
                                    "lines": lines})
            return self._json({"pattern": body["pattern"], "search_mode": "regex", "total_documents": len(matches),
                               "total_matches": sum(len(m["lines"]) for m in matches), "has_more": False, "matches": matches})
        if path == "/v1/uploads":
            self.uploads.append({"begin": body})
            return self._json({"upload_id": f"{self.prefix}up1", "part_size": 1024, "part_count": 1}, 201)
        if m := re.match(r"^/v1/uploads/([^/]+)/parts/(\d+)$", path):
            self.parts[int(m.group(2))] = request.content
            return self._json({"upload_id": m.group(1), "index": int(m.group(2))})
        if re.match(r"^/v1/uploads/[^/]+/complete$", path):
            return self._json({"id": f"{self.prefix}new", "kb_id": self.uploads[-1]["begin"]["kb_id"], "parse_status": "pending"}, 201)
        if path == "/v1/search":
            if self.fail_search:
                return self._json({"error": {"code": "boom", "message": "检索挂了"}}, 500)
            doc = next(iter(self.docs.values()))
            hits = [{"chunk_id": f"{doc['id']}-c1", "doc_id": doc["id"], "kb_id": doc["kb_id"], "seq": 1,
                     "chunk_type": "text", "title": doc["title"], "content": doc["_l1"].split("\n\n")[1],
                     "context_header": "", "score": 0.9, "relevance": 0.8, "line_start": 3, "anchor_id": None,
                     "locators": [{"page": 2}]}]
            if body.get("include_wiki", True) and "wiki.read" in self.features:
                hits.append({"chunk_id": "w1", "doc_id": "", "kb_id": doc["kb_id"], "seq": 0, "chunk_type": "wiki_page",
                             "title": "离心泵", "content": "离心泵概述\n\n正文", "context_header": "实体", "score": 0.5,
                             "wiki_slug": "entity/pump", "wiki_type": "entity", "wiki_sources": [doc["id"]]})
            out = {"query": body.get("query"), "items": hits}
            if body.get("trace"):
                out["trace"] = {"counts": {"fused": len(hits), "returned": len(hits)}, "reranked": False, "variants": []}
            return self._json(out)
        if m := re.match(r"^/v1/kbs/([^/]+)/wiki/(.+)$", path):
            if m.group(2) == "page":
                return self._json({"id": "w1", "slug": request.url.params.get("slug"), "title": "离心泵",
                                   "page_type": "entity", "summary": "泵", "content": "离心泵正文",
                                   "links_to": [{"slug": "concept/pressure", "title": "压力", "page_type": "concept"}],
                                   "linked_from": [], "sources": [{"id": "wk_d1", "title": "文档"}],
                                   "source_doc_ids": ["wk_d1"]})
            if m.group(2) == "pages":
                return self._json({"items": [{"id": "w1", "slug": "entity/pump", "title": "离心泵",
                                              "page_type": "entity", "summary": "泵"}], "total": 1})
        if path.startswith("/v1/tools/"):
            tool = path.rsplit("/", 1)[1]
            if tool == "kb_search":
                return self._json([{"pageId": "d1:L1", "kbId": "kb1", "docId": "d1", "title": "native", "native": True}])
            if tool == "doc_grep":
                return self._json({"pattern": body["pattern"], "searchMode": "regex", "totalMatches": 1,
                                   "totalDocuments": 1, "hasMore": False,
                                   "matches": [{"docId": "d1", "kbId": "kb1", "lines": [{"lineNumber": 3, "text": "native"}]}]})
            return self._json({"native": tool, "scope": request.headers.get("x-kb-scope")})
        return self._json({"error": {"code": "not_found", "message": path}}, 404)


@pytest.fixture
def env(monkeypatch):
    for k, v in KEY_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("KBS_CRED", "kbs-backend-key")
    monkeypatch.setenv("KBS_TOOLS_CRED", "kbs-tools-key")
    monkeypatch.setenv("WK_CRED", "wk-backend-key")


@pytest.fixture
def providers():
    common = ["kbs.write", "ingest.file", "documents.reprocess", "documents.file", "wiki.read", "content.structure", "grep",
              "uploads"]
    kbs = FakeProvider("kb-service", "", [*common, "questions", "tools.da"])
    wk = FakeProvider("weknora", "wk_", common)
    return {"kbs": kbs, "wk": wk}


def make_client(env, providers, tmp_path, monkeypatch, extra_backends: list[dict] | None = None, *,
                default: str = "kbs", kbs_prefix: str = "") -> TestClient:
    monkeypatch.setenv("KBGW_UPLOAD_DIR", str(tmp_path / "uploads"))
    from kb_gateway import config

    config.get_settings.cache_clear()
    hosts = {"kbs.example.com": providers["kbs"], "wk.example.com": providers["wk"], **{
        f"{b['id']}.example.com": providers[b["id"]] for b in (extra_backends or [])
    }}
    transport = httpx.MockTransport(lambda request: hosts[request.url.host].handle(request))
    backends = parse_backends({"backends": [
        {"id": "kbs", "base_url": "https://kbs.example.com", "credential_env": "KBS_CRED", "tools_credential_env": "KBS_TOOLS_CRED",
         "default": default == "kbs", "id_prefix": kbs_prefix},
        {"id": "wk", "label": "WeKnora", "base_url": "https://wk.example.com", "credential_env": "WK_CRED", "id_prefix": "wk_",
         "default": default == "wk"},
        *(extra_backends or []),
    ]})
    state = State(registry=Registry.from_configs(backends, transport=transport), keys=KeyRing(parse_keys(KEYS)))
    return TestClient(create_app(state))


@pytest.fixture
def client(env, providers, tmp_path, monkeypatch):
    with make_client(env, providers, tmp_path, monkeypatch) as c:
        yield c


def auth(key: str = "plugin-key") -> dict:
    return {"Authorization": f"Bearer {key}"}


def da(scope: str, key: str = "da-key") -> dict:
    return {**auth(key), "X-Session-Id": "s1", "X-Tenant-Id": "default", "X-Request-Id": "r1", "X-Kb-Scope": scope}
