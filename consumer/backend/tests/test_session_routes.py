"""HTTP route tests for chat session management."""

import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from flask import Flask

from config.workspace_llm_profiles import update_workspace_llm_profiles
from api.routes.sessions import sessions_bp
from api.routes.workflows import AgenticRunRegistry
from api.auth.auth import AuthManager
from persistence.chat_store import ChatStore
from persistence.execution_store import (
    INTERACTION_CANCELLED_MESSAGE,
    INTERACTION_CANCELLED_NOTICE,
    ExecutionStore,
)
from workspace.workspaces import create_workspace


class _FakeSession:
    def __init__(self, session_id, metadata=None):
        self.id = session_id
        self.metadata = dict(metadata or {})

    def to_dict(self, include_messages=False):
        del include_messages
        return {
            "id": self.id,
            "title": "新对话",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
            "message_count": 0,
            "metadata": self.metadata,
        }


class _FakeChatStore:
    def __init__(self, session=None):
        self.session = session
        self.pinned_updates = []
        self.created_sessions = []

    def create_session(self, title=None, metadata=None, session_id=None):
        del session_id
        self.session = _FakeSession("created-session", metadata=metadata)
        self.created_sessions.append({"title": title, "metadata": dict(metadata or {})})
        return self.session

    def prune_old_sessions(self, max_sessions):
        del max_sessions
        return []

    def get_session(self, session_id):
        if self.session and self.session.id == session_id:
            return self.session
        return None

    def set_session_pinned(self, session_id, pinned):
        self.pinned_updates.append((session_id, pinned))
        if not self.session or self.session.id != session_id:
            return None
        self.session.metadata["pinned"] = pinned
        return self.session


class _FakeExecutionStore:
    def __init__(self, turns=None):
        self.deleted_sessions = []
        self.turns = list(turns or [])

    def delete_session(self, session_id):
        self.deleted_sessions.append(session_id)

    def list_turns_by_status(self, session_id, status=None):
        del session_id
        rows = list(reversed(self.turns))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows


