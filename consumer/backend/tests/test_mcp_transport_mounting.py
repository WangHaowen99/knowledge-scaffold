import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

from integration.mcp.config import MCPRuntimeConfig
from api.protocols.mcp import server as mcp_server
from integration.mcp.scope import MCPRequestScope
from integration.mcp.service_catalog import list_mcp_service_cards
from integration.mcp.token_service import MCPTokenAuthError


ENDPOINT_ID = "cdd181f5b6cb47a9"
OTHER_ENDPOINT_ID = "a" * 16


def _config(tmp_path: Path, transport: str) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="http://mcp.example",
        agentic_base_url="",
        token_secret="test-secret-at-least-32-characters-long",
        token_algorithm="HS256",
        token_issuer="meta-wise",
        token_audience="meta-wise-mcp",
        token_ttl_seconds=3600,
        token_max_ttl_seconds=86400,
        http_timeout_seconds=30,
        transport=transport,
        token_store_path=tmp_path / "tokens.json",
    )


def test_search_tool_schemas_make_ontology_id_optional(tmp_path):
    async def load_schemas():
        server = mcp_server.create_mcp_server(
            _config(tmp_path, "streamable-http"),
            toolset="search",
        )
        return {
            name: tool.parameters
            for name, tool in (await server.get_tools()).items()
        }

    schemas = asyncio.run(load_schemas())

    assert len(schemas) == 14
    for schema in schemas.values():
        assert "ontology_id" in schema["properties"]
        assert "ontology_id" not in schema.get("required", [])


def test_mcp_http_root_app_mounts_only_streamable_http_when_transport_is_http(monkeypatch, tmp_path):
    captured_transports: list[str] = []

    class FakeMcpServer:
        def http_app(self, *, path: str, transport: str):
            captured_transports.append(transport)
            return Starlette()

    monkeypatch.setattr(
        mcp_server,
        "load_mcp_service_catalog",
        lambda: [
            SimpleNamespace(service_id="meta-wise-search", toolset="search", endpoint_prefix="/search"),
            SimpleNamespace(service_id="meta-wise-chat", toolset="chat", endpoint_prefix="/chat"),
        ],
    )
    monkeypatch.setattr(mcp_server, "create_mcp_server", lambda *_args, **_kwargs: FakeMcpServer())

    app = mcp_server.create_mcp_http_root_app(_config(tmp_path, "streamable-http"))
    route_paths = {route.path for route in app.routes}

    assert "/{mcp_endpoint_id}/search/mcp" in route_paths
    assert "/{mcp_endpoint_id}/chat/mcp" in route_paths
    assert "/{mcp_endpoint_id}/search/sse" not in route_paths
    assert "/{mcp_endpoint_id}/chat/sse" not in route_paths
    assert "/search/mcp" not in route_paths
    assert "/chat/mcp" not in route_paths
    assert captured_transports == ["streamable-http", "streamable-http"]


def test_mcp_http_root_app_mounts_only_sse_when_transport_is_sse(monkeypatch, tmp_path):
    captured_transports: list[str] = []

    class FakeMcpServer:
        def http_app(self, *, path: str, transport: str):
            captured_transports.append(transport)
            return Starlette()

    monkeypatch.setattr(
        mcp_server,
        "load_mcp_service_catalog",
        lambda: [
            SimpleNamespace(service_id="meta-wise-search", toolset="search", endpoint_prefix="/search"),
            SimpleNamespace(service_id="meta-wise-chat", toolset="chat", endpoint_prefix="/chat"),
        ],
    )
    monkeypatch.setattr(mcp_server, "create_mcp_server", lambda *_args, **_kwargs: FakeMcpServer())

    app = mcp_server.create_mcp_http_root_app(_config(tmp_path, "sse"))
    route_paths = {route.path for route in app.routes}

    assert "/{mcp_endpoint_id}/search/sse" in route_paths
    assert "/{mcp_endpoint_id}/chat/sse" in route_paths
    assert "/{mcp_endpoint_id}/search/mcp" not in route_paths
    assert "/{mcp_endpoint_id}/chat/mcp" not in route_paths
    assert "/search/sse" not in route_paths
    assert "/chat/sse" not in route_paths
    assert captured_transports == ["sse", "sse"]


