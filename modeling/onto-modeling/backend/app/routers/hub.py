"""Hub authorization callback; project authority remains in Onto."""
import hmac
import os
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import projects
from ..services import auth, knowledge_hub
from ..services.workspace import WorkspaceError

router = APIRouter(prefix='/api/internal/hub', tags=['hub'])


class AuthorizeBody(BaseModel):
    project_id: str
    operation: Literal['read', 'publish', 'evaluation', 'sync_resources', 'cleanup']


@router.post('/authorize')
def authorize(body: AuthorizeBody, request: Request):
    expected = os.environ.get('ONTO_HUB_SERVICE_TOKEN', '')
    if not expected or not hmac.compare_digest(request.headers.get('x-hub-service-token', '').encode(), expected.encode()):
        raise WorkspaceError('Hub 服务身份无效', 401)
    knowledge_hub.identifier(body.project_id)
    if body.operation == 'cleanup':
        task = knowledge_hub.load(knowledge_hub.state_dir() / 'cleanup' / f'{body.project_id}.json')
        if not task:
            raise WorkspaceError('没有项目清理任务', 403)
        try:
            projects.workspaces.project_root(body.project_id)
        except WorkspaceError as exc:
            if exc.status != 404:
                raise
        else:
            raise WorkspaceError('项目仍存在，拒绝清理', 409)
        return {'project_id': body.project_id, 'subject': 'hub-cleanup', 'allowed': True, 'project_state': 'deleted'}
    if body.operation == 'sync_resources':
        projects.workspaces.project_root(body.project_id)
        return {'project_id': body.project_id, 'subject': 'hub-resource-sync', 'allowed': True, 'project_state': 'active'}
    if auth.ENABLED:
        token = knowledge_hub.token_of(request)
        claims = auth.verify_access_token(token) if token else None
        if claims is None:
            raise WorkspaceError('未登录或登录已过期', 401)
        request.state.user = claims
    elif os.environ.get('ONTO_HUB_DEV_ALLOW_UNAUTH') != '1':
        raise WorkspaceError('Hub 本地开发鉴权未显式启用', 503)
    projects.ensure_access(body.project_id, request, write=body.operation == 'evaluation')
    return {'project_id': body.project_id, 'subject': projects._request_user(request) or 'local-dev',
            'allowed': True, 'project_state': 'active', 'development': not auth.ENABLED}
