from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette

from agentic.mcp.config import MCPRuntimeConfig
from agentic.mcp import server as mcp_server
from agentic.mcp.service_catalog import list_mcp_service_cards


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

    assert "/search/mcp" in route_paths
    assert "/chat/mcp" in route_paths
    assert "/search/sse" not in route_paths
    assert "/chat/sse" not in route_paths
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

    assert "/search/sse" in route_paths
    assert "/chat/sse" in route_paths
    assert "/search/mcp" not in route_paths
    assert "/chat/mcp" not in route_paths
    assert captured_transports == ["sse", "sse"]


def test_mcp_service_catalog_reports_only_exposed_transport_urls(tmp_path):
    http_cards = list_mcp_service_cards(_config(tmp_path, "streamable-http"))
    http_search = next(card for card in http_cards if card["toolset"] == "search")
    assert http_search["active_url"] == "http://mcp.example/search/mcp"
    assert http_search["streamable_http_url"] == "http://mcp.example/search/mcp"
    assert http_search["mcp_url"] == "http://mcp.example/search/mcp"
    assert http_search["sse_url"] == ""
    assert http_search["sse_enabled"] is False

    sse_cards = list_mcp_service_cards(_config(tmp_path, "sse"))
    sse_search = next(card for card in sse_cards if card["toolset"] == "search")
    assert sse_search["active_url"] == "http://mcp.example/search/sse"
    assert sse_search["streamable_http_url"] == ""
    assert sse_search["mcp_url"] == ""
    assert sse_search["sse_url"] == "http://mcp.example/search/sse"
    assert sse_search["sse_enabled"] is True
