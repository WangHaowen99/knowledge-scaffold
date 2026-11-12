"""独立评测对接（ADR-0092，API 契约 3.21a ES-00～ES-12）：评测入口、完成通知、已发布版本的评测结果与评测优化，
以及旧版评测记录（只读）。

评测在评测服务里跑。onto 只给浏览器入口、收评测服务的完成通知，查看时以当前用户自己的登录读回报告、优化建议和
完整回答；「评测优化」把产物整理成一批外部反馈交给 /ontology-evolution。

完成通知的路径是评测服务调用的外部路径，一字不改：``POST /api/internal/evaluation/notifications``，
请求头 ``X-Evaluation-Service-Token`` 带服务凭据（ONTO_EVALUATION_SERVICE_TOKEN），不经登录。
"""

from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..api import deps
from ..api.errors import ApiError
from ..services import auth, evaluation_entry, feedback_inbox, legacy_evaluations, package_evaluations, research_view
from ..services.workspace import WorkspaceError

NOTIFICATIONS_PATH = "/api/internal/evaluation/notifications"
SERVICE_TOKEN_ENV = "ONTO_EVALUATION_SERVICE_TOKEN"

router = APIRouter(tags=["evaluations"])
_P = "/api/v1/projects/{project_id}"


def service_identity(request: Request) -> None:
    expected = os.environ.get(SERVICE_TOKEN_ENV, "")
    supplied = request.headers.get("x-evaluation-service-token", "")
    if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise WorkspaceError("评测服务身份无效", 401, "unauthenticated")


@router.post(NOTIFICATIONS_PATH, dependencies=[Depends(service_identity)])
def notification(body: package_evaluations.Notification):
    """评测服务重试到收到 2xx 为止：先落盘再应答，重复或迟到的通知同样应答。"""
    package_evaluations.receive(body)
    return {"accepted": True}


@router.get("/api/v1/evaluation/entry")
def entry():
    """ES-01：评测服务的浏览器入口；url 为空时前端按建模页面同主机的 8090 端口推导。"""
    try:
        return {"url": evaluation_entry.public_url()}
    except ValueError as exc:
        raise ApiError("internal_error", f"ONTO_WEB_EVAL_PUBLIC_URL 无效：{exc}", 500) from exc


def _subject(request: Request) -> tuple[str, str] | None:
    """(用户令牌, 主体)；本地模式或没有登录时为 None。主体与 Hub 包关联、评测优化记录同一口径。"""
    claims = deps.user(request)
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not auth.ENABLED or not claims or not claims.get("sub") or not token:
        return None
    return token, json.dumps([claims.get("iss", auth.ISSUER), claims["sub"]], ensure_ascii=False)


def _evolution(root) -> bool:
    return research_view.pack_enabled(root, feedback_inbox.EVOLUTION_PACK)


def _caller(project_id: str, request: Request, write: bool = False) -> tuple[str, str]:
    (deps.require_writable if write else deps.require_owner)(project_id, request)
    identity = _subject(request)
    if identity is None:
        # 与 develop 同一文案；码不用 unauthenticated，免得前端当成登录过期去刷新
        raise ApiError("login_required", "个人 Hub 包需要统一账号登录", 401)
    return identity


@router.get(f"{_P}/evaluations")
def evaluations(project_id: str, request: Request, reconcile: bool = False):
    """ES-02：各发布版本的评测、求解验证、用户反馈状态；reconcile=1 时先向评测服务对账。"""
    token, subject = _caller(project_id, request)
    root = deps.workspaces.project_root(project_id)
    # evolution：评测优化交给反馈演进包处理，包没启用时「评测优化」置灰
    return {**package_evaluations.overview(project_id, subject, token, reconcile), "evolution": _evolution(root)}


@router.get(f"{_P}/evaluations/{{run_id}}/report")
def report(project_id: str, run_id: str, request: Request):
    """ES-03"""
    token, subject = _caller(project_id, request)
    return package_evaluations.document(project_id, subject, token, run_id, "report")


@router.get(f"{_P}/evaluations/{{run_id}}/suggestions")
def suggestions(project_id: str, run_id: str, request: Request):
    """ES-04"""
    token, subject = _caller(project_id, request)
    return package_evaluations.document(project_id, subject, token, run_id, "suggestions")


@router.get(f"{_P}/evaluations/{{run_id}}/answers/{{result_id}}")
def answer(project_id: str, run_id: str, result_id: str, request: Request):
    """ES-05：报告「完整回答」附件的一条，读自评测服务，不需要先评测优化。"""
    token, subject = _caller(project_id, request)
    return package_evaluations.answer(project_id, subject, token, run_id, result_id)


@router.post(f"{_P}/evaluations/{{run_id}}/regenerate")
def regenerate(project_id: str, run_id: str, request: Request):
    """ES-06"""
    token, subject = _caller(project_id, request)
    return package_evaluations.regenerate(project_id, subject, token, run_id)


@router.post(f"{_P}/evaluations/{{run_id}}/optimization")
def optimization(project_id: str, run_id: str, request: Request):
    """ES-07：产物拉进工作区、整理成一批外部反馈，返回交给 /ontology-evolution 的命令。"""
    token, subject = _caller(project_id, request, write=True)
    if not _evolution(deps.workspaces.project_root(project_id)):
        raise ApiError("pack_not_enabled", "评测优化由反馈演进插件处理，项目没有启用它", 409, {"pack": feedback_inbox.EVOLUTION_PACK})
    return package_evaluations.optimize(deps.workspaces, project_id, subject, token, run_id, deps.actor(request))


class FailureBody(BaseModel):
    model_config = {"extra": "forbid"}
    note: str = Field(min_length=1, max_length=package_evaluations.WRITEBACK_NOTE)


@router.post(f"{_P}/evaluations/{{run_id}}/modeling-result")
def modeling_result(project_id: str, run_id: str, body: FailureBody, request: Request):
    """ES-08：告诉评测服务这一轮建议处理失败；处理成功由发布新版本自动回写。"""
    token, subject = _caller(project_id, request, write=True)
    return {"optimization": package_evaluations.report_failure(project_id, subject, token, run_id, body.note)}


# ---- 旧版评测记录（只读） ----


@router.get(f"{_P}/legacy-evaluations")
def legacy_status(project_id: str, request: Request):
    """ES-09：评测移出前最近一轮的执行详情（develop 的「评测执行详情」）。"""
    root = deps.require_owner(project_id, request)
    return {**legacy_evaluations.status(root), "evolution_enabled": _evolution(root)}


@router.get(f"{_P}/legacy-evaluations/{{stem}}")
def legacy_report(project_id: str, stem: str, request: Request):
    """ES-10"""
    return legacy_evaluations.report(deps.require_owner(project_id, request), stem)


@router.get(f"{_P}/legacy-evaluations/{{stem}}/answers/{{result_id}}")
def legacy_answer(project_id: str, stem: str, result_id: str, request: Request):
    """ES-11"""
    return legacy_evaluations.answer(deps.require_owner(project_id, request), stem, result_id)


@router.post(f"{_P}/legacy-evaluations/{{stem}}/optimization")
def legacy_optimization(project_id: str, stem: str, request: Request):
    """ES-12：旧报告的评测优化，整理成一批外部反馈交给 /ontology-evolution。"""
    root = deps.require_writable(project_id, request)
    if not _evolution(root):
        raise ApiError("pack_not_enabled", "评测优化由反馈演进插件处理，项目没有启用它", 409, {"pack": feedback_inbox.EVOLUTION_PACK})
    return legacy_evaluations.optimize(deps.workspaces, project_id, stem, deps.actor(request))
