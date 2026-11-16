"""Contract v1.1 mechanisms (section 9): the gateway forwards only what a backend declared."""

from __future__ import annotations

import httpx
import pytest

from conftest import FakeProvider, auth, make_client

V11_FEATURES = ["actions", "jobs", "views", "settings", "search.options", "evidence", "documents.file",
                "documents.file.range", "documents.export", "folders", "folders.delete", "kbs.write"]
ACTIONS = [
    {"id": "rebuild_embeddings", "scope": "kb", "label": "重建向量", "confirm": True, "job": True},
    {"id": "regenerate_summary", "scope": "document", "label": "重新生成摘要", "job": False},
]
VIEWS = [
    {"id": "overview", "scope": "document", "label": "概览", "media_type": "text/markdown"},
    {"id": "timeline", "scope": "kb", "label": "时间线", "media_type": "application/json", "renderer": "timeline"},
]
SETTINGS_SCHEMA = {"type": "object", "properties": {"api_key": {"type": "string", "x-secret": True}}}
SEARCH_OPTIONS = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["hybrid", "keyword"]}}}
FILE = b"0123456789abcdef"


def _stream(data: bytes):
    """Streamed like a real file response (the gateway relays the raw stream)."""

    async def body():
        yield data

    return body()


class V11Provider(FakeProvider):
    """A backend that declares every v1.1 mechanism (synthetic data)."""

    def __init__(self, prefix: str = "v_", *, readonly_features: list[str] | None = None) -> None:
        super().__init__("v11", prefix, readonly_features or V11_FEATURES)

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: C901 - one fake, many routes
        path, p = request.url.path, self.prefix
        if self.down:
            return httpx.Response(503, text="down")
        if path == "/v1/capabilities":
            self.calls.append((request.method, path, {}))
            return self._json({"contract": "knowledge-provider/v1", "role": "base",
                               "provider": {"type": "v11", "version": "1"}, "features": self.features,
                               "actions": ACTIONS, "views": VIEWS, "settings_schema": SETTINGS_SCHEMA,
                               "search_options_schema": SEARCH_OPTIONS, "export_formats": ["md", "json"]})
        record = (request.method, path, {"params": dict(request.url.params), "headers": dict(request.headers),
                                         "content": request.content})
        if path.endswith("/actions/rebuild_embeddings") or path.endswith("/actions/regenerate_summary"):
            self.calls.append(record)
            if path.endswith("rebuild_embeddings"):
                return self._json({"job": {"id": f"{p}job1", "kb_id": f"{p}kb1", "action": "rebuild_embeddings",
                                           "status": "running", "progress": 0.1, "cancellable": True}})
            return self._json({"result": {"ok": True}})
        if path == f"/v1/jobs/{p}job1":
            self.calls.append(record)
            return self._json({"id": f"{p}job1", "status": "running", "report": request.url.params.get("report")})
        if path == f"/v1/jobs/{p}job1/cancel":
            self.calls.append(record)
            return self._json({"id": f"{p}job1", "status": "cancelled"})
        if path == f"/v1/kbs/{p}kb1/jobs":
            self.calls.append(record)
            return self._json({"items": [{"id": f"{p}job1", "status": "running"}]})
        if path == f"/v1/documents/{p}d1/views/overview":
            self.calls.append(record)
            return httpx.Response(200, text="# 概览", headers={"content-type": "text/markdown; charset=utf-8"})
        if path == f"/v1/kbs/{p}kb1/views/timeline":
            self.calls.append(record)
            return self._json({"events": [{"date": "2026-01-01", "title": "投运"}]})
        if path == "/v1/settings":
            self.calls.append(record)
            return self._json({"api_key": "******"})
        if path.startswith("/v1/evidence/"):
            self.calls.append(record)
            anchor = path.rsplit("/", 1)[1]
            return self._json({"anchor_id": anchor, "doc_id": f"{p}d1", "kb_id": f"{p}kb1", "text": "原文",
                               "locators": [{"page": 3, "bbox": [1.0, 2.0, 3.0, 4.0]}]})
        if path == f"/v1/documents/{p}d1/file":
            self.calls.append(record)
            rng = request.headers.get("range")
            if rng:
                start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
                return httpx.Response(206, content=_stream(FILE[start:end + 1]), headers={
                    "content-type": "application/pdf", "content-range": f"bytes {start}-{end}/{len(FILE)}",
                    "accept-ranges": "bytes"})
            return httpx.Response(200, content=_stream(FILE), headers={"content-type": "application/pdf"})
        if path == f"/v1/documents/{p}d1/export/md":
            self.calls.append(record)
            return httpx.Response(200, content=_stream("# 导出".encode()), headers={"content-type": "text/markdown",
                                                               "content-disposition": 'attachment; filename="a.md"'})
        if path == f"/v1/kbs/{p}kb1/folders":
            self.calls.append(record)
            if request.method == "DELETE":
                return httpx.Response(204)
            return self._json({"items": [{"path": "规程/泵", "documents": 2}]})
        return super().handle(request)


