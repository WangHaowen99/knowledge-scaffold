"""服务令牌接口（API 契约 3.20、3.21；ADR-0020，包出与回执部分由 ADR-0091 取代）。

外部服务持服务令牌调用，不经登录。正式版本由知识包 Hub 分发、求解验证在评测服务里做（ADR-0091），
X-01 跨项目发布列表、求解验证候选包、X-04 验证回执都已退役；这里只剩反馈入 X-05。
PB-05 发布记录、PB-06 包清单、PB-07 下载也接受服务令牌，在 routers/publish.py。
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..services import audit, feedback_inbox, service_tokens
from ..services.service_tokens import SCOPE_FEEDBACK_WRITE
from ..services.workspace import WorkspaceError
from ..api import deps

router = APIRouter(prefix="/api/v1", tags=["contracts"])

_PROJECT = r"/api/v1/projects/[^/]+"
# 只收服务令牌的接口：没带服务令牌也放行到接口，由接口回 401
_SERVICE_ONLY = (
    ("POST", re.compile(rf"^{_PROJECT}/feedback$")),
)
# 所有者或服务令牌都能读：带服务令牌时不走登录校验，带登录 token 时照常校验
_OWNER_OR_SERVICE = (
    ("GET", re.compile(rf"^{_PROJECT}/releases/[^/]+(?:/manifest|/download)?$")),
)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def skips_login(method: str, path: str, token: str) -> bool:
    """鉴权中间件用：契约接口由接口自己验服务令牌，不做登录校验（API 契约 1.2 第 6 条）。"""
    if any(method == verb and pattern.match(path) for verb, pattern in _SERVICE_ONLY):
        return True
    return service_tokens.looks_like_service_token(token) and any(
        method == verb and pattern.match(path) for verb, pattern in _OWNER_OR_SERVICE
    )


def _service(request: Request, scope: str, project_id: str | None = None) -> dict:
    token = service_tokens.verify(bearer(request), scope, project_id)
    request.state.service_token = token
    return token


def _actor(token: dict) -> str:
    return f"service:{token['name']}"


async def _json_body(request: Request, limit: int) -> object:
    raw = await request.body()
    if len(raw) > limit:
        raise WorkspaceError(f"请求体不超过 {limit // 1024} KB", 413, "payload_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkspaceError("请求体必须是 UTF-8 JSON", 400, "invalid_argument") from None


# ---- 反馈入 ----


@router.post("/projects/{project_id}/feedback")
async def post_feedback(project_id: str, request: Request):
    """X-05：接收一批外部反馈，落到工作区 feedback/ 并单独提交，供反馈演进读取。"""
    token = _service(request, SCOPE_FEEDBACK_WRITE, project_id)
    deps.workspaces.project_root(project_id)
    payload = await _json_body(request, feedback_inbox.MAX_BODY_BYTES)
    result, created = feedback_inbox.receive(deps.workspaces, project_id, payload, _actor(token))
    if created:
        audit.log(_actor(token), "POST", request.url.path, {"contract.feedback": result["feedback_id"]})
    return JSONResponse(status_code=201 if created else 200, content=result)
