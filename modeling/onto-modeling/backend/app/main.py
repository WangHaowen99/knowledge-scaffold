import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .api import deps, v1
from .routers import auth_routes
from .services.agent_sessions import SDK_AVAILABLE


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    from .services import background_jobs, backup, eval_retirement, ownership, solver_validation, startup_report
    from .services.agent_sessions import manager
    from .services.model_context_presets import write_example

    # 最先做、且在任何会话开始之前：此刻本进程没有 git 子进程，各项目 .git 下的锁都是
    # 上次停机留下的残留；元数据迁移要改各项目的真实索引，也不能和 agent 的 git 并发。
    # 启动自检同时列出各项目没提交的改动，写日志并留存（O-35）
    await asyncio.to_thread(startup_report.run, deps.workspaces)
    await asyncio.to_thread(deps.workspaces.migrate_state_tracking)
    # 单人所有权：删掉存量项目的成员表（原值记审计），日志报告待管理员指定所有者的无主项目
    await asyncio.to_thread(ownership.migrate_single_owner, deps.workspaces)
    # 发布按提交打包：旧版上传没进 git 的资料补提交一次，否则进不了包
    await asyncio.to_thread(deps.workspaces.commit_pending_uploads)
    # 评测移出（ADR-0020）：被对象引用的评测上传表迁进资料区并提交，再删掉评测状态与
    # 数据源配置（含连接密码）。幂等，迁过的项目什么都不做
    await asyncio.to_thread(eval_retirement.migrate, deps.workspaces)
    # 插件运行时启动核对（方案 D4）：镜像自带的 core 入库，逐个项目按锁文件核对、修复装配目录
    from .services.packs import service as packs_service

    await asyncio.to_thread(packs_service.reconcile_all, deps.workspaces)
    manager.start_background()  # 空闲会话回收（方案 4.1.1）
    write_example()  # 内置推荐表副本，供管理员复制成自定义表
    # 后台任务只活在进程内：重启后仍标 running 的记录改判中断，否则前端永久锁死
    background_jobs.register_startup_sweep(
        "solver_validation",
        lambda: solver_validation.sweep_interrupted(deps.workspaces.workspaces_dir),
    )
    background_jobs.run_startup_sweeps()
    # 定期备份：距上次已超过间隔时启动即补跑
    backup_task = asyncio.create_task(backup.scheduler_loop(deps.workspaces))
    # 上次删除项目时没删净的目录（文件被占用、删到一半停机）在后台清扫掉
    purge_task = asyncio.create_task(asyncio.to_thread(deps.workspaces.purge_leftovers))
    try:
        yield
    finally:
        backup_task.cancel()
        purge_task.cancel()
        await asyncio.gather(backup_task, purge_task, return_exceptions=True)
        await manager.shutdown()


app = FastAPI(title="Onto Modeling", version="0.1.0", lifespan=_lifespan)
v1.install(app)
_started_at = time.time()


@app.get("/api/health")
def health():
    """C-02：永久保留的旧健康检查，响应不变。"""
    return {"ok": True, "agent_sdk": SDK_AVAILABLE, "drawio": _drawio_available}


@app.get("/api/health/live")
def health_live():
    """C-03 存活：只要进程能响应就是 ok，不探测依赖（容器 HEALTHCHECK 用它）。"""
    return {
        "status": "ok",
        "uptime_ms": int((time.time() - _started_at) * 1000),
        "started_at": deps.iso(_started_at),
    }


# drawio 编辑器（官方 webapp 静态部署，iframe embed 协议接入；缺席时前端只读降级）
_drawio_available = auth_routes.drawio_available()
if _drawio_available:
    app.mount("/drawio", StaticFiles(directory=auth_routes.drawio_dir(), html=True), name="drawio")

# 部署形态：前端构建产物与 API 同端口服务；开发时用 vite dev + 代理，不走这里
_static_dir = Path(
    os.environ.get("ONTO_WEB_STATIC_DIR", str(config.PACKAGE_ROOT / "frontend" / "dist"))
)
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
