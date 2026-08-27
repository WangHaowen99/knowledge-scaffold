"""Workspace-scoped ontology evidence lookup and management APIs.

Dual registration:
- ``evidence_router`` is the production FastAPI router mounted by
  ``api.routes.register_routers`` on the ASGI skeleton.
- ``evidence_bp`` is a legacy Flask blueprint kept solely for the Flask route
  fixture in ``tests/test_evidence_routes.py``; it is not mounted on the
  FastAPI application.  Both re-export the same framework-agnostic core
  handlers, so business logic is defined exactly once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from flask import Blueprint, current_app, jsonify, request as flask_request

from api.auth.auth import current_user_scope, get_current_user
from api.dependencies import (
    require_admin_workspace_id,
    require_workspace_id,
)
from api.request_parsing import read_optional_json_object
from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import (
    EvidenceIndexManager,
    EvidenceIndexManagerError,
    active_pointer_is_serviceable,
    read_active_pointer_snapshot,
)
from ontology.evidence.resolver import EvidenceResolver, EvidenceResolverError
from ontology.evidence.sqlite_index import EvidenceIndexError, SQLiteEvidenceIndex
from ontology.mutations import MutationStore, OntologyMutationError, read_mutation_state_snapshot
from ontology.ontologies import get_ontology_registry, load_ontology_manifest_data
from persistence.evidence_operations import (
    EvidenceOperationsError,
    EvidenceOperationsStore,
    read_latest_build_job_snapshot,
)
from persistence.runtime_facts import RuntimeFactsStore
from workspace.workspaces import get_workspace_paths, resolve_workspace_id, workspace_scope


logger = logging.getLogger("routes.evidence")
evidence_router = APIRouter(prefix="/agentic")
evidence_bp = Blueprint("evidence", __name__, url_prefix="/agentic")

STATIC_MIN_RETENTION_DAYS = 7
RUNTIME_FACT_MIN_RETENTION_DAYS = 30
_IDEMPOTENCY_KEY_RE = re.compile(r"^[^\x00-\x1f\x7f]{8,128}$")
_ONTOLOGY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _flask_auth_manager():
    return current_app.config.get("AUTH_MANAGER")


def _flask_require_auth(roles=None):
    """Legacy Flask auth decorator kept for the evidence_bp route fixture.

    ``api.auth.auth.require_auth`` was removed by the FastAPI migration; the
    production FastAPI routes enforce auth through ``api.dependencies``
    Depends instead.  This local replica restores the same bearer-token/roles
    checks for the Flask-only test harness and binds the resolved principal to
    the current_user ContextVar so get_current_user() keeps working.
    """
    roles_set = set(roles) if roles else None

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if flask_request.method == "OPTIONS":
                return "", 204
            auth_manager = _flask_auth_manager()
            if not auth_manager:
                return jsonify({"error": "Auth not configured"}), 500
            auth_header = flask_request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "Unauthorized"}), 401
            token = auth_header.split(" ", 1)[1].strip()
            user = auth_manager.verify_token(token)
            if not user:
                return jsonify({"error": "Unauthorized"}), 401
            if roles_set and user.role not in roles_set:
                return jsonify({"error": "Forbidden"}), 403
            with current_user_scope(user):
                return fn(*args, **kwargs)
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Shared, framework-agnostic helpers
# ---------------------------------------------------------------------------


def _error(
    request,
    code: str,
    message: str,
    status: int,
    retryable: bool = False,
    retry_after_ms: Optional[int] = None,
):
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
        "request_id": str(request.headers.get("X-Request-Id") or "req_" + uuid.uuid4().hex),
    }
    if retry_after_ms is not None:
        error["retry_after_ms"] = int(retry_after_ms)
    return {"success": False, "error": error}, status


def _workspace(request):
    try:
        return resolve_workspace_id(request), None
    except KeyError:
        return None, _error(request, "ONTOLOGY_NOT_FOUND", "工作空间不存在。", 404)
    except ValueError as exc:
        return None, _error(request, "INVALID_ARGUMENTS", str(exc), 400)


def _validate_payload(request, payload, allowed_fields: set[str]):
    if not isinstance(payload, dict):
        return None, _error(request, "INVALID_ARGUMENTS", "请求体必须为 JSON object。", 400)
    unknown = sorted(set(payload) - allowed_fields)
    if unknown:
        return None, _error(request, "INVALID_ARGUMENTS", "存在未知字段：{}。".format(", ".join(unknown)), 400)
    return payload, None


def _idempotency_key(request, *, required: bool):
    value = str(request.headers.get("Idempotency-Key") or "")
    if not value and not required:
        return None, None
    if not _IDEMPOTENCY_KEY_RE.fullmatch(value):
        return None, _error(request, "INVALID_ARGUMENTS", "Idempotency-Key 必须为 8～128 个可打印字符。", 400)
    return value, None


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _same_state(before: Optional[dict[str, Any]], after: Optional[dict[str, Any]]) -> bool:
    if not before or not after:
        return False
    return all(
        before.get(field) == after.get(field)
        for field in ("workspace_id", "ontology_id", "active_version", "enabled", "mutation_revision")
    )


def _valid_ontology_id(value: str) -> bool:
    return bool(_ONTOLOGY_ID_RE.fullmatch(str(value or "")))


def _resolver_error(request, exc: EvidenceResolverError):
    status = {
        "INVALID_EVIDENCE_ID": 400,
        "EVIDENCE_NOT_FOUND": 404,
        "EVIDENCE_CATALOG_UNAVAILABLE": 503,
        "EVIDENCE_STORAGE_INCONSISTENT": 503,
        "EVIDENCE_INTEGRITY_FAILED": 500,
    }.get(exc.code, 503)
    return _error(request, exc.code, str(exc), status, exc.retryable, 1000 if exc.retryable else None)


def _manager_error(request, exc: EvidenceIndexManagerError):
    return _coded_error(request, exc)


def _operations_error(request, exc: EvidenceOperationsError):
    status = 409 if exc.code == "IDEMPOTENCY_CONFLICT" else 503
    return _error(request, exc.code, str(exc), status, status == 503, 1000 if status == 503 else None)


def _coded_error(request, exc: Exception):
    code = str(getattr(exc, "code", "INDEX_BUILD_FAILED"))
    status = {
        "INVALID_ARGUMENTS": 400,
        "INDEX_CAPACITY_EXCEEDED": 413,
        "IDEMPOTENCY_CONFLICT": 409,
        "MUTATION_STATE_CONFLICT": 409,
        "MUTATION_REVISION_CONFLICT": 409,
        "INDEX_STALE": 503,
        "INDEX_BUILDING": 503,
        "INDEX_CAPABILITY_MISSING": 503,
    }.get(code, 503)
    retryable = code in {"INDEX_STALE", "INDEX_BUILDING", "MUTATION_STATE_UNAVAILABLE"}
    return _error(request, code, str(exc), status, retryable, 1000 if retryable else None)


# ---------------------------------------------------------------------------
# Core handlers (framework-agnostic; return (payload, status_code))
# ---------------------------------------------------------------------------


def _query_params_empty(request) -> bool:
    """True when the request carries no query parameters.

    Flask exposes MultiDict ``request.args`` while Starlette exposes
    ``request.query_params``; the shared cores must check either surface.
    """
    query = getattr(request, "query_params", None)
    if query is not None:
        return len(query) == 0
    return len(request.args) == 0


def _lookup_evidence_core(request, evidence_id: str):
    if not _query_params_empty(request):
        return _error(request, "INVALID_ARGUMENTS", "Evidence Lookup 不支持 query 参数。", 400)
    workspace_id, failure = _workspace(request)
    if failure:
        return failure
    try:
        result = EvidenceResolver(
            get_workspace_paths(workspace_id).runtime_dir,
            workspace_id=workspace_id,
        ).lookup(evidence_id)
    except EvidenceResolverError as exc:
        return _resolver_error(request, exc)
    return result, 200


def _evidence_index_status_core(request, ontology_id: str):
    if not _query_params_empty(request):
        return _error(request, "INVALID_ARGUMENTS", "Index Status 不支持 query 参数。", 400)
    if not _valid_ontology_id(ontology_id):
        return _error(request, "INVALID_ARGUMENTS", "ontology_id 格式非法。", 400)
    workspace_id, failure = _workspace(request)
    if failure:
        return failure
    ontology = _get_ontology(workspace_id, ontology_id)
    if ontology is None:
        return _error(request, "ONTOLOGY_NOT_FOUND", "知识包不存在。", 404)

    paths = get_workspace_paths(workspace_id)
    config = EvidenceConfig.load()
    try:
        pointer = read_active_pointer_snapshot(paths.runtime_dir, ontology_id)
    except EvidenceIndexManagerError as exc:
        return _manager_error(request, exc)
    state = read_mutation_state_snapshot(
        paths.runtime_dir,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    operations_path = paths.runtime_dir / "ontology-evidence" / "operations.sqlite3"
    latest_job = read_latest_build_job_snapshot(
        operations_path,
        workspace_id=workspace_id,
        ontology_id=ontology_id,
    )
    metadata, generation_manifest = _generation_snapshot(
        paths.runtime_dir,
        workspace_id,
        ontology_id,
        pointer,
    )
    status = _effective_status(
        config=config,
        workspace_id=workspace_id,
        ontology=ontology,
        pointer=pointer,
        state=state,
        latest_job=latest_job,
    )
    counts = {
        "files": int((metadata or {}).get("file_count") or 0),
        "chunks": int((metadata or {}).get("chunk_count") or 0),
        "edges": int((metadata or {}).get("edge_count") or 0),
        "vectors": int(((generation_manifest or {}).get("counts") or {}).get("vectors") or 0),
    }
    build = _build_status(pointer, latest_job, generation_manifest)
    payload = {
        "contract": "OntologyEvidenceIndexStatus/v1",
        "ontology_id": ontology_id,
        "ontology_version": str(getattr(ontology, "active_version", "") or ""),
        "ontology_epoch": (pointer or {}).get("expected_epoch"),
        "mutation_revision": int(
            (state or {}).get("mutation_revision")
            if state is not None
            else (pointer or {}).get("mutation_revision") or 0
        ),
        "feature": {
            "enabled": config.workspace_mode(workspace_id) != "off",
            "rollout_mode": config.rollout_mode,
            "search_enabled": bool(config.search_enabled),
            "tool_exposure_mode": config.tool_exposure_mode,
            "auto_build_enabled": bool(config.auto_build_enabled),
            "runtime_fact_write_mode": config.runtime_fact_mode,
            "semantic_mode": config.semantic_mode,
        },
        "status": status,
        "active_generation": (pointer or {}).get("active_generation"),
        "target_generation": (
            latest_job.get("target_generation")
            if latest_job and latest_job.get("status") in {"queued", "building"}
            else (pointer or {}).get("target_generation")
        ),
        "retrieval_mode": (pointer or {}).get("retrieval_mode") or "lexical",
        "counts": counts,
        "build": build,
    }
    return payload, 200


def _rebuild_evidence_index_core(request, ontology_id: str, payload):
    if not _valid_ontology_id(ontology_id):
        return _error(request, "INVALID_ARGUMENTS", "ontology_id 格式非法。", 400)
    workspace_id, failure = _workspace(request)
    if failure:
        return failure
    payload, failure = _validate_payload(request, payload, {"reason", "semantic"})
    if failure:
        return failure
    reason = payload.get("reason")
    semantic = payload.get("semantic", "inherit")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 200:
        return _error(request, "INVALID_ARGUMENTS", "reason 长度必须为 1～200。", 400)
    if semantic not in {"inherit", "enable", "disable"}:
        return _error(request, "INVALID_ARGUMENTS", "semantic 只能为 inherit/enable/disable。", 400)
    idempotency_key, failure = _idempotency_key(request, required=True)
    if failure:
        return failure

    config = EvidenceConfig.load()
    if config.workspace_mode(workspace_id) == "off":
        return _error(request, "EVIDENCE_FEATURE_DISABLED", "当前工作空间未启用本体证据索引。", 409)
    if semantic == "enable":
        return _error(request, "INDEX_CAPABILITY_MISSING", "当前版本尚未启用 semantic generation。", 503)
    ontology = _get_ontology(workspace_id, ontology_id)
    if ontology is None:
        return _error(request, "ONTOLOGY_NOT_FOUND", "知识包不存在。", 404)
    if not bool(getattr(ontology, "enabled", False)):
        return _error(request, "ONTOLOGY_DISABLED", "知识包已禁用。", 409)

    paths = get_workspace_paths(workspace_id)
    active_version = str(getattr(ontology, "active_version", "") or "")
    try:
        mutation_store = MutationStore(paths.runtime_dir / "ontology-mutations.sqlite3")
        state_before = mutation_store.ensure_state(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            active_version=active_version,
            enabled=True,
        )
        ontology_dir = Path(ontology.ontology_dir).resolve()
        package_root = ontology_dir.parent.parent if ontology_dir.parent.name == "versions" else ontology_dir
        plan = EvidenceIndexManager(paths.runtime_dir).plan(
            ontology_id=ontology_id,
            ontology_version=active_version,
            ontology_dir=ontology_dir,
            manifest=load_ontology_manifest_data(package_root),
        )
        state_after = read_mutation_state_snapshot(
            paths.runtime_dir,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
        if not _same_state(state_before, state_after):
            return _error(request, "INDEX_STALE", "规划重建任务期间知识包发生变化。", 503, True, 500)
        request_hash = _request_hash(
            {
                "ontology_id": ontology_id,
                "reason": reason.strip(),
                "semantic": semantic,
            }
        )
        operations = EvidenceOperationsStore(
            paths.runtime_dir / "ontology-evidence" / "operations.sqlite3"
        )
        job = operations.create_or_get_build_job(
            job_id="idxjob_" + uuid.uuid4().hex,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            ontology_epoch=plan["ontology_epoch"],
            target_generation=plan["index_generation"],
            mutation_revision=int(state_before["mutation_revision"]),
            semantic=semantic,
            reason=reason.strip(),
            requested_by=str(get_current_user().username),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
    except EvidenceOperationsError as exc:
        return _operations_error(request, exc)
    except (EvidenceIndexManagerError, OntologyMutationError) as exc:
        return _coded_error(request, exc)
    logger.info(
        "ontology_evidence.rebuild.requested workspace_id=%s ontology_id=%s job_id=%s operator=%s",
        workspace_id,
        ontology_id,
        job["job_id"],
        get_current_user().username,
    )
    return job, (200 if job["duplicate"] else 202)


def _cleanup_core(request, *, kind: str, payload):
    workspace_id, failure = _workspace(request)
    if failure:
        return failure
    payload, failure = _validate_payload(
        request,
        payload,
        {"dry_run", "ontology_id", "older_than_days", "max_items", "reason"},
    )
    if failure:
        return failure
    normalized, failure = _validate_cleanup_payload(request, payload, kind=kind)
    if failure:
        return failure
    dry_run = normalized["dry_run"]
    idempotency_key: Optional[str] = None
    if not dry_run:
        idempotency_key, failure = _idempotency_key(request, required=True)
        if failure:
            return failure
    paths = get_workspace_paths(workspace_id)
    operation = "static_cleanup" if kind == "static" else "runtime_cleanup"
    request_hash = _request_hash({"kind": kind, **normalized})
    operations: Optional[EvidenceOperationsStore] = None
    if not dry_run:
        operations = EvidenceOperationsStore(
            paths.runtime_dir / "ontology-evidence" / "operations.sqlite3"
        )
        try:
            created, previous = operations.reserve_result(
                workspace_id=workspace_id,
                operation=operation,
                idempotency_key=str(idempotency_key),
                request_hash=request_hash,
                result_ref="cleanup_" + uuid.uuid4().hex,
            )
        except EvidenceOperationsError as exc:
            return _operations_error(request, exc)
        if not created:
            if previous is None:
                return _error(request, "INDEX_BUILDING", "相同幂等键的清理请求仍在执行。", 503, True, 1000)
            previous["duplicate"] = True
            return previous, 200

    try:
        result = _execute_cleanup(paths.runtime_dir, workspace_id=workspace_id, kind=kind, payload=normalized)
        result["duplicate"] = False
        if operations is not None:
            operations.complete_result(
                workspace_id=workspace_id,
                operation=operation,
                idempotency_key=str(idempotency_key),
                result=result,
            )
    except Exception as exc:
        if operations is not None:
            operations.release_result(
                workspace_id=workspace_id,
                operation=operation,
                idempotency_key=str(idempotency_key),
            )
        if isinstance(exc, (EvidenceIndexManagerError, EvidenceOperationsError)):
            return _coded_error(request, exc)
        logger.warning("Evidence cleanup failed", exc_info=True)
        return _error(request, "EVIDENCE_STORAGE_INCONSISTENT", "证据清理失败。", 503, True, 1000)
    logger.info(
        "ontology_evidence.cleanup.executed workspace_id=%s kind=%s dry_run=%s operator=%s",
        workspace_id,
        kind,
        dry_run,
        get_current_user().username,
    )
    return result, 200


def _execute_cleanup(
    runtime_dir: Path,
    *,
    workspace_id: str,
    kind: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    evidence_root = runtime_dir / "ontology-evidence"
    if kind == "static":
        if not evidence_root.is_dir():
            return _empty_static_cleanup(workspace_id, payload["dry_run"])
        return EvidenceIndexManager(runtime_dir).cleanup(
            workspace_id=workspace_id,
            ontology_id=payload["ontology_id"],
            dry_run=payload["dry_run"],
            older_than_days=payload["older_than_days"],
            max_items=payload["max_items"],
        )
    facts_path = evidence_root / "runtime-facts.sqlite3"
    if not facts_path.is_file():
        return {
            "contract": "EvidenceCleanup/v1",
            "dry_run": payload["dry_run"],
            "workspace_id": workspace_id,
            "kind": "runtime_facts",
            "summary": {"candidate_items": 0, "deleted_items": 0},
            "items": [],
        }
    return RuntimeFactsStore(facts_path).cleanup(
        workspace_id=workspace_id,
        ontology_id=payload["ontology_id"],
        dry_run=payload["dry_run"],
        older_than_days=payload["older_than_days"],
        max_items=payload["max_items"],
    )


def _get_ontology(workspace_id: str, ontology_id: str):
    with workspace_scope(workspace_id):
        registry = get_ontology_registry()
        registry.refresh()
        return registry.get_ontology(ontology_id)


def _generation_snapshot(
    runtime_dir: Path,
    workspace_id: str,
    ontology_id: str,
    pointer: Optional[dict[str, Any]],
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    if not active_pointer_is_serviceable(pointer):
        return None, None
    generation = str(pointer.get("active_generation") or "")
    if not _DIGEST_RE.fullmatch(generation):
        return None, None
    generation_dir = runtime_dir / "ontology-evidence" / "ontologies" / ontology_id / "generations" / generation
    try:
        metadata = SQLiteEvidenceIndex(generation_dir / "index.sqlite3").metadata()
    except (EvidenceIndexError, OSError, sqlite3.Error):
        return None, None
    if (
        str(metadata.get("workspace_id") or "") != workspace_id
        or str(metadata.get("ontology_id") or "") != ontology_id
    ):
        return None, None
    manifest: Optional[dict[str, Any]] = None
    manifest_path = generation_dir / "manifest.json"
    try:
        parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        pass
    return metadata, manifest


def _effective_status(
    *,
    config: EvidenceConfig,
    workspace_id: str,
    ontology: Any,
    pointer: Optional[dict[str, Any]],
    state: Optional[dict[str, Any]],
    latest_job: Optional[dict[str, Any]],
) -> str:
    if config.workspace_mode(workspace_id) == "off" or not bool(getattr(ontology, "enabled", False)):
        return "disabled"
    current_revision = int((state or {}).get("mutation_revision") or 0)
    if latest_job and int(
        latest_job.get("mutation_revision")
        if latest_job.get("mutation_revision") is not None
        else -1
    ) == current_revision:
        if latest_job.get("status") in {"queued", "building"}:
            return "building_base" if latest_job.get("status") == "building" else "queued"
        if latest_job.get("status") == "failed" and not pointer:
            return "base_failed"
    if pointer:
        if state is not None and int(
            pointer.get("mutation_revision")
            if pointer.get("mutation_revision") is not None
            else -1
        ) != current_revision:
            return "stale"
        status = str(pointer.get("status") or "stale")
        if status in {
            "disabled",
            "epoch_pending",
            "epoch_failed",
            "manual_required",
            "queued",
            "building_base",
            "lexical_ready",
            "building_semantic",
            "ready",
            "base_failed",
            "semantic_degraded",
            "stale",
        }:
            return status
    return "manual_required" if not config.auto_build_enabled else "epoch_pending"


def _build_status(
    pointer: Optional[dict[str, Any]],
    latest_job: Optional[dict[str, Any]],
    manifest: Optional[dict[str, Any]],
) -> dict[str, Any]:
    job_id = (latest_job or {}).get("job_id") or (pointer or {}).get("build_job_id")
    started_at = (latest_job or {}).get("started_at")
    completed_at = (latest_job or {}).get("completed_at") or (manifest or {}).get("built_at")
    duration_ms: Optional[int] = None
    if started_at and completed_at:
        try:
            duration_ms = max(
                0,
                int(
                    (datetime.fromisoformat(str(completed_at)) - datetime.fromisoformat(str(started_at))).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            pass
    return {
        "job_id": job_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "warnings": len((manifest or {}).get("warnings") or []),
        "error": (
            {
                "code": latest_job.get("error_code"),
                "message": latest_job.get("error_summary"),
            }
            if latest_job and latest_job.get("error_code")
            else None
        ),
    }


def _validate_cleanup_payload(request, payload: dict[str, Any], *, kind: str):
    dry_run = payload.get("dry_run", True)
    ontology_id = payload.get("ontology_id")
    min_days = STATIC_MIN_RETENTION_DAYS if kind == "static" else RUNTIME_FACT_MIN_RETENTION_DAYS
    older_than_days = payload.get("older_than_days", min_days)
    max_items = payload.get("max_items", 1000)
    reason = payload.get("reason")
    if not isinstance(dry_run, bool):
        return None, _error(request, "INVALID_ARGUMENTS", "dry_run 必须为 boolean。", 400)
    if ontology_id is not None and (not isinstance(ontology_id, str) or not _valid_ontology_id(ontology_id)):
        return None, _error(request, "INVALID_ARGUMENTS", "ontology_id 格式非法。", 400)
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < min_days:
        return None, _error(
            request,
            "INVALID_ARGUMENTS",
            "older_than_days 不能小于系统最小保留天数 {}。".format(min_days),
            400,
        )
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        return None, _error(request, "INVALID_ARGUMENTS", "max_items 必须为 1～5000。", 400)
    if not dry_run:
        if not isinstance(reason, str):
            return None, _error(request, "INVALID_ARGUMENTS", "执行清理时 reason 长度必须为 1～200。", 400)
        if not 1 <= len(reason.strip()) <= 200:
            return None, _error(request, "INVALID_ARGUMENTS", "执行清理时 reason 长度必须为 1～200。", 400)
    elif reason is not None:
        if not isinstance(reason, str):
            return None, _error(request, "INVALID_ARGUMENTS", "reason 最长 200 字。", 400)
        if len(reason.strip()) > 200:
            return None, _error(request, "INVALID_ARGUMENTS", "reason 最长 200 字。", 400)
    return {
        "dry_run": dry_run,
        "ontology_id": ontology_id,
        "older_than_days": older_than_days,
        "max_items": max_items,
        "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
    }, None


def _empty_static_cleanup(workspace_id: str, dry_run: bool) -> dict[str, Any]:
    return {
        "contract": "EvidenceCleanup/v1",
        "dry_run": bool(dry_run),
        "workspace_id": workspace_id,
        "summary": {
            "candidate_generations": 0,
            "candidate_epochs": 0,
            "candidate_bytes": 0,
            "protected_by_active": 0,
            "protected_by_retention": 0,
            "protected_by_reference": 0,
            "deleted_generations": 0,
            "deleted_bytes": 0,
        },
        "items": [],
    }


# ---------------------------------------------------------------------------
# Legacy Flask blueprint (tests/test_evidence_routes.py fixture)
# ---------------------------------------------------------------------------


@evidence_bp.route("/evidence/<evidence_id>", methods=["GET"])
@_flask_require_auth()
def lookup_evidence_flask(evidence_id: str):
    result, status = _lookup_evidence_core(flask_request, evidence_id)
    return jsonify(result), status


@evidence_bp.route("/ontologies/<ontology_id>/evidence-index/status", methods=["GET"])
@_flask_require_auth()
def evidence_index_status_flask(ontology_id: str):
    result, status = _evidence_index_status_core(flask_request, ontology_id)
    return jsonify(result), status


@evidence_bp.route("/ontologies/<ontology_id>/evidence-index/rebuild", methods=["POST"])
@_flask_require_auth(roles=["admin"])
def rebuild_evidence_index_flask(ontology_id: str):
    result, status = _rebuild_evidence_index_core(
        flask_request,
        ontology_id,
        flask_request.get_json(silent=True),
    )
    return jsonify(result), status


@evidence_bp.route("/ontology-evidence/cleanup", methods=["POST"])
@_flask_require_auth(roles=["admin"])
def cleanup_static_evidence_flask():
    result, status = _cleanup_core(
        flask_request,
        kind="static",
        payload=flask_request.get_json(silent=True),
    )
    return jsonify(result), status


@evidence_bp.route("/runtime-facts/cleanup", methods=["POST"])
@_flask_require_auth(roles=["admin"])
def cleanup_runtime_facts_flask():
    result, status = _cleanup_core(
        flask_request,
        kind="runtime",
        payload=flask_request.get_json(silent=True),
    )
    return jsonify(result), status


# ---------------------------------------------------------------------------
# FastAPI router (production, mounted by register_routers)
# ---------------------------------------------------------------------------


@evidence_router.get("/evidence/{evidence_id}")
def lookup_evidence_api(
    evidence_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    result, status = _lookup_evidence_core(request, evidence_id)
    return JSONResponse(result, status_code=status)


@evidence_router.get("/ontologies/{ontology_id}/evidence-index/status")
def evidence_index_status_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    result, status = _evidence_index_status_core(request, ontology_id)
    return JSONResponse(result, status_code=status)


@evidence_router.post("/ontologies/{ontology_id}/evidence-index/rebuild")
async def rebuild_evidence_index_api(
    ontology_id: str,
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    payload = await read_optional_json_object(request)
    result, status = _rebuild_evidence_index_core(request, ontology_id, payload)
    return JSONResponse(result, status_code=status)


@evidence_router.post("/ontology-evidence/cleanup")
async def cleanup_static_evidence_api(
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    payload = await read_optional_json_object(request)
    result, status = _cleanup_core(request, kind="static", payload=payload)
    return JSONResponse(result, status_code=status)


@evidence_router.post("/runtime-facts/cleanup")
async def cleanup_runtime_facts_api(
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    payload = await read_optional_json_object(request)
    result, status = _cleanup_core(request, kind="runtime", payload=payload)
    return JSONResponse(result, status_code=status)