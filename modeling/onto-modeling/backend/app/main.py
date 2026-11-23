# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.

import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import admin, auth_routes, hub_packages, onto_mcp, projects, sessions, settings
from .routers import flywheel, package_evaluations
from .services import auth, material_detect
from .services.agent_sessions import SDK_AVAILABLE, SessionError
from .services.auth import AuthError
from .services.conversation_store import ConversationError
from .services.workspace import WorkspaceError

_log = logging.getLogger(__name__)


async def _package_compare_cleanup_loop(service):
    import asyncio
    while True:
        try:
            await asyncio.to_thread(service.maintain)
        except Exception:
            _log.exception("知识包版本对比定时清理失败")
        await asyncio.sleep(300)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    from .auth_startup import wait_for_auth
    await wait_for_auth()

    from .routers.admin import backup_scheduler_loop
    from .services.agent_sessions import manager
    from .services.model_context_presets import write_example
    from .services.onto_mcp.package_publish import recover as recover_mcp_publishes

    manager.start_background()  # 空闲会话回收（方案 4.1.1）
    write_example()  # 内置推荐表副本，供管理员复制成自定义表
    recover_mcp_publishes(projects.workspaces)
    backup_task = asyncio.create_task(backup_scheduler_loop())  # workspace 定期备份
    from .services import knowledge_hub, package_compare
    package_compare_owner = package_compare.start()
    if package_compare_owner:
        package_compare.maintain()
    hub_task = asyncio.create_task(knowledge_hub.scheduler_loop(projects.workspaces))
    package_compare_task = (
        asyncio.create_task(_package_compare_cleanup_loop(package_compare))
        if package_compare_owner else None
    )
    # 用户离开页面后，凭开关打开时登记的委托继续推进数据飞轮
    from .services.flywheel import background as flywheel_background
    flywheel_task = asyncio.create_task(flywheel_background.scheduler_loop(projects.workspaces))
    # 上次删除项目时没删净的目录（文件被占用、删到一半停机）在后台清扫掉
    purge_task = asyncio.create_task(asyncio.to_thread(projects.workspaces.purge_leftovers))
    try:
        yield
    finally:
        hub_task.cancel()
        if package_compare_task:
            package_compare_task.cancel()
        flywheel_task.cancel()
        backup_task.cancel()
        purge_task.cancel()
        await asyncio.gather(
            backup_task,
            purge_task,
            hub_task,
            flywheel_task,
            *([package_compare_task] if package_compare_task else []),
            return_exceptions=True,
        )
        await manager.shutdown()
        package_compare.shutdown()


app = FastAPI(title="Onto Modeling", version="0.1.0", lifespan=_lifespan)
app.include_router(auth_routes.router)
app.include_router(onto_mcp.router)
app.include_router(onto_mcp.transport_router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(admin.router)
app.include_router(hub_packages.router)
app.include_router(package_evaluations.router)
app.include_router(flywheel.router)

# 白名单外的 /api 路由要求有效 access token 和当前账号状态；token 可来自
# Authorization 头或 ?token=（EventSource / 导出下载链接无法带头）
AUTH_EXEMPT_PREFIXES = ("/api/auth/", "/api/onto-mcp/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_non_api = not path.startswith("/api")
    is_exempt = (path.startswith(AUTH_EXEMPT_PREFIXES) or path == "/api/health"
                 or path == "/api/internal/evaluation/notifications")
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
    try:
        user = await asyncio.to_thread(auth.current_user, token, claims)
    except AuthError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})
    if user["password_change_required"]:
        return JSONResponse(status_code=403, content={"error": "请先修改初始密码", "code": "password_change_required"})
    request.state.user = {**claims, "role": user["role"]}
    auth.touch(claims.get("sub", ""))
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        from .services import audit

        audit.log(claims.get("sub", ""), request.method, path)
    return await call_next(request)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(material_detect.SheetSelectionError)
async def sheet_selection_error_handler(request: Request, exc: material_detect.SheetSelectionError):
    return JSONResponse(status_code=409, content={"error": str(exc), "code": "schema_selection_invalid"})


@app.exception_handler(WorkspaceError)
async def workspace_error_handler(request: Request, exc: WorkspaceError):
    content = {"error": str(exc), "code": exc.code}
    conflict = getattr(exc, "conflict", None)  # 同名冲突：候选名随错误带给前端
    if conflict:
        content["conflict"] = conflict
    return JSONResponse(status_code=exc.status, content=content)


@app.exception_handler(SessionError)
@app.exception_handler(ConversationError)
async def session_error_handler(request: Request, exc: SessionError | ConversationError):
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
