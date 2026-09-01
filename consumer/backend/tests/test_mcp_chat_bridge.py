"""MCP unary projection tests on the native ASGI event loop."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from agent.runtime.contracts import (
    PublicEvent,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeResult,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.runtime.event_bus import (
    EventSubscriberLimitExceeded,
    EventSubscriberReservationError,
)
from api.protocols.mcp import server as mcp_server
from api.protocols.mcp.chat_adapter import execute_chat_tool, execute_chat_tool_async
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
    def __init__(
        self,
        manager,
        *,
        trace=False,
        authoritative_solver_result=False,
        never_finish=False,
        detach_once=False,
        detach_terminal_once=False,
        reject_reservation=False,
        stream_error=False,
        invalidate_reservation_once=False,
    ):
        self.manager = manager
        self.trace = trace
        self.authoritative_solver_result = authoritative_solver_result
        self.never_finish = never_finish
        self.detach_once = detach_once
        self.detach_terminal_once = detach_terminal_once
        self.reject_reservation = reject_reservation
        self.stream_error = stream_error
        self.invalidate_reservation_once = invalidate_reservation_once
        self.reservations = set()
        self.released_reservations = []
        self.stream_calls = []

    async def reserve_subscriber(self, run_id):
        if self.reject_reservation:
            raise EventSubscriberLimitExceeded(scope="run", limit=1)
        reservation_id = "reservation_{}".format(len(self.reservations) + 1)
        self.reservations.add(reservation_id)
        return reservation_id

    async def release_subscriber_reservation(self, reservation_id):
        self.released_reservations.append(reservation_id)
        self.reservations.discard(reservation_id)

    def stream(self, run_id, *, after_seq=0, reservation_id=None):
        async def generate():
            self.stream_calls.append((run_id, after_seq, reservation_id))
            if (
                self.invalidate_reservation_once
                and len(self.stream_calls) == 1
                and reservation_id is not None
            ):
                self.reservations.discard(reservation_id)
                raise EventSubscriberReservationError(
                    "synthetic terminal-before-activation race"
                )
            if reservation_id is not None:
                self.reservations.discard(reservation_id)
            if self.detach_once and len(self.stream_calls) == 1:
                return
            if self.detach_terminal_once and len(self.stream_calls) == 1:
                event_count = 1
                if self.trace:
                    event_count += 2
                if self.authoritative_solver_result:
                    event_count += 1
                self.manager.finish(last_seq=event_count)
                return
            if self.stream_error:
                raise RuntimeError("synthetic stream failure")
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
            if self.authoritative_solver_result:
                events.append(
                    (
                        "tool.progress",
                        "running",
                        {
                            "run_id": run_id,
                            "job_id": "job_synthetic",
                            "attempt_id": "attempt_synthetic",
                            "event_seq": 8,
                            "status": "completed",
                            "authoritative": True,
                            "feasibility": "feasible",
                            "optimality": "optimal",
                            "proof_kind": "verifier_exhaustive_check",
                            "summary": {"combination_no": 10},
                            "result_artifact_ref": "artifact://solver/result",
                            "problem_ir": {"must_not": "leak"},
                            "tool_call_id": "call_solver",
                        },
                    )
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
    def __init__(
        self,
        manager,
        *,
        trace=False,
        authoritative_solver_result=False,
        never_finish=False,
        detach_once=False,
        detach_terminal_once=False,
        reject_reservation=False,
        stream_error=False,
        invalidate_reservation_once=False,
    ):
        self.manager = manager
        self.short_io = _InlinePool()
        self.event_hub = _EventHub(
            manager,
            trace=trace,
            authoritative_solver_result=authoritative_solver_result,
            never_finish=never_finish,
            detach_once=detach_once,
            detach_terminal_once=detach_terminal_once,
            reject_reservation=reject_reservation,
            stream_error=stream_error,
            invalidate_reservation_once=invalidate_reservation_once,
        )


class _OntologyOrchestrator:
    def __init__(self, *, session_metadata=None):
        self.session_metadata = session_metadata

    @staticmethod
    def is_deterministic_request_potentially_applicable(**_kwargs):
        return False

    def _get_stores(self, _username, _workspace_id):
        session = (
            SimpleNamespace(metadata=dict(self.session_metadata))
            if self.session_metadata is not None
            else None
        )
        return SimpleNamespace(get_session=lambda _session_id: session), None


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
    authoritative_solver_result=False,
    never_finish=False,
    detach_once=False,
    detach_terminal_once=False,
    reject_reservation=False,
    stream_error=False,
    invalidate_reservation_once=False,
    scope=None,
    runtime_capture=None,
    progress_capture=None,
):
    import api.protocols.mcp.workflow_bridge as module

    original = module.resolve_effective_llm_profile_config
    module.resolve_effective_llm_profile_config = lambda *_args, **_kwargs: {}
    try:
        runtime = _Runtime(
            manager,
            trace=trace,
            authoritative_solver_result=authoritative_solver_result,
            never_finish=never_finish,
            detach_once=detach_once,
            detach_terminal_once=detach_terminal_once,
            reject_reservation=reject_reservation,
            stream_error=stream_error,
            invalidate_reservation_once=invalidate_reservation_once,
        )
        if runtime_capture is not None:
            runtime_capture["runtime"] = runtime

        async def capture_progress(event):
            progress_capture.append(event)

        return asyncio.run(
            execute_mcp_chat_workflow_async(
                runtime=runtime,
                execution_store=ExecutionStore(str(tmp_path / "executions")),
                scope=scope or _scope(),
                payload=payload
                or {"request": "synthetic question", "session_id": "session_demo"},
                concurrency_limit=1,
                queue_wait_seconds=0.01,
                execution_timeout_seconds=0.01,
                trace_config=_config(tmp_path / "tokens.json"),
                progress_sink=(
                    capture_progress
                    if progress_capture is not None
                    else None
                ),
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


def test_mcp_streams_coarse_progress_without_changing_terminal_result(tmp_path):
    manager = _Manager()
    progress = []

    body, status_code = _execute(
        tmp_path,
        manager,
        trace=True,
        progress_capture=progress,
    )

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert [item["event"] for item in progress] == [
        "run.started",
        "ontology.ready",
        "tool.started",
        "tool.completed",
        "run.completed",
    ]
    assert all("content_delta" not in item for item in progress)


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


def test_mcp_projects_sanitized_authoritative_solver_result(tmp_path):
    manager = _Manager()
    body, status_code = _execute(
        tmp_path,
        manager,
        authoritative_solver_result=True,
    )

    assert status_code == 200
    result = body["result"]["authoritative_solver_result"]
    assert result["authoritative"] is True
    assert result["summary"] == {"combination_no": 10}
    assert result["result_artifact_ref"] == "artifact://solver/result"
    assert "problem_ir" not in result
    assert "tool_call_id" not in result


def test_mcp_preserves_non_retryable_solver_error_metadata(tmp_path):
    error = RuntimeErrorInfo(
        error_code="fact_snapshot_invalid",
        message="The synthetic fact snapshot is invalid.",
        retriable=False,
        source="tool",
        details={"phase": "snapshot"},
    )
    manager = _Manager(status=RunStatus.FAILED, content=None, error=error)
    body, status_code = _execute(tmp_path, manager)

    assert status_code == 500
    assert body["error"] == {
        "type": "workflow_error",
        "error_code": "fact_snapshot_invalid",
        "detail": "The synthetic fact snapshot is invalid.",
        "retriable": False,
        "phase": "snapshot",
    }


def test_mcp_chat_tool_exposes_selected_ontology_and_forwards_context(monkeypatch):
    registered = {}
    captured = {}

    class _FakeMCP:
        @staticmethod
        def tool():
            def decorate(function):
                registered[function.__name__] = function
                return function

            return decorate

    async def fake_execute(arguments, *, scope, progress_callback=None):
        captured.update(arguments=arguments, scope=scope)
        assert progress_callback is None
        return {"ok": True}

    scope = _scope()
    monkeypatch.setattr(mcp_server, "execute_chat_tool_async", fake_execute)

    async def call_with_scope(_required_scope, callback):
        return await callback(scope)

    mcp_server._register_chat_tool(
        _FakeMCP(),
        call_with_scope=call_with_scope,
    )

    result = asyncio.run(
        registered["chat"](
            query="solve synthetic plan",
            ctx=SimpleNamespace(request_context=SimpleNamespace(meta=None)),
            selected_ontology_id="knowledge_package_demo",
        )
    )

    assert result == {"ok": True}
    assert captured["arguments"]["context"] == {
        "selected_ontology_id": "knowledge_package_demo",
    }


def test_chat_tool_reports_progress_when_context_has_progress_token(
    monkeypatch,
):
    registered = {}
    scope = _scope()

    class _FakeMCP:
        @staticmethod
        def tool():
            def decorate(function):
                registered[function.__name__] = function
                return function

            return decorate

    async def fake_execute(arguments, *, scope, progress_callback=None):
        assert arguments["stream"] is True
        assert progress_callback is not None
        await progress_callback(
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
        return {"ok": True, "result": {"content": "synthetic answer"}}

    monkeypatch.setattr(
        mcp_server,
        "execute_chat_tool_async",
        fake_execute,
    )

    async def call_with_scope(_required_scope, callback):
        return await callback(scope)

    mcp_server._register_chat_tool(
        _FakeMCP(),
        call_with_scope=call_with_scope,
    )
    progress_calls = []

    class _Context:
        request_context = SimpleNamespace(
            meta=SimpleNamespace(progressToken="progress_demo")
        )

        @staticmethod
        async def report_progress(progress, total=None, message=None):
            progress_calls.append((progress, total, message))

    result = asyncio.run(
        registered["chat"](
            query="synthetic question",
            ctx=_Context(),
            stream=True,
        )
    )

    assert result["ok"] is True
    assert progress_calls[0][0] == 1
    assert '"event":"run.started"' in progress_calls[0][2]


def test_chat_adapter_http_budget_exceeds_backend_execution_budget(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"status": "success"}}

    def fake_post(url, *, headers, json, timeout):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _Response()

    monkeypatch.setattr(
        "api.protocols.mcp.chat_adapter.requests.post",
        fake_post,
    )

    result = execute_chat_tool(
        {
            "query": "solve synthetic plan",
            "context": {"selected_ontology_id": "knowledge_package_demo"},
        },
        scope=_scope(),
        config=_config(tmp_path / "tokens.json"),
    )

    assert result["ok"] is True
    assert captured["json"]["context"]["selected_ontology_id"] == (
        "knowledge_package_demo"
    )
    assert captured["timeout"] > 30


def test_async_chat_adapter_consumes_internal_progress_stream(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class _StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_lines(self):
            for line in (
                "event: progress",
                'data: {"schema_version":"1.0","seq":1,"event":"run.started"}',
                "",
                "event: result",
                'data: {"status_code":200,"body":{"ok":true,"result":{"content":"synthetic answer"}}}',
                "",
            ):
                yield line

    class _AsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, *, headers, json):
            captured.update(
                method=method,
                url=url,
                headers=headers,
                json=json,
            )
            return _StreamResponse()

    monkeypatch.setattr(
        "api.protocols.mcp.chat_adapter.httpx.AsyncClient",
        _AsyncClient,
    )
    progress = []

    async def on_progress(event):
        progress.append(event)

    result = asyncio.run(
        execute_chat_tool_async(
            {"query": "synthetic question", "stream": True},
            scope=_scope(),
            progress_callback=on_progress,
            config=_config(tmp_path / "tokens.json"),
        )
    )

    assert result["ok"] is True
    assert result["result"]["content"] == "synthetic answer"
    assert progress == [
        {"schema_version": "1.0", "seq": 1, "event": "run.started"}
    ]
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/agentic/mcp/internal/chat/stream")


def test_async_chat_adapter_falls_back_without_progress_callback(
    monkeypatch,
    tmp_path,
):
    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": {"content": "synthetic answer"}}

    class _AsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, json=json)
            return _Response()

    monkeypatch.setattr(
        "api.protocols.mcp.chat_adapter.httpx.AsyncClient",
        _AsyncClient,
    )

    result = asyncio.run(
        execute_chat_tool_async(
            {"query": "synthetic question", "stream": True},
            scope=_scope(),
            progress_callback=None,
            config=_config(tmp_path / "tokens.json"),
        )
    )

    assert result["ok"] is True
    assert captured["url"].endswith("/agentic/mcp/internal/chat")
    assert not captured["url"].endswith("/stream")


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
    assert manager.context.arguments["selected_ontology_version"] == "1.0.0"
    assert manager.context.arguments["selected_version"] == "1.0.0"


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


def test_workflow_bridge_auto_selects_only_enabled_ontology(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "api.routes.get_orchestrator",
        lambda: _OntologyOrchestrator(),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(
            list_enabled_ontologies=lambda: ["knowledge_package_demo"],
        ),
    )
    manager = _Manager()

    body, status_code = _execute(tmp_path, manager)

    assert status_code == 200
    assert body["ok"] is True
    assert manager.context.arguments["selected_ontology_id"] == (
        "knowledge_package_demo"
    )


def test_workflow_bridge_requires_selection_for_new_multi_ontology_session(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "api.routes.get_orchestrator",
        lambda: _OntologyOrchestrator(),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(
            list_enabled_ontologies=lambda: [
                "knowledge_package_a",
                "knowledge_package_b",
            ],
        ),
    )
    manager = _Manager()

    body, status_code = _execute(tmp_path, manager)

    assert status_code == 400
    assert body["error"]["error_code"] == "MCP_ONTOLOGY_SELECTION_REQUIRED"
    assert manager.context is None


def test_workflow_bridge_reuses_locked_session_ontology(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "api.routes.get_orchestrator",
        lambda: _OntologyOrchestrator(
            session_metadata={
                "selected_ontology_id": "knowledge_package_b",
                "selected_ontology_version": "2.1.0",
            }
        ),
    )
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.get_ontology_registry",
        lambda **_kwargs: SimpleNamespace(
            list_enabled_ontologies=lambda: [
                "knowledge_package_a",
                "knowledge_package_b",
            ],
        ),
    )
    manager = _Manager()

    body, status_code = _execute(tmp_path, manager)

    assert status_code == 200
    assert body["ok"] is True
    assert manager.context.arguments == {
        "selected_ontology_id": "knowledge_package_b",
        "selected_ontology_version": "2.1.0",
        "selected_version": "2.1.0",
    }


def test_workflow_bridge_bound_token_preserves_locked_session_version(
    monkeypatch,
    tmp_path,
):
    captured = {}
    monkeypatch.setattr(
        "api.routes.get_orchestrator",
        lambda: _OntologyOrchestrator(
            session_metadata={
                "selected_ontology_id": "knowledge_package_demo",
                "selected_ontology_version": "1.0.0",
            }
        ),
    )

    def validate_available(**kwargs):
        captured.update(kwargs)
        return kwargs.get("ontology_version") or "2.0.0"

    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.validate_runtime_ontology_availability",
        validate_available,
    )
    manager = _Manager()

    body, status_code = _execute(
        tmp_path,
        manager,
        scope=_scope(ontology_id="knowledge_package_demo"),
        payload={
            "request": "synthetic question",
            "session_id": "session_demo_locked_bound",
        },
    )

    assert status_code == 200
    assert body["ok"] is True
    assert captured == {
        "workspace_id": "workspace_demo",
        "ontology_id": "knowledge_package_demo",
        "ontology_version": "1.0.0",
    }
    assert manager.context.arguments == {
        "selected_ontology_id": "knowledge_package_demo",
        "selected_ontology_version": "1.0.0",
        "selected_version": "1.0.0",
    }


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
    captured = {}
    body, status_code = _execute(
        tmp_path,
        manager,
        never_finish=True,
        runtime_capture=captured,
    )

    assert status_code == 504
    assert body["error"]["error_code"] == "MCP_CHAT_EXECUTION_TIMEOUT"
    assert manager.cancelled == [
        (manager.context.run_id, "mcp_chat_execution_timeout")
    ]
    assert captured["runtime"].event_hub.released_reservations == [
        "reservation_1"
    ]


def test_mcp_rejects_run_before_start_when_subscriber_capacity_is_exhausted(
    tmp_path,
):
    manager = _Manager()

    body, status_code = _execute(
        tmp_path,
        manager,
        reject_reservation=True,
    )

    assert status_code == 429
    assert body["error"]["error_code"] == "RUNTIME_CAPACITY_EXCEEDED"
    assert manager.context is None
    assert manager.snapshot is None


def test_mcp_reattaches_after_slow_subscriber_detach(tmp_path):
    manager = _Manager()
    captured = {}

    body, status_code = _execute(
        tmp_path,
        manager,
        payload={"request": "synthetic question", "session_id": "session_detach"},
        detach_once=True,
        runtime_capture=captured,
    )
    runtime = captured["runtime"]

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert len(runtime.event_hub.stream_calls) == 2
    assert runtime.event_hub.stream_calls[1][1] == 0
    assert runtime.event_hub.stream_calls[1][2] is None
    assert runtime.event_hub.released_reservations == ["reservation_1"]


def test_mcp_replays_public_events_when_detach_races_terminal(tmp_path):
    manager = _Manager()
    captured = {}

    body, status_code = _execute(
        tmp_path,
        manager,
        payload={
            "request": "synthetic question",
            "session_id": "session_terminal_detach",
            "include_trace": True,
        },
        trace=True,
        authoritative_solver_result=True,
        detach_terminal_once=True,
        runtime_capture=captured,
    )
    runtime = captured["runtime"]

    assert status_code == 200
    assert body["result"]["authoritative_solver_result"]["authoritative"] is True
    assert body["trace"]["totals"]["tool_calls"] == 1
    assert len(runtime.event_hub.stream_calls) == 2
    assert runtime.event_hub.stream_calls[1][1] == 0


def test_mcp_replays_after_terminal_invalidates_lazy_reservation(tmp_path):
    manager = _Manager()
    captured = {}

    body, status_code = _execute(
        tmp_path,
        manager,
        invalidate_reservation_once=True,
        runtime_capture=captured,
    )
    runtime = captured["runtime"]

    assert status_code == 200
    assert body["result"]["content"] == "synthetic answer"
    assert len(runtime.event_hub.stream_calls) == 2
    assert runtime.event_hub.stream_calls[0][2] == "reservation_1"
    assert runtime.event_hub.stream_calls[1][2] is None


def test_mcp_request_cancellation_cancels_started_canonical_run(
    monkeypatch,
    tmp_path,
):
    manager = _Manager()
    runtime = _Runtime(manager, never_finish=True)
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )

    async def scenario():
        task = asyncio.create_task(
            execute_mcp_chat_workflow_async(
                runtime=runtime,
                execution_store=ExecutionStore(str(tmp_path / "executions")),
                scope=_scope(),
                payload={
                    "request": "synthetic question",
                    "session_id": "session_cancelled",
                },
                concurrency_limit=1,
                queue_wait_seconds=10,
                execution_timeout_seconds=10,
                trace_config=_config(tmp_path / "tokens.json"),
            )
        )
        while not runtime.event_hub.stream_calls:
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("MCP request cancellation must propagate")

    asyncio.run(scenario())

    assert manager.cancelled == [
        (manager.context.run_id, "mcp_chat_request_cancelled")
    ]
    assert runtime.event_hub.released_reservations == ["reservation_1"]


def test_mcp_cancellation_marks_turn_failed_after_offload_finishes(
    monkeypatch,
    tmp_path,
):
    manager = _Manager()
    runtime = _Runtime(manager)
    pool = BoundedOffloadPool(
        name="mcp-cancellation-test",
        max_workers=1,
        max_pending=2,
        capacity_wait_seconds=1,
        operation_timeout_seconds=5,
    )
    runtime.short_io = pool
    store = ExecutionStore(str(tmp_path / "executions"))
    started = threading.Event()
    release = threading.Event()

    class _DelayedExecutionStore:
        run_id = ""
        requested_run_id = ""

        def create_turn_for_run(self, **kwargs):
            self.requested_run_id = str(kwargs.get("run_id") or "")
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError("synthetic turn allocation was not released")
            turn, created = store.create_turn_for_run(**kwargs)
            self.run_id = str(turn["run_id"])
            return turn, created

        def mark_turn_task_start_failed(self, session_id, turn_id):
            return store.mark_turn_task_start_failed(session_id, turn_id)

    delayed_store = _DelayedExecutionStore()
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )

    async def scenario():
        try:
            task = asyncio.create_task(
                execute_mcp_chat_workflow_async(
                    runtime=runtime,
                    execution_store=delayed_store,
                    scope=_scope(),
                    payload={
                        "request": "synthetic question",
                        "session_id": "session_cancelled_allocation",
                    },
                    concurrency_limit=1,
                    queue_wait_seconds=10,
                    execution_timeout_seconds=10,
                    trace_config=_config(tmp_path / "tokens.json"),
                )
            )
            start_deadline = asyncio.get_running_loop().time() + 2
            while (
                not started.is_set()
                and not task.done()
                and asyncio.get_running_loop().time() < start_deadline
            ):
                await asyncio.sleep(0)
            assert started.is_set(), "turn allocation did not reach the worker"
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            return store.get_turn_by_run_id(
                "session_cancelled_allocation",
                delayed_store.run_id,
            )
        finally:
            release.set()
            await pool.close(deadline=asyncio.get_running_loop().time() + 2)

    turn = asyncio.run(scenario())

    assert turn is not None
    assert turn["status"] == "failed"
    assert turn["error_code"] == "TASK_START_FAILED"
    assert delayed_store.requested_run_id == delayed_store.run_id
    assert manager.context is None


def test_mcp_stream_failure_cancels_started_canonical_run(tmp_path):
    manager = _Manager()
    captured = {}

    body, status_code = _execute(
        tmp_path,
        manager,
        stream_error=True,
        runtime_capture=captured,
    )

    assert status_code == 500
    assert body["error"]["error_code"] == "MCP_CHAT_WORKFLOW_FAILED"
    assert manager.cancelled == [
        (manager.context.run_id, "mcp_chat_workflow_failed")
    ]
    assert captured["runtime"].event_hub.released_reservations == [
        "reservation_1"
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
