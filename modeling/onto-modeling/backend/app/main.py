import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import admin, auth_routes, contracts, projects, sessions, settings, onto_mcp
from .routers import service_tokens as service_token_routes
from .services import auth
from .services.agent_sessions import SDK_AVAILABLE, SessionError
from .services.auth import AuthError
from .services.conversation_store import ConversationError
from .services.feedback_inbox import FeedbackRejected
from .services.service_tokens import ServiceTokenError
from .services.solver_validation import ReceiptRejected
from .services.workspace import WorkspaceError

@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    from .services import background_jobs, backup, eval_retirement, solver_validation
    from .services.agent_sessions import manager
    from .services.model_context_presets import write_example

    # 最先做、且在任何会话开始之前：此刻本进程没有 git 子进程，各项目 .git 下的锁都是
    # 上次停机留下的残留；元数据迁移要改各项目的真实索引，也不能和 agent 的 git 并发
    await asyncio.to_thread(projects.workspaces.clear_git_locks)
    await asyncio.to_thread(projects.workspaces.migrate_state_tracking)
    # 单人所有权：删掉存量项目的成员表（原值记审计），日志报告待管理员指定所有者的无主项目
    await asyncio.to_thread(projects.workspaces.migrate_single_owner)
    # 发布按提交打包：旧版上传没进 git 的资料补提交一次，否则进不了包
    await asyncio.to_thread(projects.workspaces.commit_pending_uploads)
    # 评测移出（ADR-0020）：被对象引用的评测上传表迁进资料区并提交，再删掉评测状态与
    # 数据源配置（含连接密码）。幂等，迁过的项目什么都不做
    await asyncio.to_thread(eval_retirement.migrate, projects.workspaces)
    manager.start_background()  # 空闲会话回收（方案 4.1.1）
    write_example()  # 内置推荐表副本，供管理员复制成自定义表
    # 后台任务只活在进程内：重启后仍标 running 的记录改判中断，否则前端永久锁死
    background_jobs.register_startup_sweep(
        "solver_validation",
        lambda: solver_validation.sweep_interrupted(projects.workspaces.workspaces_dir),
    )
    background_jobs.run_startup_sweeps()
    # 定期备份：距上次已超过间隔时启动即补跑
    backup_task = asyncio.create_task(backup.scheduler_loop(projects.workspaces))
    # 上次删除项目时没删净的目录（文件被占用、删到一半停机）在后台清扫掉
    purge_task = asyncio.create_task(asyncio.to_thread(projects.workspaces.purge_leftovers))
    try:
        yield
    finally:
        backup_task.cancel()
        purge_task.cancel()
        await asyncio.gather(backup_task, purge_task, return_exceptions=True)
        await manager.shutdown()


app = FastAPI(title="Onto Modeling", version="0.1.0", lifespan=_lifespan)
app.include_router(auth_routes.router)
app.include_router(onto_mcp.router)
app.include_router(onto_mcp.transport_router)
app.include_router(projects.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(admin.router)
app.include_router(service_token_routes.router)
app.include_router(contracts.router)

# 白名单外的 /api 路由要求有效 access token；token 可来自
# Authorization 头或 ?token=（EventSource / 导出下载链接无法带头）。
# 三份契约接口由接口自己验服务令牌（contracts.skips_login），不走登录校验
AUTH_EXEMPT_PREFIXES = ("/api/auth/", "/api/health", "/api/onto-mcp/")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_non_api = not path.startswith("/api")
    is_exempt = path.startswith(AUTH_EXEMPT_PREFIXES) or path == "/api/health"
    token = contracts.bearer(request)
    if (
        not auth.ENABLED
        or is_non_api
        or is_exempt
        or contracts.skips_login(request.method, path, token)
    ):
        return await call_next(request)
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
    content = {"error": str(exc), "code": exc.code}
    conflict = getattr(exc, "conflict", None)  # 同名冲突：候选名随错误带给前端
    if conflict:
        content["conflict"] = conflict
    return JSONResponse(status_code=exc.status, content=content)


@app.exception_handler(ServiceTokenError)
@app.exception_handler(ReceiptRejected)
@app.exception_handler(FeedbackRejected)
async def contract_error_handler(
    request: Request, exc: ServiceTokenError | ReceiptRejected | FeedbackRejected
):
    # 契约接口按 API 契约 1.5 的错误体（code、message、details），error 字段兼容现有调用方
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": str(exc), "details": exc.details, "error": str(exc)},
    )


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
