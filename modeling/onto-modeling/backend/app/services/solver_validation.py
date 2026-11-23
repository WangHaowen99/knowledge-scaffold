"""求解契约端到端验证记录与发布门禁。

验证记录按求解契约修订（solver_contract.check_workspace_solver 的 revision）绑定：
上一修订的通过记录不能用于当前修订，模型、Schema、绑定或评估器一改，门禁立即
要求重新验证。记录放在 .ontology-state/（不进 git、不进导出包、文件编辑器不可写），
只存状态、摘要与诊断引用，不存 Endpoint、Token 或连接信息。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import solver_contract

VALIDATIONS_FILE = ".ontology-state/solver-validations.json"
# 建模侧原有的验证用例：已停止维护，只读导出给独立评测的求解验证集
CASES_FILE = ".ontology-state/solver-validation-cases.json"
MAX_RECORDS = 50
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

_lock = threading.Lock()


def _path(root: Path) -> Path:
    return Path(root) / VALIDATIONS_FILE


def list_records(root: Path) -> list[dict]:
    """最新的在前。"""
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    records = data.get("runs") if isinstance(data, dict) else None
    return [item for item in records or [] if isinstance(item, dict)]


def list_cases(root: Path) -> list[dict]:
    try:
        data = json.loads((Path(root) / CASES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cases = data.get("cases") if isinstance(data, dict) else None
    return [item for item in cases or [] if isinstance(item, dict)]


def _save(root: Path, records: list[dict]) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".solver-validations-")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump({"runs": records[:MAX_RECORDS]}, handle, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    finally:
        if fd is not None:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def create(
    root: Path, revision: str, solver_keys: list[str], by: str = "", contract_digest: str = ""
) -> dict:
    record = {
        "id": uuid.uuid4().hex,
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
        "created_at": int(time.time()),
        "completed_at": None,
    }
    with _lock:
        _save(root, [record, *list_records(root)])
    return record


def update(root: Path, run_id: str, **fields) -> dict | None:
    with _lock:
        records = list_records(root)
        for item in records:
            if item.get("id") == run_id:
                item.update(fields)
                if fields.get("status") in (STATUS_PASSED, STATUS_FAILED):
                    item["completed_at"] = int(time.time())
                _save(root, records)
                return item
    return None


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
