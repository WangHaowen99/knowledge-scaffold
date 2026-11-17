"""独立评测对接（ADR-0092，API 契约 3.21a ES-00～ES-06、ES-09～ES-11）：评测入口、完成通知、已发布版本的评测结果，
以及旧版评测记录（只读）。评测优化（ES-07、ES-08、ES-12）随反馈演进包删除。

评测在评测服务里跑。onto 只给浏览器入口、收评测服务的完成通知，查看时以当前用户自己的登录读回报告、优化建议和
完整回答。

完成通知的路径是评测服务调用的外部路径，一字不改：``POST /api/internal/evaluation/notifications``，
请求头 ``X-Evaluation-Service-Token`` 带服务凭据（ONTO_EVALUATION_SERVICE_TOKEN），不经登录。
"""

from __future__ import annotations

import hmac
import json
import os

from fastapi import APIRouter, Depends, Request

from ..api import deps
from ..api.errors import ApiError
from ..services import auth, evaluation_entry, legacy_evaluations, package_evaluations
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
    """ES-01：评测服务的浏览器入口；url 为空时前端按建模页面同主机的 5180 端口推导。"""
    try:
        return {"url": evaluation_entry.public_url()}
    except ValueError as exc:
        raise ApiError("internal_error", f"ONTO_WEB_EVAL_PUBLIC_URL 无效：{exc}", 500) from exc


def _subject(request: Request) -> tuple[str, str] | None:
    """(用户令牌, 主体)；本地模式或没有登录时为 None。主体与 Hub 包关联同一口径。"""
    claims = deps.user(request)
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    subject = claims.get("sub") if claims else None
    if not auth.ENABLED or not subject or not token:
        return None
    return token, json.dumps([claims.get("iss", auth.ISSUER), subject], ensure_ascii=False)


def _caller(project_id: str, request: Request) -> tuple[str, str]:
    deps.require_owner(project_id, request)
    identity = _subject(request)
    if identity is None:
        # 与 develop 同一文案；码不用 unauthenticated，免得前端当成登录过期去刷新
        raise ApiError("login_required", "个人 Hub 包需要统一账号登录", 401)
    return identity


@router.get(f"{_P}/evaluations")
def evaluations(project_id: str, request: Request, reconcile: bool = False):
    """ES-02：各发布版本的评测、求解验证、用户反馈状态；reconcile=1 时先向评测服务对账。"""
    token, subject = _caller(project_id, request)
    return package_evaluations.overview(project_id, subject, token, reconcile)


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
    """ES-05：报告「完整回答」附件的一条，读自评测服务。"""
    token, subject = _caller(project_id, request)
    return package_evaluations.answer(project_id, subject, token, run_id, result_id)


@router.post(f"{_P}/evaluations/{{run_id}}/regenerate")
def regenerate(project_id: str, run_id: str, request: Request):
    """ES-06"""
    token, subject = _caller(project_id, request)
    return package_evaluations.regenerate(project_id, subject, token, run_id)


# ---- 旧版评测记录（只读） ----


@router.get(f"{_P}/legacy-evaluations")
def legacy_status(project_id: str, request: Request):
    """ES-09：评测移出前最近一轮的执行详情（develop 的「评测执行详情」）。"""
    return legacy_evaluations.status(deps.require_owner(project_id, request))


@router.get(f"{_P}/legacy-evaluations/{{stem}}")
def legacy_report(project_id: str, stem: str, request: Request):
    """ES-10"""
    return legacy_evaluations.report(deps.require_owner(project_id, request), stem)


@router.get(f"{_P}/legacy-evaluations/{{stem}}/answers/{{result_id}}")
def legacy_answer(project_id: str, stem: str, result_id: str, request: Request):
    """ES-11"""
    return legacy_evaluations.answer(deps.require_owner(project_id, request), stem, result_id)