def test_mcp_service_catalog_reports_only_exposed_transport_urls(tmp_path):
    http_cards = list_mcp_service_cards(
        _config(tmp_path, "streamable-http"),
        mcp_endpoint_id=ENDPOINT_ID,
    )
    http_search = next(card for card in http_cards if card["toolset"] == "search")
    assert http_search["active_url"] == "http://mcp.example/{}/search/mcp".format(ENDPOINT_ID)
    assert http_search["streamable_http_url"] == "http://mcp.example/{}/search/mcp".format(ENDPOINT_ID)
    assert http_search["mcp_url"] == "http://mcp.example/{}/search/mcp".format(ENDPOINT_ID)
    assert http_search["sse_url"] == ""
    assert http_search["sse_enabled"] is False

    sse_cards = list_mcp_service_cards(
        _config(tmp_path, "sse"),
        mcp_endpoint_id=ENDPOINT_ID,
    )
    sse_search = next(card for card in sse_cards if card["toolset"] == "search")
    assert sse_search["active_url"] == "http://mcp.example/{}/search/sse".format(ENDPOINT_ID)
    assert sse_search["streamable_http_url"] == ""
    assert sse_search["mcp_url"] == ""
    assert sse_search["sse_url"] == "http://mcp.example/{}/search/sse".format(ENDPOINT_ID)
    assert sse_search["sse_enabled"] is True


def test_mcp_http_root_app_mounts_all_four_endpoints_for_both_transport(monkeypatch, tmp_path):
    captured_transports: list[str] = []

    class FakeMcpServer:
        def http_app(self, *, path: str, transport: str):
            captured_transports.append(transport)
            return Starlette()

    monkeypatch.setattr(
        mcp_server,
        "load_mcp_service_catalog",
        lambda: [
            SimpleNamespace(service_id="meta-wise-search", toolset="search", endpoint_prefix="/search"),
            SimpleNamespace(service_id="meta-wise-chat", toolset="chat", endpoint_prefix="/chat"),
        ],
    )
    monkeypatch.setattr(mcp_server, "create_mcp_server", lambda *_args, **_kwargs: FakeMcpServer())

    cfg = MCPRuntimeConfig(**{**_config(tmp_path, "streamable-http").__dict__, "transport": "both"})
    app = mcp_server.create_mcp_http_root_app(cfg)
    route_paths = {route.path for route in app.routes}

    assert {
        "/{mcp_endpoint_id}/search/mcp",
        "/{mcp_endpoint_id}/chat/mcp",
        "/{mcp_endpoint_id}/search/sse",
        "/{mcp_endpoint_id}/chat/sse",
        "/health",
    } <= route_paths
    assert {"/search/mcp", "/chat/mcp", "/search/sse", "/chat/sse"}.isdisjoint(route_paths)
    assert captured_transports.count("streamable-http") == 2
    assert captured_transports.count("sse") == 2

    cards = list_mcp_service_cards(cfg, mcp_endpoint_id=ENDPOINT_ID)
    search = next(card for card in cards if card["toolset"] == "search")
    assert search["streamable_http_url"] == "http://mcp.example/{}/search/mcp".format(ENDPOINT_ID)
    assert search["sse_url"] == "http://mcp.example/{}/search/sse".format(ENDPOINT_ID)


