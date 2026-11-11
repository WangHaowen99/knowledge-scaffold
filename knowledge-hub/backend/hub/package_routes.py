"""Hub routes; authentication is independent of project/workspace services."""
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, File, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from .package_contract import DelegationBody, Kind, PackageError, PublishBody, GrantBody
from .package_store import PackageStore, public
from .storage import HubError, read_json, digest
from .user_auth import authenticate, require_direct, validate_targets


def create_router(get_store, stream_file):
    router = APIRouter(tags=['packages'])

    @contextmanager
    def operation(request):
        # Online user validation may involve the account service; keep that network wait
        # outside the Hub's storage lock. Delegation nonce consumption remains serialized.
        identity = authenticate(request)
        store = get_store()
        try:
            with store.lock():
                PackageStore(store).authorize_delegation(identity)
                store.recover_all()
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

    @router.post('/api/package-delegations')
    def create_delegation(body: DelegationBody, request: Request):
        with operation(request) as (store, actor):
            return store.register_delegation(body.model_dump(), actor)

    @router.get('/api/package-delegations/{delegation_id}')
    def get_delegation(delegation_id: str, request: Request):
        with operation(request) as (store, actor):
            return store.delegation(delegation_id, actor)

    @router.delete('/api/package-delegations/{delegation_id}')
    def delete_delegation(delegation_id: str, request: Request):
        with operation(request) as (store, actor):
            return store.revoke_delegation(delegation_id, actor)

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
            require_direct(actor)
            return store.packages(actor.subject, offset, limit)

    @router.get('/api/packages/{ontology_id}/releases')
    def releases(ontology_id: str, request: Request, kind: Kind = 'release',
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with operation(request) as (store, actor):
            require_direct(actor)
            return store.listing(ontology_id, actor.subject, kind, offset, limit)

    @router.get('/api/packages/{ontology_id}/releases/{release_id}')
    def release(ontology_id: str, release_id: str, request: Request):
        with operation(request) as (store, actor):
            require_direct(actor)
            return store.release(ontology_id, release_id, actor.subject)[0]

    @router.get('/api/packages/{ontology_id}/releases/{release_id}/package')
    def download(ontology_id: str, release_id: str, request: Request):
        with operation(request) as (store, actor):
            require_direct(actor)
            receipt, path = store.release(ontology_id, release_id, actor.subject)
            response = stream_file(path, receipt['filename'], receipt['package_digest'], 'application/zip')
            response.headers['Content-Length'] = str(receipt['size'])
            return response

    @router.get('/api/packages/{ontology_id}')
    def summary(ontology_id: str, request: Request):
        with operation(request) as (store, actor):
            require_direct(actor)
            return store.summary(ontology_id, actor.subject)

    @router.delete('/api/packages/{ontology_id}')
    def delete(ontology_id: str, request: Request):
        with operation(request) as (store, actor):
            require_direct(actor)
            return store.delete(ontology_id, actor.subject)

    @router.get('/api/packages/{ontology_id}/authorized-users')
    def users(ontology_id: str, request: Request,
              offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with operation(request) as (store, actor):
            require_direct(actor)
            return store.authorized_users(ontology_id, actor.subject, offset, limit)

    @router.put('/api/packages/{ontology_id}/authorized-users')
    def grant(ontology_id: str, body: GrantBody, request: Request):
        with operation(request) as (store, actor):
            require_direct(actor)
            store.owned(ontology_id, actor.subject)
            targets = validate_targets([user.model_dump() for user in body.users])
            return store.grant(ontology_id, actor.subject, targets)

    @router.delete('/api/packages/{ontology_id}/authorized-users/{subject_id}')
    def revoke(ontology_id: str, subject_id: str, request: Request):
        with operation(request) as (store, actor):
            return store.revoke(ontology_id, actor.subject, subject_id)

    def resource_access(store, actor, ontology_id, write=False):
        store.owned(ontology_id, actor.subject)
        if write and actor.service != 'modeling':
            raise PackageError('service_forbidden', 'Service capability denied', 403, 'authorization')
        root = store.root_of(ontology_id)
        store.io.recover(root)
        return root / 'resources-current'

    @router.put('/api/packages/{ontology_id}/resources/current')
    def resources(ontology_id: str, request: Request, file: UploadFile = File(...),
                  generation: int = Header(..., alias='X-Resource-Generation', ge=1),
                  resource_digest: str = Header(..., alias='X-Resource-Digest', pattern=r'^[a-f0-9]{64}$')):
        with operation(request) as (store, actor):
            resource_access(store, actor, ontology_id, write=True)
            return store.io.resources(ontology_id, generation, file.file, resource_digest)

    @router.delete('/api/packages/{ontology_id}/resources/current')
    def clear_resources(ontology_id: str, request: Request,
                        generation: int = Header(..., alias='X-Resource-Generation', ge=1)):
        with operation(request) as (store, actor):
            resource_access(store, actor, ontology_id, write=True)
            return store.io.clear_resources(ontology_id, generation)

    @router.get('/api/packages/{ontology_id}/resources/current')
    def current_resources(ontology_id: str, request: Request,
                          offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with operation(request) as (store, actor):
            root = resource_access(store, actor, ontology_id)
            value = read_json(root / 'receipt.json', dict(generation=0, digest=None, files=[]))
            page = store.page(value['files'], offset, limit)
            return {**value, 'ontology_id': ontology_id, 'files': page.pop('items'), **page}

    @router.get('/api/packages/{ontology_id}/resources/current/{file_id}')
    def resource_download(ontology_id: str, file_id: str, request: Request):
        with operation(request) as (store, actor):
            root = resource_access(store, actor, ontology_id)
            value = read_json(root / 'receipt.json', {'files': []})
            row = next((row for row in value['files'] if row['id'] == file_id), None)
            if not row:
                raise PackageError('not_found', 'Resource not found', 404)
            path = root / row['path']
            if not path.is_file() or digest(path) != row['digest']:
                raise PackageError('archive_integrity', 'Resource integrity failure', 503)
            return stream_file(path, Path(row['path']).name, row['digest'])

    return router
