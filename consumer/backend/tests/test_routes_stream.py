import os
import json

from flask import Flask
import pytest

from config.workspace_llm_profiles import update_workspace_llm_profiles
from api.routes.workflows import _encode_agent_sse, workflows_bp
from api.auth.auth import AuthManager
from persistence.run_queue import RunAdmissionQueue, RunSlotManager
from persistence.run_store import RunStatus, RunStore
from persistence.run_queue import TokenBucketRateLimiter
from persistence.run_store import Run
from system_config_migration_helpers import apply_test_system_config_migrations
from workspace.workspaces import create_workspace


def _raise_when_iterated(error: BaseException):
    raise error


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


def _build_test_client(tmp_path):
    os.environ["PROJECT_ROOT"] = str(tmp_path / ".onto-runtime")
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(workflows_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def _write_system_profiles(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / ".onto-runtime" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
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
        runtime_root=tmp_path / ".onto-runtime",
    )


def test_run_stream_requires_session_id(tmp_path):
    client, headers = _build_test_client(tmp_path)

    response = client.post(
        "/agentic/runs/stream",
        json={"request": "hello"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "session_id is required"}


@pytest.mark.parametrize("endpoint", ["/agentic/runs/stream", "/agentic/runs/sync"])
def test_run_endpoints_reject_unsafe_session_id_before_starting(tmp_path, endpoint):
    client, headers = _build_test_client(tmp_path)

    response = client.post(
        endpoint,
        json={"request": "hello", "session_id": "../escape"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "session_id" in response.get_json()["error"]
    assert not (tmp_path / "escape").exists()


def test_model_profiles_returns_global_and_workspace_private_profiles(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    _write_system_profiles(tmp_path, monkeypatch)
    update_workspace_llm_profiles(
        "demo",
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

    response = client.get(
        "/agentic/model-profiles",
        headers={**headers, "X-Workspace-Id": "demo"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert [profile["id"] for profile in payload["profiles"]] == ["global:fast", "workspace:private"]
    assert payload["default_profile_id"] == "global:fast"
    assert payload["profiles"][1]["scope"] == "workspace"


def test_run_stream_forwards_requested_run_id(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    captured = {}

    class _FakeOrchestrator:
        run_store = RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            captured.update(kwargs)
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "client-run-123:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sess-1",
                    "run_id": "client-run-123",
                    "turn_id": 1,
                    "type": "turn.started",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"user_preview": "hello"},
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/stream",
        json={
            "request": "hello",
            "session_id": "sess-1",
            "run_id": "client-run-123",
        },
        headers=headers,
    )

    assert response.status_code == 200
    response_text = response.get_data(as_text=True)
    assert captured["requested_run_id"] == "client-run-123"
    assert "id: 1\nevent: agent.v2" in response_text


def test_agent_sse_replaces_non_finite_numbers_with_json_null():
    encoded = _encode_agent_sse({
        "seq": 7,
        "payload": {
            "response_time": float("nan"),
            "nested": [float("inf"), float("-inf")],
        },
    })
    data_line = next(line for line in encoded.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))

    assert payload["payload"] == {
        "response_time": None,
        "nested": [None, None],
    }
    assert "NaN" not in encoded
    assert "Infinity" not in encoded


def test_queued_run_returns_json_until_slot_is_admitted(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        def __init__(self):
            self.run_store = RunStore()
            self.slot_manager = RunSlotManager()
            self.run_admission_queue = RunAdmissionQueue(self.slot_manager)
            for _index in range(100):
                assert self.slot_manager.acquire("admin", 100, timeout=0) is True

        def run(self, **kwargs):
            del kwargs
            yield _raise_when_iterated(
                AssertionError("queued run must not start before admission")
            )

        def get_run_status(self, run_id):
            run = self.run_store.get_run(run_id)
            return {"run_id": run_id, "status": run.status.value} if run else None

    orchestrator = _FakeOrchestrator()
    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: orchestrator)

    response = client.post(
        "/agentic/runs/stream",
        json={
            "request": "hello",
            "session_id": "queued-session",
            "run_id": "queued-run-123",
        },
        headers=headers,
    )

    assert response.status_code == 202
    assert response.content_type.startswith("application/json")
    payload = response.get_json()
    assert payload["status"] == "queued"
    assert payload["queue"]["position"] == 1
    assert payload["links"]["events"] == "/agentic/runs/queued-run-123/events"

    status_response = client.get("/agentic/runs/queued-run-123/status", headers=headers)
    assert status_response.status_code == 200
    assert status_response.get_json()["queue"]["position"] == 1

    events_response = client.get("/agentic/runs/queued-run-123/events", headers=headers)
    assert events_response.status_code == 409
    assert events_response.content_type.startswith("application/json")


def test_token_bucket_allows_burst_then_refills_by_rate():
    now = [0.0]
    limiter = TokenBucketRateLimiter(
        rate_per_second=50,
        burst=50,
        clock=lambda: now[0],
    )

    assert [limiter.try_acquire() for _ in range(50)] == [True] * 50
    assert limiter.try_acquire() is False

    now[0] = 0.01
    assert limiter.try_acquire() is False

    now[0] = 0.02
    assert limiter.try_acquire() is True


def test_run_sync_returns_429_when_global_tps_is_exhausted(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _RejectingLimiter:
        def try_acquire(self):
            return False

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()
        run_rate_limiter = _RejectingLimiter()

        def run(self, **kwargs):
            raise AssertionError("run should not start when global TPS is exhausted")

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.get_json() == {
        "error": "请求过于频繁，请稍后重试",
        "limit": 50,
    }


def test_run_sync_returns_final_answer(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    captured = {}

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            captured.update(kwargs)
            assert kwargs["user_request"] == "hello"
            assert kwargs["session_id"] == "sync-session"
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-sync-1:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sync-session",
                    "run_id": "run-sync-1",
                    "turn_id": 1,
                    "type": "turn.started",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"user_preview": "hello"},
                },
            }
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-sync-1:2",
                    "seq": 2,
                    "ts": 1700000000001,
                    "session_id": "sync-session",
                    "run_id": "run-sync-1",
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"status": "success", "final_output": "done"},
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={
            "request": "hello",
            "session_id": "sync-session",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "success",
        "final_answer": "done",
        "session_id": "sync-session",
        "run_id": "run-sync-1",
    }
    assert captured["user_context"]["supports_user_interaction"] is False


def test_run_sync_coerces_structured_final_output_to_text(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            assert kwargs["user_context"]["supports_user_interaction"] is False
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-sync-2:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sync-session-structured",
                    "run_id": "run-sync-2",
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {
                        "status": "awaiting_input",
                        "final_output": {
                            "type": "user_interaction",
                            "text": "请基于业务常识给出建议。",
                            "options": [{"id": "a", "label": "继续", "value": "continue"}],
                        },
                    },
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session-structured"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "awaiting_input",
        "final_answer": "请基于业务常识给出建议。",
        "session_id": "sync-session-structured",
        "run_id": "run-sync-2",
    }


def test_run_sync_preserves_terminal_error_details(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            yield {
                "event": "agent.v2",
                "data": {
                    "run_id": "run-sync-error",
                    "type": "turn.finished",
                    "payload": {
                        "status": "error",
                        "error": "模型额度已耗尽",
                    },
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-error"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "error",
        "error": "模型额度已耗尽",
        "final_answer": "",
        "session_id": "sync-error",
        "run_id": "run-sync-error",
    }


def test_run_sync_returns_pending_interaction_without_object_string(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    interaction = {
        "id": "interaction-1",
        "type": "single_choice",
        "prompt": "请选择处理模式",
        "options": [{"id": "a", "label": "模式 A"}],
    }

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            yield {
                "event": "agent.v2",
                "data": {
                    "run_id": "run-awaiting-1",
                    "type": "turn.finished",
                    "payload": {
                        "status": "awaiting_input",
                        "final_output": interaction,
                    },
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "process records", "session_id": "sync-awaiting"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "awaiting_input",
        "final_answer": "",
        "pending_user_interaction": interaction,
        "session_id": "sync-awaiting",
        "run_id": "run-awaiting-1",
    }


def test_run_sync_can_return_tool_trace_in_the_same_authenticated_response(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            for seq, event_type, payload in (
                (1, "turn.started", {"user_preview": "hello"}),
                (2, "tool.started", {"tool_name": "business_data_search"}),
                (3, "tool.started", {"tool_name": "execute_script"}),
                (4, "turn.finished", {"status": "success", "final_output": "done"}),
            ):
                yield {
                    "event": "agent.v2",
                    "data": {
                        "protocol": "agent-stream.v2",
                        "event_id": "run-sync-trace:{}".format(seq),
                        "seq": seq,
                        "ts": 1700000000000 + seq,
                        "session_id": "sync-session-trace",
                        "run_id": "run-sync-trace",
                        "turn_id": 1,
                        "type": event_type,
                        "source": {"node": "test", "component": "unit"},
                        "payload": payload,
                    },
                }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session-trace", "include_trace": True},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["execution_trace"] == {
        "tool_calls": ["business_data_search", "execute_script"],
    }


def test_run_sync_trace_includes_bounded_tool_failures(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            for seq, event_type, payload in (
                (1, "tool.started", {"tool_name": "execute_script"}),
                (2, "tool.finished", {
                    "tool_name": "execute_script",
                    "status": "error",
                    "error": "TypeError: query() got an unexpected keyword argument 'filters'",
                    "output_preview": "sensitive output must not be copied into evaluation trace",
                }),
                (3, "turn.finished", {"status": "success", "final_output": "blocked"}),
            ):
                yield {
                    "event": "agent.v2",
                    "data": {
                        "protocol": "agent-stream.v2",
                        "event_id": f"run-sync-tool-failure:{seq}",
                        "seq": seq,
                        "ts": 1700000000000 + seq,
                        "session_id": "sync-session-tool-failure",
                        "run_id": "run-sync-tool-failure",
                        "turn_id": 1,
                        "type": event_type,
                        "source": {"node": "test", "component": "unit"},
                        "payload": payload,
                    },
                }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session-tool-failure", "include_trace": True},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["execution_trace"] == {
        "tool_calls": ["execute_script"],
        "tool_failures": [{
            "tool_name": "execute_script",
            "error": "TypeError: query() got an unexpected keyword argument 'filters'",
        }],
    }


def test_run_sync_requires_request(tmp_path):
    client, headers = _build_test_client(tmp_path)

    response = client.post(
        "/agentic/runs/sync",
        json={"session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Request is required"}


def test_run_sync_queues_when_concurrent_limit_exceeded(monkeypatch, tmp_path):
    """With queueing, the request passes through (queue gate) instead of 429."""
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return [
                    Run(
                        run_id="run-busy-0",
                        user_request="busy",
                        status=RunStatus.RUNNING,
                        metadata={"user_id": "admin"},
                    ),
                    Run(
                        run_id="run-busy-1",
                        user_request="busy",
                        status=RunStatus.RUNNING,
                        metadata={"user_id": "admin"},
                    ),
                ]

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            assert kwargs["user_request"] == "hello"
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-queued-1:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sync-session-queued",
                    "run_id": "run-queued-1",
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"status": "success", "final_output": "done"},
                },
            }

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session-queued"},
        headers=headers,
    )

    # Request passes through the queue gate (fresh semaphore)
    assert response.status_code == 200


def test_run_sync_returns_500_when_orchestrator_raises(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            yield _raise_when_iterated(RuntimeError("boom"))

    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 500
    assert response.get_json() == {
        "status": "error",
        "error": "boom",
        "final_answer": "",
        "session_id": "sync-session",
        "run_id": None,
    }
