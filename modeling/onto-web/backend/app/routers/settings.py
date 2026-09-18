from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services import mcp_expose, mcp_settings

router = APIRouter(prefix="/api/settings", tags=["settings"])


class McpBody(BaseModel):
    servers: list[dict]


class TokenBody(BaseModel):
    name: str


@router.get("/mcp-service")
def mcp_service_status():
    return {
        "path": "/mcp-service/mcp",
        "transport": "streamable-http",
        "tools": mcp_expose.TOOL_CATALOG,
        "tokens": mcp_expose.list_tokens(),
    }


@router.post("/mcp-service/tokens")
def create_service_token(body: TokenBody, request: Request):
    user = getattr(request.state, "user", None) or {}
    plaintext, entry = mcp_expose.create_token(body.name, user.get("sub", ""))
    # 明文只在本响应出现一次，服务端仅存哈希
    return {"token": plaintext, "entry": entry}


@router.delete("/mcp-service/tokens/{token_id}")
def revoke_service_token(token_id: str):
    if not mcp_expose.revoke_token(token_id):
        return JSONResponse(status_code=404, content={"error": "令牌不存在"})
    return {"ok": True}


@router.get("/mcp")
def get_mcp():
    return {"servers": mcp_settings.load()}


@router.put("/mcp")
def put_mcp(body: McpBody, request: Request):
    try:
        return {"servers": mcp_settings.save(body.servers)}
    except mcp_settings.McpConfigError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
