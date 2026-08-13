"""Chain-style tests for interactive/non-interactive recommendation fallback."""

from __future__ import annotations

from types import SimpleNamespace

from flask import Flask

import api.routes as routes_module
import api.routes.mcp as mcp_routes
from api.auth.auth import AuthManager
from api.protocols.acp import server as acp_server
from api.protocols.mcp.paths import MCP_INTERNAL_CHAT_PATH
from api.routes.a2a import a2a_bp
from api.routes.workflows import workflows_bp
from integration.mcp.scope import MCPRequestScope
from persistence.run_queue import RunAdmissionQueue, RunSlotManager
from persistence.run_store import RunStore
from workspace.workspaces import create_workspace


SCENARIO_REQUEST = "某物流集团希望集中管理多银行账户、统一收付结算并降低资金成本，应该推荐什么方案？"
FALLBACK_ANSWER = "基于业务常识，推荐财资管理云，并可结合资金池与银企直连能力。"
AWAITING_PROMPT = "当前本体没有适用操作。是否允许基于业务通识给出财资管理云建议？"


class _PermitAllLimiter:
    @staticmethod
    def try_acquire():
        return True


class _ScenarioOrchestrator:
    def __init__(self):
        self.slot_manager = RunSlotManager()
        self.run_admission_queue = RunAdmissionQueue(self.slot_manager)
        self.run_rate_limiter = _PermitAllLimiter()
        self.run_store = RunStore()
        self.last_calls = []

    def run(self, **kwargs):
        self.last_calls.append(dict(kwargs))
        run_id = str(kwargs.get("requested_run_id") or "simulated-run")
        session_id = str(kwargs.get("session_id") or "simulated-session")
        supports_interaction = bool((kwargs.get("user_context") or {}).get("supports_user_interaction"))
        if supports_interaction:
            interaction = {
                "id": "interaction_finance_recommend",
                "tool_call_id": "interaction_finance_recommend",
                "type": "single_choice",
                "prompt": AWAITING_PROMPT,
                "options": [{"id": "continue", "label": "继续", "value": "continue"}],
            }
            self.run_store.await_run(
                run_id,
                {
                    "status": "awaiting_input",
                    "pending_user_interaction": interaction,
                },
            )
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": f"{run_id}:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": session_id,
                    "run_id": run_id,
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "chain"},
                    "payload": {
                        "status": "awaiting_input",
                        "final_output": {
                            "type": "user_interaction",
                            "text": AWAITING_PROMPT,
                            "options": interaction["options"],
                            "interaction": interaction,
                        },
                    },
                },
            }
            return

        self.run_store.complete_run(run_id, {"status": "success"})
        yield {
            "event": "agent.v2",
            "data": {
                "protocol": "agent-stream.v2",
                "event_id": f"{run_id}:1",
                "seq": 1,
                "ts": 1700000000000,
                "session_id": session_id,
                "run_id": run_id,
                "turn_id": 1,
                "type": "turn.finished",
                "source": {"node": "test", "component": "chain"},
                "payload": {
                    "status": "success",
                    "final_output": FALLBACK_ANSWER,
                },
            },
        }

    def cancel_run(self, run_id, reason=None):
        return self.run_store.cancel_run(run_id, metadata={"cancel_reason": reason}) is not None


def _create_auth_app(tmp_path, monkeypatch, *blueprints):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace(
        "Chain Test Workspace",
        workspace_id="chain-test",
        owner=None,
        set_as_default=True,
    )

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Workspace-Id": "chain-test",
    }
    return client, headers


