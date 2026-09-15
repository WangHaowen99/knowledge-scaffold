from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from openjiuwen.core.foundation.tool import Tool, ToolCard

from .core_client import CoreGateway
from .errors import AgentRuntimeError


EventSink = Callable[[str, dict[str, Any]], None]
StateAction = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
AfterCoreTool = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[None]]
ConfirmationGuard = Callable[[], bool]
ScopeConfirmationGate = Callable[[], bool]
ToolReceiptLookup = Callable[[str, str, dict[str, Any]], dict[str, Any] | None]
ToolReceiptSave = Callable[[str, str, dict[str, Any], dict[str, Any]], None]
ValidationRepairSave = Callable[[dict[str, Any]], None]
RequireTool = Callable[[str, dict[str, Any]], None]
ReleaseRequiredTool = Callable[[str], None]
ResolveIssues = Callable[[list[str]], None]
FULL_READ_PROGRESS_INTERVAL_SECONDS = 5.0
# Keep the outer tool deadline beyond the core client's 1800-second deadline.
FULL_READ_TOOL_TIMEOUT_SECONDS = 1830.0
EVALUATION_PENDING_MESSAGE = "正式评测任务已创建，可在评测界面查看实时进度。"
LEADER_DOCUMENT_PAGE_CHARS = 6_000
# Keep the default leader read deliberately bounded, while allowing read-only
# specialists to request a larger page when they are doing batch material
# analysis.  The previous shared value of 5 caused every role to re-read the
# same table page and made bulk analysis needlessly expensive.
DEFAULT_TABLE_SAMPLE_ROWS = 20
MATERIAL_ANALYST_TABLE_SAMPLE_ROWS = 50
TABLE_FIELD_SAMPLE_VALUES = 3
TABLE_SAMPLE_VALUE_CHARS = 120

VALIDATION_REPAIR_ERROR_TOOLS = frozenset({
    "stage_change_set",
    "validate_change_set",
    "write_file",
    "upsert_object",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "bind_object_sources",
    "import_zip",
    "stage_package_merge",
    "compile_package_fusion_plan",
    "create_object",
    "create_rule",
    "create_operation",
    "create_metric",
    "patch_object",
    "patch_rule",
    "patch_operation",
    "patch_metric",
    "sync_schema_objects",
    "stage_schema_semantic_changes",
    "confirm_schema_semantics",
    "patch_object_source_binding",
    "delete_domain_resource",
    "stage_script_source",
    "stage_script_patch",
})
VALIDATION_REPAIR_PROGRESS_TOOLS = frozenset({
    "stage_change_set",
    "validate_change_set",
    "write_file",
    "delete_file",
    "upsert_object",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "bind_object_sources",
    "import_zip",
    "stage_package_merge",
    "compile_package_fusion_plan",
    "create_object",
    "create_rule",
    "create_operation",
    "create_metric",
    "patch_object",
    "patch_rule",
    "patch_operation",
    "patch_metric",
    "sync_schema_objects",
    "stage_schema_semantic_changes",
    "confirm_schema_semantics",
    "patch_object_source_binding",
    "delete_domain_resource",
    "stage_script_source",
    "stage_script_patch",
})
VALIDATION_REPAIR_RESET_TOOLS = frozenset({
    "discard_change_set",
    "discard_rejected_candidate_changes",
    "request_confirmation",
})
VALIDATION_REPAIR_RESOLUTION_TOOLS = VALIDATION_REPAIR_RESET_TOOLS | {
    "validate_change_set",
}

TOOL_WORK_DETAILS = {
    "list_documents": "先盘点可用资料，再确定需要精读的范围",
    "read_document": "正在从原文提取与当前任务相关的事实和依据",
    "read_documents_fully": "正在核对全文覆盖范围，并整理可引用的事实",
    "search_documents": "正在定位相关原文，再与当前建模内容交叉核对",
    "list_tables": "先盘点可用数据表，再选择需要核对的字段范围",
    "describe_table": "正在核对字段含义、类型和样例，避免只按名称推断",
    "read_table_rows": "正在用真实样例值复核字段口径和数据关系",
    "read_file": "正在比对现有建模内容与资料依据是否一致",
    "grep_package": "正在定位相关知识条目，再核对上下游引用关系",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    parallel_safe: bool = True
    display: str = "正在处理"

    @property
    def input_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": self.properties,
            "additionalProperties": False,
        }
        if self.required:
            schema["required"] = list(self.required)
        return schema


@dataclass
class ToolContext:
    project_id: str
    run_id: str
    token: str
    gateway: CoreGateway
    emit: EventSink
    phase: str = ""
    tool_profile: str = "domain-v2"
    required_tools: frozenset[str] = frozenset()
    require_tool: RequireTool | None = None
    release_required_tool: ReleaseRequiredTool | None = None
    resolve_issues: ResolveIssues | None = None
    technical_recoveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    state_action: StateAction | None = None
    after_core_tool: AfterCoreTool | None = None
    confirmation_guard: ConfirmationGuard | None = None
    scope_confirmation_gate: ScopeConfirmationGate | None = None
    tool_receipt_lookup: ToolReceiptLookup | None = None
    tool_receipt_save: ToolReceiptSave | None = None
    stable_read_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_waiting: bool = False
    pending_evaluation: dict[str, Any] | None = None
    evaluation_cancel_authorized: bool = False
    allowed_tools: frozenset[str] | None = None
    confirmation_required: bool = False
    interjection_reply_required: bool = False
    release_declined: bool = False
    decision_request_in_text: bool = False
    user_input_request_in_text: bool = False
    confirmation_claimed_in_text: bool = False
    technical_capability_claimed: bool = False
    decision_card_requested: bool = False
    validation_repair: CandidateValidationRepairState = field(default_factory=lambda: CandidateValidationRepairState())
    package_contract: dict[str, Any] = field(default_factory=dict)
    cancel_event: asyncio.Event | None = None
    inflight_core_calls: set[asyncio.Task[Any]] | None = None

    def __post_init__(self) -> None:
        confirmation = self.package_contract.get("confirmation")
        if not isinstance(confirmation, Mapping):
            return
        try:
            configured = int(confirmation.get("maxAutomaticRepairRounds") or 3)
        except (TypeError, ValueError):
            configured = 3
        self.validation_repair.max_attempts = max(1, configured)
        # Public package-contract option name, not a credential or secret.
        stop_on_repeated = confirmation.get("stopOnRepeatedFindingFingerprint")  # NOSONAR
        if isinstance(stop_on_repeated, bool):
            self.validation_repair.stop_on_repeated_fingerprint = stop_on_repeated
        self.validation_repair._notify_change()


def _cancel_requested(context: ToolContext) -> bool:
    """Return whether the current Agent invocation crossed its cancel fence."""
    return bool(context.cancel_event is not None and context.cancel_event.is_set())


async def call_core(
    context: ToolContext,
    tool: str,
    arguments: dict[str, Any],
    *,
    poll_seconds: float = 0.1,
) -> dict[str, Any]:
    """Call Core without allowing a late response to escape cancellation.

    A number of rails run outside ``CoreTool.invoke`` (notably confirmation
    preflight).  Those calls used to bypass the in-flight task set and could
    publish a blocked/failed result after the run had already been cancelled.
    Keep them on the same cancellation boundary as ordinary tools.
    """
    if _cancel_requested(context):
        raise asyncio.CancelledError
    call = asyncio.create_task(context.gateway.call(
        project_id=context.project_id,
        run_id=context.run_id,
        token=context.token,
        tool=tool,
        arguments=arguments,
    ))
    if context.inflight_core_calls is not None:
        context.inflight_core_calls.add(call)
    try:
        while True:
            if _cancel_requested(context):
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                raise asyncio.CancelledError
            done, _pending = await asyncio.wait({call}, timeout=poll_seconds)
            if done:
                if _cancel_requested(context):
                    raise asyncio.CancelledError
                return call.result()
    except BaseException:
        if not call.done():
            call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        raise
    finally:
        if context.inflight_core_calls is not None:
            context.inflight_core_calls.discard(call)


