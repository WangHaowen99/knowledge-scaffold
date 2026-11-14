"""确定性 Operation 受管契约 API。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from ..api import deps, views
from ..api.errors import ApiError
from ..services import deterministic_contract, deterministic_verification, research_view

router = APIRouter(prefix="/api/v1/projects/{project_id}/deterministic", tags=["deterministic"])
PACK = "deterministic-execution"


def _root(project_id: str, request: Request, *, write: bool = False):
    root = deps.require_writable(project_id, request) if write else deps.require_owner(project_id, request)
    if not research_view.pack_enabled(root, PACK):
        raise ApiError("pack_not_enabled", "本项目没有启用确定性执行能力", 409, {"pack": PACK})
    return root


def _operations(body: dict) -> list:
    value = body.get("operations")
    if not isinstance(value, list):
        raise ApiError("deterministic_contract_invalid", "operations 必须是列表", 400,
                       {"fields": {"operations": "list"}})
    return value


def _view(root) -> dict:
    result = deterministic_contract.contract_view(root)
    return {**result, "findings": [views.finding(item) for item in result["findings"]]}


@router.get("/contract")
def get_contract(project_id: str, request: Request):
    return _view(_root(project_id, request))


@router.get("/candidates")
def get_candidates(project_id: str, request: Request):
    return deterministic_contract.candidate_scan(_root(project_id, request))


@router.post("/candidates/draft")
async def create_candidate_draft(project_id: str, request: Request):
    root = _root(project_id, request)
    body = await deps.json_body(request)
    operation = body.get("operation")
    handler = body.get("handler", "")
    data_access = body.get("data_access", "")
    if not all(isinstance(value, str) for value in (operation, handler, data_access)) or not operation.strip():
        raise ApiError(
            "deterministic_candidate_invalid", "operation 必须是非空字符串", 400,
            {"fields": {"operation": "string"}},
        )
    return deterministic_contract.candidate_draft(
        root, operation.strip(), handler.strip(), data_access.strip(),
    )


@router.post("/contract/check")
async def check_contract(project_id: str, request: Request):
    root = _root(project_id, request)
    body = await deps.json_body(request)
    check = deterministic_contract.check_payload_with_conflicts(root, _operations(body))
    return {
        "revision": check.revision,
        "findings": [views.finding(item) for item in check.findings],
        "ready": not check.findings,
    }


@router.put("/contract")
async def save_contract(project_id: str, request: Request):
    _root(project_id, request, write=True)
    body = await deps.json_body(request)
    try:
        result = deterministic_contract.save_contracts(
            deps.workspaces,
            project_id,
            _operations(body),
            str(body.get("base_revision") or ""),
            author=deps.username(request),
        )
    except deterministic_contract.DeterministicContractRejected as exc:
        raise ApiError(
            "deterministic_contract_rejected",
            str(exc),
            422,
            {"findings": [views.finding(item) for item in exc.findings]},
        ) from exc
    return {**result, "findings": [views.finding(item) for item in result["findings"]]}


@router.post("/contract/{key}/verify")
async def verify_contract(project_id: str, key: str, request: Request):
    """执行阶段不持项目锁；写报告时再复核完整来源摘要，避免验证覆盖并发修改。"""
    root = _root(project_id, request, write=True)
    body = await deps.json_body(request)
    expected_revision = str(body.get("base_revision") or "")
    source_revision, report = await asyncio.to_thread(
        deterministic_verification.verify, root, key, expected_revision,
    )
    result = await asyncio.to_thread(
        deterministic_contract.write_verification_report,
        deps.workspaces,
        project_id,
        key,
        source_revision,
        report,
        author=deps.username(request),
    )
    return result
