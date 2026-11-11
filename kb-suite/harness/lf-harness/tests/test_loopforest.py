"""LoopForestHarness: LF_ settings, its card, and a turn against a fake DA-compatible service."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openjiuwen.harness_protocol import (
    HarnessCapability,
    HarnessContext,
    HarnessInput,
    HostCapability,
    TurnEventKind,
    TurnLifecycleEvent,
)

from deepanalyze_harness.config import DeepAnalyzeConfig
from loopforest_harness import LoopForestHarness, LoopForestHarnessProvider

LF_ENV = {"LF_BASE_URL": "https://lf.example.com:8193", "LF_TENANT_ID": "t1", "LF_ENGINE_SECRET": "lf-secret"}


def sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"id: t-{i}\nevent: {n}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n"
                   for i, (n, d) in enumerate(events, 1)).encode()


def test_settings_come_from_lf_variables() -> None:
    cfg = DeepAnalyzeConfig.from_mapping({"env": dict(LF_ENV), "cwd": "/w"}, env_prefix="LF_")
    assert (cfg.base_url, cfg.tenant_id, cfg.secret_env) == ("https://lf.example.com:8193", "t1", "LF_ENGINE_SECRET")
    with pytest.raises(ValueError, match="LF_BASE_URL"):
        DeepAnalyzeConfig.from_mapping({"env": {}}, env_prefix="LF_")


def test_card_has_no_user_interaction() -> None:
    provider = LoopForestHarnessProvider()
    assert provider.card.name == "loopforest"
    assert provider.card.supports(HarnessCapability.MCP_TOOLS)
    assert HostCapability.USER_INPUT not in provider.card.optional_host_capabilities
    assert provider.model_config(object()) == {}


async def test_turn_and_team_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in LF_ENV:
        monkeypatch.delenv(var, raising=False)
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-engine-secret"] == "lf-secret"
        if request.url.path == "/ready":
            return httpx.Response(200, json={"status": "UP"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse([
            ("start", {"taskId": "t"}), ("content_delta", {"delta": "结论", "turn": 0}),
            ("complete", {"output": "结论"}), ("done", {"status": "completed", "output": "结论"}),
        ]))

    harness = LoopForestHarness(DeepAnalyzeConfig.from_mapping({"env": dict(LF_ENV)}, env_prefix="LF_"),
                                transport=httpx.MockTransport(handler))
    await harness.start(HarnessContext(agent_name="m", agent_id="a", host_session_id="h",
                                       system_prompt="你是团队成员。"))
    receipt = await harness.send(HarnessInput(content="问题"))
    events = [e async for e in harness.turn_events(receipt.turn_id)]
    assert [e.event for e in events if isinstance(e.event, TurnLifecycleEvent)][-1].kind is TurnEventKind.FINISHED
    assert "远端的 Loop Forest 引擎" in bodies[0]["input"]
    await harness.stop()