@dataclass
class CandidateValidationRepairState:
    candidate_revision: str = ""
    attempts: int = 0
    last_fingerprint: str = ""
    repeated_findings: bool = False
    stopped: bool = False
    last_report: dict[str, Any] = field(default_factory=dict)
    current_round_id: str = ""
    current_reports: list[dict[str, Any]] = field(default_factory=list)
    current_round_progress: bool = False
    stop_notice_emitted: bool = False
    open_issue_ids: list[str] = field(default_factory=list)
    issue_attempts: dict[str, int] = field(default_factory=dict)
    stopped_fingerprints: list[str] = field(default_factory=list)
    blocked_reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    stop_notice_fingerprints: list[str] = field(default_factory=list)
    max_attempts: int = 3
    stop_on_repeated_fingerprint: bool = True
    on_change: ValidationRepairSave | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any] | None,
        *,
        on_change: ValidationRepairSave | None = None,
    ) -> CandidateValidationRepairState:
        raw = snapshot if isinstance(snapshot, Mapping) else {}
        attempts = _non_negative_int(raw.get("attempts"))
        last_fingerprint = str(raw.get("lastFingerprint") or "")
        issue_attempts = {
            str(key): _non_negative_int(value)
            for key, value in _mapping_dict(raw.get("issueAttempts")).items()
            if str(key).strip() and _non_negative_int(value) > 0
        }
        if not issue_attempts and last_fingerprint and attempts > 0:
            issue_attempts[last_fingerprint] = attempts
        stopped_fingerprints = [
            str(item) for item in raw.get("stoppedFingerprints", []) if str(item).strip()
        ]
        if not stopped_fingerprints and bool(raw.get("stopped")) and last_fingerprint:
            stopped_fingerprints = [last_fingerprint]
        blocked_reports = {
            str(key): dict(value)
            for key, value in _mapping_dict(raw.get("blockedReports")).items()
            if str(key).strip() and isinstance(value, Mapping)
        }
        last_report = _mapping_dict(raw.get("lastReport"))
        if stopped_fingerprints and last_report and not blocked_reports:
            blocked_reports[stopped_fingerprints[0]] = dict(last_report)
        stop_notice_fingerprints = [
            str(item) for item in raw.get("stopNoticeFingerprints", []) if str(item).strip()
        ]
        if not stop_notice_fingerprints and bool(raw.get("stopNoticeEmitted")):
            stop_notice_fingerprints = list(stopped_fingerprints[:1])
        state = cls(
            candidate_revision=str(raw.get("candidateRevision") or ""),
            attempts=attempts,
            last_fingerprint=last_fingerprint,
            repeated_findings=bool(raw.get("repeatedFindings")),
            stopped=bool(stopped_fingerprints),
            last_report=last_report,
            current_round_id=str(raw.get("currentRoundId") or ""),
            current_reports=[
                dict(item) for item in raw.get("currentReports", [])
                if isinstance(item, Mapping)
            ],
            current_round_progress=bool(raw.get("currentRoundProgress")),
            stop_notice_emitted=bool(raw.get("stopNoticeEmitted")),
            open_issue_ids=[
                str(item) for item in raw.get("openIssueIds", []) if str(item).strip()
            ],
            issue_attempts=issue_attempts,
            stopped_fingerprints=list(dict.fromkeys(stopped_fingerprints)),
            blocked_reports=blocked_reports,
            stop_notice_fingerprints=list(dict.fromkeys(stop_notice_fingerprints)),
            max_attempts=max(1, _positive_int(raw.get("maxAttempts"), 3)),
            stop_on_repeated_fingerprint=bool(
                raw.get("stopOnRepeatedFindingFingerprint", True)
            ),
            on_change=on_change,
        )
        return state

    def snapshot(self) -> dict[str, Any]:
        return {
            "candidateRevision": self.candidate_revision,
            "attempts": self.attempts,
            "lastFingerprint": self.last_fingerprint,
            "repeatedFindings": self.repeated_findings,
            "stopped": self.stopped,
            "lastReport": self.last_report,
            "currentRoundId": self.current_round_id,
            "currentReports": self.current_reports,
            "currentRoundProgress": self.current_round_progress,
            "stopNoticeEmitted": self.stop_notice_emitted,
            "openIssueIds": self.open_issue_ids,
            "issueAttempts": self.issue_attempts,
            "stoppedFingerprints": self.stopped_fingerprints,
            "blockedReports": self.blocked_reports,
            "stopNoticeFingerprints": self.stop_notice_fingerprints,
            "maxAttempts": self.max_attempts,
            "stopOnRepeatedFindingFingerprint": self.stop_on_repeated_fingerprint,
        }

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change(self.snapshot())

    def begin_round(self, round_id: str) -> None:
        normalized = str(round_id or "").strip() or "unscoped"
        if normalized == self.current_round_id:
            return
        self._finalize_round()
        self.current_round_id = normalized
        self._notify_change()

    def scope_candidate_revision(self, candidate_revision: str) -> None:
        normalized = str(candidate_revision or "").strip()
        if not normalized or normalized == self.candidate_revision:
            return
        if self.candidate_revision:
            self._reset_episode(preserve_round=False)
        self.candidate_revision = normalized
        self._notify_change()

    def _finalize_round(self) -> None:
        if self.current_reports:
            report = _aggregate_validation_reports(self.current_reports)
            round_states: list[tuple[str, int, bool, bool, dict[str, Any]]] = []
            seen: set[str] = set()
            for current_report in self.current_reports:
                fingerprint = _validation_report_fingerprint(current_report)
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                previous_attempts = self.issue_attempts.get(fingerprint, 0)
                attempt = previous_attempts + 1
                repeated = previous_attempts > 0
                stopped = (
                    self.stop_on_repeated_fingerprint and repeated
                ) or attempt >= self.max_attempts
                self.issue_attempts[fingerprint] = attempt
                if stopped and fingerprint not in self.stopped_fingerprints:
                    self.stopped_fingerprints.append(fingerprint)
                round_states.append((
                    fingerprint, attempt, repeated, stopped, current_report,
                ))

            aggregate_fingerprint = _validation_report_fingerprint(report)
            representative = next(
                (item for item in round_states if item[3]),
                round_states[0],
            )
            (
                fingerprint,
                self.attempts,
                _repeated,
                _stopped,
                representative_report,
            ) = representative
            self.repeated_findings = any(item[2] for item in round_states)
            self.stopped = bool(self.stopped_fingerprints)
            self.last_fingerprint = fingerprint if self.stopped else aggregate_fingerprint
            self.stop_notice_emitted = (
                fingerprint in self.stop_notice_fingerprints if self.stopped else False
            )
            issue_id = _validation_issue_id(self.candidate_revision, fingerprint)
            self.last_report = {
                **report,
                "retryable": not _stopped,
                "issueId": issue_id,
                "issueStatus": "blocked" if _stopped else "repairing",
                "repairAttempt": self.attempts,
                "repairRoundId": self.current_round_id,
                "findingFingerprint": self.last_fingerprint,
                "repeatedFinding": self.repeated_findings,
                "repairStopped": _stopped,
            }
            if _stopped:
                self.last_report["instruction"] = (
                    "自动修正已停止：相同 findings 再次出现，或已达到契约规定的 "
                    f"{self.max_attempts} 轮上限。"
                    "保留完整 findings，并等待用户决定是调整范围还是撤销候选。"
                )
                self.blocked_reports[fingerprint] = {
                    **dict(representative_report),
                    **self.last_report,
                    "findingFingerprint": fingerprint,
                }
        self.current_reports = []
        self.current_round_progress = False
        self._notify_change()

    def record(self, report: Mapping[str, Any]) -> dict[str, Any]:
        if not self.current_round_id:
            self.begin_round("unscoped")
        fingerprint = _validation_report_fingerprint(report)
        if fingerprint in self.stopped_fingerprints:
            self.last_fingerprint = fingerprint
            self.attempts = self.issue_attempts.get(fingerprint, self.max_attempts)
            self.repeated_findings = self.attempts > 1
            self.stopped = True
            self.last_report = {
                **dict(report),
                "issueId": _validation_issue_id(self.candidate_revision, fingerprint),
                "findingFingerprint": fingerprint,
                "repairAttempt": self.attempts,
                "repairRoundId": self.current_round_id,
                "repeatedFinding": self.repeated_findings,
                "repairStopped": True,
            }
            self.blocked_reports[fingerprint] = dict(self.last_report)
            blocked = self.blocked_result(fingerprint)
            return {
                "issueId": blocked["issueId"],
                "issueStatus": "blocked",
                "repairAttempt": blocked["repairAttempt"],
                "repairRoundId": blocked["repairRoundId"],
                "findingFingerprint": blocked["findingFingerprint"],
                "repeatedFinding": blocked["repeatedFinding"],
                "repairStopped": True,
            }
        normalized = dict(report)
        self.current_reports.append(normalized)
        issue_id = _validation_issue_id(self.candidate_revision, fingerprint)
        if issue_id not in self.open_issue_ids:
            self.open_issue_ids.append(issue_id)
        self._notify_change()
        return {
            "issueId": issue_id,
            "issueStatus": "repairing",
            "repairAttempt": self.issue_attempts.get(fingerprint, 0) + 1,
            "repairRoundId": self.current_round_id,
            "findingFingerprint": fingerprint,
            "repeatedFinding": False,
            "repairStopped": False,
        }

    def reset(self) -> None:
        self._reset_episode(preserve_round=False)

    def _reset_episode(self, *, preserve_round: bool) -> None:
        round_id = self.current_round_id if preserve_round else ""
        self.attempts = 0
        self.last_fingerprint = ""
        self.repeated_findings = False
        self.stopped = False
        self.last_report = {}
        self.current_round_id = round_id
        self.current_reports = []
        self.current_round_progress = False
        self.stop_notice_emitted = False
        self.open_issue_ids = []
        self.issue_attempts = {}
        self.stopped_fingerprints = []
        self.blocked_reports = {}
        self.stop_notice_fingerprints = []
        if not preserve_round:
            self.candidate_revision = ""
        self._notify_change()

    def record_success(self, tool_name: str) -> list[str]:
        if tool_name in VALIDATION_REPAIR_RESOLUTION_TOOLS:
            return self.resolve_open_issues()
        if tool_name in VALIDATION_REPAIR_PROGRESS_TOOLS:
            self.current_round_progress = True
            self._notify_change()
        return []

    def resolve_open_issues(self) -> list[str]:
        resolved = list(dict.fromkeys(self.open_issue_ids))
        self._reset_episode(preserve_round=True)
        return resolved

    def blocked_result(self, fingerprint: str = "") -> dict[str, Any]:
        selected = str(fingerprint or "").strip()
        if selected not in self.stopped_fingerprints:
            selected = next(iter(self.stopped_fingerprints), self.last_fingerprint)
        report = dict(self.blocked_reports.get(selected) or self.last_report)
        issue_id = str(report.get("issueId") or _validation_issue_id(
            self.candidate_revision, selected,
        ))
        attempts = self.issue_attempts.get(selected, self.attempts)
        return {
            **report,
            "error": "CANDIDATE_REPAIR_STOPPED",
            "retryable": False,
            "issueId": issue_id,
            "issueStatus": "awaiting_user",
            "repairAttempt": attempts,
            "repairRoundId": str(report.get("repairRoundId") or self.current_round_id),
            "findingFingerprint": selected,
            "repeatedFinding": attempts > 1,
            "repairStopped": True,
            "tracePhase": "waiting",
            "problemVisibility": "recovery",
            "customerSummary": "自动修正仍有未完成项，请选择如何处理当前成果。",
            "instruction": (
                "自动修正已达到上限。完整校验报告仍由 core 保留；停止继续尝试，"
                "等待用户选择重新检查未完成项，或保留当前成果并结束本轮。"
            ),
            "display": {"text": "自动修正仍有未完成项，等待选择后续处理"},
            "recoveryAction": {
                "id": "review_validation_findings",
                "label": "重新检查并继续",
                "message": "请读取当前问题的校验明细，只处理仍未解决的 finding，再从已保存进展继续。",
            },
            "recoveryActions": [
                {
                    "id": "review_validation_findings",
                    "label": "重新检查并继续",
                    "description": "从已保存进展继续，只处理仍未解决的内容。",
                },
                {
                    "id": "keep_current_progress",
                    "label": "保留当前成果，结束本轮",
                    "description": "保留已经完成的候选修改，暂不继续自动修正。",
                },
            ],
        }

    def claim_stop_notice(self) -> bool:
        fingerprint = (
            self.last_fingerprint
            if self.last_fingerprint in self.stopped_fingerprints
            else next(iter(self.stopped_fingerprints), self.last_fingerprint)
        )
        if fingerprint in self.stop_notice_fingerprints:
            return False
        if fingerprint:
            self.stop_notice_fingerprints.append(fingerprint)
        self.stop_notice_emitted = True
        self._notify_change()
        return True

    def decorate(self, report: Mapping[str, Any]) -> dict[str, Any]:
        repair = self.record(report)
        if repair.get("repairStopped") is True:
            return self.blocked_result()
        decorated = {**report, **repair}
        existing_instruction = str(report.get("instruction") or "").strip()
        repair_instruction = (
            "这是可自动修正的确定性校验失败。完整处理 findings 中每一项；若返回"
            " validationReportId，先按"
            " findingSummary 确定范围，再用 get_validation_findings 每次读取一页并逐项修正；"
            "不要把整份报告重新读入同一轮上下文。同一 repairRoundId 下多个文件只算一轮，"
            "修正后重新校验。"
        )
        decorated["instruction"] = " ".join(
            item for item in (existing_instruction, repair_instruction) if item
        )
        decorated["tracePhase"] = "repairing"
        return decorated


def _validation_report_fingerprint(report: Mapping[str, Any]) -> str:
    supplied = str(report.get("findingFingerprint") or "").strip()
    if supplied:
        return supplied
    findings = report.get("findings")
    fingerprint_source: Any = {
        "validatorVersion": report.get("validatorVersion"),
        "findings": findings,
    } if isinstance(findings, list) and findings else {
        "error": report.get("error"),
        "reason": report.get("reason"),
        "validatorVersion": report.get("validatorVersion"),
    }
    encoded = json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _validation_issue_id(candidate_revision: str, fingerprint: str) -> str:
    encoded = f"{candidate_revision or 'working'}\0{fingerprint or 'unknown'}"
    return f"candidate-issue-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]}"


def _fingerprint_values(values: list[str]) -> str:
    normalized = sorted({str(value).strip() for value in values if str(value).strip()})
    if len(normalized) == 1:
        return normalized[0]
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _mapping_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _positive_int(value: Any, fallback: int) -> int:
    try:
        return max(1, int(value or fallback))
    except (TypeError, ValueError):
        return fallback


def _aggregate_validation_reports(reports: list[Mapping[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    reasons: list[str] = []
    versions: list[str] = []
    next_steps: list[str] = []
    findings_by_key: dict[str, dict[str, Any]] = {}
    report_ids: list[str] = []
    report_fingerprints: list[str] = []
    summarized_finding_count = 0
    findings_truncated = False
    for report in reports:
        error = str(report.get("error") or "CANDIDATE_VALIDATION_FAILED")
        reason = str(report.get("reason") or "").strip()
        version = str(report.get("validatorVersion") or "").strip()
        if error not in errors:
            errors.append(error)
        if reason and reason not in reasons:
            reasons.append(reason)
        if version and version not in versions:
            versions.append(version)
        report_id = str(report.get("validationReportId") or "").strip()
        if report_id and report_id not in report_ids:
            report_ids.append(report_id)
        report_fingerprint = _validation_report_fingerprint(report)
        if report_fingerprint not in report_fingerprints:
            report_fingerprints.append(report_fingerprint)
        try:
            summarized_finding_count += max(0, int(report.get("findingCount") or 0))
        except (TypeError, ValueError):
            pass
        findings_truncated = findings_truncated or bool(report.get("findingsTruncated"))
        for step in report.get("next") or []:
            normalized_step = str(step).strip()
            if normalized_step and normalized_step not in next_steps:
                next_steps.append(normalized_step)
        for finding in report.get("findings") or []:
            if not isinstance(finding, Mapping):
                continue
            normalized = dict(finding)
            key = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            findings_by_key[key] = normalized
    findings = sorted(
        findings_by_key.values(),
        key=lambda item: (
            str(item.get("path") or ""),
            str(item.get("ruleId") or ""),
            str(item.get("message") or ""),
        ),
    )
    result = {
        "error": errors[0] if len(errors) == 1 else "CANDIDATE_VALIDATION_FAILED",
        "errors": errors,
        "reason": "；".join(reasons) or "候选确定性校验未通过",
        "retryable": True,
        "validatorVersion": versions[0] if len(versions) == 1 else "+".join(sorted(versions)),
        "findingCount": max(len(findings), summarized_finding_count),
        "findings": findings,
        "next": next_steps,
        "validationReportIds": report_ids,
        "findingsTruncated": findings_truncated,
    }
    result["findingFingerprint"] = _fingerprint_values(report_fingerprints)
    return result


STRING = {"type": "string"}
RELATION_NAME = {
    "type": "string",
    "description": (
        "由模型根据材料证据生成，不从固定枚举中选择；只填写简洁、有方向的中文谓词"
        "（如“属于”“包含”“生成”“引用”“依赖”“适用于”），不得包含源对象或目标对象的 key、"
        "标题、别名，也不得只填写 1:1、1:N 等关系基数。提交前将“源对象 -> 关系名称 -> 目标对象”"
        "连读为自然、符合证据的业务陈述；有更具体证据时避免只写“关联”“对应”。基数可写入 note。"
    ),
}
PATH = {"type": "string", "description": "知识包内相对路径"}
CHANGE_GROUP = {
    "type": "string",
    "pattern": "^[a-z][a-z0-9_.-]{0,63}$",
    "maxLength": 64,
    "description": "同一批相关修改使用相同的简短英文标识，例如 goal 或 process_main",
}
RESOURCE_ID = {
    "type": "string",
    "pattern": "^(object|rule|operation|metric):[^/\\\\:]+$",
    "description": "list_domain_resources 或 get_domain_resource 返回的稳定资源标识",
}
WORKING_REVISION = {"type": "string", "minLength": 1, "maxLength": 200}
RESOURCE_HASH = {"type": "string", "pattern": "^sha256:[a-f0-9]{64}$"}
MUTATION_ID = {
    "type": "string",
    "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    "description": "同一业务修改重试时保持不变的幂等标识",
}
GOAL_EVIDENCE = {
    **STRING,
    "description": "material:<资料 id>#<原文锚点> 或 user-decision:<说明>",
}
GOAL_SECTION_DATA = {
    "description": (
        "必须与 section 匹配：objective 使用 problem/target_users/success_criteria/evidence；"
        "scope 使用 included/excluded/evidence；constraints、outputs 和 open_questions 使用 items/evidence。"
        "不要提交 summary、title 或其它未列出的字段；success_criteria 必须是字符串，不是数组。"
    ),
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "problem": STRING,
                "target_users": STRING,
                "success_criteria": STRING,
                "evidence": {"type": "array", "items": GOAL_EVIDENCE, "minItems": 1, "maxItems": 30},
            },
            "required": ["problem", "target_users", "success_criteria", "evidence"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "included": {"type": "array", "items": STRING, "maxItems": 30},
                "excluded": {"type": "array", "items": STRING, "maxItems": 30},
                "evidence": {"type": "array", "items": GOAL_EVIDENCE, "minItems": 1, "maxItems": 30},
            },
            "required": ["included", "excluded", "evidence"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 30},
                "evidence": {"type": "array", "items": GOAL_EVIDENCE, "minItems": 1, "maxItems": 30},
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    ],
}
DOMAIN_FIELD = {
    "type": "object",
    "properties": {
        "key": STRING, "cn_name": STRING, "type": STRING,
        "required": {"type": "boolean"}, "note": STRING, "alias": STRING,
    },
    "required": ["key", "type", "required"],
    "additionalProperties": False,
}
DOMAIN_RELATION = {
    "type": "object",
    "properties": {
        "kind": STRING, "target_id": RESOURCE_ID, "label": STRING, "description": STRING,
    },
    "required": ["target_id"],
    "additionalProperties": False,
}
DOMAIN_ACTION_CHANGES = {
    "type": "object",
    "properties": {
        "key": STRING, "cn_name": STRING, "type": STRING,
        "required": {"type": "boolean"}, "note": STRING, "alias": STRING,
        "condition": STRING, "conclusion": STRING,
        "param": STRING, "field": STRING, "handling": STRING,
        "kind": STRING, "target_id": RESOURCE_ID, "label": STRING,
        "description": STRING,
    },
    "additionalProperties": False,
}


def domain_action_schema(operations: list[str]) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": 100,
        "items": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": operations},
                "value": {
                    "description": (
                        "文本、列表、流程步骤和普通标量动作的值；"
                        "attach_evidence/detach_evidence 禁止使用此字段。"
                    ),
                    "oneOf": [STRING, {"type": "array", "items": STRING, "maxItems": 500}],
                },
                "token": {
                    **STRING,
                    "description": "attach_evidence/detach_evidence 必填的单个证据 token。",
                },
                "item_id": STRING,
                "after_id": STRING,
                "field_key": STRING,
                "expected_field_hash": RESOURCE_HASH,
                "relation_id": STRING,
                "target_id": RESOURCE_ID,
                "input_key": STRING,
                "output_key": STRING,
                "field": {"oneOf": [DOMAIN_FIELD, STRING]},
                "relation": DOMAIN_RELATION,
                "condition": {
                    "type": "object",
                    "properties": {"condition": STRING, "conclusion": STRING},
                    "required": ["condition", "conclusion"],
                    "additionalProperties": False,
                },
                "input": {
                    "type": "object",
                    "properties": {
                        "param": STRING, "cn_name": STRING, "type": STRING,
                        "required": {"type": "boolean"}, "note": STRING,
                    },
                    "required": ["param", "type", "required"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {"field": STRING, "cn_name": STRING, "type": STRING, "note": STRING},
                    "required": ["field", "type"],
                    "additionalProperties": False,
                },
                "key": STRING,
                "changes": DOMAIN_ACTION_CHANGES,
            },
            "required": ["op"],
            "additionalProperties": False,
        },
    }


