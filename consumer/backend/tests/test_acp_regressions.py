"""ACP protocol regressions for canonical Run identity and one-run HITL."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from llm import LLMPurpose
import utils.lang as lang_module
from agent.runtime.contracts import (
    PublicEvent,
    RunPreflightSnapshot,
    RunSnapshot,
    RunStatus as CanonicalRunStatus,
    RuntimeInvocationContext,
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
)
from api.auth.auth import AuthUser
from api.protocols.acp import server as acp_server
from api.protocols.acp.models import RunStatus
from api.testing import TestApplication
from persistence.execution_store import ExecutionStore
from utils.lang import _detect_language_via_llm, detect_language, translate_text


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _Manager:
    def __init__(self):
        self.snapshots = {}
        self.contexts = []

    @asynccontextmanager
    async def session_guard(self, _scope, _session_id):
        yield

    async def assert_session_available(self, _scope, _session_id):
        return None

    async def create_run(self, context, scope, *, session_guarded=False):
        assert session_guarded is True
        snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=CanonicalRunStatus.QUEUED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            last_seq=0,
            scope=scope,
            context=context,
        )
        self.contexts.append(context)
        self.snapshots[context.run_id] = snapshot
        return snapshot, True

    async def get_run(self, run_id, scope):
        snapshot = self.snapshots.get(run_id)
        return snapshot if snapshot is not None and scope.owns(snapshot) else None


def _request(manager, *, preflight_builder=None):
    app = TestApplication()
    runtime = SimpleNamespace(
        manager=manager,
        short_io=_InlinePool(),
        event_hub=SimpleNamespace(),
    )
    if preflight_builder is not None:
        runtime.protocol_preflight_builder = preflight_builder
    app.state.runtime_container = runtime
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/acp/runs",
        "raw_path": b"/acp/runs",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
        "state": {},
    }
    from starlette.requests import Request

    return Request(scope)


def test_acp_server_has_no_private_registry_or_flask_blueprint():
    assert not hasattr(acp_server, "_ACP_RUN_REGISTRY")
    assert not hasattr(acp_server, "acp_bp")
    assert acp_server.acp_router.prefix == "/acp"


def test_acp_forwards_explicit_deterministic_operation_and_arguments(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        manager = _Manager()
        request = _request(manager)
        store = ExecutionStore(str(tmp_path / "executions"))

        async def stores(_request_value, username, workspace_id):
            assert username == "operator_a"
            assert workspace_id == "workspace_demo"
            return SimpleNamespace(), store

        monkeypatch.setattr(acp_server, "_stores", stores)
        created = await acp_server._create_from_payload(
            request,
            {
                "messages": [
                    {
                        "role": "user",
                        "parts": [{"type": "text", "content": "synthetic request"}],
                    }
                ],
                "context": {"session_id": "session_explicit_operation"},
                "arguments": {"code": "CODE-42"},
                "requested_operation_key": "operation_demo",
            },
            AuthUser(username="operator_a", role="user"),
            "workspace_demo",
        )

        assert not isinstance(created, JSONResponse)
        assert manager.contexts[-1].arguments == {"code": "CODE-42"}
        assert (
            manager.contexts[-1].requested_operation_key
            == "operation_demo"
        )
        assert (
            manager.contexts[-1].concurrency_limit
            == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
        )

    from fastapi.responses import JSONResponse

    asyncio.run(scenario())


def test_acp_builds_one_dynamic_preflight_before_each_durable_turn(tmp_path):
    async def scenario():
        manager = _Manager()
        events = []
        calls = []
        expected = RunPreflightSnapshot(
            workspace_id="workspace_demo",
            workspace_config_generation=1,
            system_config_generation=1,
        )

        def build_preflight(**kwargs):
            events.append("preflight")
            calls.append(kwargs)
            return expected

        request = _request(manager, preflight_builder=build_preflight)
        store = ExecutionStore(str(tmp_path / "executions"))
        create_turn = store.create_turn_for_run

        def tracked_create_turn(*args, **kwargs):
            events.append("durable_turn")
            return create_turn(*args, **kwargs)

        store.create_turn_for_run = tracked_create_turn
        user = AuthUser(username="operator_a", role="user")
        for index in range(2):
            await acp_server._admit_run(
                request,
                user=user,
                workspace_id="workspace_demo",
                execution_store=store,
                external_run_id=f"external_run_{index}",
                session_id=f"session_dynamic_preflight_{index}",
                query=f"synthetic question {index}",
                context_values={"model_profile_id": "global:synthetic"},
                lang=None,
            )
        return manager, expected, calls, events

    manager, expected, calls, events = asyncio.run(scenario())

    assert len(calls) == 2
    assert [call["session_id"] for call in calls] == [
        "session_dynamic_preflight_0",
        "session_dynamic_preflight_1",
    ]
    assert events == [
        "preflight",
        "durable_turn",
        "preflight",
        "durable_turn",
    ]
    assert [context.preflight_snapshot for context in manager.contexts] == [
        expected,
        expected,
    ]


def test_protocol_preflight_uses_orchestrator_for_runtime_container(monkeypatch):
    from agent.runtime.runtime import RuntimeContainer
    import api.routes as routes
    from api.protocols.preflight import build_protocol_preflight_snapshot

    calls = []
    expected = RunPreflightSnapshot(
        workspace_id="workspace_demo",
        workspace_config_generation=1,
        system_config_generation=1,
    )

    def build_preflight(**kwargs):
        calls.append(kwargs)
        return expected

    class _ProductionRuntime(RuntimeContainer):
        def __init__(self):
            self.short_io = _InlinePool()

    monkeypatch.setattr(
        routes,
        "get_orchestrator",
        lambda: SimpleNamespace(build_run_preflight_snapshot=build_preflight),
    )
    result = asyncio.run(
        build_protocol_preflight_snapshot(
            _ProductionRuntime(),
            user_request="synthetic request",
            user_context={"agent_mode": "quick"},
            arguments={"code": "CODE-42"},
            session_id="session_demo",
            model_profile_id="global:synthetic",
            user_id="operator_a",
            workspace_id="workspace_demo",
        )
    )

    assert result is expected
    assert calls == [
        {
            "user_request": "synthetic request",
            "user_context": {"agent_mode": "quick"},
            "arguments": {"code": "CODE-42"},
            "session_id": "session_demo",
            "model_profile_id": "global:synthetic",
            "user_id": "operator_a",
            "workspace_id": "workspace_demo",
        }
    ]


def test_acp_public_event_keeps_external_id_as_metadata_only():
    event = PublicEvent(
        event_id="event_demo",
        seq=1,
        run_id="agentic-canonical-demo",
        session_id="session_demo",
        turn_id=1,
        event_type="turn.finished",
        status="completed",
        source={"component": "synthetic"},
        payload={"status": "completed", "content": "synthetic answer"},
        created_at="2026-01-01T00:00:01Z",
    )

    mapped = list(acp_server._map_public_event(event, "external_run_demo"))

    assert mapped[-1] == (
        "run.completed",
        {
            "type": "run.completed",
            "run_id": "external_run_demo",
            "canonical_run_id": "agentic-canonical-demo",
        },
    )


def test_acp_interaction_option_maps_to_canonical_value():
    interaction_id, value, option_id = acp_server._interaction_response_values(
        {"await_id": "interaction_demo", "option_id": "option_a"},
        {
            "id": "interaction_demo",
            "type": "single_choice",
            "options": [
                {"id": "option_a", "label": "Option A", "value": "value_a"}
            ],
        },
        "Option A",
    )

    assert interaction_id == "interaction_demo"
    assert option_id == "option_a"
    assert value == "value_a"


def test_acp_await_projection_preserves_prompt_and_interaction_id():
    awaiting = acp_server._build_await_response(
        {
            "question": "Choose a synthetic option",
            "tool_call_id": "interaction_demo",
            "options": [{"id": "option_a", "value": "value_a"}],
        }
    )

    assert awaiting["await_id"] == "interaction_demo"
    assert awaiting["message"]["parts"][0]["content"] == "Choose a synthetic option"


def test_acp_hitl_answer_allocates_a_new_canonical_run(tmp_path):
    async def scenario():
        manager = _Manager()
        request = _request(manager)
        store = ExecutionStore(str(tmp_path / "executions"))
        user = AuthUser(username="operator_a", role="user")
        first = await acp_server._admit_run(
            request,
            user=user,
            workspace_id="workspace_demo",
            execution_store=store,
            external_run_id="external_run_demo",
            session_id="session_demo",
            query="synthetic question",
            context_values={},
            lang=None,
        )
        interaction = {
            "id": "interaction_demo",
            "type": "single_choice",
            "prompt": "Choose a synthetic option",
            "options": [{"id": "option_a", "value": "value_a"}],
        }
        store.await_turn(
            "session_demo",
            first.turn_id,
            final_output={"type": "user_interaction", "interaction": interaction},
        )
        second = await acp_server._admit_run(
            request,
            user=user,
            workspace_id="workspace_demo",
            execution_store=store,
            external_run_id="external_run_demo",
            session_id="session_demo",
            query="Choose option A",
            context_values={},
            lang=None,
            interaction_response={
                "interaction_id": "interaction_demo",
                "type": "single_choice",
                "option_id": "option_a",
                "value": "value_a",
            },
            parent_run_id=first.run_id,
        )
        return store, first, second, manager

    store, first, second, manager = asyncio.run(scenario())

    assert first.run_id.startswith("agentic-")
    assert second.run_id.startswith("agentic-")
    assert second.run_id != first.run_id
    assert manager.contexts[0].run_id == first.run_id
    assert manager.contexts[1].run_id == second.run_id
    _session_id, latest = store.find_turn_by_protocol_id(
        "acp", "external_run_demo"
    )
    assert latest["run_id"] == second.run_id
    history = store.get_session_history("session_demo")
    assert history["turns"][0]["status"] == "awaiting_input"
    assert history["turns"][0]["run_id"] == first.run_id


def test_acp_response_exposes_external_id_and_canonical_metadata():
    context = RuntimeInvocationContext(
        run_id="agentic-canonical-demo",
        session_id="session_demo",
        turn_id=1,
        workspace_id="workspace_demo",
        username="operator_a",
        query="synthetic question",
    )
    snapshot = RunSnapshot(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=1,
        status=CanonicalRunStatus.RUNNING,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
        last_seq=1,
        scope=acp_server._scope(AuthUser("operator_a", "user"), "workspace_demo"),
        context=context,
    )

    response = acp_server._response_from_snapshot(
        snapshot,
        external_run_id="external_run_demo",
    )

    assert response.run_id == "external_run_demo"
    assert response.status is RunStatus.RUNNING
    assert response.metadata["canonical_run_id"] == "agentic-canonical-demo"


def test_ascii_non_english_language_is_not_forced_to_english():
    assert detect_language("Hola como estas, necesito revisar el historial") == "es"


def test_short_ascii_english_is_not_misdetected_as_other_language():
    for text in ("hi", "ok", "hello", "test", "yes", "no"):
        assert detect_language(text) == "en"


def test_language_detection_uses_configured_default_for_empty_text():
    assert detect_language("", default_lang="en") == "en"


def test_language_llm_detection_uses_task_llm_router(monkeypatch):
    calls = []

    class _Router:
        def call(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="es")

    monkeypatch.setattr(lang_module, "get_llm_router", lambda: _Router(), raising=False)

    assert _detect_language_via_llm("texto mezclado con technical terms") == "es"
    assert calls[0]["purpose"] == LLMPurpose.TASK
    assert calls[0]["stream"] is False
    assert calls[0]["enable_reasoning"] is False
    assert calls[0]["request_label"] == "language_detect"


def test_language_translation_uses_task_llm_router(monkeypatch):
    calls = []

    class _Router:
        def call(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content="translated")

    monkeypatch.setattr(lang_module, "get_llm_router", lambda: _Router(), raising=False)

    assert translate_text("需要翻译", "en", source_lang="zh") == "translated"
    assert calls[0]["purpose"] == LLMPurpose.TASK
    assert calls[0]["stream"] is False
    assert calls[0]["enable_reasoning"] is False
    assert calls[0]["request_label"] == "language_translate"
