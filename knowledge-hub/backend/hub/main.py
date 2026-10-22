"""Knowledge package Hub HTTP API."""
from typing import Literal
from pathlib import Path
from contextlib import asynccontextmanager
import hmac
import os
import urllib.error
import urllib.request
import json

from fastapi import FastAPI, File, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from .storage import HubError, Store, digest
from .limits import UploadLimit

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
    return JSONResponse(status_code=exc.status, content={'error': str(exc)})


def actor(request: Request):
    auth = request.headers.get('authorization', '')
    return auth[7:] if auth.lower().startswith('bearer ') else ''


def authorize(request: Request, project_id: str, operation: str):
    token = actor(request)
    callback = os.environ.get('ONTO_AUTHZ_URL', '').strip()
    if not callback:
        raise HubError('Authorization service unavailable', 503)
    body = json.dumps({'project_id': project_id, 'operation': operation}).encode()
    headers = {'Content-Type': 'application/json'}
    headers['X-Hub-Service-Token'] = os.environ.get('HUB_SERVICE_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        req = urllib.request.Request(callback, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=8) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise HubError('Project access denied', exc.code) from exc
        raise HubError('Authorization service unavailable', 503) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise HubError('Authorization service unavailable', 503) from exc
    if payload.get('project_id') != project_id or not payload.get('subject'):
        raise HubError('Invalid authorization response', 503)
    if payload.get('development') and os.environ.get('HUB_DEV_ALLOW_UNAUTH') != '1':
        raise HubError('Development authentication mismatch', 503)
    if not payload.get('allowed'):
        raise HubError('Project access denied', 403)
    return payload['subject']


def internal(request: Request):
    expected = os.environ.get('HUB_SERVICE_TOKEN', '').strip()
    supplied = request.headers.get('x-hub-service-token', '')
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HubError('Internal service authentication failed', 401)


class ReserveBody(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    source_digest: str = Field(min_length=64, max_length=64)
    source_commit: str = Field(default='', max_length=128)
    publish_options: dict = Field(default_factory=dict)
    kind: Literal['release', 'evaluation_snapshot'] = 'release'


@app.exception_handler(HubError)
async def hub_error_handler(request: Request, exc: HubError):
    return error_response(exc)


@app.get('/api/health')
def health():
    import shutil
    free = shutil.disk_usage(DATA_DIR).free
    return {'ok': True, 'service': 'knowledge-hub', 'free_bytes': free}


@app.post('/api/projects/{project_id}/release-requests')
def reserve(project_id: str, body: ReserveBody, request: Request):
    internal(request)
    subject = authorize(request, project_id, 'evaluation' if body.kind == 'evaluation_snapshot' else 'publish')
    allowed_options = {'allow_unbound', 'degraded', 'unbound_objects'}
    if set(body.publish_options) - allowed_options or len(json.dumps(body.publish_options)) > 16384:
        raise HubError('Invalid publication options')
    if body.kind == 'evaluation_snapshot':
        # Snapshot creation is internal and cannot be requested by a browser.
        internal(request)
    with store.lock():
        return store.reserve(project_id, body.request_id, body.model_dump(), subject)


@app.put('/api/projects/{project_id}/release-requests/{request_id}/content')
def upload_content(project_id: str, request_id: str, request: Request,
                   file: UploadFile = File(...), package_digest: str = Header(..., alias='X-Package-Digest')):
    internal(request)
    subject = authorize(request, project_id, 'publish')
    with store.lock():
        receipt = store.request(project_id, request_id, subject)
        if receipt['kind'] == 'evaluation_snapshot':
            authorize(request, project_id, 'evaluation')
        return store.upload(project_id, request_id, subject, file.file, package_digest)


@app.post('/api/projects/{project_id}/release-requests/{request_id}/commit')
def commit(project_id: str, request_id: str, request: Request):
    internal(request)
    subject = authorize(request, project_id, 'publish')
    with store.lock():
        receipt = store.request(project_id, request_id, subject)
        authorize(request, project_id, 'evaluation' if receipt['kind'] == 'evaluation_snapshot' else 'publish')
        return store.commit(project_id, request_id, subject)


@app.get('/api/projects/{project_id}/release-requests/{request_id}')
def request_status(project_id: str, request_id: str, request: Request):
    subject = authorize(request, project_id, 'read')
    with store.lock():
        return store.request(project_id, request_id, subject)


@app.get('/api/projects/{project_id}/releases')
def releases(project_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    authorize(request, project_id, 'read')
    with store.lock():
        return store.listing(project_id, offset, limit)


@app.get('/api/projects/{project_id}/releases/{release_id}')
def release(project_id: str, release_id: str, request: Request):
    authorize(request, project_id, 'read')
    with store.lock():
        receipt, _ = store.release(project_id, release_id)
        return receipt


@app.get('/api/projects/{project_id}/releases/{release_id}/package')
def package_download(project_id: str, release_id: str, request: Request):
    authorize(request, project_id, 'read')
    with store.lock():
        receipt, path = store.release(project_id, release_id)
        return stream_file(path, f"{project_id}-{receipt['version']}.zip", receipt['package_digest'], 'application/zip')


@app.put('/api/projects/{project_id}/resources/current')
def resources(project_id: str, request: Request, generation: int = Header(..., alias='X-Resource-Generation'),
             resource_digest: str = Header(..., alias='X-Resource-Digest'), file: UploadFile = File(...)):
    internal(request)
    authorize(request, project_id, 'sync_resources')
    with store.lock():
        return store.resources(project_id, generation, file.file, resource_digest)


@app.get('/api/projects/{project_id}/resources/current')
def current_resources(project_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    authorize(request, project_id, 'read')
    with store.lock():
        root = store.project(project_id)
        store.recover(root)
        receipt = root / 'resources-current' / 'receipt.json'
        value = json.loads(receipt.read_text()) if receipt.exists() else {'generation': 0, 'files': []}
        return {**value, 'total': len(value['files']), 'files': value['files'][offset:offset + limit]}


@app.get('/api/projects/{project_id}/resources/current/{file_id}')
def resource_download(project_id: str, file_id: str, request: Request):
    authorize(request, project_id, 'read')
    with store.lock():
        project = store.project(project_id)
        store.recover(project)
        root = project / 'resources-current'
        receipt = json.loads((root / 'receipt.json').read_text()) if (root / 'receipt.json').exists() else {'files': []}
        row = next((x for x in receipt['files'] if x['id'] == file_id), None)
        if not row:
            raise HubError('Resource not found', 404)
        path = root / row['path']
        if not path.is_file():
            raise HubError('Resource not found', 404)
        if digest(path) != row['digest']:
            raise HubError('Resource integrity failure', 503)
        return stream_file(path, Path(row['path']).name, row['digest'])


@app.delete('/api/internal/projects/{project_id}/artifacts')
def delete_artifacts(project_id: str, request: Request):
    internal(request)
    authorize(request, project_id, 'cleanup')
    with store.lock():
        store.delete(project_id)
    return {'deleted': project_id}


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
