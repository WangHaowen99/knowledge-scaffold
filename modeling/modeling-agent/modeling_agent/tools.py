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
ToolReceiptLookup = Callable[[str, str, dict[str, Any]], dict[str, Any] | None]
ToolReceiptSave = Callable[[str, str, dict[str, Any], dict[str, Any]], None]
ValidationRepairSave = Callable[[dict[str, Any]], None]
FULL_READ_PROGRESS_INTERVAL_SECONDS = 5.0
LEADER_DOCUMENT_PAGE_CHARS = 6_000
LEADER_TABLE_SAMPLE_ROWS = 5
TABLE_FIELD_SAMPLE_VALUES = 3
TABLE_SAMPLE_VALUE_CHARS = 120

VALIDATION_REPAIR_ERROR_TOOLS = frozenset({
    "write_file",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "import_zip",
    "stage_package_merge",
    "compile_package_fusion_plan",
})
VALIDATION_REPAIR_PROGRESS_TOOLS = frozenset({
    "write_file",
    "delete_file",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
    "import_zip",
    "stage_package_merge",
    "compile_package_fusion_plan",
})
VALIDATION_REPAIR_RESET_TOOLS = frozenset({
    "discard_rejected_candidate_changes",
})

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
    state_action: StateAction | None = None
    after_core_tool: AfterCoreTool | None = None
    confirmation_guard: ConfirmationGuard | None = None
    tool_receipt_lookup: ToolReceiptLookup | None = None
    tool_receipt_save: ToolReceiptSave | None = None
    confirmation_required: bool = False
    interjection_reply_required: bool = False
    release_declined: bool = False
    decision_request_in_text: bool = False
    user_input_request_in_text: bool = False
    confirmation_claimed_in_text: bool = False
    decision_card_requested: bool = False
    validation_repair: CandidateValidationRepairState = field(default_factory=lambda: CandidateValidationRepairState())
    package_contract: dict[str, Any] = field(default_factory=dict)

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