@pytest.fixture
def v11(env, providers, tmp_path, monkeypatch):
    provider = V11Provider()
    providers["v"] = provider
    extra = [{"id": "v", "label": "V11", "base_url": "https://v.example.com", "id_prefix": "v_"}]
    with make_client(env, providers, tmp_path, monkeypatch, extra) as client:
        yield client, provider


def test_backend_summary_carries_declarations(v11):
    client, _ = v11
    items = {b["id"]: b for b in client.get("/v1/backends", headers=auth()).json()["items"]}
    assert items["v"]["actions"] == ACTIONS
    assert items["v"]["views"] == VIEWS
    assert items["v"]["settings_schema"] == SETTINGS_SCHEMA
    assert items["v"]["search_options_schema"] == SEARCH_OPTIONS
    assert items["v"]["export_formats"] == ["md", "json"]
    # a backend without these features declares nothing
    assert items["kbs"]["actions"] == [] and items["kbs"]["settings_schema"] is None


def test_declared_actions_are_forwarded_with_params(v11):
    client, provider = v11
    r = client.post("/v1/kbs/v_kb1/actions/rebuild_embeddings", json={"params": {"force": True}}, headers=auth())
    assert r.status_code == 200 and r.json()["job"]["id"] == "v_job1"
    method, path, info = provider.calls[-1]
    assert (method, path) == ("POST", "/v1/kbs/v_kb1/actions/rebuild_embeddings")
    assert b'"force": true' in info["content"] or b'"force":true' in info["content"]
    r = client.post("/v1/documents/v_d1/actions/regenerate_summary", headers=auth())
    assert r.status_code == 200 and r.json() == {"result": {"ok": True}}


@pytest.mark.parametrize("url", [
    "/v1/kbs/v_kb1/actions/regenerate_summary",  # declared, but for documents
    "/v1/kbs/v_kb1/actions/drop_everything",  # not declared
    "/v1/kbs/v_kb1/actions/Bad..id",
    "/v1/kbs/kb1/actions/rebuild_embeddings",  # kb-service declares no actions
])
def test_undeclared_actions_are_refused(v11, url):
    client, provider = v11
    before = len(provider.calls)
    r = client.post(url, json={"params": {}}, headers=auth())
    assert r.status_code == 501, r.text
    assert len(provider.calls) == before


def test_actions_need_manage(v11):
    client, _ = v11
    r = client.post("/v1/kbs/v_kb1/actions/rebuild_embeddings", json={}, headers=auth("mcp-key"))
    assert r.status_code == 403


def test_jobs_routes(v11):
    client, provider = v11
    assert client.get("/v1/jobs/v_job1?report=true", headers=auth()).json()["report"] == "true"
    assert client.post("/v1/jobs/v_job1/cancel", headers=auth()).json()["status"] == "cancelled"
    assert client.get("/v1/kbs/v_kb1/jobs", headers=auth()).json()["items"][0]["id"] == "v_job1"
    assert client.post("/v1/jobs/v_job1/cancel", headers=auth("mcp-key")).status_code == 403


def test_views_follow_declared_scope_and_media_type(v11):
    client, _ = v11
    r = client.get("/v1/documents/v_d1/views/overview", headers=auth("mcp-key"))
    assert r.status_code == 200 and r.text == "# 概览" and r.headers["content-type"].startswith("text/markdown")
    assert client.get("/v1/kbs/v_kb1/views/timeline", headers=auth()).json()["events"][0]["title"] == "投运"
    assert client.get("/v1/kbs/v_kb1/views/overview", headers=auth()).status_code == 501
    assert client.get("/v1/documents/v_d1/views/unknown", headers=auth()).status_code == 501


def test_settings_are_backend_wide_and_need_manage(v11):
    client, provider = v11
    assert client.get("/v1/settings?backend=v", headers=auth()).json() == {"api_key": "******"}
    r = client.put("/v1/settings?backend=v", json={"api_key": "******"}, headers=auth())
    assert r.status_code == 200 and provider.calls[-1][0] == "PUT"
    assert client.get("/v1/settings?backend=v", headers=auth("mcp-key")).status_code == 403
    assert client.get("/v1/settings?backend=kbs", headers=auth()).status_code == 501
    assert client.put("/v1/settings?backend=v", json=["x"], headers=auth()).status_code == 400


def test_evidence_routes_by_document(v11):
    client, provider = v11
    r = client.get("/v1/evidence/v_d1:paragraph:3", headers=auth("mcp-key"))
    assert r.status_code == 200 and r.json()["locators"][0]["page"] == 3
    assert provider.calls[-1][1] == "/v1/evidence/v_d1:paragraph:3"
    # a backend without evidence
    assert client.get("/v1/evidence/wk_d1:paragraph:3", headers=auth()).status_code == 501


