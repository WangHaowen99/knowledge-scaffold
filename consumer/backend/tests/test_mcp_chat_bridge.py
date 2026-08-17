from pathlib import Path

import pytest

from api.protocols.mcp.chat_adapter import execute_chat_tool
from api.protocols.mcp.workflow_bridge import execute_mcp_chat_workflow
from integration.mcp.config import MCPRuntimeConfig
from integration.mcp.scope import MCPRequestScope
from persistence.run_queue import RunAdmissionQueue, RunSlotManager
from persistence.run_store import RunStatus, RunStore


def _config(path: Path) -> MCPRuntimeConfig:
    return MCPRuntimeConfig(
        server_host="127.0.0.1",
        server_port=7488,
        public_base_url="",
        agentic_base_url="http://backend.test",
        token_secret="test-secret-at-least-32-characters-long",
        token_algorithm="HS256",
        token_issuer="meta-wise",
        token_audience="meta-wise-mcp",
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
    def __init__(self, *, terminal_status="success", deterministic_applicable=False):
        self.slot_manager = RunSlotManager()
        self.run_admission_queue = RunAdmissionQueue(self.slot_manager)
        self.run_rate_limiter = _PermitAllLimiter()
        self.run_store = RunStore()
        self.terminal_status = terminal_status
        self.deterministic_applicable = deterministic_applicable
        self.last_run_kwargs = None

    def is_deterministic_request_potentially_applicable(self, **kwargs):
        del kwargs
        return self.deterministic_applicable

    def run(self, **kwargs):
        self.last_run_kwargs = dict(kwargs)
        run_id = kwargs["requested_run_id"]
        if self.terminal_status == "awaiting_input":
            self.run_store.await_run(
                run_id,
                {
                    "status": "awaiting_input",
                    "pending_user_interaction": {"type": "input", "prompt": "请输入名称"},
                },
            )
        yield {
            "protocol": "agent-stream.v2",
            "type": "turn.finished",
            "run_id": run_id,
            "session_id": kwargs["session_id"],
            "turn_id": 1,
            "seq": 1,
            "payload": {
                "status": self.terminal_status,
                "final_output": "等待输入" if self.terminal_status == "awaiting_input" else "完成",
            },
        }

    def cancel_run(self, run_id, reason=None):
        return self.run_store.cancel_run(run_id, metadata={"cancel_reason": reason}) is not None


class _TraceOrchestrator(_FakeOrchestrator):
    def run(self, **kwargs):
        self.last_run_kwargs = dict(kwargs)
        run_id = kwargs["requested_run_id"]
        session_id = kwargs["session_id"]
        yield {
            "protocol": "agent-stream.v2",
            "type": "reasoning.summary",
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": 1,
            "seq": 1,
            "payload": {"iteration": 1, "summary": "先读取本体，再查询业务实体"},
        }
        yield {
            "protocol": "agent-stream.v2",
            "type": "tool.started",
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": 1,
            "seq": 2,
            "payload": {
                "iteration": 1,
                "tool_call_id": "call-1",
                "tool_name": "business_data_search",
                "input": {
                    "ontology_id": "equipment",
                    "entity": "TargetItem",
                    "query": "查询 TargetItem",
                },
            },
        }
        yield {
            "protocol": "agent-stream.v2",
            "type": "tool.finished",
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": 1,
            "seq": 3,
            "payload": {
                "iteration": 1,
                "tool_call_id": "call-1",
                "tool_name": "business_data_search",
                "status": "completed",
                "elapsed_ms": 20,
                "output_preview": (
                    '{"provider":"neo4j","result_count":1,'
                    '"executed_steps":[{"tool":"property_filter",'
                    '"summary":"执行内部图工具 property_filter，返回 1 条"}]}'
                ),
            },
        }
        yield {
            "protocol": "agent-stream.v2",
            "type": "turn.finished",
            "run_id": run_id,
            "session_id": session_id,
            "turn_id": 1,
            "seq": 4,
            "payload": {"status": "success", "final_output": "完成"},
        }

class _StructuredSuccessOrchestrator(_FakeOrchestrator):
    def run(self, **kwargs):
        self.last_run_kwargs = dict(kwargs)
        run_id = kwargs["requested_run_id"]
        yield {
            "protocol": "agent-stream.v2",
            "type": "turn.finished",
            "run_id": run_id,
            "session_id": kwargs["session_id"],
            "turn_id": 1,
            "seq": 1,
            "payload": {
                "status": "success",
                "final_output": {
                    "type": "report",
                    "text": "Structured final answer",
                },
            },
        }


class _DeterministicFailureOrchestrator(_FakeOrchestrator):
    def run(self, **kwargs):
        self.last_run_kwargs = dict(kwargs)
        run_id = kwargs["requested_run_id"]
        yield {
            "protocol": "agent-stream.v2",
            "type": "turn.finished",
            "run_id": run_id,
            "session_id": kwargs["session_id"],
            "turn_id": 1,
            "seq": 1,
            "payload": {
                "status": "error",
                "error": "Required deterministic arguments are missing.",
                "error_code": "missing_arguments",
                "final_output": {
                    "status": "failed",
                    "text": "Required deterministic arguments are missing.",
                    "error": {
                        "code": "missing_arguments",
                        "message": "Required deterministic arguments are missing.",
                        "retryable": False,
                    },
                },
            },
        }


def test_mcp_preserves_deterministic_workflow_error_code(tmp_path):
    scope = MCPRequestScope(
        username="operator_a",
        workspace_id="workspace-a",
        scopes=["mcp:chat"],
        token="token",
        traceparent="",
    )

    body, status = execute_mcp_chat_workflow(
        orchestrator=_DeterministicFailureOrchestrator(deterministic_applicable=True),
        scope=scope,
        payload={
            "request": "query_score",
            "session_id": "session-det-failure",
            "requested_operation_key": "query_score",
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
        trace_config=_config(tmp_path / "tokens.json"),
    )

    assert status == 500
    assert body["error"]["error_code"] == "missing_arguments"
    assert body["error"]["retriable"] is False
    assert body["result"]["final_output"] == {
        "status": "failed",
        "text": "Required deterministic arguments are missing.",
        "error": {
            "code": "missing_arguments",
            "message": "Required deterministic arguments are missing.",
            "retryable": False,
        },
    }


def test_chat_adapter_forwards_token_and_explicit_operation_to_backend_bridge(
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

    monkeypatch.setattr("api.protocols.mcp.chat_adapter.requests.post", fake_post)
    scope = MCPRequestScope(
        username="alice",
        workspace_id="workspace-a",
        scopes=["mcp:chat"],
        token="mcp_secret-token",
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
    )

    result = execute_chat_tool(
        {
            "query": "分析问题",
            "session_id": "session-1",
            "requested_operation_key": " query_exact ",
            "include_trace": True,
        },
        scope=scope,
        config=_config(tmp_path / "tokens.json"),
    )

    assert result["ok"] is True
    assert captured["url"] == "http://backend.test/agentic/mcp/internal/chat"
    assert captured["headers"]["Authorization"] == "Bearer mcp_secret-token"
    assert captured["headers"]["X-Workspace-Id"] == "workspace-a"
    assert captured["headers"]["traceparent"] == scope.traceparent
    assert captured["json"]["session_id"] == "session-1"
    assert captured["json"]["requested_operation_key"] == "query_exact"
    assert captured["json"]["include_trace"] is True


def test_workflow_bridge_times_out_in_queue_without_bypassing_admission(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    orchestrator = _FakeOrchestrator()
    assert orchestrator.run_admission_queue.try_acquire("alice", 1) is True
    scope = MCPRequestScope(
        "alice",
        "workspace-a",
        ["mcp:chat"],
        token="mcp_token",
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
    )

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=orchestrator,
        scope=scope,
        payload={"request": "排队问题", "session_id": "session-queue"},
        concurrency_limit=1,
        queue_wait_seconds=0.01,
        execution_timeout_seconds=1,
    )

    assert status_code == 504
    assert body["error"]["error_code"] == "MCP_CHAT_ADMISSION_TIMEOUT"
    run_id = body["result"]["run_id"]
    assert orchestrator.run_admission_queue.position(run_id) is None
    assert orchestrator.run_store.get_run(run_id).status == RunStatus.CANCELLED
    orchestrator.run_admission_queue.release("alice")


def test_workflow_bridge_forwards_explicit_deterministic_operation_without_llm_validation(
    monkeypatch,
):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: pytest.fail("deterministic request must not validate LLM"),
    )
    orchestrator = _FakeOrchestrator(deterministic_applicable=True)
    scope = MCPRequestScope(
        "operator_a",
        "workspace-a",
        ["mcp:chat"],
        token="mcp_token",
    )

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=orchestrator,
        scope=scope,
        payload={
            "request": "Query Exact",
            "session_id": "session-deterministic",
            "requested_operation_key": "query",
            "arguments": {"code": "A"},
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 200
    assert body["ok"] is True
    assert orchestrator.last_run_kwargs["user_context"]["requested_operation_key"] == "query"
    assert orchestrator.last_run_kwargs["arguments"] == {"code": "A"}
    assert orchestrator.last_run_kwargs["deterministic_preflight_result"] is True


def test_workflow_bridge_accepts_context_deterministic_key_without_llm_validation(
    monkeypatch,
):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: pytest.fail("context key must not validate LLM"),
    )
    orchestrator = _FakeOrchestrator(deterministic_applicable=True)
    scope = MCPRequestScope("operator_a", "workspace-a", ["mcp:chat"], token="mcp_token")

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=orchestrator,
        scope=scope,
        payload={
            "request": "Query Exact",
            "session_id": "session-context-deterministic",
            "context": {"requested_operation_key": "query"},
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 200
    assert body["ok"] is True
    assert orchestrator.last_run_kwargs["user_context"]["requested_operation_key"] == "query"
    assert orchestrator.last_run_kwargs["deterministic_preflight_result"] is True


def test_workflow_bridge_rejects_non_object_arguments(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    orchestrator = _FakeOrchestrator()
    scope = MCPRequestScope("operator_a", "workspace-a", ["mcp:chat"], token="mcp_token")

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=orchestrator,
        scope=scope,
        payload={"request": "query", "arguments": ["bad"]},
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 400
    assert body["error"]["error_code"] == "INVALID_TOOL_ARGUMENTS"
    assert orchestrator.last_run_kwargs is None


def test_workflow_bridge_preserves_awaiting_input_and_run_owner(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    orchestrator = _FakeOrchestrator(terminal_status="awaiting_input")
    scope = MCPRequestScope(
        "alice",
        "workspace-a",
        ["mcp:chat"],
        token="mcp_token",
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
    )

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=orchestrator,
        scope=scope,
        payload={"request": "需要交互", "session_id": "session-awaiting"},
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 200
    assert body["ok"] is False
    assert body["error"]["error_code"] == "MCP_CHAT_AWAITING_INPUT"
    assert body["result"]["status"] == "awaiting_input"
    assert body["result"]["resumable"] is True
    run = orchestrator.run_store.get_run(body["result"]["run_id"])
    assert run.metadata["user_id"] == "alice"
    assert run.metadata["workspace_id"] == "workspace-a"
    assert run.metadata["external_trace_id"] == "a" * 32
    assert orchestrator.last_run_kwargs["traceparent"] == scope.traceparent
    assert orchestrator.last_run_kwargs["user_context"]["supports_user_interaction"] is False


def test_workflow_bridge_trace_requires_explicit_true_request_flag(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")

    for suffix, include_trace in (("default", None), ("false", False)):
        payload = {
            "request": "查询",
            "session_id": "session-trace-{}".format(suffix),
        }
        if include_trace is not None:
            payload["include_trace"] = include_trace
        body, status_code = execute_mcp_chat_workflow(
            orchestrator=_TraceOrchestrator(),
            scope=scope,
            payload=payload,
            concurrency_limit=1,
            queue_wait_seconds=1,
            execution_timeout_seconds=1,
            trace_config=_config(Path("tokens.json")),
        )

        assert status_code == 200
        assert body["result"]["content"] == "完成"
        assert "trace" not in body


def test_workflow_bridge_adds_trace_without_changing_result(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")
    config = _config(tmp_path / "tokens.json")

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=_TraceOrchestrator(),
        scope=scope,
        payload={
            "request": "查询",
            "session_id": "session-trace-on",
            "include_trace": True,
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
        trace_config=config,
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["result"] == {
        "content": "完成",
        "final_output": "完成",
        "session_id": "session-trace-on",
        "run_id": body["result"]["run_id"],
        "status": "success",
        "resumable": False,
    }
    assert body["trace"]["schema_version"] == "5.0"
    assert len(body["trace"]["iterations"]) == 1
    iteration = body["trace"]["iterations"][0]
    assert "reasoning_summaries" not in iteration
    assert iteration["agent_output"] == {
        "summary": "完成",
        "source": "final_output",
        "truncated": False,
    }
    assert iteration["tools"]["count"] == 1
    assert iteration["tools"]["calls"][0]["database_provider"] == "neo4j"
    assert iteration["tools"]["calls"][0]["record_count"] == 1
    assert iteration["tools"]["calls"][0]["executed_steps"] == [{
        "tool": "property_filter",
        "summary": "执行内部图工具 property_filter，返回 1 条",
    }]
    assert "evidence_ids" not in iteration["tools"]["calls"][0]
    assert body["trace"]["totals"]["iterations"] == 1
    assert body["trace"]["totals"]["overall_elapsed_ms"] >= 0


def test_workflow_bridge_coerces_structured_success_output_to_text(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")

    body, status_code = execute_mcp_chat_workflow(
        orchestrator=_StructuredSuccessOrchestrator(),
        scope=scope,
        payload={"request": "query", "session_id": "session-structured-success"},
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
    )

    assert status_code == 200
    assert body["ok"] is True
    assert body["result"]["content"] == "Structured final answer"
    assert body["result"]["final_output"] == {
        "type": "report",
        "text": "Structured final answer",
    }


def test_workflow_bridge_adds_trace_to_awaiting_input_and_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")
    config = _config(tmp_path / "tokens.json")

    awaiting_body, awaiting_status = execute_mcp_chat_workflow(
        orchestrator=_FakeOrchestrator(terminal_status="awaiting_input"),
        scope=scope,
        payload={
            "request": "查询",
            "session_id": "session-trace-awaiting",
            "include_trace": True,
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
        trace_config=config,
    )
    failed_body, failed_status = execute_mcp_chat_workflow(
        orchestrator=_FakeOrchestrator(terminal_status="error"),
        scope=scope,
        payload={
            "request": "查询",
            "session_id": "session-trace-failed",
            "include_trace": True,
        },
        concurrency_limit=1,
        queue_wait_seconds=1,
        execution_timeout_seconds=1,
        trace_config=config,
    )

    assert awaiting_status == 200
    assert awaiting_body["error"]["error_code"] == "MCP_CHAT_AWAITING_INPUT"
    assert awaiting_body["trace"]["schema_version"] == "5.0"
    assert awaiting_body["trace"]["iterations"][0]["agent_output"] == {
        "summary": "等待输入",
        "source": "final_output",
        "truncated": False,
    }
    assert failed_status == 500
    assert failed_body["error"]["error_code"] == "MCP_CHAT_WORKFLOW_FAILED"
    assert failed_body["trace"]["schema_version"] == "5.0"
    assert failed_body["trace"]["iterations"][0]["agent_output"] == {
        "summary": "完成",
        "source": "final_output",
        "truncated": False,
    }
