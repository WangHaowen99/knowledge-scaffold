"""MCP tools answer within a time budget, so a slow backend cannot break the client's session (synthetic data)."""

from __future__ import annotations

import asyncio

from kb_gateway import ops
from kb_gateway.api import mcp_server
from kb_gateway.auth import Principal

WHO = Principal("mcp", frozenset({"retrieve"}))


def _run(coro):
    async def wrapped():
        token = mcp_server._principal.set(WHO)
        try:
            return await coro
        finally:
            mcp_server._principal.reset(token)

    return asyncio.run(wrapped())


def test_a_slow_search_stops_at_the_budget_with_a_note(client, monkeypatch):
    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(ops, "search", slow)
    monkeypatch.setattr(mcp_server, "TOOL_BUDGET_S", 0.05)
    text = _run(mcp_server.search_knowledge("压力"))
    assert "没有完成" in text and "告诉用户这次没有查到" in text


def test_tool_parameters_are_still_published():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools["search_knowledge"].inputSchema["properties"]) == {"query", "kb_ids", "top_k"}
    assert tools["search_knowledge"].inputSchema["required"] == ["query"]
    assert "混合检索" in tools["search_knowledge"].description
