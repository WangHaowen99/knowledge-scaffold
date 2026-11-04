"""Modeling's personal Hub package browser and explicit import/copy actions."""
import json
from pathlib import Path
import shutil
import tempfile

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from . import projects
from ..services import auth, hub_packages, knowledge_hub
from ..services.workspace import WorkspaceError

router = APIRouter(prefix='/api/hub', tags=['hub-packages'])


def identity(request):
    token = knowledge_hub.token_of(request)
    claims = auth.verify_access_token(token) if token and auth.ENABLED else None
    if not claims or not claims.get('sub'):
        raise WorkspaceError('个人 Hub 包需要统一账号登录', 401)
    request.state.user = claims
    subject = json.dumps([claims.get('iss', auth.ISSUER), claims['sub']], ensure_ascii=False)
    return token, subject, claims['sub']


class ImportBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$')
    ontology_id: str = Field(pattern=r'^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$')
    release_id: str = Field(pattern=r'^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}$')
    package_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    name: str = Field(min_length=1, max_length=128)


@router.get('/packages')
def packages(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    token, _, _ = identity(request)
    return hub_packages.call('GET', f'/api/packages?offset={offset}&limit={limit}', token)


@router.get('/packages/{package_id}/releases')
def releases(package_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    token, _, _ = identity(request)
    return hub_packages.call('GET', hub_packages.reference_path(package_id) + f'?offset={offset}&limit={limit}', token)


@router.get('/package-projects')
def package_projects(request: Request, ontology_id: list[str] = Query(...)):
    """供独立评测展示：当前用户可访问、且由本人关联到这些 Hub 包的建模项目名称。"""
    _, subject, username = identity(request)
    is_admin = request.state.user.get('role') == 'admin'
    return hub_packages.linked_projects(projects.workspaces, subject, username, is_admin, ontology_id)


@router.get('/packages/{package_id}/releases/{release_id}/download')
def download(package_id: str, release_id: str, request: Request):
    token, _, _ = identity(request)
    folder = Path(tempfile.mkdtemp(prefix='onto-hub-package-download-'))
    path = folder / 'package.zip'
    try:
        receipt = hub_packages.download(package_id, release_id, token, path)
        return FileResponse(path, filename=knowledge_hub.package_filename(receipt['ontology_id'], receipt['name'], receipt['version']), media_type='application/zip',
                            background=BackgroundTask(shutil.rmtree, folder))
    except Exception:
        shutil.rmtree(folder)
        raise


@router.post('/imports')
def import_package(body: ImportBody, request: Request):
    token, subject, username = identity(request)
    return hub_packages.import_package(projects.workspaces, body.model_dump(), token, subject, username)