def test_file_range_is_relayed(v11):
    client, provider = v11
    r = client.get("/v1/documents/v_d1/file", headers={**auth(), "Range": "bytes=2-5"})
    assert r.status_code == 206 and r.content == b"2345"
    assert r.headers["content-range"] == "bytes 2-5/16" and r.headers["accept-ranges"] == "bytes"
    assert provider.calls[-1][2]["headers"]["range"] == "bytes=2-5"


def test_range_is_not_sent_to_backends_without_it(v11, providers):
    client, _ = v11
    r = client.get("/v1/documents/wk_d1/file", headers={**auth(), "Range": "bytes=0-1"})
    assert r.status_code == 200
    assert "range" not in providers["wk"].calls[-1][2]["headers"]


def test_export_only_declared_formats(v11):
    client, _ = v11
    r = client.get("/v1/documents/v_d1/export/md", headers=auth())
    assert r.status_code == 200 and r.text == "# 导出" and "a.md" in r.headers["content-disposition"]
    assert client.get("/v1/documents/v_d1/export/docx", headers=auth()).status_code == 501


def test_folders(v11):
    client, provider = v11
    assert client.get("/v1/kbs/v_kb1/folders", headers=auth()).json()["items"][0]["path"] == "规程/泵"
    r = client.delete("/v1/kbs/v_kb1/folders?path=规程/泵", headers=auth())
    assert r.status_code == 204 and provider.calls[-1][2]["params"] == {"path": "规程/泵"}
    assert client.delete("/v1/kbs/v_kb1/folders", headers=auth()).status_code == 422
    client.get("/v1/kbs/v_kb1/documents?folder=规程", headers=auth())
    assert provider.calls[-1][2]["params"]["folder"] == "规程"


def test_readonly_backend_loses_write_mechanisms(env, providers, tmp_path, monkeypatch):
    providers["v"] = V11Provider()
    extra = [{"id": "v", "label": "V11", "base_url": "https://v.example.com", "id_prefix": "v_", "readonly": True}]
    with make_client(env, providers, tmp_path, monkeypatch, extra) as client:
        item = next(b for b in client.get("/v1/backends", headers=auth()).json()["items"] if b["id"] == "v")
        assert item["actions"] == [] and item["settings_schema"] is None
        assert "folders.delete" not in item["features"] and "folders" in item["features"]
        assert client.post("/v1/kbs/v_kb1/actions/rebuild_embeddings", json={}, headers=auth()).status_code == 501


# ---- show_source (MCP Apps card) ---------------------------------------------------------------------------


def _mcp(coro, principal):
    from kb_gateway.api import mcp_server

    async def wrapped():
        token = mcp_server._principal.set(principal)
        try:
            return await coro
        finally:
            mcp_server._principal.reset(token)

    import asyncio

    return asyncio.run(wrapped())


def test_show_source_from_chunks_on_any_backend(client):
    from kb_gateway.api import mcp_server
    from kb_gateway.auth import Principal

    result = _mcp(mcp_server.show_source("wk_d1", chunk_seq=1), Principal("mcp", frozenset({"retrieve"})))
    source = result.structuredContent["source"]
    assert source["doc_id"] == "wk_d1" and "0.3 MPa" in source["text"]
    assert source["context_before"].startswith("# 离心泵") and source["locators"] == [{"page": 1}]
    assert "出处：weknora 文档" in result.content[0].text


def test_show_source_prefers_evidence_when_declared(v11):
    from kb_gateway.api import mcp_server
    from kb_gateway.auth import Principal

    client, provider = v11
    result = _mcp(mcp_server.show_source("v_d1", anchor_id="v_d1:paragraph:3"), Principal("mcp", frozenset({"retrieve"})))
    assert result.structuredContent["source"]["locators"][0]["bbox"] == [1.0, 2.0, 3.0, 4.0]
    assert provider.calls[-1][1] == "/v1/evidence/v_d1:paragraph:3"
    # an anchor of another document is not followed
    result = _mcp(mcp_server.show_source("v_d1", anchor_id="wk_d1:paragraph:3"), Principal("mcp", frozenset({"retrieve"})))
    assert result.structuredContent is None and "没有找到" in result.content[0].text


def test_show_source_respects_the_connection_scope(client):
    from kb_gateway.api import mcp_server
    from kb_gateway.auth import Principal

    result = _mcp(mcp_server.show_source("d1", chunk_seq=0), Principal("mcp", frozenset({"retrieve"}), ("wk_kb1",)))
    assert result.structuredContent is None and "无权访问" in result.content[0].text


def test_show_source_card_is_an_mcp_app(client):
    import asyncio

    from kb_gateway.api import mcp_server

    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert tools["show_source"].meta == {"ui": {"resourceUri": mcp_server.SOURCE_CARD_URI}}
    contents = list(asyncio.run(mcp_server.mcp.read_resource(mcp_server.SOURCE_CARD_URI)))
    assert contents[0].mime_type == "text/html;profile=mcp-app"
    assert "ui/initialize" in contents[0].content and "ui/notifications/tool-result" in contents[0].content