SCHEMA_SEMANTIC_ACTIONS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 500,
    "items": {
        "oneOf": [
            {
                "type": "object",
                "properties": {"op": {"type": "string", "enum": ["set_title", "set_description"]}, "value": STRING},
                "required": ["op", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"op": {"type": "string", "enum": ["set_aliases"]}, "value": {"type": "array", "items": STRING, "maxItems": 500}},
                "required": ["op", "value"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["update_field"]},
                    "field_key": STRING,
                    "expected_field_hash": RESOURCE_HASH,
                    "changes": {
                        "type": "object",
                        "properties": {
                            "cn_name": STRING,
                            "note": STRING,
                            "alias": STRING,
                            "required": {"type": "boolean"},
                        },
                        "minProperties": 1,
                        "additionalProperties": False,
                    },
                },
                # 补空白语义时逐字段带乐观锁会反复失效：一次成功写入就让手里剩下的
                # 字段哈希整批作废，批量因此退化为逐个提交（见 #32）。对象级
                # expected_resource_hash 已经足够防止丢失更新，字段级改为可选。
                "required": ["op", "field_key", "changes"],
                "additionalProperties": False,
            },
        ],
    },
}


DOMAIN_MUTATION_ENVELOPE = {
    "expected_revision": WORKING_REVISION,
    "mutation_id": MUTATION_ID,
    "edit_session_id": MUTATION_ID,
    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
}
PLAN_TASK_ID = {"type": "string", "minLength": 1, "maxLength": 160}
OBJECT_PATCH_ACTIONS = [
    "set_title", "set_description", "set_aliases", "set_query_boundary",
    "add_field", "update_field", "remove_field",
    "add_relation", "update_relation", "remove_relation",
]
RULE_PATCH_ACTIONS = [
    "set_title", "set_description", "set_content", "attach_evidence", "detach_evidence",
    "add_condition", "update_condition", "remove_condition",
    "add_exception", "remove_exception", "add_target", "remove_target",
]
OPERATION_PATCH_ACTIONS = [
    "set_title", "set_description", "attach_evidence", "detach_evidence",
    "add_input", "update_input", "remove_input", "add_output", "update_output", "remove_output",
    "insert_step", "update_step", "move_step", "remove_step",
    "add_reference", "remove_reference", "add_note", "remove_note",
]
METRIC_PATCH_ACTIONS = [
    "set_title", "set_description", "set_caliber", "set_unit", "set_grain",
    "attach_evidence", "detach_evidence",
    "add_boundary", "update_boundary", "remove_boundary",
    "upsert_null_policy", "remove_null_policy", "upsert_dimension", "remove_dimension",
    "add_object_reference", "remove_object_reference",
]

CHANGE_PLAN_TASK = {
    "type": "object",
    "properties": {
        "client_key": {"type": "string", "minLength": 1, "maxLength": 64},
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "resource_kind": {
            "type": "string",
            "enum": ["object", "rule", "operation", "metric", "script", "binding", "other"],
        },
        "target_key": {
            "type": "string", "minLength": 1, "maxLength": 160,
            "description": "普通任务填写一个且仅一个目标资源的稳定 key；Core 注入的 Schema 同步任务使用保留目标 __schema__ 并承载末尾语义阶段。",
        },
        "allowed_tools": {
            "type": "array", "minItems": 1, "maxItems": 20,
            "items": {"type": "string", "enum": [
                "create_object", "create_rule", "create_operation", "create_metric",
                "patch_object", "patch_rule", "patch_operation", "patch_metric",
                "sync_schema_objects", "stage_schema_semantic_changes", "confirm_schema_semantics",
                "patch_object_source_binding", "delete_domain_resource", "stage_script_patch",
                "review_object_relations",
            ]},
        },
        "depends_on": {"type": "array", "maxItems": 100, "items": STRING},
        "acceptance": {"type": "string", "minLength": 1, "maxLength": 1000},
        "required": {"type": "boolean"},
    },
    "required": [
        "client_key", "title", "resource_kind", "target_key", "allowed_tools", "acceptance",
    ],
    "additionalProperties": False,
}
CHANGE_SET_TASK_ID = {
    "type": "string", "minLength": 1,
    "description": "Core 返回的任务 ID；必须与本动作 tool 和 arguments 中的单一资源目标完全对应。",
}


def change_set_arguments(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    }


def change_set_action_variant(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "task_id": CHANGE_SET_TASK_ID,
            "tool": {"type": "string", "enum": [tool]},
            "arguments": arguments,
        },
        "required": ["task_id", "tool", "arguments"],
        "additionalProperties": False,
    }


CHANGE_SET_CREATE_OBJECT_ARGUMENTS = change_set_arguments({
    "key": STRING, "title": STRING, "description": STRING,
    "aliases": {"type": "array", "items": STRING}, "query_boundary": STRING,
    "fields": {"type": "array", "minItems": 1, "items": DOMAIN_FIELD},
    "relations": {"type": "array", "items": DOMAIN_RELATION},
}, ["key", "title", "description", "fields"])
CHANGE_SET_CREATE_RULE_ARGUMENTS = change_set_arguments({
    "key": STRING, "title": STRING, "description": STRING, "content": STRING,
    "conditions": {"type": "array", "items": {
        "type": "object", "properties": {"condition": STRING, "conclusion": STRING},
        "required": ["condition", "conclusion"], "additionalProperties": False,
    }},
    "exceptions": {"type": "array", "items": STRING},
    "targets": {"type": "array", "items": RESOURCE_ID},
    "evidence": {"type": "array", "minItems": 1, "items": STRING},
}, ["key", "title", "content", "evidence"])
CHANGE_SET_CREATE_OPERATION_ARGUMENTS = change_set_arguments({
    "key": STRING, "title": STRING, "description": STRING,
    "inputs": {"type": "array", "items": {
        "type": "object", "properties": {
            "param": STRING, "cn_name": STRING, "type": STRING,
            "required": {"type": "boolean"}, "note": STRING,
        }, "required": ["param", "type", "required"], "additionalProperties": False,
    }},
    "outputs": {"type": "array", "items": {
        "type": "object", "properties": {
            "field": STRING, "cn_name": STRING, "type": STRING, "note": STRING,
        }, "required": ["field", "type"], "additionalProperties": False,
    }},
    "flow": {"type": "array", "minItems": 1, "items": STRING},
    "notes": {"type": "array", "items": STRING},
    "references": {"type": "array", "items": RESOURCE_ID},
    "evidence": {"type": "array", "minItems": 1, "items": STRING},
}, ["key", "title", "description", "flow", "evidence"])
CHANGE_SET_CREATE_METRIC_ARGUMENTS = change_set_arguments({
    "key": STRING, "title": STRING, "description": STRING, "unit": STRING, "grain": STRING,
    "caliber": STRING, "boundaries": {"type": "array", "minItems": 1, "items": STRING},
    "null_policy": {"type": "array", "items": {
        "type": "object", "properties": {"field": STRING, "handling": STRING},
        "required": ["field", "handling"], "additionalProperties": False,
    }},
    "dimensions": {"type": "array", "items": {
        "type": "object", "properties": {"key": STRING, "note": STRING},
        "required": ["key", "note"], "additionalProperties": False,
    }},
    "object_references": {"type": "array", "items": RESOURCE_ID},
    "evidence": {"type": "array", "minItems": 1, "items": STRING},
}, ["key", "title", "caliber", "boundaries", "evidence"])


def change_set_patch_arguments(operations: list[str]) -> dict[str, Any]:
    return change_set_arguments({
        "resource_id": RESOURCE_ID,
        "expected_resource_hash": RESOURCE_HASH,
        "actions": domain_action_schema(operations),
    }, ["resource_id", "expected_resource_hash", "actions"])


CHANGE_SET_ACTION = {"oneOf": [
    change_set_action_variant("create_object", CHANGE_SET_CREATE_OBJECT_ARGUMENTS),
    change_set_action_variant("create_rule", CHANGE_SET_CREATE_RULE_ARGUMENTS),
    change_set_action_variant("create_operation", CHANGE_SET_CREATE_OPERATION_ARGUMENTS),
    change_set_action_variant("create_metric", CHANGE_SET_CREATE_METRIC_ARGUMENTS),
    change_set_action_variant("patch_object", change_set_patch_arguments(OBJECT_PATCH_ACTIONS)),
    change_set_action_variant("patch_rule", change_set_patch_arguments(RULE_PATCH_ACTIONS)),
    change_set_action_variant("patch_operation", change_set_patch_arguments(OPERATION_PATCH_ACTIONS)),
    change_set_action_variant("patch_metric", change_set_patch_arguments(METRIC_PATCH_ACTIONS)),
]}


