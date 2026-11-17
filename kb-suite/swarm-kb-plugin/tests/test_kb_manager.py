"""kb-manager plugin: the page's calls about declared backend mechanisms (contract section 9) reach the right
gateway routes, and browser input that could leave its URL segment is refused.

Runs without Swarm: the SDK import is stubbed, the gateway is an httpx mock transport.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "plugin" / "kb-manager" / "extension.py"


@pytest.fixture(scope="module")
def ext() -> Any:
    try:
        import jiuwenswarm.extensions.sdk  # noqa: F401 - the real one when Swarm is installed
    except ImportError:
        sdk = types.ModuleType("jiuwenswarm.extensions.sdk")
        sdk.ApplicationPluginExtension = object
        sdk.FrontendContribution = object
        for name in ("jiuwenswarm", "jiuwenswarm.extensions"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["jiuwenswarm.extensions.sdk"] = sdk
    spec = importlib.util.spec_from_file_location("kb_manager_extension", EXTENSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    """Records requests; ``routes`` maps (method, path) to a response factory."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(501, json={"error": {"code": "unsupported", "message": "不支持"}})
        return route(request)


@pytest.fixture()
def call(ext: Any) -> Any:
    gateway = Gateway()
    plugin = ext.KbManagerPlugin()
    plugin._client = httpx.AsyncClient(base_url="https://gw", transport=httpx.MockTransport(gateway.handler))

    def run(method: str, params: dict) -> Any:
        assert method in ext.METHODS
        return asyncio.run(plugin._dispatch(method, params))

    run.gateway = gateway
    return run


def test_action_forwards_params(call: Any) -> None:
    call.gateway.routes[("POST", "/v1/kbs/da-1/actions/rebuild_index")] = lambda r: httpx.Response(
        200, json={"job": {"id": "da-job-1", "status": "queued"}})
    assert call("actions.run", {"kb_id": "da-1", "action": "rebuild_index", "params": {"full": True}}) == {
        "job": {"id": "da-job-1", "status": "queued"}}
    assert json.loads(call.gateway.requests[-1].content) == {"params": {"full": True}}


def test_document_scope_wins_and_ids_are_checked(call: Any, ext: Any) -> None:
    call.gateway.routes[("POST", "/v1/documents/d1/actions/summarize")] = lambda r: httpx.Response(200, json={"result": 1})
    assert call("actions.run", {"kb_id": "k", "doc_id": "d1", "action": "summarize"}) == {"result": 1}
    for bad in ({"kb_id": "k", "action": "Rebuild"}, {"kb_id": "../kbs", "action": "a"}, {"kb_id": "a/b", "action": "a"},
                {"kb_id": "k", "action": "a", "params": [1]}):
        with pytest.raises(ext.RpcError) as err:
            call("actions.run", bad)
        assert err.value.code == "bad_request"


def test_unsupported_keeps_gateway_error(call: Any, ext: Any) -> None:
    with pytest.raises(ext.RpcError) as err:
        call("jobs.list", {"kb_id": "k"})
    assert (err.value.status, err.value.code) == (501, "unsupported")


def test_views_missing_markdown_and_json(call: Any) -> None:
    call.gateway.routes[("GET", "/v1/documents/d1/views/overview")] = lambda r: httpx.Response(
        200, text="# 概览", headers={"content-type": "text/markdown; charset=utf-8"})
    call.gateway.routes[("GET", "/v1/kbs/k/views/timeline")] = lambda r: httpx.Response(200, json={"items": []})
    call.gateway.routes[("GET", "/v1/kbs/k/views/quality")] = lambda r: httpx.Response(
        404, json={"error": {"code": "not_found", "message": "还没有"}})
    assert call("views.get", {"doc_id": "d1", "view": "overview"}) == {"media_type": "text/markdown", "content": "# 概览"}
    assert call("views.get", {"kb_id": "k", "view": "timeline"}) == {"media_type": "application/json", "content": {"items": []}}
    assert call("views.get", {"kb_id": "k", "view": "quality"}) == {"media_type": None, "content": None}


def test_jobs_and_settings(call: Any, ext: Any) -> None:
    call.gateway.routes[("GET", "/v1/jobs/da-j1")] = lambda r: httpx.Response(200, json={"id": "da-j1"})
    call.gateway.routes[("GET", "/v1/kbs/k/jobs")] = lambda r: httpx.Response(200, json={"items": []})
    call.gateway.routes[("PUT", "/v1/settings")] = lambda r: httpx.Response(200, json=json.loads(r.content))
    call("jobs.get", {"job_id": "da-j1", "report": True})
    assert call.gateway.requests[-1].url.params["report"] == "true"
    call("jobs.list", {"kb_id": "k", "status": "running"})
    assert call.gateway.requests[-1].url.params["status"] == "running"
    with pytest.raises(ext.RpcError):
        call("jobs.list", {"kb_id": "k", "status": "x;y"})
    assert call("settings.put", {"backend": "da", "values": {"api_key": "******"}}) == {"api_key": "******"}
    assert call.gateway.requests[-1].url.params["backend"] == "da"
    with pytest.raises(ext.RpcError):
        call("settings.get", {})


