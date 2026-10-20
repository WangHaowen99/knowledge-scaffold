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

@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    from .routers.admin import backup_scheduler_loop
    from .services.agent_sessions import manager
    from .services.modeling_eval import sweep_interrupted
    from .services.modeling_cleanup import retry_due, scheduler_loop
    from .services.solver_validation_run import sweep_interrupted as sweep_solver_validations

    manager.start_background()  # 空闲会话回收（方案 4.1.1）
    # 评测编排只活在进程内：重启后仍标 running 的工作区改判中断，否则前端永久锁死
    sweep_interrupted()
    sweep_solver_validations()
    backup_task = asyncio.create_task(backup_scheduler_loop())  # workspace 定期备份
    modeling_cleanup_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        backup_task.cancel()
        modeling_cleanup_task.cancel()
        await asyncio.gather(backup_task, modeling_cleanup_task, return_exceptions=True)
        await manager.shutdown()


app = FastAPI(title="Onto Modeling", version="0.1.0", lifespan=_lifespan)
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
    is_non_api = not path.startswith("/api")
    is_exempt = path.startswith(AUTH_EXEMPT_PREFIXES) or path == "/api/health"
    if (
        not auth.ENABLED
        or is_non_api
        or is_exempt
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
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        from .services import audit

        audit.log(claims.get("sub", ""), request.method, path)
    return await call_next(request)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(WorkspaceError)
async def workspace_error_handler(request: Request, exc: WorkspaceError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": str(exc), "code": exc.code},
    )


@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.get("/api/health")
def health():
    return {"ok": True, "agent_sdk": SDK_AVAILABLE, "drawio": _drawio_available}


# drawio 编辑器（官方 webapp 静态部署，iframe embed 协议接入；缺席时前端只读降级）
_drawio_dir = Path(
    os.environ.get(
        "ONTO_WEB_DRAWIO_DIR", str(config.PACKAGE_ROOT / "vendor" / "drawio" / "webapp")
    )
)
_drawio_available = (_drawio_dir / "index.html").is_file()
if _drawio_available:
    app.mount("/drawio", StaticFiles(directory=_drawio_dir, html=True), name="drawio")

# 部署形态：前端构建产物与 API 同端口服务；开发时用 vite dev + 代理，不走这里
_static_dir = Path(
    os.environ.get("ONTO_WEB_STATIC_DIR", str(config.PACKAGE_ROOT / "frontend" / "dist"))
)
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