CORE_TOOL_SPECS = (
    ToolSpec(
        "begin_goal_draft",
        "创建结构化建模目标草稿。目标按 section 分批提交，服务端逐段校验后再组装目标文件。",
        {
            "draft_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "change_group": CHANGE_GROUP,
            "base_revision": {"type": "string", "minLength": 1, "maxLength": 200},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        ("change_group", "base_revision", "request_id"), False, "正在建立目标草稿",
    ),
    ToolSpec(
        "put_goal_section",
        "提交一个建模目标 section。只提交 objective、scope、constraints、outputs 或 open_questions 之一，服务端会立即校验字段和证据。",
        {
            "draft_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "section": {"type": "string", "enum": ["objective", "scope", "constraints", "outputs", "open_questions"]},
            "data": GOAL_SECTION_DATA,
            "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "base_revision": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        ("draft_id", "section", "data", "request_id", "base_revision"), False, "正在保存目标草稿分段",
    ),
    ToolSpec(
        "finalize_goal_draft",
        "完成目标草稿。服务端检查必需 section、证据格式和完整性，并负责渲染 .modeling/goal.md。",
        {
            "draft_id": {"type": "string", "minLength": 1, "maxLength": 80},
            "request_id": {"type": "string", "minLength": 1, "maxLength": 100},
            "base_revision": {"type": "string", "minLength": 1, "maxLength": 200},
        },
        ("draft_id", "request_id", "base_revision"), False, "正在完成建模目标草稿",
    ),
    ToolSpec(
        "get_goal_context",
        "读取服务端生成的有界目标上下文，只返回结构化资料结论、约束、候选和证据引用，不返回长篇原文工具历史。",
        {}, display="正在整理目标生成依据",
    ),
    ToolSpec("list_documents", "列出项目全部资料及修订和解析状态。", {}, display="正在查看资料清单"),
    ToolSpec(
        "get_product_ledger",
        "读取当前产品/规则大清单的逐项对账台账和确定性校验结果；没有台账时返回未建立状态。",
        {},
        display="正在读取产品清单台账",
    ),
    ToolSpec(
        "stage_product_ledger",
        "提交产品/规则大清单的结构化台账。sourceRevision 可留空，由核心绑定当前工作区修订；必须提供每个 canonical item 的唯一 ID、rule ID、原始记录定位、归并组、证据覆盖状态和处理状态；核心会确定性校验数量闭合和 ID 唯一性。",
        {
            "schemaVersion": {"type": "string", "enum": ["product-ledger-v1", "product-ledger-v2"]},
            "sourceRevision": STRING,
            "scope": STRING,
            "rawCount": {"type": "integer", "minimum": 0},
            "canonicalCount": {"type": "integer", "minimum": 0},
            "mergedGroupCount": {"type": "integer", "minimum": 0},
            "required": {"type": "boolean"},
            "entries": {
                "type": "array",
                "maxItems": 10000,
                "items": {
                    "type": "object",
                    "properties": {
                        "canonicalItemId": STRING,
                        "ruleId": STRING,
                        "ruleRefs": {"type": "array", "items": STRING, "minItems": 1, "uniqueItems": True},
                        "sourceRecordIds": {"type": "array", "items": STRING, "minItems": 1},
                        "sourceLocator": STRING,
                        "mergeGroupId": STRING,
                        "evidenceStatus": {"type": "string", "enum": ["complete", "insufficient"]},
                        "processingStatus": {"type": "string", "enum": ["pending", "ready", "generated", "blocked"]},
                        "insufficiencyReason": STRING,
                    },
                    "required": ["canonicalItemId", "sourceRecordIds", "sourceLocator", "evidenceStatus", "processingStatus"],
                    "additionalProperties": False,
                },
            },
        },
        ("scope", "rawCount", "canonicalCount", "mergedGroupCount", "entries"),
        False,
        "正在校验产品清单台账",
    ),
    ToolSpec(
        "get_schema_mapping_plan",
        "读取全部已确认 Sheet 与当前对象结构化来源的完整对照台账，返回逐 Sheet 映射状态和未闭环计数。业务流程确认并进入 knowledge 阶段后先调用，补齐来源后再次核对；只有 complete=true 才能发起知识候选确认。",
        {},
        display="正在核对数据 Schema 映射计划",
    ),
    ToolSpec(
        "read_document",
        "按需分页读取资料正文。局部核对先检索再读取命中范围；需要全文覆盖时必须改用 read_documents_fully，禁止串行翻页替代。",
        {"filename": STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20000}},
        ("filename",),
        display="正在读取资料",
    ),
    ToolSpec(
        "set_material_directive",
        "提交资料用途预判。每份已解析资料都要提交；表格资料必须逐 Sheet 给出建议。核心会要求用户确认后才允许知识候选生效；用户未指定时不得把建议伪装成用户决定。",
        {
            "filename": STRING,
            "purpose": {"type": "string", "enum": ["goal", "schema", "term", "evidence"]},
            "reading_mode": {"type": "string", "enum": ["on_demand", "full"]},
            "sheet_purposes": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "sheet": STRING,
                    "semantic_role": {"type": "string", "enum": ["schema", "evidence", "ignore"]},
                    "import_disposition": {"type": "string", "enum": ["object", "relationship", "derived", "reference", "ignore"]},
                    "reason": STRING,
                },
                "required": ["sheet", "semantic_role", "import_disposition"],
                "additionalProperties": False,
            }},
        },
        ("filename", "purpose"),
        False,
        "正在保存资料设置",
    ),
    ToolSpec(
        "read_documents_fully",
        "并发全文精读指定资料，并返回由服务端校验的逐文档 100% 字符覆盖、全文摘要、候选事实和开放问题。",
        {"filenames": {"type": "array", "items": STRING, "maxItems": 50}},
        (),
        False,
        "正在并行全文精读资料",
    ),
    ToolSpec("search_documents", "跨资料搜索原文，query 中的词按 OR 匹配。", {"query": STRING}, ("query",), display="正在检索资料"),
    ToolSpec("list_tables", "列出从表格资料解析出的数据字典。", {}, display="正在查看数据表"),
    ToolSpec(
        "get_schema_overview",
        "一次返回已解析表格的结构摘要：表名、行数、字段类型、非空率、唯一性和少量样例；不返回业务数据行。用于 Schema 盘点，避免逐表分页读取整张表。",
        {},
        display="正在生成表结构摘要",
    ),
    ToolSpec("describe_table", "按资料 ID 和 Sheet 名查看字段、类型、样例和唯一性，避免同名 Sheet 混淆。", {"document_id": STRING, "table": STRING}, ("document_id", "table"), display="正在分析数据表"),
    ToolSpec(
        "read_table_rows",
        "分页读取表格行，一次最多 50 行。",
        {"document_id": STRING, "table": STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        ("document_id", "table", "offset", "limit"),
        display="正在读取表格样例",
    ),
    ToolSpec(
        "stage_tabular_dataset",
        "将一份已解析 Excel 或 CSV 的全部已展开表和行确定性同步为数据集候选。只传资料名；严禁把整表正文放进 write_file。",
        {"source": STRING, "path": PATH},
        ("source",),
        False,
        "正在同步完整数据表",
    ),
    ToolSpec("list_dir", "列出知识包目录。", {"path": PATH}, ("path",), display="正在查看知识包目录"),
    ToolSpec(
        "read_file",
        "读取非脚本知识包文件。scripts/*.py 是受管源码，必须使用 get_script_edit_context 按符号读取，禁止通过此工具读取完整 Python 文件。",
        {"path": PATH},
        ("path",),
        display="正在读取知识文件",
    ),
    ToolSpec(
        "create_change_plan",
        "写入任何领域资源前，把用户目标或正常生成范围冻结成可逐项验收的任务。每项任务只能对应一个具体目标资源；Schema 模式的物理同步及其末尾业务语义阶段由 Core 合并为同一项 Schema 任务。若平台会自动注入对象关系任务（包含候选审阅和确认写入），不要自行创建关系审阅、关系确认或对象间关系等同义任务。要修改多个对象、规则、操作或指标时必须逐资源拆成多项。generation 计划必须等待页面批准；任务 ID 和批准状态都由 Core 返回，模型不能自行标记。",
        {
            "goal": {"type": "string", "minLength": 1, "maxLength": 4000},
            "mode": {"type": "string", "enum": ["expert_edit", "generation"]},
            "expected_revision": WORKING_REVISION,
            "tasks": {"type": "array", "minItems": 1, "maxItems": 100, "items": CHANGE_PLAN_TASK},
        },
        ("goal", "mode", "expected_revision", "tasks"),
        False,
        "正在建立受控建模计划",
    ),
    ToolSpec(
        "get_change_plan",
        "读取当前结构化建模计划、Core 分配的任务 ID、完成事实和剩余项。不要用对话记忆猜任务状态。",
        {"plan_id": STRING},
        (),
        True,
        "正在核对建模计划",
    ),
    ToolSpec(
        "revise_change_plan",
        "记录范围调整、增加任务或跳过可选任务。执行开始后扩大范围必须先得到用户明确决定。",
        {
            "plan_id": STRING,
            "expected_version": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "add_tasks": {"type": "array", "maxItems": 100, "items": CHANGE_PLAN_TASK},
            "skip_task_ids": {"type": "array", "maxItems": 100, "items": STRING},
            "scope_approved": {"type": "boolean"},
        },
        ("plan_id", "expected_version", "reason"),
        False,
        "正在记录计划调整",
    ),
    ToolSpec(
        "stage_change_set",
        "把依赖闭合的 1 到 4 个计划任务作为一个小型原子 Change Set 提交。每个 action 只对应一个计划任务和一个目标资源；同一资源需要补多个字段时应放进该 patch 的 actions，一次完成。大计划必须分批读取最新计划和资源快照后继续，最后再整体验证。只能使用白名单领域动作；每批成功或整批不生效。",
        {
            "plan_id": STRING,
            "expected_revision": WORKING_REVISION,
            "mutation_id": MUTATION_ID,
            "actions": {"type": "array", "minItems": 1, "maxItems": 4, "items": CHANGE_SET_ACTION},
        },
        ("plan_id", "expected_revision", "mutation_id", "actions"),
        False,
        "正在原子生成待确认修改",
    ),
    ToolSpec(
        "get_change_set_receipt",
        "查询 Change Set 是否实际提交。超时或响应丢失后必须先查回执，禁止猜测并重复写入。",
        {"mutation_id": MUTATION_ID},
        ("mutation_id",),
        True,
        "正在核对修改回执",
    ),
    ToolSpec(
        "review_object_relations",
        "读取当前建模计划的对象关系候选；传 apply=true 时将选中的候选写入工作版本。候选必须来自当前关系审阅页。",
        {
            "plan_task_id": PLAN_TASK_ID,
            "expected_revision": WORKING_REVISION,
            "apply": {"type": "boolean", "default": False},
            "review_id": STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0},
            "selected_fingerprints": {"type": "array", "items": STRING},
            "relation_edits": {"type": "array", "items": {
                "type": "object",
                "properties": {"fingerprint": STRING, "relation": STRING},
                "required": ["fingerprint", "relation"],
                "additionalProperties": False,
            }},
        },
        ("plan_task_id", "expected_revision"),
        False,
        "正在读取对象关系候选",
    ),
    ToolSpec(
        "validate_change_set",
        "只有计划全部必选任务形成候选后，才对完整批次校验并冻结确认回执。",
        {"plan_id": STRING, "summary": {"type": "string", "maxLength": 1000}},
        ("plan_id",),
        False,
        "正在校验完整建模计划",
    ),
    ToolSpec(
        "discard_change_set",
        "放弃尚未进入确认的当前计划和候选修改，不影响已确认版本。",
        {"plan_id": STRING, "reason": {"type": "string", "minLength": 1, "maxLength": 1000}},
        ("plan_id", "reason"),
        False,
        "正在放弃待确认修改",
    ),
    ToolSpec(
        "list_domain_resources",
        "按领域列出当前工作修订中的资源、稳定 ID 和资源哈希。修改前先读取，不要从旧对话盲改。",
        {
            "kind": {"type": "string", "enum": ["object", "rule", "operation", "metric"]},
            "working_revision": WORKING_REVISION,
            "query": STRING,
            "cursor": STRING,
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ("kind",),
        display="正在读取领域资源",
    ),
    ToolSpec(
        "get_domain_resource",
        "读取单个资源的规范化结构、资源哈希、稳定子项 ID 和允许动作。Patch 必须基于本次返回。",
        {"resource_id": RESOURCE_ID, "working_revision": WORKING_REVISION},
        ("resource_id",),
        display="正在读取资源快照",
    ),
    ToolSpec(
        "get_schema_semantics",
        "分页读取当前 Schema 对象业务语义阻断项；字段业务说明允许为空，required 已由 Schema 同步归一为布尔值。",
        {
            "level": {"type": "string", "enum": ["error", "warning"]},
            "code": STRING,
            "object_key": STRING,
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        display="正在检查 Schema 对象业务语义",
    ),
    ToolSpec(
        "sync_schema_objects",
        "按所有 purpose=schema 的已解析 Sheet 确定性同步物理对象。每张 Sheet 恰好生成一个对象，字段、主键、来源和 mapping 由 Core 生成；required 由显式约束和确定性规则归一，模型不得传对象正文或字段映射。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "plan_task_id": PLAN_TASK_ID,
        },
        ("plan_task_id", "expected_revision", "mutation_id", "reason"),
        False,
        "正在按数据 Schema 同步全部物理对象",
    ),
    ToolSpec(
        "stage_schema_semantic_changes",
        "在同一 Schema 同步计划任务中批量形成对象业务语义候选。只允许修改对象标题、说明、别名，以及字段中文名、可选说明、别名和 required；禁止修改物理身份。"
        "建议单批不超过 3 个对象、60 个字段：再大会让本次调用的 JSON 超出单次输出上限被截断，整批白跑。硬上限为 20 个对象、400 个字段。"
        "字段的 expected_field_hash 可以省略；对象级 expected_resource_hash 已提供并发保护。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "plan_task_id": PLAN_TASK_ID,
            "objects": {
                "type": "array", "minItems": 1, "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "resource_id": RESOURCE_ID,
                        "expected_resource_hash": RESOURCE_HASH,
                        "actions": SCHEMA_SEMANTIC_ACTIONS,
                    },
                    "required": ["resource_id", "expected_resource_hash", "actions"],
                    "additionalProperties": False,
                },
            },
        },
        ("plan_task_id", "expected_revision", "mutation_id", "reason", "objects"),
        False,
        "正在批量补全 Schema 对象业务语义",
    ),
    ToolSpec(
        "confirm_schema_semantics",
        "在同一 Schema 同步计划任务的最后确认所有 Schema 对象业务语义；所有 error 清零后才能继续后续任务。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "plan_task_id": PLAN_TASK_ID,
        },
        ("plan_task_id", "expected_revision", "mutation_id", "reason"),
        False,
        "正在确认 Schema 对象业务语义",
    ),
    ToolSpec(
        "create_object",
        "仅在项目没有确定数据 Schema 时创建不绑定物理表的虚拟对象。存在 Schema 时必须改用 sync_schema_objects。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "key": STRING, "title": STRING, "description": STRING,
            "aliases": {"type": "array", "items": STRING}, "query_boundary": STRING,
            "fields": {"type": "array", "minItems": 1, "items": DOMAIN_FIELD},
            "relations": {"type": "array", "items": DOMAIN_RELATION},
        },
        ("expected_revision", "mutation_id", "reason", "key", "title", "description", "fields"),
        False,
        "正在创建业务对象",
    ),
    ToolSpec(
        "create_rule",
        "创建业务规则。targets 使用 object:key 或 operation:key；服务端统一校验引用和证据。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "key": STRING, "title": STRING, "description": STRING, "content": STRING,
            "conditions": {"type": "array", "items": {
                "type": "object", "properties": {"condition": STRING, "conclusion": STRING},
                "required": ["condition", "conclusion"], "additionalProperties": False,
            }},
            "exceptions": {"type": "array", "items": STRING},
            "targets": {"type": "array", "items": RESOURCE_ID},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
        },
        ("expected_revision", "mutation_id", "reason", "key", "title", "content", "evidence"),
        False,
        "正在创建业务规则",
    ),
    ToolSpec(
        "create_operation",
        "创建业务操作。references 使用对象、规则或指标资源 ID；服务端生成详情与索引。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "key": STRING, "title": STRING, "description": STRING,
            "inputs": {"type": "array", "items": {
                "type": "object", "properties": {
                    "param": STRING, "cn_name": STRING, "type": STRING,
                    "required": {"type": "boolean"}, "note": STRING,
                }, "required": ["param", "type", "required"], "additionalProperties": False,
            }},
            "outputs": {"type": "array", "items": {
                "type": "object", "properties": {
                    "field": STRING, "cn_name": STRING, "type": STRING, "note": STRING,
                }, "required": ["field", "type"], "additionalProperties": False,
            }},
            "flow": {"type": "array", "minItems": 1, "items": STRING},
            "notes": {"type": "array", "items": STRING},
            "references": {"type": "array", "items": RESOURCE_ID},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
        },
        ("expected_revision", "mutation_id", "reason", "key", "title", "description", "flow", "evidence"),
        False,
        "正在创建业务操作",
    ),
    ToolSpec(
        "create_metric",
        "创建业务指标。服务端校验口径、边界、对象引用和证据，并生成详情与索引。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "key": STRING, "title": STRING, "description": STRING, "unit": STRING, "grain": STRING,
            "caliber": STRING, "boundaries": {"type": "array", "minItems": 1, "items": STRING},
            "null_policy": {"type": "array", "items": {
                "type": "object", "properties": {"field": STRING, "handling": STRING},
                "required": ["field", "handling"], "additionalProperties": False,
            }},
            "dimensions": {"type": "array", "items": {
                "type": "object", "properties": {"key": STRING, "note": STRING},
                "required": ["key", "note"], "additionalProperties": False,
            }},
            "object_references": {"type": "array", "items": RESOURCE_ID},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
        },
        ("expected_revision", "mutation_id", "reason", "key", "title", "caliber", "boundaries", "evidence"),
        False,
        "正在创建业务指标",
    ),
    ToolSpec(
        "patch_object",
        "按稳定字段 key 和关系 ID 增量修改对象。未出现在 actions 的内容由服务端完整保留。",
        {
            **DOMAIN_MUTATION_ENVELOPE, "resource_id": RESOURCE_ID,
            "expected_resource_hash": RESOURCE_HASH, "actions": domain_action_schema(OBJECT_PATCH_ACTIONS),
        },
        ("resource_id", "expected_revision", "expected_resource_hash", "mutation_id", "reason", "actions"),
        False,
        "正在增量修改对象",
    ),
    ToolSpec(
        "patch_object_source_binding",
        "把对象完整绑定到一张已解析数据表。提交全部字段的一一映射和主键；服务端按资源哈希防覆盖并重建对象与 mappings。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "plan_task_id": PLAN_TASK_ID,
            "resource_id": RESOURCE_ID,
            "expected_resource_hash": RESOURCE_HASH,
            "document_id": STRING,
            "table": STRING,
            "field_bindings": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {"field_key": STRING, "source_column": STRING},
                "required": ["field_key", "source_column"],
                "additionalProperties": False,
            }},
            "primary_key": {"type": "array", "minItems": 1, "items": STRING},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
        },
        (
            "resource_id", "expected_revision", "expected_resource_hash", "mutation_id", "reason",
            "document_id", "table", "field_bindings", "primary_key", "evidence",
        ),
        False,
        "正在更新对象数据绑定",
    ),
    ToolSpec(
        "patch_rule",
        "按稳定子项 ID 增量修改规则条件、例外、引用或元数据，不完整替换规则。",
        {
            **DOMAIN_MUTATION_ENVELOPE, "resource_id": RESOURCE_ID,
            "expected_resource_hash": RESOURCE_HASH, "actions": domain_action_schema(RULE_PATCH_ACTIONS),
        },
        ("resource_id", "expected_revision", "expected_resource_hash", "mutation_id", "reason", "actions"),
        False,
        "正在增量修改规则",
    ),
    ToolSpec(
        "patch_operation",
        "按参数 key 和稳定步骤 ID 增量修改操作，不重传未变化的输入、输出和流程。",
        {
            **DOMAIN_MUTATION_ENVELOPE, "resource_id": RESOURCE_ID,
            "expected_resource_hash": RESOURCE_HASH, "actions": domain_action_schema(OPERATION_PATCH_ACTIONS),
        },
        ("resource_id", "expected_revision", "expected_resource_hash", "mutation_id", "reason", "actions"),
        False,
        "正在增量修改操作",
    ),
    ToolSpec(
        "patch_metric",
        "增量修改指标口径、边界、空值策略、维度和对象引用，未涉及内容保持不变。",
        {
            **DOMAIN_MUTATION_ENVELOPE, "resource_id": RESOURCE_ID,
            "expected_resource_hash": RESOURCE_HASH, "actions": domain_action_schema(METRIC_PATCH_ACTIONS),
        },
        ("resource_id", "expected_revision", "expected_resource_hash", "mutation_id", "reason", "actions"),
        False,
        "正在增量修改指标",
    ),
    ToolSpec(
        "preview_domain_delete",
        "删除前确定性预览入站引用和受影响资源。只生成与当前引用图绑定的预览 token，不写候选。",
        {
            "resource_ids": {"type": "array", "minItems": 1, "maxItems": 100, "items": RESOURCE_ID},
            "expected_revision": WORKING_REVISION,
        },
        ("resource_ids", "expected_revision"),
        display="正在预览删除影响",
    ),
    ToolSpec(
        "delete_domain_resource",
        "使用刚生成的预览 token 原子删除领域资源并清理显式引用；状态变化后 token 自动失效。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "plan_task_id": PLAN_TASK_ID,
            "resource_ids": {"type": "array", "minItems": 1, "maxItems": 100, "items": RESOURCE_ID},
            "preview_token": STRING,
            "cleanup_references": {"type": "boolean", "const": True},
        },
        ("resource_ids", "expected_revision", "mutation_id", "reason", "preview_token", "cleanup_references"),
        False,
        "正在删除领域资源",
    ),
    ToolSpec(
        "write_file",
        "写入非受管候选文件，不改变已确认版本。规则、操作和对象数据映射优先使用领域工具；change_group 是本轮确认的最小单位。",
        {
            "path": PATH,
            "content": STRING,
            "change_group": CHANGE_GROUP,
            "evidence": {"type": "array", "items": STRING},
        },
        ("path", "content", "change_group", "evidence"),
        False,
        "正在整理待确认的修改",
    ),
    ToolSpec(
        "upsert_object",
        "创建或完整替换一个业务对象，并可在同一次调用中绑定真实数据表。更新已绑定对象时，省略 source 只会在字段集合不变时保留原绑定；字段变化必须完整重绑，remove_source=true 才会显式解绑。核心确定性生成对象详情、对象索引、数据映射和内部语义规格；不要再手写这些文件。",
        {
            "key": STRING, "title": STRING, "description": STRING,
            "aliases": {"type": "array", "items": STRING},
            "query_boundary": STRING,
            "fields": {"type": "array", "minItems": 1, "items": {
                "type": "object",
                "properties": {"key": STRING, "cn_name": STRING, "type": STRING,
                               "required": {"type": "boolean"}, "note": STRING, "alias": STRING},
                "required": ["key", "type", "required"], "additionalProperties": False,
            }},
            "relations": {"type": "array", "items": {
                "type": "object",
                "properties": {"relation": RELATION_NAME, "target": STRING, "note": STRING,
                               "evidence": {"type": "array", "items": STRING}},
                "required": ["relation", "target"], "additionalProperties": False,
            }},
            "source": {"type": "object", "properties": {
                "document_id": STRING, "table": STRING,
                "field_bindings": {"type": "array", "minItems": 1, "items": {
                    "type": "object",
                    "properties": {"field_key": STRING, "source_column": STRING},
                    "required": ["field_key", "source_column"], "additionalProperties": False,
                }},
                "primary_key": {"type": "array", "minItems": 1, "items": STRING},
            }, "required": ["document_id", "table", "field_bindings", "primary_key"],
               "additionalProperties": False},
            "source_plan": {"type": "object", "properties": {
                "primary_key": {"type": "array", "minItems": 1, "items": STRING},
                "sources": {"type": "array", "minItems": 2, "items": {
                    "type": "object",
                    "properties": {
                        "document_id": STRING,
                        "table": STRING,
                        "role": {"type": "string", "enum": ["primary", "extension"]},
                        "field_bindings": {"type": "array", "minItems": 1, "items": {
                            "type": "object",
                            "properties": {"field_key": STRING, "source_column": STRING},
                            "required": ["field_key", "source_column"],
                            "additionalProperties": False,
                        }},
                    },
                    "required": ["document_id", "table", "role", "field_bindings"],
                    "additionalProperties": False,
                }},
            }, "required": ["primary_key", "sources"], "additionalProperties": False},
            "remove_source": {"type": "boolean"},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
            "change_group": CHANGE_GROUP,
        },
        ("key", "title", "description", "fields", "evidence", "change_group"),
        False,
        "正在编译业务对象",
    ),
    ToolSpec(
        "upsert_rule",
        "创建或完整替换一条业务规则。核心校验对象/操作引用，并确定性生成规则详情、规则索引和内部语义规格；不要再手写这些文件。",
        {
            "key": STRING,
            "title": STRING,
            "description": STRING,
            "applies_to": {
                "type": "object",
                "properties": {
                    "objects": {"type": "array", "items": STRING},
                    "operations": {"type": "array", "items": STRING},
                },
                "required": ["objects", "operations"],
                "additionalProperties": False,
            },
            "conditions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"condition": STRING, "conclusion": STRING},
                    "required": ["condition", "conclusion"],
                    "additionalProperties": False,
                },
            },
            "conclusion": STRING,
            "exceptions": {"type": "array", "items": STRING},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
            "change_group": CHANGE_GROUP,
        },
        ("key", "title", "applies_to", "conditions", "conclusion", "evidence", "change_group"),
        False,
        "正在编译业务规则",
    ),
    ToolSpec(
        "upsert_operation",
        "创建或完整替换一个业务操作。核心校验所有引用，并确定性生成操作详情、操作索引和内部语义规格；不要再手写这些文件。",
        {
            "key": STRING,
            "title": STRING,
            "description": STRING,
            "inputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "param": STRING, "cn_name": STRING, "type": STRING,
                        "required": {"type": "boolean"}, "note": STRING,
                    },
                    "required": ["param", "type", "required"],
                    "additionalProperties": False,
                },
            },
            "outputs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"field": STRING, "cn_name": STRING, "type": STRING, "note": STRING},
                    "required": ["field", "type"],
                    "additionalProperties": False,
                },
            },
            "flow": {"type": "array", "minItems": 1, "items": STRING},
            "related_objects": {"type": "array", "minItems": 1, "items": STRING},
            "uses_rules": {"type": "array", "items": STRING},
            "uses_metrics": {"type": "array", "items": STRING},
            "notes": {"type": "array", "items": STRING},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
            "change_group": CHANGE_GROUP,
        },
        ("key", "title", "description", "inputs", "outputs", "flow", "related_objects", "evidence", "change_group"),
        False,
        "正在编译业务操作",
    ),
    ToolSpec(
        "bind_object_source",
        "把一个现有对象完整绑定到一张已解析数据表。必须提交对象全部字段到真实列的一一映射和主键；核心确定性重建对象来源、索引、数据映射和内部语义规格。",
        {
            "key": STRING,
            "document_id": STRING,
            "table": STRING,
            "field_bindings": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {"field_key": STRING, "source_column": STRING},
                    "required": ["field_key", "source_column"],
                    "additionalProperties": False,
                },
            },
            "primary_key": {"type": "array", "minItems": 1, "items": STRING},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
            "change_group": CHANGE_GROUP,
        },
        ("key", "document_id", "table", "field_bindings", "primary_key", "evidence", "change_group"),
        False,
        "正在编译对象数据绑定",
    ),
    ToolSpec(
        "bind_object_sources",
        "用完整计划重绑一个现有跨 Sheet 对象。必须有且只有一张 primary 主表，其余为一对一 extension；每张表都绑定全部对象主键，非主键字段只能由一张表提供。",
        {
            "key": STRING,
            "primary_key": {"type": "array", "minItems": 1, "items": STRING},
            "sources": {"type": "array", "minItems": 2, "items": {
                "type": "object",
                "properties": {
                    "document_id": STRING,
                    "table": STRING,
                    "role": {"type": "string", "enum": ["primary", "extension"]},
                    "field_bindings": {"type": "array", "minItems": 1, "items": {
                        "type": "object",
                        "properties": {"field_key": STRING, "source_column": STRING},
                        "required": ["field_key", "source_column"],
                        "additionalProperties": False,
                    }},
                },
                "required": ["document_id", "table", "role", "field_bindings"],
                "additionalProperties": False,
            }},
            "evidence": {"type": "array", "minItems": 1, "items": STRING},
            "change_group": CHANGE_GROUP,
        },
        ("key", "primary_key", "sources", "evidence", "change_group"),
        False,
        "正在编译跨 Sheet 对象数据绑定",
    ),
    ToolSpec(
        "get_validation_summary",
        "按校验失败返回的 report_id 读取稳定摘要和指纹，不返回全部明细。",
        {"report_id": STRING},
        ("report_id",),
        display="正在读取校验摘要",
    ),
    ToolSpec(
        "get_validation_findings",
        "分页读取一份校验报告的原始 findings。每页最多 50 项；修复一页后再读取下一页，禁止反复读取已处理页。",
        {
            "report_id": STRING,
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        ("report_id",),
        display="正在分页读取校验明细",
    ),
    ToolSpec(
        "delete_file",
        "从候选修订删除文件，不改变已确认版本。",
        {"path": PATH, "change_group": CHANGE_GROUP, "reason": STRING},
        ("path", "change_group", "reason"),
        False,
        "正在整理候选删除",
    ),
    ToolSpec(
        "discard_rejected_candidate_changes",
        "用户已在确认卡退回候选，并明确表示撤销修改、无需修改或保持已确认版本时，放弃退回后残留的全部业务候选。不会修改已确认版本、运行记录或项目阶段；若确认卡仍待决定会被拒绝。",
        {},
        (),
        False,
        "正在放弃已退回的修改",
    ),
    ToolSpec(
        "regroup_changes",
        "候选分组超过 5 组或分组失配时，无需参数，一键按路径和层级整理全部现有候选，不重写文件内容。",
        {},
        (),
        False,
        "正在整理确认分组",
    ),
    ToolSpec("grep_package", "跨知识包文件检索文本。", {"pattern": STRING}, ("pattern",), display="正在检索知识包"),
    ToolSpec("gate_status", "读取门禁的确定性结果；Agent 不能自行宣布门禁通过。", {"gate": STRING}, ("gate",), display="正在检查门禁"),
    ToolSpec(
        "analyze_deterministic_capabilities",
        "由核心代码分析已确认知识中的计算、聚合、排序和精确转换，返回脚本候选及建议契约；本工具只分析，不生成脚本。",
        {},
        (),
        display="正在分析确定性计算范围",
    ),
    ToolSpec(
        "confirm_deterministic_scope",
        "保存专家对当前确定性计算范围的明确决定。selected_operation_keys 为空表示专家明确决定本轮不生成脚本；不得在没有专家确认卡答案时调用。",
        {
            "expected_scope_hash": RESOURCE_HASH,
            "selected_operation_keys": {
                "type": "array", "items": STRING, "maxItems": 100, "uniqueItems": True,
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        },
        ("expected_scope_hash", "selected_operation_keys", "reason"),
        False,
        "正在保存脚本范围决定",
    ),
    ToolSpec("who_references", "返回直接引用目标的一跳条目；是否继续传播由你判断。", {"target": STRING}, ("target",), display="正在核对引用关系"),
    ToolSpec(
        "analyze_material_impact",
        "读取最近一次资料修订的确定性影响事实。新增资料的 scopeResolved=false 时，读完资料和相关知识后再次调用，"
        "用 direct_resources 提交直接影响起点；核心会验证 key、沿显式引用传播，并返回可信的保持有效项、待复核项和证据链。",
        {
            "source": {"type": "string", "description": "可选：资料文件名、source key 或资料修订 ID"},
            "sources": {
                "type": "array",
                "description": "多份资料都已完成范围界定后，按同一批资料生成确定性去重的合并影响视图。",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 20,
            },
            "direct_resources": {
                "type": "array",
                "description": "仅在新增资料影响范围待界定时提供；空列表表示没有现有知识受影响。",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["object", "relation", "operation", "rule", "metric", "script"]},
                        "key": STRING,
                    },
                    "required": ["kind", "key"],
                    "additionalProperties": False,
                },
            },
        },
        display="正在核对资料变更影响",
    ),
    ToolSpec("list_package_sources", "列出项目已上传的只读来源知识包。", {}, display="正在查看来源知识包"),
    ToolSpec(
        "list_package_fusion_plans",
        "列出一次性多来源融合计划及未裁决数量；两个以上来源时使用，不得顺序执行单包 merge。",
        {},
        display="正在查看多来源融合计划",
    ),
    ToolSpec(
        "get_package_fusion_plan",
        "读取一个多来源融合计划、逐元素去向账本、质量门禁和候选状态。",
        {"plan_id": STRING},
        ("plan_id",),
        display="正在读取多来源融合计划",
    ),
    ToolSpec(
        "create_package_fusion_plan",
        "为两个以上只读来源建立单次批量融合计划。版本可省略，由 core 使用下一评审版本；不会生成候选或修改当前包。",
        {
            "source_ids": {"type": "array", "items": STRING, "minItems": 2, "maxItems": 20},
            "source_selections": {
                "type": "array",
                "minItems": 2,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "sourceId": STRING,
                        "scopes": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string", "enum": ["objects", "operations", "rules", "metrics", "scripts", "diagrams"]},
                        },
                    },
                    "required": ["sourceId", "scopes"],
                    "additionalProperties": False,
                },
            },
            "output": {
                "type": "object",
                "properties": {
                    "id": STRING,
                    "name": STRING,
                    "description": STRING,
                    "version": STRING,
                    "language": STRING,
                },
                "required": ["id", "name", "description"],
                "additionalProperties": False,
            },
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": STRING,
                        "title": STRING,
                        "layer": {"type": "string", "enum": ["common", "parameterized"]},
                        "decision": {"type": "string", "enum": ["preserve_views"]},
                        "rationale": STRING,
                        "members": {
                            "type": "array",
                            "minItems": 2,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "sourceId": STRING,
                                    "kind": {"type": "string", "enum": ["object", "operation", "rule", "metric", "script", "diagram"]},
                                    "key": STRING,
                                    "relation": {"type": "string", "enum": ["equivalent", "broader", "narrower", "overlap"]},
                                },
                                "required": ["sourceId", "kind", "key", "relation"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "title", "layer", "decision", "rationale", "members"],
                    "additionalProperties": False,
                },
            },
            "dispositions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "sourceId": STRING,
                        "kind": {"type": "string", "enum": ["object", "operation", "rule", "metric", "script", "diagram"]},
                        "key": STRING,
                        "action": {"type": "string", "enum": ["preserve", "canonicalize", "exclude"]},
                        "targetSourceId": STRING,
                        "targetKey": STRING,
                        "reason": STRING,
                    },
                    "required": ["sourceId", "kind", "key", "action"],
                    "additionalProperties": False,
                },
            },
        },
        ("source_ids", "output"),
        False,
        "正在建立多来源融合计划",
    ),
    ToolSpec(
        "update_package_fusion_plan",
        "增量登记逐元素去向和跨包业务概念；概念按 id 更新，必须引用计划内真实元素。未全部裁决时 core 会继续阻断编译。",
        {
            "plan_id": STRING,
            "dispositions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "sourceId": STRING,
                        "kind": {"type": "string", "enum": ["object", "operation", "rule", "metric", "script", "diagram"]},
                        "key": STRING,
                        "action": {"type": "string", "enum": ["preserve", "canonicalize", "exclude"]},
                        "targetSourceId": STRING,
                        "targetKey": STRING,
                        "reason": STRING,
                    },
                    "required": ["sourceId", "kind", "key", "action"],
                    "additionalProperties": False,
                },
            },
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": STRING,
                        "title": STRING,
                        "layer": {"type": "string", "enum": ["common", "parameterized"]},
                        "decision": {"type": "string", "enum": ["preserve_views"]},
                        "rationale": STRING,
                        "members": {
                            "type": "array",
                            "minItems": 2,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "sourceId": STRING,
                                    "kind": {"type": "string", "enum": ["object", "operation", "rule", "metric", "script", "diagram"]},
                                    "key": STRING,
                                    "relation": {"type": "string", "enum": ["equivalent", "broader", "narrower", "overlap"]},
                                },
                                "required": ["sourceId", "kind", "key", "relation"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["id", "title", "layer", "decision", "rationale", "members"],
                    "additionalProperties": False,
                },
            },
        },
        ("plan_id",),
        False,
        "正在登记融合元素去向",
    ),
    ToolSpec(
        "compile_package_fusion_plan",
        "全部元素有明确去向后，一次性编译所有来源并生成一个整体待确认候选；绝不修改正式版本。",
        {"plan_id": STRING},
        ("plan_id",),
        False,
        "正在一次性编译多来源知识包",
    ),
    ToolSpec(
        "analyze_package_merge",
        "兼容单一来源的选择性导入。两个以上来源必须改用 package fusion 工具，禁止顺序两两 merge。",
        {
            "source_id": STRING,
            "scopes": {"type": "array", "items": STRING},
        },
        ("source_id",),
        display="正在计算融合范围和冲突",
    ),
    ToolSpec(
        "read_package_source_file",
        "读取只读来源包中的一个文本文件，不修改当前包。",
        {"source_id": STRING, "path": PATH},
        ("source_id", "path"),
        display="正在读取来源包内容",
    ),
    ToolSpec(
        "stage_package_merge",
        "把已选范围和已裁决冲突复制到候选修订，并为导入内容记录 provenance。",
        {
            "source_id": STRING,
            "scopes": {"type": "array", "items": STRING},
            "resolutions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "conflict_id": STRING,
                        "action": {"type": "string", "enum": ["keep_current", "use_source", "rename_source"]},
                        "target_path": PATH,
                    },
                    "required": ["conflict_id", "action"],
                    "additionalProperties": False,
                },
            },
        },
        ("source_id", "scopes", "resolutions"),
        False,
        "正在生成融合候选",
    ),
    ToolSpec(
        "diff",
        "读取两个修订之间的结构化差异。",
        {"rev_a": STRING, "rev_b": STRING, "scope": STRING},
        ("rev_a", "rev_b"),
        display="正在生成变更对照",
    ),
    ToolSpec("update_work_notes", "更新 .modeling/plan.md 的工作笔记；它不代表受控建模计划、业务阶段或门禁状态。", {"markdown": STRING}, ("markdown",), False, "正在更新工作笔记"),
    ToolSpec("update_plan", "仅供历史 checkpoint 恢复：更新 .modeling/plan.md 工作笔记。新运行使用 update_work_notes。", {"markdown": STRING}, ("markdown",), False, "正在恢复历史工作笔记"),
    ToolSpec(
        "define_script_contract",
        "先定义脚本的绑定操作、CLI 参数、结构化输出、超时和执行边界；这一步不写源码或候选文件。",
        {
            **DOMAIN_MUTATION_ENVELOPE,
            "script_key": STRING,
            "title": STRING,
            "description": STRING,
            "operation_id": RESOURCE_ID,
            "expected_operation_hash": RESOURCE_HASH,
            "entrypoint": {"type": "string", "pattern": "^[A-Za-z0-9_.-]+\\.py$"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            "parameters": {"type": "array", "maxItems": 100, "items": {
                "type": "object", "properties": {
                    "name": STRING, "flag": {"type": "string", "pattern": "^--[a-z][a-z0-9-]*$"},
                    "type": STRING, "required": {"type": "boolean"},
                    "default_value": {"oneOf": [
                        {"type": "string"}, {"type": "number"}, {"type": "boolean"},
                    ]},
                    "description": STRING, "multiple": {"type": "boolean"},
                    "action": {"type": "string", "enum": ["store_true", "store_false"]},
                }, "required": ["name", "flag", "type", "required"], "additionalProperties": False,
            }},
            "outputs": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                "type": "object", "properties": {"key": STRING, "type": STRING, "description": STRING},
                "required": ["key", "type"], "additionalProperties": False,
            }},
            "boundaries": {"type": "array", "minItems": 1, "maxItems": 100, "items": STRING},
            "evidence": {"type": "array", "minItems": 1, "maxItems": 100, "items": STRING},
        },
        (
            "expected_revision", "mutation_id", "reason", "script_key", "title", "description",
            "operation_id", "expected_operation_hash", "entrypoint", "timeout_seconds",
            "parameters", "outputs", "boundaries", "evidence",
        ),
        False,
        "正在定义脚本契约",
    ),
    ToolSpec(
        "get_script_contract",
        "读取当前 run 中已定义的脚本契约和契约哈希，不返回源码。",
        {"script_key": STRING},
        ("script_key",),
        display="正在读取脚本契约",
    ),
    ToolSpec(
        "get_script_edit_context",
        "读取脚本符号索引；指定 symbols 时只返回目标顶层符号及直接依赖源码。"
        "新增脚本返回 Core 生成的 CLI 脚手架，禁止通过该工具读取无关脚本源码。",
        {
            "script_key": STRING,
            "symbols": {
                "type": "array", "maxItems": 20, "items": {
                    "type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                },
            },
        },
        ("script_key",),
        display="正在读取脚本符号上下文",
    ),
    ToolSpec(
        "stage_script_patch",
        "按当前源码、契约和目标节点哈希提交 Python 顶层符号补丁。Core 只替换授权节点，"
        "其余源码保持不变，并复用脚本安全、绑定和候选门禁。",
        {
            "script_key": STRING,
            "plan_task_id": PLAN_TASK_ID,
            "expected_revision": WORKING_REVISION,
            "expected_contract_hash": RESOURCE_HASH,
            "base_source_hash": RESOURCE_HASH,
            "mutation_id": MUTATION_ID,
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "edits": {
                "type": "array", "minItems": 1, "maxItems": 20, "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["replace_function", "replace_assignment", "insert_before_function"],
                        },
                        "symbol": {
                            "type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                        },
                        "expected_node_hash": RESOURCE_HASH,
                        "source": {"type": "string", "minLength": 1, "maxLength": 100000},
                    },
                    "required": ["kind", "symbol", "expected_node_hash", "source"],
                    "additionalProperties": False,
                },
            },
        },
        (
            "script_key", "expected_revision", "expected_contract_hash", "base_source_hash",
            "mutation_id", "reason", "edits",
        ),
        False,
        "正在应用脚本符号补丁",
    ),
    ToolSpec(
        "stage_script_source",
        "仅供历史 checkpoint 恢复：按已定义契约提交完整 Python 源码。新运行必须使用 stage_script_patch。",
        {
            "script_key": STRING,
            "plan_task_id": PLAN_TASK_ID,
            "expected_revision": WORKING_REVISION,
            "expected_contract_hash": RESOURCE_HASH,
            "mutation_id": MUTATION_ID,
            "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            "source_code": {"type": "string", "minLength": 1, "maxLength": 100000},
        },
        ("script_key", "expected_revision", "expected_contract_hash", "mutation_id", "reason", "source_code"),
        False,
        "正在校验并提交脚本源码",
    ),
    ToolSpec(
        "run_script_test",
        "按知识包 Markdown 中的脚本契约拼装 CLI 参数，再在 JiuwenBox 中从脚本入口启动子进程。"
        "每个用例只能提供业务 input 和完整 expected；不接受 function 或 argv，不能直调 Python 内部函数。",
        {"script": STRING, "cases": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "input": {"type": "object", "additionalProperties": True},
                "expected": {},
            },
            "required": ["input", "expected"],
            "additionalProperties": False,
        }}},
        ("script", "cases"),
        False,
        "正在沙箱中测试脚本",
    ),
    ToolSpec(
        "run_evaluation",
        "创建一个后台评测任务并立即返回。省略 case_ids 表示全量；仅在验证融合前来源回归时传 source_id。"
        "任务创建后立即结束当前 Agent 回合，真实进度和终态由评测界面及终态事件提供。",
        {"case_ids": {"type": "array", "items": STRING}, "source_id": STRING},
        display="正在运行评测",
    ),
    ToolSpec(
        "get_evaluation_results",
        "读取指定或最近一轮真实评测的逐条回答、判定理由和轨迹；失败归因前调用一次。run_evaluation 已返回 roundId 时必须传 round_id，同一轮结果不得重复读取。",
        {"round_id": STRING},
        display="正在读取评测结果",
    ),
    ToolSpec(
        "cancel_evaluation",
        "仅在用户明确要求停止当前或指定评测轮次时使用。重复停止已结束轮次会返回同一终态，不会影响后来启动的轮次。",
        {"round_id": STRING},
        parallel_safe=False,
        display="正在停止评测",
    ),
    ToolSpec(
        "query_eval_db",
        "仅用于诊断 modeling 服务端可直连的 MySQL 评测库镜像，要求服务端已配置评测库直连地址；参数必须是一条只读 SQL。不要用它查询项目数据源配置，也不要用它访问 Neo4j 或仅由 NL2SQL 服务内部管理的数据连接。",
        {"sql": STRING},
        ("sql",),
        display="正在查询评测库镜像",
    ),
)

