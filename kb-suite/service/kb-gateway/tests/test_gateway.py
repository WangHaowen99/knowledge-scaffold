from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from conftest import auth, make_client

from kb_gateway.auth import AuthError, KeyRing, parse_keys
from kb_gateway.config import ConfigError, parse_backends
from kb_gateway.ops import rrf_merge
from kb_gateway.registry import Registry


def _backend(**kw):
    return {"id": "a", "base_url": "https://a.example.com", **kw}


@pytest.mark.parametrize(
    "backends, message",
    [
        ([_backend(default=True), _backend(id="b", default=True, id_prefix="b_")], "exactly one"),
        ([_backend(default=True), _backend(id="b")], "empty id_prefix"),
        ([_backend(default=True), _backend(id="b", id_prefix="b:")], "ending in '_'"),
        # a bare hex character would capture the default backend's UUIDs
        ([_backend(default=True), _backend(id="b", id_prefix="a")], "ending in '_'"),
        ([_backend(default=True, optional=True)], "cannot be optional"),
        ([_backend(default=True), _backend(id="b", id_prefix="x_"), _backend(id="c", id_prefix="x_")], "duplicate id prefixes"),
        ([], "no backends"),
    ],
)
def test_backend_config_is_validated(backends, message):
    with pytest.raises(ConfigError, match=message):
        parse_backends({"backends": backends})


def test_keys_reference_env_vars_and_check_capabilities():
    ring = KeyRing(parse_keys("p=P:retrieve", {"P": "secret"}))
    assert ring.authenticate("secret").capabilities == {"retrieve"}
    with pytest.raises(AuthError):
        ring.authenticate("other")
    with pytest.raises(ValueError, match="empty"):
        parse_keys("p=MISSING:retrieve", {})
    with pytest.raises(ValueError, match="unknown"):
        parse_keys("p=P:root", {"P": "x"})


def test_requests_need_a_key_with_the_capability(client):
    assert client.get("/v1/kbs").status_code == 401
    assert client.get("/v1/kbs", headers=auth("wrong")).status_code == 401
    assert client.post("/v1/kbs", headers=auth("mcp-key"), json={"name": "x"}).status_code == 403


def test_kb_list_merges_backends(client):
    data = client.get("/v1/kbs", headers=auth()).json()
    assert {kb["id"]: kb["backend"]["id"] for kb in data["items"]} == {"kb1": "kbs", "wk_kb1": "wk"}
    assert data["unavailable_backends"] == []
    backends = {b["id"]: b for b in client.get("/v1/backends", headers=auth()).json()["items"]}
    assert backends["wk"]["type"] == "weknora" and backends["wk"]["healthy"]


def test_requests_route_by_id_prefix_with_backend_credentials(client, providers):
    assert client.get("/v1/documents/wk_d1", headers=auth()).json()["title"] == "weknora 文档"
    assert client.get("/v1/documents/d1", headers=auth()).json()["title"] == "kb-service 文档"
    method, path, meta = providers["wk"].calls[-1]
    assert (method, path) == ("GET", "/v1/documents/wk_d1")
    assert meta["headers"]["authorization"] == "Bearer wk-backend-key"


def test_create_kb_on_a_chosen_backend(client, providers):
    kb = client.post("/v1/kbs", headers=auth(), json={"name": "新库", "backend": "wk"}).json()
    assert kb["backend"]["id"] == "wk"
    assert providers["wk"].calls[-1][2]["body"] == {"name": "新库"}


def test_unsupported_feature_is_501(client):
    r = client.post("/v1/documents/wk_d1/questions/regenerate", headers=auth())
    assert r.status_code == 501 and r.json()["error"]["code"] == "unsupported"


def test_backend_without_uploads_gets_501(env, providers, tmp_path, monkeypatch):
    providers["wk"].features.remove("uploads")
    with make_client(env, providers, tmp_path, monkeypatch) as c:
        r = c.post("/v1/uploads", headers=auth(), json={"kb_id": "wk_kb1", "file_name": "a", "file_size": 3})
        assert r.status_code == 501