def test_sync_route_uses_business_fallback_for_non_interactive_finance_scenario(monkeypatch, tmp_path):
    fake = _ScenarioOrchestrator()
    client, headers = _create_auth_app(tmp_path, monkeypatch, workflows_bp)
    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: fake)
    monkeypatch.setattr(
        "api.routes.workflows.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )

    response = client.post(
        "/agentic/runs/sync",
        json={"request": SCENARIO_REQUEST, "session_id": "sync-finance-chain"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["final_answer"] == FALLBACK_ANSWER
    assert fake.last_calls[-1]["user_context"]["supports_user_interaction"] is False


def test_a2a_stream_route_uses_business_fallback_for_non_interactive_finance_scenario(monkeypatch, tmp_path):
    fake = _ScenarioOrchestrator()
    client, headers = _create_auth_app(tmp_path, monkeypatch, a2a_bp)
    monkeypatch.setattr("api.routes.a2a._get_orchestrator", lambda: fake)

    response = client.post(
        "/a2a/tasks/stream",
        json={"message": SCENARIO_REQUEST, "session_id": "a2a-finance-chain"},
        headers=headers,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "event: task.update" in body
    assert '"status": "success"' in body
    assert FALLBACK_ANSWER in body
    assert fake.last_calls[-1]["user_context"]["supports_user_interaction"] is False


def test_mcp_internal_chat_route_uses_business_fallback_for_non_interactive_finance_scenario(
    monkeypatch,
    tmp_path,
):
    fake = _ScenarioOrchestrator()
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(mcp_routes.mcp_bp)
    client = app.test_client()
    scope = MCPRequestScope(
        username="admin",
        workspace_id="chain-test",
        scopes=["mcp:chat"],
        token="mcp_token",
    )

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace(
        "Chain Test Workspace",
        workspace_id="chain-test",
        owner=None,
        set_as_default=True,
    )
    monkeypatch.setattr(
        mcp_routes,
        "resolve_mcp_scope_from_token",
        lambda *_args, **_kwargs: scope,
    )
    monkeypatch.setattr(
        mcp_routes,
        "get_auth_manager",
        lambda: SimpleNamespace(
            get_user=lambda username: SimpleNamespace(username=username, role="user")
        ),
    )
    monkeypatch.setattr(
        mcp_routes,
        "load_mcp_runtime_config",
        lambda: SimpleNamespace(http_timeout_seconds=30),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(routes_module, "get_orchestrator", lambda: fake)

    response = client.post(
        MCP_INTERNAL_CHAT_PATH,
        headers={
            "Authorization": "Bearer mcp_token",
            "X-Workspace-Id": "chain-test",
        },
        json={"request": SCENARIO_REQUEST, "session_id": "mcp-finance-chain"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["result"]["status"] == "success"
    assert body["result"]["content"] == FALLBACK_ANSWER
    assert fake.last_calls[-1]["user_context"]["supports_user_interaction"] is False


def test_acp_execute_route_keeps_interaction_for_interactive_finance_scenario(monkeypatch, tmp_path):
    fake = _ScenarioOrchestrator()
    client, headers = _create_auth_app(tmp_path, monkeypatch, acp_server.acp_bp)
    with acp_server._ACP_REGISTRY_LOCK:
        acp_server._ACP_RUN_REGISTRY.clear()
    monkeypatch.setattr(acp_server, "_orchestrator", fake)

    create_resp = client.post(
        "/acp/runs",
        json={
            "messages": [
                {
                    "role": "user",
                    "parts": [{"type": "text", "content": SCENARIO_REQUEST}],
                }
            ],
            "context": {"session_id": "acp-finance-chain"},
        },
        headers=headers,
    )
    assert create_resp.status_code == 200
    run_id = create_resp.get_json()["run_id"]

    execute_resp = client.post(f"/acp/runs/{run_id}/execute", headers=headers)

    assert execute_resp.status_code == 200
    body = execute_resp.get_json()
    assert body["status"] == "awaiting"
    assert body["awaiting"]["await_id"] == "interaction_finance_recommend"
    assert body["awaiting"]["message"]["parts"][0]["content"] == AWAITING_PROMPT
    assert fake.last_calls[-1]["user_context"]["supports_user_interaction"] is True
