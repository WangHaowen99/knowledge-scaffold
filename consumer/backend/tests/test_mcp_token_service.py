import json
import time
from pathlib import Path

import pytest

from integration.mcp.config import (
    MCPConfigurationError,
    MCPRuntimeConfig,
    load_mcp_runtime_config,
)
from integration.mcp.token_store import MCPTokenStore
from integration.mcp.token_service import (
    MCPTokenAuthError,
    create_mcp_token,
    list_mcp_tokens,
    revoke_mcp_token,
    revoke_all_mcp_tokens_for_user,
    verify_mcp_token,
)


def _config(path: Path) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="",
        token_secret="test-secret-at-least-32-characters-long",
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
    assert "token" not in items[0]
    assert created["token"] not in cfg.token_store_path.read_text(encoding="utf-8")
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
    assert "token" not in items[0]
    with pytest.raises(MCPTokenAuthError):
        verify_mcp_token(created["token"], required_scope="mcp:search", config=cfg)


def test_mcp_token_rejects_missing_or_weak_signing_secret(tmp_path):
    cfg = MCPRuntimeConfig(**{**_config(tmp_path / "tokens.json").__dict__, "token_secret": "weak"})

    with pytest.raises(ValueError, match="MCP_TOKEN_SECRET"):
        create_mcp_token(
            username="admin",
            workspace_id="test_project",
            scopes=["mcp:search"],
            config=cfg,
        )


def test_wrong_scope_and_expired_mcp_tokens_are_rejected(tmp_path, monkeypatch):
    cfg = _config(tmp_path / "tokens.json")
    created = create_mcp_token(
        username="admin",
        workspace_id="test_project",
        scopes=["mcp:search"],
        expires_in=1,
        config=cfg,
    )

    with pytest.raises(MCPTokenAuthError) as scope_error:
        verify_mcp_token(created["token"], required_scope="mcp:chat", config=cfg)
    assert scope_error.value.code == "MCP_SCOPE_FORBIDDEN"

    created_at = time.time()
    monkeypatch.setattr(
        "integration.mcp.token_crypto.time.time",
        lambda: created_at + 2,
    )
    with pytest.raises(MCPTokenAuthError, match="过期"):
        verify_mcp_token(created["token"], required_scope="mcp:search", config=cfg)


def test_revoke_all_user_tokens_is_idempotent_and_scoped(tmp_path):
    cfg = _config(tmp_path / "tokens.json")
    store = MCPTokenStore(cfg.token_store_path)
    alice_tokens = [
        create_mcp_token(
            username="alice",
            workspace_id=workspace_id,
            scopes=["mcp:search"],
            expires_in=3600,
            config=cfg,
            store=store,
        )["token"]
        for workspace_id in ("workspace-a", "workspace-b")
    ]
    bob_token = create_mcp_token(
        username="bob",
        workspace_id="workspace-a",
        scopes=["mcp:search"],
        expires_in=3600,
        config=cfg,
        store=store,
    )["token"]

    assert revoke_all_mcp_tokens_for_user(username="alice", store=store) == 2
    assert revoke_all_mcp_tokens_for_user(username="alice", store=store) == 0
    for token in alice_tokens:
        with pytest.raises(MCPTokenAuthError):
            verify_mcp_token(token, config=cfg)
    assert verify_mcp_token(bob_token, config=cfg)["username"] == "bob"


def test_legacy_plaintext_token_is_backed_up_and_removed(tmp_path):
    path = tmp_path / "tokens.json"
    plaintext = "mcp_legacy-secret-token"
    path.write_text(
        json.dumps({
            "version": 1,
            "tokens": [{
                "token_id": "legacy-1",
                "jti": "legacy-jti",
                "token_value": plaintext,
                "username": "admin",
                "workspace_id": "test_project",
                "scopes": ["mcp:search"],
                "status": "active",
            }],
        }),
        encoding="utf-8",
    )

    store = MCPTokenStore(path)
    report = store.migrate_legacy_plaintext_tokens()

    assert report["migrated"] is True
    assert report["removed_plaintext_fields"] == 1
    backup = path.with_suffix(path.suffix + ".pre-plaintext.bak")
    assert backup.exists()
    assert plaintext in backup.read_text(encoding="utf-8")
    migrated = path.read_text(encoding="utf-8")
    assert plaintext not in migrated
    assert "token_value" not in migrated
    assert "sha256:" in migrated
    assert "token" not in store.list_tokens(username="admin", workspace_id="test_project")[0]


def test_mcp_runtime_requires_independent_token_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.delenv("MCP_TOKEN_SECRET", raising=False)
    monkeypatch.setattr("integration.mcp.config._load_local_dotenv", lambda: None)

    with pytest.raises(MCPConfigurationError, match="MCP_TOKEN_SECRET"):
        load_mcp_runtime_config()


def test_bound_mcp_token_persists_and_verifies_ontology_claim(monkeypatch, tmp_path):
    cfg = _config(tmp_path / "tokens.json")
    monkeypatch.setattr(
        "integration.mcp.ontology_binding.validate_token_ontology_binding",
        lambda **_kwargs: None,
    )

    created = create_mcp_token(
        username="operator_a",
        workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo",
        scopes=["mcp:search", "mcp:chat"],
        expires_in=3600,
        config=cfg,
    )
    verified = verify_mcp_token(created["token"], config=cfg)

    assert created["item"]["ontology_id"] == "knowledge_package_demo"
    assert created["item"]["binding_mode"] == "ontology"
    assert verified["ontology_id"] == "knowledge_package_demo"
    stored = MCPTokenStore(cfg.token_store_path).list_tokens(
        username="operator_a",
        workspace_id="workspace_demo",
    )[0]
    assert stored["ontology_id"] == "knowledge_package_demo"


def test_mcp_token_rejects_registry_binding_mismatch(monkeypatch, tmp_path):
    cfg = _config(tmp_path / "tokens.json")
    monkeypatch.setattr(
        "integration.mcp.ontology_binding.validate_token_ontology_binding",
        lambda **_kwargs: None,
    )
    created = create_mcp_token(
        username="operator_a",
        workspace_id="workspace_demo",
        ontology_id="knowledge_package_demo",
        scopes=["mcp:chat"],
        expires_in=3600,
        config=cfg,
    )
    document = json.loads(cfg.token_store_path.read_text(encoding="utf-8"))
    document["tokens"][0]["ontology_id"] = "knowledge_package_other"
    cfg.token_store_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(MCPTokenAuthError) as exc_info:
        verify_mcp_token(created["token"], config=cfg)

    assert exc_info.value.code == "MCP_TOKEN_BINDING_MISMATCH"