def _build_test_client(tmp_path):
    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "admin123")
    assert user is not None
    user = auth_manager.change_password("admin", "admin123", "RootStrong123!")
    assert user is not None

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(sessions_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def test_update_session_pinned_state(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore(_FakeSession("sess-1"))
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, object()),
    )

    response = client.patch(
        "/agentic/sessions/sess-1",
        json={"pinned": True},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["metadata"]["pinned"] is True
    assert chat_store.pinned_updates == [("sess-1", True)]


def test_update_session_pinned_state_requires_boolean(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore(_FakeSession("sess-1"))
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, object()),
    )

    response = client.patch(
        "/agentic/sessions/sess-1",
        json={"pinned": "true"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "pinned must be a boolean"
    assert chat_store.pinned_updates == []


def test_update_session_pinned_state_returns_404_for_missing_session(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (_FakeChatStore(), object()),
    )

    response = client.patch(
        "/agentic/sessions/missing",
        json={"pinned": False},
        headers=headers,
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Session not found"


def test_cancel_pending_interaction_is_durable_and_idempotent(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    chat_store.create_session(session_id="sess-cancel", title="Cancel")
    interaction = {
        "id": "interaction-cancel-1",
        "type": "input",
        "prompt": "请补充信息",
    }
    execution_store.start_turn("sess-cancel", 1, "run-1", "开始")
    execution_store.set_llm_context(
        "sess-cancel",
        [{"role": "assistant", "content": "请补充信息"}],
    )
    execution_store.await_turn(
        "sess-cancel",
        1,
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    stale = client.post(
        "/agentic/sessions/sess-cancel/interactions/stale/cancel",
        headers=headers,
    )
    assert stale.status_code == 409

    url = "/agentic/sessions/sess-cancel/interactions/interaction-cancel-1/cancel"
    response = client.post(url, headers=headers)
    retry = client.post(url, headers=headers)

    assert response.status_code == 200
    assert retry.status_code == 200
    assert response.get_json()["message"] == INTERACTION_CANCELLED_MESSAGE
    assert response.get_json()["display_message"] == INTERACTION_CANCELLED_NOTICE
    assert execution_store.get_turn("sess-cancel", 1)["status"] == "cancelled"
    assert execution_store.get_llm_context("sess-cancel") == [
        {"role": "assistant", "content": "请补充信息"},
    ]
    assert "interaction_context_consumed_at" not in execution_store.get_turn(
        "sess-cancel",
        1,
    )
    loaded = chat_store.get_session("sess-cancel")
    cancellation_events = [
        event for event in loaded.events
        if event.get("event") == "chat_message"
        and (event.get("data") or {}).get("metadata", {}).get("type")
        == "interaction_cancelled"
    ]
    assert len(cancellation_events) == 1
    assert cancellation_events[0]["data"]["role"] == "system"
    assert cancellation_events[0]["data"]["content"] == INTERACTION_CANCELLED_NOTICE

    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )
    refreshed = client.get("/agentic/sessions/sess-cancel", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.get_json()["active_run_status"] == "cancelled"


def test_list_session_turns_filters_status_and_feedback(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore(_FakeSession("sess-1"))
    execution_store = _FakeExecutionStore([
        {"turn_id": 1, "status": "success"},
        {"turn_id": 2, "status": "failed", "feedback": {"vote": "down"}},
        {"turn_id": 3, "status": "failed"},
    ])
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    response = client.get(
        "/agentic/sessions/sess-1/turns?status=failed&has_feedback=true",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "turns": [{"turn_id": 2, "status": "failed", "feedback": {"vote": "down"}}],
        "count": 1,
    }


def _write_system_profiles(tmp_path, monkeypatch):
    import config.system_config as system_config_module
    from system_config_migration_helpers import apply_test_system_config_migrations

    runtime_root = tmp_path / ".onto-runtime"
    config_dir = runtime_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "default_profile_id": "fast",
                    "profiles": [
                        {
                            "id": "fast",
                            "name": "Fast Global",
                            "provider": "openai_compatible",
                            "base_url": "https://fast.example/v1",
                            "model": "fast-model",
                            "api_key": "fast-secret",
                            "enabled": True,
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=runtime_root,
    )


def test_create_session_accepts_workspace_private_model_profile(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    _write_system_profiles(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    update_workspace_llm_profiles(
        "alpha",
        {
            "profiles": [
                {
                    "id": "private",
                    "name": "Private Model",
                    "provider": "openai_compatible",
                    "base_url": "https://private.example/v1",
                    "model": "private-model",
                    "api_key": "private-secret",
                    "enabled": True,
                }
            ]
        },
    )
    chat_store = _FakeChatStore()
    execution_store = _FakeExecutionStore()
    monkeypatch.setattr("api.routes.sessions._get_request_workspace_id", lambda: "alpha")
    monkeypatch.setattr("api.routes.sessions._get_user_stores", lambda: (chat_store, execution_store))

    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "workspace:private"},
        headers=headers,
    )

    assert response.status_code == 201
    metadata = response.get_json()["metadata"]
    assert metadata["model_profile_id"] == "workspace:private"
    assert metadata["model_profile_ref"] == {"scope": "workspace", "id": "private"}


def test_create_session_rejects_unknown_prefixed_profile_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    _write_system_profiles(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    monkeypatch.setattr("api.routes.sessions._get_request_workspace_id", lambda: "alpha")

    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "team:main"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "模型配置不存在" in response.get_json()["detail"]


def test_create_session_rejects_empty_scoped_profile_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    _write_system_profiles(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    monkeypatch.setattr("api.routes.sessions._get_request_workspace_id", lambda: "alpha")

    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "global:"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "不能为空" in response.get_json()["detail"]


def _stub_session_create_dependencies(monkeypatch, chat_store, *, workspace_id="alpha"):
    execution_store = _FakeExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: workspace_id,
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )
    monkeypatch.setattr(
        "api.routes.sessions.resolve_effective_llm_profile_config",
        lambda *args, **kwargs: {
            "id": "fast",
            "model_profile_ref": {"scope": "global", "id": "fast"},
        },
    )


def test_create_session_locks_enabled_selected_ontology_in_request_workspace(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store, workspace_id="alpha")
    active_workspaces = []
    scope_events = []

    @contextmanager
    def fake_workspace_scope(workspace_id):
        scope_events.append(("enter", workspace_id))
        active_workspaces.append(workspace_id)
        try:
            yield workspace_id
        finally:
            active_workspaces.pop()
            scope_events.append(("exit", workspace_id))

    class FakeRegistry:
        def refresh(self):
            raise AssertionError("session creation must use the maintained registry cache")

        def get_ontology(self, ontology_id):
            assert active_workspaces == ["alpha"]
            if ontology_id == "demo-ontology":
                return SimpleNamespace(id="demo-ontology", enabled=True)
            return None

    registry = FakeRegistry()
    monkeypatch.setattr("api.routes.sessions.workspace_scope", fake_workspace_scope)
    monkeypatch.setattr("api.routes.sessions.get_ontology_registry", lambda **_kwargs: registry)

    response = client.post(
        "/agentic/sessions",
        json={"selected_ontology_id": "  demo-ontology  "},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.get_json()["metadata"]["selected_ontology_id"] == "demo-ontology"
    assert chat_store.created_sessions[0]["metadata"]["selected_ontology_id"] == "demo-ontology"
    assert scope_events == [("enter", "alpha"), ("exit", "alpha")]


def test_create_session_omits_empty_selected_ontology_metadata(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store)
    monkeypatch.setattr(
        "api.routes.sessions.get_ontology_registry",
        lambda: (_ for _ in ()).throw(AssertionError("empty selection must not load registry")),
    )

    for selected_ontology_id in (None, "", "   "):
        response = client.post(
            "/agentic/sessions",
            json={"selected_ontology_id": selected_ontology_id},
            headers=headers,
        )
        assert response.status_code == 201
        assert "selected_ontology_id" not in response.get_json()["metadata"]

    assert all(
        "selected_ontology_id" not in item["metadata"]
        for item in chat_store.created_sessions
    )


def test_create_session_rejects_selected_ontology_missing_from_workspace(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store, workspace_id="alpha")

    class EmptyRegistry:
        def refresh(self):
            return 0

        def get_ontology(self, ontology_id):
            del ontology_id
            return None

    monkeypatch.setattr("api.routes.sessions.get_ontology_registry", lambda **_kwargs: EmptyRegistry())

    response = client.post(
        "/agentic/sessions",
        json={"selected_ontology_id": "beta-only"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"detail": "知识包不存在: beta-only"}
    assert chat_store.created_sessions == []


def test_create_session_rejects_disabled_selected_ontology(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store)

    class DisabledRegistry:
        def refresh(self):
            return 1

        def get_ontology(self, ontology_id):
            return SimpleNamespace(id=ontology_id, enabled=False)

    monkeypatch.setattr("api.routes.sessions.get_ontology_registry", lambda **_kwargs: DisabledRegistry())

    response = client.post(
        "/agentic/sessions",
        json={"selected_ontology_id": "disabled-ontology"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"detail": "知识包已禁用: disabled-ontology"}
    assert chat_store.created_sessions == []


def test_get_session_no_longer_exposes_delta_polling(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="sess-live", title="Live")
    chat_store.set_active_run("sess-live", "run-live")
    chat_store.append_events(
        "sess-live",
        [
            {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-live:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sess-live",
                    "run_id": "run-live",
                    "turn_id": 1,
                    "type": "turn.started",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"user_preview": "hello"},
                },
            },
            {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-live:2",
                    "seq": 2,
                    "ts": 1700000000001,
                    "session_id": "sess-live",
                    "run_id": "run-live",
                    "turn_id": 1,
                    "type": "token.delta",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"channel": "content", "text": "streaming"},
                },
            },
            {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-live:3",
                    "seq": 3,
                    "ts": 1700000000002,
                    "session_id": "sess-live",
                    "run_id": "run-live",
                    "turn_id": 1,
                    "type": "tool.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"tool_name": "ontology_context", "status": "success"},
                },
            },
        ],
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, object()),
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )

    response = client.get(
        "/agentic/sessions/sess-live?since=1",
        headers=headers,
    )

    assert response.status_code == 410
    payload = response.get_json()
    assert "delta polling has been removed" in payload["error"]


def test_get_tool_output_returns_complete_content_with_no_store_cache(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "owner-chats"))
    chat_store.create_session(session_id="sess-output", title="Trace")
    complete_output = "完整工具输出🙂" * 900
    artifact = chat_store.store_tool_output(
        "sess-output",
        "run-output",
        "call-output",
        complete_output,
    )
    assert artifact is not None
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )

    response = client.get(
        "/agentic/sessions/sess-output/runs/run-output/tool-outputs/{}".format(
            artifact["output_ref"]
        ),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.mimetype == "text/plain"
    assert response.get_data(as_text=True) == complete_output
    assert response.headers["X-Tool-Output-Size"] == str(
        len(complete_output.encode("utf-8"))
    )
    assert response.headers["X-Tool-Call-Id"] == "call-output"
    assert response.headers["X-Tool-Output-Ref"] == artifact["output_ref"]


def test_get_tool_output_rejects_invalid_workspace_before_store_lookup(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo", workspace_id="demo", set_as_default=True)
    client, headers = _build_test_client(tmp_path)

    def unexpected_store_lookup():
        raise AssertionError("invalid workspace must be rejected before store lookup")

    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        unexpected_store_lookup,
    )
    response = client.get(
        "/agentic/sessions/sess-output/runs/run-output/tool-outputs/{}".format(
            "a" * 32
        ),
        headers={**headers, "X-Workspace-Id": "../invalid"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "detail": "工作空间 ID 格式非法：必须以英文小写字母开头，只能包含英文、数字、下划线(_)或中划线(-)",
    }


def test_get_tool_output_enforces_workspace_session_and_run_scope(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    owner_store = ChatStore(str(tmp_path / "workspace-alpha"))
    owner_store.create_session(session_id="shared-session")
    artifact = owner_store.store_tool_output(
        "shared-session",
        "owner-run",
        "owner-call",
        "private output",
    )
    assert artifact is not None

    foreign_workspace_store = ChatStore(str(tmp_path / "workspace-beta"))
    foreign_workspace_store.create_session(session_id="shared-session")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (foreign_workspace_store, _FakeExecutionStore()),
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )
    foreign_response = client.get(
        "/agentic/sessions/shared-session/runs/owner-run/tool-outputs/{}".format(
            artifact["output_ref"]
        ),
        headers=headers,
    )
    assert foreign_response.status_code == 404

    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (owner_store, _FakeExecutionStore()),
    )
    wrong_run_response = client.get(
        "/agentic/sessions/shared-session/runs/another-run/tool-outputs/{}".format(
            artifact["output_ref"]
        ),
        headers=headers,
    )
    assert wrong_run_response.status_code == 404

    wrong_session_response = client.get(
        "/agentic/sessions/missing-session/runs/owner-run/tool-outputs/{}".format(
            artifact["output_ref"]
        ),
        headers=headers,
    )
    assert wrong_session_response.status_code == 404


def test_get_session_exposes_active_run_status_for_resume(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="sess-live", title="Live")
    chat_store.set_active_run("sess-live", "run-live")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, object()),
    )
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )

    response = client.get("/agentic/sessions/sess-live", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active_run_id"] == "run-live"
    assert payload["active_run_status"] == "interrupted"


class _FakeRunHandle:
    """Stand-in for AgenticRunHandle for list-enrichment tests."""

    def __init__(
        self,
        session_id,
        run_id,
        *,
        user_id="admin",
        workspace_id="test-workspace",
        status="running",
        done=False,
        last_seq=0,
    ):
        self.session_id = session_id
        self.run_id = run_id
        self.active_run_id = run_id
        self.user_id = user_id
        self.workspace_id = workspace_id
        self._status = status
        self._last_seq = last_seq
        self.done = threading.Event()
        if done:
            self.done.set()

    def runtime_snapshot(self):
        return {"active_run_id": self.active_run_id, "status": self._status, "last_seq": self._last_seq}


class _FakeRunRegistry:
    def __init__(self, handles):
        self._handles = list(handles)

    def snapshot_by_session(self, *, user_id, workspace_id):
        return {
            h.session_id: h
            for h in self._handles
            if h.session_id and h.user_id == user_id and h.workspace_id == workspace_id
        }

    def get(self, run_id):
        return next((h for h in self._handles if h.run_id == run_id), None)

    def find_by_session(self, session_id, *, user_id, workspace_id):
        return next(
            (
                h
                for h in self._handles
                if h.session_id == session_id
                and h.user_id == user_id
                and h.workspace_id == workspace_id
            ),
            None,
        )


def test_list_sessions_enriches_with_live_run_handle_in_one_pass(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="sess-a", title="A")
    chat_store.create_session(session_id="sess-b", title="B")
    chat_store.set_active_run("sess-a", "run-a")
    monkeypatch.setattr("api.routes.sessions._get_user_stores", lambda: (chat_store, object()))
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "test-workspace",
    )
    live_handle = _FakeRunHandle("sess-a", "run-a", status="running", done=False, last_seq=7)
    monkeypatch.setattr("api.routes.sessions._get_run_registry", lambda: _FakeRunRegistry([live_handle]))

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 200
    by_id = {s["id"]: s for s in response.get_json()["sessions"]}
    # Live handle resolved from the one-pass snapshot -> running, no O(runs) scan.
    assert by_id["sess-a"]["active_run_status"] == "running"
    assert by_id["sess-a"]["active_run_id"] == "run-a"
    assert by_id["sess-a"]["last_seq"] == 7
    # No handle and no active_run_id -> idle.
    assert by_id["sess-b"]["active_run_status"] == "idle"


def test_list_sessions_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)

    def raise_missing_workspace():
        raise ValueError("当前不存在任何工作空间")

    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        raise_missing_workspace,
    )

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"detail": "当前不存在任何工作空间"}


