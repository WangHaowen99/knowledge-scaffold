from __future__ import annotations

import contextlib
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__, db, models_client
from ..auth import AuthError
from ..config import get_settings
from ..documents import DocumentError
from ..parsing import docreader_client
from . import da_tools, management, wiki_api
from .common import error_body
from .mcp_server import AuthenticatedMcpApp, mcp

logger = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await db.migrate(settings.database_url)
    await db.connect(settings.database_url)
    async with mcp.session_manager.run():
        yield
    await models_client.aclose()
    await docreader_client.aclose()
    await db.close()


def create_app() -> FastAPI:
    app = FastAPI(title="kb-service", version=__version__, lifespan=lifespan)

    @app.exception_handler(AuthError)
    async def _auth_error(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(error_body("auth", exc.message), status_code=exc.status)

    @app.exception_handler(DocumentError)
    async def _doc_error(_: Request, exc: DocumentError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, **exc.extra), status_code=exc.status)

    @app.get("/healthz")
    async def healthz() -> dict:
        await db.pool().fetchval("SELECT 1")
        return {"status": "ok", "version": __version__}

    app.include_router(management.router)
    app.include_router(wiki_api.router)
    app.include_router(da_tools.router)
    app.mount("/mcp", AuthenticatedMcpApp(mcp.streamable_http_app()))
    return app


app = create_app()
