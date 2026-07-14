from types import SimpleNamespace

from flask import Flask

import api.routes as routes_module
import api.routes.mcp as mcp_routes
from integration.mcp.scope import MCPRequestScope


def _app():
    app = Flask(__name__)
    app.register_blueprint(mcp_routes.mcp_bp)
    return app


def test_internal_chat_revalidates_mcp_scope_and_preserves_owner(monkeypatch):
    captured = {}
    scope = MCPRequestScope(
        username="alice",
        workspace_id="workspace-a",
        scopes=["mcp:chat"],
        token="mcp_token",
    )

    def resolve_scope(token, *, required_scope, traceparent):
        captured.update(token=token, required_scope=required_scope, traceparent=traceparent)
        return scope

    def execute_bridge(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "result": {"run_id": "run-1", "session_id": "session-1", "status": "success"},
            "scope": {"username": kwargs["scope"].username, "workspace_id": kwargs["scope"].workspace_id},
        }, 200

    monkeypatch.setattr(mcp_routes, "resolve_mcp_scope_from_token", resolve_scope)
    monkeypatch.setattr(
        mcp_routes,
        "get_auth_manager",
        lambda: SimpleNamespace(get_user=lambda username: SimpleNamespace(username=username, role="user")),
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(http_timeout_seconds=30),
    )
    monkeypatch.setattr(mcp_routes, "execute_mcp_chat_workflow", execute_bridge)
    monkeypatch.setattr(routes_module, "get_orchestrator", lambda: object())

    response = _app().test_client().post(
        "/agentic/mcp/internal/chat",
        headers={
            "Authorization": "Bearer mcp_token",
            "X-Workspace-Id": "workspace-a",
            "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        },
        json={"request": "分析", "session_id": "session-1"},
    )

    assert response.status_code == 200
    assert response.get_json()["scope"]["username"] == "alice"
    assert captured["token"] == "mcp_token"
    assert captured["required_scope"] == "mcp:chat"
    assert captured["scope"].username == "alice"
    assert captured["payload"]["session_id"] == "session-1"


def test_internal_chat_rejects_workspace_header_mismatch(monkeypatch):
    monkeypatch.setattr(
        mcp_routes,
        "resolve_mcp_scope_from_token",
        lambda *_args, **_kwargs: MCPRequestScope(
            username="alice",
            workspace_id="workspace-a",
            scopes=["mcp:chat"],
            token="mcp_token",
        ),
    )

    response = _app().test_client().post(
        "/agentic/mcp/internal/chat",
        headers={"Authorization": "Bearer mcp_token", "X-Workspace-Id": "workspace-b"},
        json={"request": "分析"},
    )

    assert response.status_code == 403
    assert response.get_json()["error"]["error_code"] == "MCP_WORKSPACE_SCOPE_MISMATCH"
