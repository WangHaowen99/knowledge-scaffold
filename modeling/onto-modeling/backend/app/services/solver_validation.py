"""求解契约端到端验证记录与发布门禁。

验证记录按求解契约修订（solver_contract.check_workspace_solver 的 revision）绑定：
上一修订的通过记录不能用于当前修订，模型、Schema、绑定或评估器一改，门禁立即
要求重新验证。记录放在 .ontology-state/（不进 git、不进导出包、文件编辑器不可写），
只存状态、摘要与诊断引用，不存 Endpoint、Token 或连接信息。

端到端验证本身由外部验证方完成（详细设计第 11 章），结论经验证回执入口
（POST /api/v1/projects/{id}/solver/receipts，服务令牌）写进来；这里只管记录与门禁。
记录的读写走后台任务公共件 JobStore（同一份 {"runs": [...]} 格式）。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
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

# 验证回执（三份契约之「验证回执入」，API 契约 X-04）
RECEIPT_STATUSES = (STATUS_PASSED, STATUS_FAILED)
CODE_INVALID_ARGUMENT = "invalid_argument"
CODE_REVISION_UNKNOWN = "receipt_revision_unknown"
MAX_MESSAGE = 300
MAX_EVIDENCE_BYTES = 64 * 1024
_RECEIPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{0,80}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_receipt_lock = threading.Lock()


class ReceiptRejected(Exception):
    def __init__(self, message: str, status: int = 400, code: str = CODE_INVALID_ARGUMENT, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


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


# ---- 验证回执 ----


def safe_message(message: object) -> str:
    """验证方的报错可能带地址；落记录前统一脱敏截断。"""
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[地址已隐藏]", str(message or ""), flags=re.I)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "[地址已隐藏]", text)
    return text[:MAX_MESSAGE]


def _invalid(field: str, reason: str) -> ReceiptRejected:
    return ReceiptRejected(f"{field}：{reason}", 400, CODE_INVALID_ARGUMENT, {"fields": {field: reason}})


def _text(payload: dict, field: str, *, required: bool = False, limit: int = 128) -> str:
    value = payload.get(field)
    if value is None or value == "":
        if required:
            raise _invalid(field, "必填")
        return ""
    if not isinstance(value, str):
        raise _invalid(field, "必须是字符串")
    return value.strip()[:limit]


def _epoch(value: str, field: str) -> int:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(field, "必须是 RFC 3339 时间") from None
    if moment.tzinfo is None:
        raise _invalid(field, "必须带时区（UTC 以 Z 结尾）")
    return int(moment.timestamp())


def normalize_receipt(payload) -> dict:
    """回执请求体（API 契约 X-04）→ 记录字段。只收结论、摘要和诊断引用，不收地址与凭据。"""
    if not isinstance(payload, dict):
        raise ReceiptRejected("请求体必须是 JSON 对象", 400, CODE_INVALID_ARGUMENT)
    fields = {field: _text(payload, field, required=True) for field in (
        "revision", "contract_digest", "request_digest", "result_digest", "receipt_id",
    )}
    if not _RECEIPT_ID_RE.match(fields["receipt_id"]):
        raise _invalid("receipt_id", "只能包含字母、数字、点、冒号、下划线与短横线，最长 128 个字符")
    keys = payload.get("solver_keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key.strip() for key in keys):
        raise _invalid("solver_keys", "必须是非空的字符串数组")
    status = payload.get("status")
    if status not in RECEIPT_STATUSES:
        raise _invalid("status", f"必须是 {'、'.join(RECEIPT_STATUSES)} 之一")
    authoritative = payload.get("authoritative")
    if not isinstance(authoritative, bool):
        raise _invalid("authoritative", "必须是布尔值")
    release_version = _text(payload, "release_version", limit=32)
    if release_version and not _VERSION_RE.match(release_version):
        raise _invalid("release_version", "必须是发布版本号")
    error_code = _text(payload, "error_code", limit=80)
    if not _ERROR_CODE_RE.match(error_code):
        raise _invalid("error_code", "格式非法")
    count = payload.get("candidate_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise _invalid("candidate_count", "必须是非负整数")
    evidence = payload.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise _invalid("evidence", "必须是对象")
    if len(json.dumps(evidence, ensure_ascii=False).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise _invalid("evidence", f"不超过 {MAX_EVIDENCE_BYTES // 1024} KB，明细留在验证方，这里只放引用")
    message = payload.get("message")
    if message is not None and not isinstance(message, str):
        raise _invalid("message", "必须是字符串")
    return {
        **fields,
        "solver_keys": sorted({key.strip() for key in keys}),
        "status": status,
        "authoritative": authoritative,
        "release_version": release_version,
        "candidate_count": count,
        "error_code": error_code,
        "message": safe_message(message),
        "evidence": evidence,
        "completed_at": _epoch(_text(payload, "completed_at", required=True, limit=40), "completed_at"),
    }


def receive_receipt(manager, project_id: str, payload, by: str) -> tuple[dict, bool]:
    """写入一条验证回执；返回 (记录, 是否新建)。

    回执只能绑定当前修订（HEAD 提交或工作区的求解契约，见 solver_receipt_targets）：修订一变就要
    重新验证，不认识的修订返回 receipt_revision_unknown。solver_keys 必须覆盖该修订启用的全部求解操作，
    契约摘要必须一致。同一 receipt_id 只生效一次：重复提交返回第一次的记录。
    """
    fields = normalize_receipt(payload)
    root = manager.project_root(project_id)
    with _receipt_lock:
        existing = next(
            (item for item in list_records(root) if item.get("receipt_id") == fields["receipt_id"]), None
        )
        if existing is not None:
            return existing, False
        from . import publish, solver  # 发布与求解都依赖本模块（门禁读回执），在函数里导入避免循环

        check = solver.solver_receipt_targets(manager, project_id).get(fields["revision"])
        if check is None:
            raise ReceiptRejected(
                "回执里的求解契约修订不是本项目的当前修订，请对当前修订重新验证",
                422, CODE_REVISION_UNKNOWN, {"revision": fields["revision"]},
            )
        digest = str(check.digests.get("total") or "")
        if digest and fields["contract_digest"] != digest:
            raise _invalid("contract_digest", "与该修订的求解契约摘要不一致")
        enabled = sorted(item.solver_key for item in check.contracts if item.enabled)
        if fields["solver_keys"] != enabled:
            raise _invalid("solver_keys", f"必须正好是该修订启用的求解操作：{'、'.join(enabled)}")
        if fields["release_version"] and not any(
            item["version"] == fields["release_version"] for item in publish.releases(manager, project_id)
        ):
            raise _invalid("release_version", "不是本项目已有的发布版本")
        revision = fields.pop("revision")
        solver_keys = fields.pop("solver_keys")
        contract_digest = fields.pop("contract_digest")
        record = create(root, revision, solver_keys, by, contract_digest=contract_digest, **fields)
    return record, True


def receipt_view(record: dict) -> dict:
    """Receipt（X-04 响应）：记录原样，时间按 RFC 3339。"""
    view = dict(record)
    for field in ("created_at", "completed_at"):
        value = view.get(field)
        if isinstance(value, (int, float)):
            moment = datetime.fromtimestamp(value, timezone.utc)
            view[field] = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return view