PROJECT_STATE_TOOL_SPECS = (
    ToolSpec("get_project_state", "读取本项目唯一 Agent 状态、修订、上下文和受门禁约束的下一步。每轮开始和转换前必须调用。", {}, display="正在读取项目状态"),
    ToolSpec(
        "transition_project_state",
        "提出项目阶段转换；服务端只在 expected_revision 匹配、转换边合法且 core 门禁通过时更新唯一状态。",
        {
            "to_phase": {"type": "string", "enum": ["goal", "process", "knowledge", "increment", "validation", "evaluation", "release"]},
            "expected_revision": {"type": "integer", "minimum": 0},
            "intent": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "evidence": {"type": "array", "items": STRING, "maxItems": 50},
            "open_questions": {"type": "array", "items": STRING, "maxItems": 30},
        },
        ("to_phase", "expected_revision", "intent", "summary", "evidence", "open_questions"),
        False,
        "正在核对项目状态转换",
    ),
    ToolSpec(
        "update_project_context",
        "阶段不变时更新唯一项目状态中的摘要和开放问题；不创建其他状态副本。",
        {
            "expected_revision": {"type": "integer", "minimum": 0},
            "summary": {"type": "string", "minLength": 1},
            "open_questions": {"type": "array", "items": STRING, "maxItems": 30},
            "evidence": {"type": "array", "items": STRING, "maxItems": 50},
        },
        ("expected_revision", "summary", "open_questions", "evidence"),
        False,
        "正在保存项目上下文",
    ),
)


