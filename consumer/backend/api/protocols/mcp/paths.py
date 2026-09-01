"""Shared HTTP paths for the MCP backend bridge."""

MCP_API_PREFIX = "/agentic/mcp"
MCP_INTERNAL_CHAT_ROUTE = "/internal/chat"
MCP_INTERNAL_CHAT_STREAM_ROUTE = "/internal/chat/stream"
MCP_INTERNAL_CHAT_PATH = "{}{}".format(
    MCP_API_PREFIX,
    MCP_INTERNAL_CHAT_ROUTE,
)
MCP_INTERNAL_CHAT_STREAM_PATH = "{}{}".format(
    MCP_API_PREFIX,
    MCP_INTERNAL_CHAT_STREAM_ROUTE,
)
