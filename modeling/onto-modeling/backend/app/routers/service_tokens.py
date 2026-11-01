"""服务令牌管理（X-06，管理员）：签发、列出、吊销。和项目的 MCP token 分开签发、分开存储（ADR-0020）。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..services import service_tokens
from .admin import _require_admin

router = APIRouter(prefix="/api/v1/admin/service-tokens", tags=["admin"])


def _actor(request: Request) -> str:
    return (getattr(request.state, "user", None) or {}).get("sub", "")


@router.get("")
def list_service_tokens(request: Request):
    _require_admin(request)
    return {"items": service_tokens.listing()}


@router.post("")
async def issue_service_token(request: Request):
    """签发：明文只在这次响应里出现一次，之后只能吊销重发。"""
    _require_admin(request)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise service_tokens.ServiceTokenError("请求体必须是 JSON 对象", 400, service_tokens.CODE_INVALID)
    return JSONResponse(status_code=201, content=service_tokens.issue(body, _actor(request)))


@router.delete("/{token_id}", status_code=204)
def revoke_service_token(token_id: str, request: Request):
    _require_admin(request)
    service_tokens.revoke(token_id, _actor(request))
    return Response(status_code=204)
