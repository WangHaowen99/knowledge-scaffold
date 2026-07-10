from pathlib import Path

import pytest

from agentic.mcp.config import MCPRuntimeConfig
from agentic.mcp.token_store import MCPTokenStore
from agentic.mcp.token_service import (
    MCPTokenAuthError,
    create_mcp_token,
    list_mcp_tokens,
    revoke_mcp_token,
    verify_mcp_token,
)


def _config(path: Path) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="",
        token_secret="test-secret",
        token_algorithm="HS256",
        token_issuer="meta-wise",
        token_audience="meta-wise-mcp",
        token_ttl_seconds=3600,
        token_max_ttl_seconds=86400,
        http_timeout_seconds=30,
        transport="streamable-http",
        token_store_path=path,
    )


def test_mcp_token_uses_oauth_style_scope_and_registry(tmp_path):
    cfg = _config(tmp_path / "tokens.json")

    created = create_mcp_token(
        username="admin",
        workspace_id="test_project",
        scopes=["mcp:search"],
        expires_in=3600,
        config=cfg,
    )
    verified = verify_mcp_token(created["token"], required_scope="mcp:search", config=cfg)

    assert created["item"]["username"] == "admin"
    assert created["item"]["workspace_id"] == "test_project"
    assert created["item"]["scopes"] == ["mcp:search"]
    assert verified["username"] == "admin"
    assert verified["workspace_id"] == "test_project"
    assert verified["scopes"] == ["mcp:search"]

    items = list_mcp_tokens(username="admin", workspace_id="test_project", store=MCPTokenStore(cfg.token_store_path))
    assert items[0]["token"] == created["token"]
    assert items[0]["status"] == "active"


def test_revoked_mcp_token_is_rejected(tmp_path):
    cfg = _config(tmp_path / "tokens.json")
    store = MCPTokenStore(cfg.token_store_path)
    created = create_mcp_token(
        username="admin",
        workspace_id="test_project",
        scopes=["mcp:search", "mcp:chat"],
        expires_in=3600,
        config=cfg,
        store=store,
    )

    assert revoke_mcp_token(
        username="admin",
        workspace_id="test_project",
        token_id=created["item"]["token_id"],
        store=store,
    )
    items = list_mcp_tokens(username="admin", workspace_id="test_project", store=store)
    assert items[0]["status"] == "revoked"
    assert items[0]["token"] == ""
    with pytest.raises(MCPTokenAuthError):
        verify_mcp_token(created["token"], required_scope="mcp:search", config=cfg)
