"""Shared HTTP paths for the MCP backend bridge."""

MCP_API_PREFIX = "/agentic/mcp"
MCP_INTERNAL_CHAT_ROUTE = "/internal/chat"
MCP_INTERNAL_CHAT_PATH = "{}{}".format(
    MCP_API_PREFIX,
    MCP_INTERNAL_CHAT_ROUTE,
)
