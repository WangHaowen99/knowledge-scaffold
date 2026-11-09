"""Tiny stdio MCP server for the bridge test: echoes its argument and an env value."""

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool()
def echo(text: str) -> str:
    """Return the text with the MEMBER env value."""
    return f"{os.environ.get('MEMBER', '?')}:{text}"


if __name__ == "__main__":
    server.run("stdio")