def test_create_session_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)

    def raise_missing_workspace():
        raise ValueError("当前不存在任何工作空间")

    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        raise_missing_workspace,
    )

    response = client.post("/agentic/sessions", json={}, headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"detail": "当前不存在任何工作空间"}


def test_get_session_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)

    def raise_missing_workspace():
        raise ValueError("当前不存在任何工作空间")

    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        raise_missing_workspace,
    )

    response = client.get("/agentic/sessions/session-1", headers=headers)

    assert response.status_code == 400
    assert response.get_json() == {"detail": "当前不存在任何工作空间"}


def test_list_sessions_does_not_attach_foreign_handle_with_same_session_id(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="shared-session", title="Mine")
    monkeypatch.setattr("api.routes.sessions._get_user_stores", lambda: (chat_store, object()))
    monkeypatch.setattr(
        "api.routes.sessions._get_request_workspace_id",
        lambda: "my-workspace",
    )

    registry = AgenticRunRegistry()
    foreign_handle = _FakeRunHandle(
        "shared-session",
        "foreign-run",
        user_id="alice",
        workspace_id="private-workspace",
        last_seq=41,
    )
    with registry._lock:
        registry._runs[foreign_handle.run_id] = foreign_handle
    monkeypatch.setattr("api.routes.sessions._get_run_registry", lambda: registry)

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 200
    session = response.get_json()["sessions"][0]
    assert session["active_run_id"] is None
    assert session["active_run_status"] == "idle"
    assert session["last_seq"] == 0
