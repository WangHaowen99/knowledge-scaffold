import json
from pathlib import Path

import pytest

from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import resolve_mcp_scope_from_token
from integration.mcp.token_service import MCPTokenAuthError, create_mcp_token


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


def _configure_mcp(monkeypatch, cfg: MCPRuntimeConfig) -> None:
    monkeypatch.setenv("MCP_TOKEN_SECRET", cfg.token_secret)
    monkeypatch.setenv("MCP_TOKEN_ALGORITHM", cfg.token_algorithm)
    monkeypatch.setenv("MCP_TOKEN_ISSUER", cfg.token_issuer)
    monkeypatch.setenv("MCP_TOKEN_AUDIENCE", cfg.token_audience)
    monkeypatch.setenv("MCP_TOKEN_STORE_PATH", str(cfg.token_store_path))


def _write_workspace(
    runtime_root: Path,
    workspace_id: str,
    *,
    owner,
    members,
    metadata_id: str | None = None,
    mcp_endpoint_id: str | None = None,
) -> Path:
    workspace_dir = runtime_root / "workspaces" / workspace_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = workspace_dir / "workspace.json"
    metadata_path.write_text(
        json.dumps(
            {
                "id": metadata_id if metadata_id is not None else workspace_id,
                "name": workspace_id,
                "description": "",
                "created_at": "2026-07-13T00:00:00+00:00",
                "updated_at": "2026-07-13T00:00:00+00:00",
                "owner": owner,
                "members": members,
                **(
                    {"mcp_endpoint_id": mcp_endpoint_id}
                    if mcp_endpoint_id is not None
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )
    return metadata_path


def _set_default_workspace(runtime_root: Path, workspace_id: str) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "workspace-settings.json").write_text(
        json.dumps({"default_workspace_id": workspace_id}),
        encoding="utf-8",
    )


def _create_token(cfg: MCPRuntimeConfig, *, username: str, workspace_id: str) -> str:
    return create_mcp_token(
        username=username,
        workspace_id=workspace_id,
        scopes=["mcp:search"],
        expires_in=3600,
        config=cfg,
    )["token"]


@pytest.mark.parametrize(
    ("username", "owner", "members", "is_default"),
    [
        ("workspace-owner", "workspace-owner", [], False),
        ("workspace-member", "workspace-owner", ["workspace-member"], False),
        ("default-visitor", "workspace-owner", [], True),
        ("public-visitor", None, [], False),
    ],
)
def test_mcp_scope_allows_current_owner_member_default_and_ownerless_access(
    tmp_path,
    monkeypatch,
    username,
    owner,
    members,
    is_default,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    _write_workspace(runtime_root, "team-space", owner=owner, members=members)
    if is_default:
        _set_default_workspace(runtime_root, "team-space")
    token = _create_token(cfg, username=username, workspace_id="team-space")

    scope = resolve_mcp_scope_from_token(token, required_scope="mcp:search")

    assert scope.username == username
    assert scope.workspace_id == "team-space"


def test_mcp_scope_allows_legacy_workspace_without_acl_fields(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    metadata_path = _write_workspace(
        runtime_root,
        "legacy-space",
        owner=None,
        members=[],
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("owner")
    metadata.pop("members")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    token = _create_token(cfg, username="user", workspace_id="legacy-space")

    scope = resolve_mcp_scope_from_token(token, required_scope="mcp:search")

    assert scope.username == "user"
    assert scope.workspace_id == "legacy-space"


def test_mcp_scope_rejects_old_token_immediately_after_member_removal(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    metadata_path = _write_workspace(
        runtime_root,
        "private-space",
        owner="workspace-owner",
        members=["former-member"],
    )
    token = _create_token(cfg, username="former-member", workspace_id="private-space")
    assert resolve_mcp_scope_from_token(token).username == "former-member"

    metadata_path.write_text(
        json.dumps(
            {
                "id": "private-space",
                "name": "private-space",
                "owner": "workspace-owner",
                "members": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(token)

    assert exc_info.value.code == "MCP_WORKSPACE_ACCESS_REVOKED"


def test_mcp_scope_does_not_grant_admin_name_an_implicit_role_bypass(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    _write_workspace(
        runtime_root,
        "private-space",
        owner="workspace-owner",
        members=[],
    )
    token = _create_token(cfg, username="admin", workspace_id="private-space")

    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(token)

    assert exc_info.value.code == "MCP_WORKSPACE_ACCESS_REVOKED"


@pytest.mark.parametrize("metadata_body", ["{", "[]"])
def test_mcp_scope_fails_closed_for_corrupt_workspace_metadata(
    tmp_path,
    monkeypatch,
    metadata_body,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    metadata_path = _write_workspace(
        runtime_root,
        "private-space",
        owner=None,
        members=[],
    )
    metadata_path.write_text(metadata_body, encoding="utf-8")
    token = _create_token(cfg, username="user", workspace_id="private-space")

    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(token)

    assert exc_info.value.code == "MCP_WORKSPACE_METADATA_INVALID"


def test_mcp_scope_fails_closed_for_workspace_metadata_id_mismatch(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    _write_workspace(
        runtime_root,
        "private-space",
        owner=None,
        members=[],
        metadata_id="another-space",
    )
    token = _create_token(cfg, username="user", workspace_id="private-space")

    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(token)

    assert exc_info.value.code == "MCP_WORKSPACE_METADATA_INVALID"


def test_mcp_scope_reports_missing_workspace_explicitly(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    token = _create_token(cfg, username="user", workspace_id="missing-space")

    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(token)

    assert exc_info.value.code == "MCP_WORKSPACE_NOT_FOUND"


def test_mcp_scope_requires_endpoint_to_match_token_workspace(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    cfg = _config(tmp_path / "tokens.json")
    _configure_mcp(monkeypatch, cfg)
    endpoint_id = "cdd181f5b6cb47a9"
    _write_workspace(
        runtime_root,
        "team-space",
        owner=None,
        members=[],
        mcp_endpoint_id=endpoint_id,
    )
    token = _create_token(cfg, username="user", workspace_id="team-space")

    scope = resolve_mcp_scope_from_token(
        token,
        required_scope="mcp:search",
        mcp_endpoint_id=endpoint_id,
    )

    assert scope.workspace_id == "team-space"
    with pytest.raises(MCPTokenAuthError) as exc_info:
        resolve_mcp_scope_from_token(
            token,
            required_scope="mcp:search",
            mcp_endpoint_id="a" * 16,
        )
    assert exc_info.value.code == "MCP_WORKSPACE_ENDPOINT_MISMATCH"