def test_file_range(call: Any, ext: Any) -> None:
    body = bytes(range(256)) * 4

    def ranged(request: httpx.Request) -> httpx.Response:
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        part = body[start:end + 1]
        return httpx.Response(206, content=part, headers={
            "content-type": "application/pdf", "content-range": f"bytes {start}-{start + len(part) - 1}/{len(body)}"})

    call.gateway.routes[("GET", "/v1/documents/d1/file")] = ranged
    res = call("docs.file_range", {"doc_id": "d1", "offset": 10, "length": 5})
    assert (res["offset"], res["total"], base64.b64decode(res["data"])) == (10, 1024, body[10:15])
    assert call.gateway.requests[-1].headers["range"] == "bytes=10-14"
    for bad in ({"doc_id": "d1", "length": ext.MAX_RANGE + 1}, {"doc_id": "d1", "offset": -1}):
        with pytest.raises(ext.RpcError):
            call("docs.file_range", bad)

    call.gateway.routes[("GET", "/v1/documents/d2/file")] = lambda r: httpx.Response(200, content=body)
    res = call("docs.file_range", {"doc_id": "d2", "offset": 1000, "length": 100})
    assert (res["offset"], res["total"], base64.b64decode(res["data"])) == (1000, 1024, body[1000:])


def test_file_range_without_range_support_reads_only_up_to_the_slice(call: Any) -> None:
    """A 200 answer is not buffered whole: past the slice the body is no longer read."""
    served: list[int] = []

    async def chunks():  # 64 chunks of 16 bytes, no content-length
        for i in range(64):
            served.append(i)
            yield bytes([i]) * 16

    call.gateway.routes[("GET", "/v1/documents/d3/file")] = lambda r: httpx.Response(200, content=chunks())
    res = call("docs.file_range", {"doc_id": "d3", "offset": 40, "length": 20})
    assert base64.b64decode(res["data"]) == bytes([2]) * 8 + bytes([3]) * 12
    assert res["total"] is None and len(served) < 10
    call.gateway.routes[("GET", "/v1/documents/d4/file")] = lambda r: httpx.Response(200, content=b"abc")
    res = call("docs.file_range", {"doc_id": "d4", "offset": 0, "length": 100})
    assert (res["total"], base64.b64decode(res["data"])) == (3, b"abc")


def test_export_and_cap(call: Any, ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    call.gateway.routes[("GET", "/v1/documents/d1/export/md")] = lambda r: httpx.Response(200, content=b"# x", headers={
        "content-type": "text/markdown", "content-disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.md"})
    res = call("docs.export", {"doc_id": "d1", "format": "md"})
    assert (res["file_name"], base64.b64decode(res["data"])) == ("报告.md", b"# x")
    with pytest.raises(ext.RpcError):
        call("docs.export", {"doc_id": "d1", "format": "../file"})
    monkeypatch.setattr(ext, "MAX_FILE_DOWNLOAD", 2)
    with pytest.raises(ext.RpcError) as err:
        call("docs.export", {"doc_id": "d1", "format": "md"})
    assert err.value.code == "too_large"


def test_folders(call: Any, ext: Any) -> None:
    call.gateway.routes[("GET", "/v1/kbs/k/documents")] = lambda r: httpx.Response(200, json={"items": [], "total": 0})
    call.gateway.routes[("DELETE", "/v1/kbs/k/folders")] = lambda r: httpx.Response(204)
    call("docs.list", {"kb_id": "k", "folder": "/a/b/"})
    assert call.gateway.requests[-1].url.params["folder"] == "a/b"
    call("docs.list", {"kb_id": "k"})
    assert "folder" not in call.gateway.requests[-1].url.params
    assert call("folders.delete", {"kb_id": "k", "path": "a/b"}) is None
    assert call.gateway.requests[-1].url.params["path"] == "a/b"
    for path in ("", "/", "a/../b", "a//b"):
        with pytest.raises(ext.RpcError):
            call("folders.delete", {"kb_id": "k", "path": path})


def test_evidence_keeps_colons(call: Any) -> None:
    call.gateway.routes[("GET", "/v1/evidence/d1:table:3")] = lambda r: httpx.Response(200, json={"anchor_id": "d1:table:3"})
    assert call("evidence.get", {"anchor_id": "d1:table:3"}) == {"anchor_id": "d1:table:3"}
