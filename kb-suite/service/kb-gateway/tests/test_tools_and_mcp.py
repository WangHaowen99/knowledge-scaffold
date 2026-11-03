from __future__ import annotations

import asyncio
import json

import httpx
from conftest import da

from kb_gateway.api import mcp_server
from kb_gateway.auth import Principal


def test_da_tools_need_headers_and_a_scope(client):
    assert client.post("/v1/tools/kb_search", headers={"Authorization": "Bearer da-key"}, json={"query": "x"}).status_code == 400
    assert client.post("/v1/tools/kb_search", headers=da(""), json={"query": "x"}).status_code == 403
    assert client.post("/v1/tools/reprocess_document", headers=da("kb1", key="mcp-key"), json={"docId": "d1"}).status_code == 403


def test_native_backend_gets_the_call_unchanged(client, providers):
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "x", "kbIds": ["kb1"]}).json()
    assert data[0]["native"] is True
    _, _, meta = providers["kbs"].calls[-1]
    # tools credential, and only this backend's part of the session scope
    assert meta["headers"]["authorization"] == "Bearer kbs-tools-key"
    assert meta["headers"]["x-kb-scope"] == "kb1"


def test_kb_search_across_backends_is_generic_and_fused(client):
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "压力"}).json()
    pages = {r["pageId"] for r in data}
    assert "d1:L1" in pages and "wk_d1:L1" in pages and "wiki:wk_kb1/entity/pump" in pages


def test_kb_search_page_types_filter_wiki(client):
    data = client.post("/v1/tools/kb_search", headers=da("wk_kb1"), json={"query": "泵", "pageTypes": ["entity"]}).json()
    assert [r["pageType"] for r in data] == ["entity"]


def test_expand_l1_forwards_to_the_structure_endpoint(client, providers):
    data = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_d1:L1", "tokenBudget": 2000}).json()
    assert data["level"] == "L1" and "循环泵" in data["content"]
    assert data["anchors"] == [{"id": "wk_d1:paragraph:0", "type": "paragraph", "lineStart": 3,
                                "preview": data["content"][:20], "sectionTitle": None}]
    assert providers["wk"].calls[-1][2]["headers"]["authorization"] == "Bearer wk-backend-key"
    section = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docId": "wk_d1", "heading": "巡检"}).json()
    assert section["content"].startswith("## 巡检") and "totalChars" not in section
    missing = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docId": "wk_d1", "heading": "没有"}).json()
    assert "not found" in missing["error"]


def test_expand_without_structure_feature(env, providers, tmp_path, monkeypatch):
    from conftest import make_client

    providers["wk"].features.remove("content.structure")
    with make_client(env, providers, tmp_path, monkeypatch) as c:
        data = c.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_d1:L1"}).json()
        assert "不支持" in data["error"]


def test_expand_refuses_documents_outside_the_scope(client):
    data = client.post("/v1/tools/expand", headers=da("wk_other"), json={"pageId": "wk_d1:L1"}).json()
    assert "不在会话范围内" in data["error"]


def test_expand_batch_and_l2_on_generic_backend(client):
    batch = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docIds": ["wk_d1", "wk_nope"]}).json()
    assert batch["mode"] == "batch" and batch["results"][0]["outline"][0] == "离心泵"
    assert batch["results"][0]["tokenCount"] > 0
    assert "error" in batch["results"][1]
    l2 = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_d1:L2"}).json()
    assert "L2" in l2["error"]


def test_wiki_browse_generic(client):
    page = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"pageId": "wiki:wk_kb1/entity/pump"}).json()
    assert page["title"] == "离心泵" and page["linksTo"][0]["pageId"] == "wiki:wk_kb1/concept/pressure"
    listing = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"kbId": "wk_kb1", "listDocuments": True}).json()
    assert listing["totalDocuments"] == 1 and listing["documents"][0]["abstractPageId"] == "wk_d1:L0"
    pages = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"kbId": "wk_kb1"}).json()
    assert {p["pageType"] for p in pages["pages"]} == {"abstract", "structure_md", "entity"}


