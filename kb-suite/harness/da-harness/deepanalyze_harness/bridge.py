"""Expose host MCP servers to the remote DA engine.

DA runs in another process (usually another container), so it cannot launch
the stdio MCP servers a host hands over in ``HarnessContext.mcp_servers``
(for team members: the ``openjiuwen-team-mcp`` server carrying the member's
team identity). The bridge launches them here, next to the host, and serves
each one over streamable HTTP; DA mounts the result per task through
``runtimeConfig.mcpServers``.

Every server lives under ``/<token>/<name>/mcp`` with a random token, so the
URL itself is the credential; DA also sends the token as a bearer header.
The listener binds an ephemeral port and is meant for a private network
(the compose network), never a published port.

All MCP sessions are entered and left in one background task: the SDK's
stdio client and HTTP session manager use anyio task groups, which must exit
in the task that entered them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from dataclasses import dataclass
from typing import Any, Sequence

from openjiuwen.harness_protocol import McpServerConfig, McpTransport

logger = logging.getLogger(__name__)

CALL_TIMEOUT_S = 300


@dataclass(frozen=True)
class BridgedServer:
    """One MCP server as DA should mount it."""

    name: str
    url: str
    headers: dict[str, str]

    def runtime_entry(self) -> dict[str, Any]:
        """DA ``runtimeConfig.mcpServers`` item (core key names)."""
        return {
            "serverId": self.name,
            "serverName": self.name,
            "clientType": "streamable-http",
            "serverPath": self.url,
            "authHeaders": dict(self.headers),
            "callTimeoutSeconds": CALL_TIMEOUT_S,
        }


def passthrough(server: McpServerConfig) -> BridgedServer:
    """An HTTP server DA can reach directly."""
    return BridgedServer(name=server.name, url=str(server.url), headers=dict(server.headers))


class McpBridge:
    """Serve the host's stdio MCP servers over streamable HTTP for DA."""

    def __init__(self, servers: Sequence[McpServerConfig], *, advertise_host: str, bind_host: str = "0.0.0.0") -> None:
        self._servers = [s for s in servers if s.transport is McpTransport.STDIO]
        self._advertise_host = advertise_host
        self._bind_host = bind_host
        self._token = secrets.token_urlsafe(24)
        self._ready: asyncio.Future[list[BridgedServer]] | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self, timeout_s: float = 60.0) -> list[BridgedServer]:
        """Launch the stdio servers and the HTTP listener; return what DA should mount."""
        if not self._servers:
            return []
        self._ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name="deepanalyze-mcp-bridge")
        try:
            return await asyncio.wait_for(asyncio.shield(self._ready), timeout_s)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        except Exception:  # noqa: BLE001 - shutdown must not fail the harness
            logger.warning("deepanalyze MCP bridge stopped with an error", exc_info=True)

    async def _run(self) -> None:
        import uvicorn
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.applications import Starlette
        from starlette.routing import Mount

        if self._ready is None:
            raise RuntimeError("McpBridge._run() called before start()")
        try:
            async with contextlib.AsyncExitStack() as stack:
                routes = []
                managers = []
                for server in self._servers:
                    read, write = await stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=server.command[0], args=list(server.command[1:]), env=dict(server.env) or None,
                    )))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    manager = StreamableHTTPSessionManager(
                        app=_forwarding_server(server.name, session),
                        stateless=True,
                        json_response=True,
                        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
                    )
                    managers.append(manager)
                    routes.append(Mount(f"/{self._token}/{server.name}/mcp", app=_no_sse_stream(manager.handle_request)))
                for manager in managers:
                    await stack.enter_async_context(manager.run())

                sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self._bind_host, 0))
                port = sock.getsockname()[1]
                http = uvicorn.Server(uvicorn.Config(
                    Starlette(routes=routes), log_level="warning", lifespan="off", access_log=False,
                ))
                serving = asyncio.create_task(http.serve(sockets=[sock]))
                while not http.started and not serving.done():
                    await asyncio.sleep(0.05)
                if serving.done():
                    serving.result()
                bridged = [
                    BridgedServer(
                        name=server.name,
                        # Plain HTTP on the compose network only; the random path token is the credential.
                        url=f"http://{self._advertise_host}:{port}/{self._token}/{server.name}/mcp/",  # NOSONAR(S5332)
                        headers={"Authorization": f"Bearer {self._token}"},
                    )
                    for server in self._servers
                ]
                logger.info("deepanalyze MCP bridge serving %s on port %d",
                            ", ".join(s.name for s in self._servers), port)
                self._ready.set_result(bridged)
                await self._stop.wait()
                http.should_exit = True
                await serving
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            if not isinstance(exc, Exception):
                raise
            logger.warning("deepanalyze MCP bridge failed", exc_info=True)


# DA adds its own context fields to every tool call's arguments and validates
# them against the tool's input schema before calling, so a schema that rejects
# extra fields fails inside DA. The bridge accepts them and drops them again.
DA_ARGUMENT_PREFIX = "__da_"


def _accepting_da_fields(tool: Any) -> Any:
    schema = dict(tool.inputSchema or {"type": "object"})
    schema["additionalProperties"] = True
    return tool.model_copy(update={"inputSchema": schema})


def _without_da_fields(arguments: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (arguments or {}).items() if not k.startswith(DA_ARGUMENT_PREFIX)}


def _no_sse_stream(handler: Any) -> Any:
    """Answer GET with 405: a stateless forwarder pushes nothing, and an idle
    notification stream per client would only be cut off at shutdown."""

    async def app(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "GET":
            await send({"type": "http.response.start", "status": 405, "headers": [(b"allow", b"POST, DELETE")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await handler(scope, receive, send)

    return app


def _forwarding_server(name: str, session: Any) -> Any:
    """A low-level MCP server whose tools are the remote session's tools."""
    from mcp import types
    from mcp.server.lowlevel import Server

    server: Server = Server(f"deepanalyze-bridge-{name}")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [_accepting_da_fields(tool) for tool in (await session.list_tools()).tools]

    @server.call_tool(validate_input=False)
    async def _call_tool(tool: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await session.call_tool(tool, _without_da_fields(arguments))

    return server


__all__ = ["BridgedServer", "McpBridge", "passthrough"]
