import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .api import deps, v1
from .routers import auth_routes


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio

    from .governance import startup as governance_startup
    from .governance import startup_recovery
    from .services import background_jobs, backup, solver_validation
    from .services.agent_sessions import manager
    from .services.model_context_presets import write_example

    # 治理平面（方案 E5、E6、E10）：结构化日志、配置校验、会话池覆盖、密钥目录与告警，最先做
    await asyncio.to_thread(governance_startup.before_recovery)
    # 后台任务只活在进程内：重启后仍标 running 的记录改判中断（启动恢复的「中断的执行」一步统一跑）
    background_jobs.register_startup_sweep(
        "solver_validation",
        lambda: solver_validation.sweep_interrupted(deps.workspaces.workspaces_dir),
    )
    # 启动恢复（详设 8.3、方案 B8）：任何会话开始之前，按登记顺序跑完各模块的恢复步骤——清残留锁、
    # 补提交写入意图、存量迁移、核对装配目录、修对话历史、把没结束的执行改判中断——并留存报告（O-35）
    await asyncio.to_thread(startup_recovery.run, deps.workspaces, include_background=False)
    manager.start_background()  # 空闲会话回收（方案 4.1.1）
    write_example()  # 内置推荐表副本，供管理员复制成自定义表
    # 定期备份：距上次已超过间隔时启动即补跑
    backup_task = asyncio.create_task(backup.scheduler_loop(deps.workspaces))
    # 耗时、不挡就绪的恢复步骤（清扫删到一半的目录、探测外部依赖）放后台，做完补进报告
    recovery_task = asyncio.create_task(startup_recovery.run_background(deps.workspaces))
    # 启动恢复做完才就绪（C-04）；运维管理面在同一事件循环起第二个监听（E8）
    ops_listener = await governance_startup.after_recovery()
    try:
        yield
    finally:
        await governance_startup.shutdown(ops_listener)
        backup_task.cancel()
        recovery_task.cancel()
        await asyncio.gather(backup_task, recovery_task, return_exceptions=True)
        await manager.shutdown()
        governance_startup.stopped()


app = FastAPI(title="Onto Modeling", version="0.1.0", lifespan=_lifespan)
# 健康检查（/api/health、/live、/ready）由 governance/health 的路由提供，两个监听都挂
v1.install(app)


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
