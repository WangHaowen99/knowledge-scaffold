"""MCP chat tests over the canonical asynchronous run manager bridge."""

from __future__ import annotations

import asyncio
import queue
from pathlib import Path

from agent.runtime.contracts import (
    PublicEvent,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeResult,
)
from agent.runtime.event_bus import BlockingSubscription
from api.protocols.mcp.chat_adapter import execute_chat_tool
from api.protocols.mcp.workflow_bridge import execute_mcp_chat_workflow
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import MCPRequestScope


def _config(path: Path) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="http://backend.example.net",
        token_secret="unit-test-only-signing-key-not-valid",
        token_algorithm="HS256",
        token_issuer="test-issuer",
        token_audience="test-audience",
        token_ttl_seconds=3600,
        token_max_ttl_seconds=86400,
        http_timeout_seconds=30,
        transport="both",
        token_store_path=path,
    )


class _PermitAllLimiter:
    @staticmethod
    def try_acquire():
        return True


class _FakeOrchestrator:
    def __init__(self):
        self.run_rate_limiter = _PermitAllLimiter()


class _FakeAsyncManager:
    def __init__(
        self,
        *,
        status=RunStatus.COMPLETED,
        content="synthetic answer",
        structured_content=None,
        error=None,
        pending_interaction=None,
        trace_events=False,
    ):
        self.status = status
        self.content = content
        self.structured_content = structured_content
        self.error = error
        self.pending_interaction = pending_interaction
        self.trace_events = trace_events
        self.context = None
        self.scope = None
        self.cancelled = []

    async def create_run(self, context, scope):
        self.context = context
        self.scope = scope
        return self._snapshot(RunStatus.QUEUED, last_seq=0), True

    async def subscribe(self, run_id, scope, after_seq=0):
        del run_id, scope
        events = [event for event in self._events() if event.seq > after_seq]
        return BlockingSubscription(
            run_id=self.context.run_id,
            subscriber_id="subscriber_demo",
            queue=queue.Queue(),
            replay=events,
            closed=True,
        )

    async def unsubscribe(self, subscription):
        del subscription

    async def get_run(self, run_id, scope):
        if self.context is None or run_id != self.context.run_id:
            return None
        if not scope.owns(self._snapshot(self.status, last_seq=len(self._events()))):
            return None
        return self._snapshot(self.status, last_seq=len(self._events()))

    async def cancel_run(self, run_id, scope, reason):
        del scope
        self.cancelled.append((run_id, reason))
        self.status = RunStatus.CANCELLED
        return self._snapshot(RunStatus.CANCELLED, last_seq=len(self._events()))

    def _snapshot(self, status, *, last_seq):
        result = None
        if status == RunStatus.COMPLETED:
            result = RuntimeResult(
                status="completed",
                content=self.content,
                structured_content=self.structured_content,
            )
        elif status == RunStatus.AWAITING_INPUT:
            result = RuntimeResult(status="awaiting_input")
        elif status == RunStatus.FAILED:
            result = RuntimeResult(status="failed", error=self.error)
        elif status == RunStatus.CANCELLED:
            result = RuntimeResult(status="cancelled")
        return RunSnapshot(
            run_id=self.context.run_id,
            session_id=self.context.session_id,
            turn_id=1,
            status=status,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:01Z",
            last_seq=last_seq,
            scope=self.scope,
            context=self.context,
            result=result,
            error=self.error if status == RunStatus.FAILED else None,
            pending_interaction=(
                self.pending_interaction
                if status == RunStatus.AWAITING_INPUT
                else None
            ),
        )

    def _events(self):
        payloads = []
        if self.trace_events:
            payloads.extend(
                [
                    (
                        "tool.started",
                        "running",
                        {
                            "iteration": 1,
                            "tool_call_id": "tool_demo",
                            "name": "business_data_search",
                        },
                    ),
                    (
                        "tool.finished",
                        "running",
                        {
                            "iteration": 1,
                            "tool_call_id": "tool_demo",
                            "name": "business_data_search",
                            "status": "completed",
                            "elapsed_ms": 20,
                            "database_provider": "graph_provider",
                            "record_count": 1,
                        },
                    ),
                ]
            )
        terminal_payload = {"status": self.status.value}
        if self.content is not None:
            terminal_payload["content"] = self.content
        if self.structured_content is not None:
            terminal_payload["structured_content"] = self.structured_content
        if self.error is not None:
            terminal_payload["error"] = self.error.to_dict()
        payloads.append(("turn.finished", self.status.value, terminal_payload))
        return [
            PublicEvent(
                event_id="event_{}".format(index),
                run_id=self.context.run_id,
                session_id=self.context.session_id,
                turn_id=1,
                seq=index,
                event_type=event_type,
                status=status,
                source={"node": "synthetic", "component": "test"},
                payload=payload,
                created_at="2026-01-01T00:00:00Z",
            )
            for index, (event_type, status, payload) in enumerate(payloads, start=1)
        ]


def _call(coroutine):
    return asyncio.run(coroutine)


