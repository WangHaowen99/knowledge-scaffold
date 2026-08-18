"""HTTP compatibility tests over the canonical asynchronous run contract."""

import asyncio
import json
import os
import queue

from flask import Flask
import pytest

from agent.runtime.contracts import (
    PublicEvent,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeResult,
)
from agent.runtime.event_bus import BlockingSubscription
from agent.runtime.store import utc_now_iso
from api.auth.auth import AuthManager
from api.routes import workflows
from api.routes.workflows import _encode_agent_sse, workflows_bp
from config.workspace_llm_profiles import update_workspace_llm_profiles
from persistence.run_queue import TokenBucketRateLimiter
from system_config_migration_helpers import apply_test_system_config_migrations
from workspace.workspaces import create_workspace


@pytest.fixture(autouse=True)
def _restore_project_root_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "unit-model")
    previous = os.environ.get("PROJECT_ROOT")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PROJECT_ROOT", None)
        else:
            os.environ["PROJECT_ROOT"] = previous


class _PermitAllLimiter:
    @staticmethod
    def try_acquire():
        return True


class _RouteOrchestrator:
    run_rate_limiter = _PermitAllLimiter()


class _SyntheticManager:
    def __init__(
        self,
        *,
        status=RunStatus.COMPLETED,
        result=None,
        error=None,
        pending_interaction=None,
        event_payloads=None,
        create_error=None,
    ):
        self.status = status
        self.result = result or RuntimeResult(
            status="completed",
            content="synthetic answer",
        )
        self.error = error
        self.pending_interaction = pending_interaction
        self.event_payloads = list(event_payloads or [])
        self.create_error = create_error
        self.context = None
        self.scope = None
        self.snapshot = None
        self.unsubscribed = []

    async def create_run(self, context, scope, *, idempotency_key=None):
        del idempotency_key
        if self.create_error is not None:
            raise self.create_error
        self.context = context
        self.scope = scope
        now = utc_now_iso()
        self.snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=self.status,
            created_at=now,
            updated_at=now,
            last_seq=len(self._events(context)),
            scope=scope,
            context=context,
            result=self.result,
            error=self.error,
            pending_interaction=self.pending_interaction,
        )
        queued = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at=now,
            updated_at=now,
            last_seq=0,
            scope=scope,
            context=context,
        )
        return queued, True

    async def get_run(self, run_id, scope):
        del scope
        if self.snapshot is None or self.snapshot.run_id != run_id:
            return None
        return self.snapshot

    async def subscribe(self, run_id, scope, *, after_seq=0):
        del scope
        events = [
            event
            for event in self._events(self.context)
            if event.run_id == run_id and event.seq > after_seq
        ]
        return BlockingSubscription(
            run_id=run_id,
            subscriber_id="synthetic-subscriber",
            queue=queue.Queue(),
            replay=events,
            closed=True,
        )

    async def unsubscribe(self, subscription):
        self.unsubscribed.append(subscription.subscriber_id)

    async def cancel_run(self, run_id, scope, reason="user_requested"):
        del scope
        if self.snapshot is None or self.snapshot.run_id != run_id:
            return None
        self.snapshot = RunSnapshot(
            **{
                **self.snapshot.__dict__,
                "status": RunStatus.CANCELLED,
                "result": RuntimeResult(status="cancelled"),
                "error": None,
                "cancel_reason": reason,
            }
        )
        return self.snapshot

    def _events(self, context):
        if context is None:
            return []
        events = []
        for index, value in enumerate(self.event_payloads, start=1):
            event_type, status, payload = value
            events.append(PublicEvent(
                schema_version="1.0",
                event_id="{}:{}".format(context.run_id, index),
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                seq=index,
                event_type=event_type,
                status=status,
                source={"node": "synthetic", "component": "test"},
                payload=payload,
                created_at=utc_now_iso(),
            ))
        if not events or events[-1].event_type != "turn.finished":
            terminal_payload = {"status": self.status.value}
            if self.result.content is not None:
                terminal_payload["content"] = self.result.content
            if self.result.structured_content is not None:
                terminal_payload["structured_content"] = self.result.structured_content
            if self.error is not None:
                terminal_payload["error"] = self.error.to_dict()
            events.append(PublicEvent(
                schema_version="1.0",
                event_id="{}:{}".format(context.run_id, len(events) + 1),
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                seq=len(events) + 1,
                event_type="turn.finished",
                status=self.status.value,
                source={"node": "synthetic", "component": "test"},
                payload=terminal_payload,
                created_at=utc_now_iso(),
            ))
        return events


