"""业务面与运维面共用的治理中间件（方案 E4、E6、E8，契约 1.1、1.12、1.13）。

GovernanceMiddleware 位于链路 ID 中间件之内、兼容与鉴权中间件之外：

- 从路径取项目 ID 放进公共标签，记一段请求链路与请求计数、耗时；
- 维护模式只读时拒绝写请求（`503 maintenance_read_only`），运维面与登录除外；
- 写请求在**响应之后**记审计：操作者、类型、状态码、trace id、来源 IP（鉴权关闭时操作者是 local）。

OpsOnMainGuard 放在兼容中间件之内（旧路径改写之后）：业务面默认不处理 `/api/v1/ops/*`，
`ONTO_WEB_OPS_ON_MAIN=true` 时才放行。
"""

from __future__ import annotations

import re
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from .. import config
from . import context, ops_state, telemetry

WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")
_PROJECT_RE = re.compile(r"^/api(?:/v1)?/(?:projects|onto-mcp)/([a-z0-9][a-z0-9-]*)(?:/|$)")
# 不记默认审计的写请求：MCP 传输（工具调用各自记专项审计）、健康检查
_AUDIT_SKIP = ("/api/onto-mcp/", "/api/v1/onto-mcp/", "/api/health")
# 维护模式只读时仍放行：运维面、登录与刷新令牌（契约 1.13）
_READ_ONLY_EXEMPT = ("/api/v1/ops/", "/api/v1/auth/", "/api/auth/")
OPS_PREFIX = "/api/v1/ops"


def project_of(path: str) -> str:
    found = _PROJECT_RE.match(path or "")
    return found.group(1) if found else ""


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return ""


def client_ip(scope: Scope) -> str | None:
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    client = scope.get("client")
    return client[0] if client else None


def _actor(scope: Scope) -> tuple[str, str]:
    from ..services import auth

    user = (scope.get("state") or {}).get("user")
    if isinstance(user, dict) and user.get("sub"):
        return str(user["sub"]), "user"
    bearer = _header(scope, b"authorization")
    if bearer.lower().startswith("bearer onto_svc_"):
        return "service", "service"
    if not auth.ENABLED:
        return "local", "user"
    return "anonymous", "user"


async def _json_error(send: Send, status: int, code: str, message: str, details: dict | None) -> None:
    import json

    body = json.dumps({"code": code, "message": message, "details": details,
                       "trace_id": context.get("trace_id") or None}, ensure_ascii=False).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _maintenance_details() -> dict:
    state = ops_state.maintenance_view() or {}
    return {"message": state.get("message", ""), "until": state.get("until")}


class GovernanceMiddleware:
    def __init__(self, app: ASGIApp, plane: str = "business"):
        self.app = app
        self.plane = plane

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope.get("path", ""), scope.get("method", "GET")
        status_holder = {"status": None}

        async def capture(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        started = time.monotonic()
        with context.bind(project=project_of(path)):
            with telemetry.span(f"{method} {self._route(path)}", "event", plane=self.plane, method=method) as span:
                if self._read_only_blocks(method, path):
                    await _json_error(capture, 503, "maintenance_read_only", "系统维护中，暂时只读",
                                      _maintenance_details())
                else:
                    await self.app(scope, receive, capture)
                status = status_holder["status"] or 500
                span["attrs"]["status"] = status
                if status >= 500:
                    span["status"] = "error"
            elapsed = time.monotonic() - started
            telemetry.count("onto_http_requests_total", plane=self.plane, method=method, status=f"{status // 100}xx")
            telemetry.observe("onto_http_request_seconds", elapsed, plane=self.plane)
            if method in WRITE_METHODS and path.startswith("/api") and not path.startswith(_AUDIT_SKIP):
                self._audit(scope, path, method, status)

    def _read_only_blocks(self, method: str, path: str) -> bool:
        """维护只读时挡下业务面的写请求（豁免的路径除外）。"""
        if self.plane != "business" or method not in WRITE_METHODS:
            return False
        return ops_state.maintenance_mode() == "read_only" and not path.startswith(_READ_ONLY_EXEMPT)

    @staticmethod
    def _route(path: str) -> str:
        """链路名不带项目 ID 之类的变量段，便于检索聚合。"""
        return re.sub(r"/(projects|onto-mcp)/[^/]+", r"/\1/{id}", path)[:120]

    def _audit(self, scope: Scope, path: str, method: str, status: int) -> None:
        from ..services import audit

        actor, actor_kind = _actor(scope)
        kind = "ops" if self.plane == "ops" else audit.classify_path(path)
        if actor_kind == "service":
            kind = "contract"
        audit.record(
            type=kind, action=f"http.{method.lower()}", actor=actor, actor_kind=actor_kind, method=method,
            path=path, status=status, project_id=project_of(path) or None, ip=client_ip(scope),
            trace_id=context.get("trace_id") or None,
        )


class OpsOnMainGuard:
    """业务面上的 `/api/v1/ops/*`：没开 ONTO_WEB_OPS_ON_MAIN 时 404（契约 1.1）。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and not config.OPS_ON_MAIN:
            path = scope.get("path", "")
            if path == OPS_PREFIX or path.startswith(OPS_PREFIX + "/"):
                return await _json_error(send, 404, "not_found", "接口不存在", None)
        return await self.app(scope, receive, send)


def reject_new_session(project_id: str) -> None:
    """维护或排空期间拒绝新会话（契约 1.13）：项目没有在线会话时发消息返回 503。"""
    if not ops_state.rejects_new_sessions():
        return
    from ..api.errors import ApiError
    from ..services.agent_sessions import manager

    session = manager.get(project_id)
    if session is not None and session.engine is not None:
        return  # 已有会话：跑完当前这一轮，排空时再回收
    raise ApiError("maintenance_no_new_sessions", "系统维护中，暂不接受新会话", 503, _maintenance_details())
