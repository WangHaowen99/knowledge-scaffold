"""Hub routes; authentication is independent of project/workspace services."""
from contextlib import contextmanager

from fastapi import APIRouter, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from .package_contract import Kind, PackageError, PublishBody
from .package_store import PackageStore, public
from .storage import HubError
from .user_auth import authenticate


def create_router(get_store, stream_file):
    router = APIRouter(tags=['packages'])

    @contextmanager
    def operation(request):
        identity = authenticate(request)
        store = get_store()
        try:
            with store.lock():
                yield PackageStore(store), identity
        except PackageError:
            raise
        except HubError as exc:
            raise PackageError('package_error', 'Package operation failed', exc.status) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise PackageError('storage_unavailable', 'Package storage unavailable', 503, 'storage') from exc

    @router.post('/api/package-publish-requests')
    def reserve(body: PublishBody, request: Request):
        with operation(request) as (store, actor):
            result, created = store.reserve(body.model_dump(), actor)
            return JSONResponse(result, status_code=201 if created else 200)

    @router.put('/api/package-publish-requests/{request_id}/content')
    def upload(request_id: str, request: Request, file: UploadFile = File(...),
               package_digest: str = Header(..., alias='X-Package-Digest')):
        with operation(request) as (store, actor):
            return store.upload(request_id, actor, file.file, package_digest)

    @router.post('/api/package-publish-requests/{request_id}/commit')
    def commit(request_id: str, request: Request):
        with operation(request) as (store, actor):
            return store.commit(request_id, actor)

    @router.get('/api/package-publish-requests/{request_id}')
    def status(request_id: str, request: Request):
        with operation(request) as (store, actor):
            return public(store.get_request(request_id, actor))

    @router.get('/api/packages')
    def packages(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with operation(request) as (store, actor):
            return store.packages(actor.subject, offset, limit)

    @router.get('/api/packages/{package_id}/releases')
    def releases(package_id: str, request: Request, kind: Kind = 'release',
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with operation(request) as (store, actor):
            return store.listing(package_id, actor.subject, kind, offset, limit)

    @router.get('/api/packages/{package_id}/releases/{release_id}')
    def release(package_id: str, release_id: str, request: Request):
        with operation(request) as (store, actor):
            return store.release(package_id, release_id, actor.subject)[0]

    @router.get('/api/packages/{package_id}/releases/{release_id}/package')
    def download(package_id: str, release_id: str, request: Request):
        with operation(request) as (store, actor):
            receipt, path = store.release(package_id, release_id, actor.subject)
            response = stream_file(path, f'{package_id}-{receipt["version"]}.zip', receipt['package_digest'], 'application/zip')
            response.headers['Content-Length'] = str(receipt['size'])
            return response

    return router
