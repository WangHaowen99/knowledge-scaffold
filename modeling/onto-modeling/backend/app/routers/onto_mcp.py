"""项目级 MCP：管理接口（M-01～M-04，所有者）与 HTTP/SSE 传输（M-05～M-07，MCP token）。

管理接口走契约错误体；传输层保留 JSON-RPC 信封与 MCP 大写错误码（契约 2.12）。
"""
import asyncio
import json
import math
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import config
from ..services import auth
from ..api import deps
from ..services.onto_mcp import package_publish, tokens
from ..services.onto_mcp import workspace as tools
from ..services.onto_mcp.definitions import SCOPES, TOOLS
from ..services.onto_mcp.errors import McpError
from ..services.workspace import WorkspaceError

router = APIRouter(prefix="/api/v1/projects")
# M-05～M-07：v1 路径与永久保留的旧路径，行为完全相同（契约 1.2 第 5 条）
transport_router = APIRouter(prefix="/api/v1/onto-mcp")
legacy_transport_router = APIRouter(prefix="/api/onto-mcp")
_sessions = {}
MAX_BODY = 1024 * 1024
MAX_LISTED_TOKENS = 100
TOKEN_POLICY = {"min_ttl_s": 300, "default_ttl_s": 86400, "max_ttl_s": 2592000, "supports_non_expiring": True}
# 签发、校验与这里的目录同一份口径（tokens）：版本回退 scope 两个工具集都认，发布只在改
SCOPE_LABELS = {scope: tokens.SCOPE_DESCRIPTIONS[scope] for scope in SCOPES}
TOOLSET_SCOPES = tokens.TOOLSET_SCOPES
# 客户端配置里的服务器名沿用旧版，已配好的客户端不用改名
CLIENT_SERVER_NAMES = {"read": "modeling-read", "change": "modeling-change"}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def manager():
    return deps.workspaces


def access(project_id, request):
    """项目级 MCP 管理（状态、Token 签发与吊销）只归项目所有者。"""
    deps.require_owner(project_id, request)
    return manager().get_project(project_id)


def public_base(request) -> str:
    return config.ONTO_MCP_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def endpoints(project_id: str, base: str) -> list[dict]:
    return [
        {"toolset": group, "transport": transport, "url": f"{base}/api/v1/onto-mcp/{project_id}/{group}/{suffix}"}
        for group in ("read", "change")
        for transport, suffix in (("streamable-http", "mcp"), ("sse", "sse"))
    ]


def client_configs(project_id: str, base: str, tokens_by_toolset: dict[str, str]) -> dict:
    """MCP 客户端配置 JSON（字符串）：read、change 各一份，full 两个工具集都配上。"""
    def server(group, token):
        url = f"{base}/api/v1/onto-mcp/{project_id}/{group}/mcp"
        return {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}

    servers = {group: server(group, token) for group, token in tokens_by_toolset.items() if token}

    def dump(groups):
        selected = {CLIENT_SERVER_NAMES[g]: servers[g] for g in groups if g in servers}
        return json.dumps({"mcpServers": selected}, ensure_ascii=False, indent=2) if selected else ""

    return {"read": dump(["read"]), "change": dump(["change"]), "full": dump(["read", "change"])}


def remaining_text(created: float | None, expires: float | None, now: float | None = None) -> str:
    """「30 天 · 剩 21 天」「永不过期」「已过期」。"""
    if expires is None:
        return "永不过期"
    now = time.time() if now is None else now
    if expires <= now:
        return "已过期"

    def span(seconds):
        return f"{max(1, math.ceil(seconds / 86400))} 天" if seconds >= 86400 else f"{max(1, math.ceil(seconds / 3600))} 小时"

    total = span(expires - (created if created is not None else now))
    return f"{total} · 剩 {span(expires - now)}"


