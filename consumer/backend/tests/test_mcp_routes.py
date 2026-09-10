"""FastAPI tests for MCP management and canonical chat admission."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import api.routes as routes_module
import api.routes.mcp as mcp_routes
import httpx
from agent.runtime.contracts import USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
from api.auth.auth import AuthUser
from api.protocols.mcp.paths import (
    MCP_INTERNAL_CHAT_PATH,
    MCP_INTERNAL_CHAT_STREAM_PATH,
)
from api.testing import TestApplication
from backend_synthetic_credentials import synthetic_secret
from fastapi.testclient import TestClient
from integration.mcp.ontology_binding import MCPOntologyBindingError
from integration.mcp.scope import MCPRequestScope


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def _app():
    app = TestApplication()
    app.state.runtime_container = SimpleNamespace(short_io=_InlinePool())
    app.include_router(mcp_routes.mcp_router)
    return app


class _AuthBackend:
    def __init__(self, *, role="user"):
        self.verify_calls = 0
        self.role = role

    def verify_token(self, token):
        self.verify_calls += 1
        return (
            AuthUser(username="operator_a", role=self.role)
            if token == synthetic_secret("mcp_routes-credential-03")
            else None
        )

    def get_user(self, username):
        return AuthUser(username=username, role=self.role)


def test_internal_chat_path_is_registered():
    paths = {route.path for route in mcp_routes.mcp_router.routes}
    assert MCP_INTERNAL_CHAT_PATH in paths
    assert MCP_INTERNAL_CHAT_STREAM_PATH in paths


def test_mcp_concurrency_get_returns_default_and_bounds(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")
    monkeypatch.setattr(
        mcp_routes,
        "get_workspace_runtime_config",
        lambda _workspace_id: {},
        raising=False,
    )

    response = TestClient(app).get(
        "/agentic/mcp/runtime/concurrency",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "stream_user_workspace_concurrency": 5,
        "default": 5,
        "min": 1,
        "max": 32,
    }


def test_runtime_status_uses_workspace_scoped_service_urls(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    config = SimpleNamespace(
        transport="both",
        effective_public_base_url=lambda: "https://service.example.net/mcp",
    )
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")
    monkeypatch.setattr(mcp_routes, "load_mcp_runtime_config", lambda: config)
    monkeypatch.setattr(mcp_routes, "get_workspace_mcp_endpoint_id", lambda _workspace_id: "cdd181f5b6cb47a9")
    monkeypatch.setattr(
        mcp_routes,
        "_probe_mcp_runtime_status",
        lambda _config: {"running": True, "probe_url": "tcp://127.0.0.1:7488", "detail": ""},
    )

    response = TestClient(app).get(
        "/agentic/mcp/runtime/status",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
    )

    assert response.status_code == 200
    assert response.json()["workspace_id"] == "workspace_demo"
    assert response.json()["mcp_endpoint_id"] == "cdd181f5b6cb47a9"


def test_internal_chat_revalidates_scope_and_passes_runtime_store(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    captured = {}
    scope = MCPRequestScope(
        username="operator_a",
        workspace_id="workspace_demo",
        scopes=["mcp:chat"],
        token=synthetic_secret("mcp_routes-credential-02"),
    )
    monkeypatch.setattr(mcp_routes, "resolve_mcp_scope_from_token", lambda *_args, **_kwargs: scope)
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(http_timeout_seconds=30),
    )
    store = object()
    monkeypatch.setattr(routes_module, "get_user_stores", lambda *_args: (object(), store))

    async def execute(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "result": {"status": "success"}}, 200

    monkeypatch.setattr(mcp_routes, "execute_mcp_chat_workflow_async", execute)

    response = TestClient(app).post(
        "/agentic/mcp/internal/chat",
        headers={
            "Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-02')}",
            "X-Workspace-Id": "workspace_demo",
        },
        json={"request": "synthetic question", "session_id": "session_demo"},
    )

    assert response.status_code == 200
    assert captured["runtime"] is app.state.runtime_container
    assert captured["execution_store"] is store
    assert captured["scope"] is scope
    assert captured["payload"]["session_id"] == "session_demo"
    assert captured["concurrency_limit"] == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT


def test_internal_chat_stream_returns_progress_then_terminal_result(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    captured = {}
    scope = MCPRequestScope(
        username="operator_a",
        workspace_id="workspace_demo",
        scopes=["mcp:chat"],
        token=synthetic_secret("mcp_routes-credential-02"),
    )
    monkeypatch.setattr(
        mcp_routes,
        "resolve_mcp_scope_from_token",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(
            http_timeout_seconds=30,
            chat_progress_queue_size=32,
            chat_progress_heartbeat_seconds=5,
            chat_progress_max_message_bytes=4096,
        ),
    )
    monkeypatch.setattr(
        mcp_routes,
        "get_workspace_runtime_config",
        lambda _workspace_id: {"mcp": {"stream_user_workspace_concurrency": 7}},
        raising=False,
    )
    monkeypatch.setattr(
        routes_module,
        "get_user_stores",
        lambda *_args: (object(), object()),
    )

    async def execute(**kwargs):
        captured.update(kwargs)
        await kwargs["progress_sink"](
            {
                "schema_version": "1.0",
                "seq": 1,
                "event": "run.started",
                "stage": "request",
                "status": "running",
                "summary": "已接收请求，开始处理",
                "run_id": "run_demo",
                "tool": "",
                "tool_call_id": "",
                "provider": "",
                "record_count": None,
                "elapsed_ms": None,
                "warning_code": "",
                "truncated": False,
            }
        )
        return {"ok": True, "result": {"content": "synthetic answer"}}, 200

    monkeypatch.setattr(
        mcp_routes,
        "execute_mcp_chat_workflow_async",
        execute,
    )

    async def request_stream():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client:
            return await client.post(
                MCP_INTERNAL_CHAT_STREAM_PATH,
                headers={
                    "Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-02')}",
                    "X-Workspace-Id": "workspace_demo",
                },
                json={
                    "request": "synthetic question",
                    "session_id": "session_demo",
                },
            )

    response = asyncio.run(request_stream())
    content = response.text

    assert response.status_code == 200
    assert "event: progress" in content
    assert '"event":"run.started"' in content
    assert "event: result" in content
    assert '"content":"synthetic answer"' in content
    assert captured["concurrency_limit"] == 7


def test_internal_chat_stream_preserves_platform_limit_for_admin(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend(role="admin")
    captured = {}
    scope = MCPRequestScope(
        username="operator_a",
        workspace_id="workspace_demo",
        scopes=["mcp:chat"],
        token="token-placeholder",
    )
    monkeypatch.setattr(
        mcp_routes,
        "resolve_mcp_scope_from_token",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(
            http_timeout_seconds=30,
            chat_progress_queue_size=32,
            chat_progress_heartbeat_seconds=5,
            chat_progress_max_message_bytes=4096,
        ),
    )
    monkeypatch.setattr(
        mcp_routes,
        "get_workspace_runtime_config",
        lambda _workspace_id: {"mcp": {"stream_user_workspace_concurrency": 7}},
        raising=False,
    )
    monkeypatch.setattr(
        routes_module,
        "get_user_stores",
        lambda *_args: (object(), object()),
    )

    async def execute(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "result": {"content": "synthetic answer"}}, 200

    monkeypatch.setattr(
        mcp_routes,
        "execute_mcp_chat_workflow_async",
        execute,
    )

    response = TestClient(app).post(
        MCP_INTERNAL_CHAT_STREAM_PATH,
        headers={
            "Authorization": "Bearer token-placeholder",
            "X-Workspace-Id": "workspace_demo",
        },
        json={"request": "synthetic question", "session_id": "session_admin"},
    )

    assert response.status_code == 200
    assert captured["concurrency_limit"] == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT


def test_admin_updates_mcp_stream_concurrency_policy(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend(role="admin")
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")
    captured = {}

    def update(workspace_id, patch):
        captured["workspace_id"] = workspace_id
        captured["patch"] = patch
        return {"mcp": {"stream_user_workspace_concurrency": 9}}

    monkeypatch.setattr(
        mcp_routes,
        "update_workspace_runtime_config",
        update,
        raising=False,
    )

    response = TestClient(app).patch(
        "/agentic/mcp/runtime/concurrency",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
        json={"stream_user_workspace_concurrency": 9},
    )

    assert response.status_code == 200
    assert captured == {
        "workspace_id": "workspace_demo",
        "patch": {"mcp": {"stream_user_workspace_concurrency": 9}},
    }
    assert response.json()["stream_user_workspace_concurrency"] == 9


def test_admin_update_rejects_mcp_stream_concurrency_outside_supported_range(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend(role="admin")
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")
    update_calls = []
    monkeypatch.setattr(
        mcp_routes,
        "update_workspace_runtime_config",
        lambda *args: update_calls.append(args),
        raising=False,
    )

    for invalid in (0, 33, True, 1.5, "5"):
        response = TestClient(app).patch(
            "/agentic/mcp/runtime/concurrency",
            headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
            json={"stream_user_workspace_concurrency": invalid},
        )
        assert response.status_code == 400

    assert update_calls == []


def test_non_admin_cannot_update_mcp_stream_concurrency_policy(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend(role="user")
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")

    response = TestClient(app).patch(
        "/agentic/mcp/runtime/concurrency",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
        json={"stream_user_workspace_concurrency": 9},
    )

    assert response.status_code == 403


def test_internal_chat_rejects_workspace_scope_mismatch(monkeypatch):
    app = _app()
    monkeypatch.setattr(
        mcp_routes,
        "resolve_mcp_scope_from_token",
        lambda *_args, **_kwargs: MCPRequestScope(
            username="operator_a",
            workspace_id="workspace_demo",
            scopes=["mcp:chat"],
            token=synthetic_secret("mcp_routes-credential-02"),
        ),
    )

    response = TestClient(app).post(
        "/agentic/mcp/internal/chat",
        headers={
            "Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-02')}",
            "X-Workspace-Id": "workspace_other",
        },
        json={"request": "synthetic question"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["error_code"] == "MCP_WORKSPACE_SCOPE_MISMATCH"


def test_internal_chat_authenticates_before_consuming_body():
    app = _app()
    body_chunks_requested = []

    def chunks():
        body_chunks_requested.append(True)
        yield b'{"request":"synthetic question"}'

    response = TestClient(app).post(
        "/agentic/mcp/internal/chat",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["error_code"] == "AUTH_TOKEN_MISSING"
    assert body_chunks_requested == []


def test_management_token_creation_uses_native_dependencies(monkeypatch):
    app = _app()
    backend = _AuthBackend()
    app.state.services["AUTH_MANAGER"] = backend
    monkeypatch.setattr(mcp_routes, "resolve_workspace_id", lambda _request: "workspace_demo")
    captured = {}

    def create_token(**kwargs):
        captured.update(kwargs)
        return {"item": {"id": "token_demo"}, "token": synthetic_secret("mcp_routes-credential-01")}

    monkeypatch.setattr(mcp_routes, "_create_token", create_token)
    response = TestClient(app).post(
        "/agentic/mcp/tokens",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
        json={"name": "synthetic", "scopes": ["mcp:chat"]},
    )

    assert response.status_code == 201
    assert response.json()["item"]["id"] == "token_demo"
    assert captured["username"] == "operator_a"
    assert captured["workspace_id"] == "workspace_demo"
    assert backend.verify_calls == 1


def test_management_auth_rejects_before_consuming_body():
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    body_chunks_requested = []

    def chunks():
        body_chunks_requested.append(True)
        yield b'{"name":"synthetic"}'

    response = TestClient(app).post(
        "/agentic/mcp/tokens",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert body_chunks_requested == []


def test_create_token_api_forwards_validated_ontology_binding(monkeypatch):
    app = _app()
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    monkeypatch.setattr(
        mcp_routes,
        "resolve_workspace_id",
        lambda _request: "workspace_demo",
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(token_ttl_seconds=3600),
    )
    captured = {}

    def create_token(**kwargs):
        captured.update(kwargs)
        return {
            "token": synthetic_secret("mcp_routes-credential-01"),
            "item": {
                "token_id": "token_demo",
                "ontology_id": kwargs["ontology_id"],
                "binding_mode": "ontology",
            },
        }

    monkeypatch.setattr(mcp_routes, "create_mcp_token", create_token)
    response = TestClient(app).post(
        "/agentic/mcp/tokens",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
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
    app.state.services["AUTH_MANAGER"] = _AuthBackend()
    monkeypatch.setattr(
        mcp_routes,
        "resolve_workspace_id",
        lambda _request: "workspace_demo",
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

    invalid = TestClient(app).post(
        "/agentic/mcp/tokens",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
        json={"ontology_id": ""},
    )
    disabled = TestClient(app).post(
        "/agentic/mcp/tokens",
        headers={"Authorization": f"Bearer {synthetic_secret('mcp_routes-credential-03')}"},
        json={"ontology_id": "knowledge_package_demo"},
    )

    assert invalid.status_code == 400
    assert invalid.json()["error_code"] == "MCP_ONTOLOGY_ID_INVALID"
    assert disabled.status_code == 409
    assert disabled.json()["error_code"] == "MCP_ONTOLOGY_DISABLED"
