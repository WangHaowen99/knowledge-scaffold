"""Onto-local MCP management and HTTP/SSE transport."""
import asyncio
import json
import secrets
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from .. import config
from ..services.onto_mcp import tokens
from ..services.onto_mcp import workspace as tools
from ..services.onto_mcp.definitions import TOOLS
from ..services.onto_mcp.errors import McpError
from ..services.workspace import WorkspaceError

router = APIRouter(prefix="/api/projects")
transport_router = APIRouter(prefix="/api/onto-mcp")
_sessions = {}
MAX_BODY = 1024 * 1024


def manager():
    from .projects import workspaces
    return workspaces


def access(project_id, request, write=False):
    from .projects import ensure_access
    ensure_access(project_id, request, write=write)
    return manager().get_project(project_id)


def management_error(exc):
    if isinstance(exc, (McpError, WorkspaceError)):
        return JSONResponse(status_code=exc.status, content={"error": str(exc), "code": exc.code})
    raise exc


@router.get("/{project_id}/modeling-mcp")
def status(project_id: str, request: Request):
    try:
        access(project_id, request)
        base = config.ONTO_MCP_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        return {"runtime": tokens.status(project_id, base), "workspace": {"source": "onto"}}
    except (McpError, WorkspaceError) as exc:
        return management_error(exc)


@router.get("/{project_id}/modeling-mcp/tokens")
def list_tokens(project_id: str, request: Request):
    try:
        access(project_id, request, write=True)
        return tokens.listing(manager(), project_id)
    except (McpError, WorkspaceError) as exc:
        return management_error(exc)


@router.post("/{project_id}/modeling-mcp/tokens")
async def create_token(project_id: str, request: Request):
    try:
        meta = access(project_id, request, write=True)
        body = await request.json()
        if not isinstance(body, dict):
            raise McpError("INVALID_INPUT", "请求必须是对象")
        user = getattr(request.state, "user", None) or {"sub": meta.get("owner", "")}
        return JSONResponse(status_code=201, content=tokens.issue(manager(), meta, body, user))
    except (McpError, WorkspaceError) as exc:
        return management_error(exc)


@router.delete("/{project_id}/modeling-mcp/tokens/{token_id}")
def delete_token(project_id: str, token_id: str, request: Request):
    try:
        access(project_id, request, write=True)
        return tokens.revoke(manager(), project_id, token_id)
    except (McpError, WorkspaceError) as exc:
        return management_error(exc)


def bearer(request):
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def rpc_error(identity, code, message, data=None):
    return {"jsonrpc": "2.0", "id": identity, "error": {"code": code, "message": message, "data": data or {}}}


async def parse_rpc(request):
    chunks = bytearray()
    async for part in request.stream():
        chunks.extend(part)
        if len(chunks) > MAX_BODY:
            raise McpError("REQUEST_TOO_LARGE", "MCP 请求超过大小限制", 413)
    try:
        rpc = json.loads(chunks)
    except ValueError:
        raise McpError("PARSE_ERROR", "JSON 无效") from None
    if not isinstance(rpc, dict):
        raise McpError("INVALID_REQUEST", "JSON-RPC 请求格式无效")
    if (rpc.get("jsonrpc") != "2.0" or not isinstance(rpc.get("method"), str)
            or not isinstance(rpc.get("params", {}), dict)):
        raise McpError("INVALID_REQUEST", "JSON-RPC 请求格式无效")
    if "id" in rpc and isinstance(rpc["id"], (dict, list, bool)):
        raise McpError("INVALID_REQUEST", "JSON-RPC 请求格式无效")
    return rpc


def version(rpc, request):
    body = rpc.get("params", {}).get("protocolVersion") if rpc["method"] == "initialize" else None
    header = request.headers.get("mcp-protocol-version")
    if header and header not in tokens.VERSIONS:
        raise McpError("UNSUPPORTED_PROTOCOL", "不支持的 MCP 协议版本")
    if header and body and header != body:
        raise McpError("PROTOCOL_MISMATCH", "请求头与协商版本不一致")
    return (body if body in tokens.VERSIONS else header) or "2025-11-25"


