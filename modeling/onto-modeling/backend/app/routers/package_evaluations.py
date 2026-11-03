"""Evaluation service notifications and the evaluation results of a project's Hub package.

Project routes stay outside /api/projects/*/eval/: main.py retires mutations under that legacy prefix.
"""
import hmac
import os

from fastapi import APIRouter, Depends, Request

from . import projects
from .hub_packages import identity
from ..services import package_evaluations
from ..services.workspace import WorkspaceError

router = APIRouter(tags=['package-evaluations'])


def service_identity(request: Request):
    expected = os.environ.get('ONTO_EVALUATION_SERVICE_TOKEN', '')
    supplied = request.headers.get('x-evaluation-service-token', '')
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise WorkspaceError('评测服务身份无效', 401)


@router.post('/api/internal/evaluation/notifications', dependencies=[Depends(service_identity)])
def notification(body: package_evaluations.Notification):
    """评测服务重试到收到 2xx 为止：先落盘再应答，重复或迟到的通知同样应答。"""
    package_evaluations.receive(body)
    return {'accepted': True}


def caller(project_id: str, request: Request, write: bool = False):
    token, subject, _ = identity(request)
    projects.ensure_access(project_id, request, write=write)
    return token, subject


@router.get('/api/projects/{project_id}/package/evaluations')
def evaluations(project_id: str, request: Request, reconcile: bool = False):
    token, subject = caller(project_id, request)
    return package_evaluations.overview(project_id, subject, token, reconcile)


@router.get('/api/projects/{project_id}/package/evaluations/{run_id}/report')
def report(project_id: str, run_id: str, request: Request):
    token, subject = caller(project_id, request)
    return package_evaluations.document(project_id, subject, token, run_id, 'report')


@router.get('/api/projects/{project_id}/package/evaluations/{run_id}/suggestions')
def suggestions(project_id: str, run_id: str, request: Request):
    token, subject = caller(project_id, request)
    return package_evaluations.document(project_id, subject, token, run_id, 'suggestions')


@router.post('/api/projects/{project_id}/package/evaluations/{run_id}/regenerate')
def regenerate(project_id: str, run_id: str, request: Request):
    token, subject = caller(project_id, request)
    return package_evaluations.regenerate(project_id, subject, token, run_id)


@router.post('/api/projects/{project_id}/package/evaluations/{run_id}/optimization')
def optimization(project_id: str, run_id: str, request: Request):
    token, subject = caller(project_id, request, write=True)
    return package_evaluations.optimize(projects.workspaces, project_id, subject, token, run_id)