def token_summary(raw: dict) -> dict:
    """McpTokenSummary（契约 7.16）。"""
    created = deps.parse_time(raw["createdAt"], "created_at") if raw.get("createdAt") else None
    expires = deps.parse_time(raw["expiresAt"], "expires_at") if raw.get("expiresAt") else None
    return {
        "id": raw["id"],
        "name": raw["name"],
        "scopes": raw["scopes"],
        "subject": raw["subjectUsername"],
        "issued_by": raw["issuedBy"],
        "created_at": deps.iso(created),
        "expires_at": deps.iso(expires),
        "last_used_at": deps.iso(raw.get("lastUsedAt")),
        "remaining_text": remaining_text(created, expires),
    }


@router.get("/{project_id}/mcp")
def status(project_id: str, request: Request):
    """M-01：对外 MCP 概况。"""
    access(project_id, request)
    base = public_base(request)
    host = (urlsplit(base).hostname or "").lower()
    return {
        "status": "running",
        "base_url": base,
        "local_only": host in LOCAL_HOSTS,
        "endpoints": endpoints(project_id, base),
        "toolsets": [
            {
                "name": group,
                "scope_requirement": "all" if group == "read" else "any",
                "accepted_scopes": list(TOOLSET_SCOPES[group]),
                "tools": [name for name, tool in TOOLS.items() if tool["toolset"] == group],
            }
            for group in ("read", "change")
        ],
        "pack_tools": [],
        "scope_catalog": [{"scope": scope, "description": label} for scope, label in SCOPE_LABELS.items()],
        "token_policy": dict(TOKEN_POLICY),
        "protocol_versions": list(tokens.VERSIONS),
        "client_config_templates": client_configs(
            project_id, base, {"read": "<READ_TOKEN>", "change": "<CHANGE_TOKEN>"}
        ),
    }


@router.get("/{project_id}/mcp/tokens")
def list_tokens(project_id: str, request: Request):
    """M-02：未吊销的 token，按签发时间倒序，不分页，上限 100。"""
    access(project_id, request)
    listed = tokens.listing(manager(), project_id)["tokens"][:MAX_LISTED_TOKENS]
    return {"items": [token_summary(item) for item in listed]}


@router.post("/{project_id}/mcp/tokens")
async def create_token(project_id: str, request: Request):
    """M-03：签发 token，明文与填好 token 的客户端配置只在这次响应里出现一次。"""
    meta = access(project_id, request)
    body = await deps.json_body(request, MAX_BODY)
    issue_body = {"name": body.get("name", ""), "scopes": body.get("scopes", [SCOPES[0]])}
    issue_body["expires_in_seconds"] = body["expires_in_s"] if "expires_in_s" in body else TOKEN_POLICY["default_ttl_s"]
    # 鉴权关闭时没有登录用户，以项目所有者的名义签发
    user = deps.user(request) or {"sub": meta.get("owner", "")}
    # 含 package.publish 时要登录：用这次的登录令牌在 Hub 登记委托（只用这一次，不落盘）
    # 签发方缺省取本服务配置的统一账号地址，与网页发布的 Hub 主体同一口径
    issuer = user.get("iss") or (auth.ISSUER if deps.user(request) else "")
    issued = tokens.issue(manager(), meta, issue_body, user, bearer(request), issuer, user.get("sub", ""))
    summary = token_summary(issued["token_summary"])
    scopes = set(summary["scopes"])
    token = issued["token"]
    by_toolset = {group: token for group, accepted in TOOLSET_SCOPES.items() if scopes & set(accepted)}
    return JSONResponse(status_code=201, content={
        "token": token,
        "summary": summary,
        "client_config": client_configs(project_id, public_base(request), by_toolset),
    })


@router.delete("/{project_id}/mcp/tokens/{token_id}", status_code=204)
def delete_token(project_id: str, token_id: str, request: Request):
    """M-04：吊销。"""
    access(project_id, request)
    try:
        tokens.revoke(manager(), project_id, token_id, bearer(request))
    except McpError as exc:
        if exc.status == 404:
            from ..api.errors import ApiError

            raise ApiError("not_found", "Token 不存在或已吊销", 404) from exc
        raise
    return Response(status_code=204)


def transport_error(exc):
    """SSE 建连前的鉴权失败：传输层沿用 MCP 大写错误码（契约 2.12）。"""
    return JSONResponse(status_code=exc.status, content={"error": str(exc), "code": exc.code})


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