def test_doc_grep_across_backends_merges_their_grep(client, providers):
    data = client.post("/v1/tools/doc_grep", headers=da("kb1,wk_kb1"), json={"pattern": "每"}).json()
    assert {m["docId"] for m in data["matches"]} == {"d1", "wk_d1"}
    assert data["matches"][0]["pageId"].endswith(":L1") and data["matches"][0]["lines"][0]["lineNumber"] > 0
    assert data["total"] == data["totalMatches"] == 2
    assert all(c[1] != "/v1/tools/doc_grep" for c in providers["kbs"].calls)


def test_doc_grep_on_one_native_backend_is_forwarded(client):
    data = client.post("/v1/tools/doc_grep", headers=da("kb1"), json={"pattern": "每"}).json()
    assert data["matches"][0]["lines"][0]["text"] == "native"


def test_grep_skips_backends_without_it(env, providers, tmp_path, monkeypatch):
    from conftest import make_client

    providers["wk"].features.remove("grep")
    with make_client(env, providers, tmp_path, monkeypatch) as c:
        data = c.post("/v1/tools/doc_grep", headers=da("kb1,wk_kb1"), json={"pattern": "每"}).json()
        assert [m["docId"] for m in data["matches"]] == ["d1"] and data["skipped_backends"][0]["id"] == "wk"


def test_reprocess_generic(client, providers):
    data = client.post("/v1/tools/reprocess_document", headers=da("wk_kb1"), json={"docId": "wk_d1"}).json()
    assert data["status"] == "queued"
    assert providers["wk"].calls[-1][:2] == ("POST", "/v1/documents/wk_d1/reprocess")


def _run(coro, principal: Principal):
    async def wrapped():
        token = mcp_server._principal.set(principal)
        try:
            return await coro
        finally:
            mcp_server._principal.reset(token)

    return asyncio.run(wrapped())


def test_mcp_tools_cover_all_backends(client):
    who = Principal("mcp", frozenset({"retrieve"}))
    kbs = json.loads(_run(mcp_server.list_knowledge_bases(), who))
    assert {kb["backend"] for kb in kbs} == {"kbs", "WeKnora"}
    text = _run(mcp_server.search_knowledge("压力"), who)
    assert "doc_id=wk_d1" in text and "doc_id=d1" in text
    read = _run(mcp_server.read_document("wk_d1"), who)
    assert read.startswith("《weknora 文档》") and "[seq 1]" in read
    wiki = _run(mcp_server.read_wiki_page("wk_kb1", "entity/pump"), who)
    assert "链接到：压力(concept/pressure)" in wiki


def test_mcp_scope_header_confines_the_connection(client):
    who = Principal("mcp", frozenset({"retrieve"}), ("wk_kb1",))
    kbs = json.loads(_run(mcp_server.list_knowledge_bases(), who))
    assert [kb["id"] for kb in kbs] == ["wk_kb1"]
    assert "无权访问" in _run(mcp_server.read_document("d1"), who)


def test_expand_batch_reports_failures_per_document(client, providers):
    providers["wk"].features.remove("content.structure")
    asyncio.run(client.app.state.gateway.registry.refresh())
    data = client.post("/v1/tools/expand", headers=da("kb1,wk_kb1"), json={"docIds": ["d1", "wk_d1"]}).json()
    by_doc = {r["docId"]: r for r in data["results"]}
    assert "outline" in by_doc["d1"] and "error" in by_doc["wk_d1"]


def test_expand_heading_section_is_capped(client, providers):
    providers["wk"].docs["wk_d1"]["_l1"] += "\n## 大节\n\n" + "合成内容。" * 2000
    data = client.post("/v1/tools/expand", headers=da("wk_kb1"),
                       json={"pageId": "wk_d1:L1", "heading": "大节", "tokenBudget": 1000}).json()
    assert len(data["content"]) == 1500 and data["truncated"] is True


def test_doc_grep_clamps_limits_before_forwarding(client, providers):
    client.post("/v1/tools/doc_grep", headers=da("kb1,wk_kb1"),
                json={"pattern": "每", "maxResults": 5000, "maxLinesPerPage": 5000})
    body = next(meta["body"] for method, path, meta in reversed(providers["wk"].calls) if path == "/v1/grep")
    assert body["max_results"] == 200 and body["max_lines_per_doc"] == 100


