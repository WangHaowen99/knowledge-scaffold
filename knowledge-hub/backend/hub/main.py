"""Knowledge package Hub HTTP API."""
from pathlib import Path
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from .storage import HubError, Store
from .limits import UploadLimit
from .package_contract import PackageError

DATA_DIR = Path(os.environ.get('HUB_DATA_DIR', '/data/knowledge-hub'))
store = Store(DATA_DIR,
             pending_ttl=int(os.environ.get('HUB_PENDING_TTL_SECONDS', 7 * 86400)),
             max_members=int(os.environ.get('HUB_MAX_MEMBERS', 10000)),
             max_file=int(os.environ.get('HUB_MAX_FILE_BYTES', 256 * 1024**2)),
             max_bytes=int(os.environ.get('HUB_MAX_UPLOAD_BYTES', 256 * 1024**2)),
             max_expanded=int(os.environ.get('HUB_MAX_EXPANDED_BYTES', 1024 * 1024**2)))


@asynccontextmanager
async def lifespan(app):
    with store.lock():
        store.recover_all()
    yield


app = FastAPI(title='Knowledge Package Hub', version='0.1.0', lifespan=lifespan)
app.add_middleware(UploadLimit, max_bytes=store.max_bytes + 1024**2)


def error_response(exc):
    if isinstance(exc, PackageError):
        return JSONResponse(status_code=exc.status, content=exc.payload())
    return JSONResponse(status_code=exc.status, content={'error': str(exc)})


@app.exception_handler(HubError)
async def hub_error_handler(request: Request, exc: HubError):
    return error_response(exc)


@app.get('/api/health')
def health():
    import shutil
    free = shutil.disk_usage(DATA_DIR).free
    return {'ok': True, 'service': 'knowledge-hub', 'free_bytes': free}


def stream_file(path, filename, digest, media_type='application/octet-stream'):
    from urllib.parse import quote
    handle = path.open('rb')
    def chunks():
        try:
            while chunk := handle.read(1024 * 1024):
                yield chunk
        finally:
            handle.close()
    return StreamingResponse(chunks(), media_type=media_type,
                             headers={'X-Package-Digest': digest,
                                      'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)})


from .package_routes import create_router
from fastapi.exceptions import RequestValidationError

app.include_router(create_router(lambda: store, stream_file))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith(('/api/packages', '/api/package-publish-requests')):
        return JSONResponse(status_code=422, content=PackageError(
            'invalid_request', 'Invalid package request', 422).payload())
    from fastapi.exception_handlers import request_validation_exception_handler
    return await request_validation_exception_handler(request, exc)