async def dispatch(rpc, group, principal, protocol):
    method, params = rpc["method"], rpc.get("params", {})
    if method.startswith("notifications/"):
        return None
    if method == "ping":
        return {}
    if method == "initialize":
        return {"protocolVersion": protocol, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": tokens.SERVER}
    if method == "server/discover":
        return {"resultType": "complete", "supportedVersions": tokens.VERSIONS,
                "capabilities": {"tools": {"listChanged": False}}, "ttlMs": 300000, "cacheScope": "private",
                "_meta": {"io.modelcontextprotocol/serverInfo": tokens.SERVER}}
    if method == "tools/list":
        available = [{**{k: tool[k] for k in ("name", "title", "description", "inputSchema")},
                      "annotations": {"readOnlyHint": tool["scope"] not in {
                          "ontology.change.apply", "project.version.revert"
                      }}}
                     for tool in TOOLS.values() if tool["toolset"] == group and tool["scope"] in principal["scopes"]]
        return {"tools": available, **({"resultType": "complete", "ttlMs": 300000, "cacheScope": "private"}
                                      if protocol == "2026-07-28" else {})}
    if method != "tools/call":
        raise McpError("METHOD_NOT_FOUND", "不支持的 MCP 方法", 404)
    tool = TOOLS.get(params.get("name")) if isinstance(params.get("name"), str) else None
    if tool is None or tool["toolset"] != group:
        raise McpError("MCP_TOOL_NOT_FOUND", "当前工具集不支持该工具", 404)
    if tool["scope"] not in principal["scopes"]:
        raise McpError("MCP_SCOPE_FORBIDDEN", "Token 缺少工具所需 scope", 403)
    try:
        result = await tools.execute_async(manager(), principal["project_id"], tool["name"],
                                           params.get("arguments", {}), principal["username"], principal["token_id"])
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result, "isError": False}
    except (McpError, WorkspaceError) as exc:
        data = exc.public() if isinstance(exc, McpError) else {"code": exc.code, "message": str(exc)}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "structuredContent": data, "isError": True}


async def call(project_id, toolset, request, session_id=None):
    identity = None
    try:
        principal = await asyncio.to_thread(tokens.verify, manager(), project_id, bearer(request), toolset)
        rpc = await parse_rpc(request)
        identity = rpc.get("id")
        protocol = version(rpc, request)
        session = None
        if session_id is not None:
            session = _sessions.get(session_id)
            if not session or session["project"] != project_id or session["group"] != toolset:
                raise McpError("SESSION_NOT_FOUND", "SSE 会话不存在", 404)
            if session["token"] != principal["token_id"]:
                raise McpError("SESSION_PRINCIPAL_MISMATCH", "SSE 会话与 Token 不匹配", 403)
        result = await dispatch(rpc, toolset, principal, protocol)
        if result is None or "id" not in rpc:
            return Response(status_code=202)
        envelope = {"jsonrpc": "2.0", "id": identity, "result": result}
        if session:
            try:
                session["queue"].put_nowait(envelope)
            except asyncio.QueueFull:
                raise McpError("SESSION_BUSY", "SSE 消息积压，请重连", 429) from None
            return Response(status_code=202)
        return JSONResponse(envelope, headers={"MCP-Protocol-Version": protocol})
    except (McpError, WorkspaceError) as exc:
        status = exc.status
        code = -32001 if status in (401, 403) else -32601 if exc.code == "METHOD_NOT_FOUND" else -32700 if exc.code == "PARSE_ERROR" else -32602
        return JSONResponse(rpc_error(identity, code, str(exc), {"code": exc.code}), status_code=status)
    except Exception:
        return JSONResponse(rpc_error(identity, -32603, "MCP 服务内部错误", {"code": "MCP_INTERNAL_ERROR"}), status_code=500)


@transport_router.post("/{project_id}/{toolset}/mcp")
async def mcp(project_id: str, toolset: str, request: Request):
    return await call(project_id, toolset, request)


@transport_router.get("/{project_id}/{toolset}/sse")
async def sse(project_id: str, toolset: str, request: Request):
    try:
        principal = await asyncio.to_thread(tokens.verify, manager(), project_id, bearer(request), toolset)
        if len(_sessions) >= 128:
            raise McpError("SESSION_LIMIT", "SSE 连接数已达上限", 429)
    except (McpError, WorkspaceError) as exc:
        return management_error(exc)
    sid = secrets.token_urlsafe(32)
    queue = asyncio.Queue(maxsize=64)
    _sessions[sid] = {"project": project_id, "group": toolset, "token": principal["token_id"], "queue": queue}

    async def events():
        try:
            endpoint = f"/api/onto-mcp/{project_id}/{toolset}/sse/messages?session_id={sid}"
            yield "event: endpoint\ndata: " + endpoint + "\n\n"
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield "event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n"
                except asyncio.TimeoutError:
                    await asyncio.to_thread(tokens.verify, manager(), project_id, bearer(request), toolset)
                    yield ": keep-alive\n\n"
        except (McpError, WorkspaceError):
            return
        finally:
            _sessions.pop(sid, None)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@transport_router.post("/{project_id}/{toolset}/sse/messages")
@transport_router.post("/{project_id}/{toolset}/sse/messages/")
async def sse_message(project_id: str, toolset: str, request: Request, session_id: str = ""):
    return await call(project_id, toolset, request, session_id)
