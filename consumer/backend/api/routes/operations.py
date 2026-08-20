"""HTTP API for the operation-evolution buffer and staged patches.

Routes (all under `/agentic/operations`):

- GET    /<onto>/<op>/proposals                    list aggregates
- GET    /proposals                                cross-op aggregate index
- GET    /proposals/export                         export aggregates + raw records as JSON
- POST   /<onto>/<op>/proposals/<fp>/endorse       👍
- POST   /<onto>/<op>/proposals/<fp>/reject        🛑 (status → rejected)
- PATCH  /<onto>/<op>/proposals/<fp>               edit canonical_content
- POST   /<onto>/<op>/rescan                       manual patcher trigger
- GET    /pending                                  cross-op pending staged list
- GET    /<onto>/<op>/staged                       fetch staged + diff
- POST   /<onto>/<op>/staged/apply                 admin: apply staged → .md
- POST   /<onto>/<op>/staged/reject                admin: drop staged

Reads are open to any authenticated user; staged apply/reject require admin.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from config import LimitsConfig
from agent.evolution import (
    get_operation_patcher,
    get_proposal_buffer,
    list_pending_staged,
    resolve_operation_md,
    staged_paths_for,
)
from api.dependencies import (
    require_admin_workspace_id,
    require_workspace_id,
    run_in_runtime_pool,
)
from api.request_parsing import read_optional_json_object
from utils.logging import get_logger

logger = get_logger("routes.operations")

operations_router = APIRouter(prefix="/agentic/operations")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aggregate_to_dict(agg) -> Dict[str, Any]:
    return agg.to_dict()


def _proposal_to_dict(proposal) -> Dict[str, Any]:
    return proposal.to_dict()


def _read_meta(meta_path: Path) -> Dict[str, Any]:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _parse_statuses(request: Request, default: set) -> set:
    raw_filter = (request.query_params.get("status") or "").strip()
    if raw_filter.lower() == "all":
        return {"pending", "promoted", "rejected", "applied"}
    if raw_filter:
        return {s.strip().lower() for s in raw_filter.split(",") if s.strip()}
    return set(default)


def _operation_summary(
    *,
    workspace_id: str,
    ontology_id: str,
    operation_key: str,
    aggregates: List[Any],
) -> Dict[str, Any]:
    status_counts: Dict[str, int] = {}
    kind_counts: Dict[str, int] = {}
    source_counts: Dict[str, int] = {}
    total_occurrences = 0
    total_active_endorsements = 0
    total_applied_count = 0
    total_rejection_count = 0
    first_seen = None
    latest_seen = None

    for agg in aggregates:
        status_counts[agg.status] = status_counts.get(agg.status, 0) + 1
        kind_counts[agg.kind] = kind_counts.get(agg.kind, 0) + 1
        for source, count in (agg.sources or {}).items():
            source_counts[source] = source_counts.get(source, 0) + int(count)
        total_occurrences += int(agg.occurrences)
        total_active_endorsements += int(agg.active_endorsements)
        total_applied_count += int(agg.applied_count)
        total_rejection_count += int(agg.rejection_count)
        first_seen = agg.first_seen if first_seen is None else min(first_seen, agg.first_seen)
        latest_seen = agg.last_seen if latest_seen is None else max(latest_seen, agg.last_seen)

    top = max(
        aggregates,
        key=lambda a: (
            a.status == "promoted",
            a.active_endorsements,
            a.applied_count,
            a.occurrences,
            a.last_seen,
        ),
    )
    staged_paths = staged_paths_for(ontology_id, operation_key, workspace_id=workspace_id)
    staged_path = staged_paths[0] if staged_paths is not None else None
    return {
        "workspace_id": workspace_id,
        "ontology_id": ontology_id,
        "operation_key": operation_key,
        "aggregate_count": len(aggregates),
        "status_counts": status_counts,
        "kind_counts": kind_counts,
        "source_counts": source_counts,
        "occurrences": total_occurrences,
        "active_endorsements": total_active_endorsements,
        "applied_count": total_applied_count,
        "rejection_count": total_rejection_count,
        "first_seen": first_seen,
        "last_seen": latest_seen,
        "top_fingerprint": top.fingerprint,
        "top_kind": top.kind,
        "top_status": top.status,
        "top_content": top.canonical_content,
        "has_staged": bool(staged_path and staged_path.exists()),
    }


# ---------------------------------------------------------------------------
# Proposals API
# ---------------------------------------------------------------------------


@operations_router.get("/proposals")
def list_operation_proposals(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """Cross-operation index of aggregate snapshots for the active workspace."""
    statuses = _parse_statuses(request, {"pending", "promoted", "rejected"})
    buf = get_proposal_buffer()
    items: List[Dict[str, Any]] = []
    aggregate_count = 0
    for ws, ontology_id, operation_key in buf.iter_op_dirs():
        if ws != workspace_id:
            continue
        aggs = buf.list_aggregates(
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
            statuses=statuses,
        )
        if not aggs:
            continue
        aggregate_count += len(aggs)
        items.append(
            _operation_summary(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                operation_key=operation_key,
                aggregates=aggs,
            )
        )
    items.sort(key=lambda item: item.get("last_seen") or 0, reverse=True)
    return {
        "count": len(items),
        "aggregate_count": aggregate_count,
        "items": items,
    }


@operations_router.get("/proposals/export")
def export_operation_proposals(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """Export operation-evolution proposals for the active workspace as JSON."""
    statuses = _parse_statuses(
        request,
        {"pending", "promoted", "rejected", "applied"},
    )
    try:
        records_limit = int(request.query_params.get("records_limit") or 1000)
    except ValueError:
        records_limit = 1000
    records_limit = max(0, min(records_limit, 10000))

    buf = get_proposal_buffer()
    operations: List[Dict[str, Any]] = []
    aggregate_count = 0
    record_count = 0

    for ws, ontology_id, operation_key in buf.iter_op_dirs():
        if ws != workspace_id:
            continue
        aggs = buf.list_aggregates(
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
            statuses=statuses,
        )
        if not aggs:
            continue
        records = buf.list_proposal_records(
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
            limit=records_limit,
        )
        wanted_fps = {a.fingerprint for a in aggs}
        proposal_records = [
            _proposal_to_dict(record)
            for record in records
            if record.fingerprint in wanted_fps
        ]
        aggregate_count += len(aggs)
        record_count += len(proposal_records)
        operations.append({
            "summary": _operation_summary(
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                operation_key=operation_key,
                aggregates=aggs,
            ),
            "proposals": [_aggregate_to_dict(agg) for agg in aggs],
            "proposal_records": proposal_records,
        })

    operations.sort(key=lambda item: item["summary"].get("last_seen") or 0, reverse=True)
    return {
        "schema": "operation-evolution-proposals.v1",
        "exported_at": time.time(),
        "workspace_id": workspace_id,
        "status_filter": sorted(statuses),
        "operation_count": len(operations),
        "aggregate_count": aggregate_count,
        "record_count": record_count,
        "operations": operations,
    }


@operations_router.get("/{ontology_id}/{operation_key}/proposals")
def list_proposals(
    ontology_id: str,
    operation_key: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """List aggregates for an operation. Filter via `?status=pending,promoted`.

    Default returns everything except `applied` (already merged) so the UI can
    focus on what the human still needs to act on.
    """
    statuses = _parse_statuses(request, {"pending", "promoted", "rejected"})
    buf = get_proposal_buffer()
    aggs = buf.list_aggregates(
        ontology_id,
        operation_key,
        workspace_id=workspace_id,
        statuses=statuses,
    )
    payload = {
        "ontology_id": ontology_id,
        "operation_key": operation_key,
        "proposals": [_aggregate_to_dict(a) for a in aggs],
    }
    include_records = (request.query_params.get("include_records") or "").strip().lower()
    if include_records in {"1", "true", "yes"}:
        try:
            limit = int(request.query_params.get("records_limit") or 200)
        except ValueError:
            limit = 200
        records = buf.list_proposal_records(
            ontology_id,
            operation_key,
            workspace_id=workspace_id,
            limit=limit,
        )
        wanted_fps = {a.fingerprint for a in aggs}
        payload["proposal_records"] = [
            _proposal_to_dict(p) for p in records if p.fingerprint in wanted_fps
        ]
    return payload


@operations_router.post(
    "/{ontology_id}/{operation_key}/proposals/{fingerprint}/endorse",
)
def endorse_proposal(
    ontology_id: str,
    operation_key: str,
    fingerprint: str,
    workspace_id: str = Depends(require_workspace_id),
):
    buf = get_proposal_buffer()
    buf.endorse(
        ontology_id,
        operation_key,
        fingerprint,
        workspace_id=workspace_id,
    )
    return {"success": True}


@operations_router.post(
    "/{ontology_id}/{operation_key}/proposals/{fingerprint}/reject",
)
def reject_proposal(
    ontology_id: str,
    operation_key: str,
    fingerprint: str,
    workspace_id: str = Depends(require_workspace_id),
):
    buf = get_proposal_buffer()
    buf.reject(
        ontology_id,
        operation_key,
        fingerprint,
        workspace_id=workspace_id,
    )
    return {"success": True}


@operations_router.patch(
    "/{ontology_id}/{operation_key}/proposals/{fingerprint}",
)
async def edit_proposal(
    ontology_id: str,
    operation_key: str,
    fingerprint: str,
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    data = await read_optional_json_object(request)
    new_content = str(data.get("content") or "").strip()
    if len(new_content) < 4:
        return JSONResponse(
            {"error": "content must be at least 4 chars"},
            status_code=400,
        )
    buf = get_proposal_buffer()
    try:
        agg = await run_in_runtime_pool(
            request,
            "short_io",
            buf.edit,
            ontology_id,
            operation_key,
            fingerprint,
            new_content,
            workspace_id=workspace_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if agg is None:
        return JSONResponse({"error": "Proposal not found"}, status_code=404)
    return {"success": True, "aggregate": _aggregate_to_dict(agg)}


@operations_router.post("/{ontology_id}/{operation_key}/rescan")
def rescan_operation(
    ontology_id: str,
    operation_key: str,
    workspace_id: str = Depends(require_workspace_id),
):
    """Manual trigger for the patcher to re-evaluate this op now."""
    patcher = get_operation_patcher()
    if patcher is None:
        return JSONResponse({"error": "Patcher not running"}, status_code=503)
    patcher.request_rescan(
        ontology_id,
        operation_key,
        workspace_id=workspace_id,
    )
    return {"success": True, "queued": True}


# ---------------------------------------------------------------------------
# Staged API
# ---------------------------------------------------------------------------


@operations_router.get("/pending")
def list_pending(
    workspace_id: str = Depends(require_workspace_id),
):
    """Cross-op list of staged patches awaiting admin review. Drives the badge."""
    items = list_pending_staged(workspace_id=workspace_id)
    return {"count": len(items), "items": items}


@operations_router.get("/{ontology_id}/{operation_key}/staged")
def get_staged(
    ontology_id: str,
    operation_key: str,
    workspace_id: str = Depends(require_workspace_id),
):
    """Return original + staged content + metadata for the diff viewer."""
    paths = staged_paths_for(ontology_id, operation_key, workspace_id=workspace_id)
    if paths is None:
        return JSONResponse(
            {"error": "Operation .md not found"},
            status_code=404,
        )
    staged_path, meta_path = paths
    if not staged_path.exists():
        return JSONResponse({"error": "No staged patch"}, status_code=404)
    op_path = resolve_operation_md(ontology_id, operation_key, workspace_id=workspace_id)
    original = op_path.read_text(encoding="utf-8") if op_path and op_path.exists() else ""
    staged = staged_path.read_text(encoding="utf-8")
    meta = _read_meta(meta_path)
    return {
        "ontology_id": ontology_id,
        "operation_key": operation_key,
        "original": original,
        "staged": staged,
        "meta": meta,
    }


def _archive_original(op_path: Path) -> Path:
    """Snapshot the current `.md` to `applied/<op>-<ts>.md` so apply is reversible."""
    archive_dir = op_path.parent / "applied"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    target = archive_dir / "{}-{}.md".format(op_path.stem, ts)
    shutil.copy2(op_path, target)
    return target


@operations_router.post("/{ontology_id}/{operation_key}/staged/apply")
def apply_staged(
    ontology_id: str,
    operation_key: str,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    """Promote staged → live `.md`. Admin only. Archives previous `.md` and
    transitions the merged proposals to status=applied."""
    paths = staged_paths_for(ontology_id, operation_key, workspace_id=workspace_id)
    if paths is None:
        return JSONResponse(
            {"error": "Operation .md not found"},
            status_code=404,
        )
    staged_path, meta_path = paths
    if not staged_path.exists():
        return JSONResponse({"error": "No staged patch"}, status_code=404)
    op_path = resolve_operation_md(ontology_id, operation_key, workspace_id=workspace_id)
    if op_path is None:
        return JSONResponse({"error": "Operation .md missing"}, status_code=404)

    meta = _read_meta(meta_path)
    fingerprints: List[str] = list(meta.get("fingerprints") or [])
    expected_sha = str(meta.get("original_sha") or "").strip()

    archived_to: Optional[Path] = None
    try:
        current_content = op_path.read_text(encoding="utf-8")
        if expected_sha and _sha256_text(current_content) != expected_sha:
            return JSONResponse(
                {
                    "error": "Operation .md changed since staged patch was created",
                    "expected_original_sha": expected_sha,
                    "current_sha": _sha256_text(current_content),
                },
                status_code=409,
            )
        new_content = staged_path.read_text(encoding="utf-8")
        max_chars = LimitsConfig.from_env().operation_max_chars
        if len(new_content) > max_chars:
            return JSONResponse(
                {
                    "error": "单个逻辑长度超过上限: {} / {} 字符".format(
                        len(new_content),
                        max_chars,
                    )
                },
                status_code=400,
            )
        archived_to = _archive_original(op_path)
        op_path.write_text(new_content, encoding="utf-8")
    except Exception as exc:
        logger.exception("apply_staged failed for %s/%s", ontology_id, operation_key)
        return JSONResponse(
            {"error": "Apply failed: {}".format(exc)},
            status_code=500,
        )

    # Drop staged + meta only after successful write.
    try:
        staged_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    except Exception:
        logger.warning("apply_staged: failed to clean staged files", exc_info=True)

    # Transition proposals to applied + archive.
    if fingerprints:
        try:
            get_proposal_buffer().archive_applied(
                ontology_id,
                operation_key,
                fingerprints,
                workspace_id=workspace_id,
            )
        except Exception:
            logger.exception("apply_staged: archive_applied failed")

    return {
        "success": True,
        "applied": True,
        "archived_to": str(archived_to) if archived_to else None,
        "fingerprints_applied": fingerprints,
    }


def _reject_staged(
    *,
    ontology_id: str,
    operation_key: str,
    workspace_id: str,
    reject_proposals: bool,
):
    paths = staged_paths_for(ontology_id, operation_key, workspace_id=workspace_id)
    if paths is None:
        return JSONResponse(
            {"error": "Operation .md not found"},
            status_code=404,
        )
    staged_path, meta_path = paths
    if not staged_path.exists():
        return JSONResponse({"error": "No staged patch"}, status_code=404)

    meta = _read_meta(meta_path)
    fingerprints: List[str] = list(meta.get("fingerprints") or [])

    try:
        staged_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
    except Exception:
        logger.warning("reject_staged: failed to clean staged files", exc_info=True)

    buf = get_proposal_buffer()
    if fingerprints:
        if reject_proposals:
            for fp in fingerprints:
                try:
                    buf.reject(ontology_id, operation_key, fp, workspace_id=workspace_id)
                except Exception:
                    logger.warning(
                        "reject_staged: buf.reject failed for fp=%s", fp, exc_info=True
                    )
        else:
            try:
                buf.revert_promoted(
                    ontology_id,
                    operation_key,
                    fingerprints,
                    workspace_id=workspace_id,
                )
            except Exception:
                logger.warning("reject_staged: revert_promoted failed", exc_info=True)

    return {
        "success": True,
        "rejected": True,
        "fingerprints_rejected": fingerprints if reject_proposals else [],
    }


@operations_router.post("/{ontology_id}/{operation_key}/staged/reject")
async def reject_staged(
    ontology_id: str,
    operation_key: str,
    request: Request,
    workspace_id: str = Depends(require_admin_workspace_id),
):
    """Drop the staged patch and optionally reject its merged proposals."""
    body = await read_optional_json_object(request)
    return await run_in_runtime_pool(
        request,
        "short_io",
        _reject_staged,
        ontology_id=ontology_id,
        operation_key=operation_key,
        workspace_id=workspace_id,
        reject_proposals=bool(body.get("reject_proposals", True)),
    )
