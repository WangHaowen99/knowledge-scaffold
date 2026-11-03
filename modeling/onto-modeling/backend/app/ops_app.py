"""运维管理面（契约 1.1、3.25；方案 E8）：同进程第二个监听，默认 127.0.0.1:8101。

会话池、维护状态都在业务进程的内存里，所以运维面必须同进程（在业务面的 lifespan 里起）。
独立端口挡不住同容器的 agent，运维面照样校验 JWT；Prometheus 抓取地址 `/metrics` 只在这里。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from .api import errors, v1
from .governance import health, telemetry
from .governance.http import GovernanceMiddleware
from .routers import ops, ops_control


def create_app() -> FastAPI:
    app = FastAPI(title="Onto Modeling Ops", docs_url=None, redoc_url=None, openapi_url=None)
    for router in (ops.router, ops_control.router, health.router):
        app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        from .services import ops_center

        if not ops_center.prometheus_enabled():  # 运维在导出器里关了 Prometheus（O-12）
            return PlainTextResponse("not found\n", status_code=404)
        return PlainTextResponse(telemetry.prometheus_text(), media_type="text/plain; version=0.0.4")

    errors.install(app)
    app.middleware("http")(v1.auth_middleware)
    app.add_middleware(GovernanceMiddleware, plane="ops")
    app.add_middleware(v1.TraceMiddleware)
    return app


ops_app = create_app()