def test_real_fastmcp_streamable_http_preserves_dynamic_endpoint_context(
    monkeypatch,
    tmp_path,
):
    resolved_endpoint_ids: list[str] = []

    monkeypatch.setattr(
        mcp_server,
        "load_mcp_service_catalog",
        lambda: [
            SimpleNamespace(
                service_id="meta-wise-chat",
                toolset="chat",
                endpoint_prefix="/chat",
            ),
        ],
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_headers_best_effort",
        lambda: {"authorization": "Bearer mcp-token"},
    )

    def resolve_scope(
        _token,
        *,
        required_scope,
        traceparent,
        mcp_endpoint_id,
    ):
        assert required_scope == "mcp:chat"
        assert traceparent == ""
        resolved_endpoint_ids.append(mcp_endpoint_id)
        return MCPRequestScope(
            username="alice",
            workspace_id="workspace-a",
            scopes=["mcp:chat"],
        )

    monkeypatch.setattr(mcp_server, "resolve_mcp_scope_from_token", resolve_scope)
    monkeypatch.setattr(
        mcp_server,
        "execute_chat_tool",
        lambda _args, *, scope: {
            "ok": True,
            "workspace_id": scope.workspace_id,
            "endpoint_id": resolved_endpoint_ids[-1],
        },
    )
    app = mcp_server.create_mcp_http_root_app(
        _config(tmp_path, "streamable-http"),
    )

    def jsonrpc_payload(response):
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            data_line = next(
                line for line in response.text.splitlines() if line.startswith("data: ")
            )
            return json.loads(data_line.removeprefix("data: "))
        return response.json() if response.content else None

    async def exercise_app():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                follow_redirects=False,
            ) as client:
                old_route = await client.post("/chat/mcp")
                invalid_route = await client.post("/invalid/chat/mcp")
                assert old_route.status_code == 404
                assert invalid_route.status_code == 404

                endpoint_path = "/{}/chat/mcp".format(ENDPOINT_ID)
                headers = {
                    "accept": "application/json, text/event-stream",
                    "content-type": "application/json",
                }
                initialized = await client.post(
                    endpoint_path,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200
                session_id = initialized.headers["mcp-session-id"]
                headers["mcp-session-id"] = session_id
                cross_endpoint = await client.post(
                    "/{}/chat/mcp".format(OTHER_ENDPOINT_ID),
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert cross_endpoint.status_code == 404
                assert cross_endpoint.json()["error"]["message"] == "Session not found"
                notification = await client.post(
                    endpoint_path,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                )
                assert notification.status_code == 202

                tools_response = await client.post(
                    endpoint_path,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                tools_payload = jsonrpc_payload(tools_response)
                assert [
                    item["name"] for item in tools_payload["result"]["tools"]
                ] == ["chat"]

                call_response = await client.post(
                    endpoint_path,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "chat",
                            "arguments": {"query": "hello"},
                        },
                    },
                )
                call_payload = jsonrpc_payload(call_response)
                assert call_payload["result"]["structuredContent"][
                    "endpoint_id"
                ] == ENDPOINT_ID

                terminated = await client.delete(endpoint_path, headers=headers)
                assert terminated.status_code == 200
            assert mcp_server.get_bound_mcp_endpoint_id() == ""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(exercise_app())
    finally:
        pending_tasks = asyncio.all_tasks(loop)
        for task in pending_tasks:
            task.cancel()
        loop.run_until_complete(
            asyncio.gather(*pending_tasks, return_exceptions=True),
        )
        loop.close()
        asyncio.set_event_loop(None)

    assert resolved_endpoint_ids == [ENDPOINT_ID]


def test_workspace_endpoint_binding_app_validates_and_resets_context():
    seen_endpoint_ids: list[str] = []
    seen_state_endpoint_ids: list[str] = []

    async def child_app(scope, receive, send):
        from starlette.responses import JSONResponse

        seen_endpoint_ids.append(mcp_server.get_bound_mcp_endpoint_id())
        seen_state_endpoint_ids.append(
            scope["state"][mcp_server._MCP_ENDPOINT_STATE_KEY],
        )
        await JSONResponse({"ok": True})(scope, receive, send)

    binding_app = mcp_server._WorkspaceEndpointBindingApp(
        child_app,
        session_bindings=mcp_server._WorkspaceEndpointSessionBindings(),
    )

    async def invoke(endpoint_id):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await binding_app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
                "path_params": {"mcp_endpoint_id": endpoint_id},
                "state": {mcp_server._MCP_ENDPOINT_STATE_KEY: "f" * 16},
            },
            receive,
            send,
        )
        return next(
            message["status"]
            for message in messages
            if message["type"] == "http.response.start"
        )

    async def exercise_binding():
        assert await invoke(ENDPOINT_ID) == 200
        assert mcp_server.get_bound_mcp_endpoint_id() == ""
        assert await invoke("a" * 16) == 200
        assert mcp_server.get_bound_mcp_endpoint_id() == ""
        assert await invoke("invalid") == 404
        assert mcp_server.get_bound_mcp_endpoint_id() == ""

    asyncio.run(exercise_binding())

    assert seen_endpoint_ids == [ENDPOINT_ID, "a" * 16]
    assert seen_state_endpoint_ids == [ENDPOINT_ID, "a" * 16]