class CoreTool(Tool):
    def __init__(
        self,
        spec: ToolSpec,
        context: ToolContext,
        *,
        table_sample_row_limit: int | None = None,
    ) -> None:
        super().__init__(
            ToolCard(
                id=f"{spec.name}_{context.run_id}",
                name=spec.name,
                description=spec.description,
                input_params=spec.input_schema,
                properties=(
                    {"resilience": {"timeout_s": FULL_READ_TOOL_TIMEOUT_SECONDS}}
                    if spec.name == "read_documents_fully" else {}
                ),
                parallel_safe=spec.parallel_safe,
            )
        )
        self.spec = spec
        self.context = context
        self.table_sample_row_limit = _bounded_positive_integer(
            table_sample_row_limit,
            DEFAULT_TABLE_SAMPLE_ROWS,
        )

    async def invoke(self, inputs: Any, **_kwargs: Any) -> str:
        if self.context.cancel_event is not None and self.context.cancel_event.is_set():
            raise asyncio.CancelledError
        arguments = input_dict(inputs)
        if self.spec.name == "cancel_evaluation" and not self.context.evaluation_cancel_authorized:
            return json.dumps({
                "error": "EVALUATION_CANCEL_NOT_REQUESTED",
                "reason": "用户没有明确要求停止评测",
                "retryable": False,
                "instruction": "不要停止评测；如需说明能力，只回答当前能力和状态。",
                "display": {"text": "未停止评测：当前没有明确的停止请求"},
            }, ensure_ascii=False)
        if (
            self.spec.name == "create_change_plan"
            and self.context.phase == "increment"
            and str(arguments.get("mode") or "") == "generation"
        ):
            return json.dumps({
                "error": "INCREMENT_PLAN_MODE_REQUIRED",
                "reason": "增量建模必须基于影响范围修改现有基线，不能建立全量生成计划",
                "retryable": False,
                "instruction": (
                    "先完成资料影响分析，再使用 mode='expert_edit'，"
                    "只为本轮直接受影响的资源建立修改任务。"
                ),
                "display": {"text": "已阻止全量生成计划，请只规划本轮受影响内容"},
            }, ensure_ascii=False)
        arguments = _bounded_source_arguments(
            self.spec.name,
            arguments,
            table_sample_row_limit=self.table_sample_row_limit,
        )
        cache_key = _stable_read_cache_key(self.spec.name, arguments)
        if cache_key is not None:
            cached = self.context.stable_read_cache.get(cache_key)
            if cached is not None:
                return json.dumps(cached, ensure_ascii=False)
        started_after = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        activity_id = f"{self.spec.name}:{started_after}"
        if self.spec.name == "read_documents_fully":
            filenames = [str(item) for item in arguments.get("filenames", []) if str(item).strip()]
            self.context.emit("progress", {
                "activityId": activity_id,
                "text": _full_read_heading(filenames),
                "detail": "正在建立完整阅读计划",
                "phase": "full_read",
                "counters": {"elapsedSeconds": 0, "current": 0, "total": 0, "unit": "个处理检查点"},
            })
        call = asyncio.create_task(self.context.gateway.call(
            project_id=self.context.project_id,
            run_id=self.context.run_id,
            token=self.context.token,
            tool=self.spec.name,
            arguments=arguments,
        ))
        if self.context.inflight_core_calls is not None:
            self.context.inflight_core_calls.add(call)
        started = time.monotonic()
        try:
            while True:
                if self.context.cancel_event is not None and self.context.cancel_event.is_set():
                    raise asyncio.CancelledError
                done, _pending = await asyncio.wait({call}, timeout=FULL_READ_PROGRESS_INTERVAL_SECONDS)
                if done:
                    if self.context.cancel_event is not None and self.context.cancel_event.is_set():
                        raise asyncio.CancelledError
                    result = call.result()
                    break
                elapsed = max(0, int(time.monotonic() - started))
                if self.spec.name == "read_documents_fully":
                    progress_unavailable = False
                    try:
                        progress = await call_core(
                            self.context,
                            "get_material_analysis_progress",
                            {
                                "started_after": started_after,
                                "filenames": arguments.get("filenames", []),
                            },
                        )
                    except Exception:
                        progress = {}
                        progress_unavailable = True
                    if not isinstance(progress, dict):
                        progress = {}
                        progress_unavailable = True
                    counters = progress.get("counters") if isinstance(progress.get("counters"), dict) else {}
                    self.context.emit("progress", {
                        "activityId": activity_id,
                        "text": str(progress.get("title") or _full_read_heading(arguments.get("filenames", []))),
                        "detail": str(
                            "暂时无法获取精确进度；任务仍在等待，你可以继续输入或停止任务"
                            if progress_unavailable else progress.get("detail") or _long_task_detail(elapsed)
                        ),
                        "phase": str(progress.get("stage") or "full_read"),
                        "counters": {**counters, "elapsedSeconds": elapsed},
                    })
                else:
                    self.context.emit("progress", {
                        "activityId": activity_id,
                        "text": self.spec.display,
                        "detail": tool_work_detail(self.spec.name, elapsed),
                        "phase": "tool",
                        "counters": {"elapsedSeconds": elapsed},
                    })
        except BaseException:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            raise
        finally:
            if self.context.inflight_core_calls is not None:
                self.context.inflight_core_calls.discard(call)
        if cache_key is not None and not result.get("error"):
            self.context.stable_read_cache[cache_key] = result
        if _cancel_requested(self.context):
            raise asyncio.CancelledError
        autonomous_recovery = _autonomous_recovery(self.spec.name, arguments, result, self.context)
        if autonomous_recovery is not None:
            result = autonomous_recovery
        redirect = _tool_redirect(result)
        if redirect is not None:
            issue_id = str(redirect["issueId"])
            required_tool = str(redirect["requiredTool"])
            recovery = {
                **redirect,
                "sourceTool": self.spec.name,
                "arguments": arguments,
            }
            self.context.technical_recoveries[issue_id] = recovery
            if self.context.require_tool is not None:
                self.context.require_tool(required_tool, recovery)
            result = {
                **result,
                "issueId": issue_id,
                "issueStatus": "repairing",
                "tracePhase": "repairing",
            }
        if _is_retryable_validation_failure(self.spec.name, result):
            result = self.context.validation_repair.decorate(result)
        elif not result.get("error"):
            resolved_validation_issue_ids = self.context.validation_repair.record_success(
                self.spec.name,
            )
            resolved_issue_ids = [
                issue_id for issue_id, recovery in self.context.technical_recoveries.items()
                if str(recovery.get("requiredTool") or "") == self.spec.name
            ]
            for issue_id in resolved_issue_ids:
                self.context.technical_recoveries.pop(issue_id, None)
            resolved_issue_ids = list(dict.fromkeys([
                *resolved_validation_issue_ids,
                *resolved_issue_ids,
            ]))
            if resolved_issue_ids:
                if self.context.resolve_issues is not None:
                    self.context.resolve_issues(resolved_issue_ids)
                result = {
                    **result,
                    "issueStatus": "resolved",
                    "resolvedIssueIds": resolved_issue_ids,
                }
        if result.get("problemVisibility") == "recovery":
            self.context.recovery_waiting = True
        # 失败也要上报：连续撞同一个错误同样是打转，原来只在成功时回调，
        # 于是一个反复失败的工具在停滞判据里完全不可见（见 #40）。
        if self.context.after_core_tool is not None:
            await self.context.after_core_tool(self.spec.name, arguments, result)
        return json.dumps(_compact_source_result(self.spec.name, result), ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


def _bounded_source_arguments(
    name: str,
    arguments: dict[str, Any],
    *,
    table_sample_row_limit: int = DEFAULT_TABLE_SAMPLE_ROWS,
) -> dict[str, Any]:
    bounded = dict(arguments)
    if name == "read_document":
        bounded["limit"] = _bounded_positive_integer(
            bounded.get("limit"), LEADER_DOCUMENT_PAGE_CHARS,
        )
    elif name == "read_table_rows":
        bounded["limit"] = _bounded_positive_integer(
            bounded.get("limit"), table_sample_row_limit,
        )
    return bounded


def _stable_read_cache_key(name: str, arguments: Mapping[str, Any]) -> str | None:
    if name not in {"list_tables", "read_table_rows"}:
        return None
    return json.dumps(
        {"tool": name, "arguments": dict(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _is_retryable_validation_failure(name: str, result: Mapping[str, Any]) -> bool:
    if name not in VALIDATION_REPAIR_ERROR_TOOLS:
        return False
    has_retryable_error = bool(result.get("error")) and result.get("retryable") is True
    has_report = bool(str(result.get("validationReportId") or "").strip())
    return has_retryable_error and (
        isinstance(result.get("findings"), list) or has_report
    )


def _tool_redirect(result: Mapping[str, Any]) -> dict[str, Any] | None:
    if result.get("error") is None or result.get("requiredAction") != "redirect_tool":
        return None
    required_tool = str(result.get("requiredTool") or "").strip()
    if not required_tool:
        return None
    issue_id = str(result.get("issueId") or "").strip()
    if not issue_id:
        source = json.dumps({
            "error": result.get("error"),
            "requiredTool": required_tool,
            "targetKind": result.get("targetKind"),
            "managedPath": result.get("managedPath"),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        issue_id = f"tool-issue-{hashlib.sha256(source.encode('utf-8')).hexdigest()[:20]}"
    recovery_action = result.get("recoveryAction")
    action_id = str(
        recovery_action.get("id")
        if isinstance(recovery_action, Mapping) else "resume_required_tool"
    ).strip() or "resume_required_tool"
    return {
        "issueId": issue_id,
        "requiredTool": required_tool,
        "targetKind": str(result.get("targetKind") or ""),
        "actionId": action_id,
    }


def _autonomous_recovery(
    tool_name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    context: ToolContext,
) -> dict[str, Any] | None:
    recovery_config = result.get("recovery")
    if (
        not result.get("error")
        or not isinstance(recovery_config, Mapping)
        or str(recovery_config.get("owner") or "") != "agent"
    ):
        return None
    strategy = str(recovery_config.get("strategy") or "refresh_and_retry").strip()
    required_tool = str(recovery_config.get("requiredTool") or tool_name).strip() or tool_name
    try:
        max_attempts = max(1, int(recovery_config.get("maxAttempts") or 2))
    except (TypeError, ValueError):
        max_attempts = 2
    scope = {
        key: arguments.get(key)
        for key in ("plan_id", "resource_id", "round_id", "path")
        if arguments.get(key) is not None
    }
    identity = json.dumps({
        "error": result.get("error"),
        "sourceTool": tool_name,
        "strategy": strategy,
        "scope": scope,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    issue_id = str(result.get("issueId") or "").strip() or (
        f"agent-recovery-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"
    )
    previous = context.technical_recoveries.get(issue_id, {})
    previous_attempt = _non_negative_int(previous.get("attempt"))
    exhausted = previous_attempt >= max_attempts
    attempt = previous_attempt if exhausted else previous_attempt + 1
    recovery_actions = [
        {
            "id": "retry_from_latest",
            "label": "基于最新内容继续",
            "description": "重新同步当前计划和内容，只处理尚未完成的部分。",
        },
        {
            "id": "keep_current_progress",
            "label": "保留当前成果，结束本轮",
            "description": "保留已经完成的修改，暂不继续本轮处理。",
        },
    ]
    recovery = {
        "issueId": issue_id,
        "requiredTool": required_tool,
        "sourceTool": tool_name,
        "strategy": strategy,
        "actionId": "retry_from_latest",
        "recoveryActions": recovery_actions,
        "attempt": attempt,
        "maxAttempts": max_attempts,
        "exhausted": exhausted,
        "scope": scope,
    }
    context.technical_recoveries[issue_id] = recovery
    if context.require_tool is not None:
        context.require_tool(required_tool, recovery)
    context.stable_read_cache.clear()
    if not exhausted:
        return {
            **dict(result),
            "issueId": issue_id,
            "issueStatus": "repairing",
            "repairAttempt": attempt,
            "tracePhase": "repairing",
            "problemVisibility": "activity",
            "instruction": (
                "这是系统可自行恢复的版本冲突。先重新读取当前建模计划和涉及的资源快照，"
                "再按最新 revision 与 resource hash 生成新的参数和 mutation_id；"
                "不得原样重放旧 Change Set。"
            ),
            "display": {"text": "内容版本已更新，正在同步最新状态后继续"},
        }
    if context.release_required_tool is not None:
        context.release_required_tool(required_tool)
    return {
        **dict(result),
        "issueId": issue_id,
        "issueStatus": "awaiting_user",
        "repairAttempt": attempt,
        "repairStopped": True,
        "tracePhase": "waiting",
        "problemVisibility": "recovery",
        "customerSummary": "自动同步仍未完成，请选择如何处理当前成果。",
        "instruction": (
            "自动恢复已达到上限。停止继续尝试并等待用户选择；不要把技术错误或内部参数交给用户。"
        ),
        "display": {"text": "自动同步仍未完成，等待选择后续处理"},
        "recoveryAction": recovery_actions[0],
        "recoveryActions": recovery_actions,
    }


def _bounded_positive_integer(value: Any, maximum: int) -> int:
    try:
        parsed = int(value or maximum)
    except (TypeError, ValueError):
        parsed = maximum
    return min(max(1, parsed), maximum)


def _compact_source_result(name: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("error") or name != "describe_table":
        return result
    table = result.get("table")
    if not isinstance(table, Mapping):
        return result
    raw_fields = table.get("fields")
    if not isinstance(raw_fields, list):
        return result
    fields = [_compact_table_field(field) for field in raw_fields if isinstance(field, Mapping)]
    return {
        **result,
        "table": {
            "name": table.get("name"),
            "documentId": table.get("documentId"),
            "rowCount": table.get("rowCount"),
            "fieldCount": len(fields),
            "fields": fields,
        },
    }


def _compact_table_field(field: Mapping[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    field_keys = (
        "name", "inferredType", "type", "nullable", "required",
        "unique", "isPrimaryKey", "primaryKeyCandidate", "distinctRatio",
        "description", "comment",
    )
    for key in field_keys:
        if key in field:
            compact[key] = field[key]
    for sample_key in ("sampleValues", "samples", "examples"):
        samples = field.get(sample_key)
        if not isinstance(samples, list):
            continue
        compact[sample_key] = [
            _compact_sample_value(value)
            for value in samples[:TABLE_FIELD_SAMPLE_VALUES]
        ]
        break
    return compact


def _compact_sample_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    if len(text) <= TABLE_SAMPLE_VALUE_CHARS:
        return text
    return f"{text[:TABLE_SAMPLE_VALUE_CHARS]}..."


def _full_read_heading(filenames: Any) -> str:
    values = [str(item).strip() for item in filenames if str(item).strip()] if isinstance(filenames, list) else []
    if len(values) == 1:
        return f"正在全文精读《{values[0]}》"
    if values:
        return f"正在并行全文精读 {len(values)} 份资料"
    return "正在并行全文精读资料"


def _long_task_detail(elapsed: int) -> str:
    if elapsed >= 120:
        return "这一步等待时间较长，你可以继续输入或停止任务"
    if elapsed >= 30:
        return "仍在等待下一项进度，你可以继续输入或离开页面"
    return "正在读取和整理原始资料"


def tool_work_detail(name: str, elapsed: int = 0) -> str:
    if elapsed >= 120:
        return "这一步等待时间较长，你可以继续输入或停止任务"
    if elapsed >= 30:
        return "仍在等待下一项进度，你可以继续输入或离开页面"
    return TOOL_WORK_DETAILS.get(name, "正在处理当前步骤")


class ProjectStateTool(Tool):
    def __init__(self, spec: ToolSpec, context: ToolContext) -> None:
        super().__init__(
            ToolCard(
                id=f"{spec.name}_{context.run_id}",
                name=spec.name,
                description=spec.description,
                input_params=spec.input_schema,
                parallel_safe=spec.parallel_safe,
            )
        )
        self.spec = spec
        self.context = context

    async def invoke(self, inputs: Any, **_kwargs: Any) -> str:
        if self.context.state_action is None:
            raise RuntimeError("项目状态工具未绑定")
        if _cancel_requested(self.context):
            raise asyncio.CancelledError
        try:
            result = await self.context.state_action(self.spec.name, input_dict(inputs))
        except AgentRuntimeError as error:
            instruction = (
                "先调用 get_project_state 读取唯一状态的最新 revision，再用新的 "
                "expected_revision 重试；不要绕过乐观锁或自行维护状态副本。"
                if error.code == "STATE_REVISION_CONFLICT"
                else "根据错误原因修正参数或前置条件；需要判断下一步时先重新调用 get_project_state。"
            )
            result = {
                **error.to_dict(),
                "instruction": instruction,
                "display": {"text": "项目状态已变化，正在重新对齐"},
            }
        if not result.get("error"):
            self.context.validation_repair.record_success(self.spec.name)
        # A cancelled state snapshot is an expected terminal observation, not
        # a failed tool invocation.  Keep the cancellation reason out of the
        # generic validation/recovery classifier.
        cancellation = result.get("cancellation")
        if result.get("execution") in {"stopping", "cancelled"} or (
            isinstance(cancellation, Mapping) and cancellation.get("requested") is True
        ):
            result.pop("error", None)
        return json.dumps(result, ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class FocusTool(Tool):
    def __init__(self, context: ToolContext) -> None:
        super().__init__(
            ToolCard(
                id=f"focus_{context.run_id}",
                name="focus",
                description=(
                    "控制前端中栏当前展示和高亮。引用文件、流程节点、知识条目、diff 或门禁前先调用；"
                    "影响分析结果会由系统自动定位，通常不要传 data，也不要复制原始证据清单。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["flow", "file", "diff", "doc", "impact", "conflict", "gates", "evaluation", "studio"]},
                        "target": STRING,
                        "node": STRING,
                        "anchor": STRING,
                        "view": {"type": "string", "enum": ["graph", "table", "entry", "runs"]},
                        "roundId": STRING,
                        "elementId": {
                            "type": "string",
                            "description": "Studio 稳定元素 ID，例如 rule:safety-stock。",
                        },
                        "data": {
                            "type": "object",
                            "description": "中栏结构化数据；影响分析、冲突清单和门禁视图使用。",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                parallel_safe=True,
                stateless=True,
                idempotent=True,
            )
        )
        self.context = context

    async def invoke(self, inputs: Any, **_kwargs: Any) -> str:
        target = input_dict(inputs)
        target = {
            key: target[key]
            for key in ("kind", "target", "node", "anchor", "view", "roundId", "elementId", "data")
            if target.get(key)
        }
        if target.get("kind") == "studio":
            target.setdefault("target", "知识包 Studio")
            target.setdefault("view", "entry" if target.get("elementId") else "graph")
        elif not target.get("target"):
            return json.dumps({
                "ok": False,
                "code": "INVALID_FOCUS_TARGET",
                "display": {"text": "这个定位缺少目标，请补充要查看的内容"},
            }, ensure_ascii=False)
        self.context.emit("focus", {"target": target})
        return json.dumps({"ok": True, "display": {"text": "已把相关内容定位到中栏"}}, ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class NotifyTool(Tool):
    def __init__(self, context: ToolContext) -> None:
        super().__init__(
            ToolCard(
                id=f"notify_{context.run_id}",
                name="notify",
                description="在对话流显示一条简短进度说明，不会挂起运行。",
                input_params={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 300},
                        "level": {"type": "string", "enum": ["info", "success", "warning"]},
                    },
                    "required": ["text", "level"],
                    "additionalProperties": False,
                },
                parallel_safe=True,
                stateless=True,
                idempotent=True,
            )
        )
        self.context = context

    async def invoke(self, inputs: Any, **_kwargs: Any) -> str:
        payload = input_dict(inputs)
        self.context.emit("progress", {"text": str(payload.get("text") or ""), "level": payload.get("level", "info"), "counters": {}})
        return json.dumps({"ok": True, "display": {"text": payload.get("text", "")}}, ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class AskTool(Tool):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            ToolCard(
                id=f"ask_{run_id}",
                name="ask",
                description="向专家提问并挂起。资料请求必须提供我口述、稍后补充和跳过三个不冲突的出口。",
                input_params={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "minLength": 1},
                        "kind": {"type": "string", "enum": ["clarification", "business_decision", "conflict_decision", "scope_confirmation", "material_request"]},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": STRING,
                                    "description": STRING,
                                    "value": STRING,
                                    "requiresText": {"type": "boolean"},
                                    "textPrompt": {"type": "string", "minLength": 1, "maxLength": 500},
                                    # 选项需要用户去别的界面完成时必须声明 route，
                                    # 否则点了只是把这句话发回来，界面不会跳转（见 #39）。
                                    "route": {"type": "string", "enum": ["evaluation"]},
                                },
                                "required": ["label", "description", "value"],
                                "additionalProperties": False,
                            },
                            "maxItems": 6,
                        },
                        "evidence": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["question", "kind"],
                    "additionalProperties": False,
                },
                parallel_safe=False,
            )
        )

    async def invoke(self, _inputs: Any, **_kwargs: Any) -> str:
        return ""

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class RequestConfirmationTool(CoreTool):
    SPEC = ToolSpec(
        "request_confirmation",
        "请求专家确认候选修订。只需提供业务摘要；系统会从实际候选自动整理确认分组，禁止猜测内部 ID。",
        {
            "summary": {"type": "string", "minLength": 1},
        },
        ("summary",),
        False,
        "正在提交待修改内容供你确认",
    )

    def __init__(self, context: ToolContext) -> None:
        super().__init__(self.SPEC, context)


class RequestReleaseTool(CoreTool):
    SPEC = ToolSpec(
        "request_release",
        "在全量真实评测通过后，请项目负责人确认用例覆盖和稳定版本号并正式发布。",
        {
            "summary": {"type": "string", "minLength": 1},
            "version": STRING,
        },
        ("summary",),
        False,
        "正在提交正式发布确认",
    )

    def __init__(self, context: ToolContext) -> None:
        super().__init__(self.SPEC, context)


def build_tools(context: ToolContext) -> list[Tool]:
    core_specs = core_tool_specs_for_phase(
        context.phase,
        context.required_tools,
        tool_profile=context.tool_profile,
    )
    return [
        *(ProjectStateTool(spec, context) for spec in PROJECT_STATE_TOOL_SPECS),
        *(CoreTool(spec, context) for spec in core_specs),
        FocusTool(context),
        NotifyTool(context),
        AskTool(context.run_id),
        RequestConfirmationTool(context),
        *([RequestReleaseTool(context)] if context.phase in {"", "evaluation", "release"} else []),
    ]


DOMAIN_PHASE_TOOLS = frozenset({
    "create_change_plan", "get_change_plan", "revise_change_plan",
    "stage_change_set", "get_change_set_receipt", "validate_change_set", "discard_change_set",
    "review_object_relations",
    "list_domain_resources", "get_domain_resource",
    "create_object", "create_rule", "create_operation", "create_metric",
    "patch_object", "patch_rule", "patch_operation", "patch_metric",
    "sync_schema_objects", "get_schema_semantics", "stage_schema_semantic_changes",
    "confirm_schema_semantics", "patch_object_source_binding",
    "preview_domain_delete", "delete_domain_resource",
    "upsert_object", "upsert_rule", "upsert_operation", "bind_object_source",
    "stage_tabular_dataset",
    "get_product_ledger", "stage_product_ledger",
})
DOMAIN_READ_TOOLS = frozenset({
    "list_domain_resources",
    "get_domain_resource",
    "get_product_ledger",
    "get_schema_semantics",
    "review_object_relations",
})
FUSION_PHASE_TOOLS = frozenset({
    "analyze_material_impact", "list_package_sources", "list_package_fusion_plans",
    "get_package_fusion_plan", "create_package_fusion_plan", "update_package_fusion_plan",
    "compile_package_fusion_plan", "analyze_package_merge", "read_package_source_file",
    "stage_package_merge",
})
EVALUATION_PHASE_TOOLS = frozenset({"run_evaluation", "get_evaluation_results", "cancel_evaluation", "query_eval_db"})
SCRIPT_PHASE_TOOLS = frozenset({
    "analyze_deterministic_capabilities", "confirm_deterministic_scope",
    "define_script_contract", "get_script_contract", "get_script_edit_context",
    "stage_script_patch", "stage_script_source", "run_script_test",
})
LEGACY_TOOL_ALIASES = frozenset({
    "upsert_object", "upsert_rule", "upsert_operation", "bind_object_source",
    "update_plan", "stage_script_source",
})
CHANGE_SET_DIRECT_ACTION_TOOLS = frozenset({
    "create_object", "create_rule", "create_operation", "create_metric",
    "patch_object", "patch_rule", "patch_operation", "patch_metric",
})


def core_tool_specs_for_phase(
    phase: str,
    required_tools: frozenset[str] = frozenset(),
    *,
    tool_profile: str = "domain-v2",
) -> tuple[ToolSpec, ...]:
    """Keep production Leader schemas aligned with the current business phase.

    An empty phase preserves the full surface for old tests and recovery callers that do not
    carry project state yet. Explicit read-only subagents continue to use build_core_tools.
    """
    legacy_profile = tool_profile == "legacy-v1"
    if not phase:
        selected_specs: list[ToolSpec] = []
        for spec in CORE_TOOL_SPECS:
            if legacy_profile or spec.name not in LEGACY_TOOL_ALIASES:
                selected_specs.append(spec)
        return tuple(selected_specs)
    allowed_domain = phase in {"knowledge", "increment"}
    allowed_domain_read = allowed_domain or phase == "validation"
    allowed_fusion = phase == "increment"
    allowed_evaluation = phase in {"validation", "evaluation", "release"}
    selected_specs = []
    for spec in CORE_TOOL_SPECS:
        if not legacy_profile and spec.name in LEGACY_TOOL_ALIASES:
            continue
        required_for_recovery = spec.name in required_tools
        allowed_for_phase = (
            (spec.name not in DOMAIN_PHASE_TOOLS
             or allowed_domain
             or (allowed_domain_read and spec.name in DOMAIN_READ_TOOLS))
            and (spec.name not in FUSION_PHASE_TOOLS or allowed_fusion)
            and (spec.name not in EVALUATION_PHASE_TOOLS or allowed_evaluation)
            and (spec.name not in SCRIPT_PHASE_TOOLS or phase in {"validation", "increment"})
        )
        if not required_for_recovery and not allowed_for_phase:
            continue
        direct_change_forbidden = (
            not legacy_profile
            and phase in {"knowledge", "increment"}
            and spec.name in CHANGE_SET_DIRECT_ACTION_TOOLS | {"write_file", "delete_file"}
        )
        if direct_change_forbidden:
            continue
        selected_specs.append(spec)
    return tuple(selected_specs)


def build_core_tools(context: ToolContext, names: set[str]) -> list[Tool]:
    """Build a least-privilege tool surface for an openJiuwen subagent."""
    table_limit = (
        MATERIAL_ANALYST_TABLE_SAMPLE_ROWS
        if "read_documents_fully" in names and "search_documents" in names
        else DEFAULT_TABLE_SAMPLE_ROWS
    )
    return [
        CoreTool(spec, context, table_sample_row_limit=table_limit)
        for spec in CORE_TOOL_SPECS
        if spec.name in names
    ]


def input_dict(inputs: Any) -> dict[str, Any]:
    if isinstance(inputs, Mapping):
        return dict(inputs)
    if hasattr(inputs, "model_dump"):
        value = inputs.model_dump()
        return value if isinstance(value, dict) else {}
    data = getattr(inputs, "data", None)
    return dict(data) if isinstance(data, Mapping) else {}


def tool_spec(name: str) -> ToolSpec | None:
    if name == "request_confirmation":
        return RequestConfirmationTool.SPEC
    if name == "request_release":
        return RequestReleaseTool.SPEC
    return next((spec for spec in (*PROJECT_STATE_TOOL_SPECS, *CORE_TOOL_SPECS) if spec.name == name), None)