def test_readonly_backend_rejects_writes(env, providers, tmp_path, monkeypatch):
    providers["ro"] = type(providers["wk"])("weknora", "ro_", ["kbs.write", "ingest.file"])
    with make_client(env, providers, tmp_path, monkeypatch,
                     [{"id": "ro", "base_url": "https://ro.example.com", "id_prefix": "ro_", "readonly": True}]) as c:
        r = c.post("/v1/kbs", headers=auth(), json={"name": "x", "backend": "ro"})
        assert r.status_code == 501
        assert c.get("/v1/kbs/ro_kb1", headers=auth()).json()["features"] == []


def test_enhancement_services_are_not_attached_yet(env, providers, tmp_path, monkeypatch):
    providers["enh"] = type(providers["wk"])("enh", "enh_", [], role="enhancement")
    with make_client(env, providers, tmp_path, monkeypatch,
                     [{"id": "enh", "base_url": "https://enh.example.com", "id_prefix": "enh_"}]) as c:
        backends = {b["id"]: b for b in c.get("/v1/backends", headers=auth()).json()["items"]}
        assert not backends["enh"]["healthy"] and "增强服务" in backends["enh"]["error"]
        assert c.get("/v1/kbs", headers=auth()).json()["unavailable_backends"][0]["id"] == "enh"


def test_search_across_backends_fuses_ranks(client):
    data = client.post("/v1/search", headers=auth(), json={"query": "压力", "top_k": 5}).json()
    assert {h["backend"] for h in data["items"]} == {"kbs", "wk"}
    assert all("fused_score" in h for h in data["items"])
    one = client.post("/v1/search", headers=auth(), json={"query": "压力", "kb_ids": ["wk_kb1"]}).json()
    assert {h["backend"] for h in one["items"]} == {"wk"} and "fused_score" not in one["items"][0]


def test_search_reports_a_failing_backend(client, providers):
    providers["wk"].fail_search = True
    data = client.post("/v1/search", headers=auth(), json={"query": "x"}).json()
    assert data["failed_backends"][0]["id"] == "wk"
    assert {h["backend"] for h in data["items"]} == {"kbs"}


def test_rrf_interleaves_by_rank():
    merged = rrf_merge([[{"id": "a1"}, {"id": "a2"}], [{"id": "b1"}]], 3)
    assert [m["id"] for m in merged] == ["a1", "b1", "a2"]


def test_chunked_upload_is_forwarded_to_the_owning_backend(client, providers):
    data = b"hello " * 100
    begin = client.post("/v1/uploads", headers=auth(), json={
        "kb_id": "wk_kb1", "file_name": "x.md", "file_size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
    }).json()
    # the session lives on the backend, so its id routes there
    assert begin["upload_id"] == "wk_up1"
    assert client.put("/v1/uploads/wk_up1/parts/0", headers=auth(), content=data).status_code == 200
    doc = client.post("/v1/uploads/wk_up1/complete", headers=auth())
    assert doc.status_code == 201 and doc.json()["kb_id"] == "wk_kb1"
    assert providers["wk"].parts[0] == data and not providers["kbs"].parts


def test_wiki_routes_need_wiki_features(client):
    assert client.get("/v1/kbs/wk_kb1/wiki/pages", headers=auth()).json()["total"] == 1
    assert client.put("/v1/kbs/wk_kb1/wiki/page?slug=a", headers=auth(), json={}).status_code == 501


def test_config_meta_is_backend_specific(client):
    assert client.get("/v1/config/meta?backend=wk", headers=auth()).status_code == 501
    assert client.get("/v1/config/meta?backend=nope", headers=auth()).status_code == 404


def test_json_error_shape(client):
    r = client.get("/v1/documents/wk_missing", headers=auth())
    assert r.status_code == 404 and r.json()["error"] == {"code": "not_found", "message": "no doc", "backend": "wk"}
    assert json.loads(client.get("/healthz").content)["backends"] == {"kbs": "ok", "wk": "ok"}


def test_structure_and_grep_are_forwarded(client, providers):
    win = client.get("/v1/documents/wk_d1/structure", headers=auth(), params={"heading": "巡检"}).json()
    assert win["content"].startswith("## 巡检") and providers["wk"].calls[-1][1] == "/v1/documents/wk_d1/structure"
    data = client.post("/v1/grep", headers=auth(), json={"pattern": "每"}).json()
    assert {m["doc_id"] for m in data["matches"]} == {"d1", "wk_d1"} and data["total_documents"] == 2


