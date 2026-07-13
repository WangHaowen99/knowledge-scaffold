from pathlib import Path

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
        token_secret="test-secret",
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
    def __init__(self, *, terminal_status="success"):
        self.slot_manager = RunSlotManager()
        self.run_admission_queue = RunAdmissionQueue(self.slot_manager)
        self.run_rate_limiter = _PermitAllLimiter()
        self.run_store = RunStore()
        self.terminal_status = terminal_status

    def run(self, **kwargs):
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
    scope = MCPRequestScope(
        username="alice",
        workspace_id="workspace-a",
        scopes=["mcp:chat"],
        token="mcp_secret-token",
    )

    result = execute_chat_tool(
        {"query": "分析问题", "session_id": "session-1"},
        scope=scope,
        config=_config(tmp_path / "tokens.json"),
    )

    assert result["ok"] is True
    assert captured["url"] == "http://backend.test/agentic/internal/mcp/chat"
    assert captured["headers"]["Authorization"] == "Bearer mcp_secret-token"
    assert captured["headers"]["X-Workspace-Id"] == "workspace-a"
    assert captured["json"]["session_id"] == "session-1"


def test_workflow_bridge_times_out_in_queue_without_bypassing_admission(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    orchestrator = _FakeOrchestrator()
    assert orchestrator.run_admission_queue.try_acquire("alice", 1) is True
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")

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


def test_workflow_bridge_preserves_awaiting_input_and_run_owner(monkeypatch):
    monkeypatch.setattr(
        "api.protocols.mcp.workflow_bridge.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {},
    )
    orchestrator = _FakeOrchestrator(terminal_status="awaiting_input")
    scope = MCPRequestScope("alice", "workspace-a", ["mcp:chat"], token="mcp_token")

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