def _pack_tools(project_id: str) -> list[dict]:
    """本项目生效中的包声明的只读工具（详设 5.6；ADR-0068），跟着装配走。"""
    from ..services.packs import extensions

    return extensions.mcp_tools(manager(), project_id)


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
        listed = [*TOOLS.values(), *(await asyncio.to_thread(_pack_tools, principal["project_id"]))]
        # 确认发布与确认回退会改主线或 Hub（破坏性），同一请求重放拿同一结果（幂等）
        available = [{**{k: tool[k] for k in ("name", "title", "description", "inputSchema")},
                      "annotations": {"readOnlyHint": tool.get("readOnly", tool["scope"] != "ontology.change.apply"
                                                               and tool["name"] != "revert_project_version"),
                                      **({"destructiveHint": True, "idempotentHint": True}
                                         if tool["name"] in ("publish_package_version", "revert_project_version") else {})}}
                     for tool in listed if tool["toolset"] == group and tool["scope"] in principal["scopes"]]
        return {"tools": available, **({"resultType": "complete", "ttlMs": 300000, "cacheScope": "private"}
                                      if protocol == "2026-07-28" else {})}
    if method != "tools/call":
        raise McpError("METHOD_NOT_FOUND", "不支持的 MCP 方法", 404)
    tool = TOOLS.get(params.get("name")) if isinstance(params.get("name"), str) else None
    if tool is None and isinstance(params.get("name"), str):
        tool = next((item for item in await asyncio.to_thread(_pack_tools, principal["project_id"])
                     if item["name"] == params["name"]), None)
    if tool is None or tool["toolset"] != group:
        raise McpError("MCP_TOOL_NOT_FOUND", "当前工具集不支持该工具", 404)
    if tool["scope"] not in principal["scopes"]:
        raise McpError("MCP_SCOPE_FORBIDDEN", "Token 缺少工具所需 scope", 403)
    try:
        if tool["scope"] == "package.publish":
            from ..services.onto_mcp.definitions import validate_input

            arguments = params.get("arguments", {})
            validate_input(arguments, tool["inputSchema"])
            result = await asyncio.to_thread(package_publish.execute, manager(), principal, tool["name"], arguments)
        elif tool.get("pack"):
            result = await asyncio.to_thread(tools.execute_pack_tool, manager(), principal["project_id"], tool,
                                             params.get("arguments", {}))
        else:
            result = await tools.execute_async(manager(), principal["project_id"], tool["name"],
                                               params.get("arguments", {}), principal["username"],
                                               principal["token_id"])
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
@legacy_transport_router.post("/{project_id}/{toolset}/mcp")
async def mcp(project_id: str, toolset: str, request: Request):
    return await call(project_id, toolset, request)


@transport_router.get("/{project_id}/{toolset}/sse")
@legacy_transport_router.get("/{project_id}/{toolset}/sse")
async def sse(project_id: str, toolset: str, request: Request):
    try:
        principal = await asyncio.to_thread(tokens.verify, manager(), project_id, bearer(request), toolset)
        if len(_sessions) >= 128:
            raise McpError("SESSION_LIMIT", "SSE 连接数已达上限", 429)
    except (McpError, WorkspaceError) as exc:
        return transport_error(exc)
    sid = secrets.token_urlsafe(32)
    queue = asyncio.Queue(maxsize=64)
    _sessions[sid] = {"project": project_id, "group": toolset, "token": principal["token_id"], "queue": queue}

    async def events():
        try:
            prefix = "/api/v1/onto-mcp" if request.url.path.startswith("/api/v1/") else "/api/onto-mcp"
            endpoint = f"{prefix}/{project_id}/{toolset}/sse/messages?session_id={sid}"
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
@legacy_transport_router.post("/{project_id}/{toolset}/sse/messages")
@legacy_transport_router.post("/{project_id}/{toolset}/sse/messages/")
async def sse_message(project_id: str, toolset: str, request: Request, session_id: str = ""):
    return await call(project_id, toolset, request, session_id)
