"""HTTP route tests for chat session management."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.runtime.errors import SessionBusyError
from api.testing import TestApplication
from config.workspace_llm_profiles import update_workspace_llm_profiles
from api.routes.sessions import _converge_session_prune_batches, sessions_router
from auth_test_backend import AuthManager
from persistence.chat_store import ChatStore
from persistence.execution_store import (
    INTERACTION_CANCELLED_MESSAGE,
    INTERACTION_CANCELLED_NOTICE,
    ExecutionStore,
)
from persistence.session_summary_index import SessionSummary, SessionSummaryIndex
from utils.streaming import build_agent_event
from workspace.user_store_manager import UserStoreManager
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

    def delete_session(self, session_id):
        if not self.session or self.session.id != session_id:
            return False
        self.session = None
        return True


class _FakeExecutionStore:
    def __init__(self, turns=None):
        self.deleted_sessions = []
        self.turns = list(turns or [])

    def delete_session(self, session_id):
        self.deleted_sessions.append(session_id)
        return True

    def list_turns_by_status(self, session_id, status=None):
        del session_id
        rows = list(reversed(self.turns))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def get_session_history(self, session_id):
        del session_id
        return {"turns": list(self.turns)}


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _GuardedPool:
    def __init__(self):
        self.active = False
        self.calls = []

    async def run(self, function, *args, **kwargs):
        self.active = True
        self.calls.append(getattr(function, "__name__", type(function).__name__))
        try:
            return function(*args, **kwargs)
        finally:
            self.active = False


class _AvailableManager:
    async def list_runs(self, _scope):
        return []

    @asynccontextmanager
    async def session_guard(self, _scope, _session_id):
        yield

    async def assert_session_available(self, _scope, _session_id):
        return None


def test_background_prune_converges_across_bounded_batches():
    calls = []

    def prune(batch_size):
        calls.append(batch_size)
        remaining = max(0, 60 - (len(calls) - 1) * batch_size)
        deleted = tuple(
            f"session-{index}"
            for index in range(min(batch_size, remaining))
        )
        return SimpleNamespace(
            deleted_session_ids=deleted,
            has_more=remaining > batch_size,
            candidates_exhausted=remaining <= batch_size,
            no_progress=False,
        )

    deleted = asyncio.run(
        _converge_session_prune_batches(
            SimpleNamespace(short_io=_InlinePool()),
            prune,
            batch_size=25,
        )
    )

    assert deleted == 60
    assert calls == [25, 25, 25]


def test_background_prune_uses_explicit_has_more_not_deleted_batch_length():
    calls = []
    outcomes = [
        SimpleNamespace(
            deleted_session_ids=(),
            has_more=True,
            candidates_exhausted=False,
            no_progress=True,
        ),
        SimpleNamespace(
            deleted_session_ids=("deleted",),
            has_more=False,
            candidates_exhausted=True,
            no_progress=False,
        ),
    ]

    def prune(_batch_size):
        calls.append(True)
        return outcomes.pop(0)

    deleted = asyncio.run(
        _converge_session_prune_batches(
            SimpleNamespace(short_io=_InlinePool()),
            prune,
            batch_size=25,
        )
    )

    assert deleted == 1
    assert calls == [True, True]


def test_background_prune_stops_after_bounded_no_progress():
    calls = []

    def prune(_batch_size):
        calls.append(True)
        return SimpleNamespace(
            deleted_session_ids=(),
            has_more=True,
            candidates_exhausted=False,
            no_progress=True,
        )

    deleted = asyncio.run(
        _converge_session_prune_batches(
            SimpleNamespace(short_io=_InlinePool()),
            prune,
            batch_size=25,
            deadline_seconds=1.0,
            max_no_progress=3,
            initial_backoff_seconds=0,
        )
    )

    assert deleted == 0
    assert len(calls) == 3


def test_background_prune_is_single_flight_per_user_workspace():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class ThreadPool:
        async def run(self, function, *args, **kwargs):
            return await asyncio.to_thread(function, *args, **kwargs)

    def prune(_batch_size):
        calls.append(True)
        started.set()
        assert release.wait(timeout=1.0)
        return SimpleNamespace(
            deleted_session_ids=(),
            has_more=False,
            candidates_exhausted=True,
            no_progress=False,
        )

    async def scenario():
        runtime = SimpleNamespace(short_io=ThreadPool())
        first = asyncio.create_task(
            _converge_session_prune_batches(
                runtime,
                prune,
                convergence_key=("workspace-single-flight", "alice"),
            )
        )
        while not started.is_set():
            await asyncio.sleep(0)
        second = asyncio.create_task(
            _converge_session_prune_batches(
                runtime,
                prune,
                convergence_key=("workspace-single-flight", "alice"),
            )
        )
        assert await asyncio.wait_for(second, timeout=0.25) == 0
        release.set()
        assert await asyncio.wait_for(first, timeout=1.0) == 0

    asyncio.run(scenario())

    assert calls == [True]


class _BusyManager(_AvailableManager):
    async def assert_session_available(self, _scope, _session_id):
        raise SessionBusyError("run-active")


def _build_test_client(
    tmp_path,
    monkeypatch,
    *,
    create_default_workspace=True,
    manager=None,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    if create_default_workspace:
        create_workspace(
            "Session Test Workspace",
            workspace_id="session-test",
            set_as_default=True,
        )
    app = TestApplication()
    app.state.services["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
    assert user is not None

    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(sessions_router)
    runtime = SimpleNamespace(
        short_io=_InlinePool(),
        external_io=_InlinePool(),
        manager=manager or _AvailableManager(),
    )
    app.state.runtime_container = runtime
    app.state.agent_execution_runtime = runtime

    client = TestClient(app)
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def test_update_session_pinned_state(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    assert response.json()["metadata"]["pinned"] is True
    assert chat_store.pinned_updates == [("sess-1", True)]


def test_update_session_pinned_state_requires_boolean(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    assert response.json()["error"] == "pinned must be a boolean"
    assert chat_store.pinned_updates == []


def test_update_session_pinned_state_returns_404_for_missing_session(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    assert response.json()["error"] == "Session not found"


def test_delete_session_removes_stores_after_runtime_guard(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = _FakeChatStore(_FakeSession("sess-delete"))
    execution_store = _FakeExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    response = client.delete(
        "/agentic/sessions/sess-delete",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert chat_store.session is None
    assert execution_store.deleted_sessions == ["sess-delete"]


def test_delete_session_preserves_stores_when_run_is_active(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        manager=_BusyManager(),
    )
    chat_store = _FakeChatStore(_FakeSession("sess-delete"))
    execution_store = _FakeExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    response = client.delete(
        "/agentic/sessions/sess-delete",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "SESSION_RUN_ACTIVE"
    assert response.json()["run_id"] == "run-active"
    assert chat_store.session is not None
    assert execution_store.deleted_sessions == []


def test_cancel_pending_interaction_is_durable_and_idempotent(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    cancel_headers = {**headers, "Idempotency-Key": "cancel-interaction-1"}

    stale = client.post(
        "/agentic/sessions/sess-cancel/interactions/stale/cancel",
        headers=cancel_headers,
    )
    assert stale.status_code == 404

    url = "/agentic/sessions/sess-cancel/interactions/interaction-cancel-1/cancel"
    response = client.post(url, headers=cancel_headers)
    retry = client.post(url, headers=cancel_headers)

    assert response.status_code == 200
    assert retry.status_code == 200
    assert response.json()["message"] == INTERACTION_CANCELLED_MESSAGE
    assert response.json()["display_message"] == INTERACTION_CANCELLED_NOTICE
    source_turn = execution_store.get_turn("sess-cancel", 1)
    assert source_turn["status"] == "awaiting_input"
    assert source_turn["interaction_consumed_at"]
    assert "interaction_consumed_by_turn_id" not in source_turn
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

    refreshed = client.get("/agentic/sessions/sess-cancel", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["active_run_status"] == "awaiting_input"
    assert "pending_interaction" not in refreshed.json()


def test_list_session_turns_filters_status_and_feedback(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    assert response.json() == {
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
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    monkeypatch.setattr("api.routes.sessions._get_user_stores", lambda: (chat_store, execution_store))

    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "workspace:private"},
        headers={**headers, "X-Workspace-Id": "alpha"},
    )

    assert response.status_code == 201
    metadata = response.json()["metadata"]
    assert metadata["model_profile_id"] == "workspace:private"
    assert metadata["model_profile_ref"] == {"scope": "workspace", "id": "private"}


def test_create_session_rejects_unknown_prefixed_profile_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    _write_system_profiles(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "team:main"},
        headers={**headers, "X-Workspace-Id": "alpha"},
    )

    assert response.status_code == 400
    assert "模型配置不存在" in response.json()["detail"]


def test_create_session_rejects_empty_scoped_profile_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    _write_system_profiles(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    response = client.post(
        "/agentic/sessions",
        json={"model_profile_id": "global:"},
        headers={**headers, "X-Workspace-Id": "alpha"},
    )

    assert response.status_code == 400
    assert "不能为空" in response.json()["detail"]


def _stub_session_create_dependencies(monkeypatch, chat_store):
    execution_store = _FakeExecutionStore()
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
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store)
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
            assert active_workspaces == ["session-test"]
            if ontology_id == "demo-ontology":
                return SimpleNamespace(
                    id="demo-ontology",
                    name="Demo Ontology",
                    enabled=True,
                    active_version="2.1.0",
                    version="2.1.0",
                )
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
    assert response.json()["metadata"]["selected_ontology_id"] == "demo-ontology"
    assert response.json()["metadata"]["selected_ontology_version"] == "2.1.0"
    assert response.json()["metadata"]["selected_ontology_name"] == "Demo Ontology"
    assert chat_store.created_sessions[0]["metadata"]["selected_ontology_id"] == "demo-ontology"
    assert chat_store.created_sessions[0]["metadata"]["selected_ontology_version"] == "2.1.0"
    assert chat_store.created_sessions[0]["metadata"]["selected_ontology_name"] == "Demo Ontology"
    assert scope_events == [
        ("enter", "session-test"),
        ("exit", "session-test"),
    ]


def test_create_session_omits_empty_selected_ontology_metadata(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
        assert "selected_ontology_id" not in response.json()["metadata"]

    assert all(
        "selected_ontology_id" not in item["metadata"]
        for item in chat_store.created_sessions
    )


def test_create_session_rejects_selected_ontology_missing_from_workspace(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store)

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
    assert response.json() == {"detail": "知识包不存在: beta-only"}
    assert chat_store.created_sessions == []


def test_create_session_rejects_disabled_selected_ontology(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    assert response.json() == {"detail": "知识包已禁用: disabled-ontology"}
    assert chat_store.created_sessions == []


def test_get_session_no_longer_exposes_delta_polling(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    response = client.get(
        "/agentic/sessions/sess-live?since=1",
        headers=headers,
    )

    assert response.status_code == 410
    payload = response.json()
    assert "delta polling has been removed" in payload["error"]


def test_get_tool_output_returns_complete_content_with_no_store_cache(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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
    response = client.get(
        "/agentic/sessions/sess-output/runs/run-output/tool-outputs/{}".format(
            artifact["output_ref"]
        ),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == complete_output
    assert response.headers["X-Tool-Output-Size"] == str(
        len(complete_output.encode("utf-8"))
    )
    assert response.headers["X-Tool-Call-Id"] == "call-output"
    assert response.headers["X-Tool-Output-Ref"] == artifact["output_ref"]
    assert response.headers["X-Tool-Output-SHA256"] == artifact["sha256"]
    assert "X-Tool-Output-SHA256" in response.headers[
        "Access-Control-Expose-Headers"
    ]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "sandbox; default-src 'none'"


def test_get_tool_output_rejects_invalid_workspace_before_store_lookup(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo", workspace_id="demo", set_as_default=True)
    client, headers = _build_test_client(tmp_path, monkeypatch)

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
    assert response.json() == {
        "detail": "工作空间 ID 格式非法：必须以英文小写字母开头，只能包含英文、数字、下划线(_)或中划线(-)",
    }


def test_get_tool_output_enforces_workspace_session_and_run_scope(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
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


def test_get_session_exposes_interrupted_status_after_restart(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="sess-live", title="Live")
    chat_store.set_active_run("sess-live", "run-live")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (
            chat_store,
            _FakeExecutionStore(
                [
                    {
                        "run_id": "run-live",
                        "status": "failed",
                        "error_code": "SERVICE_RESTARTED",
                    }
                ]
            ),
        ),
    )

    response = client.get("/agentic/sessions/sess-live", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_run_id"] is None
    assert payload["active_run_status"] == "interrupted"


def test_list_sessions_enriches_with_async_run_snapshot_in_one_pass(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="sess-a", title="A")
    chat_store.create_session(session_id="sess-b", title="B")
    chat_store.set_active_run("sess-a", "run-a")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )
    live_run = SimpleNamespace(
        session_id="sess-a",
        run_id="run-a",
        status=SimpleNamespace(value="running"),
        turn_id=1,
        last_seq=7,
        updated_at="2026-01-01T00:00:00Z",
    )
    client.app.state.runtime_container.manager = SimpleNamespace(
        list_runs=lambda _scope: None,
    )

    async def list_runs(_scope):
        return [live_run]

    client.app.state.runtime_container.manager.list_runs = list_runs

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 200
    by_id = {s["id"]: s for s in response.json()["sessions"]}
    # The manager snapshot is loaded once for the whole session listing.
    assert by_id["sess-a"]["active_run_status"] == "running"
    assert by_id["sess-a"]["active_run_id"] == "run-a"
    assert by_id["sess-a"]["last_seq"] == 7
    # No handle and no active_run_id -> idle.
    assert by_id["sess-b"]["active_run_status"] == "idle"


def test_list_sessions_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        create_default_workspace=False,
    )

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "当前不存在任何工作空间"}


def test_create_session_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        create_default_workspace=False,
    )

    response = client.post("/agentic/sessions", json={}, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "当前不存在任何工作空间"}


def test_get_session_reports_missing_workspace_as_bad_request(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        create_default_workspace=False,
    )

    response = client.get("/agentic/sessions/session-1", headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "当前不存在任何工作空间"}


def test_list_sessions_does_not_attach_run_outside_async_scope(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = ChatStore(str(tmp_path / "chats"))
    chat_store.create_session(session_id="shared-session", title="Mine")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 200
    session = response.json()["sessions"][0]
    assert session["active_run_id"] is None
    assert session["active_run_status"] == "idle"
    assert session["last_seq"] == 0


class _IndexedChatStore:
    def __init__(self, index):
        self.session_summary_index = index
        self.list_calls = 0
        self.get_calls = []

    def list_sessions(self):
        self.list_calls += 1
        raise AssertionError("indexed listing must not enumerate chat sessions")

    def get_session(self, session_id):
        self.get_calls.append(session_id)
        return _FakeSession(session_id)


class _CountingExecutionStore(_FakeExecutionStore):
    def __init__(self):
        super().__init__()
        self.history_calls = []

    def get_session_history(self, session_id):
        self.history_calls.append(session_id)
        return super().get_session_history(session_id)


class _FaultingSummaryIndex:
    def __init__(self, stage):
        self.stage = stage

    @property
    def state(self):
        return "degraded" if self.stage == "rebuild" else "ready"

    def rebuild(self):
        raise OSError("synthetic index rebuild failure")

    def list_all(self):
        raise AssertionError("legacy fallback must not read the failed index")

    def list_page(self, **_options):
        raise OSError("synthetic index page failure")


def _summary_record(index, *, pending=False):
    status = "awaiting_input" if pending else "idle"
    interaction = (
        {"id": "interaction-{}".format(index), "type": "input"}
        if pending
        else None
    )
    return SessionSummary.create(
        session_id="session-{:04d}".format(index),
        title="Synthetic {}".format(index),
        created_at="2026-08-31T00:00:{:04d}+00:00".format(index),
        updated_at="2026-08-31T00:00:{:04d}+00:00".format(index),
        active_run_id="run-{:04d}".format(index) if pending else None,
        metadata={"pinned": False},
        last_run_status=status,
        pending_interaction=interaction,
    )


def test_create_session_schedules_prune_until_retention_converges(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = _FakeChatStore()
    _stub_session_create_dependencies(monkeypatch, chat_store)
    calls = []
    scheduled = []

    def prune_request_sessions_batch(*, max_sessions, batch_size):
        del max_sessions
        calls.append(batch_size)
        deleted = tuple(
            f"deleted-{index}"
            for index in range(batch_size if len(calls) < 3 else 3)
        )
        return SimpleNamespace(
            deleted_session_ids=deleted,
            has_more=len(calls) < 3,
            candidates_exhausted=len(calls) >= 3,
            no_progress=False,
        )

    class Supervisor:
        def start_derived(self, operation, *, name):
            scheduled.append((operation, name))

    monkeypatch.setattr(
        "api.routes.prune_request_sessions_batch",
        prune_request_sessions_batch,
    )
    client.app.state.runtime_container.supervisor = Supervisor()

    response = client.post("/agentic/sessions", json={}, headers=headers)

    assert response.status_code == 201
    assert len(scheduled) == 1
    asyncio.run(scheduled[0][0])
    assert calls == [25, 25, 25]
    assert scheduled[0][1].startswith("session-prune-convergence-")


def test_list_sessions_uses_revisioned_index_pagination_without_detail_reads(
    tmp_path,
    monkeypatch,
):
    records = [_summary_record(index) for index in range(3)]
    index = SessionSummaryIndex(
        tmp_path / "session-summary-index.json",
        rebuild_loader=lambda: records,
    )
    index.rebuild()
    chat_store = _IndexedChatStore(index)
    execution_store = _CountingExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )
    client, headers = _build_test_client(tmp_path, monkeypatch)

    first = client.get("/agentic/sessions?limit=2", headers=headers)
    assert first.status_code == 200
    first_payload = first.json()
    assert [item["id"] for item in first_payload["sessions"]] == [
        "session-0002",
        "session-0001",
    ]
    assert first_payload["has_more"] is True
    assert first_payload["next_cursor"]
    assert isinstance(first_payload["index_revision"], int)

    second = client.get(
        "/agentic/sessions",
        params={"limit": 2, "cursor": first_payload["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["sessions"]] == [
        "session-0000"
    ]
    assert second.json()["has_more"] is False
    assert second.json()["next_cursor"] is None
    assert chat_store.list_calls == 0
    assert chat_store.get_calls == []
    assert execution_store.history_calls == []

    legacy = client.get("/agentic/sessions", headers=headers)
    assert legacy.status_code == 200
    assert set(legacy.json()) == {"sessions"}
    assert len(legacy.json()["sessions"]) == 3

    invalid = client.get(
        "/agentic/sessions?limit=2&cursor=not-a-cursor",
        headers=headers,
    )
    assert invalid.status_code == 400


def test_disabled_summary_index_keeps_full_legacy_list_response(tmp_path, monkeypatch):
    monkeypatch.setenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "false")
    client, headers = _build_test_client(tmp_path, monkeypatch)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("admin", "session-test")
    execution_store = manager.get_execution_store("admin", "session-test")
    for index in range(3):
        chat_store.create_session(
            session_id="legacy-{}".format(index),
            title="Legacy {}".format(index),
        )
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 200
    assert set(response.json()) == {"sessions"}
    assert {item["id"] for item in response.json()["sessions"]} == {
        "legacy-0",
        "legacy-1",
        "legacy-2",
    }
    assert manager.get_session_summary_index("admin", "session-test") is None


def test_cursor_without_limit_is_rejected_even_when_summary_index_is_disabled(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "false")
    client, headers = _build_test_client(tmp_path, monkeypatch)
    manager = UserStoreManager("", "")
    chat_store = manager.get_chat_store("admin", "session-test")
    execution_store = manager.get_execution_store("admin", "session-test")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )

    response = client.get(
        "/agentic/sessions",
        params={"cursor": "opaque-cursor"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json() == {"error": "limit is required when cursor is provided"}


def test_index_rebuild_failure_returns_explicit_error_for_legacy_full_list(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = ChatStore(str(tmp_path / "legacy-fallback-chats"))
    chat_store.create_session(session_id="legacy-fallback", title="Legacy fallback")
    chat_store._summary_index = _FaultingSummaryIndex("rebuild")
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )

    response = client.get("/agentic/sessions", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"error": "session summary index unavailable"}


@pytest.mark.parametrize("failure_stage", ["rebuild", "list_page"])
def test_index_failure_returns_explicit_error_for_paginated_requests(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    chat_store = ChatStore(str(tmp_path / "paged-index-failure" / failure_stage))
    chat_store.create_session(session_id="paged-session", title="Paged")
    chat_store._summary_index = _FaultingSummaryIndex(failure_stage)
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )

    response = client.get(
        "/agentic/sessions",
        params={"limit": 50},
        headers=headers,
    )

    assert response.status_code == 503
    assert response.json() == {"error": "session summary index unavailable"}


def test_index_store_construction_and_page_read_are_offloaded(tmp_path, monkeypatch):
    records = [_summary_record(index) for index in range(2)]
    index = SessionSummaryIndex(
        tmp_path / "session-summary-index.json",
        rebuild_loader=lambda: records,
    )
    index.rebuild()
    chat_store = _IndexedChatStore(index)
    execution_store = _CountingExecutionStore()
    client, headers = _build_test_client(tmp_path, monkeypatch)
    pool = _GuardedPool()
    client.app.state.runtime_container.short_io = pool

    def guarded_stores():
        assert pool.active is True
        return chat_store, execution_store

    monkeypatch.setattr("api.routes.sessions._get_user_stores", guarded_stores)
    original_list_page = index.list_page
    monkeypatch.setattr(
        index,
        "list_page",
        lambda **options: (
            original_list_page(**options)
            if pool.active is True
            else (_ for _ in ()).throw(AssertionError("page read blocked event loop"))
        ),
    )

    response = client.get("/agentic/sessions?limit=1", headers=headers)

    assert response.status_code == 200
    assert response.json()["sessions"][0]["id"] == "session-0001"
    assert pool.calls


@pytest.mark.parametrize("query", ["", "?limit=50"])
def test_cold_missing_index_never_runs_full_rebuild_in_request_path(
    tmp_path,
    monkeypatch,
    query,
):
    rebuild_threads = []
    index = SessionSummaryIndex(
        tmp_path / "missing-session-summary-index.json",
        rebuild_loader=lambda: rebuild_threads.append(threading.current_thread().name) or [],
    )
    chat_store = _IndexedChatStore(index)
    execution_store = _CountingExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )
    client, headers = _build_test_client(tmp_path, monkeypatch)

    response = client.get(f"/agentic/sessions{query}", headers=headers)

    assert response.status_code == 503
    assert rebuild_threads == []


def test_empty_authoritative_store_publishes_empty_index_on_first_request(
    tmp_path,
    monkeypatch,
):
    holder = {}
    index = SessionSummaryIndex(
        tmp_path / "empty-session-summary-index.json",
        rebuild_loader=lambda: holder["store"].rebuild_session_summaries(),
    )
    chat_store = ChatStore(
        str(tmp_path / "empty-chats"),
        summary_index=index,
    )
    holder["store"] = chat_store
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, _FakeExecutionStore()),
    )
    client, headers = _build_test_client(tmp_path, monkeypatch)

    response = client.get("/agentic/sessions?limit=50", headers=headers)

    assert response.status_code == 200
    assert response.json()["sessions"] == []
    assert index.state == "ready"


def test_first_page_of_1000_sessions_recovers_only_page_active_or_pending_items(
    tmp_path,
    monkeypatch,
):
    records = [
        _summary_record(index, pending=index in {0, 999})
        for index in range(1000)
    ]
    index = SessionSummaryIndex(
        tmp_path / "session-summary-index.json",
        rebuild_loader=lambda: records,
    )
    index.rebuild()
    chat_store = _IndexedChatStore(index)
    execution_store = _CountingExecutionStore()
    monkeypatch.setattr(
        "api.routes.sessions._get_user_stores",
        lambda: (chat_store, execution_store),
    )
    client, headers = _build_test_client(tmp_path, monkeypatch)

    response = client.get("/agentic/sessions?limit=50", headers=headers)

    assert response.status_code == 200
    assert len(response.json()["sessions"]) == 50
    assert response.json()["sessions"][0]["id"] == "session-0999"
    assert chat_store.list_calls == 0
    assert chat_store.get_calls == ["session-0999"]
    assert execution_store.history_calls == ["session-0999"]
