"""API v1 汇总（方案 A14、第 3 章目标代码布局）：挂全部 v1 路由与中间件。

中间件从外到内：链路 ID → 治理（标签、链路、维护只读、响应后审计）→ 旧路径兼容（compat）
→ 运维面守卫 → 鉴权 → 配额闸门 → 路由。兼容中间件必须在鉴权之前，旧路径改写成 v1 后才按 v1 的免鉴权前缀判断
（契约 1.2）；运维面守卫在改写之后，旧的运维路径也一样 404。
"""

from __future__ import annotations

import re

from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from ..services import auth, service_tokens
from . import compat, errors

# 免鉴权前缀（契约 1.2 第 6 条）
AUTH_EXEMPT_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/client-config",
    "/api/health",
    "/api/v1/onto-mcp/",
    "/api/onto-mcp/",
    "/api/v1/pack-ui/",  # 上传包界面的静态资源：路径里的短期票据即授权，iframe 拿不到登录态（ADR-0055）
)
# 允许用 ?token= 携带令牌的接口（契约 1.3：SSE 与文件下载），其余接口的 ?token= 一律忽略
_P = r"/api/v1/projects/[^/]+"
QUERY_TOKEN_PATHS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"^{_P}/conversations/[^/]+/events$",
        rf"^{_P}/events$",
        r"^/api/v1/me/events$",
        r"^/api/v1/ops/logs/stream$",
        rf"^{_P}/files/raw$",
        rf"^{_P}/transfer-package$",
        rf"^{_P}/releases/[^/]+/download$",
        rf"^{_P}/versions/[^/]+/files$",
        r"^/api/v1/admin/packs/uploads/[^/]+/report$",
        r"^/api/v1/ops/diagnostics/[^/]+/download$",
        r"^/api/v1/ops/audit/export$",
    )
)
WRITE_METHODS = ("POST", "PUT", "DELETE", "PATCH")


def _routers():
    from ..governance import health
    from ..routers import (
        admin,
        artifacts,
        auth_routes,
        autobuild,
        blueprint,
        changesets,
        contracts,
        events,
        evidence,
        files,
        flow,
        inbox,
        knowledge,
        me,
        metamodel,
        onto_mcp,
        ops,
        ops_control,
        packs,
        project_settings,
        projects,
        publish,
        quotas,
        resources,
        scripts,
        service_tokens as service_token_routes,
        sessions,
        settings,
        solver,
        studio,
        uploads,
        versions,
    )

    return (
        auth_routes.router,
        me.router,
        metamodel.router,
        uploads.router,
        projects.router,
        project_settings.router,
        files.router,
        resources.router,
        studio.router,
        knowledge.router,
        versions.router,
        changesets.router,
        flow.router,
        blueprint.router,
        inbox.router,
        autobuild.router,
        packs.router,
        artifacts.router,
        evidence.router,
        sessions.router,
        events.router,
        solver.router,
        scripts.router,
        publish.router,
        contracts.router,
        onto_mcp.router,
        onto_mcp.transport_router,
        onto_mcp.legacy_transport_router,
        settings.router,
        admin.router,
        quotas.router,
        autobuild.admin_router,
        service_token_routes.router,
        ops.router,
        ops_control.router,
        health.router,
    )


def _query_token_allowed(path: str) -> bool:
    return any(pattern.match(path) for pattern in QUERY_TOKEN_PATHS)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def auth_middleware(request: Request, call_next):
    """白名单外的 /api 路由要求有效 access token；三份契约接口由接口自己验服务令牌。"""
    from ..routers import contracts

    path = request.url.path
    token = bearer(request)
    if (
        not auth.ENABLED
        or not path.startswith("/api")
        or path.startswith(AUTH_EXEMPT_PREFIXES)
        or contracts.skips_login(request.method, path, token)
    ):
        return await call_next(request)
    if service_tokens.looks_like_service_token(token):
        # 服务令牌只能调三份契约接口（契约 1.2 第 6 条）
        return errors.response("unauthenticated", "服务令牌不能调用该接口", 401)
    if not token and _query_token_allowed(path):
        token = request.query_params.get("token", "")
    if not token:
        return errors.response("unauthenticated", "未登录或登录已过期", 401)
    claims = auth.verify_access_token(token)
    if claims is None:
        expired = auth.check_access_token(token)[1] == "expired"
        if expired:
            return errors.response("token_expired", "登录已过期，请重新登录", 401)
        return errors.response("unauthenticated", "未登录或登录已过期", 401)
    request.state.user = claims
    auth.touch(claims.get("sub", ""))
    # 写请求的审计在响应之后由治理中间件记（带状态码，方案 E4）
    return await call_next(request)


class TraceMiddleware:
    """每个请求一个链路 ID（契约 1.5 trace_id），响应头 X-Trace-Id 带回。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        trace_id = errors.new_trace_id()
        token = errors.trace_id_var.set(trace_id)

        async def send_with_trace(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-trace-id", trace_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            errors.trace_id_var.reset(token)


def install(app: FastAPI) -> None:
    from ..governance.http import GovernanceMiddleware, OpsOnMainGuard

    from . import deps
    from ..services.packs import governance as packs_governance

    for router in _routers():
        app.include_router(router)
    # 插件运行时接到治理平面：就绪检查的装配项、O-29 包查询、O-42 版本清单、提交 trailer 的 Onto-Packs
    packs_governance.install(deps.workspaces)
    from ..governance.quota import QuotaGate

    errors.install(app)
    # 配额闸门在鉴权之内（最内层）：发消息、发命令前查并发、当日与当月 token（方案 E9）
    app.add_middleware(QuotaGate)
    app.middleware("http")(auth_middleware)
    app.add_middleware(OpsOnMainGuard)
    app.add_middleware(compat.LegacyPathMiddleware)
    app.add_middleware(GovernanceMiddleware, plane="business")
    app.add_middleware(TraceMiddleware)
