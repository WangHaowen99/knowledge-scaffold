"""求解契约端到端验证记录与发布门禁。

验证记录按求解契约修订（solver_contract.check_workspace_solver 的 revision）绑定：
上一修订的通过记录不能用于当前修订，模型、Schema、绑定或评估器一改，门禁立即
要求重新验证。记录放在 .ontology-state/（不进 git、不进导出包、文件编辑器不可写），
只存状态、摘要与诊断引用，不存 Endpoint、Token 或连接信息。

记录的读写走后台任务公共件 JobStore（同一份 {"runs": [...]} 格式）。
"""

from __future__ import annotations

from pathlib import Path

from . import background_jobs, solver_contract

VALIDATIONS_FILE = ".ontology-state/solver-validations.json"
MAX_RECORDS = 50
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
CODE_INTERRUPTED = "solver_validation_interrupted"
INTERRUPTED_MESSAGE = "上一次验证被服务重启中断，请对当前修订重新验证"

def _store(root: Path) -> background_jobs.JobStore:
    return background_jobs.JobStore(
        Path(root) / VALIDATIONS_FILE,
        list_key="runs",
        max_records=MAX_RECORDS,
        terminal_statuses=(STATUS_PASSED, STATUS_FAILED),
    )


def list_records(root: Path) -> list[dict]:
    """最新的在前。"""
    return _store(root).list()


def create(
    root: Path, revision: str, solver_keys: list[str], by: str = "", contract_digest: str = "",
    **fields,
) -> dict:
    """登记一条验证记录；fields 覆盖默认值（回执直接带终态与结论）。"""
    record = {
        "revision": revision,
        "solver_keys": sorted(solver_keys),
        "status": STATUS_QUEUED,
        # 规范化契约总摘要（solver_contract.contract_digests 的 total），与 revision 的文件字节摘要互补
        "contract_digest": contract_digest,
        "request_digest": "",
        "result_digest": "",
        "authoritative": False,
        "candidate_count": None,
        "error_code": "",
        "message": "",
        "evidence": {},
        "by": by,
        **fields,
    }
    return _store(root).create(**record)


def update(root: Path, run_id: str, **fields) -> dict | None:
    return _store(root).update(run_id, **fields)


def sweep_interrupted(workspaces_dir: Path) -> list[str]:
    """进程启动时收敛孤儿记录：仍是 queued / running 的改判失败，返回有改动的项目 ID。"""
    return background_jobs.sweep_workspace_stores(
        workspaces_dir, VALIDATIONS_FILE, _store,
        status=STATUS_FAILED, error_code=CODE_INTERRUPTED, message=INTERRUPTED_MESSAGE,
    )


def latest(root: Path, revision: str | None = None) -> dict | None:
    for item in list_records(root):
        if revision is None or item.get("revision") == revision:
            return item
    return None


def is_accepted(record: dict | None) -> bool:
    """只有 Consumer 返回权威结果的通过记录才能过门禁。"""
    return bool(
        record
        and record.get("status") == STATUS_PASSED
        and record.get("authoritative") is True
    )


def gate(root: Path, revision: str) -> list[dict]:
    """当前修订的验证门禁；通过返回空列表，否则返回可直接展示的 finding。"""
    current = latest(root, revision)
    if is_accepted(current):
        return []
    if current is None:
        previous = latest(root)
        if previous is not None and is_accepted(previous):
            return [_finding(
                solver_contract.CODE_VALIDATION_STALE,
                "求解契约在通过验证后已修改，请对当前修订重新验证",
            )]
        return [_finding(
            solver_contract.CODE_VALIDATION_REQUIRED, "当前修订尚未完成 Consumer 端到端验证"
        )]
    status = current.get("status")
    if status in ACTIVE_STATUSES:
        return [_finding(
            solver_contract.CODE_VALIDATION_REQUIRED, "当前修订的端到端验证仍在进行中"
        )]
    if status == STATUS_PASSED:
        return [_finding(
            solver_contract.CODE_VALIDATION_FAILED, "验证结果不是 Consumer 的权威求解结果，不能发布"
        )]
    detail = current.get("message") or current.get("error_code") or "未知原因"
    return [_finding(
        solver_contract.CODE_VALIDATION_FAILED, f"当前修订的端到端验证未通过：{detail}"
    )]


def _finding(code: str, message: str) -> dict:
    return {
        "code": code,
        "message": message,
        "path": VALIDATIONS_FILE,
        "field": "",
        "severity": "error",
        "solver_key": "",
        "hint": solver_contract.FINDING_HINTS.get(code, ""),
    }
