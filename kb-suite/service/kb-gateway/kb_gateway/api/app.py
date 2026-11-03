from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..auth import AuthError, KeyRing, parse_keys
from ..config import get_settings, load_backends
from ..deps import State, set_state, state
from ..registry import Registry, UpstreamError
from . import da_tools, management
from .mcp_server import AuthenticatedMcpApp, mcp

logger = logging.getLogger(__name__)


def _error(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if app.state.gateway is None:
        registry = Registry.from_configs(load_backends())
        set_state(State(registry=registry, keys=KeyRing(parse_keys(settings.keys))))
    else:
        set_state(app.state.gateway)
    registry = state().registry
    await registry.refresh()
    for backend in registry.backends:
        logger.info("backend %s (%s): %s", backend.id, backend.type, "ok" if backend.healthy else backend.last_error)
    registry.start(settings.refresh_interval_s)
    async with mcp.session_manager.run():
        yield
    await registry.close()
    set_state(None)


def create_app(gateway: State | None = None) -> FastAPI:
    """``gateway`` is injected by tests; production builds it from settings."""

    app = FastAPI(title="kb-gateway", version=__version__, lifespan=lifespan)
    app.state.gateway = gateway

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(_error("auth", exc.message), status_code=exc.status)

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
        extra = {"backend": exc.backend} if exc.backend else {}
        return JSONResponse(_error(exc.code, exc.message, **extra), status_code=exc.status)

    @app.get("/healthz")
    async def healthz() -> dict:
        registry = state().registry
        return {
            "status": "ok",
            "version": __version__,
            "backends": {b.id: "ok" if b.healthy else "unavailable" for b in registry.backends if b.visible},
        }

    app.include_router(management.router)
    app.include_router(da_tools.router)
    # a session manager runs once; each app (tests build several) needs its own
    mcp._session_manager = None  # noqa: SLF001
    app.mount("/mcp", AuthenticatedMcpApp(mcp.streamable_http_app()))
    return app
