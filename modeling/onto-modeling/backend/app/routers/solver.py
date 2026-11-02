"""求解契约（API 契约 3.18）：SV-01 读取、SV-02 保存、SV-03 检查（不落盘）、SV-04 验证回执。

写入回执的是 X-04（服务令牌，routers/contracts.py）；验证候选包也在那里。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..api import deps, views
from ..api.errors import ApiError
from ..services import mainline, solver, solver_contract, solver_validation

router = APIRouter(prefix="/api/v1/projects/{project_id}/solver", tags=["solver"])

SIDECAR_ROLES = {
    "model.yaml": "entry",
    "input.schema.json": "input_schema",
    "result.schema.json": "result_schema",
    "fact-snapshot.schema.json": "fact_snapshot_schema",
    "presentation.yaml": "presentation",
}
_RECEIPT_STATUS = {
    "solver_receipt_required": "required",
    "solver_receipt_stale": "stale",
    "solver_receipt_failed": "failed",
}


def revision_no(root) -> int:
    """给人看的修订号（界面 r8）：受管目录在主线上被提交改动的次数。"""
    out = mainline._git(root, "rev-list", "--count", "--first-parent", "HEAD", "--", solver_contract.SOLVER_DIR)
    return int(out) if out.isdigit() else 0


def receipt_gate(root, revision: str) -> dict:
    """当前修订的回执门禁：pass / required / stale / failed，加不通过时的 finding。"""
    findings = [views.finding(item) for item in solver_validation.gate(root, revision)] if revision else []
    if not findings:
        return {"status": "pass", "finding": None}
    return {"status": _RECEIPT_STATUS.get(findings[0]["code"], "failed"), "finding": findings[0]}


def receipt(record: dict | None, root) -> dict | None:
    if not record:
        return None
    view = solver_validation.receipt_view(record)
    view["revision_no"] = revision_no(root) if record.get("revision") else None
    return view


def _pair(values) -> list[str] | None:
    """两项文字（选项显示名称、结果文字）：都空时为 null，否则按顺序给两项（空的给空串）。"""
    items = [str(value or "").strip() for value in values]
    return items if any(items) else None


def _evaluator(root, evaluator: str) -> dict | None:
    """契约 7.12 evaluator：{path, kind}；kind 按评估器源码判断（声明式 declarative，旧式 script）。"""
    if not evaluator:
        return None
    summary = solver_contract.evaluator_summary(root / evaluator)
    return {"path": evaluator, "kind": (summary or {}).get("kind") or "script"}


def _operation(item: dict, catalog: dict, root) -> dict:
    binding = item.get("binding") or {}
    declaration = binding.get("declaration") or {}
    family = str((declaration.get("engine_requirements") or {}).get("problem_family") or "")
    params = item.get("template_params") or {}
    mapping = params.get("display_mapping") or {}
    evaluator = item.get("evaluator") or ""
    return {
        "solver_key": item["solver_key"],
        "operation": {"kind": "operation", "id": f"operation:{item['operation']}", "key": item["operation"], "name": item["operation"]},
        "enabled": item["enabled"],
        "mode": "template" if item.get("managed") else "manual",
        "problem_family": family,
        "capabilities": list((catalog.get(family) or {}).get("capabilities") or []),
        "field_units": [
            {"field": str(unit.get("field", "")), "unit": str(unit.get("unit", "")), "note": str(unit.get("note", ""))}
            for unit in binding.get("units") or [] if isinstance(unit, dict)
        ],
        "evaluator": _evaluator(root, evaluator),
        # 模板参数还原出来的显示文字（契约 7.12）：选项显示名称、结果标题、结果文字
        "option_labels": _pair(mapping.get(option) for option in solver_contract.ORDERED_ROUTE_OPTIONS),
        "result_title": str(params.get("title") or "").strip() or None,
        "result_texts": _pair(params.get(name) for name in solver_contract.TEXT_PRESENTATION_FIELDS),
        "sidecars": {SIDECAR_ROLES.get(name, name): text for name, text in (item.get("sidecars") or {}).items()},
        "managed": sorted(SIDECAR_ROLES.values()) if item.get("managed") else [],
        # 面板编辑要原样回传：binding 全量与还原的模板参数（契约补充，见 contract-changes）
        "binding": binding,
        "template_params": params,
    }


def contract_view(project_id: str) -> dict:
    root = deps.workspaces.project_root(project_id)
    data = solver.solver_contract_view(deps.workspaces, project_id)
    catalog = {item["id"]: item for item in data["problem_families"]}
    revision = data["revision"] or ""
    gate = receipt_gate(root, revision) if data["enabled"] else {"status": "pass", "finding": None}
    return {
        "revision": revision,
        "revision_no": revision_no(root),
        "enabled": data["enabled"],
        "operations": [_operation(item, catalog, root) for item in data["contract"]["operations"]],
        "findings": [views.finding(item) for item in data["findings"]],
        "gate": [views.finding(item) for item in data["gate"]],
        "receipt": {"status": gate["status"], "latest": receipt(data.get("validation"), root)},
        "available_operations": [
            {"kind": "operation", "id": f"operation:{stem}", "key": stem, "name": stem}
            for stem in data["available_operations"]
        ],
        "evaluators": data["evaluator_options"],
        "problem_families": [
            {"id": item["id"], "label": item.get("label") or item["id"], "capabilities": item["capabilities"],
             "aliases": item.get("aliases") or []}
            for item in data["problem_families"]
        ],
        "supported_profiles": data["supported_profiles"],
        "template": data["template"],
        "lock": _solver_lock(project_id),
    }


def _solver_lock(project_id: str) -> dict | None:
    """写入范围锁里有求解契约（受管目录 ontology/solver/）时给出整份锁（方案 C10）。"""
    from ..kernel.changesets import service as changesets

    scope = changesets.scope_lock(deps.workspaces, project_id)
    hit = any(ref.get("type") == "codegen.solver_contract" for ref in scope.get("artifacts") or []) or any(
        rel.startswith("ontology/solver/") for rel in scope.get("paths") or [])
    return scope if hit else None


def _operations(body: dict) -> list:
    operations = (body.get("contract") or {}).get("operations") if isinstance(body.get("contract"), dict) else None
    if not isinstance(operations, list):
        raise ApiError("solver_contract_invalid", "contract.operations 必须是列表", 400,
                       {"findings": [], "fields": {"contract.operations": "list"}})
    return operations


@router.get("/contract")
def get_contract(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    return contract_view(project_id)


@router.put("/contract")
async def save_contract(project_id: str, request: Request):
    """整体保存（受管目录全量替换），修订号加 1，旧修订的回执对新修订不再有效。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    operations = _operations(body)
    try:
        solver.save_solver_contract(
            deps.workspaces, project_id, operations, str(body.get("base_revision") or ""),
            author=deps.username(request), summary=str(body.get("summary") or "")[:80],
        )
    except solver_contract.SolverContractRejected as exc:
        findings = [views.finding(solver_contract.finding(problem)) for problem in exc.problems]
        raise ApiError("solver_contract_rejected", str(exc), 422, {"findings": findings}) from exc
    state = mainline.state(root)
    return {**contract_view(project_id), "commit": state["head"], "version_label": state["version_label"]}


@router.post("/contract/check")
async def check_contract(project_id: str, request: Request):
    deps.require_owner(project_id, request)
    body = await deps.json_body(request)
    result = solver.check_solver_contract_draft(deps.workspaces, project_id, _operations(body))
    return {
        "rejected": [views.finding(item) for item in result["rejected"]],
        "findings": [views.finding(item) for item in result["findings"]],
        "rendered": result["rendered"],
        "evaluator": result["evaluator"],
    }


@router.get("/receipts")
def list_receipts(project_id: str, request: Request, revision: str = "", cursor: str = "", limit: int = 0):
    """SV-04：当前修订（或指定修订）的验证回执与门禁状态；回执由评测服务经 X-04 写入。"""
    root = deps.require_owner(project_id, request)
    current = solver.solver_check(deps.workspaces, project_id).revision or ""
    revision = revision or current
    records = [item for item in solver_validation.list_records(root) if item.get("revision") == revision]
    page = deps.page([receipt(item, root) for item in records], cursor, limit, {"revision": revision})
    return {
        "revision": revision,
        "revision_no": revision_no(root),
        "latest": receipt(records[0], root) if records else None,
        "items": page["items"],
        "next_cursor": page["next_cursor"],
        "gate": receipt_gate(root, revision),
    }