def test_search_skips_an_unavailable_backend_in_scope(client):
    client.app.state.gateway.registry.by_id["wk"].healthy = False
    data = client.post("/v1/search", headers=auth(), json={"query": "x", "kb_ids": ["kb1", "wk_kb1"]}).json()
    assert {h["backend"] for h in data["items"]} == {"kbs"}
    assert data["failed_backends"][0]["id"] == "wk"
    assert client.post("/v1/search", headers=auth(), json={"query": "x", "kb_ids": ["wk_kb1"]}).status_code == 503


def test_search_trace_is_passed_through(client):
    data = client.post("/v1/search", headers=auth(), json={"query": "x", "kb_ids": ["kb1"], "trace": True}).json()
    assert data["trace"]["counts"]["returned"] == len(data["items"])


def test_backend_with_non_json_capabilities_is_unavailable_not_fatal(env, providers, tmp_path, monkeypatch):
    real = providers["wk"].handle

    def html_page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not a backend</html>")

    providers["wk"].handle = html_page
    with make_client(env, providers, tmp_path, monkeypatch) as c:
        backends = {b["id"]: b for b in c.get("/v1/backends", headers=auth()).json()["items"]}
        assert not backends["wk"]["healthy"] and "JSON" in backends["wk"]["error"]
        providers["wk"].handle = real
        asyncio.run(c.app.state.gateway.registry.refresh())
        assert c.app.state.gateway.registry.by_id["wk"].healthy


def test_refresh_loop_survives_a_failing_round():
    calls = []

    class Flaky(Registry):
        async def refresh(self) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")

    async def run() -> bool:
        registry = Flaky.__new__(Flaky)
        task = asyncio.create_task(registry._loop(0.001))
        while len(calls) < 3:
            await asyncio.sleep(0.001)
        alive = not task.done()
        task.cancel()
        return alive

    assert asyncio.run(run())


def test_optional_backend_is_hidden_until_it_answers(env, providers, tmp_path, monkeypatch):
    providers["opt"] = type(providers["wk"])("weknora", "opt_", ["kbs.write"])
    providers["opt"].down = True
    extra = [{"id": "opt", "base_url": "https://opt.example.com", "id_prefix": "opt_", "optional": True}]
    with make_client(env, providers, tmp_path, monkeypatch, extra) as c:
        registry = c.app.state.gateway.registry
        assert "opt" not in {b["id"] for b in c.get("/v1/backends", headers=auth()).json()["items"]}
        assert c.get("/v1/kbs", headers=auth()).json()["unavailable_backends"] == []
        assert "opt" not in c.get("/healthz").json()["backends"]
        providers["opt"].down = False
        asyncio.run(registry.refresh())
        assert "opt_kb1" in {kb["id"] for kb in c.get("/v1/kbs", headers=auth()).json()["items"]}
        # once seen, going away again is reported
        providers["opt"].down = True
        asyncio.run(registry.refresh())
        assert c.get("/v1/kbs", headers=auth()).json()["unavailable_backends"][0]["id"] == "opt"


def test_readonly_backend_rejects_chunked_uploads(env, providers, tmp_path, monkeypatch):
    providers["ro"] = type(providers["wk"])("weknora", "ro_", ["uploads"])
    with make_client(env, providers, tmp_path, monkeypatch,
                     [{"id": "ro", "base_url": "https://ro.example.com", "id_prefix": "ro_", "readonly": True}]) as c:
        r = c.post("/v1/uploads", headers=auth(), json={"kb_id": "ro_kb1", "file_name": "a", "file_size": 3})
        assert r.status_code == 501
        assert not providers["ro"].uploads


def test_wiki_route_forwards_only_contract_endpoints(client, providers):
    before = len(providers["wk"].calls)
    r = client.get("/v1/kbs/wk_kb1/wiki/%2e%2e/%2e%2e/%2e%2e/documents/wk_d1/content", headers=auth("mcp-key"))
    assert r.status_code == 404
    r = client.get("/v1/kbs/wk_kb1/wiki/rebuild", headers=auth())
    assert r.status_code == 404
    assert len(providers["wk"].calls) == before


def test_file_download_is_relayed(client):
    r = client.get("/v1/documents/wk_d1/file", headers=auth())
    assert r.status_code == 200 and r.content == b"%PDF-synthetic"
    assert r.headers["content-disposition"] == 'attachment; filename="a.pdf"'
