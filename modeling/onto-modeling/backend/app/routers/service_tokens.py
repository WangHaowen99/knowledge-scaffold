"""服务令牌管理（X-06，管理员）：签发、列出、吊销。和项目的 MCP token 分开签发、分开存储（ADR-0020）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..api import deps
from ..services import service_tokens

router = APIRouter(prefix="/api/v1/admin/service-tokens", tags=["admin"])


@router.get("")
def list_service_tokens(request: Request):
    deps.require_role(request, "admin")
    return {"items": service_tokens.listing()}


@router.post("")
async def issue_service_token(request: Request):
    """签发：明文只在这次响应里出现一次，之后只能吊销重发。"""
    deps.require_role(request, "admin")
    body = await deps.json_body(request)
    return JSONResponse(status_code=201, content=service_tokens.issue(body, deps.actor(request)))


@router.delete("/{token_id}", status_code=204)
def revoke_service_token(token_id: str, request: Request):
    deps.require_role(request, "admin")
    service_tokens.revoke(token_id, deps.actor(request))
    return Response(status_code=204)