def _build_test_client(tmp_path):
    os.environ["PROJECT_ROOT"] = str(tmp_path / ".synthetic-runtime")
    create_workspace("Synthetic Workspace", workspace_id="workspace_demo", set_as_default=True)
    app = Flask(__name__)
    app.config["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(workflows_bp)
    headers = {
        "Authorization": "Bearer {}".format(auth_manager.issue_token(user)),
        "X-Workspace-Id": "workspace_demo",
    }
    return app.test_client(), headers


def _install_manager(monkeypatch, manager, orchestrator=None):
    monkeypatch.setattr(workflows, "get_async_run_manager", lambda: manager)
    monkeypatch.setattr(workflows, "call_async_run", asyncio.run)
    monkeypatch.setattr(
        workflows,
        "_get_orchestrator",
        lambda: orchestrator or _RouteOrchestrator(),
    )


def _write_system_profiles(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / ".synthetic-runtime" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        json.dumps({
            "llm_profiles": {
                "default_profile_id": "fast",
                "profiles": [{
                    "id": "fast",
                    "name": "Fast Global",
                    "provider": "openai_compatible",
                    "base_url": "https://llm.example/v1",
                    "model": "fast-model",
                    "api_key": "your-api-key-here",
                    "enabled": True,
                }],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=tmp_path / ".synthetic-runtime",
    )


def test_run_stream_requires_session_id(tmp_path):
    client, headers = _build_test_client(tmp_path)
    response = client.post("/agentic/runs/stream", json={"request": "hello"}, headers=headers)
    assert response.status_code == 400
    assert response.get_json() == {"error": "session_id is required"}


@pytest.mark.parametrize("endpoint", ["/agentic/runs/stream", "/agentic/runs/sync"])
def test_run_endpoints_reject_unsafe_session_id(tmp_path, endpoint):
    client, headers = _build_test_client(tmp_path)
    response = client.post(
        endpoint,
        json={"request": "hello", "session_id": "../escape"},
        headers=headers,
    )
    assert response.status_code == 400
    assert "session_id" in response.get_json()["error"]


def test_model_profiles_returns_global_and_workspace_profiles(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    _write_system_profiles(tmp_path, monkeypatch)
    update_workspace_llm_profiles("workspace_demo", {"profiles": [{
        "id": "private",
        "name": "Private Model",
        "provider": "openai_compatible",
        "base_url": "https://model.example/v1",
        "model": "private-model",
        "api_key": "your-api-key-here",
        "enabled": True,
    }]})
    response = client.get("/agentic/model-profiles", headers=headers)
    assert response.status_code == 200
    payload = response.get_json()
    assert [profile["id"] for profile in payload["profiles"]] == [
        "global:fast",
        "workspace:private",
    ]


def test_compatibility_stream_uses_requested_run_id(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    manager = _SyntheticManager(event_payloads=[
        ("turn.started", "running", {"user_preview": "hello"}),
    ])
    _install_manager(monkeypatch, manager)
    response = client.post(
        "/agentic/runs/stream",
        json={"request": "hello", "session_id": "session_demo", "run_id": "run_demo"},
        headers=headers,
    )
    assert response.status_code == 200
    assert manager.context.run_id == "run_demo"
    assert "event: agent.v2" in response.get_data(as_text=True)


def test_agent_sse_replaces_non_finite_numbers_with_json_null():
    encoded = _encode_agent_sse({
        "seq": 7,
        "payload": {"response_time": float("nan"), "nested": [float("inf")]},
    })
    data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["payload"] == {"response_time": None, "nested": [None]}


def test_token_bucket_allows_burst_then_refills_by_rate():
    now = [0.0]
    limiter = TokenBucketRateLimiter(rate_per_second=50, burst=50, clock=lambda: now[0])
    assert [limiter.try_acquire() for _ in range(50)] == [True] * 50
    assert limiter.try_acquire() is False
    now[0] = 0.02
    assert limiter.try_acquire() is True


def test_sync_route_returns_final_answer(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    manager = _SyntheticManager()
    _install_manager(monkeypatch, manager)
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["final_answer"] == "synthetic answer"
    assert manager.context.supports_user_interaction is False


def test_sync_route_coerces_structured_output(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    manager = _SyntheticManager(result=RuntimeResult(
        status="completed",
        structured_content={"type": "report", "text": "structured answer"},
    ))
    _install_manager(monkeypatch, manager)
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo"},
        headers=headers,
    )
    assert response.get_json()["final_answer"] == "structured answer"


def test_sync_route_preserves_public_error(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    error = RuntimeErrorInfo(
        error_code="MODEL_PROVIDER_ERROR",
        message="Synthetic provider failure",
        source="model",
    )
    manager = _SyntheticManager(
        status=RunStatus.FAILED,
        result=RuntimeResult(status="failed", error=error),
        error=error,
    )
    _install_manager(monkeypatch, manager)
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "error"
    assert response.get_json()["error"] == "Synthetic provider failure"


def test_sync_route_collects_public_tool_trace(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    manager = _SyntheticManager(event_payloads=[
        ("tool.started", "running", {"name": "synthetic_tool"}),
        ("tool.finished", "failed", {
            "name": "synthetic_tool",
            "status": "failed",
            "error": {"message": "Synthetic tool failure"},
        }),
    ])
    _install_manager(monkeypatch, manager)
    response = client.post(
        "/agentic/runs/sync",
        json={
            "request": "hello",
            "session_id": "session_demo",
            "include_trace": True,
        },
        headers=headers,
    )
    trace = response.get_json()["execution_trace"]
    assert trace["tool_calls"] == ["synthetic_tool"]
    assert trace["tool_failures"] == [{
        "tool_name": "synthetic_tool",
        "error": "Synthetic tool failure",
    }]


def test_sync_route_returns_429_before_creating_run(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _RejectingLimiter:
        @staticmethod
        def try_acquire():
            return False

    manager = _SyntheticManager()
    _install_manager(
        monkeypatch,
        manager,
        orchestrator=type("Orchestrator", (), {"run_rate_limiter": _RejectingLimiter()})(),
    )
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo"},
        headers=headers,
    )
    assert response.status_code == 429
    assert manager.context is None


def test_sync_route_rejects_invalid_arguments(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    _install_manager(monkeypatch, _SyntheticManager())
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo", "arguments": []},
        headers=headers,
    )
    assert response.status_code == 400


def test_sync_route_returns_generic_500_when_manager_fails(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    _install_manager(
        monkeypatch,
        _SyntheticManager(create_error=RuntimeError("synthetic internal detail")),
    )
    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "session_demo"},
        headers=headers,
    )
    assert response.status_code == 500
    assert response.get_json()["error"] == "运行失败"
