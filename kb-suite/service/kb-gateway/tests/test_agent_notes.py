"""Notes the gateway adds to agent tool results: unavailable backends, knowledge-base counts,
grep truncation and files without text (synthetic data only)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from conftest import auth, da

from kb_gateway import notes
from kb_gateway.api import mcp_server
from kb_gateway.auth import Principal

WHO = Principal("mcp", frozenset({"retrieve"}))


def _run(coro, principal: Principal = WHO):
    async def wrapped():
        token = mcp_server._principal.set(principal)
        try:
            return await coro
        finally:
            mcp_server._principal.reset(token)

    return asyncio.run(wrapped())


def _stop(client, backend_id: str = "wk") -> None:
    """The backend as the refresh loop leaves it once it stops answering; its names were listed before."""

    client.get("/v1/kbs", headers=auth())
    backend = client.app.state.gateway.registry.by_id[backend_id]
    backend.healthy, backend.last_error = False, "connection refused"


def _add_doc(provider, doc_id: str, l1: str, **extra) -> None:
    kb_id = next(iter(provider.kbs))
    provider.docs[doc_id] = {"id": doc_id, "kb_id": kb_id, "title": f"合成文档 {doc_id}", "file_name": f"{doc_id}.pdf",
                             "file_type": "pdf", "folder_path": "", "parse_status": "completed", "summary": "",
                             "chunk_count": 1 if l1 else 0, "l1_chars": len(l1), "_l1": l1, **extra}


def _native_tool(provider, tool: str, payload) -> None:
    real = provider.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/tools/{tool}":
            return httpx.Response(200, json=payload)
        return real(request)

    provider.handle = handle


# ---- 1. knowledge bases on an unavailable backend --------------------------------------


def test_kb_search_names_the_knowledge_bases_it_could_not_search(client):
    _stop(client)
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "电流"}).json()
    assert {r["kbId"] for r in data["results"]} == {"kb1"}
    assert data["unavailableKbs"] == [{"kbId": "wk_kb1", "name": "weknora 库", "backend": "WeKnora",
                                       "code": "backend_unavailable", "reason": "WeKnora 当前不可用：connection refused"}]
    assert "weknora 库（wk_kb1，WeKnora）" in data["message"] and "没有检索" in data["message"]
    # notes lead the result, so a runtime that cuts long tool output keeps them
    assert list(data)[:2] == ["message", "unavailableKbs"]


def test_kb_search_on_one_backend_still_names_the_other_unavailable_ones_in_the_session(client):
    # the model searched only kb-service, but the session also holds a WeKnora knowledge base
    _stop(client)
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "x", "kbIds": ["kb1"]}).json()
    assert data["results"][0]["native"] is True
    assert [k["kbId"] for k in data["unavailableKbs"]] == ["wk_kb1"]


def test_native_search_is_unchanged_while_everything_is_up(client):
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "x", "kbIds": ["kb1"]}).json()
    assert isinstance(data, list) and data[0]["native"] is True


def test_a_backend_that_stops_answering_mid_call_is_reported(client, providers):
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    providers["kbs"].handle = refused
    data = client.post("/v1/tools/kb_search", headers=da("kb1"), json={"query": "x"}).json()
    assert data["results"] == [] and data["unavailableKbs"][0]["kbId"] == "kb1"
    providers["wk"].down = True
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "x"}).json()
    assert {k["kbId"] for k in data["unavailableKbs"]} == {"kb1", "wk_kb1"}


def test_doc_grep_and_wiki_browse_name_unavailable_knowledge_bases(client):
    _stop(client)
    grep = client.post("/v1/tools/doc_grep", headers=da("kb1,wk_kb1"), json={"pattern": "每"}).json()
    assert {m["docId"] for m in grep["matches"]} == {"d1"} and grep["unavailableKbs"][0]["kbId"] == "wk_kb1"
    listing = client.post("/v1/tools/wiki_browse", headers=da("kb1,wk_kb1"), json={"kbId": "kb1"}).json()
    assert listing["native"] == "wiki_browse" and listing["unavailableKbs"][0]["kbId"] == "wk_kb1"
    down = client.post("/v1/tools/wiki_browse", headers=da("kb1,wk_kb1"), json={"kbId": "wk_kb1", "listDocuments": True}).json()
    assert "当前不可用" in down["error"] and down["unavailableKbs"][0]["name"] == "weknora 库"


def test_expand_on_an_unavailable_backend_is_not_reported_as_missing(client):
    _stop(client)
    one = client.post("/v1/tools/expand", headers=da("kb1,wk_kb1"), json={"pageId": "wk_d1:L1"}).json()
    assert "当前不可用" in one["error"] and "不是不存在" in one["error"] and one["backendUnavailable"] == "WeKnora"
    batch = client.post("/v1/tools/expand", headers=da("kb1,wk_kb1"), json={"docIds": ["d1", "wk_d1"]}).json()
    by_doc = {r["docId"]: r for r in batch["results"]}
    assert "outline" in by_doc["d1"] and by_doc["wk_d1"]["backendUnavailable"] == "WeKnora"


def test_mcp_tools_say_which_knowledge_bases_were_not_searched(client):
    _stop(client)
    listing = json.loads(_run(mcp_server.list_knowledge_bases()))
    assert listing["total"] == 1 and listing["unavailable_backends"] == [
        {"backend": "WeKnora", "last_seen_knowledge_bases": ["weknora 库"]}]
    assert "WeKnora" in listing["summary"] and "检索不到" in listing["summary"]
    searched = _run(mcp_server.search_knowledge("电流"))
    assert searched.startswith("（以下知识后端的知识库本次没有检索到：WeKnora 当前不可用（其上的知识库：weknora 库）")
    assert "doc_id=d1" in searched
    scoped = _run(mcp_server.search_knowledge("电流", kb_ids=["kb1", "wk_kb1"]))
    assert "weknora 库（wk_kb1，WeKnora）" in scoped
    only_down = _run(mcp_server.search_knowledge("电流", kb_ids=["wk_kb1"]))
    assert "没有检索" in only_down and "weknora 库" in only_down
    read = _run(mcp_server.read_document("wk_d1"))
    assert "当前不可用" in read and "不存在" not in read.split("（")[0]
    grep = json.loads(_run(mcp_server.grep_documents("每")))
    assert "WeKnora" in grep["message"] and "没有检索" in grep["message"]


def test_search_over_every_knowledge_base_reports_an_unavailable_backend(client):
    client.app.state.gateway.registry.by_id["wk"].healthy = False
    data = client.post("/v1/search", headers=auth(), json={"query": "x"}).json()
    assert {h["backend"] for h in data["items"]} == {"kbs"} and data["failed_backends"][0]["id"] == "wk"


# ---- 4. knowledge-base counts for the Swarm expert -----------------------------------------


def test_list_knowledge_bases_gives_exact_counts_per_backend_first(client, providers):
    for i in (2, 3):
        providers["kbs"].kbs[f"kb{i}"] = {**providers["kbs"].kbs["kb1"], "id": f"kb{i}", "name": f"合成库 {i}"}
    text = _run(mcp_server.list_knowledge_bases())
    data = json.loads(text)
    assert data["total"] == 4 and {b["backend"]: b["count"] for b in data["backends"]} == {"kbs": 3, "WeKnora": 1}
    assert data["summary"] == "共 4 个可用的知识库：kbs 3 个、WeKnora 1 个。用户要求列出知识库时，按后端逐个写出全部库名。"
    # the counts come before the list, so a cut-off result still has them
    assert text.index('"total"') < text.index('"backends"')
    assert all(len(b["knowledge_bases"]) == b["count"] for b in data["backends"])
    assert "不要自己数" in mcp_server.list_knowledge_bases.__doc__ and "不要自己数" in mcp_server.INSTRUCTIONS
    assert "全部库名" in mcp_server.list_knowledge_bases.__doc__ and "全部库名" in mcp_server.INSTRUCTIONS


def test_list_knowledge_bases_is_capped_but_counts_everything(client, providers, monkeypatch):
    monkeypatch.setattr(mcp_server, "LIST_MAX_KBS", 2)
    monkeypatch.setattr(mcp_server, "LIST_DESCRIPTION_CHARS", 5)
    for i in (2, 3):
        providers["kbs"].kbs[f"kb{i}"] = {**providers["kbs"].kbs["kb1"], "id": f"kb{i}", "description": "很长的合成说明文字"}
    data = json.loads(_run(mcp_server.list_knowledge_bases()))
    assert data["total"] == 4 and sum(len(b["knowledge_bases"]) for b in data["backends"]) == 2
    assert sum(b.get("not_listed", 0) for b in data["backends"]) == 2 and "只列出前 2 个" in data["summary"]
    assert data["backends"][0]["knowledge_bases"][-1]["description"] == "很长的合成…"


# ---- 5. doc_grep truncation --------------------------------------------------------------------


def test_doc_grep_says_how_many_matches_of_a_document_were_not_returned(client, providers):
    _add_doc(providers["wk"], "wk_d2", "\n".join(f"第 {i} 行 合成关键字" for i in range(1, 26)))
    data = client.post("/v1/tools/doc_grep", headers=da("wk_kb1"),
                       json={"pattern": "合成关键字", "maxLinesPerPage": 10}).json()
    match = next(m for m in data["matches"] if m["docId"] == "wk_d2")
    assert match["truncated"] is True and match["omittedMatches"] == 15 and match["matchCount"] == 25
    assert "本篇还有 15 处没有返回" in data["message"] and "maxLinesPerPage（最大 100）" in data["message"]
    assert next(iter(data)) == "message"


def test_doc_grep_without_truncation_has_no_note(client):
    data = client.post("/v1/tools/doc_grep", headers=da("wk_kb1"), json={"pattern": "出口压力"}).json()
    assert "message" not in data and all("truncated" not in m for m in data["matches"])


def test_native_doc_grep_is_annotated_too(client, providers):
    lines = [{"lineNumber": i, "text": f"合成 {i}"} for i in range(1, 11)]
    _native_tool(providers["kbs"], "doc_grep", {
        "pattern": "合成", "searchMode": "regex", "totalMatches": 40, "totalDocuments": 1, "hasMore": False,
        "matches": [{"docId": "d1", "kbId": "kb1", "title": "合成报告", "lines": lines}]})
    data = client.post("/v1/tools/doc_grep", headers=da("kb1"), json={"pattern": "合成"}).json()
    assert data["matches"][0]["omittedMatches"] == 30 and "《合成报告》共 40 处匹配" in data["message"]


def test_grep_note_when_several_documents_are_cut_or_more_documents_exist():
    result = {"totalMatches": 30, "hasMore": False, "matches": [
        {"title": "甲", "lines": [1] * 5}, {"title": "乙", "lines": [1] * 5}, {"title": "丙", "lines": [1] * 2}]}
    assert notes.annotate_grep(result, max_lines=5, max_results=50, lines_cap=100, results_cap=200)
    assert [m.get("truncated") for m in result["matches"]] == [True, True, None]
    assert "合计还有 18 处没有返回" in result["message"]
    more = {"totalMatches": 3, "hasMore": True, "matches": [{"title": "甲", "lines": [1, 2]}]}
    notes.annotate_grep(more, max_lines=5, max_results=1, lines_cap=100, results_cap=200)
    assert "maxResults（最大 200）" in more["message"]
    # a document with exactly max_lines matches is not cut
    exact = {"totalMatches": 5, "hasMore": False, "matches": [{"title": "甲", "lines": [1] * 5}]}
    assert not notes.annotate_grep(exact, max_lines=5, max_results=50, lines_cap=100, results_cap=200)
    # a backend's own per-document count wins
    own = {"totalMatches": 99, "matches": [{"title": "甲", "matchedLines": ["a"] * 5, "matchCount": 7}]}
    notes.annotate_grep(own, max_lines=5, max_results=50, lines_cap=100, results_cap=200)
    assert own["matches"][0]["omittedMatches"] == 2


def test_mcp_grep_documents_notes_truncation(client, providers):
    _add_doc(providers["wk"], "wk_d2", "\n".join(f"合成关键字 {i}" for i in range(12)))
    data = json.loads(_run(mcp_server.grep_documents("合成关键字", kb_ids=["wk_kb1"], max_lines_per_doc=4)))
    assert data["matches"][0]["omittedMatches"] == 8 and "本篇还有 8 处没有返回" in data["message"]


# ---- 6. files without readable text ----------------------------------------------------------


@pytest.mark.parametrize("text, readable", [
    ("", False), ("\n\n  \n", False), ("![](images/p1.png)\n\n![扫描页](images/p2.png)", False),
    ("<!-- page 1 -->\n<img src='a.png'>", False), ("第1页", True), ("Pump", True), ("![](a.png)\n温度 35 ℃", True),
])
def test_readable_text(text, readable):
    assert notes.has_readable_text(text) is readable


def test_expand_flags_a_file_without_text(client, providers):
    _add_doc(providers["wk"], "wk_scan", "", summary="合成摘要：一份设备说明")
    _add_doc(providers["wk"], "wk_img", "![](images/p1.png)\n\n![](images/p2.png)\n")
    l1 = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_scan:L1"}).json()
    assert l1["noText"] is True and l1["message"] == notes.NO_TEXT
    # a summary written from an empty body is flagged as well
    l0 = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_scan:L0"}).json()
    assert l0["content"] == "合成摘要：一份设备说明" and l0["noText"] is True
    images = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_img:L1"}).json()
    assert images["noText"] is True
    batch = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docIds": ["wk_d1", "wk_scan"]}).json()
    assert [r.get("noText") for r in batch["results"]] == [None, True]
    normal = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"pageId": "wk_d1:L1"}).json()
    assert "noText" not in normal


def test_native_expand_and_document_listing_flag_files_without_text(client, providers):
    _native_tool(providers["kbs"], "expand", {"docId": "d1", "level": "L1", "content": "", "totalChars": 0,
                                              "offset": 0, "hasMore": False, "anchors": []})
    data = client.post("/v1/tools/expand", headers=da("kb1"), json={"pageId": "d1:L1"}).json()
    assert data["noText"] is True
    _add_doc(providers["wk"], "wk_scan", "")
    listing = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"kbId": "wk_kb1", "listDocuments": True}).json()
    flagged = [d["docId"] for d in listing["documents"] if d.get("noText")]
    assert flagged == ["wk_scan"] and "wk_scan.pdf" in listing["message"]
    page = client.post("/v1/tools/wiki_browse", headers=da("wk_kb1"), json={"pageId": "wk_scan:L1"}).json()
    assert page["noText"] is True


def test_mcp_reading_a_file_without_text_says_so(client, providers):
    _add_doc(providers["wk"], "wk_scan", "")
    assert notes.NO_TEXT in _run(mcp_server.read_document("wk_scan"))
    docs = json.loads(_run(mcp_server.list_documents("wk_kb1")))
    assert {d["doc_id"]: d.get("no_text") for d in docs} == {"wk_d1": None, "wk_scan": True}


# ---- review fixes: document state, error codes, warnings ---------------------------------------


def _answer(provider, path: str, response: httpx.Response) -> None:
    real = provider.handle

    def handle(request: httpx.Request) -> httpx.Response:
        return response if request.url.path == path else real(request)

    provider.handle = handle


@pytest.mark.parametrize("status, flagged", [("completed", True), ("processing", False), ("failed", False)])
def test_empty_native_expand_is_flagged_only_for_a_processed_document(client, providers, status, flagged):
    providers["kbs"].docs["d1"]["parse_status"] = status
    _native_tool(providers["kbs"], "expand", {"docId": "d1", "level": "L1", "content": "", "totalChars": 0,
                                              "offset": 0, "hasMore": False, "anchors": []})
    data = client.post("/v1/tools/expand", headers=da("kb1"), json={"pageId": "d1:L1"}).json()
    assert data.get("noText", False) is flagged


def test_empty_batch_items_are_flagged_only_when_processed_and_known(client, providers):
    providers["kbs"].docs["d1"]["parse_status"] = "processing"
    _native_tool(providers["kbs"], "expand", {"mode": "batch", "totalDocs": 2, "results": [
        {"docId": "d1", "level": "L1", "outline": [], "tokenCount": 0},
        {"docId": "d404", "level": "L1", "outline": [], "tokenCount": 0}]})
    data = client.post("/v1/tools/expand", headers=da("kb1"), json={"docIds": ["d1", "d404"]}).json()
    assert [r.get("noText") for r in data["results"]] == [None, None]
    # the gateway's own batch: a document still processing has no L1 yet, and is not a file without text
    _add_doc(providers["wk"], "wk_busy", "", parse_status="processing", l1_chars=None)
    batch = client.post("/v1/tools/expand", headers=da("wk_kb1"), json={"docIds": ["wk_busy"]}).json()
    assert "noText" not in batch["results"][0]


def test_a_backend_search_error_is_named_with_its_reason(client, providers):
    # kb-service answers a search it cannot run with 200 {error, code}: not "no hits"
    _native_tool(providers["kbs"], "kb_search", {"error": "向量模型不可用，且没有可用的关键词索引", "code": "search_unavailable"})
    client.get("/v1/kbs", headers=auth())
    data = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "压力"}).json()
    assert {r["kbId"] for r in data["results"]} == {"wk_kb1"}
    assert data["unavailableKbs"][0]["code"] == "search_unavailable"
    assert "kb-service 库（kb1，kbs）" in data["message"] and "检索暂不可用：向量模型不可用" in data["message"]
    assert "当前不可用，本次没有检索" not in data["message"]


def test_one_native_backend_failing_with_a_code_keeps_the_reason(client, providers):
    _answer(providers["kbs"], "/v1/tools/kb_search",
            httpx.Response(503, json={"error": {"code": "search_unavailable", "message": "嵌入模型密钥无效"}}))
    data = client.post("/v1/tools/kb_search", headers=da("kb1"), json={"query": "x"}).json()
    assert data["results"] == [] and "检索暂不可用：嵌入模型密钥无效" in data["message"]


def test_a_refused_pattern_is_reported_as_such(client, providers):
    timeout = httpx.Response(400, json={"error": {"code": "pattern_timeout", "message": "正则匹配超时，请简化"}})
    _answer(providers["wk"], "/v1/grep", timeout)
    one = client.post("/v1/tools/doc_grep", headers=da("wk_kb1"), json={"pattern": "(a+)+$"}).json()
    assert one["error"] == "查找条件无效：正则匹配超时，请简化" and one["code"] == "pattern_timeout"
    both = client.post("/v1/tools/doc_grep", headers=da("kb1,wk_kb1"), json={"pattern": "每"}).json()
    assert {m["docId"] for m in both["matches"]} == {"d1"}
    assert "WeKnora 查找条件被拒绝：正则匹配超时" in both["message"] and "当前不可用" not in both["message"]
    assert _run(mcp_server.grep_documents("(a+)+$", kb_ids=["wk_kb1"])) == "查找条件无效：正则匹配超时，请简化"


def test_search_warnings_reach_the_agents(client, providers):
    real = providers["wk"].handle

    def degraded(request: httpx.Request) -> httpx.Response:
        response = real(request)
        if request.url.path == "/v1/search":
            return httpx.Response(200, json={**response.json(), "warnings": ["向量检索不可用，本次只用了关键词检索"]})
        return response

    providers["wk"].handle = degraded
    page = client.post("/v1/search", headers=auth(), json={"query": "压力", "kb_ids": ["wk_kb1"]}).json()
    assert page["warnings"] == ["向量检索不可用，本次只用了关键词检索"]
    tool = client.post("/v1/tools/kb_search", headers=da("wk_kb1"), json={"query": "压力"}).json()
    assert tool["message"] == "向量检索不可用，本次只用了关键词检索" and tool["results"]
    mixed = client.post("/v1/tools/kb_search", headers=da("kb1,wk_kb1"), json={"query": "压力"}).json()
    assert "WeKnora：向量检索不可用" in mixed["message"]
    text = _run(mcp_server.search_knowledge("压力"))
    assert text.startswith("注意：WeKnora：向量检索不可用，本次只用了关键词检索")
    single = _run(mcp_server.search_knowledge("压力", kb_ids=["wk_kb1"]))
    assert single.startswith("注意：向量检索不可用")


def test_mcp_search_error_keeps_the_backend_reason(client, providers):
    _answer(providers["wk"], "/v1/search",
            httpx.Response(503, json={"error": {"code": "search_unavailable", "message": "向量模型不可用"}}))
    text = _run(mcp_server.search_knowledge("压力", kb_ids=["wk_kb1"]))
    assert "（WeKnora 检索暂不可用：向量模型不可用）：wk_kb1（WeKnora）" in text and "当前不可用" not in text


def test_unlisted_backends_carry_what_a_page_needs_to_keep_a_selection(client):
    _stop(client)
    down = client.get("/v1/kbs", headers=auth()).json()["unavailable_backends"]
    assert down == [{"id": "wk", "label": "WeKnora", "type": "weknora", "error": "connection refused", "id_prefix": "wk_",
                     "knowledge_bases": [{"id": "wk_kb1", "name": "weknora 库"}]}]
