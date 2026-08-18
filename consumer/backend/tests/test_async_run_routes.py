import asyncio
import threading
import time

from flask import Flask

from agent.runtime.contracts import RuntimeResult
from agent.runtime.events import RuntimeEvent
from agent.runtime.manager import AsyncRunManager
from agent.runtime.service import AsyncExecutionService
from api.auth.auth import AuthManager
from api.routes.workflows import workflows_bp
from workspace.workspaces import create_workspace


class _PermitAllLimiter:
    @staticmethod
    def try_acquire():
        return True


class _SyntheticOrchestrator:
    run_rate_limiter = _PermitAllLimiter()


class _SyntheticRuntime:
    async def astream(self, context):
        yield RuntimeEvent(
            event_type="turn.started",
            status="running",
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            source={"node": "synthetic_runtime", "component": "test"},
            payload={"user_preview": "synthetic question"},
        )
        await asyncio.sleep(0)
        yield RuntimeEvent(
            event_type="turn.finished",
            status="completed",
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            source={"node": "synthetic_runtime", "component": "test"},
            payload={"status": "completed", "content": "synthetic answer"},
        )

    async def arun(self, context, sink):
        async for event in self.astream(context):
            await sink.emit(event)
        return RuntimeResult(status="completed", content="synthetic answer")

    async def cancel(self, run_id, reason):
        del run_id, reason
        return True

    async def release(self, jiuwen_session_id):
        del jiuwen_session_id


class _AsyncThreadLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.started.wait(timeout=2.0)

    def _run(self):
        asyncio.set_event_loop(self.loop)

        def keep_cross_thread_wakeup_bounded():
            self.loop.call_later(0.01, keep_cross_thread_wakeup_bounded)

        self.loop.call_soon(keep_cross_thread_wakeup_bounded)
        self.loop.call_soon(self.started.set)
        self.loop.run_forever()

    def call(self, coroutine, timeout=5.0):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)
        self.loop.close()


class _RuntimeHarness:
    def __init__(self):
        self.loop = _AsyncThreadLoop()
        self.manager = AsyncRunManager(
            AsyncExecutionService(_SyntheticRuntime())
        )

    def close(self):
        self.loop.call(self.manager.shutdown())
        self.loop.close()


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".synthetic-runtime"))
    create_workspace("Synthetic Workspace", workspace_id="workspace_demo", set_as_default=True)
    app = Flask(__name__)
    app.config["TESTING"] = True
    bootstrap_password = "TestBootstrap#2026"
    auth_manager = AuthManager(
        str(tmp_path / "users.json"),
        "synthetic-test-secret",
        bootstrap_admin_password=bootstrap_password,
    )
    user = auth_manager.authenticate("admin", bootstrap_password)
    assert user is not None
    user = auth_manager.change_password(
        "admin",
        bootstrap_password,
        "SyntheticStrong123!",
    )
    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(workflows_bp)
    orchestrator = _SyntheticOrchestrator()
    harness = _RuntimeHarness()
    monkeypatch.setattr("api.routes.workflows._get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(
        "api.routes.workflows.get_async_run_manager",
        lambda: harness.manager,
    )
    monkeypatch.setattr(
        "api.routes.workflows.call_async_run",
        harness.loop.call,
    )
    headers = {
        "Authorization": "Bearer {}".format(auth_manager.issue_token(user)),
        "X-Workspace-Id": "workspace_demo",
    }
    return app.test_client(), headers, harness


def test_create_then_subscribe_to_async_run(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.routes.workflows.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {"id": "profile_demo"},
    )
    client, headers, harness = _client(tmp_path, monkeypatch)
    try:
        created = client.post(
            "/agentic/runs",
            json={
                "request": "synthetic question",
                "session_id": "session_demo",
                "agent_mode": "quick",
                "llm_request_mode": "stream",
                "include_trace": False,
            },
            headers=headers,
        )
        assert created.status_code == 202
        run_id = created.get_json()["run_id"]

        deadline = time.time() + 1.0
        while time.time() < deadline:
            status = client.get("/agentic/runs/{}/status".format(run_id), headers=headers)
            if status.get_json()["status"] == "completed":
                break
            time.sleep(0.01)
        assert status.get_json()["status"] == "completed"

        events = client.get("/agentic/runs/{}/events".format(run_id), headers=headers)
        assert events.status_code == 200
        body = events.get_data(as_text=True)
        assert "event: run.started" in body
        assert "event: turn.finished" in body
        assert "synthetic answer" in body
    finally:
        harness.close()


def test_async_run_rejects_non_object_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.routes.workflows.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {"id": "profile_demo"},
    )
    client, headers, harness = _client(tmp_path, monkeypatch)
    try:
        response = client.post(
            "/agentic/runs",
            json={"request": "synthetic question", "arguments": ["invalid"]},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.get_json()["error"]["error_code"] == "INVALID_ARGUMENT"
    finally:
        harness.close()