def test_workspace_endpoint_binding_app_pins_and_releases_http_session():
    seen_endpoint_ids: list[str] = []
    bindings = mcp_server._WorkspaceEndpointSessionBindings()

    async def child_app(scope, receive, send):
        from starlette.responses import JSONResponse

        seen_endpoint_ids.append(
            scope["state"][mcp_server._MCP_ENDPOINT_STATE_KEY],
        )
        response = JSONResponse({"ok": True})
        if not mcp_server._request_mcp_session_id(scope):
            response.headers["mcp-session-id"] = "session-a"
        await response(scope, receive, send)

    binding_app = mcp_server._WorkspaceEndpointBindingApp(
        child_app,
        session_bindings=bindings,
    )

    async def invoke(endpoint_id, *, method="POST", session_id=""):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        headers = []
        if session_id:
            headers.append((b"mcp-session-id", session_id.encode("ascii")))
        await binding_app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": headers,
                "client": ("test", 1),
                "server": ("test", 80),
                "path_params": {"mcp_endpoint_id": endpoint_id},
            },
            receive,
            send,
        )
        start = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        return start["status"]

    async def exercise_binding():
        assert await invoke(ENDPOINT_ID) == 200
        assert await invoke(
            OTHER_ENDPOINT_ID,
            session_id="session-a",
        ) == 404
        assert await invoke(
            ENDPOINT_ID,
            method="DELETE",
            session_id="session-a",
        ) == 200
        assert await invoke(
            OTHER_ENDPOINT_ID,
            session_id="session-a",
        ) == 200

    asyncio.run(exercise_binding())

    assert seen_endpoint_ids == [
        ENDPOINT_ID,
        ENDPOINT_ID,
        OTHER_ENDPOINT_ID,
    ]


def test_workspace_endpoint_session_bindings_expire_after_idle_timeout():
    now = [0.0]
    bindings = mcp_server._WorkspaceEndpointSessionBindings(
        idle_timeout_seconds=10,
        max_active_sessions=2,
        clock=lambda: now[0],
    )

    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True
    assert bindings.active_count() == 1

    now[0] = 9
    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True
    now[0] = 18
    assert bindings.expire_idle() == 0
    assert bindings.active_count() == 1

    now[0] = 19
    assert bindings.expire_idle() == 1
    assert bindings.active_count() == 0


def test_workspace_endpoint_session_bindings_expire_in_last_seen_order():
    now = [0.0]
    bindings = mcp_server._WorkspaceEndpointSessionBindings(
        idle_timeout_seconds=10,
        max_active_sessions=3,
        clock=lambda: now[0],
    )

    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True
    now[0] = 1
    assert bindings.bind_or_validate("session-b", ENDPOINT_ID) is True
    now[0] = 5
    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True

    now[0] = 11
    assert bindings.expire_idle() == 1
    assert bindings.active_count() == 1
    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True


def test_workspace_endpoint_session_bindings_reject_capacity_without_eviction():
    bindings = mcp_server._WorkspaceEndpointSessionBindings(
        idle_timeout_seconds=60,
        max_active_sessions=1,
    )

    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True
    with pytest.raises(mcp_server._MCPActiveSessionLimitError):
        bindings.reserve_new_session(ENDPOINT_ID)
    with pytest.raises(mcp_server._MCPActiveSessionLimitError):
        bindings.bind_or_validate("session-b", ENDPOINT_ID)

    assert bindings.bind_or_validate("session-a", OTHER_ENDPOINT_ID) is False
    assert bindings.active_count() == 1


def test_workspace_endpoint_binding_app_returns_503_at_session_capacity():
    child_calls: list[str] = []
    bindings = mcp_server._WorkspaceEndpointSessionBindings(
        idle_timeout_seconds=60,
        max_active_sessions=1,
    )
    assert bindings.bind_or_validate("session-a", ENDPOINT_ID) is True

    async def child_app(scope, receive, send):
        child_calls.append(scope["path"])

    binding_app = mcp_server._WorkspaceEndpointBindingApp(
        child_app,
        session_bindings=bindings,
    )

    async def exercise_binding():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await binding_app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
                "path_params": {"mcp_endpoint_id": ENDPOINT_ID},
            },
            receive,
            send,
        )
        start = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        body = next(
            message["body"]
            for message in messages
            if message["type"] == "http.response.body"
        )
        assert start["status"] == 503
        assert json.loads(body)["error"]["message"] == (
            "Server session capacity reached"
        )

    asyncio.run(exercise_binding())
    assert child_calls == []


