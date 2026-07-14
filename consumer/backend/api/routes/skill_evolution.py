"""HTTP routes for skill-evolution tasks.

All under `/agentic/skill-evolution`, JWT-required (same as other `/agentic/*`).
Workspace resolved via `X-Workspace-Id`. approve/reject/cancel are admin-only.
"""

from __future__ import annotations

from typing import Any, Dict

from flask import Blueprint, jsonify, request

from api.auth.auth import get_current_user, require_auth
from utils.logging import get_logger
from workspace.workspaces import resolve_workspace_id, workspace_scope
from agent.skill_evolution.registry import get_skill_evolution_service
from agent.skill_evolution.service import GateError

logger = get_logger("skill_evolution.routes")

skill_evolution_bp = Blueprint("skill_evolution", __name__, url_prefix="/agentic/skill-evolution")


def _request_workspace_id() -> str:
    return resolve_workspace_id(request)


def _service():
    return get_skill_evolution_service()


def _task_or_404(workspace_id: str, task_id: str):
    record = _service()._store.get_task(workspace_id, task_id)  # noqa: SLF001
    if record is None:
        return None, (jsonify({"error": "任务不存在"}), 404)
    return record, None


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


@skill_evolution_bp.route("/start", methods=["POST"])
@require_auth()
def start_evolution():
    workspace_id = _request_workspace_id()
    data = request.get_json(silent=True) or {}
    ontology_id = str(data.get("ontology_id") or "").strip()  # optional: inferred from trace if empty
    session_id = str(data.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id 必填"}), 400

    user = get_current_user()
    # Resolve the triggering user's execution store (trace source).
    from api.routes import get_request_stores  # local import to avoid cycle

    try:
        _, execution_store = get_request_stores()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    with workspace_scope(workspace_id):
        try:
            record = _service().start(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                session_id=session_id,
                triggered_by=user.username,
                execution_store=execution_store,
            )
        except GateError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("skill-evolution start failed")
            return jsonify({"error": "发起进化失败：{}".format(str(exc)[:200])}), 500
    return jsonify(_public_task(record)), 202


@skill_evolution_bp.route("/tasks", methods=["GET"])
@require_auth()
def list_tasks():
    workspace_id = _request_workspace_id()
    ontology_id = str(request.args.get("ontology_id") or "").strip() or None
    pending_only = str(request.args.get("pending_only") or "").lower() in {"1", "true", "yes"}
    user = get_current_user()
    is_admin = user.role == "admin"

    with workspace_scope(workspace_id):
        records = _service()._store.list_tasks(  # noqa: SLF001
            workspace_id,
            ontology_id=ontology_id,
            pending_only=pending_only,
            triggered_by=user.username,
            is_admin=is_admin,
        )
    return jsonify({"tasks": [_public_task(r) for r in records]})


@skill_evolution_bp.route("/tasks/<task_id>", methods=["GET"])
@require_auth()
def get_task(task_id: str):
    workspace_id = _request_workspace_id()
    user = get_current_user()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        if user.role != "admin" and record.triggered_by != user.username:
            return jsonify({"error": "无权访问该任务"}), 403
        payload = _public_task(record)
        # drift 只对待审批(completed)有意义；终态任务已不能审批，跳过避免
        # 每次 10s 轮询都跑 registry.refresh + 整集 sha（无意义消耗）。
        if record.status == "completed":
            payload["drift"] = _service().detect_drift(record).to_dict()
    return jsonify(payload)


@skill_evolution_bp.route("/tasks/<task_id>/diff", methods=["GET"])
@require_auth()
def get_diff(task_id: str):
    workspace_id = _request_workspace_id()
    user = get_current_user()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        if user.role != "admin" and record.triggered_by != user.username:
            return jsonify({"error": "无权访问该任务"}), 403
        if record.status not in ("completed", "approved", "rejected"):
            return jsonify({"error": "任务尚未完成，暂无 diff"}), 400
        diff = _service().get_diff(record)
        payload = diff.to_dict()
        # drift 只对待审批(completed)有意义；approved/rejected 终态跳过。
        if record.status == "completed":
            payload["drift"] = _service().detect_drift(record).to_dict()
    return jsonify(payload)


@skill_evolution_bp.route("/tasks/<task_id>/approve", methods=["POST"])
@require_auth(roles=["admin"])
def approve_task(task_id: str):
    workspace_id = _request_workspace_id()
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode") or "").strip()
    version_name = str(data.get("version_name") or "").strip() or None
    user = get_current_user()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        try:
            record = _service().approve(record, mode=mode, version_name=version_name, decided_by=user.username)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("skill-evolution approve failed")
            return jsonify({"error": "审批失败：{}".format(str(exc)[:200])}), 500
    return jsonify(_public_task(record))


@skill_evolution_bp.route("/tasks/<task_id>/reject", methods=["POST"])
@require_auth(roles=["admin"])
def reject_task(task_id: str):
    workspace_id = _request_workspace_id()
    user = get_current_user()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        try:
            record = _service().reject(record, decided_by=user.username)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("skill-evolution reject failed")
            return jsonify({"error": "拒绝失败：{}".format(str(exc)[:200])}), 500
    return jsonify(_public_task(record))


@skill_evolution_bp.route("/tasks/<task_id>/cancel", methods=["POST"])
@require_auth()
def cancel_task(task_id: str):
    workspace_id = _request_workspace_id()
    user = get_current_user()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        if user.role != "admin" and record.triggered_by != user.username:
            return jsonify({"error": "无权取消该任务"}), 403
        try:
            record = _service().cancel(record, decided_by=user.username)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("skill-evolution cancel failed")
            return jsonify({"error": "取消失败：{}".format(str(exc)[:200])}), 500
    return jsonify(_public_task(record))


@skill_evolution_bp.route("/tasks/<task_id>", methods=["DELETE"])
@require_auth(roles=["admin"])
def delete_task(task_id: str):
    workspace_id = _request_workspace_id()
    with workspace_scope(workspace_id):
        record, err = _task_or_404(workspace_id, task_id)
        if err:
            return err
        try:
            _service().delete_task(record)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        except Exception as exc:
            logger.exception("skill-evolution delete failed")
            return jsonify({"error": "删除失败：{}".format(str(exc)[:200])}), 500
    return jsonify({"success": True, "task_id": task_id})
