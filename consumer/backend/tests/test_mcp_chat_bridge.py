"""MCP unary projection tests on the native ASGI event loop."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from agent.runtime.contracts import (
    PublicEvent,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeResult,
)
from api.protocols.mcp.chat_adapter import execute_chat_tool
from api.protocols.mcp.workflow_bridge import execute_mcp_chat_workflow_async
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import MCPRequestScope
from persistence.execution_store import ExecutionStore


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _Manager:
    def __init__(
        self,
        *,
        status=RunStatus.COMPLETED,
        content="synthetic answer",
        error=None,
        pending_interaction=None,
    ):
        self.final_status = status
        self.content = content
        self.error = error
        self.pending_interaction = pending_interaction
        self.snapshot = None
        self.context = None
        self.cancelled = []

    @asynccontextmanager
    async def session_guard(self, _scope, _session_id):
        yield

    async def assert_session_available(self, _scope, _session_id):
        return None

    async def create_run(self, context, scope, *, session_guarded=False):
        assert session_guarded is True
        self.context = context
        self.snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            last_seq=0,
            scope=scope,
            context=context,
        )
        return self.snapshot, True

    async def get_run(self, run_id, scope):
        if self.snapshot is None or run_id != self.snapshot.run_id:
            return None
        return self.snapshot if scope.owns(self.snapshot) else None

    async def cancel_run(self, run_id, _scope, reason):
        self.cancelled.append((run_id, reason))
        self.snapshot = replace(
            self.snapshot,
            status=RunStatus.CANCELLED,
            result=RuntimeResult(status="cancelled"),
        )
        return self.snapshot

    def finish(self, *, last_seq):
        if self.final_status is RunStatus.COMPLETED:
            result = RuntimeResult(status="completed", content=self.content)
        elif self.final_status is RunStatus.AWAITING_INPUT:
            result = RuntimeResult(status="awaiting_input")
        elif self.final_status is RunStatus.FAILED:
            result = RuntimeResult(status="failed", error=self.error)
        else:
            result = RuntimeResult(status="cancelled")
        self.snapshot = replace(
            self.snapshot,
            status=self.final_status,
            result=result,
            error=self.error if self.final_status is RunStatus.FAILED else None,
            pending_interaction=(
                self.pending_interaction
                if self.final_status is RunStatus.AWAITING_INPUT
                else None
            ),
            last_seq=last_seq,
            updated_at="2026-01-01T00:00:01Z",
        )


class _EventHub:
    def __init__(self, manager, *, trace=False, never_finish=False):
        self.manager = manager
        self.trace = trace
        self.never_finish = never_finish

    def stream(self, run_id, *, after_seq=0):
        async def generate():
            if self.never_finish:
                await asyncio.Event().wait()
                return
            events = []
            if self.trace:
                events.extend(
                    [
                        ("tool.started", "running", {"name": "synthetic_tool", "tool_call_id": "tool_demo"}),
                        ("tool.finished", "running", {"name": "synthetic_tool", "tool_call_id": "tool_demo", "status": "completed", "elapsed_ms": 5}),
                    ]
                )
            terminal_payload = {"status": self.manager.final_status.value}
            if self.manager.content:
                terminal_payload["content"] = self.manager.content
            if self.manager.error is not None:
                terminal_payload["error"] = self.manager.error.to_dict()
            events.append(("turn.finished", self.manager.final_status.value, terminal_payload))
            self.manager.finish(last_seq=len(events))
            for seq, (event_type, status, payload) in enumerate(events, start=1):
                if seq <= after_seq:
                    continue
                yield PublicEvent(
                    event_id="event_{}".format(seq),
                    run_id=run_id,
                    session_id=self.manager.context.session_id,
                    turn_id=self.manager.context.turn_id,
                    seq=seq,
                    event_type=event_type,
                    status=status,
                    source={"component": "synthetic"},
                    payload=payload,
                    created_at="2026-01-01T00:00:01Z",
                )

        return generate()


class _Runtime:
    def __init__(self, manager, *, trace=False, never_finish=False):
        self.manager = manager
        self.short_io = _InlinePool()
        self.event_hub = _EventHub(
            manager,
            trace=trace,
            never_finish=never_finish,
        )


def _scope(*, ontology_id=""):
    return MCPRequestScope(
        "operator_a",
        "workspace_demo",
        ["mcp:chat"],
        token="token-placeholder",
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
        ontology_id=ontology_id,
    )


def _config(path: Path) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="http://service.example.net",
        token_secret="test-placeholder-not-a-secret",
        token_algorithm="HS256",
        token_issuer="synthetic-issuer",
        token_audience="synthetic-audience",
        token_ttl_seconds=3600,
        token_max_ttl_seconds=86400,
        http_timeout_seconds=30,
        transport="both",
        token_store_path=path,
    )


def _execute(
    tmp_path,
    manager,
    *,
    payload=None,
    trace=False,
    never_finish=False,
    scope=None,
):
    import api.protocols.mcp.workflow_bridge as module

    original = module.resolve_effective_llm_profile_config
    module.resolve_effective_llm_profile_config = lambda *_args, **_kwargs: {}
    try:
        return asyncio.run(
            execute_mcp_chat_workflow_async(
                runtime=_Runtime(
                    manager,
                    trace=trace,
                    never_finish=never_finish,
                ),
                execution_store=ExecutionStore(str(tmp_path / "executions")),
                scope=scope or _scope(),
                payload=payload
                or {"request": "synthetic question", "session_id": "session_demo"},
                concurrency_limit=1,
                queue_wait_seconds=0.01,
                execution_timeout_seconds=0.01,
                trace_config=_config(tmp_path / "tokens.json"),
            )
        )
    finally:
        module.resolve_effective_llm_profile_config = original


def test_mcp_awaits_canonical_run_without_sync_bridge(tmp_path):
    manager = _Manager()
    body, status_code = _execute(tmp_path, manager)

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert body["result"]["resumable"] is False
    assert manager.context.run_id.startswith("agentic-")
    assert manager.context.supports_user_interaction is False
    assert manager.context.traceparent == _scope().traceparent


def test_mcp_forwards_explicit_deterministic_operation_and_arguments(tmp_path):
    manager = _Manager()

    body, status_code = _execute(
        tmp_path,
        manager,
        payload={
            "request": "synthetic deterministic request",
            "session_id": "session_explicit_operation",
            "requested_operation_key": "operation_demo",
            "arguments": {"code": "CODE-42"},
        },
    )

    assert status_code == 200
    assert body["ok"] is True
    assert manager.context.requested_operation_key == "operation_demo"
    assert manager.context.arguments == {"code": "CODE-42"}


def test_mcp_rejects_unexpected_awaiting_without_resuming_run(tmp_path):
    manager = _Manager(
        status=RunStatus.AWAITING_INPUT,
        content=None,
        pending_interaction={"id": "interaction_demo", "type": "input"},
    )
    body, status_code = _execute(tmp_path, manager)

    assert status_code == 409
    assert body["error"]["error_code"] == "INTERACTION_NOT_SUPPORTED"
    assert body["result"]["resumable"] is False


def test_mcp_trace_is_collected_from_public_events(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        payload={
            "request": "synthetic question",
            "session_id": "session_demo",
            "include_trace": True,
        },
        trace=True,
    )

    assert status_code == 200
    assert body["trace"]["totals"]["tool_calls"] == 1


def test_chat_adapter_injects_bound_ontology(monkeypatch, tmp_path):
    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"status": "success"}}

    monkeypatch.setattr(
        "api.protocols.mcp.chat_adapter.requests.post",
        lambda _url, **kwargs: captured.update(kwargs) or _Response(),
    )
    result = execute_chat_tool(
        {"query": "合成示例问题"},
        scope=_scope(ontology_id="knowledge_package_demo"),
        config=_config(tmp_path / "tokens.json"),
    )

    assert result["ok"] is True
    assert (
        captured["json"]["arguments"]["selected_ontology_id"]
        == "knowledge_package_demo"
    )


def test_chat_adapter_rejects_conflicting_bound_ontology(monkeypatch, tmp_path):
    called = False

    def fake_post(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("api.protocols.mcp.chat_adapter.requests.post", fake_post)
    result = execute_chat_tool(
        {
            "query": "合成示例问题",
            "arguments": {"selected_ontology_id": "knowledge_package_other"},
        },
        scope=_scope(ontology_id="knowledge_package_demo"),
        config=_config(tmp_path / "tokens.json"),
    )

    assert called is False
    assert result["error"]["error_code"] == "MCP_ONTOLOGY_SCOPE_MISMATCH"


def test_workflow_bridge_rebinds_and_revalidates_token_ontology(
    monkeypatch,
    tmp_path,
):
    captured = {}
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.validate_runtime_ontology_availability",
        lambda **kwargs: captured.update(kwargs) or "1.0.0",
    )
    manager = _Manager()

    body, status_code = _execute(
        tmp_path,
        manager,
        scope=_scope(ontology_id="knowledge_package_demo"),
        payload={"request": "合成示例问题", "session_id": "session_demo_bound"},
    )

    assert status_code == 200
    assert body["ok"] is True
    assert captured["ontology_id"] == "knowledge_package_demo"
    assert (
        manager.context.arguments["selected_ontology_id"]
        == "knowledge_package_demo"
    )


def test_workflow_bridge_rejects_context_conflicting_with_token_ontology(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        scope=_scope(ontology_id="knowledge_package_demo"),
        payload={
            "request": "合成示例问题",
            "session_id": "session_demo_context_conflict",
            "context": {"selected_ontology_id": "knowledge_package_other"},
        },
    )

    assert status_code == 403
    assert body["error"]["type"] == "auth_error"
    assert body["error"]["error_code"] == "MCP_ONTOLOGY_SCOPE_MISMATCH"
    assert manager.context is None


def test_workflow_bridge_preserves_unbound_argument_ontology(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        payload={
            "request": "合成示例问题",
            "session_id": "session_demo_unbound",
            "arguments": {"selected_ontology_id": "knowledge_package_demo"},
        },
    )

    assert status_code == 200
    assert body["ok"] is True
    assert (
        manager.context.arguments["selected_ontology_id"]
        == "knowledge_package_demo"
    )


def test_workflow_bridge_rejects_conflicting_token_ontology_before_run(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        scope=_scope(ontology_id="knowledge_package_demo"),
        payload={
            "request": "合成示例问题",
            "session_id": "session_demo_argument_conflict",
            "arguments": {"selected_ontology_id": "knowledge_package_other"},
        },
    )

    assert status_code == 403
    assert body["error"]["type"] == "auth_error"
    assert body["error"]["error_code"] == "MCP_ONTOLOGY_SCOPE_MISMATCH"
    assert manager.context is None


def test_mcp_timeout_cancels_the_same_canonical_run(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        never_finish=True,
    )

    assert status_code == 504
    assert body["error"]["error_code"] == "MCP_CHAT_EXECUTION_TIMEOUT"
    assert manager.cancelled == [
        (manager.context.run_id, "mcp_chat_execution_timeout")
    ]


def test_mcp_maps_runtime_admission_timeout(tmp_path):
    error = RuntimeErrorInfo(
        error_code="RUN_ADMISSION_TIMEOUT",
        message="synthetic admission timeout",
        retriable=True,
    )
    manager = _Manager(status=RunStatus.FAILED, content=None, error=error)
    body, status_code = _execute(tmp_path, manager)

    assert status_code == 504
    assert body["error"]["error_code"] == "MCP_CHAT_ADMISSION_TIMEOUT"
