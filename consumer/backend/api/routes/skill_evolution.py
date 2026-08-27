"""HTTP routes for skill-evolution tasks.

All under `/agentic/skill-evolution`, JWT-required (same as other `/agentic/*`).
Workspace resolved via `X-Workspace-Id`. approve/reject/cancel are admin-only.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.auth.auth import (
    AuthUser,
    require_admin_user,
    require_authenticated_user,
)
from api.dependencies import (
    require_admin_workspace_id,
    require_workspace_id,
    run_in_runtime_pool,
)
from api.request_parsing import read_optional_json_object
from utils.logging import get_logger
from agent.skill_evolution.registry import get_skill_evolution_service
from agent.skill_evolution.service import GateError

logger = get_logger("skill_evolution.routes")

skill_evolution_router = APIRouter(prefix="/agentic/skill-evolution")


def _service():
    return get_skill_evolution_service()


def _task_or_404(workspace_id: str, task_id: str):
    record = _service()._store.get_task(workspace_id, task_id)  # noqa: SLF001
    if record is None:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    return record


def _public_task(record) -> Dict[str, Any]:
    return {
        "task_id": record.task_id,
        "workspace_id": record.workspace_id,
        "ontology_id": record.ontology_id,
        "ontology_name": record.ontology_name,
        "session_id": record.session_id,
        "triggered_by": record.triggered_by,
        "run_id": record.run_id,
        "status": record.status,
        "stage": record.stage,
        "active_version": record.active_version,
        "change_summary": record.change_summary,
        "error_message": record.error_message,
        "failed_stage": record.failed_stage,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "decided_by": record.decided_by,
        "decided_at": record.decided_at,
        "decision": record.decision,
        "new_version": record.new_version,
    }


@skill_evolution_router.post("/start")
async def start_evolution(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    data = await read_optional_json_object(request)
    ontology_id = str(data.get("ontology_id") or "").strip()  # optional: inferred from trace if empty
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id 必填"}, status_code=400)

    # Resolve the triggering user's execution store (trace source).
    from api.routes import get_request_stores  # local import to avoid cycle

    try:
        _, execution_store = get_request_stores()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    try:
        record = await run_in_runtime_pool(
            request,
            "external_io",
            _service().start,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            session_id=session_id,
            triggered_by=user.username,
            execution_store=execution_store,
        )
    except GateError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("skill-evolution start failed")
        return JSONResponse(
            {"error": "发起进化失败：{}".format(str(exc)[:200])},
            status_code=500,
        )
    return JSONResponse(_public_task(record), status_code=202)


@skill_evolution_router.get("/tasks")
async def list_tasks(
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    ontology_id = str(request.query_params.get("ontology_id") or "").strip() or None
    pending_only = str(request.query_params.get("pending_only") or "").lower() in {"1", "true", "yes"}
    is_admin = user.role == "admin"

    records = await run_in_runtime_pool(
        request,
        "short_io",
        _service()._store.list_tasks,  # noqa: SLF001
        workspace_id,
        ontology_id=ontology_id,
        pending_only=pending_only,
        triggered_by=user.username,
        is_admin=is_admin,
    )
    return {"tasks": [_public_task(r) for r in records]}


@skill_evolution_router.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    if user.role != "admin" and record.triggered_by != user.username:
        return JSONResponse({"error": "无权访问该任务"}, status_code=403)
    payload = _public_task(record)
    # drift 只对待审批(completed)有意义；终态任务已不能审批，跳过避免
    # 每次 10s 轮询都跑 registry.refresh + 整集 sha（无意义消耗）。
    if record.status == "completed":
        drift = await run_in_runtime_pool(
            request,
            "short_io",
            _service().detect_drift,
            record,
        )
        payload["drift"] = drift.to_dict()
    return payload


@skill_evolution_router.get("/tasks/{task_id}/diff")
async def get_diff(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    if user.role != "admin" and record.triggered_by != user.username:
        return JSONResponse({"error": "无权访问该任务"}, status_code=403)
    if record.status not in ("completed", "approved", "rejected"):
        return JSONResponse({"error": "任务尚未完成，暂无 diff"}, status_code=400)
    diff = await run_in_runtime_pool(
        request,
        "tool_io",
        _service().get_diff,
        record,
    )
    payload = diff.to_dict()
    # drift 只对待审批(completed)有意义；approved/rejected 终态跳过。
    if record.status == "completed":
        drift = await run_in_runtime_pool(
            request,
            "short_io",
            _service().detect_drift,
            record,
        )
        payload["drift"] = drift.to_dict()
    return payload


@skill_evolution_router.post("/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin_user),
    workspace_id: str = Depends(require_admin_workspace_id),
):
    data = await read_optional_json_object(request)
    mode = str(data.get("mode") or "").strip()
    version_name = str(data.get("version_name") or "").strip() or None
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    try:
        record = await run_in_runtime_pool(
            request,
            "tool_io",
            _service().approve,
            record,
            mode=mode,
            version_name=version_name,
            decided_by=user.username,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.exception("skill-evolution approve failed")
        return JSONResponse(
            {"error": "审批失败：{}".format(str(exc)[:200])},
            status_code=500,
        )
    return _public_task(record)


@skill_evolution_router.post("/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_admin_user),
    workspace_id: str = Depends(require_admin_workspace_id),
):
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    try:
        record = await run_in_runtime_pool(
            request,
            "tool_io",
            _service().reject,
            record,
            decided_by=user.username,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.exception("skill-evolution reject failed")
        return JSONResponse(
            {"error": "拒绝失败：{}".format(str(exc)[:200])},
            status_code=500,
        )
    return _public_task(record)


@skill_evolution_router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    user: AuthUser = Depends(require_authenticated_user),
    workspace_id: str = Depends(require_workspace_id),
):
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    if user.role != "admin" and record.triggered_by != user.username:
        return JSONResponse({"error": "无权取消该任务"}, status_code=403)
    try:
        record = await run_in_runtime_pool(
            request,
            "short_io",
            _service().cancel,
            record,
            decided_by=user.username,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.exception("skill-evolution cancel failed")
        return JSONResponse(
            {"error": "取消失败：{}".format(str(exc)[:200])},
            status_code=500,
        )
    return _public_task(record)


@skill_evolution_router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    request: Request,
    _user: AuthUser = Depends(require_admin_user),
    workspace_id: str = Depends(require_admin_workspace_id),
):
    record = await run_in_runtime_pool(
        request,
        "short_io",
        _task_or_404,
        workspace_id,
        task_id,
    )
    if isinstance(record, JSONResponse):
        return record
    try:
        await run_in_runtime_pool(
            request,
            "tool_io",
            _service().delete_task,
            record,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception as exc:
        logger.exception("skill-evolution delete failed")
        return JSONResponse(
            {"error": "删除失败：{}".format(str(exc)[:200])},
            status_code=500,
        )
    return {"success": True, "task_id": task_id}
