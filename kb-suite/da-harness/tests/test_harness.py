"""DeepAnalyzeHarness against a fake DA engine (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from openjiuwen.harness_protocol import (
    DeliveryMode,
    HarnessContext,
    HarnessInput,
    HarnessState,
    HostCapability,
    InteractionResponseStatus,
    ItemEventKind,
    ItemLifecycleEvent,
    OutputChannel,
    OutputEvent,
    ProviderEvent,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalResponse,
    TurnEventKind,
    TurnLifecycleEvent,
    UsageUpdatedEvent,
    UserInputResponse,
)
from openjiuwen.harness_providers.base import ProviderStartupError

from deepanalyze_harness import DeepAnalyzeConfig, DeepAnalyzeHarness, DeepAnalyzeHarnessProvider

TASK = "task-1"


def sse(events: list[tuple[str, dict[str, Any]]], *, start_id: int = 1) -> bytes:
    out = []
    for offset, (name, data) in enumerate(events):
        out.append(f"id: {TASK}-{start_id + offset}\nevent: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")
    return "".join(out).encode()


class FakeDA:
    def __init__(self) -> None:
        self.run_body: bytes = b""
        self.calls: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []
        self.run_stream: Any = b""
        self.reconnect_stream: bytes = b""
        self.status = "running"

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, request.url.path, body, dict(request.headers)))
        path = request.url.path
        if path == "/ready":
            return httpx.Response(200, json={"status": "UP"})
        if path.endswith("/run-stream"):
            stream = self.run_stream.pop(0) if isinstance(self.run_stream, list) else self.run_stream
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)
        if path.startswith("/api/agents/stream/"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=self.reconnect_stream)
        if path.startswith("/api/agents/status/"):
            return httpx.Response(200, json={"taskId": TASK, "status": self.status})
        if path.startswith(("/api/agents/answer/", "/api/agents/cancel/", "/api/agents/inject/")):
            return httpx.Response(200, json={"taskId": TASK, "status": "ok"})
        if path == "/v1/kbs":
            return httpx.Response(200, json={"items": [{"id": "kb-a", "name": "A"}, {"id": "kb-b", "name": "B"}]})
        return httpx.Response(404)

    def posted(self, kind: str) -> list[dict[str, Any]]:
        return [body for method, path, body, _ in self.calls if method == "POST" and f"/{kind}/" in path]


class Handler:
    def __init__(self, user_answer: Any = "蓝色", decision: ToolApprovalDecision = ToolApprovalDecision.ALLOW,
                 status: InteractionResponseStatus = InteractionResponseStatus.COMPLETED) -> None:
        self.requests: list[Any] = []
        self.user_answer, self.decision, self.status = user_answer, decision, status

    async def handle(self, request: Any) -> Any:
        self.requests.append(request)
        if type(request).__name__ == "ToolApprovalRequest":
            return ToolApprovalResponse(request_id=request.request_id, decision=self.decision)
        return UserInputResponse(request_id=request.request_id, status=self.status, content=self.user_answer)

    async def cancel(self, request_id: str, **_: Any) -> None:
        pass


def make(fake: FakeDA, monkeypatch: pytest.MonkeyPatch, **config: Any) -> DeepAnalyzeHarness:
    monkeypatch.setenv("DA_ENGINE_SECRET", "s3cret")
    cfg = DeepAnalyzeConfig.from_mapping({"base_url": "http://da", "tenant_id": "t1", "kb_ids": ["kb1"],
                                          "reconnect_backoff_s": 0.01, "abort_wait_s": 0.5, **config})
    return DeepAnalyzeHarness(cfg, transport=httpx.MockTransport(fake.handler))


def context(handler: Any = None, **kw: Any) -> HarnessContext:
    caps = frozenset({HostCapability.USER_INPUT, HostCapability.TOOL_APPROVAL}) if handler else frozenset()
    return HarnessContext(agent_name="t", agent_id="agent-t", host_session_id=kw.pop("host_session_id", "host-1"),
                          system_prompt=kw.pop("system_prompt", ""), host_capabilities=caps, interactions=handler,
                          **kw)


async def run_turn(harness: DeepAnalyzeHarness, text: str = "hi") -> list[Any]:
    receipt = await harness.send(HarnessInput(content=text))
    return [event async for event in harness.turn_events(receipt.turn_id)]


def payloads(events: list[Any], kind: type) -> list[Any]:
    return [e.event for e in events if isinstance(e.event, kind)]


TEXT_TURN = [
    ("start", {"taskId": TASK}),
    ("content_start", {"turn": 0}),
    ("content_delta", {"delta": "你", "turn": 0}),
    ("content_delta", {"delta": "好", "turn": 0}),
    ("turn_usage", {"turn": 0, "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}),
    ("turn", {"turn": 0}),
    ("content", {"content": "你好"}),
    ("complete", {"output": "你好", "toolCalls": []}),
    ("done", {"status": "completed", "output": "你好"}),
]


async def test_text_turn_maps_outputs_usage_and_result(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN)
    harness = make(fake, monkeypatch)
    await harness.start(context())
    assert harness.state is HarnessState.IDLE
    events = await run_turn(harness, "打个招呼")
    lifecycle = payloads(events, TurnLifecycleEvent)
    assert [e.kind for e in lifecycle] == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
    deltas = [e for e in payloads(events, OutputEvent) if e.channel is OutputChannel.ANSWER]
    assert "".join(str(e.content) for e in deltas if e.operation.value == "delta") == "你好"
    assert payloads(events, UsageUpdatedEvent)[0].usage.total_tokens == 12
    steps = [e for e in payloads(events, ItemLifecycleEvent) if e.item_type == "step"]
    assert steps and steps[0].kind is ItemEventKind.COMPLETED
    result = lifecycle[-1].result
    assert result.final_output == "你好" and result.usage.total_tokens == 12 and result.duration_ms is not None
    run_body = fake.calls[1][2]
    assert run_body["scope"] == {"kbIds": ["kb1"]} and run_body["sessionId"] == "t1:host-1"
    headers = fake.calls[1][3]
    assert headers["x-engine-secret"] == "s3cret" and headers["x-da-tenant-id"] == "t1"
    checkpoint = await harness.export_checkpoint()
    assert checkpoint.data["da_session_id"] == "t1:host-1" and checkpoint.data["last_task_id"] == TASK
    assert "s3cret" not in json.dumps(dict(checkpoint.data))
    await harness.stop()
    assert harness.state is HarnessState.TERMINATED


async def test_tool_items_and_unknown_events(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("tool_call", {"id": "c1", "toolName": "kb_search", "input": {"query": "油泵"}}),
        ("tool_result", {"id": "c1", "toolName": "kb_search", "output": "[]"}),
        ("todo_update", {"items": [1]}),
        ("done", {"status": "completed", "output": "ok"}),
    ])
    harness = make(fake, monkeypatch)
    await harness.start(context())
    events = await run_turn(harness)
    tools = [e for e in events if isinstance(e.event, ItemLifecycleEvent) and e.event.item_type == "tool"]
    assert [t.event.kind for t in tools] == [ItemEventKind.STARTED, ItemEventKind.COMPLETED]
    assert tools[0].item_id == tools[1].item_id == f"{TASK}:0:c1"
    assert tools[0].event.data["name"] == "kb_search" and tools[1].event.data["tool_name"] == "kb_search"
    assert [p.event_type for p in payloads(events, ProviderEvent)] == ["todo_update"]
    await harness.stop()


async def test_ask_user_routes_through_host_and_bumps_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("ask_user", {"question": "你喜欢什么颜色？", "options": ["红", "蓝色"], "kind": "question",
                      "toolCallId": "ask-1", "taskId": TASK}),
        ("tool_call", {"id": "c1", "toolName": "expand", "input": {}}),
        ("tool_result", {"id": "c1", "toolName": "expand", "output": "x"}),
        ("done", {"status": "completed", "output": "COLOR: 蓝色"}),
    ])
    handler = Handler()
    harness = make(fake, monkeypatch)
    await harness.start(context(handler))
    events = await run_turn(harness)
    assert handler.requests[0].prompt == "你喜欢什么颜色？" and handler.requests[0].choices == ("红", "蓝色")
    assert fake.posted("answer") == [{"answer": "蓝色", "toolCallId": "ask-1"}]
    tool = next(e for e in events if isinstance(e.event, ItemLifecycleEvent) and e.event.item_type == "tool")
    assert tool.item_id == f"{TASK}:1:c1"
    assert payloads(events, TurnLifecycleEvent)[-1].kind is TurnEventKind.FINISHED
    await harness.stop()


async def test_ask_user_tool_result_closes_the_call_opened_before_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DA reports the ask_user result after the resume and restarts tool numbering,
    # so a fresh call in the new segment can reuse the same DA id.
    ask_id = f"{TASK}-tc-0-0-ask_user"
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("tool_call", {"id": ask_id, "toolName": "ask_user", "input": {}}),
        ("ask_user", {"question": "哪个子系统？", "options": ["润滑油", "冷却水"], "kind": "question",
                      "toolCallId": ask_id, "taskId": TASK}),
        ("tool_result", {"id": ask_id, "toolName": "ask_user", "output": "冷却水"}),
        ("tool_call", {"id": f"{TASK}-tc-0-0-expand", "toolName": "expand", "input": {}}),
        ("tool_result", {"id": f"{TASK}-tc-0-0-expand", "toolName": "expand", "output": "x"}),
        ("done", {"status": "completed", "output": "ok"}),
    ])
    harness = make(fake, monkeypatch)
    await harness.start(context(Handler()))
    events = await run_turn(harness)
    tools = [(e.item_id, e.event.kind) for e in events
             if isinstance(e.event, ItemLifecycleEvent) and e.event.item_type == "tool"]
    assert tools == [
        (f"{TASK}:0:{ask_id}", ItemEventKind.STARTED),
        (f"{TASK}:0:{ask_id}", ItemEventKind.COMPLETED),
        (f"{TASK}:1:{TASK}-tc-0-0-expand", ItemEventKind.STARTED),
        (f"{TASK}:1:{TASK}-tc-0-0-expand", ItemEventKind.COMPLETED),
    ]
    await harness.stop()


async def test_declined_question_cancels_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("ask_user", {"question": "继续吗？", "kind": "question", "toolCallId": "ask-1"}),
        ("done", {"status": "cancelled"}),
    ])
    harness = make(fake, monkeypatch)
    await harness.start(context(Handler(status=InteractionResponseStatus.DECLINED)))
    events = await run_turn(harness)
    assert fake.posted("cancel") and not fake.posted("answer")
    terminal = payloads(events, TurnLifecycleEvent)[-1]
    assert terminal.kind is TurnEventKind.ABORTED and terminal.result.termination is not None
    await harness.stop()


async def test_permission_confirm_maps_to_tool_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("ask_user", {"question": "允许执行 bash 吗？", "kind": "permission_confirm", "toolName": "bash",
                      "toolCallId": "tc-9", "options": ["允许", "拒绝"]}),
        ("done", {"status": "completed", "output": "done"}),
    ])
    handler = Handler(decision=ToolApprovalDecision.DENY)
    harness = make(fake, monkeypatch)
    await harness.start(context(handler))
    await run_turn(harness)
    assert type(handler.requests[0]).__name__ == "ToolApprovalRequest"
    assert handler.requests[0].tool_name == "bash" and handler.requests[0].call_id == "tc-9"
    assert fake.posted("answer") == [{"answer": "拒绝", "toolCallId": "tc-9"}]
    await harness.stop()


async def test_without_host_interactions_questions_continue_and_permissions_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": TASK}),
        ("ask_user", {"question": "q?", "kind": "question", "toolCallId": "a1"}),
        ("ask_user", {"question": "允许？", "kind": "permission_confirm", "toolCallId": "a2", "toolName": "bash"}),
        ("done", {"status": "completed", "output": "ok"}),
    ])
    harness = make(fake, monkeypatch)
    await harness.start(context())
    await run_turn(harness)
    answers = fake.posted("answer")
    assert answers[0]["toolCallId"] == "a1" and "无法向用户提问" in answers[0]["answer"]
    assert answers[1] == {"answer": "拒绝", "toolCallId": "a2"}
    await harness.stop()


async def test_reconnects_with_last_event_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN[:3])  # ends after ids 1..3, no done
    fake.reconnect_stream = sse(TEXT_TURN[3:], start_id=4)
    harness = make(fake, monkeypatch)
    await harness.start(context())
    events = await run_turn(harness)
    reconnect = [c for c in fake.calls if c[1] == f"/api/agents/stream/{TASK}"]
    assert reconnect and reconnect[0][3]["last-event-id"] == f"{TASK}-3"
    terminal = payloads(events, TurnLifecycleEvent)[-1]
    assert terminal.kind is TurnEventKind.FINISHED and terminal.result.final_output == "你好"
    await harness.stop()


async def test_abort_cancels_and_ends_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    cancelled = asyncio.Event()
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/agents/cancel/"):
            cancelled.set()
        return original(request)

    async def slow_stream():
        yield sse(TEXT_TURN[:3])
        await cancelled.wait()
        yield sse([("done", {"status": "cancelled"})], start_id=4)

    fake.run_stream = slow_stream()
    monkeypatch.setenv("DA_ENGINE_SECRET", "s3cret")
    harness = DeepAnalyzeHarness(
        DeepAnalyzeConfig.from_mapping({"base_url": "http://da", "tenant_id": "t1"}),
        transport=httpx.MockTransport(handler),
    )
    await harness.start(context())
    receipt = await harness.send(HarnessInput(content="long"))
    events = []
    async for event in harness.turn_events(receipt.turn_id):
        events.append(event)
        if isinstance(event.event, OutputEvent) and not cancelled.is_set():
            await harness.abort()
    terminal = payloads(events, TurnLifecycleEvent)[-1]
    assert terminal.kind is TurnEventKind.ABORTED and fake.posted("cancel")
    assert harness.state is HarnessState.IDLE
    await harness.stop()


async def test_follow_up_and_steer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN)
    harness = make(fake, monkeypatch)
    await harness.start(context())
    first = await harness.send(HarnessInput(content="a"))
    second = await harness.send(HarnessInput(content="b"))
    assert second.accepted_mode is DeliveryMode.FOLLOW_UP
    for receipt in (first, second):
        events = [e async for e in harness.turn_events(receipt.turn_id)]
        assert payloads(events, TurnLifecycleEvent)[-1].kind is TurnEventKind.FINISHED
    await harness.stop()


async def test_steer_hook_takes_the_receipt_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Newer openjiuwen passes the receipt's message_id to _steer; older ones do not.
    fake = FakeDA()
    harness = make(fake, monkeypatch)
    await harness.start(context())
    harness._task_id = TASK  # noqa: SLF001 - a task is running
    await harness._steer(None, HarnessInput(content="补充"), message_id="message-1")  # noqa: SLF001
    await harness._steer(None, HarnessInput(content="再补充"))  # noqa: SLF001
    assert fake.posted("inject") == [{"message": "补充"}, {"message": "再补充"}]
    await harness.stop()


async def test_resume_uses_checkpoint_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN)
    first = make(fake, monkeypatch, session_prefix="p-")
    await first.start(context(host_session_id="h-9"))
    await run_turn(first)
    checkpoint = await first.export_checkpoint()
    await first.stop()
    fake.run_stream = sse(TEXT_TURN)
    second = make(fake, monkeypatch)
    await second.start(context(host_session_id="h-9", resume_policy=ResumePolicy.REQUIRE_RESUME,
                               checkpoint=checkpoint))
    assert second.provider_session_id == "t1:p-h-9"
    await second.stop()
    third = make(fake, monkeypatch)
    with pytest.raises(ProviderStartupError):
        await third.start(context(host_session_id=str(uuid.uuid4()), resume_policy=ResumePolicy.REQUIRE_RESUME))


async def test_engine_busy_is_retried_before_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = [
        sse([("start", {"taskId": TASK}),
             ("error", {"code": "ENGINE_BUSY", "message": "Conversation busy"}),
             ("done", {"status": "failed"})]),
        sse(TEXT_TURN),
    ]
    harness = make(fake, monkeypatch, busy_poll_s=0.01)
    await harness.start(context())
    events = await run_turn(harness)
    terminal = payloads(events, TurnLifecycleEvent)[-1]
    assert terminal.kind is TurnEventKind.FINISHED and terminal.result.final_output == "你好"
    assert sum(1 for c in fake.calls if c[1].endswith("/run-stream")) == 2
    assert [e.kind for e in payloads(events, TurnLifecycleEvent)] == [TurnEventKind.STARTED, TurnEventKind.FINISHED]
    await harness.stop()


async def test_start_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DA_ENGINE_SECRET", raising=False)
    harness = DeepAnalyzeHarnessProvider.create({"base_url": "http://da", "tenant_id": "t"})
    with pytest.raises(ProviderStartupError):
        await harness.start(context())


def test_config_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        DeepAnalyzeConfig.from_mapping({"base_url": "http://da", "bogus": 1})
    with pytest.raises(ValueError):
        DeepAnalyzeConfig.from_mapping({"base_url": "ftp://x"})
    assert DeepAnalyzeHarnessProvider().card.name == "deepanalyze"


def test_turn_result_survives_a_wall_clock_step_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as real_time

    from deepanalyze_harness import mapping

    started_at, started_monotonic = real_time.time(), real_time.monotonic()
    monkeypatch.setattr(mapping.time, "time", lambda: started_at - 5)  # clock resynced backwards
    kind, result = mapping.DaTurnState(user_text="q").result(
        started_at=started_at, started_monotonic=started_monotonic)
    assert kind is TurnEventKind.FINISHED and result.completed_at >= result.started_at
