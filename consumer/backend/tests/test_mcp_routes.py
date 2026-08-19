from types import SimpleNamespace

from flask import Flask

import api.routes as routes_module
import api.routes.mcp as mcp_routes
from api.protocols.mcp.paths import MCP_INTERNAL_CHAT_PATH
from integration.mcp.scope import MCPRequestScope
from integration.mcp.ontology_binding import MCPOntologyBindingError


def _app():
    app = Flask(__name__)
    app.register_blueprint(mcp_routes.mcp_bp)
    return app


def test_internal_chat_bridge_path_is_registered():
    rules = {rule.rule for rule in _app().url_map.iter_rules()}

    assert MCP_INTERNAL_CHAT_PATH in rules


def test_runtime_status_returns_workspace_specific_mcp_urls(monkeypatch):
    endpoint_id = "cdd181f5b6cb47a9"
    app = _app()
    app.config["AUTH_MANAGER"] = SimpleNamespace(
        verify_token=lambda _token: SimpleNamespace(
            username="alice",
            role="user",
            password_change_required=False,
        ),
    )
    config = SimpleNamespace(
        transport="both",
        effective_public_base_url=lambda: "https://api.example.com/mcp",
    )
    monkeypatch.setattr(
        mcp_routes,
        "_resolve_workspace_or_response",
        lambda: ("workspace-a", None),
    )
    monkeypatch.setattr(mcp_routes, "load_mcp_runtime_config", lambda: config)
    monkeypatch.setattr(
        mcp_routes,
        "get_workspace_mcp_endpoint_id",
        lambda workspace_id: endpoint_id if workspace_id == "workspace-a" else "",
    )
    monkeypatch.setattr(
        mcp_routes,
        "_probe_mcp_runtime_status",
        lambda _config: {
            "running": True,
            "probe_url": "http://mcp:7488/health",
            "detail": "",
        },
    )

    response = app.test_client().get(
        "/agentic/mcp/runtime/status",
        headers={"Authorization": "Bearer access-token"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["workspace_id"] == "workspace-a"
    assert payload["mcp_endpoint_id"] == endpoint_id
    assert {
        service["toolset"]: service["streamable_http_url"]
        for service in payload["services"]
    } == {
        "search": "https://api.example.com/mcp/{}/search/mcp".format(endpoint_id),
        "chat": "https://api.example.com/mcp/{}/chat/mcp".format(endpoint_id),
    }


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
    assert captured["trace_config"].http_timeout_seconds == 30


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


def test_create_token_api_forwards_validated_ontology_binding(monkeypatch):
    captured = {}
    app = _app()
    app.config["AUTH_MANAGER"] = SimpleNamespace(
        verify_token=lambda _token: SimpleNamespace(
            username="operator_a",
            role="user",
            password_change_required=False,
        ),
    )
    monkeypatch.setattr(
        mcp_routes,
        "_resolve_workspace_or_response",
        lambda: ("workspace_demo", None),
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(token_ttl_seconds=3600),
    )

    def create_token(**kwargs):
        captured.update(kwargs)
        return {
            "token": "<MCP_TOKEN>",
            "item": {
                "token_id": "token_demo",
                "ontology_id": kwargs["ontology_id"],
                "binding_mode": "ontology",
            },
        }

    monkeypatch.setattr(mcp_routes, "create_mcp_token", create_token)
    response = app.test_client().post(
        "/agentic/mcp/tokens",
        headers={"Authorization": "Bearer <ACCESS_TOKEN>"},
        json={
            "ontology_id": "knowledge_package_demo",
            "scopes": ["mcp:search", "mcp:chat"],
        },
    )

    assert response.status_code == 201
    assert captured["username"] == "operator_a"
    assert captured["workspace_id"] == "workspace_demo"
    assert captured["ontology_id"] == "knowledge_package_demo"


def test_create_token_api_returns_stable_ontology_errors(monkeypatch):
    app = _app()
    app.config["AUTH_MANAGER"] = SimpleNamespace(
        verify_token=lambda _token: SimpleNamespace(
            username="operator_a",
            role="user",
            password_change_required=False,
        ),
    )
    monkeypatch.setattr(
        mcp_routes,
        "_resolve_workspace_or_response",
        lambda: ("workspace_demo", None),
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(token_ttl_seconds=3600),
    )
    def reject_disabled_ontology(**_kwargs):
        raise MCPOntologyBindingError(
            "MCP_ONTOLOGY_DISABLED",
            "指定知识包当前已禁用",
            status_code=409,
        )

    monkeypatch.setattr(mcp_routes, "create_mcp_token", reject_disabled_ontology)

    invalid = app.test_client().post(
        "/agentic/mcp/tokens",
        headers={"Authorization": "Bearer <ACCESS_TOKEN>"},
        json={"ontology_id": ""},
    )
    disabled = app.test_client().post(
        "/agentic/mcp/tokens",
        headers={"Authorization": "Bearer <ACCESS_TOKEN>"},
        json={"ontology_id": "knowledge_package_demo"},
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["error_code"] == "MCP_ONTOLOGY_ID_INVALID"
    assert disabled.status_code == 409
    assert disabled.get_json()["error_code"] == "MCP_ONTOLOGY_DISABLED"