@dataclass
class CandidateValidationRepairState:
    attempts: int = 0
    last_fingerprint: str = ""
    repeated_findings: bool = False
    stopped: bool = False
    last_report: dict[str, Any] = field(default_factory=dict)
    current_round_id: str = ""
    current_reports: list[dict[str, Any]] = field(default_factory=list)
    current_round_progress: bool = False
    stop_notice_emitted: bool = False
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
        state = cls(
            attempts=_non_negative_int(raw.get("attempts")),
            last_fingerprint=str(raw.get("lastFingerprint") or ""),
            repeated_findings=bool(raw.get("repeatedFindings")),
            stopped=bool(raw.get("stopped")),
            last_report=_mapping_dict(raw.get("lastReport")),
            current_round_id=str(raw.get("currentRoundId") or ""),
            current_reports=[
                dict(item) for item in raw.get("currentReports", [])
                if isinstance(item, Mapping)
            ],
            current_round_progress=bool(raw.get("currentRoundProgress")),
            stop_notice_emitted=bool(raw.get("stopNoticeEmitted")),
            max_attempts=max(1, _positive_int(raw.get("maxAttempts"), 3)),
            stop_on_repeated_fingerprint=bool(
                raw.get("stopOnRepeatedFindingFingerprint", True)
            ),
            on_change=on_change,
        )
        return state

    def snapshot(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "lastFingerprint": self.last_fingerprint,
            "repeatedFindings": self.repeated_findings,
            "stopped": self.stopped,
            "lastReport": self.last_report,
            "currentRoundId": self.current_round_id,
            "currentReports": self.current_reports,
            "currentRoundProgress": self.current_round_progress,
            "stopNoticeEmitted": self.stop_notice_emitted,
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

    def _finalize_round(self) -> None:
        if self.current_reports:
            report = _aggregate_validation_reports(self.current_reports)
            fingerprint = _validation_report_fingerprint(report)
            self.attempts += 1
            self.repeated_findings = bool(
                self.last_fingerprint and fingerprint == self.last_fingerprint
            )
            self.last_fingerprint = fingerprint
            self.stopped = (
                self.stop_on_repeated_fingerprint and self.repeated_findings
            ) or self.attempts >= self.max_attempts
            self.last_report = {
                **report,
                "retryable": not self.stopped,
                "repairAttempt": self.attempts,
                "repairRoundId": self.current_round_id,
                "findingFingerprint": fingerprint,
                "repeatedFinding": self.repeated_findings,
                "repairStopped": self.stopped,
            }
            if self.stopped:
                self.last_report["instruction"] = (
                    "自动修正已停止：相同 findings 再次出现，或已达到契约规定的 "
                    f"{self.max_attempts} 轮上限。"
                    "保留完整 findings，并等待用户决定是调整范围还是撤销候选。"
                )
        self.current_reports = []
        self.current_round_progress = False
        self._notify_change()

    def record(self, report: Mapping[str, Any]) -> dict[str, Any]:
        if not self.current_round_id:
            self.begin_round("unscoped")
        if self.stopped:
            blocked = self.blocked_result()
            return {
                "repairAttempt": blocked["repairAttempt"],
                "repairRoundId": blocked["repairRoundId"],
                "findingFingerprint": blocked["findingFingerprint"],
                "repeatedFinding": blocked["repeatedFinding"],
                "repairStopped": True,
            }
        normalized = dict(report)
        self.current_reports.append(normalized)
        fingerprint = _validation_report_fingerprint(normalized)
        self._notify_change()
        return {
            "repairAttempt": self.attempts + 1,
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
        self._notify_change()

    def record_success(self, tool_name: str) -> None:
        if tool_name in VALIDATION_REPAIR_RESET_TOOLS:
            self._reset_episode(preserve_round=True)
        elif tool_name in VALIDATION_REPAIR_PROGRESS_TOOLS:
            self.current_round_progress = True
            self._notify_change()

    def blocked_result(self) -> dict[str, Any]:
        report = dict(self.last_report)
        return {
            **report,
            "error": "CANDIDATE_REPAIR_STOPPED",
            "retryable": False,
            "repairAttempt": self.attempts,
            "repairRoundId": str(report.get("repairRoundId") or self.current_round_id),
            "findingFingerprint": self.last_fingerprint,
            "repeatedFinding": self.repeated_findings,
            "repairStopped": True,
            "tracePhase": "blocked",
            "instruction": (
                "自动修正已停止。完整校验报告仍由 core 保留；可以读取、删除或撤销候选，"
                "但不要继续生成新的候选文件，直到用户决定下一步。"
            ),
            "display": {"text": "候选自动修正已停止，需要检查校验明细"},
        }

    def claim_stop_notice(self) -> bool:
        if self.stop_notice_emitted:
            return False
        self.stop_notice_emitted = True
        self._notify_change()
        return True

    def decorate(self, report: Mapping[str, Any]) -> dict[str, Any]:
        if self.stopped:
            return self.blocked_result()
        repair = self.record(report)
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
PATH = {"type": "string", "description": "知识包内相对路径"}
CHANGE_GROUP = {
    "type": "string",
    "pattern": "^[a-z][a-z0-9_.-]{0,63}$",
    "maxLength": 64,
    "description": "同一批相关修改使用相同的简短英文标识，例如 goal 或 process_main",
}


CORE_TOOL_SPECS = (
    ToolSpec("list_documents", "列出项目全部资料及修订和解析状态。", {}, display="正在查看资料清单"),
    ToolSpec(
        "read_document",
        "按需分页读取资料正文。局部核对先检索再读取命中范围；需要全文覆盖时必须改用 read_documents_fully，禁止串行翻页替代。",
        {"filename": STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20000}},
        ("filename",),
        display="正在读取资料",
    ),
    ToolSpec(
        "set_material_directive",
        "按用户自然语言或上传卡设置资料的主要用途与阅读方式；用户未指定时不得替用户强行分类。",
        {
            "filename": STRING,
            "purpose": {"type": "string", "enum": ["auto", "goal", "schema", "evidence"]},
            "reading_mode": {"type": "string", "enum": ["on_demand", "full"]},
        },
        ("filename",),
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
    ToolSpec("read_file", "读取知识包文件。", {"path": PATH}, ("path",), display="正在读取知识文件"),
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
    ToolSpec("update_plan", "更新 .modeling/plan.md 的工作拆解；不得在计划里声明当前业务阶段、门禁状态或复制项目状态。", {"markdown": STRING}, ("markdown",), False, "正在更新工作拆解"),
    ToolSpec(
        "run_script_test",
        "在 JiuwenBox 中运行脚本契约测试。",
        {"script": STRING, "cases": {"type": "array", "items": {"type": "object"}}},
        ("script", "cases"),
        False,
        "正在沙箱中测试脚本",
    ),
    ToolSpec(
        "run_evaluation",
        "运行现有评测管道。省略 case_ids 表示全量；仅在验证融合前来源回归时传 source_id。",
        {"case_ids": {"type": "array", "items": STRING}, "source_id": STRING},
        display="正在运行评测",
    ),
    ToolSpec(
        "get_evaluation_results",
        "读取指定或最近一轮真实评测的逐条回答、判定理由和轨迹；失败归因前必须调用。",
        {"round_id": STRING},
        display="正在读取评测结果",
    ),
    ToolSpec("query_eval_db", "只读查询评测数据源。", {"sql": STRING}, ("sql",), display="正在查询评测数据"),
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
        arguments = input_dict(inputs)
        arguments = _bounded_source_arguments(self.spec.name, arguments)
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
        started = time.monotonic()
        try:
            while True:
                done, _pending = await asyncio.wait({call}, timeout=FULL_READ_PROGRESS_INTERVAL_SECONDS)
                if done:
                    result = call.result()
                    break
                elapsed = max(0, int(time.monotonic() - started))
                if self.spec.name == "read_documents_fully":
                    progress_unavailable = False
                    try:
                        progress = await self.context.gateway.call(
                            project_id=self.context.project_id,
                            run_id=self.context.run_id,
                            token=self.context.token,
                            tool="get_material_analysis_progress",
                            arguments={
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
            raise
        if _is_retryable_validation_failure(self.spec.name, result):
            result = self.context.validation_repair.decorate(result)
        elif not result.get("error"):
            self.context.validation_repair.record_success(self.spec.name)
        if not result.get("error") and self.context.after_core_tool is not None:
            await self.context.after_core_tool(self.spec.name, arguments, result)
        return json.dumps(_compact_source_result(self.spec.name, result), ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


def _bounded_source_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    bounded = dict(arguments)
    if name == "read_document":
        bounded["limit"] = _bounded_positive_integer(
            bounded.get("limit"), LEADER_DOCUMENT_PAGE_CHARS,
        )
    elif name == "read_table_rows":
        bounded["limit"] = _bounded_positive_integer(
            bounded.get("limit"), LEADER_TABLE_SAMPLE_ROWS,
        )
    return bounded


def _is_retryable_validation_failure(name: str, result: Mapping[str, Any]) -> bool:
    if name not in VALIDATION_REPAIR_ERROR_TOOLS:
        return False
    has_retryable_error = bool(result.get("error")) and result.get("retryable") is True
    has_report = bool(str(result.get("validationReportId") or "").strip())
    return has_retryable_error and (
        isinstance(result.get("findings"), list) or has_report
    )


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
        return json.dumps(result, ensure_ascii=False)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class FocusTool(Tool):
    def __init__(self, context: ToolContext) -> None:
        super().__init__(
            ToolCard(
                id=f"focus_{context.run_id}",
                name="focus",
                description="控制前端中栏当前展示和高亮。引用文件、流程节点、知识条目、diff 或门禁前先调用。",
                input_params={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["flow", "file", "diff", "doc", "impact", "conflict", "gates", "evaluation", "studio"]},
                        "target": STRING,
                        "node": STRING,
                        "anchor": STRING,
                        "view": {"type": "string", "enum": ["graph", "table", "entry"]},
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
            for key in ("kind", "target", "node", "anchor", "view", "elementId", "data")
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
    return [
        *(ProjectStateTool(spec, context) for spec in PROJECT_STATE_TOOL_SPECS),
        *(CoreTool(spec, context) for spec in CORE_TOOL_SPECS),
        FocusTool(context),
        NotifyTool(context),
        AskTool(context.run_id),
        RequestConfirmationTool(context),
        RequestReleaseTool(context),
    ]


def build_core_tools(context: ToolContext, names: set[str]) -> list[Tool]:
    """Build a least-privilege tool surface for an openJiuwen subagent."""
    return [CoreTool(spec, context) for spec in CORE_TOOL_SPECS if spec.name in names]


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
