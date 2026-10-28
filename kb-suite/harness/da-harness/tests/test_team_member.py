"""The harness as an openJiuwen team member: host keys, env settings, team prompt, KB catalog, MCP."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from openjiuwen.harness_protocol import HarnessCapability, McpServerConfig, McpTransport

from deepanalyze_harness import DeepAnalyzeConfig, DeepAnalyzeHarness, DeepAnalyzeHarnessProvider
from deepanalyze_harness.bridge import McpBridge

from test_harness import TEXT_TURN, FakeDA, context, run_turn, sse

TEAM_ENV = {
    "DA_BASE_URL": "https://da.example.com/",
    "DA_TENANT_ID": "t9",
    "DA_ENGINE_SECRET": "from-env",
    "DA_KB_CATALOG_URL": "https://kbs.example.com/v1/kbs",
    "DA_KB_CATALOG_KEY_ENV": "KBS_KEY",
    "KBS_KEY": "kb-key",
}


def team_config(**extra: Any) -> dict[str, Any]:
    # The keys openjiuwen's team spawn path hands every provider package.
    return {"cwd": "/work", "env": dict(TEAM_ENV), "inherit_process_env": False, "skills": (),
            "skill_conflict": "skip", "reconnect_backoff_s": 0.01, **extra}


def member(fake: FakeDA, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> DeepAnalyzeHarness:
    for var in ("DA_ENGINE_SECRET", "DA_BASE_URL", "DA_TENANT_ID", "DA_KB_IDS", "DA_KB_CATALOG_URL",
                "DA_KB_CATALOG_KEY_ENV", "DA_MCP_BRIDGE_HOST"):
        monkeypatch.delenv(var, raising=False)
    return DeepAnalyzeHarness(DeepAnalyzeConfig.from_mapping(team_config(**extra)),
                              transport=httpx.MockTransport(fake.handler))


def test_team_spawn_keys_are_accepted_and_settings_come_from_env() -> None:
    cfg = DeepAnalyzeConfig.from_mapping(team_config(cli_path="/bin/x", system_prompt_mode="append"))
    assert (cfg.base_url, cfg.tenant_id, cfg.kb_catalog_url) == ("https://da.example.com", "t9", "https://kbs.example.com/v1/kbs")
    assert "from-env" not in repr(cfg)
    with pytest.raises(ValueError, match="unknown deepanalyze config keys: base_ur"):
        DeepAnalyzeConfig.from_mapping({"base_ur": "https://da.example.com"})


def test_provider_maps_any_model_choice_to_engine_default() -> None:
    provider = DeepAnalyzeHarnessProvider()
    assert provider.model_config(object()) == {}
    assert provider.card.supports(HarnessCapability.MCP_TOOLS)


async def test_member_uses_env_secret_team_prompt_once_and_catalog_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = [sse(TEXT_TURN), sse(TEXT_TURN)]
    harness = member(fake, monkeypatch)
    await harness.start(context(system_prompt="你是团队成员 analyst。"))
    await run_turn(harness, "第一件事")
    await run_turn(harness, "第二件事")
    runs = [(body, headers) for method, path, body, headers in fake.calls if path.endswith("/run-stream")]
    first = runs[0][0]["input"]
    assert first.startswith("你是团队成员 analyst。\n\n【运行环境】") and first.endswith("\n\n---\n\n第一件事")
    assert runs[1][0]["input"] == "第二件事"
    assert runs[0][0]["scope"] == {"kbIds": ["kb-a", "kb-b"]}
    assert runs[0][1]["x-engine-secret"] == "from-env" and runs[0][1]["x-da-tenant-id"] == "t9"
    catalog = [headers for method, path, _, headers in fake.calls if path == "/v1/kbs"]
    assert catalog and catalog[0]["authorization"] == "Bearer kb-key"
    assert len(catalog) == 1  # second turn served from the short cache
    assert "runtimeConfig" not in runs[0][0]
    await harness.stop()


async def test_http_mcp_servers_pass_through_to_runtime_config(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN)
    harness = member(fake, monkeypatch)
    server = McpServerConfig(name="team", transport=McpTransport.HTTP, url="https://mcp.example.com:9/mcp",
                             headers={"Authorization": "Bearer x"})
    await harness.start(context(mcp_servers=(server,)))
    await run_turn(harness)
    body = next(body for _, path, body, _ in fake.calls if path.endswith("/run-stream"))
    runtime = body["runtimeConfig"]
    assert runtime["version"] == 1 and runtime["tenantId"] == "t9"
    assert runtime["mcpServers"] == [{
        "serverId": "team", "serverName": "team", "clientType": "streamable-http", "serverPath": "https://mcp.example.com:9/mcp",
        "authHeaders": {"Authorization": "Bearer x"}, "callTimeoutSeconds": 300,
    }]
    await harness.stop()


async def test_stdio_mcp_without_bridge_host_runs_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = sse(TEXT_TURN)
    harness = member(fake, monkeypatch)
    server = McpServerConfig(name="team", transport=McpTransport.STDIO, command=("never-started",))
    await harness.start(context(mcp_servers=(server,)))
    await run_turn(harness)
    body = next(body for _, path, body, _ in fake.calls if path.endswith("/run-stream"))
    assert "runtimeConfig" not in body
    await harness.stop()


async def test_bridge_serves_a_stdio_server_over_streamable_http() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    script = Path(__file__).parent / "fixtures" / "echo_mcp.py"
    server = McpServerConfig(name="team", transport=McpTransport.STDIO, command=(sys.executable, str(script)),
                             env={"MEMBER": "analyst"})
    bridge = McpBridge([server], advertise_host="127.0.0.1", bind_host="127.0.0.1")
    [mounted] = await bridge.start()
    try:
        entry = mounted.runtime_entry()
        assert entry["clientType"] == "streamable-http" and entry["serverPath"].startswith("http://127.0.0.1:")
        async with streamablehttp_client(entry["serverPath"], headers=entry["authHeaders"]) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert [t.name for t in tools] == ["echo"]
                # DA validates its own context fields against the schema, then sends them along.
                assert tools[0].inputSchema["additionalProperties"] is True
                result = await session.call_tool("echo", {"text": "hi", "__da_session": "s", "__da_tenant": "t"})
                assert not result.isError and result.content[0].text == "analyst:hi"
        wrong = entry["serverPath"].replace(mounted.headers["Authorization"].split()[1], "nope")
        async with httpx.AsyncClient() as client:
            assert (await client.post(wrong, json={})).status_code == 404
    finally:
        await bridge.stop()


async def test_host_mounted_mcp_tools_are_approved_others_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    # DA's default policy asks before every MCP tool; the team host has no
    # approval channel, yet its own coordination tools must work.
    fake = FakeDA()
    fake.run_stream = sse([
        ("start", {"taskId": "task-1"}),
        ("ask_user", {"question": "允许 send_message？", "kind": "permission_confirm",
                      "toolName": "mcp_team_send_message", "toolCallId": "p1"}),
        ("ask_user", {"question": "允许写文件？", "kind": "permission_confirm",
                      "toolName": "write_file", "toolCallId": "p2"}),
        ("done", {"status": "completed", "output": "ok"}),
    ])
    harness = member(fake, monkeypatch)
    server = McpServerConfig(name="team", transport=McpTransport.HTTP, url="https://mcp.example.com:9/mcp")
    await harness.start(context(mcp_servers=(server,)))
    await run_turn(harness)
    assert fake.posted("answer") == [{"answer": "允许", "toolCallId": "p1"}, {"answer": "拒绝", "toolCallId": "p2"}]
    await harness.stop()