def test_readonly_backend_is_not_asked_to_reprocess(env, providers, tmp_path, monkeypatch):
    from conftest import make_client

    providers["ro"] = type(providers["kbs"])("kb-service", "ro_", ["tools.da", "documents.reprocess", "content.structure"])
    with make_client(env, providers, tmp_path, monkeypatch,
                     [{"id": "ro", "base_url": "https://ro.example.com", "id_prefix": "ro_", "readonly": True}]) as c:
        data = c.post("/v1/tools/reprocess_document", headers=da("ro_kb1"), json={"docId": "ro_d1"}).json()
        assert "error" in data
        assert not [p for _, p, _ in providers["ro"].calls if p.startswith("/v1/tools/")]


def test_wiki_page_ids_with_path_syntax_are_not_followed(client, providers):
    before = len(providers["wk"].calls)
    for slug in ("../../../wk_kb2/wiki/pages/x", "entity/../x", "a?b=1", "a#b", "a%2F..", "a//b"):
        data = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"pageId": f"wiki:wk_kb1/{slug}"}).json()
        assert "error" in data, slug
    assert len(providers["wk"].calls) == before
    ok = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"pageId": "wiki:wk_kb1/entity/pump"}).json()
    assert ok["slug"] == "entity/pump"


def test_ids_with_path_syntax_never_reach_a_backend(client, providers):
    before = {name: len(p.calls) for name, p in providers.items()}
    for page_id in ("wk_kb1/../../kbs/wk_kb1/wiki/page:L1", "wiki:wk_kb1/../../x", "wk_d1?x=1:L1"):
        client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"pageId": page_id})
    client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docId": "wk_d1/../wk_d2"})
    assert client.get("/v1/documents/wk_d1%3Fx=1", headers={"Authorization": "Bearer plugin-key"}).status_code == 400
    assert {name: len(p.calls) for name, p in providers.items()} == before



def test_cross_backend_kb_search_uses_the_short_timeout(client, monkeypatch):
    from kb_gateway import config

    monkeypatch.setenv("KBGW_SEARCH_TIMEOUT_S", "7")
    config.get_settings.cache_clear()
    seen = []
    for backend in client.app.state.gateway.registry.backends:
        real = backend.client.send

        async def spy(request, *, _real=real, **kw):
            seen.append(request.extensions["timeout"]["read"])
            return await _real(request, **kw)

        monkeypatch.setattr(backend.client, "send", spy)
    client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "压力"})
    assert len(seen) >= 2 and set(seen) == {7.0}


def test_a_failing_linked_from_keeps_the_search_results(client, providers):
    real = providers["wk"].handle

    def dead_link(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wiki/page"):
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "dead link"}})
        return real(request)

    providers["wk"].handle = dead_link
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"),
                       json={"query": "压力", "linkedFrom": "wiki:wk_kb1/entity/pump"}).json()
    assert isinstance(data, list) and {r["kbId"] for r in data} >= {"kb1", "wk_kb1"}


def test_search_wiki_stays_within_the_contract_top_k(client, providers):
    _run(mcp_server.search_wiki("泵", limit=20), Principal("mcp", frozenset({"retrieve"})))
    sent = [meta["body"]["top_k"] for p in providers.values() for _, path, meta in p.calls if path == "/v1/search"]
    assert sent and max(sent) <= 50


def test_native_doc_grep_and_large_token_budget_are_clamped(client, providers):
    client.post("/v1/tools/doc_grep", headers=da("kb1"), json={"pattern": "每", "maxLinesPerPage": 100000})
    body = providers["kbs"].calls[-1][2]["body"]
    assert body["maxLinesPerPage"] == 100 and body["maxResults"] == 50
    client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_d1:L1", "tokenBudget": 10_000_000})
    params = [meta["params"] for _, path, meta in providers["wk"].calls if path.endswith("/structure")][-1]
    assert int(params["limit"]) <= 200000
