import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .routers import projects, sessions
from .services.agent_sessions import SDK_AVAILABLE, SessionError
from .services.workspace import WorkspaceError

app = FastAPI(title="Onto Web", version="0.1.0")
app.include_router(projects.router)
app.include_router(sessions.router)


@app.exception_handler(WorkspaceError)
async def workspace_error_handler(request: Request, exc: WorkspaceError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return JSONResponse(status_code=exc.status, content={"error": str(exc)})


@app.get("/api/health")
def health():
    return {"ok": True, "agent_sdk": SDK_AVAILABLE}


# 部署形态：前端构建产物与 API 同端口服务；开发时用 vite dev + 代理，不走这里
_static_dir = Path(
    os.environ.get("ONTO_WEB_STATIC_DIR", str(config.PACKAGE_ROOT / "frontend" / "dist"))
)
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
