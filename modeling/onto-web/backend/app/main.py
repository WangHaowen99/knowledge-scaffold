import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import admin, auth_routes, projects, sessions, settings
from .services import auth
from .services.agent_sessions import SDK_AVAILABLE, SessionError
from .services.auth import AuthError
from .services.workspace import WorkspaceError

# 对外 MCP 服务（streamable-http）；其会话管理器依赖自身 lifespan，
# Starlette 子应用的 lifespan 不会被自动执行，须并入主应用 lifespan
try:
    from .services.mcp_expose import build_asgi_app

    _mcp_asgi = build_asgi_app()
except Exception:  # mcp 包缺失等情况不阻塞主应用
    _mcp_asgi = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if _mcp_asgi is not None:
        async with _mcp_asgi.router.lifespan_context(_mcp_asgi):
            yield
    else:
        yield


app = FastAPI(title="Onto Web", version="0.1.0", lifespan=_lifespan)
app.include_router(auth_routes.router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(admin.router)

# 白名单外的 /api 路由要求有效 access token；token 可来自
# Authorization 头或 ?token=（EventSource / 导出下载链接无法带头）
AUTH_EXEMPT_PREFIXES = ("/api/auth/", "/api/health")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/mcp-service"):
        # 对外 MCP 服务：走独立的服务 token（与登录态无关）
        from .services import mcp_expose

        header = request.headers.get("authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        if not mcp_expose.verify_token(token):
            return JSONResponse(status_code=401, content={"error": "无效的服务令牌"})
        return await call_next(request)
    if (
        not auth.ENABLED
        or not path.startswith("/api")
        or path.startswith(AUTH_EXEMPT_PREFIXES)
        or path == "/api/health"
    ):
        return await call_next(request)
    token = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:]
    if not token:
        token = request.query_params.get("token", "")
    claims = auth.verify_access_token(token) if token else None
    if claims is None:
        return JSONResponse(status_code=401, content={"error": "未登录或登录已过期"})
    request.state.user = claims
    auth.touch(claims.get("sub", ""))
    return await call_next(request)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(WorkspaceError)
async def workspace_error_handler(request: Request, exc: WorkspaceError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.get("/api/health")
def health():
    return {"ok": True, "agent_sdk": SDK_AVAILABLE}


if _mcp_asgi is not None:
    app.mount("/mcp-service", _mcp_asgi)

# 部署形态：前端构建产物与 API 同端口服务；开发时用 vite dev + 代理，不走这里
_static_dir = Path(
    os.environ.get("ONTO_WEB_STATIC_DIR", str(config.PACKAGE_ROOT / "frontend" / "dist"))
)
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