def test_workspace_endpoint_binding_tracks_session_from_error_response():
    bindings = mcp_server._WorkspaceEndpointSessionBindings(
        idle_timeout_seconds=60,
        max_active_sessions=1,
    )

    async def child_app(scope, receive, send):
        from starlette.responses import JSONResponse

        response = JSONResponse({"error": "bad request"}, status_code=400)
        response.headers["mcp-session-id"] = "session-from-error"
        await response(scope, receive, send)

    binding_app = mcp_server._WorkspaceEndpointBindingApp(
        child_app,
        session_bindings=bindings,
    )

    async def exercise_binding():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await binding_app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("test", 1),
                "server": ("test", 80),
                "path_params": {"mcp_endpoint_id": ENDPOINT_ID},
            },
            receive,
            send,
        )
        start = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        assert start["status"] == 400

    asyncio.run(exercise_binding())

    assert bindings.active_count() == 1
    with pytest.raises(mcp_server._MCPActiveSessionLimitError):
        bindings.reserve_new_session(ENDPOINT_ID)


def test_configure_streamable_http_session_idle_timeout():
    session_manager = SimpleNamespace(session_idle_timeout=None)
    app = SimpleNamespace(
        routes=[SimpleNamespace(app=SimpleNamespace(session_manager=session_manager))],
    )

    mcp_server._configure_streamable_http_session_idle_timeout(
        app,
        mcp_server._MCP_SESSION_IDLE_TIMEOUT_SECONDS,
    )

    assert session_manager.session_idle_timeout == 1800.0