def _install_manager(monkeypatch, manager):
    import agent.runtime.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "async_manager_matches", lambda _value: True)
    monkeypatch.setattr(bootstrap, "get_async_run_manager", lambda: manager)
    monkeypatch.setattr(bootstrap, "call_async_run", _call)


def _scope():
    return MCPRequestScope(
        "operator_a",
        "workspace_demo",
        ["mcp:chat"],
        token="unit-test-token-placeholder",
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
    )


def _execute(monkeypatch, manager, *, payload=None, trace_config=None):
    _install_manager(monkeypatch, manager)
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    return execute_mcp_chat_workflow(
        orchestrator=_FakeOrchestrator(),
        scope=_scope(),
        payload=payload
        or {"request": "synthetic question", "session_id": "session_demo"},
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
        trace_config=trace_config,
    )


def test_chat_adapter_forwards_original_token_to_backend_bridge(monkeypatch, tmp_path):
    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"status": "success"}}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response()

    monkeypatch.setattr("api.protocols.mcp.chat_adapter.requests.post", fake_post)
    scope = _scope()

    result = execute_chat_tool(
        {"query": "synthetic question", "session_id": "session_demo"},
        scope=scope,
        config=_config(tmp_path / "tokens.json"),
    )

    assert result["ok"] is True
    assert captured["url"] == "http://backend.example.net/agentic/mcp/internal/chat"
    assert captured["headers"]["Authorization"] == "Bearer {}".format(scope.token)
    assert captured["headers"]["X-Workspace-Id"] == "workspace_demo"


def test_workflow_bridge_requires_manager_bound_to_current_orchestrator(monkeypatch):
    import agent.runtime.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "async_manager_matches", lambda _value: False)
    body, status_code = execute_mcp_chat_workflow(
        orchestrator=_FakeOrchestrator(),
        scope=_scope(),
        payload={"request": "synthetic question"},
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 503
    assert body["error"]["error_code"] == "MCP_CHAT_RUNTIME_UNAVAILABLE"


def test_workflow_bridge_uses_runtime_context_and_preserves_unary_shape(monkeypatch):
    manager = _FakeAsyncManager()
    body, status_code = _execute(monkeypatch, manager)

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert body["result"]["status"] == "success"
    assert body["result"]["resumable"] is False
    assert manager.context.supports_user_interaction is False
    assert manager.context.traceparent == _scope().traceparent
    assert manager.context.workspace_id == "workspace_demo"


def test_workflow_bridge_maps_admission_timeout(monkeypatch):
    error = RuntimeErrorInfo(
        error_code="RUN_ADMISSION_TIMEOUT",
        message="synthetic admission timeout",
        retriable=True,
    )
    body, status_code = _execute(
        monkeypatch,
        _FakeAsyncManager(status=RunStatus.FAILED, content=None, error=error),
    )

    assert status_code == 504
    assert body["error"]["error_code"] == "MCP_CHAT_ADMISSION_TIMEOUT"


def test_workflow_bridge_trace_is_explicit_and_uses_public_events(monkeypatch, tmp_path):
    manager = _FakeAsyncManager(trace_events=True)
    config = _config(tmp_path / "tokens.json")
    body, status_code = _execute(
        monkeypatch,
        manager,
        payload={
            "request": "synthetic question",
            "session_id": "session_demo",
            "include_trace": True,
        },
        trace_config=config,
    )

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert body["trace"]["schema_version"] == "5.0"
    assert body["trace"]["totals"]["tool_calls"] == 1
    tool = body["trace"]["iterations"][0]["tools"]["calls"][0]
    assert tool["name"] == "business_data_search"
    assert tool["database_provider"] == "graph_provider"
    assert "reasoning_summaries" not in body["trace"]["iterations"][0]


def test_workflow_bridge_omits_trace_by_default(monkeypatch, tmp_path):
    body, status_code = _execute(
        monkeypatch,
        _FakeAsyncManager(trace_events=True),
        trace_config=_config(tmp_path / "tokens.json"),
    )

    assert status_code == 200
    assert "trace" not in body


def test_workflow_bridge_coerces_structured_success_to_text(monkeypatch):
    body, status_code = _execute(
        monkeypatch,
        _FakeAsyncManager(
            content=None,
            structured_content={"type": "report", "text": "structured answer"},
        ),
    )

    assert status_code == 200
    assert body["result"]["content"] == "structured answer"


def test_workflow_bridge_preserves_awaiting_projection_defensively(monkeypatch):
    pending = {
        "id": "interaction_demo",
        "type": "input",
        "prompt": "Synthetic prompt",
        "options": [],
    }
    body, status_code = _execute(
        monkeypatch,
        _FakeAsyncManager(
            status=RunStatus.AWAITING_INPUT,
            content=None,
            pending_interaction=pending,
        ),
    )

    assert status_code == 200
    assert body["error"]["error_code"] == "MCP_CHAT_AWAITING_INPUT"
    assert body["result"]["pending_user_interaction"] == pending