def test_resolve_scope_prefers_current_http_request_endpoint(monkeypatch, tmp_path):
    captured_endpoint_ids: list[str] = []

    monkeypatch.setattr(
        mcp_server,
        "_get_headers_best_effort",
        lambda: {"authorization": "Bearer mcp-token"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_request_mcp_endpoint_id_best_effort",
        lambda: ENDPOINT_ID,
    )

    def resolve_scope(
        _token,
        *,
        required_scope,
        traceparent,
        mcp_endpoint_id,
    ):
        assert required_scope == "mcp:chat"
        assert traceparent == ""
        captured_endpoint_ids.append(mcp_endpoint_id)
        return MCPRequestScope(
            username="alice",
            workspace_id="workspace-a",
            scopes=["mcp:chat"],
        )

    monkeypatch.setattr(mcp_server, "resolve_mcp_scope_from_token", resolve_scope)

    with mcp_server.bind_mcp_endpoint_id(OTHER_ENDPOINT_ID):
        scope = mcp_server._resolve_scope(
            _config(tmp_path, "streamable-http"),
            required_scope="mcp:chat",
        )

    assert scope.workspace_id == "workspace-a"
    assert captured_endpoint_ids == [ENDPOINT_ID]


def test_current_http_request_without_endpoint_state_fails_closed(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_load_get_http_request",
        lambda: lambda: SimpleNamespace(scope={"state": {}}),
    )

    with pytest.raises(MCPTokenAuthError) as exc_info:
        mcp_server._get_request_mcp_endpoint_id_best_effort()

    assert exc_info.value.code == "MCP_WORKSPACE_ENDPOINT_INVALID"


def test_real_fastmcp_sse_uses_dynamic_message_path_and_endpoint_context(
    monkeypatch,
    tmp_path,
):
    resolved_endpoint_ids: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "load_mcp_service_catalog",
        lambda: [
            SimpleNamespace(
                service_id="meta-wise-chat",
                toolset="chat",
                endpoint_prefix="/chat",
            ),
        ],
    )
    monkeypatch.setattr(
        mcp_server,
        "_get_headers_best_effort",
        lambda: {"authorization": "Bearer mcp-token"},
    )

    def resolve_scope(
        _token,
        *,
        required_scope,
        traceparent,
        mcp_endpoint_id,
    ):
        assert required_scope == "mcp:chat"
        assert traceparent == ""
        resolved_endpoint_ids.append(mcp_endpoint_id)
        return MCPRequestScope(
            username="alice",
            workspace_id="workspace-a",
            scopes=["mcp:chat"],
        )

    monkeypatch.setattr(mcp_server, "resolve_mcp_scope_from_token", resolve_scope)
    monkeypatch.setattr(
        mcp_server,
        "execute_chat_tool",
        lambda _args, *, scope: {
            "ok": True,
            "workspace_id": scope.workspace_id,
            "endpoint_id": resolved_endpoint_ids[-1],
        },
    )
    app = mcp_server.create_mcp_http_root_app(_config(tmp_path, "sse"))

    async def exercise_sse():
        receive_queue: asyncio.Queue[dict] = asyncio.Queue()
        send_queue: asyncio.Queue[dict] = asyncio.Queue()
        event_buffer = ""
        await receive_queue.put({
            "type": "http.request",
            "body": b"",
            "more_body": False,
        })

        async def receive():
            return await receive_queue.get()

        async def send(message):
            await send_queue.put(message)

        async def next_sse_event():
            nonlocal event_buffer
            while True:
                separator = re.search(r"\r?\n\r?\n", event_buffer)
                if separator is not None:
                    raw_event = event_buffer[:separator.start()]
                    event_buffer = event_buffer[separator.end():]
                    event_match = re.search(
                        r"event: ([^\r\n]+)\r?\ndata: ([^\r\n]+)",
                        raw_event,
                    )
                    if event_match is not None:
                        return event_match.group(1), event_match.group(2)
                message = await asyncio.wait_for(send_queue.get(), timeout=5)
                if message["type"] == "http.response.body" and message.get("body"):
                    event_buffer += message["body"].decode("utf-8")

        sse_path = "/{}/chat/sse".format(ENDPOINT_ID)
        sse_task = asyncio.create_task(
            app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "GET",
                    "scheme": "http",
                    "path": sse_path,
                    "raw_path": sse_path.encode("ascii"),
                    "root_path": "",
                    "query_string": b"",
                    "headers": [
                        (b"accept", b"text/event-stream"),
                        (b"host", b"testserver"),
                    ],
                    "client": ("test", 1),
                    "server": ("testserver", 80),
                },
                receive,
                send,
            ),
        )
        try:
            event_name, message_url = await next_sse_event()
            assert event_name == "endpoint"
            assert message_url.startswith(
                "/{}/chat/sse/messages/?session_id=".format(ENDPOINT_ID),
            )

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                headers = {"content-type": "application/json"}
                initialized = await client.post(
                    message_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 202
                event_name, event_data = await next_sse_event()
                assert event_name == "message"
                assert json.loads(event_data)["id"] == 1

                other_message_url = message_url.replace(
                    "/{}/".format(ENDPOINT_ID),
                    "/{}/".format(OTHER_ENDPOINT_ID),
                    1,
                )
                cross_endpoint = await client.post(
                    other_message_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                )
                assert cross_endpoint.status_code == 404
                assert cross_endpoint.json()["error"]["message"] == "Session not found"

                notification = await client.post(
                    message_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                )
                assert notification.status_code == 202

                tools_response = await client.post(
                    message_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {},
                    },
                )
                assert tools_response.status_code == 202
                event_name, event_data = await next_sse_event()
                tools_payload = json.loads(event_data)
                assert event_name == "message"
                assert [
                    item["name"] for item in tools_payload["result"]["tools"]
                ] == ["chat"]

                call_response = await client.post(
                    message_url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "chat",
                            "arguments": {"query": "hello"},
                        },
                    },
                )
                assert call_response.status_code == 202
                event_name, event_data = await next_sse_event()
                call_payload = json.loads(event_data)
                assert event_name == "message"
                assert call_payload["result"]["structuredContent"][
                    "endpoint_id"
                ] == ENDPOINT_ID
        finally:
            await receive_queue.put({"type": "http.disconnect"})
            await asyncio.wait_for(sse_task, timeout=5)

        assert mcp_server.get_bound_mcp_endpoint_id() == ""
        registries = app.state.mcp_session_binding_registries
        assert len(registries) == 1
        assert registries[0].active_count() == 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_with_lifespan():
        async with app.router.lifespan_context(app):
            await exercise_sse()

    try:
        loop.run_until_complete(run_with_lifespan())
    finally:
        pending_tasks = asyncio.all_tasks(loop)
        for task in pending_tasks:
            task.cancel()
        loop.run_until_complete(
            asyncio.gather(*pending_tasks, return_exceptions=True),
        )
        loop.close()
        asyncio.set_event_loop(None)

    assert resolved_endpoint_ids == [ENDPOINT_ID]


def test_run_server_runs_startup_migrations_before_listening(monkeypatch, tmp_path):
    calls: list[str] = []
    config = _config(tmp_path, "streamable-http")
    fake_app = object()

    monkeypatch.setattr(
        "migrations.runner.run_startup_migrations",
        lambda: calls.append("migrate"),
    )
    monkeypatch.setattr(
        mcp_server,
        "create_mcp_http_root_app",
        lambda *_args, **_kwargs: calls.append("create-app") or fake_app,
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(
            run=lambda app, *, host, port: calls.append(
                "listen:{}:{}:{}".format(app is fake_app, host, port),
            ),
        ),
    )

    mcp_server.run_server(config)

    assert calls == [
        "migrate",
        "create-app",
        "listen:True:127.0.0.1:7488",
    ]
