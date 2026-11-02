from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

from .agent import AgentBundle, build_agent, build_model, list_skills, output_budget_for_phase
from .core_client import CoreGateway, HttpCoreGateway
from .errors import AgentRuntimeError
from .events import EventHub
from .schemas import (
    ACTIVE_RUN_STATUSES,
    InteractionRecord,
    RECOVERY_CHECKPOINT_MISSING,
    RECOVERY_NONE,
    RECOVERY_RESUME_FAILED,
    RECOVERY_STALE_RUN,
    RECOVERY_VOLUME_MISMATCH,
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_STOPPING,
    RUN_SUSPENDED,
    RUN_WAITING,
    RunRecord,
)
from .settings import Settings
from .store import (
    CANCELLATION_PENDING_REASON,
    INTERRUPTED_CANCELLATION_REASON,
    RuntimeStore,
    token_fingerprint,
)
from .project_transfer import (
    delete_project_transfer,
    export_project_transfer,
    import_project_transfer,
)
from .rails import claims_confirmation_card
from .tools import (
    BUSINESS_PHASE_EDGES,
    BUSINESS_PHASE_LABELS,
    CandidateValidationRepairState,
    EVALUATION_PENDING_MESSAGE,
    PHASE_CAPABILITY_STRATEGY,
    ToolContext,
)
from .visible_text import VisibleReasoningSanitizer, VisibleTextSanitizer, sanitize_visible_text
from .workflows import (
    build_execution_brief_workflow,
    build_tool_redirect_workflow,
    execution_brief_prompt,
    tool_redirect_prompt,
)

logger = logging.getLogger("modeling_agent")
INTERJECTION_CONTINUATION_PREFIX = "interjection-continuation:"
ACTIVE_TOOL_PHASES = frozenset({"started", "repairing"})
TERMINAL_TOOL_PHASES = frozenset({
    "completed", "failed", "blocked", "rejected", "waiting", "cancelled",
})
CANCEL_PENDING_TOOL_PHASE = "cancel_pending"
CANCELLATION_RECONCILIATION_ATTEMPTS = 3
CANCELLATION_RECONCILIATION_DELAY_SECONDS = 0.05


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


PHASE_LABELS = BUSINESS_PHASE_LABELS
PHASE_EDGES = BUSINESS_PHASE_EDGES
PHASE_GATES = {
    "goal": "materials_ready",
    "process": "goal_confirmed",
    "knowledge": "process_confirmed",
    "validation": "knowledge_confirmed",
    "evaluation": "ready_for_eval",
    "release": "releasable",
}
# 阶段自身的门禁。PHASE_GATES 记的是「进入某阶段要过哪道门」，表达不了
# 「在这个阶段内部做到哪一步」。增量轮次的全部工作都在同一阶段内完成，
# 它的前向门禁是全量口径的，拿来当本轮目标会派下基线治理这类本轮不该做的活。
PHASE_SELF_GATES = {"increment": "increment_resolved"}


def _phase_projection() -> list[dict[str, Any]]:
    """阶段顺序、标签和入口门禁的只读投影。

    前端的阶段轨照这份数据渲染，不再自己抄一份阶段表——否则 PHASE_LABELS
    一改就得改两处。increment 是正式基线上的独立轮次，标出来让前端画成支线。
    """
    return [
        {
            "phase": phase,
            "label": label,
            "gate": PHASE_GATES.get(phase),
            "linear": phase != "increment",
        }
        for phase, label in PHASE_LABELS.items()
    ]


MAX_AUTONOMOUS_CONTINUATIONS = 8
MAX_STALLED_CONTINUATIONS = 2
# 会带回台账进度的工具回执；恢复本轮快照时按完成顺序重放这些回执。
PRODUCT_LEDGER_STATE_TOOLS = (
    "begin_product_ledger",
    "append_product_ledger_batch",
    "get_product_ledger_progress",
    "finalize_product_ledger",
    "stage_product_ledger",
    "get_product_ledger",
)
# 计划仍有必选任务未完成时的停滞上限。取证、核对台账、检索原文都不算“进展”，
# 但它们是写入前的正常准备，用 2 轮上限会让计划刚起步就被判成本轮完成。
MAX_STALLED_PLAN_CONTINUATIONS = 5
MAX_DECISION_CARD_RETRIES = 2
# 思维链吃光输出预算导致本轮零产出时的重试上限。截断不是“没事可做”，
# 换一次更克制的推理方式往往就能出结果；连着两次仍然截断说明当前任务
# 在这个预算下想不完，交给用户处理比继续空转有意义。
MAX_TRUNCATION_RETRIES = 2

MAX_PROGRESS_ONLY_CONTINUATIONS = 2
MAX_VISIBLE_REASONING_CHARS = 12_000
REASONING_EVENT_BATCH_CHARS = 160
COMPACT_SUMMARY_MAX_TOKENS = 4_096
COMPACT_SUMMARY_TIMEOUT_SECONDS = 60.0
MAX_TOOL_SUMMARIES = 60
MAX_MODEL_RECOVERY_ATTEMPTS = 1
MODEL_RECOVERY_DELAY_SECONDS = 0.5
RECOVERY_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
# 首轮恢复因瞬时原因失败后的进程内对账间隔：resume_failed 不再是进程内终态，
# 否则一次启动竞态会让项目卡死到下一次服务重启（cs 实测同一批项目连续 23 次重启均恢复失败）。
RECOVERY_RECONCILE_DELAYS = (60.0, 300.0, 900.0)
# 启动恢复前等待建模核心就绪的上限；等不到也继续，交给每次恢复自身的退避与对账。
CORE_READY_TIMEOUT_SECONDS = 60.0
CORE_READY_PROBE_INTERVAL_SECONDS = 2.0
# 瞬时性按「错误族」判断而不是单一错误码：发布分支曾新增 CORE_CANCELLATION_UNAVAILABLE
# （停止栅栏探测）这类 503 新码，单码判据会让退避重试对它直接失明。
TRANSIENT_CORE_ERROR_CODES = frozenset({
    "CORE_UNAVAILABLE",
    "CORE_CANCELLATION_UNAVAILABLE",
})
TRANSIENT_CORE_ERROR_STATUSES = frozenset({502, 503, 504})


def _is_transient_core_error(error: Exception) -> bool:
    if not isinstance(error, AgentRuntimeError):
        return False
    return (
        error.code in TRANSIENT_CORE_ERROR_CODES
        or error.status in TRANSIENT_CORE_ERROR_STATUSES
    )


EVALUATION_LAUNCH_SETTLE_TIMEOUT_SECONDS = 3.5
AUTONOMOUS_PROGRESS_TOOLS = frozenset({
    # 切换阶段是明确的前进。不计入的话，「刚进入新阶段、还没来得及产出」
    # 会和「无事可做」判成同一件事，两轮后直接收工（见 #35）。
    "transition_project_state",
    "create_change_plan",
    "revise_change_plan",
    "review_object_relations",
    "stage_change_set",
    "validate_change_set",
    "discard_change_set",
    "analyze_material_impact",
    "stage_tabular_dataset",
    "update_work_notes",
    "write_file",
    "upsert_object",
    "delete_file",
    "upsert_rule",
    "upsert_operation",
    "bind_object_source",
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
    "confirm_deterministic_scope",
    "define_script_contract",
    "stage_script_source",
    "stage_script_patch",
    "bind_object_sources",
    "discard_rejected_candidate_changes",
    "regroup_changes",
    "create_package_fusion_plan",
    "update_package_fusion_plan",
    "compile_package_fusion_plan",
    "transition_project_state",
    "run_script_test",
    "cancel_evaluation",
})

# 脚本契约/确定性建模交互要求本轮形成实际产物：新契约、脚本候选、脚本测试或
# 范围确认结果。空正文紧凑摘要恢复时，若这些工具没有任何完成记录且没有生成
# 下一张确认卡，不得把 Run 结算为成功完成，否则会形成“只有摘要没有产物”的假完成。
SCRIPT_ARTIFACT_TOOLS = frozenset({
    "define_script_contract",
    "stage_script_source",
    "confirm_deterministic_scope",
    "run_script_test",
    "bind_object_sources",
})
# 专家已经批准契约之后，重新定义一遍契约不算交付物——define_script_contract
# 产出的正是被批准的那个对象本身。把它留在期望产物里，等于允许“确认完再原地
# 重定义一次契约”通过检查：线上实测正是这条路把一个没有任何后续产物的运行
# 判成了成功完成，页面上待办消失、也没有下一张卡。
CONTRACT_ACCEPTED_ARTIFACT_TOOLS = SCRIPT_ARTIFACT_TOOLS - {"define_script_contract"}
# 表示“按当前契约继续实现”的交互动作。这些动作之后必须出现新的脚本产物。
CONTRACT_ACCEPTED_ACTION_KEYS = frozenset({"accept_contract"})
# 整体确认卡连续未形成时，候选修改仍然是可恢复的中间状态。
# 该恢复不依赖用户重新描述业务，也不把内部工具参数暴露给用户。
CONFIRMATION_REVIEW_STRATEGY = "confirmation_review"
CONFIRMATION_REVIEW_REQUIRED_TOOL = "list_domain_resources"
# 思维链占满输出预算导致零产出时的恢复策略。它没有 requiredTool——要换的是
# 推进方式，不是补一次工具调用，因此需要一条自己的续跑提示。
MODEL_TRUNCATION_STRATEGY = "model_truncation"


def _confirmation_review_actions() -> list[dict[str, str]]:
    return [
        {
            "id": "retry_from_latest",
            "label": "重新复核",
            "description": "重新读取当前内容，继续形成整体确认卡。",
        },
        {
            "id": "keep_current_progress",
            "label": "保留当前成果，结束本轮",
            "description": "保留已经形成的候选修改，暂不继续本轮处理。",
        },
    ]


# 评测返回业务门禁失败时已经完成了用户要求的“执行评测”；其余必需工具
# 会产生成果或完成证明，只有成功返回才算满足本轮要求。
FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL = frozenset({"run_evaluation"})
# 已批准计划执行必须闭合候选写入、校验和统一确认卡，不能用进度文字结算。
APPROVED_PLAN_REQUIRED_TOOLS = frozenset({"stage_change_set", "validate_change_set", "request_confirmation"})
PURPOSE_REVIEW_ALLOWED_TOOLS = frozenset({
    "get_project_state", "list_documents", "list_document_outline", "read_document",
    "list_tables", "describe_table",
    "set_material_directive", "read_file", "list_skill", "focus", "notify",
})
PHASE_ORDER = tuple(PHASE_LABELS)


class Verdict(Enum):
    """状态核对的三态结论。

    UNKNOWN 不是 DENY：还没观察到状态时不能当成「确认为否」，
    否则会把用户真实的诉求吞掉。每条规则自己声明拿不准时偏哪边，
    依据是「猜错了哪边代价大」。
    """

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass
class RunSituation:
    """本轮运行的权威状态快照。

    决策一律读这里：要不要继续、必须调用什么工具，都由状态回答，
    不再靠正则猜用户措辞、也不靠工具名单猜有没有干活。
    字段只由核心工具结果回填，Python 侧不做二次推导——真相在核心侧，
    这里重新实现一遍就会产生第二个真相源。

    每个字段都配一个 *_observed 标志：还没观察到时是「拿不准」，
    不能和「确认为否」混为一谈，否则会吞掉用户的真实诉求。
    """

    materials_observed: bool = False
    full_read_materials: tuple[str, ...] = ()
    # 资料修订级别的全文覆盖事实。仅在核心返回 coverage=100% 后回填，
    # 不能把“readingMode=full”误当作已经完成全文分析。
    completed_full_read_materials: tuple[str, ...] = ()
    # filename -> revision fingerprint for the authoritative 100% coverage.
    # A filename alone is not sufficient: replacing a document must invalidate
    # the previous completion and require a fresh analysis.
    completed_full_read_fingerprints: dict[str, str] = field(default_factory=dict)
    # 台账工具返回的确定性校验快照，供恢复和回执守卫使用。
    product_ledger_status: str = ""
    product_ledger_counts: dict[str, int] = field(default_factory=dict)
    product_ledger_id: str = ""
    product_ledger_last_batch: int | None = None
    product_ledger_processed: int = 0
    product_ledger_remaining: int | None = None
    product_ledger_analysis_revision: str = ""

    plan_observed: bool = False
    plan_remaining: int = 0
    plan_next_task: str = ""

    # 本轮切换过阶段但新阶段尚无产出。用于区分「刚进新阶段」和「本阶段做完了」。
    stage_started_without_delivery: bool = False

    # 剩余阻塞项全部要用户在别的界面自己完成。助手再宣告多少遍也推不动，
    # 继续轮转只会一轮轮重复同一段话（见 #39）。
    awaiting_user_action: bool = False
    awaiting_user_reason: str = ""

    # 门禁未过，但服务端明确标注这道门禁没有助手能独立推进的事项。
    # 和 awaiting_user_action 同义，来源不同：那个来自模型自己调门禁的返回值，
    # 四小时里只发生过一次；这个来自每轮续跑前的目标指派，每轮都会算。
    no_actionable_gate: str = ""
    no_actionable_reason: str = ""

    def has_full_read_materials(self) -> Verdict:
        if not self.materials_observed:
            return Verdict.UNKNOWN
        return Verdict.ALLOW if self.full_read_materials else Verdict.DENY

    def has_completed_full_read(self) -> bool:
        return bool(self.completed_full_read_materials)


@dataclass
class LiveRun:
    run_id: str
    project_id: str
    token: str
    tool_profile: str = "domain-v2"
    steering: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    task: asyncio.Task | None = None
    bundle: AgentBundle | None = None
    restore_interaction: InteractionRecord | None = None
    required_tools: set[str] = field(default_factory=set)
    attempted_tools: set[str] = field(default_factory=set)
    observed_tools: set[str] = field(default_factory=set)
    required_material_directives: set[str] = field(default_factory=set)
    observed_material_directives: set[str] = field(default_factory=set)
    allowed_tools: frozenset[str] | None = None
    proof_retries: dict[str, int] = field(default_factory=dict)
    release_reentry_authorized: bool = False
    release_declined: bool = False
    confirmation_required: bool = False
    autonomous_seed: bool = False
    # 本轮的权威状态快照。所有「要不要继续」「必须调什么工具」的判断都读它。
    situation: RunSituation = field(default_factory=RunSituation)
    # 被状态否决掉的强制工具，对应的说明。必须送进提示词，否则模型只会看到
    # 「消息里提了某件事却毫无动静」，然后自己反复推敲（见 #1 的原地打转）。
    intent_notes: list[str] = field(default_factory=list)
    # 打转检测的痕迹：最近若干次(工具,参数)指纹，以及每轮输出的指纹。
    # 「没产出」和「卡住」是两件事——今天见过助手逐字重复同一段推理却因为
    # 期间调过写工具而不被计为停滞，现有机制对真打转完全无感（见 #35 讨论）。
    recent_tool_calls: list[str] = field(default_factory=list)
    recent_outputs: list[frozenset[str]] = field(default_factory=list)
    # 同一工具连续失败：参数每次都变，靠「同参数重复」抓不到，
    # 但反复撞同一个错误同样是打转（见 #40）。
    consecutive_tool_failures: tuple[str, int] = ("", 0)
    tool_summaries: list[str] = field(default_factory=list)
    stable_read_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    reasoning_chars_published: int = 0
    reasoning_truncated: bool = False
    package_contract: dict[str, Any] = field(default_factory=dict)
    # 用户在开始建模前确认过的创建目标；"" = 未填写或存量项目
    project_goal: str = ""
    validation_repair: CandidateValidationRepairState = field(default_factory=CandidateValidationRepairState)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    inflight_core_calls: set[asyncio.Task[Any]] = field(default_factory=set)
    plan_approval_required: bool = False
    technical_recoveries: dict[str, dict[str, Any]] = field(default_factory=dict)
    deferred_purpose_continuation: tuple[int, str, str] | None = None
    evaluation_launch_active: bool = False
    # 最近一次 requiredText 答案的业务约束。它与交互恢复解耦，服务重启后
    # 可从 RunRecord.latest_interaction 重新装载，避免只依赖框架的 tool result。
    required_text_constraint: dict[str, Any] | None = None
    required_text_guard_retries: int = 0
    # Once set, non-terminal tool/progress events for this run are suppressed.
    # The persisted Run status is the restart-safe fence; this flag closes the
    # in-process race between task cancellation and callback delivery.
    cancel_requested: bool = False
    # 最近一次主模型调用的度量。ActivityRail 把 model_call 事件发到同一个 emit，
    # 这里顺手留存，用于判断本轮是不是被思维链吃光了输出预算。
    last_model_call: dict[str, Any] = field(default_factory=dict)
    # 本轮实际生效的模型配置（平台下发的运行时配置优先）。紧凑摘要直接调用
    # model.stream 覆盖 max_tokens，需要按同一份配置换算预算。
    model_settings: Settings | None = None


def _missing_required_tools(live: LiveRun) -> set[str]:
    terminal_attempts = live.required_tools & FAILED_REQUIRED_TOOL_ATTEMPT_IS_TERMINAL
    successful_results = live.required_tools - terminal_attempts
    missing = (terminal_attempts - live.attempted_tools) | (successful_results - live.observed_tools)
    # 服务端确认的全文阅读强于单页读取，不要让模型被迫重复读取同一份资料。
    if "read_document" in missing and "read_documents_fully" in live.observed_tools:
        missing.remove("read_document")
    if live.required_material_directives - live.observed_material_directives:
        missing.add("set_material_directive")
    return missing


def _missing_material_directives(live: LiveRun) -> set[str]:
    return live.required_material_directives - live.observed_material_directives


def _unsatisfiable_required_tools(live: LiveRun, missing: set[str]) -> set[str]:
    """缺失的必需工具全部真实调用过仍未完成时，判定该要求疑似不可满足。

    工具被调用过却始终没有 completed，通常是服务端门禁在确定性拒绝，而不是
    助手在回避调用（产品台账误判事故）。但"调用失败"也可能只是"还没做完"
    （如全文精读分段未完成），服务端不能据此单方面撤销要求——只把判定结果
    标注到恢复卡上，让专家知情后自行选择"取消该要求"。
    """
    if missing and missing <= live.attempted_tools:
        return set(missing)
    return set()


def _expert_answer_intent_text(answer: dict[str, Any]) -> str:
    """只保留专家自己给出的答复内容参与意图判定。

    卡片的问题正文和选项文案都出自助手，一旦参与意图判定，助手描述建模
    内容的行文（如「终产品……10 项」）会被误判成用户诉求，形成不可满足
    的必需工具要求。displayText 是选项文案的回显，一并剔除。
    """
    return json.dumps(
        {key: value for key, value in (answer or {}).items() if key != "displayText"},
        ensure_ascii=False,
    )


# 「已满足/已完成」处置的证据锚点语法：type:值，分号分隔，可附说明文字。
# 锚点必须指向真实存在的记录——这是防谎报处置的确定性防线。
_EVIDENCE_ANCHOR_PATTERN = re.compile(
    r"^(constraint|task|interjection|interaction|revision|file|receipt|report|change_set|mutation)"
    r"\s*[:：]\s*(.+)$",
    re.IGNORECASE,
)

# LIKE 类锚点的最小长度：太短的标识（如 file:a）会匹配到任何记录，验真失效。
_EVIDENCE_TOKEN_MIN_LENGTH = 4


def _parse_evidence_anchors(evidence: str) -> list[tuple[str, str]]:
    """把 evidence 拆成锚点列表；非 type:值 形状的片段视为说明文字忽略。"""
    anchors: list[tuple[str, str]] = []
    for part in re.split(r"[;；\n]", str(evidence or "")):
        entry = part.strip()
        if not entry:
            continue
        match = _EVIDENCE_ANCHOR_PATTERN.match(entry)
        if match:
            anchors.append((match.group(1).lower(), match.group(2).strip()))
    return anchors

def _is_blocked_phase_capability(recovery: dict[str, Any]) -> bool:
    """判断这条恢复是否为已被门禁挡死、需要恢复卡兜底的阶段能力写入。"""
    if recovery.get("strategy") != PHASE_CAPABILITY_STRATEGY:
        return False
    return recovery.get("blocked") is True


def _interaction_requires_artifact(interaction: InteractionRecord) -> bool:
    """判断交互是否要求本轮形成实际脚本产物（新契约/候选/确认卡）。

    只对脚本契约与确定性范围确认类交互开启：这类交互回答后如果模型返回空正文，
    必须确认本轮真的形成了新契约或候选，否则不得把 Run 结算为成功完成。
    """
    kind = str(interaction.kind or "")
    payload = interaction.payload or {}
    if kind == "scope_confirmation":
        return True
    if kind in {"confirmation", "release_confirmation"}:
        return False
    question = str(payload.get("question") or "")
    contract_summary = str(payload.get("contractSummary") or "")
    summary = str(payload.get("summary") or "")
    return (
        "脚本契约" in question
        or "script contract" in question.lower()
        or "脚本契约" in contract_summary
        or "script contract" in contract_summary.lower()
        or "脚本契约" in summary
        or "script contract" in summary.lower()
    )


_SCRIPT_SKIP_ARTIFACT_INTENT = re.compile(
    r"(?:本轮)?不生成|不调整|跳过|无需生成|skip",
    re.IGNORECASE,
)


def _answer_requires_script_artifact(
    interaction: InteractionRecord,
    answer: dict[str, Any],
) -> bool:
    """按专家答案判断本轮是否必须形成脚本产物。

    交互本身要求脚本产物时，如果专家明确选择“本轮不生成/不调整/跳过”，
    空正文恢复不应再强制要求新契约或候选，否则会把合法的“不生成”误判为失败。
    """
    if not _interaction_requires_artifact(interaction):
        return False
    # 豁免只认专家选中的选项身份（value/action/selected/displayText），
    # 不扫自由文本：答复里写「不调整 A，但请生成 B」不构成对产物要求的
    # 豁免，否则一句带否定词的补充说明就会静默放水。
    selected_parts = [
        str(answer.get("value") or ""),
        str(answer.get("action") or ""),
        str(answer.get("displayText") or ""),
    ]
    selected_parts.extend(
        str(item) for item in (answer.get("selected") or []) if item is not None
    )
    joined = "\n".join(part for part in selected_parts if part)
    return not bool(_SCRIPT_SKIP_ARTIFACT_INTENT.search(joined))


def _required_text_constraint(
    interaction: InteractionRecord,
    answer: dict[str, Any],
) -> dict[str, Any] | None:
    """将 requiredText 答案规范化为可恢复、可审计的业务约束。

    选项卡的 label/value 只是 UI 选择；真正需要交给模型执行的是用户在
    ``answer.text`` 中填写的原文。恢复卡也会在 payload 中携带同一约束，
    允许用户选择“按原说明继续”而无需再次复制长文本。
    """
    existing = interaction.payload.get("requiredTextConstraint")
    replacing = str(answer.get("value") or "").strip() == "edit_required_text"
    if not replacing and isinstance(existing, dict) and str(existing.get("text") or "").strip():
        inherited = dict(existing)
    else:
        option = _required_text_option(interaction.payload, answer)
        text = str(answer.get("text") or "").strip()
        if option is None or not text:
            return None
        value = str(option.get("value") or option.get("label") or "").strip()
        inherited = {
            "required": True,
            "optionValue": value,
            "optionLabel": str(option.get("label") or value).strip(),
            "text": text[:8_000],
        }
    text = str(inherited.get("text") or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    contract = _parse_required_text_contract(text)
    return {
        **inherited,
        "required": True,
        "question": str(
            inherited.get("question")
            or interaction.payload.get("question")
            or ""
        )[:4_000],
        "text": text[:8_000],
        "textHash": str(inherited.get("textHash") or f"sha256:{digest}"),
        "contract": contract,
    }


def _parse_required_text_contract(text: str) -> dict[str, Any]:
    """Extract a small, auditable contract from free-form requiredText.

    This is deliberately additive: the original Chinese text remains the
    authority, while common count/preservation/prohibition clauses become
    machine-checkable fields for recovery receipts.
    """
    count_tokens = re.findall(
        r"(?<![A-Za-z0-9])\d+\s*(?:条|项|组|个|份|张|件|条目)", text,
    )
    expected_counts = []
    for token in count_tokens:
        expected_counts.append(int(re.sub(r"\D", "", token)))
    preserve_ids = bool(re.search(
        r"(?:保留|沿用|不修改|不得修改).{0,40}(?:既有|现有|已有)?\s*(?:产品\s*)?(?:ID|id|标识)|"
        r"(?:ID|id|标识).{0,20}(?:保留|沿用|不修改|不得修改)",
        text,
    ))
    preserve_package = bool(re.search(r"(?:不修改|不得修改|不改动).{0,12}知识包", text))
    prohibited_clauses = re.findall(
        r"(?:禁止|不要|不再|无需|不用|不必|不得|避免)([^，。；\n]{1,80})", text,
    )
    prohibited_actions = []
    for clause in prohibited_clauses:
        normalized_clause = re.sub(r"\s+", "", clause).strip("，。；、,; ")
        if normalized_clause:
            prohibited_actions.append(normalized_clause)
    return {
        "expectedCounts": list(dict.fromkeys(expected_counts)),
        "preserveIds": preserve_ids,
        "preserveKnowledgePackage": preserve_package,
        "prohibitedActions": list(dict.fromkeys(prohibited_actions)),
    }


def _required_text_constraint_from_snapshot(
    snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("requiredTextConstraint")
    if isinstance(value, dict) and str(value.get("text") or "").strip():
        return dict(value)
    if snapshot.get("requiredText") is not True:
        return None
    answer = snapshot.get("answer")
    text = answer.get("text") if isinstance(answer, dict) else None
    if not str(text or "").strip():
        return None
    normalized = {
        "required": True,
        "question": str(snapshot.get("question") or "")[:4_000],
        "optionValue": str(answer.get("value") or "").strip(),
        "optionLabel": str(answer.get("displayText") or answer.get("value") or "").strip(),
        "text": str(text).strip()[:8_000],
    }
    digest = hashlib.sha256(normalized["text"].encode("utf-8")).hexdigest()
    normalized["textHash"] = f"sha256:{digest}"
    normalized["contract"] = _parse_required_text_contract(normalized["text"])
    return normalized


def _required_text_prompt(constraint: dict[str, Any]) -> str:
    text = str(constraint.get("text") or "").strip()
    return (
        "本轮必须遵守用户在决策卡中填写的补充说明。以下内容是用户原文，"
        "不得丢弃、改写为模糊要求或用旧计划替代：\n"
        f"【{text}】\n"
        f"结构化回执契约：{json.dumps(constraint.get('contract') or _parse_required_text_contract(text), ensure_ascii=False, sort_keys=True)}\n"
        "请逐项核对并执行；最终回复必须明确说明每项已满足，或指出具体无法满足的原因。"
        "如果当前资料或工具不足以满足任一项，必须调用 ask 生成下一张可操作的问题卡，"
        "不能只在自然语言中写“待专家明确后继续”。"
    )


def _contains_any_token(value: str, tokens: tuple[str, ...] | list[str]) -> bool:
    for token in tokens:
        if token in value:
            return True
    return False


def _contains_all_tokens(value: str, tokens: list[str]) -> bool:
    for token in tokens:
        if token not in value:
            return False
    return True


def _required_text_response_acknowledged(
    output: str,
    constraint: dict[str, Any],
) -> bool:
    """判断最终正文是否至少明确承接了补充说明。

    这里不要求模型逐字复述长文本，避免把业务说明泄漏到重复摘要中；
    但必须出现承接/执行类措辞，且不能仅以“缺少资料”结束。
    """
    normalized = re.sub(r"\s+", "", str(output or ""))
    if not normalized:
        return False
    # “不能修改/不能创建/不能重读” can be the user's prohibition being
    # correctly honored. Only treat negative wording as failure when it
    # describes an inability or missing evidence, not a prohibited action.
    if re.search(r"(?:无法|缺少|不足以|未能|信息不足|尚未提供)|不能(?!修改|创建|删除|重读|再次|全文|绕过|改动)", normalized):
        return False
    if not _contains_any_token(normalized, (
        "已收到", "已按你的补充", "已按补充说明", "已按要求",
        "已逐项", "已保留", "不会修改", "不修改知识包", "已核对",
        "已根据", "已记录", "已采用", "按口述", "无需修改", "无需调整",
        "保持当前", "补充说明已", "约束已",
        "遵守", "禁止", "不会创建", "不会修改", "不能修改", "不得修改",
        "不会删除", "不会重读",
    )):
        return False
    # 用户给出两个以上精确数量时，至少要在回执中逐项回显这些数量，
    # 避免“已按要求”掩盖 201/198/3 等统计没有真正执行。
    required_numbers: list[str] = []
    for item in re.findall(
        r"(?<![A-Za-z0-9])\d+\s*(?:条|项|组|个|份|张|件|条目)",
        str(constraint.get("text") or ""),
    ):
        required_numbers.append(re.sub(r"\D", "", item))
    if len(required_numbers) >= 2 and not _contains_all_tokens(normalized, required_numbers):
        return False
    requirement_text = re.sub(r"\s+", "", str(constraint.get("text") or ""))
    if "不修改知识包" in requirement_text and not _contains_any_token(
        normalized,
        ("不修改知识包", "知识包保持不变", "不改动知识包"),
    ):
        return False
    return True


def _required_text_constraint_status(
    output: str,
    constraint: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Return an auditable response status instead of treating one keyword as proof.

    The guard remains intentionally conservative: exact counts and explicit
    prohibitions must be echoed in a successful receipt; otherwise the run is
    routed to a review card and cannot silently proceed to generation.
    """
    missing: list[str] = []
    normalized = re.sub(r"\s+", "", str(output or ""))
    text = re.sub(r"\s+", "", str(constraint.get("text") or ""))
    if not normalized:
        missing.append("response")
    if re.search(r"(?:无法|缺少|不足以|未能|信息不足|尚未提供)|不能(?!修改|创建|删除|重读|再次|全文|绕过|改动)", normalized):
        missing.append("reason_for_unmet_constraint")
    for item in re.findall(r"(?<![A-Za-z0-9])\d+\s*(?:条|项|组|个|份|张|件|条目)", str(constraint.get("text") or "")):
        number = re.sub(r"\D", "", item)
        if number and number not in normalized:
            missing.append(f"count:{number}")
    if "不修改知识包" in text and not _contains_any_token(normalized, ("不修改知识包", "知识包保持不变", "不改动知识包")):
        missing.append("preserve_knowledge_package")
    return not missing and _required_text_response_acknowledged(output, constraint), missing


def _required_text_interrupt_acknowledged(
    result: dict[str, Any],
    constraint: dict[str, Any],
) -> bool:
    interaction = result.get("interaction")
    if not isinstance(interaction, InteractionRecord):
        return False
    if interaction.kind in {"confirmation", "release_confirmation"}:
        return True
    question = re.sub(r"\s+", "", str(interaction.payload.get("question") or ""))
    original = re.sub(r"\s+", "", str(constraint.get("question") or ""))
    if not question or (original and question == original):
        return False
    # 新卡必须明确承接约束或说明还缺什么，不能换一种说法重复原问题。
    return _contains_any_token(question, (
        "补充说明", "业务约束", "无法满足", "不能满足", "仍需补充",
        "缺少", "证据", "具体原因", "按原", "调整补充", "唯一目标",
        "明确范围", "具体目标",
    ))


def _required_text_interrupt_requires_review(
    result: dict[str, Any],
    live: LiveRun,
    guard_mode: str,
) -> bool:
    if result.get("result_type") != "interrupt":
        return False
    constraint = live.required_text_constraint
    if not constraint or guard_mode == "off":
        return False
    return not _required_text_interrupt_acknowledged(result, constraint)


def _required_text_response_requires_review(
    result: dict[str, Any],
    live: LiveRun,
    guard_mode: str,
) -> bool:
    if result.get("result_type") != "success":
        return False
    constraint = live.required_text_constraint
    if not constraint or guard_mode == "off":
        return False
    acknowledged, _missing = _required_text_constraint_status(
        str(result.get("output") or ""),
        constraint,
    )
    return not acknowledged


def _purpose_review_suggestions_complete(live: LiveRun) -> bool:
    return (
        bool(live.required_material_directives)
        and live.allowed_tools == PURPOSE_REVIEW_ALLOWED_TOOLS
        and not _missing_required_tools(live)
    )


_LANGUAGE_REMINDER = (
    "\n\n【输出语言】本轮的内部推理和用户可见正文都必须使用简体中文。"
    "不要用英文推理，也不要中英混写整句；工具名、字段名、表名、代码标识符保留原文。"
)


LOOP_TOOL_REPEAT_THRESHOLD = 3
# 同一工具连续失败多少次算卡住。取 5：正常的自我纠错一两次就该成功。
LOOP_TOOL_FAILURE_THRESHOLD = 5
LOOP_OUTPUT_REPEAT_THRESHOLD = 2
# 归一化后 4-gram 的 Jaccard 相似度阈值：换个说法重复也算打转。
# 实测同领域但内容不同的相邻输出 ≤0.12，换说法重复 ≥0.25，取中间偏保守值。
LOOP_OUTPUT_SIMILARITY = 0.22
LOOP_TRACE_WINDOW = 12


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def _situation_of(live: LiveRun | None) -> RunSituation | None:
    """还没有活动运行时返回 None，让规则按「拿不准」处理。"""
    return live.situation if live is not None else None


def _record_tool_call(live: LiveRun, name: str, args: Any) -> None:
    live.recent_tool_calls.append(f"{name}:{_fingerprint(args)}")
    del live.recent_tool_calls[:-LOOP_TRACE_WINDOW]


def _record_tool_outcome(live: LiveRun, name: str, failed: bool) -> None:
    """同一工具连续失败计数。参数每次都变时「同参数重复」判据完全失效，
    但连着二十几次撞同一个错误显然是卡住了。"""
    if not failed:
        live.consecutive_tool_failures = ("", 0)
        return
    previous, count = live.consecutive_tool_failures
    live.consecutive_tool_failures = (name, count + 1 if previous == name else 1)


_LOOP_NOISE = re.compile(r"[\s，。、；：！？…—·「」『』（）()\[\]{}<>\"'`*#\-]+")


def _shingles(text: str) -> frozenset[str]:
    """归一化后的字符 4-gram。用于识别「换个说法再说一遍」——
    真实的打转极少逐字相同，只比对完全相同的指纹会全部漏掉（见 #39）。"""
    normalized = _LOOP_NOISE.sub("", str(text or ""))
    # 太短的输出（「好的」「完成」）没有判别力，不参与判定
    if len(normalized) < 8:
        return frozenset()
    # 短句不足以切出有意义的 4-gram，按整体比对：重复三遍的短句同样是打转
    if len(normalized) < 12:
        return frozenset({normalized})
    return frozenset(normalized[i:i + 4] for i in range(len(normalized) - 3))


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _record_turn_output(live: LiveRun, output: str) -> None:
    marks = _shingles(output)
    if not marks:
        return
    live.recent_outputs.append(marks)
    del live.recent_outputs[:-LOOP_TRACE_WINDOW]


def _detect_loop(live: LiveRun) -> str:
    """识别真正的原地打转，返回原因；不是打转则返回空串。

    与「没产出」区分开：没产出可能只是在取证准备，而同一调用或同一段输出
    反复出现才是卡住。后者原有机制完全检测不到——只要期间碰巧写过一次，
    停滞计数就归零，可以一直转下去。
    """
    failing_tool, failures = live.consecutive_tool_failures
    if failures >= LOOP_TOOL_FAILURE_THRESHOLD:
        return f"{failing_tool} 连续失败 {failures} 次"
    if live.recent_tool_calls:
        latest = live.recent_tool_calls[-1]
        repeats = 0
        for item in reversed(live.recent_tool_calls):
            if item != latest:
                break
            repeats += 1
        if repeats >= LOOP_TOOL_REPEAT_THRESHOLD:
            return f"连续 {repeats} 次以相同参数调用同一工具"
    if len(live.recent_outputs) > LOOP_OUTPUT_REPEAT_THRESHOLD:
        latest = live.recent_outputs[-1]
        near = sum(
            1 for item in live.recent_outputs[:-1]
            if _similarity(item, latest) >= LOOP_OUTPUT_SIMILARITY
        )
        if near >= LOOP_OUTPUT_REPEAT_THRESHOLD:
            return f"最近 {near + 1} 轮在重复同样的内容"
    return ""


def _model_output_truncated(metrics: dict[str, Any]) -> bool:
    """这次主模型调用是否被思维链吃光了输出预算。

    判据是「推理 token 用满了全部输出 token，且一个正文字符都没有」：这说明
    预算全被思维链占掉，正文和工具调用都没来得及产生。

    两个条件缺一不可。只看「输出预算用满」会误判——线上实测有多次调用把预算
    全部用掉，却仍然正常吐出了 tool_calls。只看字符数更不行：截断轮与成功轮的
    推理字符数互有高低（成功轮到过 14655 字符，截断轮是 15568 字符），真正的
    分界线在 token 上。
    """
    if not metrics or metrics.get("outcome") == "tool_calls":
        return False
    output_tokens = int(metrics.get("outputTokens") or 0)
    reasoning_tokens = int(metrics.get("reasoningTokens") or 0)
    if output_tokens <= 0 or reasoning_tokens < output_tokens:
        return False
    return not int(metrics.get("contentChars") or 0)


# 截断后的续跑提示。截断的成因是模型想在一次思考里把整套方案推演完，
# 因此这里要求它把问题切小、用工具取证代替空想，而不是简单地重试一遍。
TRUNCATION_RECOVERY_PROMPT = (
    "上一轮你把全部输出预算都用在了推理上，没有产出任何工具调用或正文，本轮必须换一种做法。"
    "不要试图在一次思考里把整套方案想完：只做下一步最小的一件事，"
    "先用一次工具调用去取证或落地，拿到真实结果再决定下一步。"
    "如果关键前提还不确定，直接调用 ask 把问题交给专家，不要自己反复推演。"
)


def _with_intent_notes(live: LiveRun, query: Any) -> Any:
    """把被状态否决的强制工具说明并进本轮 query，消费一次即清空。

    否决如果静默发生，模型只会看到「消息里提了某件事却毫无动静」，
    然后在矛盾里反复推敲——这正是 #1 现场那段逐字重复的推理。
    """
    if not live.intent_notes:
        return query
    notes = "\n".join(f"- {note}" for note in dict.fromkeys(live.intent_notes))
    live.intent_notes.clear()
    if not isinstance(query, str):
        return query
    return f"{query}\n\n【状态核对】\n{notes}"


def _with_language_reminder(query: Any) -> Any:
    """系统提示词里的语言约束压不住模型的英文思维链先验，
    在每轮 query 末尾就近提醒一次（见 #4）。非字符串 query 原样返回。"""
    if not isinstance(query, str) or not query.strip():
        return query
    if "【输出语言】" in query:
        return query
    return query + _LANGUAGE_REMINDER


def _segment_separator(previous_tail: str) -> str:
    """多轮模型输出会拼进同一条消息；上一段没有自带断句时补一个空行，
    否则「执行全文精读。」和「全文精读完成」会粘成同一句话（见 #6）。"""
    if not previous_tail or previous_tail[-1] in "\n \t":
        return ""
    return "\n\n"


class _ReasoningEmitter:
    def __init__(self, events: EventHub, live: LiveRun, run: RunRecord) -> None:
        self._events = events
        self._live = live
        self._run = run
        self._sanitizer = VisibleReasoningSanitizer()
        self._pending = ""
        self._emitted = False
        self._observed = False
        self._segment_break = False
        self._tail = ""

    def mark_segment_break(self) -> None:
        """本轮推理已结束（中间夹了工具调用或正文输出），下一段另起。"""
        if self._observed:
            self._segment_break = True

    @property
    def seen(self) -> bool:
        return self._observed

    def feed(self, raw_text: str) -> None:
        if not raw_text or self._live.cancel_requested or self._live.reasoning_truncated:
            return
        chunk = self._sanitizer.feed(raw_text)
        if chunk and self._segment_break:
            self._pending += _segment_separator(self._pending or self._tail)
            self._segment_break = False
        self._observed = True
        self._pending += chunk
        if chunk:
            self._tail = chunk[-1:]
        if len(self._pending) >= REASONING_EVENT_BATCH_CHARS or "\n" in self._pending:
            self._flush()

    def finish(self) -> None:
        if self._live.cancel_requested:
            self._pending = ""
            return
        if not self._live.reasoning_truncated:
            self._pending += self._sanitizer.finish()
        self._flush()
        if self._emitted:
            self._events.publish(self._run.project_id, self._run.id, "reasoning_done", {
                "messageId": f"reasoning_{self._run.id}",
                "role": "assistant",
            })

    def _flush(self) -> None:
        if self._live.cancel_requested:
            self._pending = ""
            return
        if not self._pending:
            return
        remaining = MAX_VISIBLE_REASONING_CHARS - self._live.reasoning_chars_published
        if remaining <= 0:
            self._pending = ""
            self._publish_truncated()
            return
        delta = self._pending[:remaining]
        self._pending = self._pending[remaining:]
        if delta:
            self._emitted = True
            self._live.reasoning_chars_published += len(delta)
            self._events.publish(self._run.project_id, self._run.id, "reasoning_delta", {
                "messageId": f"reasoning_{self._run.id}",
                "role": "assistant",
                "delta": delta,
            })
        if self._pending or self._live.reasoning_chars_published >= MAX_VISIBLE_REASONING_CHARS:
            self._pending = ""
            self._publish_truncated()

    def _publish_truncated(self) -> None:
        if self._live.cancel_requested or self._live.reasoning_truncated:
            return
        self._live.reasoning_truncated = True
        self._emitted = True
        self._events.publish(self._run.project_id, self._run.id, "reasoning_delta", {
            "messageId": f"reasoning_{self._run.id}",
            "role": "assistant",
            "delta": "\n\n（思考过程较长，后续内容已收起）",
            "truncated": True,
        })


class AgentService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: RuntimeStore | None = None,
        gateway: CoreGateway | None = None,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or RuntimeStore(settings.runtime_db)
        self.events = EventHub(self.store)
        self._owns_gateway = gateway is None
        self._gateway = gateway or HttpCoreGateway(
            settings.core_base_url,
            service_token=settings.service_token,
        )
        self._model_factory = model_factory
        self._live: dict[str, LiveRun] = {}
        self._started = False
        self._shutting_down = False
        self._storage_diag: dict[str, Any] = {}
        self._recovery_summary: dict[str, Any] = {}
        self._cancel_locks: dict[str, asyncio.Lock] = {}
        self._recovery_tasks: set[asyncio.Task[None]] = set()

    async def startup(self) -> None:
        if self._started:
            return
        self._shutting_down = False
        from openjiuwen.core.runner.runner import Runner
        from openjiuwen.core.runner.runner_config import (
            DistributedConfig,
            MessageQueueConfig,
            MessageQueueType,
            RunnerConfig,
        )
        from openjiuwen.core.session.checkpointer.checkpointer import CheckpointerConfig

        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        Runner.set_config(
            RunnerConfig(
                distributed_mode=False,
                distributed_config=DistributedConfig(
                    message_queue_config=MessageQueueConfig(type=MessageQueueType.FAKE),
                ),
                checkpointer_config=CheckpointerConfig(
                    type="persistence",
                    conf={"db_type": "sqlite", "db_path": str(self.settings.checkpoint_db)},
                ),
            )
        )
        await Runner.start()
        self._started = True
        self._storage_diag = self._storage_diagnostic()
        if self._storage_diag.get("healthy", False):
            self.store.clear_recovery_status(
                RECOVERY_VOLUME_MISMATCH,
                cause="storage_verified_after_checkpoint_path_fix",
            )
        recovered_runs = self.store.recover_interrupted()
        if recovered_runs:
            if not self._storage_diag.get("healthy", True):
                # 数据卷自检失败时不允许自动续跑，避免半恢复继续污染状态。
                for recovered in recovered_runs:
                    self._mark_resume_failed(
                        recovered,
                        RECOVERY_VOLUME_MISMATCH,
                        "建模服务持久化自检未通过，已暂停自动恢复，请核对数据卷",
                    )
                return
            interrupted_cancellations = []
            resumable_runs = []
            cancellation_reasons = {
                INTERRUPTED_CANCELLATION_REASON,
                CANCELLATION_PENDING_REASON,
            }
            for recovered in recovered_runs:
                if recovered.error in cancellation_reasons:
                    interrupted_cancellations.append(recovered)
                else:
                    resumable_runs.append(recovered)
            await self._wait_core_ready()
            await asyncio.gather(*(
                self._resume_interrupted_cancellation(recovered)
                for recovered in interrupted_cancellations
            ))
            await asyncio.gather(*(
                self._auto_resume_recovered(recovered) for recovered in resumable_runs
            ))

    async def _resume_interrupted_cancellation(self, run: RunRecord) -> None:
        restore = getattr(self._gateway, "restore_run_credential", None)
        if callable(restore) and run.run_credential:
            restore(run.id, run.run_credential)
        try:
            await self.cancel(project_id=run.project_id)
        except Exception as error:  # noqa: BLE001 - an interrupted stop must remain suspended
            logger.warning("Could not resume cancellation for run %s: %s", run.id, error)
            self.events.publish(run.project_id, run.id, "status", {
                "status": RUN_SUSPENDED,
                "text": "停止结算尚未完成，已保留当前进展，请稍后继续核对",
            })

    async def _wait_core_ready(self) -> None:
        """启动恢复前的就绪门：agent 容器常早于核心就绪，直接开扫会整批撞 503。"""
        health = getattr(self._gateway, "health", None)
        if not callable(health):
            return
        deadline = time.monotonic() + CORE_READY_TIMEOUT_SECONDS
        while not self._shutting_down:
            try:
                if await health():
                    return
            except Exception:  # noqa: BLE001 - 探测失败等同未就绪
                pass
            if time.monotonic() >= deadline:
                logger.warning(
                    "Modeling core not ready within %.0fs; starting recovery with per-run retries",
                    CORE_READY_TIMEOUT_SECONDS,
                )
                return
            await asyncio.sleep(CORE_READY_PROBE_INTERVAL_SECONDS)

    async def _auto_resume_recovered(
        self,
        run: RunRecord,
        *,
        reconcile_attempt: int = 0,
    ) -> None:
        self._publish_state(run.project_id, run.id)
        if not run.run_credential or not run.session_id:
            self.events.publish(
                run.project_id,
                run.id,
                "status",
                {"status": RUN_SUSPENDED, "text": "已保留最近进展，重新打开项目后会继续"},
            )
            return
        if not self._checkpoint_available():
            self._mark_resume_failed(
                run,
                RECOVERY_CHECKPOINT_MISSING,
                "检查点数据库缺失或不可读，已暂停自动恢复，请核对数据卷",
            )
            return
        restore = getattr(self._gateway, "restore_run_credential", None)
        if not callable(restore):
            self.events.publish(
                run.project_id,
                run.id,
                "status",
                {"status": RUN_SUSPENDED, "text": "已保留最近进展，等待安全接续"},
            )
            return
        restore(run.id, run.run_credential)
        error = await self._resume_with_retries(run)
        if error is not None:
            logger.warning("Could not automatically resume run %s: %s", run.id, error)
            detail = (
                f"[{error.code}] {error.message}"
                if isinstance(error, AgentRuntimeError)
                else f"[{type(error).__name__}] {error}"
            )
            self._mark_resume_failed(
                run,
                RECOVERY_RESUME_FAILED,
                "自动恢复失败，已安全停在最近进展",
                detail=detail,
            )
            if _is_transient_core_error(error):
                self._schedule_recovery_reconcile(run, attempt=reconcile_attempt)
            return
        self.store.mark_recovery_status(
            run.project_id,
            RECOVERY_NONE,
            error="",
            cause="recovery_resumed",
        )
        self.events.publish(
            run.project_id,
            run.id,
            "status",
            {"status": RUN_QUEUED, "text": "服务恢复完成，助手正在从原进度继续"},
        )

    async def _resume_with_retries(self, run: RunRecord) -> Exception | None:
        for attempt in range(len(RECOVERY_RETRY_DELAYS) + 1):
            try:
                await self._resume(
                    run,
                    "",
                    {},
                    preserve_token_fingerprint=True,
                )
                return None
            except AgentRuntimeError as error:
                if not _is_transient_core_error(error) or attempt >= len(RECOVERY_RETRY_DELAYS):
                    return error
                await asyncio.sleep(RECOVERY_RETRY_DELAYS[attempt])
            except Exception as error:  # noqa: BLE001 - one failed recovery must not block other projects
                return error
        return None

    def _schedule_recovery_reconcile(self, run: RunRecord, *, attempt: int) -> None:
        if self._shutting_down or attempt >= len(RECOVERY_RECONCILE_DELAYS):
            return
        task = asyncio.create_task(self._recovery_reconcile(run, attempt))
        self._recovery_tasks.add(task)
        task.add_done_callback(self._recovery_tasks.discard)

    async def _recovery_reconcile(self, run: RunRecord, attempt: int) -> None:
        await asyncio.sleep(RECOVERY_RECONCILE_DELAYS[attempt])
        if self._shutting_down:
            return
        # 状态可能已被人工恢复或推进；只有仍停在 resume_failed 且无在跑任务时才重试。
        state = self.store.project_state(run.project_id)
        if state.recovery_status != RECOVERY_RESUME_FAILED or state.run_id != run.id:
            return
        if run.project_id in self._live:
            return
        logger.info(
            "Reconciling failed recovery for run %s (attempt %d)", run.id, attempt + 1
        )
        await self._auto_resume_recovered(run, reconcile_attempt=attempt + 1)

    def _mark_resume_failed(
        self,
        run: RunRecord,
        status: str,
        message: str,
        *,
        detail: str = "",
    ) -> None:
        # detail 落库：运维排查时必须能从状态里看到「为什么」，而不是翻进程日志。
        stored_error = f"{message}（{detail[:200]}）" if detail else message
        self.store.mark_recovery_status(
            run.project_id,
            status,
            error=stored_error,
            cause="recovery_failed",
        )
        self._publish_state(run.project_id, run.id)
        payload: dict[str, Any] = {"status": RUN_SUSPENDED, "text": message}
        if detail:
            payload["reason"] = detail[:200]
        self.events.publish(
            run.project_id,
            run.id,
            "status",
            payload,
        )

    def _checkpoint_available(self) -> bool:
        checkpoint_db = self.settings.checkpoint_db
        if not checkpoint_db.exists():
            return False
        try:
            database = sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)
            database.close()
            return True
        except sqlite3.Error:
            return False

    def _storage_diagnostic(self) -> dict[str, Any]:
        runtime_db = self.settings.runtime_db
        checkpoint_db = self.settings.checkpoint_db
        same_directory = runtime_db.parent.resolve() == checkpoint_db.parent.resolve()
        diagnostic: dict[str, Any] = {
            "healthy": True,
            "sameDirectory": same_directory,
            "runtimeDbExists": runtime_db.exists(),
            "checkpointDbExists": checkpoint_db.exists(),
            "volumeChanged": False,
            "runCount": 0,
            "checkpointCount": 0,
            "checkedAt": now_iso(),
        }
        identity_path = self.settings.data_dir / ".modeling_agent_identity"
        volume_id = ""
        try:
            if identity_path.exists():
                identity = _object(identity_path.read_text(encoding="utf-8"))
                volume_id = str(identity.get("volumeId") or "")
            if not volume_id:
                volume_id = f"vol_{uuid.uuid4().hex}"
                identity_path.write_text(
                    _json({"volumeId": volume_id, "createdAt": now_iso()}),
                    encoding="utf-8",
                )
                diagnostic["volumeChanged"] = not runtime_db.exists() or not checkpoint_db.exists()
        except OSError:
            diagnostic["volumeChanged"] = True
        diagnostic["volumeId"] = volume_id
        try:
            if runtime_db.exists():
                database = sqlite3.connect(f"file:{runtime_db}?mode=ro", uri=True)
                try:
                    diagnostic["runCount"] = int(database.execute(
                        "SELECT COUNT(*) FROM agent_runs"
                    ).fetchone()[0])
                finally:
                    database.close()
        except sqlite3.Error:
            diagnostic["healthy"] = False
            diagnostic["reason"] = "runtime.sqlite 无法读取"
        missing_checkpoint_with_runs = not checkpoint_db.exists() and diagnostic["runCount"] > 0
        if not same_directory or not runtime_db.exists() or missing_checkpoint_with_runs:
            diagnostic["healthy"] = False
            diagnostic["reason"] = "runtime.sqlite 与 checkpoints.sqlite.db 不在同一持久卷，或存在运行记录但检查点数据库缺失"
        try:
            if checkpoint_db.exists():
                database = sqlite3.connect(f"file:{checkpoint_db}?mode=ro", uri=True)
                try:
                    total = 0
                    for table in database.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall():
                        try:
                            total += int(database.execute(
                                f"SELECT COUNT(*) FROM {table[0]}"
                            ).fetchone()[0])
                        except sqlite3.Error:
                            continue
                    diagnostic["checkpointCount"] = total
                finally:
                    database.close()
        except sqlite3.Error:
            diagnostic["healthy"] = False
            diagnostic["reason"] = "checkpoints.sqlite.db 无法读取"
        self._recovery_summary = {
            "status": "healthy" if diagnostic["healthy"] else "storage_unhealthy",
            "volumeId": volume_id,
            "runCount": diagnostic["runCount"],
            "checkpointCount": diagnostic["checkpointCount"],
            "checkedAt": diagnostic["checkedAt"],
        }
        return diagnostic

    async def shutdown(self) -> None:
        self._shutting_down = True
        for task in list(self._recovery_tasks):
            task.cancel()
        if self._recovery_tasks:
            await asyncio.gather(*self._recovery_tasks, return_exceptions=True)
        self._recovery_tasks.clear()
        for live in list(self._live.values()):
            if live.task and not live.task.done():
                live.task.cancel()
        tasks = [live.task for live in self._live.values() if live.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_gateway:
            await self._gateway.aclose()
        if self._started:
            from openjiuwen.core.runner.runner import Runner

            await Runner.stop()
            self._started = False
        self.store.close()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "service": "modeling-agent",
            "runtime": "openJiuwen",
            "config": self.settings.public_config(),
            "skills": list_skills(self.settings.skills_dir),
            "storage": self._storage_diag,
            "recovery": self._recovery_summary,
        }

    def project_state(self, project_id: str) -> dict[str, Any]:
        state = self.store.project_state(project_id)
        run = self.store.get_run(state.run_id) if state.run_id else None
        interaction = self.store.pending_interaction(project_id)
        activity_event = (
            self.store.latest_event(project_id, run.id, "progress")
            if run and run.status in {"queued", "running"}
            else None
        )
        status_event = self.store.latest_event(project_id, run.id, "status") if run else None
        focus_event = self.store.latest_event(project_id, run.id, "focus") if run else None
        restored_focus = focus_event.payload.get("target") if focus_event else None
        public_state = state.public()
        if (
            public_state.get("phase") == "evaluation"
            and _claims_terminal_evaluation(str(public_state.get("summary") or ""))
        ):
            latest_evaluation = self.store.latest_presented_evaluation(project_id)
            if latest_evaluation is not None:
                public_state["summary"] = _evaluation_terminal_summary(latest_evaluation)
        recovery = self._recovery_diagnostic(project_id, state)
        if recovery:
            public_state["recovery"] = recovery
        public_state["phaseLabel"] = PHASE_LABELS.get(public_state["phase"], public_state["phase"])
        cancellation = status_event.payload if status_event and status_event.payload.get("status") in {
            RUN_CANCELLED, RUN_SUSPENDED,
        } else None
        return {
            "state": public_state,
            "phases": _phase_projection(),
            "run": self._run_public(run, state) if run else None,
            "interaction": interaction.public() if interaction else None,
            "activity": {
                **activity_event.payload,
                "createdAt": activity_event.created_at,
            } if activity_event else None,
            "statusText": str(status_event.payload.get("text") or "") if status_event else "",
            "cancellation": cancellation,
            "focus": restored_focus if isinstance(restored_focus, dict) else None,
            "lastSeq": self.store.latest_event_seq(project_id),
        }

    def _recovery_diagnostic(
        self,
        project_id: str,
        state: Any,
    ) -> dict[str, Any] | None:
        missing: list[str] = []
        if state.run_id and self.store.get_run(state.run_id) is None:
            missing.append("run")
        _phase, _separator, execution = state.state.partition(":")
        has_recoverable_run = bool(state.run_id) and execution in ACTIVE_RUN_STATUSES
        storage = self._storage_diag or {}
        if has_recoverable_run and "run" not in missing:
            storage_unhealthy = not storage.get("healthy", True)
            checkpoint_missing = not storage.get("checkpointDbExists", False)
            if storage_unhealthy and checkpoint_missing:
                missing.append("checkpoint")
        status = state.recovery_status or ""
        if not status and missing:
            status = RECOVERY_STALE_RUN if "run" in missing else RECOVERY_VOLUME_MISMATCH
        if not status:
            return None
        next_steps: list[str] = []
        if "run" in missing:
            next_steps.append("安全终止失效计划后重新规划")
        if "checkpoint" in missing:
            next_steps.append("核对建模服务数据卷与 checkpoints.sqlite.db 后重启")
        if status == RECOVERY_RESUME_FAILED:
            next_steps.append("从最近检查点手动恢复，或取消当前运行后重新规划")
        diagnostic: dict[str, Any] = {"status": status}
        if missing:
            diagnostic["missing"] = missing
        if next_steps:
            diagnostic["nextSteps"] = next_steps
        return diagnostic

    def export_project_transfer(self, project_id: str) -> dict[str, Any]:
        return export_project_transfer(self.store, self.settings.checkpoint_db, project_id)

    def import_project_transfer(self, project_id: str, payload: dict[str, Any]) -> dict[str, str]:
        return import_project_transfer(self.store, self.settings.checkpoint_db, project_id, payload)

    def delete_project_transfer(self, project_id: str) -> None:
        delete_project_transfer(self.store, self.settings.checkpoint_db, project_id)

    async def start_run(
        self,
        *,
        project_id: str,
        token: str,
        message: str,
        baseline_revision: str = "",
        resume_plan_id: str = "",
        publish_user_message: bool = True,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_reentry_authorized: bool | None = None,
        confirmation_required: bool = False,
        technical_recoveries: dict[str, dict[str, Any]] | None = None,
        validation_repair_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        prompt = message.strip()
        if not prompt:
            raise AgentRuntimeError("INVALID_MESSAGE", "请输入希望助手处理的内容")
        run_id = f"run_{uuid.uuid4().hex}"
        run = self.store.create_run(
            RunRecord(
                id=run_id,
                project_id=project_id,
                session_id=f"project:{project_id}:run:{run_id}",
                status=RUN_QUEUED,
                prompt=prompt,
                token_fingerprint=token_fingerprint(token),
                baseline_revision=baseline_revision,
            )
        )
        for issue_id, recovery in (technical_recoveries or {}).items():
            self.store.save_technical_recovery(run_id, issue_id, recovery)
        if validation_repair_snapshot:
            self.store.save_validation_repair_state(
                run_id, validation_repair_snapshot,
            )
        self._publish_state(project_id, run_id)
        try:
            claimed = await self._claim_workspace(
                project_id=project_id,
                run_id=run_id,
                token=token,
                resume_plan_id=resume_plan_id,
            )
        except Exception as error:
            self.store.update_run(run_id, status=RUN_FAILED, error=f"{type(error).__name__}: {error}")
            self._publish_state(project_id, run_id)
            raise
        if publish_user_message:
            self.events.publish(project_id, run_id, "message", {"role": "user", "blocks": [{"type": "text", "text": prompt}]})
        self.events.publish(project_id, run_id, "status", {"status": RUN_QUEUED, "text": "助手已接手，正在准备"})
        intent_required_tools = (
            set(required_tools)
            if required_tools is not None
            else _required_tools_for_user_intent(prompt)
        )
        if resume_plan_id:
            intent_required_tools.update(APPROVED_PLAN_REQUIRED_TOOLS)
        resumed_plan = claimed.get("resumedPlan")
        if resume_plan_id:
            if not isinstance(resumed_plan, dict) or str(resumed_plan.get("id") or "") != resume_plan_id:
                raise AgentRuntimeError(
                    "PAUSED_CHANGE_PLAN_RESUME_INVALID",
                    "暂停计划没有返回可验证的恢复快照",
                    status=502,
                )
            intent_required_tools.add("get_change_plan")
        # 台账门禁不再由措辞猜测静默启用（0.26.9.2 台账死锁事故）：启用只能
        # 来自专家确认——助手判断需要对账时先发确认卡，批准后再调
        # require_product_ledger；核心侧对启用与规则批次仍保持确定性门禁。
        live = LiveRun(
            run_id=run_id,
            project_id=project_id,
            token=token,
            tool_profile=run.tool_profile,
            required_tools=intent_required_tools,
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=(
                release_reentry_authorized
                if release_reentry_authorized is not None
                else _release_reentry_requested(prompt)
            ),
            confirmation_required=confirmation_required,
            package_contract=dict(claimed.get("packageContract") or {}),
            project_goal=str(claimed.get("projectGoal") or ""),
            autonomous_seed=(
                confirmation_required
                or bool(intent_required_tools)
                or _autonomous_work_requested(prompt)
            ),
            validation_repair=self._validation_repair_state(run_id),
            technical_recoveries=self.store.technical_recoveries(run_id),
        )
        self._live[run_id] = live
        state_payload = self.project_state(project_id)["state"]
        state_snapshot = json.dumps(state_payload, ensure_ascii=False)[:12_000]
        increment_constraint = ""
        paused_plan_constraint = ""
        if isinstance(resumed_plan, dict):
            plan_facts = {
                "id": resumed_plan.get("id"),
                "goal": resumed_plan.get("goal"),
                "status": resumed_plan.get("status"),
                "approval_status": resumed_plan.get("approval_status"),
                "version": resumed_plan.get("version"),
                "remaining_count": resumed_plan.get("remaining_count"),
                "tasks": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "status": item.get("status"),
                        "required": item.get("required"),
                    }
                    for item in resumed_plan.get("tasks", [])
                    if isinstance(item, dict)
                ],
            }
            paused_plan_constraint = (
                "\n# 页面显式恢复的暂停计划（服务端权威事实）\n"
                f"{json.dumps(plan_facts, ensure_ascii=False)}\n"
                "这是页面通过 resumePlanId 恢复的原计划，不是从用户文本猜测出的新意图。"
                "先调用 get_change_plan(plan_id) 核对最新状态；保留 staged/validated 结果，"
                "只继续 pending/failed 的必选任务。计划完成前不得另建替代计划、不得宣布知识建模完成，"
                "也不得进入确定性验证。"
            )
        if state_payload.get("roundMode") == "incremental":
            increment_constraint = (
                "\n# 当前增量轮次约束（服务端已绑定正式基线）\n"
                "留在 increment 阶段完成影响分析和受控修改，不得回到 goal、process 或 knowledge "
                "重跑全量建模。先核对新增或变化的资料，只处理直接受影响的已有资源；"
                "create_change_plan 必须使用 expert_edit，未受影响的正式内容保持不变。"
            )
            if _document_rule_change_requested(prompt):
                increment_constraint += (
                    "\n本轮是发布后指定文档新增规则：先读取用户点名的文档，再核对现有规则及其引用；"
                    "如果平台已有该文档的修订影响记录，再调用资料影响分析，"
                    "如果只是读取已存在文档且没有修订记录，则直接用文档证据界定范围，"
                    "只建立 rules/ 下新增规则的受控修改计划。不得修改或重写 objects/、operations/、"
                    "metrics/、scripts/，不得改动已有规则或正式基线；索引等平台派生文件由核心服务维护。"
                    "资料影响结果会由服务端自动定位到中栏，不要调用 focus 携带完整 data；"
                    "如确需定位，只传 kind 和 target，不要复制带引号的原始清单。"
                )
        runtime_prompt = (
            f"{prompt}\n\n"
            "# 项目唯一 Agent 状态快照（服务端在本轮启动时注入）\n"
            f"{state_snapshot}\n"
            "执行前仍应调用 get_project_state 获取最新版本；状态转换只能调用 transition_project_state。"
            f"{increment_constraint}{paused_plan_constraint}"
        )
        live.task = asyncio.create_task(self._execute(live, runtime_prompt))
        return self._run_public(run, self.store.project_state(project_id))

    async def handle_material_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        status = str(event.get("status") or "").strip()
        if not event_id or status not in {"parsing", "parsed", "failed"}:
            raise AgentRuntimeError("INVALID_MATERIAL_EVENT", "资料事件不完整", status=400)
        inserted = self.store.record_external_event(event_id, project_id, "material_parse", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        if status == "parsing":
            self.events.publish_external_once(event_id, project_id, run_id, "message", {
                "role": "system",
                "externalEventId": event_id,
                "blocks": [self._material_event_block(event)],
            })
            self.store.mark_external_event_continued(event_id, project_id)
            return {"accepted": True, "duplicate": not inserted, "continued": False}

        batch_id = str(event.get("uploadBatchId") or event.get("documentId") or event_id)
        terminal_events = self.store.material_batch_events(project_id, batch_id)
        batch_size = max(
            [max(1, int(item.get("uploadBatchSize") or 1)) for item in terminal_events],
            default=1,
        )
        if len(terminal_events) < batch_size:
            return {
                "accepted": True,
                "duplicate": not inserted,
                "continued": False,
                "waitingFor": batch_size - len(terminal_events),
            }

        aggregate_id = f"material-batch:{batch_id}:terminal"
        aggregate = self._material_batch_event(aggregate_id, batch_id, terminal_events)
        self.store.record_external_event(aggregate_id, project_id, "material_batch", aggregate)
        self.events.publish_external_once(aggregate_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": aggregate_id,
            "blocks": [self._material_batch_block(terminal_events)],
        })
        await self._start_increment_for_material_events(
            project_id=project_id,
            token=token,
            events=terminal_events,
        )
        self._rewind_for_material_events(project_id, terminal_events)
        if aggregate.get("autoContinue") is False:
            self.store.mark_material_batch_continued(project_id, batch_id)
            self.store.mark_external_event_continued(aggregate_id, project_id)
            return {"accepted": True, "duplicate": not inserted, "continued": False}
        if not self.store.external_event_pending(aggregate_id, project_id):
            self.store.mark_material_batch_continued(project_id, batch_id)
            return {"accepted": True, "duplicate": True, "continued": False}

        parsed_events = [item for item in terminal_events if item.get("status") == "parsed"]
        purpose_review_names = {
            str(item.get("filename")) for item in parsed_events
            if item.get("purposeReviewRequired") is True and str(item.get("filename") or "").strip()
        }
        prompt = self._material_batch_prompt(terminal_events)
        if purpose_review_names:
            required_tools = {"set_material_directive"}
            allowed_tools = PURPOSE_REVIEW_ALLOWED_TOOLS
        else:
            required_tools = {"analyze_material_impact"} if parsed_events else set()
            if any(item.get("readingMode") == "full" for item in parsed_events):
                required_tools.add("read_documents_fully")
            allowed_tools = None
        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=prompt,
            required_tools=required_tools,
            required_material_directives=purpose_review_names,
            allowed_tools=allowed_tools,
            event=aggregate,
        )
        self.store.mark_material_batch_continued(project_id, batch_id)
        self.store.mark_external_event_continued(aggregate_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    async def handle_package_import_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        baseline_revision = str(event.get("baselineRevision") or "").strip()
        package_name = str(event.get("packageName") or "").strip()
        package_version = str(event.get("packageVersion") or "").strip()
        if not event_id or not baseline_revision or not package_name or not package_version:
            raise AgentRuntimeError("INVALID_PACKAGE_IMPORT_EVENT", "导入基线事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "package_import", event)
        state = self.store.project_state(project_id)
        if state.revision != 0 or state.state != "intake:idle":
            self.store.mark_external_event_continued(event_id, project_id)
            return {
                "accepted": True,
                "duplicate": not inserted,
                "initialized": False,
                "preserved": True,
                "state": state.public(),
            }

        self.events.publish_external_once(event_id, project_id, "", "message", {
            "role": "assistant",
            "externalEventId": event_id,
            "blocks": [{
                "type": "text",
                "text": (
                    f"已把“{package_name}” {package_version} 作为当前增量基线。"
                    "你现在可以直接补充或替换资料；解析完成后，我会自动展示保持有效、"
                    "需要处理的内容和证据链，再形成增量候选。确认前，当前基线保持不变。"
                ),
            }],
        })
        round_id = f"increment_{uuid.uuid4().hex}"
        state = self.store.transition_project_state(
            project_id,
            expected_revision=state.revision,
            next_state="increment:idle",
            cause="package_imported",
            summary=(
                f"已导入知识包“{package_name}” {package_version} 作为增量基线；"
                "尚未运行本项目的真实业务评测。"
            ),
            round_mode="incremental",
            round_id=round_id,
            baseline_revision=baseline_revision,
            metadata={
                "eventId": event_id,
                "roundId": round_id,
                "baselineRevision": baseline_revision,
                "packageName": package_name,
                "packageVersion": package_version,
                "sourceFilename": str(event.get("sourceFilename") or ""),
                "fileCount": int(event.get("fileCount") or 0),
            },
        )
        public = state.public()
        public["phaseLabel"] = PHASE_LABELS["increment"]
        self.events.publish(project_id, "", "state", public)
        self.store.mark_external_event_continued(event_id, project_id)
        return {
            "accepted": True,
            "duplicate": not inserted,
            "initialized": True,
            "preserved": False,
            "state": state.public(),
        }

    async def start_increment_round(
        self,
        *,
        project_id: str,
        token: str,
        baseline_revision: str,
        package_version: str = "",
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        state, started = self.store.start_increment_round(
            project_id,
            baseline_revision=baseline_revision,
            package_version=package_version,
        )
        public = state.public()
        public["phaseLabel"] = PHASE_LABELS["increment"]
        if started:
            self.events.publish(project_id, state.run_id, "state", public)
            version = package_version.strip()
            self.events.publish(project_id, state.run_id, "message", {
                "role": "assistant",
                "blocks": [{
                    "type": "text",
                    "text": (
                        f"已基于知识包 {version} 开始增量建模。"
                        if version else "已基于当前正式知识包开始增量建模。"
                    ) + "本轮只核对新增或变化的资料，未受影响的已确认内容保持不变。",
                }],
            })
        return {"started": started, "state": public}

    async def handle_evaluation_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        round_id = str(event.get("roundId") or "").strip()
        status = str(event.get("status") or "").strip()
        if not event_id or not round_id or status not in {"done", "failed", "cancelled"}:
            raise AgentRuntimeError("INVALID_EVALUATION_EVENT", "评测终态事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "evaluation_terminal", event)
        claim = self.store.claim_external_event_continuation(event_id, project_id)
        if claim == "continued":
            return {"accepted": True, "duplicate": True, "reconciled": False}
        if claim == "continuing":
            raise AgentRuntimeError(
                "EVALUATION_EVENT_IN_PROGRESS",
                "评测终态正在处理，请稍后重试",
                status=409,
            )
        if claim != "claimed":
            raise AgentRuntimeError(
                "EVALUATION_EVENT_CONFLICT",
                "评测终态事件与项目不匹配",
                status=409,
            )

        try:
            result = await self._handle_claimed_evaluation_event(
                project_id=project_id,
                token=token,
                event=event,
                event_id=event_id,
                round_id=round_id,
                status=status,
                inserted=inserted,
            )
            self.store.mark_external_event_continued(event_id, project_id)
            return result
        except BaseException:
            self.store.release_external_event_continuation(event_id, project_id)
            raise

    async def _handle_claimed_evaluation_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
        event_id: str,
        round_id: str,
        status: str,
        inserted: bool,
    ) -> dict[str, Any]:

        latest = self.store.latest_presented_evaluation(
            project_id,
            exclude_event_id=event_id,
        )
        if latest is not None and _evaluation_event_is_stale(event, latest):
            return {
                "accepted": True,
                "duplicate": not inserted,
                "reconciled": False,
                "stale": True,
                "status": status,
                "roundId": round_id,
            }

        for live in self._live.values():
            if live.project_id == project_id:
                live.stable_read_cache.clear()

        summary = _evaluation_terminal_summary(event)
        active = self.store.active_run(project_id)
        run_id = active.id if active else self.store.project_state(project_id).run_id
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": summary}],
        })

        current = self.store.project_state(project_id)
        phase, _separator, _execution = current.state.partition(":")
        reconciled = False
        needs_reconciliation = (
            current.summary != summary
            or any(_claims_active_evaluation(item) for item in current.open_questions)
        )
        if phase == "evaluation" and needs_reconciliation:
            updated = self.store.transition_project_state(
                project_id,
                expected_revision=current.revision,
                next_state=current.state,
                cause="evaluation_terminal",
                actor_run_id=run_id,
                summary=summary,
                open_questions=[
                    item for item in current.open_questions
                    if not _claims_active_evaluation(item)
                ],
                metadata={
                    "eventId": event_id,
                    "roundId": round_id,
                    "status": status,
                    "done": max(0, int(event.get("done") or 0)),
                    "total": max(0, int(event.get("total") or 0)),
                    "passed": max(0, int(event.get("passed") or 0)),
                    "finishedAt": str(event.get("finishedAt") or ""),
                },
            )
            public = updated.public()
            public["phaseLabel"] = PHASE_LABELS.get(phase, phase)
            self.events.publish(project_id, run_id, "state", public)
            reconciled = True

        continuation = _evaluation_terminal_continuation(event)
        if continuation is None:
            return {
                "accepted": True,
                "duplicate": not inserted,
                "reconciled": reconciled,
                "continued": False,
                "status": status,
                "roundId": round_id,
            }

        await self._wait_for_evaluation_launch_boundary(project_id)
        prompt, required_tools = continuation
        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=prompt,
            required_tools=required_tools,
            event=event,
        )
        return {
            "accepted": True,
            "duplicate": not inserted,
            "reconciled": reconciled,
            "continued": True,
            "status": status,
            "roundId": round_id,
        }

    async def _wait_for_evaluation_launch_boundary(self, project_id: str) -> None:
        active = self.store.active_run(project_id)
        live = self._live.get(active.id) if active is not None else None
        if live is None or not live.evaluation_launch_active:
            return
        task = live.task
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            raise AgentRuntimeError(
                "EVALUATION_LAUNCH_SETTLING",
                "评测终态已保存，启动回合仍在结束",
                status=409,
            )
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=EVALUATION_LAUNCH_SETTLE_TIMEOUT_SECONDS,
            )
        except TimeoutError as error:
            raise AgentRuntimeError(
                "EVALUATION_LAUNCH_SETTLING",
                "评测终态已保存，启动回合仍在结束",
                status=409,
            ) from error

    async def handle_studio_review_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        client_mutation_id = str(event.get("clientMutationId") or "").strip()
        request_hash = str(event.get("requestHash") or "").strip()
        if not event_id or not client_mutation_id or len(request_hash) != 64:
            raise AgentRuntimeError("INVALID_STUDIO_EVENT", "知识复核事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "studio_review", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": self._studio_event_message(event)}],
        })
        if not self.store.external_event_pending(event_id, project_id):
            return {"accepted": True, "duplicate": True, "continued": False}
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None:
            raise AgentRuntimeError(
                "STUDIO_EVENT_WAITING_CONFIRMATION",
                "当前还有一项决定等待处理，新的知识修改会在这项决定处理后继续核对",
                status=409,
            )

        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=self._studio_event_prompt(event),
            required_tools={"diff"},
            event=event,
            confirmation_required=True,
        )
        self.store.mark_external_event_continued(event_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    async def handle_fusion_review_event(
        self,
        *,
        project_id: str,
        token: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        event_id = str(event.get("eventId") or "").strip()
        plan_id = str(event.get("planId") or "").strip()
        candidate_id = str(event.get("candidateId") or "").strip()
        if not event_id or not plan_id or not candidate_id:
            raise AgentRuntimeError("INVALID_FUSION_EVENT", "融合复核事件不完整", status=400)

        inserted = self.store.record_external_event(event_id, project_id, "fusion_review", event)
        run = self.store.active_run(project_id)
        run_id = run.id if run else ""
        self.events.publish_external_once(event_id, project_id, run_id, "message", {
            "role": "system",
            "externalEventId": event_id,
            "blocks": [{"type": "text", "text": self._fusion_event_message(event)}],
        })
        if not self.store.external_event_pending(event_id, project_id):
            return {"accepted": True, "duplicate": True, "continued": False}
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None:
            raise AgentRuntimeError(
                "FUSION_EVENT_WAITING_CONFIRMATION",
                "当前还有一项决定等待处理，融合候选会在这项决定处理后继续核对",
                status=409,
            )

        await self._continue_for_external_event(
            project_id=project_id,
            token=token,
            prompt=self._fusion_event_prompt(event),
            required_tools={"compile_package_fusion_plan", "diff"},
            event=event,
            confirmation_required=True,
        )
        self.store.mark_external_event_continued(event_id, project_id)
        return {"accepted": True, "duplicate": not inserted, "continued": True}

    @staticmethod
    def _studio_event_message(event: dict[str, Any]) -> str:
        count = len(event.get("elementIds") or [])
        scope = f"，涉及 {count} 个知识条目" if count else ""
        return (
            f"知识包 Studio 的修改已交给助手复核{scope}。"
            "助手会先核对整批变更，再请你整体确认；确认前不会改变当前版本。"
        )

    @staticmethod
    def _fusion_event_message(event: dict[str, Any]) -> str:
        source_count = max(2, int(event.get("sourceCount") or 2))
        output_name = str(event.get("outputName") or "融合知识包")
        return (
            f"“{output_name}”的融合候选已交给助手整体复核，包含 {source_count} 个来源知识包。"
            "助手会核对完整差异和质量结果，再请你一次确认；确认前不会改变当前版本。"
        )

    @staticmethod
    def _studio_event_prompt(event: dict[str, Any]) -> str:
        summary = str(event.get("summary") or "知识包 Studio 已形成待确认修改")
        element_ids = [str(value) for value in event.get("elementIds") or []]
        return (
            "服务端已持久化一项知识包 Studio 整体复核请求。"
            "这不是用户对话消息，不得伪造用户发言，也不要重复编辑这些内容。"
            "先读取项目唯一状态，再调用 diff 并令 rev_b='candidate' 核对整批真实差异；"
            "把整体差异定位到中栏，然后立即调用 request_confirmation 展示一个整体确认卡。"
            "只能展示候选，绝不能替用户批准或直接提交。不要向用户复述工具名、参数或内部标识。\n"
            f"业务摘要：{summary}\n"
            f"涉及的知识条目：{json.dumps(element_ids, ensure_ascii=False)}"
        )

    @staticmethod
    def _fusion_event_prompt(event: dict[str, Any]) -> str:
        summary = str(event.get("summary") or "多来源融合已形成整体候选")
        plan_id = str(event.get("planId") or "")
        candidate_id = str(event.get("candidateId") or "")
        return (
            "服务端已持久化一项多来源融合整体复核请求。"
            "这不是用户对话消息，不得伪造用户发言，也不要重新建立或改写融合方案。"
            "先读取项目唯一状态并接续候选上下文，调用 compile_package_fusion_plan 挂接已生成的候选；"
            "再调用 diff 并令 rev_b='candidate' 核对整批真实差异，随后定位整体差异并立即调用 "
            "request_confirmation 展示一个整体确认卡。只能展示候选，绝不能替用户批准或直接提交。"
            "不要向用户复述工具名、参数、计划编号、候选编号或其他内部标识。\n"
            f"业务摘要：{summary}\n"
            f"融合计划：{plan_id}\n"
            f"候选校验值：{candidate_id}"
        )

    def _rewind_for_material_events(self, project_id: str, events: list[dict[str, Any]]) -> None:
        changed = [item for item in events if item.get("status") == "parsed" and item.get("semanticChanged") is True]
        if not changed:
            return
        target = "goal" if any(item.get("purpose") == "goal" for item in changed) else "knowledge"
        current = self.store.project_state(project_id)
        phase, _separator, execution = current.state.partition(":")
        # 带正式基线的新事件已在上一步进入 increment；这里只阻止增量轮次退回全量阶段。
        # 部署前已经入队、缺少基线字段的存量事件仍沿用原来的定向回退，确保能够被消费。
        if phase == "increment":
            return
        if phase not in PHASE_ORDER or PHASE_ORDER.index(phase) <= PHASE_ORDER.index(target):
            return
        updated = self.store.transition_project_state(
            project_id,
            expected_revision=current.revision,
            next_state=f"{target}:{execution}",
            cause="material_parse_completed",
            actor_run_id=current.run_id,
            metadata={
                "eventIds": [item.get("eventId") for item in changed],
                "documentIds": [item.get("documentId") for item in changed],
                "purposes": [item.get("purpose") for item in changed],
                "semanticChanged": True,
            },
        )
        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS[target]
        self.events.publish(project_id, current.run_id, "state", public)

    async def _start_increment_for_material_events(
        self,
        *,
        project_id: str,
        token: str,
        events: list[dict[str, Any]],
    ) -> None:
        baseline_event = None
        for item in events:
            is_parsed_change = (
                item.get("status") == "parsed"
                and item.get("semanticChanged") is True
            )
            if not is_parsed_change:
                continue
            is_released_package = str(item.get("packageStatus") or "") == "released"
            has_package_revision = bool(str(item.get("packageRevision") or "").strip())
            if is_released_package and has_package_revision:
                baseline_event = item
                break
        if baseline_event is None:
            return
        await self.start_increment_round(
            project_id=project_id,
            token=token,
            baseline_revision=str(baseline_event.get("packageRevision") or ""),
            package_version=str(baseline_event.get("packageVersion") or ""),
        )

    @staticmethod
    def _material_event_block(event: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "upload",
            "files": [{
                "id": event.get("documentId"),
                "documentId": event.get("documentId"),
                "name": event.get("filename"),
                "status": "parsing",
                "purpose": event.get("purpose") or "auto",
                "readingMode": event.get("readingMode") or "on_demand",
            }],
        }

    @staticmethod
    def _material_batch_block(events: list[dict[str, Any]]) -> dict[str, Any]:
        details = []
        for event in events:
            detail = {
                "documentId": event.get("documentId"),
                "name": event.get("filename"),
                "status": event.get("status"),
            }
            if event.get("parseError"):
                detail["error"] = event["parseError"]
            details.append(detail)
        parsed = len([item for item in events if item.get("status") == "parsed"])
        return {"type": "uploadResult", "parsed": parsed, "failed": len(events) - parsed, "details": details}

    @staticmethod
    def _material_batch_event(
        event_id: str,
        batch_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        parsed = [item for item in events if item.get("status") == "parsed"]
        return {
            "eventId": event_id,
            "status": "parsed" if parsed else "failed",
            "uploadBatchId": batch_id,
            "autoContinue": any(item.get("autoContinue") is not False for item in events),
            "filename": "、".join(str(item.get("filename") or "新资料") for item in events),
            "filenames": [item.get("filename") for item in parsed],
            "parsed": len(parsed),
            "failed": len(events) - len(parsed),
            "events": events,
        }

    @staticmethod
    def _material_batch_prompt(events: list[dict[str, Any]]) -> str:
        parsed = [item for item in events if item.get("status") == "parsed"]
        failed = [item for item in events if item.get("status") == "failed"]
        impact_calls = "；".join(
            f"《{item.get('filename')}》使用 source='{item.get('sourceKey') or item.get('documentId')}'"
            for item in parsed
        )
        full_read_names = [str(item.get("filename")) for item in parsed if item.get("readingMode") == "full"]
        purpose_review_names = [
            str(item.get("filename")) for item in parsed if item.get("purposeReviewRequired") is True
        ]
        if purpose_review_names:
            return (
                f"服务端资料批次已经全部结束：解析成功 {len(parsed)}/{len(events)}，失败 {len(failed)} 份。"
                f"以下资料尚未确认用途：{purpose_review_names!r}。这是独立的用途预判轮。"
                "逐份轻量预判用途：先使用资料清单中的文件名、类型和目录；普通文档只有判断依据不足时才按需读取开头片段，"
                "表格只读取全部 Sheet 结构而不读取全量数据行。逐份提交明确的文件级用途，不能保留为“由助手决定”；"
                "表格同时提交全部 Sheet 建议。完成所有建议后定位第一份待确认资料，立即结束本轮并等待用户逐份确认。"
                "本轮禁止执行影响分析、全文精读、候选写入、目标确认或阶段推进；全文精读设置留到全部用途确认后执行。"
                + ("说明失败资料的补救方式，且不要假装已经读到正文。" if failed else "")
                + "内部返回字段、参数名和调用步骤只供运行时使用，绝不能向用户复述。"
            )
        return (
            f"服务端资料批次已经全部结束：解析成功 {len(parsed)}/{len(events)}，失败 {len(failed)} 份。"
            f"逐份调用 analyze_material_impact 核对成功资料：{impact_calls or '没有可分析的成功资料'}。"
            "如果首次分析表明影响范围尚未界定，只能先展示正在界定范围；读完资料和相关知识后，"
            "再按工具契约逐份提交直接影响起点。两份以上资料都界定完成后，必须按同一批资料再取得一次"
            "核心去重的合并影响视图并原样显示在中栏，不得自行合并或估算数量。"
            "语义未变化就说明无需候选，有影响则从唯一项目状态主动继续。"
            + (f"必须调用 read_documents_fully 一次并传入 filenames={full_read_names!r} 完成全文覆盖。" if full_read_names else "")
            + ("说明失败资料的补救方式，且不要假装已经读到正文。" if failed else "")
            + "内部返回字段、参数名和调用步骤只供运行时使用，绝不能向用户复述。"
        )

    async def _continue_for_external_event(
        self,
        *,
        project_id: str,
        token: str,
        prompt: str,
        required_tools: set[str],
        event: dict[str, Any],
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        confirmation_required: bool = False,
    ) -> None:
        run = self.store.active_run(project_id)
        if run is None:
            await self.start_run(
                project_id=project_id,
                token=token,
                message=prompt,
                publish_user_message=False,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        execution = self._execution(project_id, run.id)
        interaction = self.store.pending_interaction(project_id)
        if interaction and interaction.run_id == run.id:
            if interaction.kind == "material_request" and event.get("status") == "parsed":
                answer = {
                    "action": "uploaded",
                    "files": event.get("filenames") or [],
                    "parsed": event.get("parsed") or 0,
                    "failed": event.get("failed") or 0,
                }
                self.store.answer_interaction(interaction.id, answer)
                self.events.publish(project_id, run.id, "interaction", {
                    "id": interaction.id, "status": "answered", "answer": answer,
                })
                self._persist_latest_interaction(
                    run,
                    interaction=interaction,
                    answer=answer,
                    continuation=prompt,
                    action_key="uploaded",
                    requires_artifact=_answer_requires_script_artifact(interaction, answer),
                )
                await self._resume(
                    run, token, {interaction.tool_call_id: answer},
                    required_tools=required_tools,
                    required_material_directives=required_material_directives,
                    allowed_tools=allowed_tools,
                    release_reentry_authorized=True,
                    confirmation_required=confirmation_required,
                )
                return
            answer = {"action": "interject", "text": prompt}
            self.store.answer_interaction(interaction.id, answer)
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key="interject",
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            await self._resume(
                run, token, {interaction.tool_call_id: answer},
                restore_interaction=interaction,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_SUSPENDED:
            await self._continue(
                run, token, prompt,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        if execution == RUN_WAITING:
            await self._continue(
                run, token, prompt,
                required_tools=required_tools,
                required_material_directives=required_material_directives,
                allowed_tools=allowed_tools,
                release_reentry_authorized=True,
                confirmation_required=confirmation_required,
            )
            return
        live = self._live.get(run.id)
        if live is None or execution not in {RUN_QUEUED, RUN_RUNNING}:
            raise AgentRuntimeError("RUN_NOT_STEERABLE", "当前运行暂时不能接收资料事件", status=409)
        steering_id = self.store.queue_steering(project_id, run.id, prompt)
        live.token = token
        live.stable_read_cache.clear()
        live.required_tools.update(required_tools)
        live.required_material_directives.update(required_material_directives or ())
        if allowed_tools is not None:
            live.allowed_tools = allowed_tools
        live.release_reentry_authorized = True
        live.confirmation_required = live.confirmation_required or confirmation_required
        await live.steering.put(prompt)
        self.store.mark_steering_delivered(steering_id)

    async def answer_interaction(
        self,
        *,
        project_id: str,
        interaction_id: str,
        token: str,
        answer: dict[str, Any],
    ) -> dict[str, Any]:
        if not token:
            raise AgentRuntimeError("TOKEN_REQUIRED", "需要登录后才能回答", status=401)
        interaction = self.store.get_interaction(interaction_id)
        if interaction is None:
            raise AgentRuntimeError("INTERACTION_NOT_FOUND", "待答问题不存在", status=404)
        if interaction.project_id != project_id:
            raise AgentRuntimeError("INTERACTION_NOT_FOUND", "待答问题不存在", status=404)
        required_text_option = _required_text_option(interaction.payload, answer)
        if required_text_option is not None and not str(answer.get("text") or "").strip():
            raise AgentRuntimeError(
                "INTERACTION_TEXT_REQUIRED",
                str(required_text_option.get("textPrompt") or "请补充具体说明后再提交"),
                status=400,
            )
        required_text_constraint = _required_text_constraint(interaction, answer)
        # Technical recovery cards are rendered as ordinary decision cards by
        # the workbench, but their answer must enter the issue-recovery path.
        # Otherwise the answer is treated as a new business prompt and the
        # persisted required tools are silently dropped on resume.
        recovery_payload = interaction.payload.get("recovery")
        recovery_action = ""
        if isinstance(recovery_payload, dict):
            recovery_action = str(
                answer.get("action") or answer.get("value") or ""
            ).strip()
            allowed_actions = {
                str(item).strip()
                for item in recovery_payload.get("allowedActions") or []
                if str(item).strip()
            }
            allowed_actions.update({
                str(item.get("id") or "").strip()
                for item in recovery_payload.get("recoveryActions") or []
                if isinstance(item, dict)
            })
            if recovery_payload.get("actionId"):
                allowed_actions.add(str(recovery_payload.get("actionId")).strip())
            if not recovery_action or recovery_action not in allowed_actions:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_INVALID",
                    "请选择当前恢复卡提供的有效动作",
                    status=400,
                )
        if (
            interaction.payload.get("requiredTextConstraint")
            and str(answer.get("value") or "").strip() == "edit_required_text"
            and not str(answer.get("text") or "").strip()
        ):
            raise AgentRuntimeError(
                "INTERACTION_TEXT_REQUIRED",
                "请补充新的业务约束后再提交",
                status=400,
            )
        direct_confirmation = (
            interaction.kind == "confirmation"
            and answer.get("action") != "interject"
        )
        direct_release = (
            interaction.kind == "release_confirmation"
            and answer.get("action") != "interject"
            and answer.get("approved") is True
            and answer.get("coverage_confirmed") is True
        )
        restored_confirmation = (
            direct_confirmation
            and interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX)
        )
        confirmation_result: dict[str, Any] | None = None
        release_result: dict[str, Any] | None = None
        if direct_confirmation:
            approved = answer.get("approved") is True
            validation_receipt = interaction.payload.get("validationReceipt")
            if not isinstance(validation_receipt, dict):
                validation_receipt = {}
            interaction_live = self._live.get(interaction.run_id)
            confirmation_result = await self._core_call_for_run(
                project_id,
                interaction.run_id,
                token,
                "request_confirmation" if approved else "request_confirmation_rejected",
                {
                    "summary": str(interaction.payload.get("summary") or interaction.payload.get("question") or "确认待修改内容"),
                    "change_groups": interaction.payload.get("changeGroups") or [],
                    "expected_candidate_revision": str(interaction.payload.get("candidateRevision") or ""),
                    "expected_candidate_hash": str(validation_receipt.get("candidateHash") or ""),
                    "expected_validator_version": str(validation_receipt.get("validatorVersion") or ""),
                    "review": dict(answer),
                },
                live=interaction_live,
            )
            if confirmation_result.get("error"):
                reason = str(confirmation_result.get("reason") or "待修改内容没有成功结算")
                error_code = str(confirmation_result.get("error") or "CONFIRMATION_FAILED")
                if error_code in {"CANDIDATE_REVALIDATION_REQUIRED", "STALE_CONFIRMATION"}:
                    decision = "接受" if approved else "退回"
                    superseded_answer = {
                        "approved": False,
                        "action": "superseded",
                        "reason": reason,
                    }
                    self.store.answer_interaction(interaction.id, superseded_answer)
                    run = self.store.require_run(interaction.run_id)
                    self.events.publish(run.project_id, run.id, "interaction", {
                        "id": interaction.id,
                        "status": "answered",
                        "answer": superseded_answer,
                    })
                    self.events.publish(run.project_id, run.id, "message", {
                        "role": "system",
                        "blocks": [{
                            "type": "text",
                            "text": f"刚才的待确认内容已变化，{decision}操作未生效；助手正在重新校验，新的内容需要再次确认。",
                        }],
                    })
                    self._persist_latest_interaction(
                        run,
                        interaction=interaction,
                        answer=answer,
                        continuation="确认卡已经失效，用户没有应用任何候选；继续执行本次预期。",
                        action_key=str(answer.get("action") or "superseded"),
                        requires_artifact=_answer_requires_script_artifact(interaction, answer),
                    )
                    return await self._resume(
                        run,
                        token,
                        {interaction.tool_call_id: {
                            "action": "settled_confirmation",
                            "result": confirmation_result,
                            "continuation": (
                                f"旧确认卡已经作废，用户的{decision}没有应用到任何候选。"
                                "立即重新执行完整预检；若有 findings，自动修正后重新预检。"
                                "只有通过后才能展示新的确认卡，绝不能沿用刚才的决定。"
                            ),
                        }},
                        autonomous_seed=True,
                        required_tools=APPROVED_PLAN_REQUIRED_TOOLS,
                    )
                failed_interaction = self.store.set_interaction_submission_error(interaction.id, reason)
                if failed_interaction is not None:
                    self.events.publish(
                        interaction.project_id,
                        interaction.run_id,
                        "interaction",
                        failed_interaction.public(),
                    )
                raise AgentRuntimeError(
                    error_code,
                    reason,
                    status=409,
                    details={"next": confirmation_result.get("next") or []},
                )
        if direct_release:
            interaction_live = self._live.get(interaction.run_id)
            release_result = await self._core_call_for_run(
                project_id,
                interaction.run_id,
                token,
                "request_release",
                {
                    "summary": str(
                        interaction.payload.get("summary")
                        or interaction.payload.get("question")
                        or "确认正式发布"
                    ),
                    "version": str(
                        answer.get("version")
                        or interaction.payload.get("version")
                        or ""
                    ),
                    "review": dict(answer),
                },
                live=interaction_live,
            )
            if release_result.get("error"):
                reason = str(release_result.get("reason") or "正式发布没有成功完成")
                error_code = str(release_result.get("error") or "RELEASE_FAILED")
                failed_interaction = self.store.set_interaction_submission_error(interaction.id, reason)
                if failed_interaction is not None:
                    self.events.publish(
                        interaction.project_id,
                        interaction.run_id,
                        "interaction",
                        failed_interaction.public(),
                    )
                raise AgentRuntimeError(
                    error_code,
                    reason,
                    status=409,
                    details={"next": release_result.get("next") or []},
                )
        self.store.answer_interaction(interaction_id, answer)
        run = self.store.require_run(interaction.run_id)
        self.events.publish(run.project_id, run.id, "interaction", {"id": interaction.id, "status": "answered", "answer": answer})
        self.events.publish(run.project_id, run.id, "message", {"role": "user", "blocks": [{"type": "answer", "interactionId": interaction.id, "answer": answer}]})
        if isinstance(recovery_payload, dict):
            # The interaction is marked answered before delegating so
            # resume_issue does not reject the request as "still waiting for
            # an interaction". The recovery record itself remains active until
            # the selected action settles it.
            issue_id = str(
                recovery_payload.get("issueId")
                or interaction.payload.get("issueId")
                or ""
            ).strip()
            if not issue_id:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_INVALID",
                    "恢复卡缺少问题标识，请刷新当前运行状态后重试",
                    status=409,
                )
            return await self.resume_issue(
                project_id=project_id,
                token=token,
                issue_id=issue_id,
                action_id=recovery_action,
            )
        # 卡片问题文案出自助手，不能参与任何意图判定：建模确认卡里的
        # 「终产品……N 项」曾被误判成产品台账诉求，形成不可满足的必需
        # 工具循环。这里只判定专家自己给出的答复内容。
        answer_text = _expert_answer_intent_text(answer)
        live_for_answer = self._live.get(interaction.run_id)
        required_tools = _required_tools_for_user_intent(
            answer_text,
            full_read_text=answer_text,
            situation=live_for_answer.situation if live_for_answer else None,
        )
        constraint_prompt = (
            _required_text_prompt(required_text_constraint)
            if required_text_constraint else ""
        )
        if required_text_constraint:
            self.events.publish(run.project_id, run.id, "tool", {
                "name": "required_text_constraint",
                "phase": "completed",
                "summary": "补充说明已绑定到恢复任务",
                "constraintHash": required_text_constraint.get("textHash"),
            })
        if direct_confirmation:
            approved = answer.get("approved") is True
            if not approved:
                required_tools = required_tools | APPROVED_PLAN_REQUIRED_TOOLS
            prompt = (
                "用户已经正式接受了候选修改，服务端已将该卡片绑定的候选写入当前修订。"
                if approved else
                "用户已经正式退回了候选修改，原候选已关闭。"
            )
            prompt += (
                f"\n用户反馈：{str(answer.get('feedback') or '').strip()}\n"
                f"确认结果：{json.dumps(confirmation_result or {}, ensure_ascii=False)}\n"
                "从当前项目继续主动完成下一步；不要再次询问已经结算的同一项确认。"
            )
            if restored_confirmation:
                self._persist_latest_interaction(
                    run,
                    interaction=interaction,
                    answer=answer,
                    continuation=prompt,
                    action_key=str(answer.get("action") or "confirm"),
                    requires_artifact=_answer_requires_script_artifact(interaction, answer),
                )
                return await self._continue(
                    run,
                    token,
                    prompt,
                    required_tools=required_tools,
                    autonomous_seed=True,
                )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "confirm"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._resume(
                run,
                token,
                {interaction.tool_call_id: {
                    "action": "settled_confirmation",
                    "result": confirmation_result or {},
                    "continuation": prompt,
                }},
                required_tools=required_tools,
                autonomous_seed=True,
            )
        if direct_release and release_result is not None:
            version = str(release_result.get("version") or answer.get("version") or "")
            prompt = (
                f"用户已确认真实用例覆盖，服务端已完成稳定版本 {version} 的正式发布。\n"
                f"发布结果：{json.dumps(release_result, ensure_ascii=False)}\n"
                "只依据该发布结果说明已完成的版本和交付状态；不要再次发起发布确认。"
            )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "release"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._resume(
                run,
                token,
                {interaction.tool_call_id: {
                    "action": "settled_release",
                    "result": release_result,
                    "continuation": prompt,
                }},
                required_tools=required_tools,
                autonomous_seed=True,
            )
        if interaction.tool_call_id.startswith(INTERJECTION_CONTINUATION_PREFIX):
            prompt = (
                "这是用户对先前待确认问题的正式答复。\n"
                f"原问题：{interaction.payload.get('question') or '待确认问题'}\n"
                f"用户答复：{json.dumps(answer, ensure_ascii=False)}\n"
                "请把答复作为明确约束，从当前项目和同一会话继续主动建模。"
                "不要重复询问已经回答的问题；下一次确实需要用户决定时再调用 ask 或 request_confirmation。"
            )
            if constraint_prompt:
                prompt = f"{prompt}\n\n{constraint_prompt}"
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation=prompt,
                action_key=str(answer.get("action") or "interject"),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            return await self._continue(
                run,
                token,
                prompt,
                required_tools=required_tools,
                autonomous_seed=True,
                required_text_constraint=required_text_constraint,
            )
        self._persist_latest_interaction(
            run,
            interaction=interaction,
            answer=answer,
            continuation=constraint_prompt,
            action_key=str(answer.get("action") or ""),
            requires_artifact=_answer_requires_script_artifact(interaction, answer),
        )
        return await self._resume(
            run,
            token,
            {interaction.tool_call_id: answer},
            steering=constraint_prompt,
            required_tools=required_tools,
            confirmation_required=bool(interaction.payload.get("overallConfirmationRequired")),
            release_declined=(
                interaction.kind == "release_confirmation"
                and answer.get("action") != "interject"
                and (answer.get("approved") is not True or answer.get("coverage_confirmed") is not True)
            ),
            autonomous_seed=True,
            required_text_constraint=required_text_constraint,
        )

    async def steer(
        self,
        *,
        project_id: str,
        token: str,
        text: str,
        purpose_review_completed: bool = False,
        plan_decision: str = "",
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        message = text.strip()
        if not message:
            raise AgentRuntimeError("INVALID_MESSAGE", "请输入要补充或追问的内容")
        # 页面批准建模计划的那次点击本身就是结构化信号：必需工具从这里
        # 进料，不依赖续跑文案的措辞（意图正则双轨保留，待统一拔除）。
        plan_required_tools = (
            set(APPROVED_PLAN_REQUIRED_TOOLS)
            if str(plan_decision).strip().lower() == "approved"
            else set()
        )
        run = self.store.active_run(project_id)
        if run is None:
            return {"mode": "new_run", "run": await self.start_run(project_id=project_id, token=token, message=message)}
        execution = self._execution(project_id, run.id)

        interaction = self.store.pending_interaction(project_id)
        if interaction and interaction.run_id == run.id:
            # 批量确认已经把这批资料的用途落库，挂起的逐份卡问的正是同一件事：
            # 它是被解决了，不是被插话打断，所以不能再作为「未解决的原问题」恢复。
            # 但仍要给出真实答案让助手那次在途的 ask 调用返回，否则运行会挂住（见 #36）。
            resolved_elsewhere = purpose_review_completed
            answer = {
                "action": "resolved_elsewhere" if resolved_elsewhere else "interject",
                "text": message,
            }
            self.store.answer_interaction(interaction.id, answer)
            self.events.publish(
                project_id,
                run.id,
                "message",
                {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "interaction_boundary"},
            )
            self._persist_latest_interaction(
                run,
                interaction=interaction,
                answer=answer,
                continuation="",
                action_key=str(answer["action"]),
                requires_artifact=_answer_requires_script_artifact(interaction, answer),
            )
            resumed = await self._resume(
                run,
                token,
                {interaction.tool_call_id: answer},
                restore_interaction=None if resolved_elsewhere else interaction,
                required_tools=_required_tools_for_user_intent(
                    message, situation=_situation_of(self._live.get(run.id)),
                ) | plan_required_tools,
                autonomous_seed=True,
            )
            return {
                "mode": "resolved_elsewhere" if resolved_elsewhere else "interjection",
                "run": resumed,
            }

        if execution == RUN_SUSPENDED:
            steering_id = self.store.queue_steering(
                project_id, run.id, message, origin="expert",
            )
            resumed = await self._continue(
                run,
                token,
                message,
                required_tools=_required_tools_for_user_intent(
                    message, situation=_situation_of(self._live.get(run.id)),
                ) | plan_required_tools,
                autonomous_seed=True,
            )
            self.store.mark_steering_delivered(steering_id)
            self.events.publish(
                project_id,
                run.id,
                "message",
                {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "checkpoint_resume"},
            )
            return {"mode": "checkpoint_resume", "run": resumed}

        live = self._live.get(run.id)
        if live is None or execution not in {RUN_QUEUED, RUN_RUNNING}:
            raise AgentRuntimeError("RUN_NOT_STEERABLE", "当前运行暂时不能接收插话", status=409)
        if purpose_review_completed and live.allowed_tools == PURPOSE_REVIEW_ALLOWED_TOOLS:
            if live.deferred_purpose_continuation is None:
                # 批量确认已经把这批资料的用途落库，助手挂起的逐份卡问的是同一件事，
                # 必须一并结算。否则它会被当成「仍未解决的原问题」自动恢复，
                # 用户刚一次性确认完又被推回同一张卡（见 #36）。
                for settled in self.store.settle_interactions_resolved_elsewhere(
                    run.id, reason="资料用途已通过批量确认完成",
                ):
                    live.restore_interaction = None
                    self.events.publish(project_id, run.id, "interaction", settled.public())
                steering_id = self.store.queue_steering(
                    project_id, run.id, message, origin="expert",
                )
                live.deferred_purpose_continuation = (steering_id, token, message)
                self.events.publish(project_id, run.id, "message", {
                    "role": "user",
                    "blocks": [{"type": "text", "text": message}],
                    "delivery": "after_purpose_review",
                })
            return {
                "mode": "after_purpose_review",
                "runId": run.id,
                "delivery": "after_purpose_review",
            }
        steering_id = self.store.queue_steering(
            project_id, run.id, message, origin="expert",
        )
        live.token = token
        live.stable_read_cache.clear()
        live.required_tools.update(_required_tools_for_user_intent(
            message, situation=live.situation, notes=live.intent_notes,
        ))
        live.required_tools.update(plan_required_tools)
        live.release_reentry_authorized = (
            live.release_reentry_authorized or _release_reentry_requested(message)
        )
        await live.steering.put(message)
        self.store.mark_steering_delivered(steering_id)
        self.events.publish(project_id, run.id, "message", {"role": "user", "blocks": [{"type": "text", "text": message}], "delivery": "next_tool_boundary"})
        return {"mode": "steering", "runId": run.id, "delivery": "next_tool_boundary"}

    def project_constraints(self, project_id: str) -> dict[str, Any]:
        """专家约束登记表，含已撤销/被覆盖的历史，页面可见是登记表的一部分。"""
        return {"constraints": self.store.list_constraints(project_id)}

    def revoke_project_constraint(
        self,
        *,
        project_id: str,
        constraint_id: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """专家在页面上直接撤销一条约束——登记表的改判兜底通道。

        模型判断（哪些插话进表）必须是明处可推翻的：第一层是处置工具留痕，
        这里是第二层，专家看到错登的规矩能直接撤掉，不用等下一轮对话。
        """
        revoked = self.store.revoke_constraint(project_id, constraint_id, reason=reason)
        state = self.store.project_state(project_id)
        self._publish_constraints(project_id, state.run_id)
        return {"ok": True, **revoked}

    async def resume_issue(
        self,
        *,
        project_id: str,
        token: str,
        issue_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        _require_project_and_token(project_id, token)
        normalized_issue = str(issue_id or "").strip()
        normalized_action = str(action_id or "").strip()
        if not normalized_issue or not normalized_action:
            raise AgentRuntimeError(
                "ISSUE_ACTION_INVALID", "恢复动作缺少问题身份或动作标识", status=400,
            )
        state = self.store.project_state(project_id)
        run = self.store.active_run(project_id) or (
            self.store.get_run(state.run_id) if state.run_id else None
        )
        if run is None or run.project_id != project_id:
            raise AgentRuntimeError("ISSUE_NOT_FOUND", "当前没有可恢复的问题", status=404)

        technical = self.store.technical_recoveries(run.id).get(normalized_issue)
        repair_snapshot = self.store.validation_repair_state(run.id) or {}
        repair_issue_ids = {
            str(item) for item in repair_snapshot.get("openIssueIds", []) if str(item).strip()
        }
        last_report = repair_snapshot.get("lastReport")
        if isinstance(last_report, dict) and last_report.get("issueId"):
            repair_issue_ids.add(str(last_report["issueId"]))
        is_validation_issue = normalized_issue in repair_issue_ids
        execution = self._execution(project_id, run.id)
        interaction = self.store.pending_interaction(project_id)
        if interaction is not None and interaction.run_id == run.id:
            raise AgentRuntimeError(
                "ISSUE_WAITING_INTERACTION",
                "当前还有一项业务决定等待处理，请先完成该决定",
                status=409,
            )

        if normalized_action == "keep_current_progress":
            if technical is None and not is_validation_issue:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "这个问题已经解决或被后续状态替代", status=409,
                )
            if technical is not None:
                self.store.resolve_technical_recoveries(run.id, [normalized_issue])
            if is_validation_issue:
                self._validation_repair_state(run.id).reset()
            live = self._live.get(run.id)
            if live is not None:
                live.technical_recoveries.pop(normalized_issue, None)
                if technical is not None:
                    required_from_recovery = {
                        str(item).strip()
                        for item in technical.get("requiredTools") or []
                        if str(item).strip()
                    }
                    required_from_recovery.add(str(technical.get("requiredTool") or "").strip())
                    live.required_tools.difference_update(required_from_recovery)
            if execution in {RUN_QUEUED, RUN_RUNNING, RUN_STOPPING}:
                settled_run = await self.cancel(project_id=project_id)
            else:
                updated = self.store.update_run(
                    run.id,
                    status=RUN_CANCELLED,
                    result="已保留当前成果并结束本轮",
                    error="",
                )
                self._publish_state(project_id, run.id)
                self.events.publish(project_id, run.id, "status", {
                    "status": RUN_CANCELLED,
                    "text": "已结束本轮，已完成的候选内容保留",
                })
                settled_run = self._run_public(updated, self.store.project_state(project_id))
            self.events.publish(project_id, run.id, "tool", {
                "name": "issue_recovery",
                "phase": "completed",
                "summary": "已保留当前成果并结束本轮",
                "issueStatus": "resolved",
                "resolvedIssueIds": [normalized_issue],
            })
            return {
                "mode": "kept",
                "run": settled_run,
                "issueId": normalized_issue,
                "resolvedIssueIds": [normalized_issue],
            }

        if normalized_action == "cancel_required_tools":
            # 用户明确表示这项必需步骤不是自己的要求：撤销要求并从当前
            # 进度继续本轮，而不是像 keep_current_progress 那样结束本轮。
            if technical is None or technical.get("strategy") != "required_tools":
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "这个问题已经解决或被后续状态替代", status=409,
                )
            if execution not in {RUN_SUSPENDED, RUN_WAITING}:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "当前运行状态已经变化，请刷新后重试", status=409,
                )
            required_from_recovery = {
                str(item).strip()
                for item in technical.get("requiredTools") or []
                if str(item).strip()
            }
            cancelled_tool = str(technical.get("requiredTool") or "").strip()
            if cancelled_tool:
                required_from_recovery.add(cancelled_tool)
            self.store.resolve_technical_recoveries(run.id, [normalized_issue])
            live = self._live.get(run.id)
            if live is not None:
                live.technical_recoveries.pop(normalized_issue, None)
                live.required_tools.difference_update(required_from_recovery)
            self.events.publish(project_id, run.id, "tool", {
                "name": "issue_recovery",
                "phase": "completed",
                "summary": "已按你的选择取消该必需步骤，本轮从当前进度继续",
                "issueStatus": "resolved",
                "resolvedIssueIds": [normalized_issue],
                "waivedTools": sorted(required_from_recovery),
            })
            resumed = await self._continue(
                run,
                token,
                "用户已明确取消刚才的必需步骤要求，不要再尝试补做这些工具。"
                "从当前项目状态继续主动完成下一步；已经确认的内容保持不变。",
                required_tools=set(),
                autonomous_seed=True,
            )
            return {
                "mode": "checkpoint_resume",
                "run": resumed,
                "issueId": normalized_issue,
                "resolvedIssueIds": [normalized_issue],
            }

        if technical is not None:
            available_actions = {
                str(item.get("id") or "").strip()
                for item in technical.get("recoveryActions") or []
                if isinstance(item, dict)
            }
            available_actions.add(str(technical.get("actionId") or "resume_required_tool"))
            available_actions.discard("")
            if normalized_action not in available_actions:
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "当前问题的恢复动作已经变化，请刷新后重试", status=409,
                )
            technical = dict(technical)
            if normalized_action == "retry_from_latest":
                technical.update({"attempt": 0, "exhausted": False})
                self.store.save_technical_recovery(run.id, normalized_issue, technical)
            required_tools = {
                str(item).strip()
                for item in technical.get("requiredTools") or []
                if str(item).strip()
            }
            required_tool = str(technical.get("requiredTool") or "").strip()
            if required_tool:
                required_tools.add(required_tool)
            if technical.get("strategy") == CONFIRMATION_REVIEW_STRATEGY:
                recovery_prompt = self._confirmation_review_prompt(run)
            elif technical.get("strategy") == MODEL_TRUNCATION_STRATEGY:
                # 这条恢复没有 required tool：上一轮之所以零产出，是模型想在一次
                # 思考里把整套方案推演完，要换的是推进方式而不是补一次工具调用。
                recovery_prompt = (
                    "页面请求从当前进展继续。不要把它当成用户业务发言；"
                    + TRUNCATION_RECOVERY_PROMPT
                )
            elif technical.get("strategy") == PHASE_CAPABILITY_STRATEGY:
                required_phase = str(technical.get("requiredPhase") or "").strip()
                required_label = str(
                    technical.get("requiredPhaseLabel")
                    or PHASE_LABELS.get(required_phase, required_phase)
                )
                if technical.get("blocked") is True:
                    # 这一跳已经被门禁或阶段边挡回来过。还让模型重提同一次转换，
                    # 用户点「继续」只会回到同一个循环，所以改成先读状态、按真正的
                    # 未满足项推进，拿不定主意时回到用户，而不是继续空转。
                    blockers = _string_list(technical.get("blockers"), 10)
                    blocker_text = "；".join(blockers) if blockers else "前置条件尚未满足"
                    required_tools = {"get_project_state"}
                    recovery_prompt = (
                        "页面请求继续上一轮没有落盘的修改。不要把它当成用户业务发言，"
                        "也不要向用户解释平台能力或要求用户等待。"
                        f"这次修改由「{required_label}」阶段承载，但上一次转换被前置条件挡回："
                        f"{blocker_text}。先调用 get_project_state 读取最新状态和可走的转换，"
                        "再针对未满足项推进；需要用户拿主意时用 ask 或 request_confirmation "
                        "给出具体选项，不要重复尝试同一次转换。"
                    )
                else:
                    required_tools = {"transition_project_state"}
                    recovery_prompt = (
                        "页面请求继续上一轮没有落盘的修改。不要把它当成用户业务发言，"
                        "也不要向用户解释平台能力或要求用户等待。"
                        f"先调用 transition_project_state 转到 {required_phase} 阶段，"
                        "工具面重新加载后按已经确认的修复方案原样重提同一次修改，再完成候选确认。"
                    )
            else:
                recovery_prompt = (
                    "页面请求基于最新内容继续一个版本同步问题。不要把它当成用户业务发言；"
                    "先清除旧参数，重新读取当前建模计划和涉及的资源快照，"
                    "再用新的 mutation_id 生成 Change Set。"
                    "只处理尚未完成的任务，不得原样重放旧参数。"
                    if technical.get("strategy") == "refresh_change_set"
                    else (
                        "页面请求继续一个仍然有效的系统恢复问题。不要把它当成用户业务发言；"
                        "立即按已持久化的 required tool 恢复，成功后重新校验候选。"
                    )
                )
            initial_recoveries = {normalized_issue: technical}
            initial_repair = None
        elif is_validation_issue:
            if normalized_action != "review_validation_findings":
                raise AgentRuntimeError(
                    "ISSUE_ACTION_STALE", "当前问题的恢复动作已经变化，请刷新后重试", status=409,
                )
            report_id = str(
                last_report.get("validationReportId")
                if isinstance(last_report, dict) else ""
            ).strip()
            finding_fingerprint = str(
                last_report.get("findingFingerprint")
                if isinstance(last_report, dict) else ""
            ).strip()
            self._validation_repair_state(run.id).reset()
            repair_snapshot = self.store.validation_repair_state(run.id) or {}
            if report_id:
                required_tools = {"get_validation_summary"}
                recovery_prompt = (
                    "页面请求继续当前校验问题。不要重做已经完成的修改；"
                    f"先读取校验报告 {report_id} 的最新摘要和分页明细，"
                    "只处理仍未解决的 finding，随后执行完整候选校验。"
                )
            else:
                required_tools = {"validate_change_set"}
                recovery_prompt = (
                    "页面请求继续当前校验问题。不要重做已经完成的修改；"
                    "先对当前候选重新执行一次完整 Change Set 校验，再只处理仍未解决的 finding。"
                    f"上一轮问题指纹为 {finding_fingerprint}。"
                )
            initial_recoveries = None
            initial_repair = repair_snapshot
        else:
            raise AgentRuntimeError(
                "ISSUE_ACTION_STALE", "这个问题已经解决或被后续状态替代", status=409,
            )

        self.events.publish(project_id, run.id, "tool", {
            "name": "issue_recovery",
            "phase": "repairing",
            "summary": "已按你的选择从当前进展继续处理",
            "code": "RECOVERY_RESUMED",
            "issueId": normalized_issue,
            "issueStatus": "repairing",
            "problemVisibility": "activity",
        })
        if execution in {RUN_SUSPENDED, RUN_WAITING}:
            resumed = await self._continue(
                run,
                token,
                recovery_prompt,
                required_tools=required_tools,
                autonomous_seed=True,
            )
            return {"mode": "checkpoint_resume", "run": resumed, "issueId": normalized_issue}
        live = self._live.get(run.id)
        if live is not None and execution in {RUN_QUEUED, RUN_RUNNING}:
            live.token = token
            live.required_tools.update(required_tools)
            if technical is not None:
                live.technical_recoveries[normalized_issue] = technical
            live.stable_read_cache.clear()
            await live.steering.put(recovery_prompt)
            return {
                "mode": "steering",
                "runId": run.id,
                "issueId": normalized_issue,
                "delivery": "next_tool_boundary",
            }

        started = await self.start_run(
            project_id=project_id,
            token=token,
            message=recovery_prompt,
            publish_user_message=False,
            required_tools=required_tools,
            release_reentry_authorized=True,
            technical_recoveries=initial_recoveries,
            validation_repair_snapshot=initial_repair,
        )
        if technical is not None:
            self.store.resolve_technical_recoveries(run.id, [normalized_issue])
        return {"mode": "new_run", "run": started, "issueId": normalized_issue}

    def _verify_evidence_anchors(
        self,
        project_id: str,
        evidence: str,
        *,
        exclude_interjection_id: int = 0,
    ) -> None:
        """证据锚点逐个查表验真：只能引用真实存在的记录。

        「已满足/已完成」是模型自报，纯文字承诺无法审计。锚点必须指向本库
        的记录（约束、插话、交互），或本项目真实工具往来里出现过的标识
        （修订号、文件路径等）。只验存在与归属，不验语义——语义兜底靠处置
        记录对专家可见、可改判。被处置的插话自己不能作为自己的证据。
        """
        anchors = _parse_evidence_anchors(evidence)
        if not anchors:
            raise AgentRuntimeError(
                "INTERJECTION_EVIDENCE_UNVERIFIABLE",
                "evidence 必须包含至少一个可验真锚点（形如 revision:修订号、"
                "file:路径、constraint:编号、task:编号、interaction:交互ID，"
                "多个用分号分隔，可附说明文字），不能只写文字承诺",
                status=400,
            )
        for kind, value in anchors:
            if kind == "constraint":
                verified = value.isdigit() and self.store.constraint_exists(
                    project_id, int(value),
                )
            elif kind in {"task", "interjection"}:
                verified = (
                    value.isdigit()
                    and int(value) != exclude_interjection_id
                    and self.store.interjection_exists(project_id, int(value))
                )
            elif kind == "interaction":
                verified = self.store.interaction_exists(project_id, value)
            else:
                verified = (
                    len(value) >= _EVIDENCE_TOKEN_MIN_LENGTH
                    and self.store.evidence_token_exists(project_id, value)
                )
            if not verified:
                raise AgentRuntimeError(
                    "INTERJECTION_EVIDENCE_NOT_FOUND",
                    f"证据锚点查无实据：{kind}:{value}。只能引用真实存在的记录，"
                    "或本项目工具结果里实际出现过的标识；不要编造，也不要引用"
                    "被处置的插话自身",
                    status=400,
                )

    def _interjection_action(
        self,
        live: LiveRun,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """插话处置与约束工具。不走乐观锁：处置的是插话自己的生命周期，
        不动 project_agent_state；重复处置由 store 的状态守卫拒绝。"""
        project_id = live.project_id
        if name == "revoke_constraint":
            constraint_id = int(arguments.get("constraint_id") or 0)
            reason = str(arguments.get("reason") or "").strip()
            revoked = self.store.revoke_constraint(project_id, constraint_id, reason=reason)
            self._publish_constraints(project_id, live.run_id)
            return {
                "ok": True,
                **revoked,
                "display": {"text": f"已撤销专家约束 #{constraint_id}，下一轮起不再生效"},
            }
        if name == "complete_interjection_task":
            interjection_id = int(arguments.get("interjection_id") or 0)
            evidence = str(arguments.get("evidence") or "").strip()
            if not evidence:
                raise AgentRuntimeError(
                    "INTERJECTION_EVIDENCE_REQUIRED",
                    "完成待办必须在 evidence 提供可验真锚点（形如 revision:修订号、"
                    "file:路径，分号分隔）作为证据",
                    status=400,
                )
            self._verify_evidence_anchors(
                project_id, evidence, exclude_interjection_id=interjection_id,
            )
            completed = self.store.complete_interjection_task(
                project_id, interjection_id, evidence=evidence,
            )
            return {
                "ok": True,
                **completed,
                "display": {"text": f"插话待办 #{interjection_id} 已完成并留下证据"},
            }
        # dispose_interjection
        interjection_id = int(arguments.get("interjection_id") or 0)
        resolution = str(arguments.get("resolution") or "")
        text = str(arguments.get("text") or "").strip()
        evidence = str(arguments.get("evidence") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        if resolution == "constraint":
            if not text:
                raise AgentRuntimeError(
                    "INTERJECTION_TEXT_REQUIRED", "登记约束必须提供规矩文本", status=400,
                )
            raw_replaces = arguments.get("replaces_constraint_id")
            replaces_id = (
                int(raw_replaces)
                if isinstance(raw_replaces, int) and not isinstance(raw_replaces, bool)
                else None
            )
            constraint = self.store.register_constraint(
                project_id, text,
                source_steering_id=interjection_id,
                replaces_id=replaces_id,
            )
            detail: dict[str, Any] = {"constraintId": constraint["id"], "text": text}
            outcome = f"已登记为专家约束 #{constraint['id']}，每轮生效直到专家撤销"
        elif resolution == "task":
            if not text:
                raise AgentRuntimeError(
                    "INTERJECTION_TEXT_REQUIRED", "转待办必须提供要做的事", status=400,
                )
            detail = {"text": text}
            outcome = "已转成跨轮待办，完成后需调用 complete_interjection_task 附证据"
        elif resolution == "satisfied":
            # 「已满足」是模型自报，天然是合理化逃生口：不带证据锚点的满足
            # 声明会让强制表态退化成合规表演，歧义只是从「沉默」搬到「假阳性」。
            if not evidence:
                raise AgentRuntimeError(
                    "INTERJECTION_EVIDENCE_REQUIRED",
                    "「已满足」必须在 evidence 提供可验真锚点（形如 revision:修订号、"
                    "file:路径、constraint:编号，分号分隔，可附说明文字）",
                    status=400,
                )
            self._verify_evidence_anchors(
                project_id, evidence, exclude_interjection_id=interjection_id,
            )
            detail = {"evidence": evidence}
            outcome = "已标记满足并留下经验真的证据"
        elif resolution == "answered":
            # 纯提问型插话：不产生产物，没有锚点可引。回答本身由可见正文
            # 承载（rails 保证本轮必有非空可见输出），这里只登记回答要点。
            if not text:
                raise AgentRuntimeError(
                    "INTERJECTION_TEXT_REQUIRED",
                    "「已回答」必须在 text 简述本轮给出的回答要点",
                    status=400,
                )
            detail = {"answer": text}
            outcome = "已登记为提问并在本轮正文回答"
        elif resolution == "rejected":
            if not reason:
                raise AgentRuntimeError(
                    "INTERJECTION_REASON_REQUIRED", "拒绝插话必须说明理由", status=400,
                )
            detail = {"reason": reason}
            outcome = "已拒绝并记录理由；专家可以在对话里继续追问或调整"
        else:
            raise AgentRuntimeError(
                "INTERJECTION_RESOLUTION_INVALID",
                "resolution 只能是 constraint、task、satisfied、answered 或 rejected",
                status=400,
            )
        disposed = self.store.dispose_interjection(
            project_id, interjection_id, resolution=resolution, detail=detail,
        )
        if resolution == "constraint":
            self._publish_constraints(project_id, live.run_id)
        return {"ok": True, **disposed, "display": {"text": outcome}}

    def _publish_constraints(self, project_id: str, run_id: str) -> None:
        self.events.publish(project_id, run_id, "constraints", {
            "active": self.store.active_constraints(project_id),
        })

    async def _state_action(
        self,
        live: LiveRun,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "get_project_state":
            return await self._state_for_agent(live)
        if name in {"dispose_interjection", "complete_interjection_task", "revoke_constraint"}:
            return self._interjection_action(live, name, arguments)

        current = self.store.project_state(live.project_id)
        expected = arguments.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != current.revision:
            raise AgentRuntimeError(
                "STATE_REVISION_CONFLICT",
                "项目 Agent 状态已经变化，请重新读取",
                status=409,
                details={"expectedRevision": expected, "currentRevision": current.revision},
            )
        phase, _separator, execution = current.state.partition(":")
        if live.cancel_requested or execution in {RUN_STOPPING, RUN_CANCELLED}:
            raise asyncio.CancelledError
        if current.run_id != live.run_id or execution != RUN_RUNNING:
            raise AgentRuntimeError(
                "STATE_TRANSITION_INVALID",
                "只有当前运行中的 Agent 可以更新项目状态",
                status=409,
                details={"state": current.state, "runId": current.run_id},
            )
        summary = str(arguments.get("summary") or "").strip()
        if not summary:
            raise AgentRuntimeError("STATE_SUMMARY_REQUIRED", "更新状态时必须提供精简摘要", status=400)
        questions = _string_list(arguments.get("open_questions"), 30)
        evidence = _string_list(arguments.get("evidence"), 50)
        evaluation = await self._evaluation_status_for_agent(live)
        claims_running = (
            _claims_active_evaluation(summary)
            or any(_claims_active_evaluation(item) for item in questions)
        )
        if claims_running and evaluation.get("available") is not True:
            raise AgentRuntimeError(
                "STATE_EVALUATION_STATUS_UNAVAILABLE",
                "尚未取得权威评测状态，不能把评测记录为仍在运行",
                status=409,
                details={
                    "evaluation": evaluation,
                    "next": ["重新调用 get_project_state，取得可用的 evaluation 快照后再更新"],
                },
            )
        if evaluation.get("terminal") is True and claims_running:
            raise AgentRuntimeError(
                "STATE_EVALUATION_STATUS_CONFLICT",
                "最近一轮评测已经结束，不能把它记录为仍在运行",
                status=409,
                details={
                    "evaluation": evaluation,
                    "next": ["以 get_project_state 返回的 evaluation 终态更新摘要和开放问题"],
                },
            )

        if name == "update_project_context":
            updated = self.store.transition_project_state(
                live.project_id,
                expected_revision=current.revision,
                next_state=current.state,
                cause="context_updated",
                actor_run_id=live.run_id,
                summary=summary,
                open_questions=questions,
                metadata={"evidence": evidence},
            )
        elif name == "transition_project_state":
            target = str(arguments.get("to_phase") or "")
            allowed = PHASE_EDGES.get(phase, ())
            if target not in allowed:
                raise AgentRuntimeError(
                    "STATE_TRANSITION_INVALID",
                    f"不能从“{PHASE_LABELS.get(phase, phase)}”直接进入“{PHASE_LABELS.get(target, target)}”",
                    status=409,
                    details={"allowed": list(allowed)},
                )
            if phase == "release" and target in {"goal", "process", "knowledge"} and not live.release_reentry_authorized:
                raise AgentRuntimeError(
                    "STATE_RELEASE_REENTRY_REQUIRED",
                    "已发布项目只有在用户明确发起新迭代后才能回到知识建模",
                    status=409,
                    details={"next": ["请用户明确说明要增量补资料、融合来源包或修改已发布知识"]},
                )
            gate = PHASE_GATES.get(target)
            if gate:
                result = await self._core_call(
                    live, "gate_status", {"gate": gate},
                )
                if result.get("error"):
                    raise AgentRuntimeError(
                        str(result.get("error")),
                        str(result.get("reason") or "状态门禁检查失败"),
                        status=409,
                        details={"next": result.get("next") or []},
                    )
                if not result.get("pass"):
                    raise AgentRuntimeError(
                        "STATE_GATE_BLOCKED",
                        f"还不能进入“{PHASE_LABELS[target]}”",
                        status=409,
                        details={"gate": gate, "blockers": result.get("blockers") or []},
                    )
            intent = str(arguments.get("intent") or "").strip()
            if not intent:
                raise AgentRuntimeError("STATE_INTENT_REQUIRED", "状态转换必须说明意图", status=400)
            updated = self.store.transition_project_state(
                live.project_id,
                expected_revision=current.revision,
                next_state=f"{target}:{execution}",
                cause="phase_transition",
                actor_run_id=live.run_id,
                summary=summary,
                open_questions=questions,
                metadata={"intent": intent[:2_000], "evidence": evidence, "gate": gate},
            )
        else:
            raise AgentRuntimeError("STATE_TOOL_NOT_FOUND", f"未知项目状态工具：{name}", status=404)

        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS.get(public["phase"], public["phase"])
        self.events.publish(live.project_id, live.run_id, "state", public)
        return {**public, "display": {"text": f"项目状态已更新为“{public['phaseLabel']}”"}}

    async def _core_call(
        self,
        live: LiveRun,
        tool: str,
        arguments: dict[str, Any],
        *,
        poll_seconds: float = 0.1,
    ) -> dict[str, Any]:
        """Run a read/guard Core call behind the live cancellation fence."""
        if live.cancel_requested or live.cancel_event.is_set():
            raise asyncio.CancelledError
        call = asyncio.create_task(self._gateway.call(
            project_id=live.project_id,
            run_id=live.run_id,
            token=live.token,
            tool=tool,
            arguments=arguments,
        ))
        live.inflight_core_calls.add(call)
        try:
            while True:
                if live.cancel_requested or live.cancel_event.is_set():
                    call.cancel()
                    await asyncio.gather(call, return_exceptions=True)
                    raise asyncio.CancelledError
                done, _pending = await asyncio.wait({call}, timeout=poll_seconds)
                if done:
                    if live.cancel_requested or live.cancel_event.is_set():
                        raise asyncio.CancelledError
                    return call.result()
        except BaseException:
            if not call.done():
                call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            raise
        finally:
            live.inflight_core_calls.discard(call)

    async def _core_call_for_run(
        self,
        project_id: str,
        run_id: str,
        token: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        live: LiveRun | None = None,
    ) -> dict[str, Any]:
        """Cancellation-aware Core call for an interaction callback.

        Answering a card can happen outside the model task.  Reuse the live
        call fence when present; otherwise consult the persisted run state and
        refuse to submit a late answer to a stopped run.
        """
        if live is not None:
            return await self._core_call(live, tool, arguments)
        execution = self._execution(project_id, run_id)
        if execution in {RUN_STOPPING, RUN_CANCELLED, ""}:
            raise AgentRuntimeError("RUN_NOT_ACTIVE", "当前运行已经停止，操作未提交", status=409)
        call = asyncio.create_task(self._gateway.call(
            project_id=project_id,
            run_id=run_id,
            token=token,
            tool=tool,
            arguments=arguments,
        ))
        try:
            return await call
        finally:
            if not call.done():
                call.cancel()

    async def _after_core_tool(
        self,
        live: LiveRun,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        failed = bool(isinstance(result, dict) and result.get("error"))
        _record_tool_outcome(live, name, failed)
        _record_tool_call(live, name, arguments)
        if failed:
            loop_reason = _detect_loop(live)
            if loop_reason:
                raise AgentRuntimeError(
                    "AGENT_TOOL_LOOP_DETECTED",
                    f"连续工具失败，已停止自动重试：{loop_reason}。之前已保存的进展已保留，请修正失败原因后从当前进展重试。",
                    status=409,
                    details={"loop_reason": loop_reason, "tool": name},
                )
            # 失败结果不代表状态，不能拿去回填快照
            return
        _absorb_plan_progress(live, result)
        _absorb_reading_modes(live, result)
        if name in {
            "get_product_ledger", "stage_product_ledger", "finalize_product_ledger",
            "get_product_ledger_progress",
        }:
            _absorb_product_ledger(live, result)
        _absorb_stage_delivery(live, name, result)
        _absorb_awaiting_user(live, result)
        if name == "create_change_plan":
            consume = getattr(self._gateway, "acceptance_fault", None)
            fault: dict[str, Any] = {}
            if callable(consume):
                try:
                    value = await consume(
                        project_id=live.project_id,
                        run_id=live.run_id,
                        token=live.token,
                        fault_id="F16",
                    )
                    fault = value if isinstance(value, dict) else {}
                except Exception:  # noqa: BLE001 - acceptance controls never affect production
                    fault = {}
            if fault.get("active") is True:
                plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
                if plan:
                    self.events.publish(live.project_id, live.run_id, "change_plan", {"plan": plan})
                self.events.publish(live.project_id, live.run_id, "progress", {
                    "activityId": f"acceptance-batch-wait:{live.run_id}",
                    "text": "当前业务批次已暂停，等待继续或停止",
                    "detail": "建模计划已经保存，尚未开始第一批写入",
                    "phase": "waiting_checkpoint",
                    "counters": {},
                })
                await live.cancel_event.wait()
                raise asyncio.CancelledError
            plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
            if plan.get("approval_status") == "pending":
                live.plan_approval_required = True
            return
        if name == "set_material_directive":
            document = result.get("document") if isinstance(result.get("document"), dict) else {}
            filename = str(document.get("filename") or arguments.get("filename") or "").strip()
            if filename:
                live.observed_material_directives.add(filename)
            if result.get("requiresUserConfirmation") is True:
                return
        # 读取资料是只读动作，不能改变已发布事实或业务阶段。
        if name == "read_documents_fully":
            return
        if name != "set_material_directive":
            return
        purposes: list[str] = []
        filenames: list[str] = []
        if name == "set_material_directive":
            document = result.get("document") if isinstance(result.get("document"), dict) else {}
            purpose = str(document.get("purpose") or arguments.get("purpose") or "auto")
            purposes.append(purpose)
            filenames.append(str(document.get("filename") or arguments.get("filename") or ""))
            # 只改变阅读方式时，等全文读取成功后再回退阶段。
            if purpose == "auto":
                return
        target = "goal" if "goal" in purposes else "knowledge"
        current = self.store.project_state(live.project_id)
        phase, _separator, execution = current.state.partition(":")
        if phase in {"release", "increment"}:
            return
        if phase not in PHASE_ORDER or PHASE_ORDER.index(phase) <= PHASE_ORDER.index(target):
            return
        if current.run_id != live.run_id or execution != RUN_RUNNING:
            raise AgentRuntimeError(
                "STATE_TRANSITION_INVALID",
                "资料设置完成，但项目阶段已经由另一轮处理更新",
                status=409,
            )
        updated = self.store.transition_project_state(
            live.project_id,
            expected_revision=current.revision,
            next_state=f"{target}:{execution}",
            cause="material_directive_applied",
            actor_run_id=live.run_id,
            metadata={
                "filenames": [item for item in filenames if item][:50],
                "purposes": purposes[:50],
            },
        )
        public = updated.public()
        public["phaseLabel"] = PHASE_LABELS.get(target, target)
        self.events.publish(live.project_id, live.run_id, "state", public)

    async def _gate_progress(
        self,
        live: LiveRun,
        gate: str,
    ) -> tuple[str, list[str], str]:
        """读一道门禁，归一成 (状态, 可推进项, 卡住原因)。

        状态五取一：unreachable 是网关调用失败；unknown 是门禁不存在或返回错误；
        passed 已通过；actionable 有助手能独立推进的事；blocked 是服务端明确标注
        为空——助手推不动，和「没算出来」必须区分开。前向门禁和阶段自身门禁共用
        这段判定，但对 unreachable 的处置不同：前向门禁沿用原先的整体中止。
        """
        try:
            result = await self._gateway.call(
                project_id=live.project_id,
                run_id=live.run_id,
                token=live.token,
                tool="gate_status",
                arguments={"gate": gate},
            )
        except Exception:
            # 目标只是引导，取不到不该影响本轮执行。
            logger.warning("turn goal gate lookup failed run=%s gate=%s", live.run_id, gate)
            return ("unreachable", [], "")
        if result.get("error"):
            return ("unknown", [], "")
        if result.get("pass"):
            return ("passed", [], "")
        # 优先用服务端标注的「助手可独立推进项」。blockers 面向用户，说的是
        # 「还差什么」，其中不少条目的主语是用户——把「由项目负责人完成发布」
        # 指派给助手只会让它困惑或越界。
        raw_actionable = result.get("agentActionable")
        if raw_actionable is None:
            # 服务端还没标注这道门禁，退回 blockers 保持兼容。
            items = _string_list(result.get("blockers") or result.get("next"), 30)
        else:
            # 已标注：空表示这道门禁当前没有助手能独立推进的事，
            # 此时不能退回 blockers，否则又会把用户的事派给助手。
            items = _string_list(raw_actionable, 30)
        if items:
            return ("actionable", items, "")
        if raw_actionable is None:
            return ("unknown", [], "")
        return ("blocked", [], "；".join(_string_list(result.get("blockers"), 3)))

    @staticmethod
    def _actionable_goal(gate: str, items: list[str]) -> str:
        remaining = f"，该门禁共 {len(items)} 项待推进" if len(items) > 1 else ""
        return (
            f"{items[0]}\n\n"
            f"这一项来自门禁 {gate}{remaining}。本轮只推进这一项，完成后即可结束本轮；"
            "不要顺带去做清单上的其它项，也不要自行改换目标。\n"
            "如果这一项的前提确实不具备，直接调用 ask 说明缺什么，"
            "不要反复读取资料试图自行补齐前提。"
        )

    def _blocked_goal(self, live: LiveRun, gate: str, reason: str) -> str:
        # 「算出来助手没事做」和「没算出来」必须给出不同指令。过去两者都是
        # 返回空串，模型这一轮就完全没有目标——而没有目标正是空转的起点，
        # 它会自己找活，再读一份资料永远算得上可执行动作。
        live.situation.no_actionable_gate = gate
        live.situation.no_actionable_reason = reason
        detail = f"当前卡在：{reason}\n\n" if reason else ""
        return (
            "本轮没有你能独立推进的事项。\n\n"
            f"{detail}"
            f"门禁 {gate} 还没通过，但它剩下的事要由用户或专家在别处完成，"
            "你再宣告多少遍也推不动。请用一段话向专家说明卡在哪一项、"
            "需要他做什么，然后结束本轮。不要自行改换目标去做别的事，"
            "也不要反复读取资料等它变化——外部事件会重新唤醒你。"
        )

    @staticmethod
    def _interjection_goal(items: list[dict[str, Any]]) -> str:
        """未处置的专家插话即本轮目标（items[0] 纪律，与门禁目标同构）。

        宽进：不在这里做语义准入判断——「这句话会不会碰不变量」代码算不出来。
        探测点后移到执行层：阶段守卫和工具拦截是确定性的，撞墙时目标文本已经
        约定了升级出口（conflict_decision 裁决卡），模型没有仲裁角色。
        """
        head = items[0]
        remaining = f"，另有 {len(items) - 1} 条插话排在后面" if len(items) > 1 else ""
        return (
            f"专家插话 #{head['id']} 尚未处置：\n\n"
            f"「{head['text']}」\n\n"
            f"本轮以完成并处置这条插话为唯一目标{remaining}。"
            "按「这条指令什么时候失效」判断它的寿命并选择出口：\n"
            "- 「不许/一律/以后都」这类长期规矩：调用 dispose_interjection(resolution='constraint') 登记为专家约束；\n"
            "- 本轮能完成的一次性动作：直接执行，然后 dispose_interjection(resolution='satisfied') 并在 evidence "
            "提供可验真锚点（形如 revision:修订号、file:路径，分号分隔）；\n"
            "- 本轮做不完的动作：dispose_interjection(resolution='task') 转成待办；\n"
            "- 纯提问或咨询：先在正文回答，然后 dispose_interjection(resolution='answered') 并在 text 简述回答要点；\n"
            "- 与已批准计划或业务事实冲突：dispose_interjection(resolution='rejected') 说明理由。\n"
            "执行中被阶段守卫或工具拦截时，不要放弃也不要绕过：把拦截返回的原因、"
            "合法路径和代价整理成 ask(kind='conflict_decision') 请专家裁决，"
            "系统会把这条插话标记为已升级待决。\n"
            "本轮结束前必须完成处置；不要顺带去做门禁清单上的其它项。"
        )

    @staticmethod
    def _interjection_task_goal(items: list[dict[str, Any]]) -> str:
        head = items[0]
        remaining = f"，该队列共 {len(items)} 项待办" if len(items) > 1 else ""
        return (
            f"完成插话转来的待办 #{head['id']}：\n\n"
            f"「{head['text']}」\n\n"
            f"这一项来自专家插话{remaining}。本轮只推进这一项，完成后调用 "
            "complete_interjection_task 并在 evidence 提供可验真锚点"
            "（形如 revision:修订号、file:路径），然后结束本轮；"
            "不要顺带去做清单上的其它项，也不要自行改换目标。\n"
            "如果这一项的前提确实不具备，直接调用 ask 说明缺什么，"
            "不要反复读取资料试图自行补齐前提。"
        )

    async def _turn_goal(self, live: LiveRun) -> str:
        """从当前阶段「向前推进」那个门禁的未完成项里，派生本轮要完成的单一目标。

        门禁 blockers 本来就是服务端算出来的任务清单，只是过去它只作为
        get_project_state 的一个字段返回——和文件正文一样是普通工具返回值，
        很快被后续读取挤出窗口，模型于是自己另找事做。这里把清单的第一项
        提升为本轮的指派目标，交给 system 段常驻。

        专家插话优先于门禁：门禁是服务端替专家算出来的约束，是手段；插话是
        专家亲口说的目的。两者不并列——插话成为那个唯一目标，门禁项排在它
        之后。一次性动作转出的待办排第二，都清空后才回到门禁目标。

        只看前进方向的门禁：退回目标阶段的门禁不代表本轮该做什么。

        配置了自身门禁的阶段（增量轮次）从不落回前向门禁。前向门禁
        （knowledge_confirmed、ready_for_eval）都是全量口径，派下来的是「补齐
        基线里某个操作的输入定义」这类本轮明令不准碰的活；自身门禁拿不到
        结论时（导入开的轮次还没有影响记录）派「说明并待命」目标。

        两种返回都有意义，不能混用：有可推进项时返回那一项，门禁未过但服务端
        明确标注助手无可推进项时返回一段「说明并收尾」的指令，并在 situation
        上记下门禁名，让续跑判定收掉本轮。只有确实拿不到门禁结论时才返回空串。
        """
        live.situation.no_actionable_gate = ""
        live.situation.no_actionable_reason = ""
        interjections = self.store.pending_interjections(live.project_id)
        if interjections:
            return self._interjection_goal(interjections)
        tasks = self.store.open_interjection_tasks(live.project_id)
        if tasks:
            return self._interjection_task_goal(tasks)
        phase = self.store.project_state(live.project_id).state.partition(":")[0]
        if phase not in PHASE_ORDER:
            return ""
        self_gate = PHASE_SELF_GATES.get(phase)
        if self_gate:
            status, items, reason = await self._gate_progress(live, self_gate)
            if status == "actionable":
                return self._actionable_goal(self_gate, items)
            if status == "blocked":
                return self._blocked_goal(live, self_gate, reason)
            if status == "passed":
                return (
                    "本阶段的工作已经完成。\n\n"
                    f"门禁 {self_gate} 已通过。调用 transition_project_state 转到下一阶段"
                    "（受影响内容涉及脚本时转 validation，否则转 evaluation），然后结束本轮。\n"
                    "不要回头去做全量建模阶段的事，也不要自行改换目标。"
                )
            # unreachable / unknown：增量轮自身门禁拿不到结论——典型是知识包导入
            # 开的轮次还没有资料影响记录。过去这里落回前向门禁「保持原有行为」，
            # 但前向门禁全是全量口径，派下来的是「补齐基线里某操作的输入定义」
            # 这类明令不归本次增量的活：防空转和阶段隔离在这里打架，落回全量是
            # 选错了边。第三个选项一直存在——派「说明并待命」的收尾目标，
            # 既不给模型自己找活的空档，也不越增量的界。
            live.situation.no_actionable_gate = self_gate
            live.situation.no_actionable_reason = "尚无已界定的资料影响记录"
            return (
                "本轮没有已界定的增量影响需要处理。\n\n"
                "增量轮次只处理受资料变更影响的内容；导入基线的已有问题不属于本次增量。"
                "请用一段话向专家说明当前在等待补充或替换资料（或其它明确指示），"
                "然后结束本轮。不要去做全量建模阶段的事，也不要反复读取资料"
                "等它变化——新资料上传后系统会重新唤醒你。"
            )
        current_index = PHASE_ORDER.index(phase)
        blocked: tuple[str, str] | None = None
        for target in PHASE_EDGES.get(phase, ()):
            if target not in PHASE_ORDER or PHASE_ORDER.index(target) <= current_index:
                continue
            gate = PHASE_GATES.get(target)
            if not gate:
                continue
            status, items, reason = await self._gate_progress(live, gate)
            if status == "unreachable":
                # 取不到门禁结论时不派目标，沿用原先的整体中止。
                return ""
            if status in {"unknown", "passed"}:
                continue
            if status == "blocked" and blocked is None:
                # 先记下来继续看后面的前向门禁——增量轮次这类阶段有不止一条
                # 前向边，不能因为第一道卡住就放弃第二道确实有活的那道。
                blocked = (gate, reason)
                continue
            if status == "actionable":
                return self._actionable_goal(gate, items)
        if blocked is not None:
            return self._blocked_goal(live, blocked[0], blocked[1])
        return ""

    async def _state_for_agent(self, live: LiveRun) -> dict[str, Any]:
        state = self.store.project_state(live.project_id)
        public = state.public()
        execution = str(public.get("execution") or "")
        cancellation_pending = execution == RUN_SUSPENDED and state.error in {
            INTERRUPTED_CANCELLATION_REASON,
            CANCELLATION_PENDING_REASON,
        }
        if live.cancel_requested or execution in {RUN_STOPPING, RUN_CANCELLED} or cancellation_pending:
            # Cancellation is a normal terminal observation.  In particular,
            # do not expose the persisted cancellation reason as a generic
            # ``error`` field which the tool-trace classifier would render as
            # a failed get_project_state call.
            public.pop("error", None)
            public["cancellation"] = {
                "requested": True,
                "status": execution,
                "reason": "用户已停止运行" if execution == RUN_CANCELLED else "当前运行正在停止",
            }
            public["phaseLabel"] = PHASE_LABELS.get(str(public.get("phase") or ""), str(public.get("phase") or ""))
            return {
                **public,
                "evaluation": {"available": False, "status": "cancelled", "terminal": True},
                "dataSource": {"configured": False, "mode": "unknown"},
                "allowedTransitions": [],
                "display": {"text": "运行已停止，当前操作已取消"},
            }
        evaluation = await self._evaluation_status_for_agent(live)
        if evaluation.get("terminal") is True:
            if (
                _claims_active_evaluation(str(public.get("summary") or ""))
                or _claims_terminal_evaluation(str(public.get("summary") or ""))
            ):
                public["summary"] = _evaluation_terminal_summary(evaluation)
            public["openQuestions"] = [
                item for item in public.get("openQuestions") or []
                if not _claims_active_evaluation(str(item))
            ]
        phase = str(public["phase"])
        data_source = await self._core_call(live, "project_data_source_status", {})
        if data_source.get("error"):
            data_source = {
                "configured": False,
                "mode": "unknown",
                "error": str(data_source.get("reason") or "数据源配置读取失败"),
            }
        transitions: list[dict[str, Any]] = []
        for target in PHASE_EDGES.get(phase, ()):
            gate = PHASE_GATES.get(target)
            ready = True
            blockers: list[str] = []
            if gate:
                result = await self._core_call(live, "gate_status", {"gate": gate})
                ready = not result.get("error") and bool(result.get("pass"))
                blockers = _string_list(result.get("blockers") or result.get("next"), 30)
                if result.get("error") and not blockers:
                    blockers = [str(result.get("reason") or "门禁检查失败")]
            if phase == "release" and target in {"goal", "process", "knowledge"} and not live.release_reentry_authorized:
                ready = False
                blockers = ["本轮没有用户明确发起新迭代"]
            transitions.append({
                "toPhase": target,
                "label": PHASE_LABELS[target],
                "gate": gate,
                "ready": ready,
                "blockers": blockers,
            })
        public["phaseLabel"] = PHASE_LABELS.get(phase, phase)
        return {
            **public,
            "evaluation": evaluation,
            "dataSource": data_source,
            "allowedTransitions": transitions,
            # 插话编号是处置工具的入参来源；这些字段只供内部编排，
            # 不得出现在用户可见正文里（与 scopeResolved 同一纪律）。
            "pendingInterjections": self.store.pending_interjections(live.project_id),
            "openInterjectionTasks": self.store.open_interjection_tasks(live.project_id),
            "expertConstraints": self.store.active_constraints(live.project_id),
            "display": {"text": f"当前阶段：{public['phaseLabel']}"},
        }

    async def _evaluation_status_for_agent(self, live: LiveRun) -> dict[str, Any]:
        state = self.store.project_state(live.project_id)
        execution = state.state.partition(":")[2]
        cancellation_pending = execution == RUN_SUSPENDED and state.error in {
            INTERRUPTED_CANCELLATION_REASON,
            CANCELLATION_PENDING_REASON,
        }
        if live.cancel_requested or execution in {RUN_STOPPING, RUN_CANCELLED} or cancellation_pending:
            return {"available": False, "status": "cancelled", "terminal": True}
        try:
            result = await self._core_call(live, "evaluation_status", {})
        except Exception as error:  # noqa: BLE001 - project state remains readable during Core outage
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": f"暂时无法读取评测状态：{type(error).__name__}",
            }
        if result.get("error") == "EVALUATION_NOT_STARTED":
            return {"available": False, "status": "not_started", "terminal": False}
        if result.get("error"):
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": str(result.get("reason") or "暂时无法读取评测状态"),
            }
        status = str(result.get("status") or "unknown")
        if status not in {"running", "done", "failed", "cancelled"}:
            return {
                "available": False,
                "status": "unavailable",
                "terminal": False,
                "reason": "Core 没有返回可识别的评测状态",
            }
        return {
            "available": True,
            "roundId": str(result.get("roundId") or ""),
            "status": status,
            "terminal": status in {"done", "failed", "cancelled"},
            "done": max(0, int(result.get("done") or 0)),
            "total": max(0, int(result.get("total") or 0)),
            "passed": max(0, int(result.get("passed") or 0)),
            "createdAt": result.get("createdAt"),
            "finishedAt": result.get("finishedAt"),
        }

    async def cancel(self, *, project_id: str, token: str = "") -> dict[str, Any]:
        lock = self._cancel_locks.setdefault(project_id, asyncio.Lock())
        async with lock:
            return await self._cancel_locked(project_id=project_id, token=token)

    async def _cancel_locked(self, *, project_id: str, token: str = "") -> dict[str, Any]:
        run = self.store.active_run(project_id)
        if run is None:
            state = self.store.project_state(project_id)
            _phase, _separator, execution = state.state.partition(":")
            settled = self.store.get_run(state.run_id) if state.run_id else None
            if settled is not None and execution == RUN_CANCELLED:
                public = self._run_public(settled, state)
                public["cancellation"] = {"quiescent": True, "idempotent": True}
                return public
            if (
                execution in ACTIVE_RUN_STATUSES
                and state.run_id
                and self.store.get_run(state.run_id) is None
            ):
                return await self.safe_terminate_stale(project_id=project_id)
            raise AgentRuntimeError("RUN_NOT_ACTIVE", "当前没有运行中的 Agent", status=409)

        live = self._live.get(run.id)
        if live:
            live.cancel_requested = True
            live.cancel_event.set()
        _stopping, closed_interactions = self.store.begin_run_cancellation(run.id)
        for interaction in closed_interactions:
            self.events.publish(project_id, run.id, "interaction", interaction.public())
        self.events.publish(project_id, run.id, "status", {
            "status": RUN_STOPPING,
            "text": "正在停止并核对已经发出的修改",
        })
        self._publish_state(project_id, run.id)

        # Install the Core-side fence before draining local tasks. A missing
        # optional method is treated as legacy compatibility; an unavailable
        # real Core is not considered quiescent.
        core_reconciliation = await self._request_core_cancellation(
            run, live, token=token, drain=False,
        )
        # Evaluation cancellation is a best-effort side effect.  Do not let
        # the local task cancellation race block Core reconciliation or turn
        # a normal stop into a timeout error.
        if live and live.evaluation_launch_active:
            try:
                await asyncio.wait_for(
                    self._gateway.call(
                        project_id=project_id,
                        run_id=run.id,
                        token=live.token,
                        tool="cancel_evaluation",
                        arguments={},
                    ),
                    timeout=10,
                )
            except Exception as error:  # noqa: BLE001 - local cancellation must still complete
                logger.warning("Could not cancel core evaluation for run %s: %s", run.id, error)

        tasks: list[asyncio.Task[Any]] = []
        if live:
            tasks.extend(live.inflight_core_calls)
            if live.task and not live.task.done():
                tasks.append(live.task)
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # The Core writer may finish just after the first fence request. Poll
        # its reconciliation endpoint briefly before deciding whether the Run
        # can truthfully become cancelled.
        for attempt in range(CANCELLATION_RECONCILIATION_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CANCELLATION_RECONCILIATION_DELAY_SECONDS)
            core_reconciliation = await self._request_core_cancellation(
                run, live, token=token, drain=True,
            )
            if core_reconciliation.get("quiescent") is True:
                break

        core_quiescent = core_reconciliation.get("quiescent") is True
        event_phase = "cancelled" if core_quiescent else CANCEL_PENDING_TOOL_PHASE
        event_summary = (
            "运行已停止，当前操作已取消"
            if core_quiescent
            else "停止请求已记录，正在核对在途修改"
        )
        remaining_events: list[Any] = []
        cancelled_tools: dict[str, dict[str, Any]] = {}
        settled_mutations = {
            str(item) for item in core_reconciliation.get("settledMutations", [])
            if str(item).strip()
        }
        for attempt in range(CANCELLATION_RECONCILIATION_ATTEMPTS):
            settled_tools = self._settle_cancelled_tool_events(
                project_id,
                run.id,
                phase=event_phase,
                summary=event_summary,
                settled_mutations=settled_mutations,
            )
            for item in settled_tools:
                identity = str(item.get("toolCallId") or item.get("traceGroupId") or item.get("name") or "")
                if identity:
                    cancelled_tools[identity] = item
            remaining_events = self.store.unfinished_tool_events(run.id)
            if not remaining_events:
                break
            if attempt + 1 < CANCELLATION_RECONCILIATION_ATTEMPTS:
                await asyncio.sleep(CANCELLATION_RECONCILIATION_DELAY_SECONDS)

        quiescent = core_quiescent and not remaining_events
        if not quiescent:
            reason = CANCELLATION_PENDING_REASON
            updated = self.store.update_run(run.id, status=RUN_SUSPENDED, error=reason)
            self._publish_state(project_id, run.id)
            self.events.publish(project_id, run.id, "checkpoint", {
                "id": run.session_id,
                "revision": run.candidate_revision or run.baseline_revision,
            })
            self.events.publish(project_id, run.id, "status", {
                "status": RUN_SUSPENDED,
                "text": reason,
                "cancelledToolCount": len(cancelled_tools),
                "cancelledTools": list(cancelled_tools.values()),
            })
            public = self._run_public(updated, self.store.project_state(project_id))
            public["cancellation"] = {
                **core_reconciliation,
                "quiescent": False,
                "unresolvedToolEvents": len(remaining_events),
                "cancelledToolCount": len(cancelled_tools),
                "cancelledTools": list(cancelled_tools.values()),
            }
            return public

        updated = self.store.update_run(run.id, status=RUN_CANCELLED, error="用户已停止运行")
        self._publish_state(project_id, run.id)
        self.events.publish(project_id, run.id, "status", {
            "status": RUN_CANCELLED,
            "text": "已停止，已完成的候选内容保留",
            "cancelledToolCount": len(cancelled_tools),
            "cancelledTools": list(cancelled_tools.values()),
        })
        public = self._run_public(updated, self.store.project_state(project_id))
        public["cancellation"] = {
            **core_reconciliation,
            "quiescent": True,
            "unresolvedToolEvents": 0,
            "cancelledToolCount": len(cancelled_tools),
            "cancelledTools": list(cancelled_tools.values()),
        }
        return public
    async def _request_core_cancellation(
        self,
        run: RunRecord,
        live: LiveRun | None,
        *,
        token: str = "",
        drain: bool = False,
    ) -> dict[str, Any]:
        cancel_run = getattr(self._gateway, "cancel_run", None)
        if not callable(cancel_run):
            return {"quiescent": True, "compatibility": "legacy_core"}
        restore = getattr(self._gateway, "restore_run_credential", None)
        current_credential = getattr(self._gateway, "run_credential", None)
        if callable(restore) and run.run_credential:
            has_current_credential = (
                callable(current_credential) and current_credential(run.id)
            )
            if not has_current_credential:
                restore(run.id, run.run_credential)
        authorization = live.token if live else token
        try:
            try:
                result = await cancel_run(
                    project_id=run.project_id,
                    run_id=run.id,
                    token=authorization,
                    drain=drain,
                )
            except TypeError as error:
                if "drain" not in str(error):
                    raise
                result = await cancel_run(
                    project_id=run.project_id,
                    run_id=run.id,
                    token=authorization,
                )
        except Exception as error:  # noqa: BLE001 - cancellation must return a resumable state
            logger.warning("Could not reconcile Core cancellation for run %s: %s", run.id, error)
            return {
                "quiescent": False,
                "error": "CORE_CANCELLATION_UNAVAILABLE",
                "reason": "建模核心停止回执暂时不可用",
            }
        return result if isinstance(result, dict) else {"quiescent": False}

    def _settle_cancelled_tool_events(
        self,
        project_id: str,
        run_id: str,
        *,
        phase: str = "cancelled",
        summary: str = "运行已停止，当前操作已取消",
        settled_mutations: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if phase not in TERMINAL_TOOL_PHASES and phase != CANCEL_PENDING_TOOL_PHASE:
            phase = "cancelled"
        cancelled_tools: list[dict[str, Any]] = []
        cancelled_at = now_iso()
        for event in self.store.unfinished_tool_events(run_id):
            started = event.payload
            mutation_id = str(started.get("mutationId") or "").strip()
            event_phase = phase
            event_summary = summary
            mutation_was_settled = False
            if mutation_id and settled_mutations:
                mutation_was_settled = mutation_id in settled_mutations
            is_stage_change_set = str(started.get("name") or "") == "stage_change_set"
            if mutation_was_settled and is_stage_change_set:
                event_phase = "completed"
                event_summary = "已核对在途修改回执，候选内容已保留"
            if str(started.get("phase") or "") == event_phase:
                continue
            payload = {
                "name": str(started.get("name") or ""),
                "phase": event_phase,
                "summary": event_summary,
                "toolCallId": str(started.get("toolCallId") or ""),
                "cancelledAt": cancelled_at,
            }
            if mutation_id:
                payload["mutationId"] = mutation_id
            trace_group_id = str(started.get("traceGroupId") or "")
            if trace_group_id:
                payload["traceGroupId"] = trace_group_id
            cancelled_tools.append({
                "name": payload["name"],
                "toolCallId": payload["toolCallId"],
                "traceGroupId": trace_group_id,
                "startedAt": event.created_at,
                "cancelledAt": cancelled_at,
                "phase": event_phase,
                "summary": event_summary,
            })
            self.events.publish(project_id, run_id, "tool", payload)
        return cancelled_tools

    async def safe_terminate_stale(self, *, project_id: str) -> dict[str, Any]:
        state = self.store.project_state(project_id)
        terminated_run_id = state.run_id
        terminated = self.store.terminate_stale_occupancy(project_id)
        self._publish_state(project_id, terminated_run_id)
        self.events.publish(
            project_id,
            terminated_run_id,
            "status",
            {"status": RUN_CANCELLED, "text": "已安全终止失效运行，候选内容与审计记录保留，可以重新规划"},
        )
        return {
            "run": None,
            "state": terminated.public(),
            "terminatedStale": True,
            "terminatedRunId": terminated_run_id,
        }

    def _settle_tool_events(
        self,
        project_id: str,
        run_id: str,
        *,
        phase: str,
        summary: str,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        for event in self.store.unfinished_tool_events(run_id):
            started = event.payload
            payload = {
                "name": str(started.get("name") or ""),
                "phase": phase,
                "summary": summary,
                "toolCallId": str(started.get("toolCallId") or ""),
            }
            if code is not None:
                payload["code"] = code
            if retryable is not None:
                payload["retryable"] = retryable
            mutation_id = str(started.get("mutationId") or "").strip()
            if mutation_id:
                payload["mutationId"] = mutation_id
            trace_group_id = str(started.get("traceGroupId") or "")
            if trace_group_id:
                payload["traceGroupId"] = trace_group_id
            self.events.publish(project_id, run_id, "tool", payload)

    def _settle_failed_tool_events(self, project_id: str, run_id: str, summary: str) -> None:
        self._settle_tool_events(
            project_id,
            run_id,
            phase="failed",
            summary=summary or "本次操作未完成，已保留当前成果",
            code="RUN_FAILED",
            retryable=False,
        )

    def _persist_latest_interaction(
        self,
        run: RunRecord,
        *,
        interaction: InteractionRecord,
        answer: dict[str, Any],
        continuation: str = "",
        action_key: str = "",
        requires_artifact: bool | None = None,
    ) -> None:
        """持久化最近一次交互的业务事实，供空正文紧凑摘要恢复使用。

        快照包含最新交互问题、答案、操作 key、续跑提示与脚本契约摘要；紧凑摘要
        只能基于这些最新事实生成，避免恢复摘要回退到最初 run.prompt 或旧计划。
        """
        payload = interaction.payload or {}
        question = str(payload.get("question") or "")[:4_000]
        contract_summary = str(
            payload.get("contractSummary")
            or payload.get("summary")
            or payload.get("question")
            or ""
        )[:4_000]
        answer_snapshot = {
            key: (value[:4_000] if isinstance(value, str) else value)
            for key, value in dict(answer).items()
        }
        required_text_constraint = _required_text_constraint(interaction, answer)
        if required_text_constraint:
            # Store a machine-readable receipt alongside the raw answer so
            # recovery can distinguish an acknowledged constraint from a
            # merely echoed sentence.
            acknowledged, missing_constraints = _required_text_constraint_status(
                str(answer.get("text") or ""), required_text_constraint,
            )
            answer_snapshot["constraintStatus"] = "acknowledged" if acknowledged else "unconfirmed"
            answer_snapshot["missingConstraintItems"] = missing_constraints
        snapshot = {
            "interactionId": interaction.id,
            "kind": interaction.kind,
            "question": question,
            "answer": answer_snapshot,
            "actionKey": (
                action_key
                or str(answer.get("action") or answer.get("value") or "")
            )[:200],
            "continuation": str(continuation or "")[:2_000],
            "contractSummary": contract_summary,
            "operation": str(payload.get("operation") or "")[:500],
            **({"requiredText": True} if required_text_constraint else {}),
            **({"requiredTextConstraint": required_text_constraint} if required_text_constraint else {}),
            "requiresArtifact": (
                _interaction_requires_artifact(interaction)
                if requires_artifact is None
                else bool(requires_artifact)
            ),
            "updatedAt": now_iso(),
        }
        self.store.update_run(run.id, latest_interaction=snapshot)

    def _script_artifact_produced(self, live: LiveRun, run: RunRecord) -> bool:
        """本轮是否已经形成脚本产物或下一张确认卡。

        专家批准契约后，重新定义一遍契约不算交付物：define_script_contract
        产出的正是被批准的那个对象本身。线上现场就是这样——确认后模型只是把
        契约又定义了一遍，随后几轮被思维链吃光预算、零产出，却因为这次调用
        被判成「本轮有产物」，Run 被结算成成功完成。
        """
        snapshot = run.latest_interaction or {}
        expected = SCRIPT_ARTIFACT_TOOLS
        if str(snapshot.get("actionKey") or "") in CONTRACT_ACCEPTED_ACTION_KEYS:
            expected = CONTRACT_ACCEPTED_ARTIFACT_TOOLS
        if live.observed_tools & expected:
            return True
        pending = self.store.pending_interaction(run.project_id)
        return pending is not None and pending.run_id == run.id

    def _plan_execution_boundary_reached(self, live: LiveRun, run: RunRecord, output: str) -> bool:
        if live.observed_tools & {"stage_change_set", "validate_change_set", "request_confirmation"}:
            return True
        if claims_confirmation_card(output):
            return True
        pending = self.store.pending_interaction(run.project_id)
        return pending is not None and pending.run_id == run.id

    def _progress_only_continuation_prompt(self, project_id: str) -> str:
        state = json.dumps(self.project_state(project_id)["state"], ensure_ascii=False)[:12_000]
        return (
            "你刚才只输出了执行进度，尚未形成候选修订、校验回执或确认卡。"
            "不要再次只描述下一步；立即调用 get_project_state 读取最新状态，"
            "再按已批准计划调用 stage_change_set 与 validate_change_set，"
            "完整校验通过后调用 request_confirmation 生成统一确认卡。\n\n"
            "# 项目唯一 Agent 状态快照\n"
            f"{state}"
        )

    def _suspend_required_text_review(
        self,
        live: LiveRun,
        run: RunRecord,
        constraint: dict[str, Any],
    ) -> None:
        """模型两次都未承接 requiredText 时，落一张可操作复核卡。

        这条出口不修改知识包，也不把“之后再明确”当作完成；用户可以直接
        让助手按原说明重试，或在卡片中替换补充说明。
        """
        _, missing_constraints = _required_text_constraint_status("", constraint)
        interaction_id = f"interaction_{uuid.uuid4().hex}"
        interaction = InteractionRecord(
            id=interaction_id,
            project_id=run.project_id,
            run_id=run.id,
            tool_call_id=f"required-text-review:{run.id}",
            kind="clarification",
            payload={
                "question": "助手还没有明确响应这条补充说明，请选择后继续",
                "options": [
                    {
                        "label": "按原补充说明继续",
                        "description": "重新核对并逐项响应刚才提交的要求",
                        "value": "retry_required_text",
                    },
                    {
                        "label": "调整补充说明",
                        "description": "用新的说明替换刚才的约束",
                        "value": "edit_required_text",
                        "requiresText": True,
                        "textPrompt": "请填写新的业务约束",
                    },
                ],
                "allowFreeText": True,
                "requiredTextConstraint": constraint,
                "missingConstraintItems": missing_constraints,
                "constraintStatus": "unconfirmed",
            },
        )
        self.store.create_interaction(interaction)
        self.store.update_run(run.id, status=RUN_WAITING, result="", error="")
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "checkpoint", {
            "id": run.session_id,
            "revision": run.candidate_revision or run.baseline_revision,
        })
        self.events.publish(run.project_id, run.id, "interaction", interaction.public())
        self.events.publish(run.project_id, run.id, "status", {
            "status": RUN_WAITING,
            "text": "等待你确认补充说明后继续",
        })

    def _suspend_model_truncation(self, live: LiveRun, run: RunRecord) -> None:
        """连续截断后落一张可操作的复核卡，而不是悄悄结算成功。

        截断意味着本轮完全没有产出，既不是完成也不是失败。原先它会沿着自主
        续跑分支耗完停滞预算、再被结算成「成功但没话说」——页面上表现为待办
        消失、没有新卡也没有报错。这条出口保留已有成果，把下一步交还给用户。
        """
        issue_id = f"model-truncation-{run.id}"
        state = self.store.project_state(run.project_id).public()
        recovery_actions = [
            {
                # 复用通用的「从最新进展重试」动作 id：恢复入口据此重置重试计数。
                "id": "retry_from_latest",
                "label": "拆小步骤继续",
                "description": "从当前进展继续，一次只推进一小步。",
            },
            {
                "id": "keep_current_progress",
                "label": "保留当前成果，结束本轮",
                "description": "保留已经形成的内容，暂不继续本轮处理。",
            },
        ]
        recovery = {
            "issueId": issue_id,
            "strategy": MODEL_TRUNCATION_STRATEGY,
            "actionId": "retry_from_latest",
            "recoveryActions": recovery_actions,
            "projectPhase": str(state.get("phase") or ""),
            "toolProfile": run.tool_profile,
            "attempt": MAX_TRUNCATION_RETRIES,
            "maxAttempts": MAX_TRUNCATION_RETRIES,
            "exhausted": True,
        }
        live.technical_recoveries[issue_id] = recovery
        self.store.save_technical_recovery(run.id, issue_id, recovery)
        self.store.update_run(run.id, status=RUN_SUSPENDED, result="", error="")
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "tool", {
            "name": "model_output_truncated",
            "phase": "waiting",
            "summary": "助手连续几轮都没能在预算内给出结论，已有成果已保留",
            "code": "MODEL_OUTPUT_TRUNCATED",
            "issueId": issue_id,
            "issueStatus": "awaiting_user",
            "problemVisibility": "recovery",
            "projectPhase": recovery["projectPhase"],
            "toolProfile": recovery["toolProfile"],
            "recoveryActions": recovery_actions,
        })
        self.events.publish(run.project_id, run.id, "checkpoint", {
            "id": run.session_id,
            "revision": run.candidate_revision or run.baseline_revision,
        })
        self.events.publish(run.project_id, run.id, "status", {
            "status": RUN_SUSPENDED,
            "text": "助手这一步没能得出结论，已有成果已保留，请选择后续处理",
        })

    def _suspend_required_tool_review(
        self,
        live: LiveRun,
        run: RunRecord,
        missing_tools: set[str],
    ) -> None:
        """Expose an actionable, diagnostic card instead of a generic stop."""
        issue_id = f"required-tool-review-{run.id}"
        constraint = live.required_text_constraint or {}
        # 全部缺失工具都真实调用过仍被拒绝时，标注出来供专家判断是否
        # 「取消该要求」；服务端不据此单方面撤销（失败不等于已完成）。
        attempted_but_rejected = sorted(_unsatisfiable_required_tools(live, missing_tools))
        recovery = {
            "issueId": issue_id,
            "requiredTool": sorted(missing_tools)[0] if missing_tools else "",
            "requiredTools": sorted(missing_tools),
            "missingRequiredTools": sorted(missing_tools),
            "actionId": "retry_required_tools",
            "recoveryActions": [
                {
                    "id": "retry_required_tools",
                    "label": "从当前进度继续",
                    "description": "从最后已保存的步骤重试缺失工具，不生成正式规则候选。",
                },
                {
                    "id": "cancel_required_tools",
                    "label": "取消该要求",
                    "description": "撤销这项必需步骤并从当前进度继续本轮，已有成果保持不变。",
                },
                {
                    "id": "keep_current_progress",
                    "label": "保留成果并停止",
                    "description": "保留当前成果并结束本轮，不产生新的规则、指标或操作候选。",
                },
            ],
            "strategy": "required_tools",
            "ledgerId": live.situation.product_ledger_id or None,
            "ledgerStatus": live.situation.product_ledger_status or "unknown",
            "lastSuccessfulBatch": live.situation.product_ledger_last_batch,
            "processed": live.situation.product_ledger_processed,
            "remaining": live.situation.product_ledger_remaining,
            "analysisRevision": live.situation.product_ledger_analysis_revision or None,
            "failureReason": (
                "必需工具已多次真实调用但均被当前项目状态拒绝，可能无法在当前状态下满足"
                if attempted_but_rejected else "必需工具在重试后仍未完成"
            ),
            "attemptedButRejected": attempted_but_rejected,
            "constraintStatus": "unconfirmed" if constraint else "not_applicable",
            "missingConstraintItems": (
                _required_text_constraint_status("", constraint)[1] if constraint else []
            ),
            "allowedActions": [
                "retry_required_tools", "cancel_required_tools", "keep_current_progress",
            ],
            "constraintHash": constraint.get("textHash"),
        }
        live.technical_recoveries[issue_id] = recovery
        self.store.save_technical_recovery(run.id, issue_id, recovery)
        interaction = InteractionRecord(
            id=f"interaction_{uuid.uuid4().hex}",
            project_id=run.project_id,
            run_id=run.id,
            tool_call_id=f"required-tool-review:{run.id}",
            kind="clarification",
            payload={
                "question": "本轮必需步骤尚未完成，请选择恢复方式",
                "summary": "必需工具未完成",
                "options": [
                    {
                        "label": "从当前进度继续",
                        "description": "从最后已保存的步骤重试缺失工具",
                        "value": "retry_required_tools",
                    },
                    {
                        "label": "取消该要求",
                        "description": "撤销这项必需步骤并从当前进度继续",
                        "value": "cancel_required_tools",
                    },
                    {
                        "label": "保留成果并停止",
                        "description": "不产生新的规则、指标或操作候选",
                        "value": "keep_current_progress",
                    },
                ],
                "allowFreeText": False,
                "recovery": recovery,
                "missingRequiredTools": recovery["missingRequiredTools"],
                "ledgerId": recovery["ledgerId"],
                "ledgerStatus": recovery["ledgerStatus"],
                "processed": recovery["processed"],
                "remaining": recovery["remaining"],
                "analysisRevision": recovery["analysisRevision"],
                "failureReason": recovery["failureReason"],
                "attemptedButRejected": recovery["attemptedButRejected"],
                "constraintStatus": recovery["constraintStatus"],
                "missingConstraintItems": recovery["missingConstraintItems"],
            },
        )
        self.store.create_interaction(interaction)
        self.store.update_run(run.id, status=RUN_WAITING, result="", error="")
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "checkpoint", {
            "id": run.session_id,
            "revision": run.candidate_revision or run.baseline_revision,
        })
        self.events.publish(run.project_id, run.id, "interaction", interaction.public())
        self.events.publish(run.project_id, run.id, "status", {
            "status": RUN_WAITING,
            "text": "必需步骤未完成，等待你选择恢复方式",
        })

    def _suspend_confirmation_review(self, live: LiveRun, run: RunRecord) -> None:
        """把未形成确认卡的终态转换为可恢复的用户选择。"""
        issue_id = f"confirmation-review-{run.id}"
        snapshot = run.latest_interaction or {}
        state = self.store.project_state(run.project_id).public()
        recovery = {
            "issueId": issue_id,
            "requiredTool": CONFIRMATION_REVIEW_REQUIRED_TOOL,
            "strategy": CONFIRMATION_REVIEW_STRATEGY,
            "actionId": "retry_from_latest",
            "recoveryActions": _confirmation_review_actions(),
            "interactionId": str(snapshot.get("interactionId") or ""),
            "projectPhase": str(state.get("phase") or ""),
            "toolProfile": run.tool_profile,
            "attempt": 0,
            "maxAttempts": 1,
            "exhausted": True,
        }
        live.technical_recoveries[issue_id] = recovery
        self.store.save_technical_recovery(run.id, issue_id, recovery)
        self.store.update_run(
            run.id,
            status=RUN_SUSPENDED,
            result="",
            error="",
        )
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "tool", {
            "name": "confirmation_review",
            "phase": "waiting",
            "summary": "整体复核卡未形成，待确认修改已保留",
            "code": "CONFIRMATION_CARD_NOT_CREATED",
            "issueId": issue_id,
            "issueStatus": "awaiting_user",
            "problemVisibility": "recovery",
            "projectPhase": recovery["projectPhase"],
            "toolProfile": recovery["toolProfile"],
            "recoveryActions": _confirmation_review_actions(),
        })
        self.events.publish(run.project_id, run.id, "checkpoint", {
            "id": run.session_id,
            "revision": run.candidate_revision or run.baseline_revision,
        })
        self.events.publish(run.project_id, run.id, "status", {
            "status": RUN_SUSPENDED,
            "text": "整体复核卡未形成，待确认修改已保留，请选择后续处理",
        })

    def _suspend_technical_recovery(
        self,
        live: LiveRun,
        run: RunRecord,
        issue_id: str,
    ) -> None:
        """把未完成的系统恢复转成用户可操作的选择。

        自动恢复用尽后，整轮不能只留下一句「已停止」。界面需要 issueId、
        问题码和恢复动作才能渲染出可点的出口，否则用户只能被动等待。
        """
        recovery = live.technical_recoveries.get(issue_id) or {}
        actions = [
            dict(item) for item in recovery.get("recoveryActions") or []
            if isinstance(item, dict)
        ] or _confirmation_review_actions()
        try:
            attempt = max(1, int(recovery.get("maxAttempts") or 1))
        except (TypeError, ValueError):
            attempt = 1
        updated = {
            **recovery,
            "actionId": str(recovery.get("actionId") or "retry_from_latest"),
            "recoveryActions": actions,
            "attempt": attempt,
            "exhausted": True,
        }
        live.technical_recoveries[issue_id] = updated
        self.store.save_technical_recovery(run.id, issue_id, updated)
        live.required_tools.discard(str(recovery.get("requiredTool") or "").strip())
        summary = str(
            recovery.get("stoppedSummary")
            or "这一步还没有完成，已保留当前成果，请选择后续处理"
        )
        # 未满足项是用户唯一能据以行动的信息。卡片上只写「已停止」，用户仍然不知道
        # 差什么，也就还是只能等平台。
        blockers = _string_list(recovery.get("blockers"), 10)
        self.store.update_run(run.id, status=RUN_SUSPENDED, result="", error="")
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "tool", {
            "name": str(recovery.get("sourceTool") or "issue_recovery"),
            "phase": "waiting",
            "summary": summary,
            "code": str(recovery.get("code") or "RECOVERY_NOT_COMPLETED"),
            "issueId": issue_id,
            "issueStatus": "awaiting_user",
            "problemVisibility": "recovery",
            "recoveryActions": actions,
            **({"blockers": blockers} if blockers else {}),
        })
        self.events.publish(run.project_id, run.id, "checkpoint", {
            "id": run.session_id,
            "revision": run.candidate_revision or run.baseline_revision,
        })
        self.events.publish(run.project_id, run.id, "status", {
            "status": RUN_SUSPENDED,
            "text": summary,
        })

    @staticmethod
    def _confirmation_review_prompt(run: RunRecord) -> str:
        snapshot = run.latest_interaction or {}
        facts = {
            "question": str(snapshot.get("question") or ""),
            "answer": snapshot.get("answer") or {},
            "continuation": str(snapshot.get("continuation") or ""),
            "contractSummary": str(snapshot.get("contractSummary") or ""),
            "operation": str(snapshot.get("operation") or ""),
        }
        return (
            "页面请求重新复核上一轮未形成的整体确认。不要把这当成新的用户业务问题，"
            "也不要询问用户平台是否开放某项工具能力。先调用 get_project_state 获取最新状态，"
            "再读取当前领域资源清单和相关资源快照，核对真实候选差异；如果候选完整，"
            "立即调用 request_confirmation 生成整体确认卡，不能只输出文字。"
            "只能依据当前项目事实和下面保存的最新交互继续，不得原样重放旧参数。\n"
            f"最新交互事实：{json.dumps(facts, ensure_ascii=False)}"
        )

    async def _resume(
        self,
        run: RunRecord,
        token: str,
        decisions: dict[str, Any],
        *,
        steering: str = "",
        restore_interaction: InteractionRecord | None = None,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_declined: bool = False,
        release_reentry_authorized: bool = False,
        confirmation_required: bool = False,
        preserve_token_fingerprint: bool = False,
        autonomous_seed: bool = False,
        required_text_constraint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._execution(run.project_id, run.id) not in {RUN_WAITING, RUN_SUSPENDED}:
            raise AgentRuntimeError("RUN_NOT_WAITING", "当前运行不在等待恢复状态", status=409)
        await self._release_core_cancellation_fence(run, token)
        claimed = await self._claim_workspace(project_id=run.project_id, run_id=run.id, token=token)
        from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

        resume = InteractiveInput()
        for tool_call_id, value in decisions.items():
            resume.update(tool_call_id, value)
        updates: dict[str, Any] = {
            "status": RUN_QUEUED,
            "interaction_id": "",
            "error": "",
            "_allow_status_regression": True,
        }
        if not preserve_token_fingerprint:
            updates["token_fingerprint"] = token_fingerprint(token)
        self.store.update_run(run.id, **updates)
        self._publish_state(run.project_id, run.id)
        technical_recoveries = self.store.technical_recoveries(run.id)
        resolved_required_text_constraint = (
            required_text_constraint
            or _required_text_constraint_from_snapshot(run.latest_interaction)
        )
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            tool_profile=run.tool_profile,
            observed_tools=self.store.completed_tool_names(run.id),
            restore_interaction=restore_interaction,
            required_tools=set(required_tools or ()) | {
                str(item.get("requiredTool") or "")
                for item in technical_recoveries.values()
                if str(item.get("requiredTool") or "").strip()
            },
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=release_reentry_authorized,
            release_declined=release_declined,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            project_goal=str(claimed.get("projectGoal") or ""),
            validation_repair=self._validation_repair_state(run.id),
            technical_recoveries=technical_recoveries,
            required_text_constraint=resolved_required_text_constraint,
        )
        if steering:
            await live.steering.put(steering)
        self._restore_product_ledger_situation(live)
        self._live[run.id] = live
        live.task = asyncio.create_task(self._execute(live, resume))
        updated = self.store.require_run(run.id)
        return self._run_public(updated, self.store.project_state(run.project_id))

    async def _release_core_cancellation_fence(self, run: RunRecord, token: str) -> None:
        resume_run = getattr(self._gateway, "resume_run", None)
        if not callable(resume_run):
            return
        result = await resume_run(
            project_id=run.project_id,
            run_id=run.id,
            token=token,
        )
        if isinstance(result, dict) and result.get("quiescent") is False:
            raise AgentRuntimeError(
                "RUN_CANCELLATION_PENDING",
                "仍有在途修改没有取得明确回执，暂不能继续运行",
                status=409,
                details=result,
            )
        if isinstance(result, dict) and result.get("requested"):
            settled_mutations = {
                str(item) for item in result.get("settledMutations", [])
                if str(item).strip()
            }
            self._settle_cancelled_tool_events(
                run.project_id,
                run.id,
                phase="cancelled",
                summary="停止请求已结算，未完成操作已取消",
                settled_mutations=settled_mutations,
            )

    async def _continue(
        self,
        run: RunRecord,
        token: str,
        prompt: str,
        *,
        required_tools: set[str] | None = None,
        required_material_directives: set[str] | None = None,
        allowed_tools: frozenset[str] | None = None,
        release_reentry_authorized: bool = False,
        confirmation_required: bool = False,
        autonomous_seed: bool = False,
        required_text_constraint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._execution(run.project_id, run.id) not in {RUN_WAITING, RUN_SUSPENDED}:
            raise AgentRuntimeError("RUN_NOT_WAITING", "当前运行不在等待恢复状态", status=409)
        await self._release_core_cancellation_fence(run, token)
        claimed = await self._claim_workspace(project_id=run.project_id, run_id=run.id, token=token)
        self.store.update_run(
            run.id,
            status=RUN_QUEUED,
            interaction_id="",
            token_fingerprint=token_fingerprint(token),
            error="",
            _allow_status_regression=True,
        )
        self._publish_state(run.project_id, run.id)
        technical_recoveries = self.store.technical_recoveries(run.id)
        resolved_required_text_constraint = (
            required_text_constraint
            or _required_text_constraint_from_snapshot(run.latest_interaction)
        )
        live = LiveRun(
            run_id=run.id,
            project_id=run.project_id,
            token=token,
            tool_profile=run.tool_profile,
            observed_tools=self.store.completed_tool_names(run.id),
            required_tools=set(required_tools or ()) | {
                str(item.get("requiredTool") or "")
                for item in technical_recoveries.values()
                if str(item.get("requiredTool") or "").strip()
            },
            required_material_directives=set(required_material_directives or ()),
            allowed_tools=allowed_tools,
            release_reentry_authorized=release_reentry_authorized,
            confirmation_required=confirmation_required,
            autonomous_seed=autonomous_seed,
            package_contract=dict(claimed.get("packageContract") or {}),
            project_goal=str(claimed.get("projectGoal") or ""),
            validation_repair=self._validation_repair_state(run.id),
            technical_recoveries=technical_recoveries,
            required_text_constraint=resolved_required_text_constraint,
        )
        self._restore_product_ledger_situation(live)
        self._live[run.id] = live
        live.task = asyncio.create_task(self._execute(live, prompt))
        updated = self.store.require_run(run.id)
        return self._run_public(updated, self.store.project_state(run.project_id))

    async def _claim_workspace(
        self,
        *,
        project_id: str,
        run_id: str,
        token: str,
        resume_plan_id: str = "",
    ) -> dict[str, Any]:
        current_credential = getattr(self._gateway, "run_credential", None)
        restore_credential = getattr(self._gateway, "restore_run_credential", None)
        if callable(current_credential) and callable(restore_credential) and not current_credential(run_id):
            persisted = self.store.require_run(run_id).run_credential
            if persisted:
                restore_credential(run_id, persisted)
        claimed = await self._gateway.call(
            project_id=project_id,
            run_id=run_id,
            token=token,
            tool="claim_candidate_workspace",
            arguments={"resume_plan_id": resume_plan_id} if resume_plan_id else {},
        )
        if claimed.get("error"):
            raise AgentRuntimeError(
                str(claimed.get("error")),
                str(claimed.get("reason") or "候选工作区接续失败"),
                status=409,
                details={"next": claimed.get("next") or []},
            )
        package_contract = claimed.get("packageContract")
        if not isinstance(package_contract, dict) or not str(package_contract.get("validatorVersion") or ""):
            raise AgentRuntimeError(
                "PACKAGE_CONTRACT_UNAVAILABLE",
                "核心服务没有提供带版本的知识包生成契约，Agent 已停止生成候选",
                status=503,
                details={"next": ["同步部署核心服务与 Agent 后从当前进度重试"]},
            )
        credential = getattr(self._gateway, "run_credential", None)
        if callable(credential):
            issued = str(credential(run_id) or "")
            if issued:
                self.store.update_run(run_id, run_credential=issued)
        return claimed

    def _restore_product_ledger_situation(self, live: LiveRun) -> None:
        """重放台账工具回执，把已完成的批次进度带回本轮快照。

        恢复时 observed_tools 只重建"调用过哪些工具"，业务结果只存在于回执里。
        不重放的话，续跑的诊断回执会把已落库的批次报成 unknown / 0 条，
        让用户以为进度丢了，从而重头再来。
        """
        for _name, result in self.store.tool_receipts_by_name(live.run_id, PRODUCT_LEDGER_STATE_TOOLS):
            _absorb_product_ledger(live, result)

    def _validation_repair_state(self, run_id: str) -> CandidateValidationRepairState:
        return CandidateValidationRepairState.from_snapshot(
            self.store.validation_repair_state(run_id),
            on_change=lambda snapshot: self.store.save_validation_repair_state(
                run_id, snapshot,
            ),
        )

    async def _execute(self, live: LiveRun, query: Any) -> None:
        from openjiuwen.core.runner.runner import Runner

        run = self.store.require_run(live.run_id)
        try:
            initial_phase = self.store.project_state(run.project_id).state.partition(":")[0]
            output_budget = output_budget_for_phase(self.settings, initial_phase)
            analyst_model = None
            model_settings = self.settings
            acceptance_model = False
            if self._model_factory is not None:
                model = self._model_factory()
            elif len(self.settings.service_token) < 32:
                model = build_model(self.settings, output_tokens=output_budget)
                analyst_model = build_model(
                    self.settings,
                    output_tokens=self.settings.analyst_output_tokens,
                )
            else:
                runtime_llm = await self._gateway.model_config(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                )
                if runtime_llm.get("acceptanceDriver") is True:
                    from .acceptance_model import AcceptanceModel
                    model = AcceptanceModel(str(runtime_llm.get("acceptanceScenario") or ""))
                    acceptance_model = True
                else:
                    runtime_settings = self.settings.with_runtime_llm(runtime_llm)
                    model_settings = runtime_settings
                    model = build_model(runtime_settings, output_tokens=output_budget)
                    analyst_model = build_model(
                        runtime_settings,
                        output_tokens=runtime_settings.analyst_output_tokens,
                    )
            if live.cancel_requested or self._execution(run.project_id, run.id) in {
                RUN_STOPPING, RUN_CANCELLED,
            }:
                return
            running = self.store.update_run(run.id, status=RUN_RUNNING)
            if running.status != RUN_RUNNING or live.cancel_requested:
                return
            self._publish_state(run.project_id, run.id)
            live.model_settings = model_settings
            if not live.cancel_requested:
                self.events.publish(run.project_id, run.id, "status", {"status": RUN_RUNNING, "text": "助手正在建模"})
            autonomous = live.autonomous_seed
            stalled = 0
            continuations = 0
            decision_card_retries = 0
            confirmation_card_retries = 0
            truncation_retries = 0
            progress_only_retries = 0
            model_operation_seq = 0
            current_query = query
            active_phase = initial_phase
            while True:
                def emit(kind: str, payload: dict[str, Any]) -> None:
                    phase = str(payload.get("phase") or "")
                    # Cancellation is a monotonic fence. Terminal receipts are
                    # still allowed through for reconciliation, but a late
                    # progress/started callback must not resurrect the run.
                    persisted_execution = self._execution(live.project_id, live.run_id)
                    cancelling = live.cancel_requested or persisted_execution in {
                        RUN_STOPPING, RUN_CANCELLED,
                    }
                    if cancelling:
                        if kind == "progress":
                            return
                        # Once cancellation has been requested, only an
                        # explicit receipt produced by the cancellation
                        # reconciler may be shown.  In particular, a late
                        # Core error such as RUN_CANCELLING is activity, not a
                        # user-recovery problem.
                        if kind == "tool":
                            return
                        if kind == "status" and str(payload.get("status") or "") in {
                            RUN_QUEUED, RUN_RUNNING, RUN_STOPPING, RUN_WAITING,
                        }:
                            return
                        if kind in {"message", "message_delta", "reasoning_delta", "reasoning_done"}:
                            return
                    if kind == "model_call":
                        # ActivityRail 的度量事件走的是同一个 emit。主模型调用完成时
                        # 留存最后一次度量，供截断守卫判断本轮是不是零产出。
                        if (
                            payload.get("phase") == "completed"
                            and payload.get("actor") == "modeling_leader"
                        ):
                            live.last_model_call = dict(payload)
                    if kind == "tool":
                        code = str(payload.get("code") or "")
                        if code in {"RUN_CANCELLING", "RUN_CANCELLED"}:
                            payload = {
                                **payload,
                                "phase": "cancelled",
                                "summary": "运行已停止，当前操作已取消",
                                "problemVisibility": "activity",
                                "retryable": False,
                                "repairStopped": False,
                                "issueId": "",
                                "issueStatus": "cancelled",
                                "recoveryActions": [],
                            }
                            phase = "cancelled"
                        name = str(payload.get("name") or "")
                        if name == "run_evaluation" and phase == "started":
                            live.evaluation_launch_active = True
                        if phase in {"completed", "failed", "repairing", "blocked", "rejected"}:
                            live.attempted_tools.add(name)
                            summary = sanitize_visible_text(str(payload.get("summary") or "")).strip()
                            if summary:
                                live.tool_summaries.append(
                                    f"{name} / {phase}: {summary[:300]}"
                                )
                                del live.tool_summaries[:-MAX_TOOL_SUMMARIES]
                        if phase == "completed":
                            live.observed_tools.add(name)
                    self.events.publish(live.project_id, live.run_id, kind, payload)

                def save_recovery(issue_id: str, recovery: dict[str, Any]) -> None:
                    normalized = str(issue_id or "").strip()
                    if not normalized:
                        return
                    live.technical_recoveries[normalized] = dict(recovery)
                    self.store.save_technical_recovery(
                        live.run_id, normalized, dict(recovery),
                    )

                def require_tool(name: str, recovery: dict[str, Any]) -> None:
                    normalized = str(name or "").strip()
                    if not normalized:
                        return
                    live.required_tools.add(normalized)
                    save_recovery(str(recovery.get("issueId") or ""), recovery)

                context = ToolContext(
                    project_id=live.project_id,
                    run_id=live.run_id,
                    token=live.token,
                    gateway=self._gateway,
                    emit=emit,
                    phase=self.store.project_state(run.project_id).state.partition(":")[0],
                    tool_profile=live.tool_profile,
                    required_tools=frozenset(live.required_tools),
                    require_tool=require_tool,
                    release_required_tool=lambda name: live.required_tools.discard(
                        str(name or "").strip(),
                    ),
                    resolve_issues=lambda issue_ids: self.store.resolve_technical_recoveries(
                        live.run_id, issue_ids,
                    ),
                    save_recovery=save_recovery,
                    technical_recoveries=live.technical_recoveries,
                    state_action=lambda name, arguments: self._state_action(live, name, arguments),
                    after_core_tool=lambda name, arguments, result: self._after_core_tool(
                        live, name, arguments, result,
                    ),
                    confirmation_guard=lambda: (
                        not live.confirmation_required
                        or not _missing_required_tools(live)
                    ),
                    scope_confirmation_gate=lambda: (
                        "analyze_deterministic_capabilities" in live.observed_tools
                    ),
                    tool_receipt_lookup=lambda name, call_id, arguments: self.store.tool_receipt(
                        live.run_id, call_id, name, arguments,
                    ),
                    tool_receipt_save=lambda name, call_id, arguments, result: self.store.save_tool_receipt(
                        live.run_id, call_id, name, arguments, result,
                    ),
                    stable_read_cache=live.stable_read_cache,
                    evaluation_cancel_authorized="cancel_evaluation" in live.required_tools,
                    allowed_tools=live.allowed_tools,
                    confirmation_required=live.confirmation_required,
                    release_declined=live.release_declined,
                    validation_repair=live.validation_repair,
                    package_contract=live.package_contract,
                    cancel_event=live.cancel_event,
                    inflight_core_calls=live.inflight_core_calls,
                )
                phase = context.phase
                if phase != active_phase and self._model_factory is None and not acceptance_model:
                    model = build_model(
                        model_settings,
                        output_tokens=output_budget_for_phase(self.settings, phase),
                    )
                    analyst_model = build_model(
                        model_settings,
                        output_tokens=model_settings.analyst_output_tokens,
                    )
                    active_phase = phase
                live.bundle = build_agent(
                    self.settings,
                    context,
                    model=model,
                    analyst_model=analyst_model,
                    package_contract=live.package_contract,
                    project_goal=live.project_goal,
                    # 用户刚下达指令或刚回答完问题时，指令本身就是本轮目标，
                    # 不需要服务端再指派。只有模型自己决定下一步时才需要——
                    # 那正是过去它漫无目的地反复取证的地方。
                    # 判据是「有没有待处理指令」而不只是「是不是续跑」：
                    # 未处置的专家插话和插话待办在任何一轮都构成本轮目标，
                    # 否则插话只能作为普通消息随窗口滚动而被静默丢弃。
                    turn_goal=await self._turn_goal(live) if (
                        continuations
                        or self.store.pending_interjections(live.project_id)
                        or self.store.open_interjection_tasks(live.project_id)
                    ) else "",
                    # 专家约束每轮从登记表重算注入，撤销即时生效；system 段
                    # 常驻保证它不被工具返回值挤出窗口。
                    constraints=self.store.active_constraints(live.project_id),
                )
                if live.cancel_requested:
                    return
                boundary = self.events.publish(
                    run.project_id,
                    run.id,
                    "status",
                    {"status": RUN_RUNNING, "text": "助手正在执行下一步" if continuations else "助手正在建模"},
                )
                model_operation_seq += 1
                live.last_model_call = {}
                result = await self._run_model_operation(
                    live,
                    run,
                    Runner,
                    current_query,
                    operation_id=f"model-stream-{model_operation_seq:04d}",
                )
                if live.cancel_requested or self._execution(run.project_id, run.id) in {
                    RUN_STOPPING, RUN_CANCELLED,
                }:
                    return
                # 截断守卫必须排在最前面：本轮模型一个 token 都没吐出来，
                # 后面所有「模型说了什么/做了什么」的判断都无从谈起。尤其不能
                # 让它掉进自主续跑分支——那里会把零产出当成停滞，扣完预算后
                # 直接结算成功，页面上就是待办消失、没有下文。
                if (
                    result.get("result_type") == "success"
                    and not str(result.get("output") or "").strip()
                    and _model_output_truncated(live.last_model_call)
                ):
                    metrics = live.last_model_call
                    logger.warning(
                        "model output truncated by reasoning run=%s attempt=%s "
                        "outputTokens=%s reasoningTokens=%s",
                        run.id,
                        truncation_retries + 1,
                        metrics.get("outputTokens"),
                        metrics.get("reasoningTokens"),
                    )
                    if truncation_retries < MAX_TRUNCATION_RETRIES:
                        truncation_retries += 1
                        self.events.publish(run.project_id, run.id, "tool", {
                            "name": "model_output_truncated",
                            "phase": "repairing",
                            "summary": "本轮推理占满了输出预算，正在换一种更小步的做法重试",
                            "code": "MODEL_OUTPUT_TRUNCATED",
                            "attempt": truncation_retries,
                            "maxAttempts": MAX_TRUNCATION_RETRIES,
                            "problemVisibility": "internal",
                        })
                        current_query = TRUNCATION_RECOVERY_PROMPT
                        continue
                    self._suspend_model_truncation(live, run)
                    return
                if _required_text_interrupt_requires_review(
                    result,
                    live,
                    self.settings.required_text_guard_mode,
                ):
                    if self.settings.required_text_guard_mode == "observe":
                        self.events.publish(run.project_id, run.id, "tool", {
                            "name": "required_text_guard",
                            "phase": "completed",
                            "summary": "模型重复了原问题卡（观测模式）",
                            "constraintHash": live.required_text_constraint.get("textHash"),
                        })
                    elif live.required_text_guard_retries < 1:
                        live.required_text_guard_retries += 1
                        current_query = _required_text_prompt(live.required_text_constraint)
                        continue
                    else:
                        self._suspend_required_text_review(
                            live,
                            run,
                            live.required_text_constraint,
                        )
                        return
                if context.technical_capability_claimed and result.get("result_type") == "interrupt":
                    self._suspend_confirmation_review(live, run)
                    return
                if context.pending_evaluation is not None:
                    result = await self._ensure_visible_result(live, run, {
                        "result_type": "success",
                        "output": EVALUATION_PENDING_MESSAGE,
                        "streamed": (
                            result.get("streamed") is True
                            and str(result.get("output") or "").strip() == EVALUATION_PENDING_MESSAGE
                        ),
                    })
                    self._settle(live, run, result)
                    return
                if context.recovery_waiting or result.get("recovery_wait") is True:
                    # 承载阶段被门禁挡住的写入已经没有自愈路径，只把本轮挂起会让用户
                    # 回到「没有任何可操作入口」的老问题；这里补一张带未满足项的恢复卡。
                    blocked_issue = next(
                        (
                            issue_id
                            for issue_id, recovery in live.technical_recoveries.items()
                            if _is_blocked_phase_capability(recovery)
                        ),
                        "",
                    )
                    if blocked_issue:
                        self._suspend_technical_recovery(live, run, blocked_issue)
                        return
                    self.store.update_run(
                        run.id,
                        status=RUN_SUSPENDED,
                        result="",
                        error="",
                    )
                    self._publish_state(run.project_id, run.id)
                    self.events.publish(run.project_id, run.id, "checkpoint", {
                        "id": run.session_id,
                        "revision": run.candidate_revision or run.baseline_revision,
                    })
                    self.events.publish(run.project_id, run.id, "status", {
                        "status": RUN_SUSPENDED,
                        "text": "已保留当前成果，等待你选择后续处理",
                    })
                    return
                if live.plan_approval_required:
                    output = str(result.get("output") or "").strip()
                    self.store.update_run(
                        run.id,
                        status=RUN_SUSPENDED,
                        result=output,
                        error="",
                    )
                    self._publish_state(run.project_id, run.id)
                    self.events.publish(run.project_id, run.id, "checkpoint", {
                        "id": run.session_id,
                        "revision": run.candidate_revision or run.baseline_revision,
                    })
                    self.events.publish(run.project_id, run.id, "status", {
                        "status": RUN_SUSPENDED,
                        "text": "等待你批准当前建模计划",
                    })
                    return
                if result.get("result_type") == "success" and context.technical_capability_claimed:
                    capability_key = "technical_capability_claimed"
                    used_capability_retries = live.proof_retries.get(capability_key, 0)
                    live.required_tools.add(CONFIRMATION_REVIEW_REQUIRED_TOOL)
                    if used_capability_retries < 2:
                        live.proof_retries[capability_key] = used_capability_retries + 1
                        current_query = (
                            "你刚才把内部工具能力问题转给了用户。不要解释平台状态，也不要询问用户是否开放工具；"
                            "立即调用 list_domain_resources 读取当前领域资源，再基于真实结果继续完成本轮复核。"
                        )
                        continue
                    self._suspend_confirmation_review(live, run)
                    return
                missing_tools = _missing_required_tools(live)
                if result.get("result_type") == "success" and missing_tools:
                    proof_key = "|".join(sorted(missing_tools))
                    used_proof_retries = live.proof_retries.get(proof_key, 0)
                    if used_proof_retries < 2:
                        live.proof_retries[proof_key] = used_proof_retries + 1
                        current_query = await self._required_tool_prompt(
                            live, run, Runner, missing_tools,
                        )
                        continue
                    if any(
                        recovery.get("strategy") == CONFIRMATION_REVIEW_STRATEGY
                        for recovery in live.technical_recoveries.values()
                    ):
                        self._suspend_confirmation_review(live, run)
                        return
                    # 阶段能力这类恢复自带承载阶段和重提指引，交给通用的必需工具卡会丢掉
                    # requiredPhase，用户点「继续」后模型就不知道该切回哪个阶段重提修改。
                    phase_capability_issue = next(
                        (
                            issue_id
                            for issue_id, recovery in live.technical_recoveries.items()
                            if recovery.get("strategy") == PHASE_CAPABILITY_STRATEGY
                        ),
                        "",
                    )
                    if phase_capability_issue:
                        self._suspend_technical_recovery(
                            live, run, phase_capability_issue,
                        )
                        return
                    self._suspend_required_tool_review(live, run, missing_tools)
                    return
                if _required_text_response_requires_review(
                    result,
                    live,
                    self.settings.required_text_guard_mode,
                ):
                    if self.settings.required_text_guard_mode == "observe":
                        self.events.publish(run.project_id, run.id, "tool", {
                            "name": "required_text_guard",
                            "phase": "completed",
                            "summary": "补充说明已记录，但模型正文没有明确回执（观测模式）",
                            "constraintHash": live.required_text_constraint.get("textHash"),
                        })
                    elif live.required_text_guard_retries < 1:
                        live.required_text_guard_retries += 1
                        current_query = _required_text_prompt(live.required_text_constraint)
                        continue
                    else:
                        self._suspend_required_text_review(
                            live,
                            run,
                            live.required_text_constraint,
                        )
                        return
                if result.get("result_type") == "success" and _purpose_review_suggestions_complete(live):
                    result = await self._ensure_visible_result(live, run, result)
                    self._settle(live, run, result)
                    return
                if (
                    result.get("result_type") == "success"
                    and context.confirmation_claimed_in_text
                    and not context.interjection_reply_required
                    and live.restore_interaction is None
                ):
                    live.confirmation_required = True
                if (
                    result.get("result_type") == "success"
                    and context.user_input_request_in_text
                    and not context.decision_card_requested
                    and not context.interjection_reply_required
                    and live.restore_interaction is None
                ):
                    if decision_card_retries < MAX_DECISION_CARD_RETRIES:
                        decision_card_retries += 1
                        current_query = (
                            "你刚才在普通文字里要求用户从业务选项中选择，但尚未生成可操作的问题卡。"
                            "立即调用 ask，把同一问题整理成少量互斥选项并保留自由输入；"
                            "不要改用 request_confirmation，也不要再次只输出文字选项。"
                            if context.decision_request_in_text else
                            "你刚才在普通文字里要求用户补充描述，但尚未生成可输入的问题卡。"
                            "立即调用 ask，把同一补充请求放入问题卡；可以不提供选项，并保留自由输入。"
                            "不要继续执行项目、转换阶段，也不要再次只用普通文字索要输入。"
                        )
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手没能把待用户补充的问题整理成可操作卡片，本轮已停止；已有内容保持不变。",
                        "streamed": False,
                    }
                if result.get("result_type") == "success" and live.confirmation_required:
                    if confirmation_card_retries < 2:
                        confirmation_card_retries += 1
                        current_query = (
                            "整体差异已经核对，但页面上尚未形成需要专家处理的真实确认卡。"
                            "立即发起整体确认；不得替专家批准，也不得只用文字询问。"
                        )
                        continue
                    self._suspend_confirmation_review(live, run)
                    return
                if (
                    result.get("result_type") == "success"
                    and _progress_only_final_text(str(result.get("output") or ""))
                    and not self._plan_execution_boundary_reached(
                        live, run, str(result.get("output") or ""),
                    )
                    and APPROVED_PLAN_REQUIRED_TOOLS <= live.required_tools
                ):
                    if progress_only_retries < MAX_PROGRESS_ONLY_CONTINUATIONS:
                        progress_only_retries += 1
                        current_query = self._progress_only_continuation_prompt(run.project_id)
                        continue
                    result = {
                        "result_type": "error",
                        "output": "助手只输出了执行进度，未形成候选修订、校验回执或确认卡；本轮已停止，已保留当前成果。",
                        "streamed": True,
                        "errorCode": "PROGRESS_ONLY_COMPLETION",
                    }
                if result.get("result_type") == "interrupt" or live.restore_interaction:
                    self._settle(live, run, result)
                    return

                phase = self.store.project_state(run.project_id).state.partition(":")[0]
                if phase == "release":
                    result = await self._ensure_visible_result(
                        live,
                        run,
                        result,
                    )
                    self._settle(live, run, result)
                    return

                progressed = self._made_autonomous_progress(run.project_id, boundary.seq)
                if progressed:
                    autonomous = True
                    stalled = 0
                elif autonomous:
                    stalled += 1

                # 已批准的计划还有必选任务时放宽停滞上限：只做调研不写入同样是没干活，
                # 但收尾的判据应该是计划做没做完，而不是模型连着几轮没调用写包工具。
                # 受控计划只存在于知识建模阶段；资料准备、目标确认、流程建模、
                # 确定性验证都没有计划，不能因此就用紧上限把它们判成没事可做。
                stall_budget = (
                    MAX_STALLED_PLAN_CONTINUATIONS
                    if (live.situation.plan_remaining or live.situation.stage_started_without_delivery)
                    else MAX_STALLED_CONTINUATIONS
                )
                # 打转和没产出是两回事：没产出可能只是在取证，打转才是真卡住。
                # 检测到打转直接收尾，不受上面放宽的停滞预算保护。
                loop_reason = _detect_loop(live)
                if loop_reason:
                    logger.warning(
                        "autonomous loop detected run=%s reason=%s", run.id, loop_reason,
                    )
                # 剩下的事只能由用户在别的界面完成时，继续轮转推不动任何进展，
                # 只会一轮轮重复同一段话。结束本轮，等外部事件把它唤醒。
                if live.situation.awaiting_user_action:
                    loop_reason = loop_reason or "剩余事项需要用户在其它界面完成"
                # 指派目标时就算出这道门禁没有助手能推的事。不收尾的话，下一轮
                # 仍然拿不到目标，模型又会退回「自己找活」——空转就是这么起来的。
                if live.situation.no_actionable_gate:
                    loop_reason = loop_reason or (
                        f"门禁 {live.situation.no_actionable_gate} 的剩余事项"
                        "需要用户或专家完成"
                    )
                # 强制表态：本轮结束时还有未处置的专家插话，不能就这样收尾——
                # 沉默必须是一种错误状态，不是默认状态。在预算内强制续一轮，
                # 让下一轮的指派目标带着处置要求重新出现；预算耗尽或确认打转时
                # 不再硬转（防止模型拒绝处置时无限循环），改为发一条可见事件，
                # 把「插话没被处置」摆到台面上。
                undisposed = self.store.pending_interjections(run.project_id)
                must_dispose = bool(undisposed)
                can_continue_autonomously = (autonomous or must_dispose) and not loop_reason
                has_continuation_budget = continuations < MAX_AUTONOMOUS_CONTINUATIONS
                has_stall_budget = stalled < stall_budget
                if can_continue_autonomously and has_continuation_budget and has_stall_budget:
                    continuations += 1
                    current_query = (
                        self._interjection_continuation_prompt(undisposed)
                        if must_dispose
                        else self._autonomous_continuation_prompt(
                            run.project_id, live, stalled=stalled,
                        )
                    )
                    continue
                if must_dispose:
                    self._publish_interjections_undisposed(run, undisposed)
                if live.situation.plan_remaining:
                    self._publish_plan_incomplete_activity(live, run, stalled=stalled)

                result = await self._ensure_visible_result(
                    live,
                    run,
                    result,
                )
                self._settle(live, run, result)
                return
        except asyncio.CancelledError:
            execution = self._execution(run.project_id, run.id)
            if self._shutting_down and not live.cancel_requested and execution != RUN_CANCELLED:
                self.store.update_run(
                    run.id,
                    status=RUN_SUSPENDED,
                    error="Agent 服务重启，已停在最近检查点",
                )
                self.events.publish(
                    run.project_id,
                    run.id,
                    "status",
                    {"status": RUN_SUSPENDED, "text": "服务正在重启，已保留当前进展"},
                )
                self._publish_state(run.project_id, run.id)
            elif not live.cancel_requested and execution not in {
                RUN_CANCELLED, RUN_STOPPING,
            }:
                self.store.update_run(run.id, status=RUN_CANCELLED, error="运行被停止")
                self._publish_state(run.project_id, run.id)
            raise
        except Exception as error:  # noqa: BLE001
            logger.exception("Agent run %s failed", run.id)
            if live.cancel_requested or self._execution(run.project_id, run.id) in {
                RUN_STOPPING, RUN_CANCELLED,
            }:
                return
            self.store.update_run(run.id, status=RUN_FAILED, error=f"{type(error).__name__}: {error}")
            self._publish_state(run.project_id, run.id)
            visible_error = (
                error.message
                if isinstance(error, AgentRuntimeError) and error.code in {"LLM_NOT_CONFIGURED", "AGENT_TOOL_LOOP_DETECTED"}
                else (
                    "模型上下文超过本轮可用范围；之前已经完成的处理和候选修改均已保留，请从当前进展重试。"
                    if _is_context_limit_exception(error)
                    else "模型响应等待超时；之前已经完成的处理和候选修改均已保留，请从当前进展重试。"
                    if _is_timeout_exception(error)
                    else "这次处理没有完成，之前已经确认的内容和候选修改都已保留。"
                )
            )
            self.events.publish(run.project_id, run.id, "message", {"role": "assistant", "blocks": [{"type": "error", "text": visible_error, "actions": [{"id": "retry", "label": "从当前进展重试"}]}]})
            if live.restore_interaction:
                self._restore_interaction(run, live.restore_interaction)
            else:
                self.events.publish(run.project_id, run.id, "status", {"status": RUN_FAILED, "text": "本次处理未完成", "error": visible_error})
        finally:
            self._live.pop(run.id, None)
            deferred = live.deferred_purpose_continuation
            run_status = self.store.require_run(run.id).status
            run_cancelled = run_status in {RUN_CANCELLED, RUN_STOPPING} or live.cancel_requested
            if deferred is not None and not self._shutting_down and not run_cancelled:
                steering_id, token, message = deferred
                try:
                    await self.start_run(
                        project_id=run.project_id,
                        token=token,
                        message=message,
                        publish_user_message=False,
                    )
                    self.store.mark_steering_delivered(steering_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Could not start post-purpose-review run for project %s",
                        run.project_id,
                    )
                    self.events.publish(run.project_id, run.id, "message", {
                        "role": "assistant",
                        "blocks": [{
                            "type": "error",
                            "text": "资料用途已经确认，但后续建模轮次未能启动，请从当前进展重试。",
                        }],
                    })

    def _made_autonomous_progress(self, project_id: str, after: int) -> bool:
        return any(
            event.kind == "tool"
            and event.payload.get("phase") == "completed"
            and event.payload.get("name") in AUTONOMOUS_PROGRESS_TOOLS
            for event in self.store.events_after(project_id, after, limit=1_000)
        )

    async def _run_model_operation(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        query: Any,
        *,
        operation_id: str,
    ) -> dict[str, Any]:
        query = _with_intent_notes(live, _with_language_reminder(query))
        acceptance_fault = await self._consume_acceptance_model_fault(run, live.token)
        acceptance_attempt = 0
        while True:
            try:
                if acceptance_fault == "F01":
                    acceptance_attempt += 1
                    raise TimeoutError("acceptance first visible chunk timeout")
                if acceptance_fault == "F02":
                    acceptance_attempt += 1
                    if acceptance_attempt == 1:
                        self._publish_acceptance_partial_message(run)
                    raise TimeoutError("acceptance stream idle timeout")
                result = await self._run_streaming(live, run, runner, query)
            except asyncio.CancelledError:
                entry = self.store.retry_entry(run.id, operation_id)
                if entry and entry.get("status") == "retrying":
                    self.store.settle_retry(run.id, operation_id, "cancelled")
                raise
            except Exception as error:  # noqa: BLE001 - only classified failures are recovered
                category = _recoverable_model_failure(error)
                if not category or live.cancel_event.is_set():
                    raise
                current = self.store.retry_entry(run.id, operation_id)
                used = int(current.get("attempt") or 0) if current else 0
                if used >= MAX_MODEL_RECOVERY_ATTEMPTS:
                    self.store.settle_retry(
                        run.id,
                        operation_id,
                        "exhausted",
                        last_error=f"{type(error).__name__}: {error}",
                    )
                    raise
                fingerprint = _error_fingerprint(error)
                entry = self.store.record_retry(
                    run.id,
                    operation_id,
                    category=category,
                    max_attempts=MAX_MODEL_RECOVERY_ATTEMPTS,
                    error_fingerprint=fingerprint,
                    last_error=f"{type(error).__name__}: {error}",
                    delay_seconds=MODEL_RECOVERY_DELAY_SECONDS,
                )
                attempt = int(entry.get("attempt") or 1)
                recovery_session = f"project:{run.project_id}:run:{run.id}:recovery:{attempt}:{uuid.uuid4().hex[:8]}"
                self.store.update_run(run.id, session_id=recovery_session)
                run.session_id = recovery_session
                live.stable_read_cache.clear()
                query = await self._model_recovery_prompt(
                    live, run, runner, category, operation_id,
                )
                label = "上下文已收拢" if category == "context_limit" else "模型响应超时"
                self.events.publish(run.project_id, run.id, "recovery", {
                    "operationId": operation_id,
                    "category": category,
                    "attempt": attempt,
                    "maxAttempts": MAX_MODEL_RECOVERY_ATTEMPTS,
                    "status": "retrying",
                    "text": f"{label}，正在从已保存进展恢复（{attempt}/{MAX_MODEL_RECOVERY_ATTEMPTS}）",
                    "errorFingerprint": fingerprint,
                })
                self.events.publish(run.project_id, run.id, "progress", {
                    "activityId": f"model-recovery:{run.id}:{operation_id}",
                    "text": "正在从已保存进展恢复",
                    "detail": f"{label}，已保留完成项并核对写入回执",
                    "phase": "recovery",
                    "counters": {
                        "current": attempt,
                        "total": MAX_MODEL_RECOVERY_ATTEMPTS,
                    },
                })
                self.events.publish(run.project_id, run.id, "status", {
                    "status": RUN_RUNNING,
                    "text": f"{label}，正在从已保存进展恢复（{attempt}/{MAX_MODEL_RECOVERY_ATTEMPTS}）",
                })
                await asyncio.sleep(MODEL_RECOVERY_DELAY_SECONDS)
                continue

            entry = self.store.retry_entry(run.id, operation_id)
            if entry and entry.get("status") == "retrying":
                settled = self.store.settle_retry(run.id, operation_id, "succeeded") or entry
                self.events.publish(run.project_id, run.id, "recovery", {
                    "operationId": operation_id,
                    "category": settled.get("category"),
                    "attempt": settled.get("attempt"),
                    "maxAttempts": settled.get("maxAttempts"),
                    "status": "succeeded",
                    "text": "已从保存的进展继续",
                })
            return result

    async def _consume_acceptance_model_fault(
        self,
        run: RunRecord,
        token: str,
    ) -> str:
        consume = getattr(self._gateway, "acceptance_fault", None)
        if not callable(consume):
            return ""
        for fault_id in ("F01", "F02"):
            try:
                result = await consume(
                    project_id=run.project_id,
                    run_id=run.id,
                    token=token,
                    fault_id=fault_id,
                )
            except Exception:  # noqa: BLE001 - acceptance controls never affect production
                return ""
            if isinstance(result, dict) and result.get("active") is True:
                return fault_id
        return ""

    def _publish_acceptance_partial_message(self, run: RunRecord) -> None:
        message_id = f"stream_{run.id}_acceptance_partial"
        self.events.publish(run.project_id, run.id, "message_delta", {
            "messageId": message_id,
            "role": "assistant",
            "delta": "已定位合成资源，正在准备后续修改",
        })
        self.events.publish(run.project_id, run.id, "message_done", {
            "messageId": message_id,
            "role": "assistant",
            "incomplete": True,
        })

    async def _model_recovery_prompt(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        category: str,
        operation_id: str,
    ) -> str:
        state = self.store.project_state(run.project_id).public()
        completed_facts = [
            sanitize_visible_text(item)[:300]
            for item in live.tool_summaries[-20:]
            if sanitize_visible_text(item).strip()
        ]
        brief = {
            "briefVersion": "execution-brief-v1",
            "recoveryReason": category,
            "operation": operation_id,
            "userGoal": sanitize_visible_text(run.prompt)[:2_000],
            "projectState": {
                "phase": state.get("phase"),
                "execution": state.get("execution"),
                "revision": state.get("revision"),
                "summary": sanitize_visible_text(str(state.get("summary") or ""))[:4_000],
                "openQuestions": [
                    sanitize_visible_text(str(item))[:300]
                    for item in list(state.get("openQuestions") or [])[:10]
                ],
            },
            "candidateRevision": run.candidate_revision or None,
            "requiredTools": sorted(live.required_tools),
            "completedRequiredTools": sorted(live.observed_tools & live.required_tools),
            "recentCompletedFacts": completed_facts,
        }
        output = await runner.run_workflow(
            build_execution_brief_workflow(),
            {"payload_json": json.dumps(brief, ensure_ascii=False, sort_keys=True)},
            session=f"{run.session_id}:execution-brief",
        )
        prompt = execution_brief_prompt(output)
        if not prompt:
            raise AgentRuntimeError(
                "EXECUTION_BRIEF_WORKFLOW_INVALID",
                "恢复摘要没有形成有效结果",
                status=500,
            )
        return prompt

    async def _required_tool_prompt(
        self,
        live: LiveRun,
        run: RunRecord,
        runner: Any,
        missing_tools: set[str],
    ) -> str:
        matching = [
            recovery for recovery in live.technical_recoveries.values()
            if str(recovery.get("requiredTool") or "") in missing_tools
        ]
        if not matching:
            required = "、".join(sorted(missing_tools))
            return (
                f"服务端尚未观察到本轮必须执行的工具：{required}。"
                "不要声称已经运行、重试、完成或生成轮次；立即调用该工具，以真实返回为准继续。"
            )
        recovery = matching[0]
        state = self.store.project_state(run.project_id).public()
        payload = {
            "issueId": recovery.get("issueId"),
            "requiredTool": recovery.get("requiredTool"),
            "targetKind": recovery.get("targetKind"),
            "sourceTool": recovery.get("sourceTool"),
            "projectPhase": state.get("phase"),
            "candidateRevision": run.candidate_revision or None,
            "requiredTools": sorted(missing_tools),
        }
        output = await runner.run_workflow(
            build_tool_redirect_workflow(),
            {"payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True)},
            session=f"{run.session_id}:tool-redirect:{payload['issueId']}",
        )
        prompt = tool_redirect_prompt(output)
        if not prompt:
            raise AgentRuntimeError(
                "TOOL_REDIRECT_WORKFLOW_INVALID",
                "领域工具恢复流程没有形成有效步骤",
                status=500,
            )
        return prompt

    @staticmethod
    def _interjection_continuation_prompt(undisposed: list[dict[str, Any]]) -> str:
        head = undisposed[0]
        return (
            f"专家插话 #{head['id']} 还没有处置结论。本轮系统指派的目标就是这条插话："
            "先完成或裁决它，并调用 dispose_interjection 登记处置（登记约束/转待办/"
            "已满足附可验真锚点/纯提问已回答/拒绝说明理由），再考虑其它工作。不要请求用户指示，"
            "也不要把插话内容当成普通聊天忽略。"
        )

    def _publish_interjections_undisposed(
        self,
        run: RunRecord,
        undisposed: list[dict[str, Any]],
    ) -> None:
        """插话没处置就收尾时，把这件事摆到台面上。

        否则界面只显示「本轮完成」，专家无法区分「插话办了」和「插话被静默
        丢弃」——后者正是这套机制要消灭的失败形态，它至少必须可见。
        """
        head = undisposed[0]
        self.events.publish(run.project_id, run.id, "tool", {
            "name": "interjection_undisposed",
            "phase": "waiting",
            "summary": (
                f"本轮结束时还有 {len(undisposed)} 条专家插话没有处置结论，"
                f"最早一条是：「{str(head['text'])[:200]}」。"
                "它会在下一轮继续作为指派目标；也可以直接发消息催促或调整。"
            ),
            "undisposedCount": len(undisposed),
            "headInterjectionId": head["id"],
            "problemVisibility": "user",
        })
        logger.warning(
            "run %s settled with %s undisposed interjections (head=%s)",
            run.id, len(undisposed), head["id"],
        )

    def _autonomous_continuation_prompt(
        self,
        project_id: str,
        live: LiveRun | None = None,
        *,
        stalled: int = 0,
    ) -> str:
        state = json.dumps(self.project_state(project_id)["state"], ensure_ascii=False)[:12_000]
        plan_hint = ""
        if live is not None and live.situation.plan_remaining:
            next_task = f"，下一项是「{live.situation.plan_next_task}」" if live.situation.plan_next_task else ""
            plan_hint = (
                f"\n# 受控建模计划待办\n"
                f"计划还有 {live.situation.plan_remaining} 项必选任务没有形成候选{next_task}。\n"
            )
            if stalled:
                plan_hint += (
                    f"你已经连续 {stalled} 轮只做读取和核对、没有写入任何候选。"
                    "本轮必须对下一项待办发起实际写入（create_*/patch_*/stage_change_set 等）；"
                    "若确实无法写入，调用 ask 说明具体阻塞点，不要再重复核对现状。\n"
                )
        return (
            "服务端检测到项目已经取得实质进展，但尚未到达需要专家决定、失败或发布交付的边界。"
            "不要请求用户指示，也不要只描述下一步；立即继续执行能自行完成的下一步，直到调用 ask、"
            "request_confirmation，或确实没有可执行动作。\n\n"
            "# 项目唯一 Agent 状态快照\n"
            f"{state}\n"
            f"{plan_hint}"
            "先调用 get_project_state 获取最新版本，状态转换只能调用 transition_project_state。"
        )

    def _publish_plan_incomplete_activity(
        self,
        live: LiveRun,
        run: RunRecord,
        *,
        stalled: int,
    ) -> None:
        """计划没做完就收尾时，把待办和空转轮数摆到台面上。

        否则界面只显示“本轮完成”，用户无法区分“真做完了”和“连着几轮没写东西被判停”。
        """
        next_task = f"，下一项是「{live.situation.plan_next_task}」" if live.situation.plan_next_task else ""
        self.events.publish(run.project_id, run.id, "tool", {
            "name": "change_plan_incomplete",
            "phase": "waiting",
            "summary": (
                f"本轮结束时建模计划还有 {live.situation.plan_remaining} 项必选任务没有形成候选{next_task}；"
                f"已连续 {stalled} 轮没有写入候选。可以直接发消息让助手继续，或说明需要调整的地方。"
            ),
            "planRemaining": live.situation.plan_remaining,
            "planNextTask": live.situation.plan_next_task,
            "stalledRounds": stalled,
        })

    async def _ensure_visible_result(
        self,
        live: LiveRun,
        run: RunRecord,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if result.get("result_type") != "success":
            return result
        output = str(result.get("output") or "").strip()
        if output:
            if result.get("streamed"):
                return result
            self._publish_stream_text(run, output)
            return {**result, "output": output, "streamed": True}

        empty_kind = str(result.get("emptyKind") or "empty_stream")
        self.events.publish(run.project_id, run.id, "progress", {
            "activityId": f"compact-summary:{run.id}",
            "text": "正在重新整理工作摘要",
            "detail": "原模型调用没有返回可读正文，正在使用已完成的处理事实重新生成",
            "phase": "summary_retry",
            "counters": {},
        })
        snapshot = run.latest_interaction or {}
        if snapshot.get("requiresArtifact") and not self._script_artifact_produced(live, run):
            # 脚本契约调整类交互要求本轮形成新契约/候选/下一张确认卡；空正文恢复
            # 若没有这些产物，不允许把 Run 结算为成功完成，否则会形成假完成状态。
            failure = (
                "本轮脚本契约调整没有形成新的契约、脚本候选或下一张确认卡；"
                "专家调整意见已保留，请从当前交互继续。"
            )
            self._publish_stream_text(run, failure)
            return {
                "result_type": "error",
                "output": failure,
                "streamed": True,
                "errorCode": "EMPTY_MODEL_RESPONSE_WITHOUT_ARTIFACT",
                "emptyKind": empty_kind,
                "retryable": True,
            }
        summary = await self._run_compact_summary(live, run, empty_kind)
        if summary:
            return {
                "result_type": "success",
                "output": summary,
                "streamed": True,
                "recoveredFrom": empty_kind,
            }

        failure = "模型没有返回可读摘要；已经完成的处理和候选修改均已保留，请从当前进展重试。"
        self._publish_stream_text(run, failure)
        return {
            "result_type": "error",
            "output": failure,
            "streamed": True,
            "errorCode": "EMPTY_MODEL_RESPONSE",
            "emptyKind": empty_kind,
        }

    async def _run_compact_summary(
        self,
        live: LiveRun,
        run: RunRecord,
        empty_kind: str,
    ) -> str:
        if live.bundle is None:
            return ""
        state = self.store.project_state(run.project_id).public()
        facts = "\n".join(f"- {item}" for item in live.tool_summaries[-MAX_TOOL_SUMMARIES:])
        snapshot = run.latest_interaction or {}
        if snapshot:
            snapshot_lines = [
                f"- 最新交互问题：{sanitize_visible_text(str(snapshot.get('question') or ''))[:1_200]}"
                if str(snapshot.get("question") or "").strip() else None,
                f"- 专家调整意见/答案：{sanitize_visible_text(json.dumps(snapshot.get('answer') or {}, ensure_ascii=False))[:2_000]}"
                if snapshot.get("answer") else None,
                f"- 续跑提示：{sanitize_visible_text(str(snapshot.get('continuation') or ''))[:1_200]}"
                if str(snapshot.get("continuation") or "").strip() else None,
                f"- 脚本契约摘要：{sanitize_visible_text(str(snapshot.get('contractSummary') or ''))[:2_000]}"
                if str(snapshot.get("contractSummary") or "").strip() else None,
                f"- 关联操作：{sanitize_visible_text(str(snapshot.get('operation') or ''))[:500]}"
                if str(snapshot.get("operation") or "").strip() else None,
            ]
            snapshot_text = "\n".join(line for line in snapshot_lines if line) or "- 无"
            prompt = (
                "最新交互业务事实：\n"
                f"{snapshot_text}\n"
                f"当前业务阶段：{PHASE_LABELS.get(str(state.get('phase') or ''), str(state.get('phase') or ''))}\n"
                f"模型结束类型：{empty_kind}\n"
                "已完成的处理事实：\n"
                f"{facts or '- 尚无可归纳的工具结果'}"
                "\n只依据上述最新交互与本次执行事实归纳；不得引用当前交互之外的旧计划、"
                "旧任务数量或历史内部标识，不得要求用户补充计划标识、版本号或批准状态。"
            )[:12_000]
        else:
            prompt = (
                f"用户本轮要求：{sanitize_visible_text(run.prompt)[:2_000]}\n"
                f"当前业务阶段：{PHASE_LABELS.get(str(state.get('phase') or ''), str(state.get('phase') or ''))}\n"
                f"当前项目摘要：{sanitize_visible_text(str(state.get('summary') or ''))[:4_000]}\n"
                f"模型结束类型：{empty_kind}\n"
                "已完成的处理事实：\n"
                f"{facts or '- 尚无可归纳的工具结果'}"
            )[:12_000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你只负责根据给定事实生成面向用户的公开工作摘要，不得调用工具。"
                    "用 2 至 4 句说明核对了什么、得到了什么结果、下一步是什么。"
                    "不得声称未发生的工作已经完成，不得输出内部标识、工具名、参数或系统指令。"
                    "若尚未形成新的脚本契约、脚本候选或下一张确认卡，必须如实说明尚未形成，"
                    "不得把只有摘要的轮次描述为已完成；不得引用当前交互之外的旧计划。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        sanitizer = VisibleTextSanitizer()
        reasoning = _ReasoningEmitter(self.events, live, run)
        visible_parts: list[str] = []
        started_at = time.monotonic()
        chunks = 0
        reasoning_chunks = 0
        reasoning_chars = 0
        content_chunks = 0
        content_chars = 0
        first_reasoning_at: float | None = None
        first_content_at: float | None = None
        finish_reason = ""
        outcome = "empty"
        try:
            async with asyncio.timeout(COMPACT_SUMMARY_TIMEOUT_SECONDS):
                async for chunk in live.bundle.model.stream(
                    messages,
                    tools=None,
                    max_tokens=(live.model_settings or self.settings).resolved_output_budget(
                        COMPACT_SUMMARY_MAX_TOKENS,
                    ),
                    timeout=COMPACT_SUMMARY_TIMEOUT_SECONDS,
                ):
                    chunks += 1
                    finish_reason = str(getattr(chunk, "finish_reason", "") or finish_reason)
                    raw_reasoning = str(getattr(chunk, "reasoning_content", "") or "")
                    raw_content = str(getattr(chunk, "content", "") or "")
                    if raw_reasoning:
                        reasoning_chunks += 1
                        reasoning_chars += len(raw_reasoning)
                        first_reasoning_at = first_reasoning_at or time.monotonic()
                        reasoning.feed(raw_reasoning)
                    if raw_content:
                        content_chunks += 1
                        content_chars += len(raw_content)
                        first_content_at = first_content_at or time.monotonic()
                        delta = sanitizer.feed(raw_content)
                        if delta:
                            visible_parts.append(delta)
                            self.events.publish(run.project_id, run.id, "message_delta", {
                                "messageId": message_id,
                                "role": "assistant",
                                "delta": delta,
                            })
            tail = sanitizer.finish()
            if tail:
                visible_parts.append(tail)
                self.events.publish(run.project_id, run.id, "message_delta", {
                    "messageId": message_id,
                    "role": "assistant",
                    "delta": tail,
                })
            output = "".join(visible_parts).strip()
            if output:
                outcome = "content"
            elif reasoning.seen:
                outcome = "reasoning_only"
            elif chunks:
                outcome = "empty_content"
            else:
                outcome = "zero_chunk"
            if output:
                self.events.publish(run.project_id, run.id, "message_done", {
                    "messageId": message_id,
                    "role": "assistant",
                })
            return output
        except TimeoutError:
            outcome = "timeout"
            return ""
        except Exception as error:  # noqa: BLE001
            outcome = "error"
            logger.warning("Compact summary failed for run %s: %s", run.id, type(error).__name__)
            return ""
        finally:
            reasoning.finish()

            def elapsed(value: float | None) -> int | None:
                return round((value - started_at) * 1000) if value else None

            self.events.publish(run.project_id, run.id, "model_call", {
                "callId": f"compact_{uuid.uuid4().hex[:12]}",
                "actor": "compact_summary",
                "phase": "failed" if outcome != "content" else "completed",
                "outcome": outcome,
                "durationMs": round((time.monotonic() - started_at) * 1000),
                "firstReasoningMs": elapsed(first_reasoning_at),
                "firstContentMs": elapsed(first_content_at),
                "chunks": chunks,
                "reasoningChunks": reasoning_chunks,
                "reasoningChars": reasoning_chars,
                "contentChunks": content_chunks,
                "contentChars": content_chars,
                "finishReason": finish_reason,
                "inputMessages": len(messages),
                "inputChars": sum(len(str(item["content"])) for item in messages),
                "toolDefinitions": 0,
            })

    def _publish_stream_text(self, run: RunRecord, text: str) -> None:
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        self.events.publish(run.project_id, run.id, "message_delta", {
            "messageId": message_id,
            "role": "assistant",
            "delta": text,
        })
        self.events.publish(run.project_id, run.id, "message_done", {
            "messageId": message_id,
            "role": "assistant",
        })

    async def _run_streaming(self, live: LiveRun, run: RunRecord, runner: Any, query: Any) -> dict[str, Any]:
        message_id = f"stream_{run.id}_{uuid.uuid4().hex[:8]}"
        output_parts: list[str] = []
        answer_parts: list[str] = []
        visible_parts: list[str] = []
        sanitizer = VisibleTextSanitizer()
        reasoning = _ReasoningEmitter(self.events, live, run)
        interaction: InteractionRecord | None = None
        emitted = False
        held_parts: list[str] = []
        model_events = 0
        stream_completed = False
        # 一次 _run_streaming 里模型可能"输出 → 调工具 → 再输出"多轮，
        # 这两个标志用来在轮次之间补断句，而不是把所有轮次拍平成一段。
        output_segment_break = False

        def publish_visible(raw_text: str) -> None:
            nonlocal emitted, output_segment_break
            delta = sanitizer.feed(raw_text)
            if not delta:
                return
            if output_segment_break:
                separator = _segment_separator(visible_parts[-1] if visible_parts else "")
                if separator:
                    delta = separator + delta
                output_segment_break = False
            visible_parts.append(delta)
            emitted = True
            self.events.publish(run.project_id, run.id, "message_delta", {
                "messageId": message_id,
                "role": "assistant",
                "delta": delta,
            })

        stream = runner.run_agent_streaming(
            live.bundle.agent,
            {"query": query, "_steering_queue": live.steering},
            session=run.session_id,
        )
        try:
            async for chunk in stream:
                chunk_type = str(getattr(chunk, "type", "") or "")
                if chunk_type == "llm_reasoning":
                    model_events += 1
                    output_segment_break = emitted
                    reasoning.feed(_chunk_text(chunk))
                elif chunk_type == "llm_output":
                    model_events += 1
                    delta = _chunk_text(chunk)
                    if not delta:
                        continue
                    reasoning.mark_segment_break()
                    output_parts.append(delta)
                    if _missing_required_tools(live):
                        held_parts.append(delta)
                        continue
                    if held_parts:
                        delta = "".join(held_parts) + delta
                        held_parts.clear()
                    publish_visible(delta)
                elif chunk_type == "answer":
                    model_events += 1
                    framework_error = _stream_error_text(chunk)
                    if framework_error:
                        raise RuntimeError(framework_error)
                    answer = _chunk_text(chunk)
                    if answer:
                        answer_parts.append(answer)
                elif chunk_type == "__interaction__":
                    interaction = _interaction_from_stream(run, getattr(chunk, "payload", None))
            stream_completed = True
        finally:
            close_stream = getattr(stream, "aclose", None)
            if callable(close_stream):
                try:
                    await asyncio.shield(close_stream())
                except (asyncio.CancelledError, Exception):
                    # The runner/SDK may not expose a cooperative close; the
                    # live cancellation fence still drops every late event.
                    pass
            reasoning.finish()
            if not _missing_required_tools(live):
                if held_parts:
                    publish_visible("".join(held_parts))
                    held_parts.clear()
                tail = sanitizer.finish()
                if tail:
                    visible_parts.append(tail)
                    emitted = True
                    self.events.publish(run.project_id, run.id, "message_delta", {
                        "messageId": message_id,
                        "role": "assistant",
                        "delta": tail,
                    })
            if emitted:
                self.events.publish(run.project_id, run.id, "message_done", {
                    "messageId": message_id,
                    "role": "assistant",
                    "incomplete": not stream_completed,
                })

        if interaction is not None:
            return {"result_type": "interrupt", "interaction": interaction, "streamed": emitted}

        raw_output = "".join(output_parts) or "".join(answer_parts)
        output = "".join(visible_parts) or sanitize_visible_text(raw_output)
        _record_turn_output(live, output)
        if output and not emitted and not _missing_required_tools(live):
            self.events.publish(run.project_id, run.id, "message_delta", {
                "messageId": message_id,
                "role": "assistant",
                "delta": output,
            })
            self.events.publish(run.project_id, run.id, "message_done", {
                "messageId": message_id,
                "role": "assistant",
            })
            emitted = True
        if output:
            empty_kind = ""
        elif reasoning.seen:
            empty_kind = "reasoning_only"
        elif model_events:
            empty_kind = "empty_content"
        else:
            empty_kind = "zero_chunk"
        return {
            "result_type": "success",
            "output": output,
            "streamed": emitted,
            "emptyKind": empty_kind,
        }

    def _settle(self, live: LiveRun, run: RunRecord, result: dict[str, Any]) -> None:
        if live.cancel_requested or self._execution(run.project_id, run.id) in {
            RUN_STOPPING, RUN_CANCELLED,
        }:
            return
        if result.get("result_type") == "interrupt":
            if live.restore_interaction:
                self.events.publish(
                    run.project_id,
                    run.id,
                    "interaction",
                    {"id": live.restore_interaction.id, "status": "answered", "answer": {"action": "interject"}},
                )
            interaction = result.get("interaction")
            if not isinstance(interaction, InteractionRecord):
                interaction = _interaction_from_result(run, result)
            self.store.create_interaction(interaction)
            # 冲突裁决卡出现时，服务端确定性地把未处置插话绑定为「已升级待决」，
            # 不靠模型自觉再调一次登记工具——绑定动作可以算出来就不该交给推断。
            # 升级不是终态：卡被回答后插话会重新开放，按专家裁决完成收尾。
            if interaction.kind == "conflict_decision":
                escalated = self.store.escalate_interjections(run.project_id, interaction.id)
                if escalated:
                    self.events.publish(run.project_id, run.id, "tool", {
                        "name": "interjection_escalated",
                        "phase": "completed",
                        "summary": f"{len(escalated)} 条专家插话已升级为待专家裁决",
                        "interjectionIds": escalated,
                        "interactionId": interaction.id,
                        "problemVisibility": "internal",
                    })
            self._publish_state(run.project_id, run.id)
            self.events.publish(run.project_id, run.id, "checkpoint", {"id": run.session_id, "revision": run.candidate_revision or run.baseline_revision})
            self.events.publish(run.project_id, run.id, "interaction", interaction.public())
            self.events.publish(run.project_id, run.id, "status", {"status": RUN_WAITING, "text": "等待你的决定；输入框仍可继续提问"})
            return

        output = str(result.get("output") or "").strip()
        if result.get("result_type") == "success" and (not output or not result.get("streamed")):
            output = "模型没有返回可读结果；已经完成的处理和候选修改均已保留，请从当前进展重试。"
            self._publish_stream_text(run, output)
            result = {
                "result_type": "error",
                "output": output,
                "streamed": True,
                "errorCode": "INVISIBLE_SUCCESS",
            }
        status = RUN_FAILED if result.get("result_type") == "error" else RUN_COMPLETED
        self.store.update_run(run.id, status=status, result=output, error="" if status == RUN_COMPLETED else output)
        self._publish_state(run.project_id, run.id)
        if output and not result.get("streamed"):
            self.events.publish(run.project_id, run.id, "message", {"role": "assistant", "blocks": [{"type": "text", "text": output}]})
        if live.restore_interaction:
            self._restore_interaction(run, live.restore_interaction)
            return
        self.events.publish(run.project_id, run.id, "status", {"status": status, "text": "本轮完成" if status == RUN_COMPLETED else "本轮未完成"})

    def _restore_interaction(self, run: RunRecord, original: InteractionRecord) -> None:
        self.events.publish(
            run.project_id,
            run.id,
            "interaction",
            {"id": original.id, "status": "answered", "answer": {"action": "interject"}},
        )
        restored = InteractionRecord(
            id=f"interaction_{uuid.uuid4().hex}",
            project_id=original.project_id,
            run_id=original.run_id,
            tool_call_id=f"{INTERJECTION_CONTINUATION_PREFIX}{original.tool_call_id}",
            kind=original.kind,
            payload={**original.payload, "restoredAfterInterjection": True},
        )
        self.store.create_interaction(restored)
        self._publish_state(run.project_id, run.id)
        self.events.publish(run.project_id, run.id, "checkpoint", {"id": run.session_id, "revision": run.candidate_revision or run.baseline_revision})
        self.events.publish(run.project_id, run.id, "interaction", restored.public())
        self.events.publish(run.project_id, run.id, "status", {"status": RUN_WAITING, "text": "已回答插话，回到原问题；输入框仍可继续提问"})

    def _execution(self, project_id: str, run_id: str) -> str:
        state = self.store.project_state(project_id)
        if state.run_id != run_id:
            return ""
        return str(state.public()["execution"])

    def _publish_state(self, project_id: str, run_id: str) -> None:
        state = self.store.project_state(project_id)
        public = state.public()
        if public.get("execution") in {RUN_STOPPING, RUN_CANCELLED}:
            public.pop("error", None)
            public["cancellation"] = {
                "requested": True,
                "status": public.get("execution"),
                "reason": "用户已停止运行",
            }
        public["phaseLabel"] = PHASE_LABELS.get(public["phase"], public["phase"])
        self.events.publish(project_id, run_id, "state", public)

    @staticmethod
    def _run_public(run: RunRecord, state: Any) -> dict[str, Any]:
        public = run.public()
        if state.run_id == run.id:
            public["status"] = state.public()["execution"]
            public["interactionId"] = state.interaction_id or None
            public["error"] = state.error or None
        return public


def _interaction_from_result(run: RunRecord, result: dict[str, Any]) -> InteractionRecord:
    interrupt_ids = [str(value) for value in result.get("interrupt_ids") or []]
    described = _describe_interrupts(result)
    info = described[0] if described else {}
    tool_call_id = str(info.get("id") or (interrupt_ids[0] if interrupt_ids else ""))
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    kind = str(metadata.get("kind") or "clarification")
    payload = {
        "question": info.get("message") or metadata.get("question") or "需要你补充一个决定",
        "options": info.get("uiOptions") or [],
        "schema": info.get("payloadSchema") or {},
        **metadata,
    }
    return InteractionRecord(
        id=f"interaction_{uuid.uuid4().hex}",
        project_id=run.project_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        kind=kind,
        payload=payload,
    )


def _required_text_option(
    payload: dict[str, Any],
    answer: dict[str, Any],
) -> dict[str, Any] | None:
    raw_selected = answer.get("selected")
    selected = {
        str(value)
        for value in raw_selected
        if str(value).strip()
    } if isinstance(raw_selected, (list, tuple, set)) else set()
    value = str(answer.get("value") or "").strip()
    if value:
        selected.add(value)
    for option in payload.get("options") or []:
        if not isinstance(option, dict) or option.get("requiresText") is not True:
            continue
        option_value = str(option.get("value") or option.get("label") or "").strip()
        if option_value and option_value in selected:
            return option
    return None


def _interaction_from_stream(run: RunRecord, payload: Any) -> InteractionRecord:
    tool_call_id = str(_field(payload, "interaction_id") or _field(payload, "id") or "")
    value = _field(payload, "value") or payload
    nested = _field(value, "value")
    if nested is not None and not _field(value, "message"):
        value = nested
    tool_call_id = str(tool_call_id or _field(value, "id") or "")
    metadata = _field(value, "metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    kind = str(metadata.get("kind") or "clarification")
    interaction_payload = {
        "question": _field(value, "message") or metadata.get("question") or "需要你补充一个决定",
        "options": _field(value, "ui_options") or _field(value, "uiOptions") or [],
        "schema": _field(value, "payload_schema") or _field(value, "payloadSchema") or {},
        **metadata,
    }
    return InteractionRecord(
        id=f"interaction_{uuid.uuid4().hex}",
        project_id=run.project_id,
        run_id=run.id,
        tool_call_id=tool_call_id,
        kind=kind,
        payload=interaction_payload,
    )


def _chunk_text(chunk: Any) -> str:
    payload = getattr(chunk, "payload", None)
    if isinstance(payload, dict):
        value = payload.get("content") or payload.get("output") or ""
    else:
        value = payload if isinstance(payload, str) else ""
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value or "")


def _stream_error_text(chunk: Any) -> str:
    payload = getattr(chunk, "payload", None)
    if not isinstance(payload, dict):
        return ""
    candidates = [payload, payload.get("output")]
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("result_type") != "error":
            continue
        return str(candidate.get("output") or candidate.get("error") or "模型执行失败").strip()
    return ""


def _is_timeout_exception(error: BaseException) -> bool:
    current: BaseException | None = error
    for _depth in range(4):
        if current is None:
            break
        name = type(current).__name__.lower()
        message = str(current).lower()
        if "timeout" in name or "timed out" in message or "timeout" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_context_limit_exception(error: BaseException) -> bool:
    current: BaseException | None = error
    markers = (
        "context length",
        "context_length",
        "context window",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit",
        "上下文过长",
        "上下文长度",
    )
    for _depth in range(4):
        if current is None:
            break
        message = str(current).lower()
        if any(marker in message for marker in markers):
            return True
        current = current.__cause__ or current.__context__
    return False


def _recoverable_model_failure(error: BaseException) -> str:
    if _is_context_limit_exception(error):
        return "context_limit"
    if _is_timeout_exception(error):
        return "model_timeout"
    return ""


def _error_fingerprint(error: BaseException) -> str:
    value = f"{type(error).__name__}:{str(error).strip()}".encode("utf-8", errors="replace")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _describe_interrupts(result: dict[str, Any]) -> list[dict[str, Any]]:
    described: list[dict[str, Any]] = []
    for schema in result.get("state") or []:
        payload = getattr(schema, "payload", None)
        value = getattr(payload, "value", None) if payload is not None else None
        described.append({
            "id": _field(payload, "id"),
            "message": _field(value, "message"),
            "toolName": _field(value, "tool_name"),
            "payloadSchema": _field(value, "payload_schema") or {},
            "uiOptions": _field(value, "ui_options") or [],
            "metadata": _field(value, "metadata") or {},
        })
    return described


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None) if value is not None else None


def _require_project_and_token(project_id: str, token: str) -> None:
    if not project_id.strip():
        raise AgentRuntimeError("PROJECT_REQUIRED", "缺少项目标识")
    if not token:
        raise AgentRuntimeError("TOKEN_REQUIRED", "需要登录后才能使用建模助手", status=401)


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


_EVALUATION_EXECUTION_INTENT = re.compile(
    r"(?:重试|重新运行|再次运行|立即运行|运行|开始|启动|执行).{0,12}评测|"
    r"评测.{0,12}(?:重试|重新运行|再次运行|立即运行|运行|开始|启动|执行)",
)

_EVALUATION_RESULT_INTENT = re.compile(
    r"(?:归因|分析|查看|读取|诊断|排查).{0,16}(?:评测|测试).{0,16}(?:失败|结果|错误)|"
    r"(?:评测|测试).{0,16}(?:失败|结果|错误).{0,16}(?:归因|分析|查看|读取|诊断|排查)",
)

_EVALUATION_CANCEL_INTENT = re.compile(
    r"(?:停止|取消|终止|中止|停掉).{0,12}(?:正式)?(?:评测|测评|测试)|"
    r"(?:正式)?(?:评测|测评|测试).{0,12}(?:停止|取消|终止|中止|停掉)",
)

_EVALUATION_CAPABILITY_QUESTION = re.compile(
    r"(?:能力|能否|是否|有没有|可不可以|可以吗|怎么|如何).{0,20}(?:评测|测评|测试)|"
    r"(?:评测|测评|测试).{0,20}(?:能力|能否|是否|有没有|可不可以|可以吗|怎么|如何)",
)

_FULL_READ_INTENT = re.compile(
    r"全文.{0,6}(?:精读|阅读|通读)|(?:精读|通读).{0,12}(?:文档|文件|资料|材料)",
)

# Full-read wording can occur in a prohibition or in a statement that the
# result already exists. Those meanings must win over the positive keyword.
_FULL_READ_PROHIBITED = re.compile(
    r"(?:禁止|不要|不再|无需|不用|不必|不得|别再|避免).{0,16}(?:全文\s*(?:精读|阅读|通读)|(?:精读|通读).{0,8}(?:文档|文件|资料|材料))|"
    r"(?:全文\s*(?:精读|阅读|通读)|(?:精读|通读).{0,8}(?:文档|文件|资料|材料)).{0,16}(?:禁止|不要|不再|无需|不用|不必|不得|别再|避免)",
    re.IGNORECASE,
)
# Covers the common Chinese order “不要再次全文阅读/无需继续全文分析”，where
# an adverb sits between the negation and the full-read phrase.
_FULL_READ_PROHIBITED_BROAD = re.compile(
    r"(?:禁止|不要|不再|无需|不用|不必|不得|别再|避免).{0,40}(?:全文\s*(?:精读|阅读|通读)|(?:精读|通读).{0,12}(?:文档|文件|资料|材料))",
    re.IGNORECASE,
)
_FULL_READ_ALREADY_SATISFIED = re.compile(
    r"(?:已经|已|此前|先前|复用|沿用|基于).{0,20}(?:完成|读完|分析完成|覆盖完成|全文\s*(?:精读|阅读|通读))|"
    r"(?:全文\s*(?:精读|阅读|通读)|分析).{0,16}(?:已经|已)完成",
    re.IGNORECASE,
)


def _full_read_state(situation: RunSituation) -> str:
    """Classify full-read state before mapping intent to tools."""
    if situation.has_completed_full_read():
        return "already_satisfied"
    if not situation.materials_observed:
        return "unknown"
    return "requested" if situation.has_full_read_materials() is Verdict.ALLOW else "not_configured"

_DOCUMENT_REFERENCE_INTENT = re.compile(
    r"《[^》]{1,200}》|"
    r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff ._-]{0,120}\.(?:docx?|pdf|xlsx?|csv|md|txt)|"
    r"(?:这个|该|指定的?|上述)\s*(?:文档|文件|资料)|"
    r"(?:读取|查看|参考|根据|按照|依据).{0,80}(?:文档|文件|资料)",
    re.IGNORECASE,
)

_RULE_MUTATION_INTENT = re.compile(
    r"(?:新增|添加|补充|增加|制定|写入|修改|更新|完善)\s*(?:一条|一项|新的?|相关的?)?\s*(?:业务)?规则|"
    r"(?:业务)?规则\s*(?:新增|添加|补充|增加|制定|写入|修改|更新|完善)",
    re.IGNORECASE,
)

_MATERIAL_DIRECTIVE_INTENT = re.compile(
    r"(?:作为|用作|设为).{0,12}(?:建模目标|目标依据|数据\s*Schema|数据模式|业务依据)|"
    r"(?:建模目标|目标依据|数据\s*Schema|数据模式|业务依据).{0,12}(?:资料|材料|文档|文件)",
    re.IGNORECASE,
)

_PACKAGE_FUSION_INTENT = re.compile(
    r"(?:一次性|批量|多个|多份|全部)?.{0,8}(?:融合|合并).{0,12}(?:知识包|来源包)|"
    r"(?:知识包|来源包).{0,12}(?:批量融合|一次性融合|合并到一起)",
    re.IGNORECASE,
)

_RELEASE_REENTRY_INTENT = re.compile(
    r"增量|迭代|新版本|补充.{0,8}资料|替换.{0,8}资料|导入.{0,8}(?:知识包|来源包)|"
    r"融合|修改.{0,8}(?:知识|规则|流程|对象|指标|脚本)|修复.{0,8}(?:知识|规则|流程|对象|指标|脚本)|"
    r"处理.{0,8}开放问题|全文.{0,6}(?:精读|阅读)|精读.{0,8}(?:文档|文件|资料|材料)|启动\s*v?\d",
    re.IGNORECASE,
)

_AUTONOMOUS_WORK_INTENT = re.compile(
    r"(?:开始|继续|接着|推进|完成|生成|创建|构建|执行|运行|重试|修复|优化|融合|导入|发布|更新|修改|核对|分析|精读|全文阅读)"
    r"|(?:请|请你|帮我).{0,32}(?:建模|整理|提取|处理)"
    r"|\b(?:start|continue|proceed|build|create|run|retry|fix|optimize|merge|import|release|update)\b",
    re.IGNORECASE,
)


_APPROVED_PLAN_EXECUTION_INTENT = re.compile(
    r"(?:按|根据|依照).{0,16}(?:已批准|已审批|已同意|批准后?|审批后?)(?:了)?(?:这份|当前|这些|该|这个|本|上述)?(?:的)?(?:完整)?(?:修复|建模|变更)?计划.{0,20}(?:继续|执行|推进|完成|开始)|"
    r"(?<![不未])(?:已批准|已审批|已同意|批准|同意|确认).{0,20}(?:计划).{0,20}(?:继续|执行|推进|完成|开始)|"
    r"(?:修复计划|变更计划|已批准计划).{0,12}(?:继续|执行|推进|完成)|"
    r"(?:候选预检|完整校验|统一确认卡).{0,12}(?:继续|执行|完成|生成)",
    re.IGNORECASE,
)
_APPROVED_PLAN_NEGATION = re.compile(
    r"(?:不|未|没有|无需|不用|别|不是).{0,8}(?:按|根据|依照).{0,8}(?:继续|执行|推进|完成|开始)|"
    r"(?:确认|核对|检查|查看).{0,16}(?:计划).{0,16}(?:已完成|已执行|已处理|完成情况|执行情况|状态)|"
    r"(?:计划|任务).{0,8}(?:已完成|已执行|已处理|完成情况|执行情况)",
    re.IGNORECASE,
)


def _approved_plan_execution_requested(text: str) -> bool:
    value = str(text or "")
    return not _APPROVED_PLAN_NEGATION.search(value) and bool(_APPROVED_PLAN_EXECUTION_INTENT.search(value))


_PROGRESS_ONLY_FINAL_TEXT = re.compile(
    r"正在执行(?:候选)?预检|正在(?:准备|进行|执行)(?:候选)?预检|"
    r"下一步(?:将|会|就)?(?:展示|生成|执行|进入)|"
    r"(?:尚未|还未|没有)(?:展示|生成|形成).{0,12}(?:确认卡|候选)|"
    r"(?:即将|准备)(?:展示|生成).{0,12}(?:确认卡|候选)|"
    r"预检通过后.{0,16}(?:展示|生成)",
)


def _progress_only_final_text(text: str) -> bool:
    return bool(_PROGRESS_ONLY_FINAL_TEXT.search(str(text or "")))


def _release_reentry_requested(text: str) -> bool:
    return bool(_RELEASE_REENTRY_INTENT.search(str(text or "")))


def _document_rule_change_requested(text: str) -> bool:
    value = str(text or "").strip()
    if not value or re.search(r"(?:为什么|为何|怎么|如何|是否|有没有|是什么|能否|可否|请问|吗\s*[？?]?|[？?])", value):
        return False
    return bool(_DOCUMENT_REFERENCE_INTENT.search(value) and _RULE_MUTATION_INTENT.search(value))


def _autonomous_work_requested(text: str) -> bool:
    return bool(_AUTONOMOUS_WORK_INTENT.search(str(text or "")))


# 会形成本阶段交付物的工具。阶段切换后重新计数，用来区分
# 「刚进新阶段还没产出」和「本阶段做完了、确实没事可做」（见 #35）。
_STAGE_DELIVERY_TOOLS = frozenset(
    AUTONOMOUS_PROGRESS_TOOLS - {"transition_project_state"}
)


def _absorb_awaiting_user(live: LiveRun, result: Any) -> None:
    """从门禁结果里回填「只剩用户要自己完成的事情」。

    判据由核心侧结构化下发（GateResult.awaitingUser），不靠在阻塞文案里
    找关键词——那正是 #1/#24 反复栽过的代理指标。
    """
    if not isinstance(result, dict):
        return
    if "awaitingUser" not in result:
        return
    waiting = result.get("awaitingUser") is True
    live.situation.awaiting_user_action = waiting
    if waiting:
        blockers = result.get("blockers")
        items = [str(item) for item in blockers if str(item).strip()] if isinstance(blockers, list) else []
        live.situation.awaiting_user_reason = "；".join(items[:3])
    else:
        live.situation.awaiting_user_reason = ""


def _absorb_stage_delivery(live: LiveRun, name: str, result: Any) -> None:
    if name == "transition_project_state":
        live.situation.stage_started_without_delivery = True
        return
    if name in _STAGE_DELIVERY_TOOLS:
        live.situation.stage_started_without_delivery = False


def _absorb_reading_modes(live: LiveRun, result: Any) -> None:
    """从任何返回资料清单的核心工具结果里回填「哪些资料真的要全文精读」。

    以前是否强制精读只看用户消息里有没有「全文精读」四个字，
    于是一句由前端代发的续跑文案就能让 9.9MB 文档被整篇灌进上下文（见 #1）。
    """
    if not isinstance(result, dict):
        return
    documents = result.get("documents")
    coverage = result.get("coverage")
    if not isinstance(documents, list):
        single = result.get("document")
        documents = [single] if isinstance(single, dict) else []
    if not documents:
        return
    names: list[str] = []
    current_fingerprints: dict[str, str] = {}
    for item in documents:
        if not isinstance(item, dict):
            continue
        mode = str(item.get("readingMode") or item.get("reading_mode") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if mode == "full" and filename:
            names.append(filename)
            fingerprint = str(
                item.get("revisionFingerprint")
                or item.get("revision_fingerprint")
                or ""
            ).strip()
            if fingerprint:
                current_fingerprints[filename] = fingerprint
    live.situation.full_read_materials = tuple(dict.fromkeys(names))
    # Invalidate a previously complete result as soon as the same filename is
    # observed with another revision. This keeps a completed read tied to the
    # exact document revision instead of a mutable filename.
    if live.situation.completed_full_read_materials:
        valid_completed = []
        valid_fingerprints: dict[str, str] = {}
        for filename in live.situation.completed_full_read_materials:
            previous = live.situation.completed_full_read_fingerprints.get(filename, "")
            current = current_fingerprints.get(filename, "")
            # If the previous completion has no fingerprint, it predates the
            # revision-bound protocol and cannot be trusted after a fresh
            # revision-bearing listing. Treat it as stale rather than reusing
            # an unverifiable result.
            if current and (not previous or previous != current):
                continue
            valid_completed.append(filename)
            if previous:
                valid_fingerprints[filename] = previous
        live.situation.completed_full_read_materials = tuple(valid_completed)
        live.situation.completed_full_read_fingerprints = valid_fingerprints
    completed: list[str] = []
    if isinstance(coverage, list):
        complete_ids: set[str] = set()
        complete_names: set[str] = set()
        for coverage_item in coverage:
            if not isinstance(coverage_item, dict) or coverage_item.get("complete") is not True:
                continue
            complete_ids.add(str(coverage_item.get("documentId") or "").strip())
            complete_names.add(str(coverage_item.get("filename") or "").strip())
        for item in documents:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or "").strip()
            document_id = str(item.get("documentId") or item.get("id") or "").strip()
            if filename and (filename in complete_names or document_id in complete_ids):
                completed.append(filename)
                fingerprint = str(
                    item.get("revisionFingerprint")
                    or item.get("revision_fingerprint")
                    or ""
                ).strip()
                if not fingerprint:
                    for coverage_item in coverage:
                        if not isinstance(coverage_item, dict):
                            continue
                        coverage_filename = str(
                            coverage_item.get("filename") or "",
                        ).strip()
                        coverage_document_id = str(
                            coverage_item.get("documentId") or "",
                        ).strip()
                        if coverage_filename != filename and coverage_document_id != document_id:
                            continue
                        fingerprint = str(
                            coverage_item.get("revisionFingerprint")
                            or coverage_item.get("revision_fingerprint")
                            or "",
                        ).strip()
                        break
                if fingerprint:
                    live.situation.completed_full_read_fingerprints[filename] = fingerprint
        # Only a coverage-bearing response is authoritative for completion.
        # list_documents/get_project_state responses must not erase a prior
        # revision-bound 100% result.
        if completed:
            live.situation.completed_full_read_materials = tuple(dict.fromkeys(completed))
    live.situation.materials_observed = True


def _absorb_product_ledger(live: LiveRun, result: Any) -> None:
    """将台账工具的结构化状态保留在本轮快照中，供诊断回执使用。"""
    if not isinstance(result, dict):
        return
    validation = result.get("validation")
    counts = validation.get("counts") if isinstance(validation, dict) else result.get("counts")
    if isinstance(counts, dict):
        aliases = {
            "raw": ("raw", "rawCount"),
            "canonical": ("canonical", "canonicalCount"),
            "mergedGroups": ("mergedGroups", "mergedGroupCount"),
            "complete": ("complete", "completeCount"),
            "insufficient": ("insufficient", "insufficientCount"),
        }
        normalized: dict[str, int] = {}
        for target, keys in aliases.items():
            for key in keys:
                value = counts.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    normalized[target] = value
                    break
        if normalized:
            live.situation.product_ledger_counts = normalized
    status = str(result.get("status") or "").strip()
    if not status and isinstance(validation, dict):
        status = "finalized" if validation.get("pass") is True else "needs_review"
    if status:
        live.situation.product_ledger_status = status
    for key, attr in (
        ("ledgerId", "product_ledger_id"),
        ("analysisRevision", "product_ledger_analysis_revision"),
    ):
        value = str(result.get(key) or "").strip()
        if value:
            setattr(live.situation, attr, value)
    if isinstance(result.get("lastSuccessfulBatch"), int):
        live.situation.product_ledger_last_batch = int(result["lastSuccessfulBatch"])
    if isinstance(result.get("processed"), int):
        live.situation.product_ledger_processed = max(0, int(result["processed"]))
    if isinstance(result.get("remaining"), int):
        live.situation.product_ledger_remaining = max(0, int(result["remaining"]))


def _absorb_plan_progress(live: LiveRun, result: Any) -> None:
    """从任何带计划快照的核心工具结果里回填待办进度。

    计划的权威状态在核心侧（TypeScript），Python 侧原本完全看不到它，
    导致已批准但未做完的计划会在几轮纯调研之后被判定为“本轮完成”。
    """
    plan = result.get("plan") if isinstance(result, dict) else None
    if not isinstance(plan, dict):
        return
    remaining = plan.get("remaining_count")
    if not isinstance(remaining, int) or isinstance(remaining, bool):
        return
    live.situation.plan_remaining = max(0, remaining)
    live.situation.plan_next_task = ""
    if not live.situation.plan_remaining:
        return
    tasks = plan.get("tasks")
    if not isinstance(tasks, list):
        return
    settled = {"accepted", "skipped"}
    for task in tasks:
        if not isinstance(task, dict) or not task.get("required"):
            continue
        if str(task.get("status") or "") in settled:
            continue
        live.situation.plan_next_task = str(task.get("title") or "")
        return


@dataclass(frozen=True)
class IntentRule:
    """一条「文本线索 → 强制工具」规则，外加一道状态否决权。

    文本只提供线索，状态拥有否决权：命中关键词只说明「可能要做这件事」，
    这件事在当前状态下成不成立由 precondition 判断。缺了这一层，
    一句由前端代发、复述已保存设置的模板文案就能把大文档整篇灌进上下文（见 #1）。
    """

    name: str
    requires: frozenset[str]
    # 拿不准时偏哪边，依据是「猜错了哪边代价大」
    unknown_default: Verdict = Verdict.ALLOW
    precondition: Callable[[RunSituation], Verdict] | None = None
    unmet_note: str = ""

    def verdict(self, situation: RunSituation) -> Verdict:
        if self.precondition is None:
            return Verdict.ALLOW
        result = self.precondition(situation)
        return self.unknown_default if result is Verdict.UNKNOWN else result


_FULL_READ_RULE = IntentRule(
    name="full_read",
    requires=frozenset({"set_material_directive", "read_documents_fully"}),
    # 宁可多读一次，也不能吞掉用户真实的精读诉求
    unknown_default=Verdict.ALLOW,
    precondition=lambda situation: situation.has_full_read_materials(),
    unmet_note=(
        "你提到了全文精读，但当前没有任何资料被设为全文精读，所有资料都是按需读取。"
        "如果确实需要整篇读取，请先用 set_material_directive 调整该资料的阅读方式，"
        "不要直接调用全文精读。"
    ),
)


def _full_read_required_tools(situation: RunSituation) -> set[str]:
    """Return only the still-missing full-read steps for the observed state."""
    if situation.has_completed_full_read():
        return set()
    if not situation.materials_observed:
        return set(_FULL_READ_RULE.requires)
    if situation.has_full_read_materials() is Verdict.ALLOW:
        # The directive is already an observed fact; only coverage remains.
        return {"read_documents_fully"}
    return set()


def _required_tools_for_user_intent(
    text: str,
    *,
    full_read_text: str | None = None,
    situation: RunSituation | None = None,
    notes: list[str] | None = None,
) -> set[str]:
    """返回本轮必须执行的工具；被状态否决的规则把说明追加到 notes。

    否决必须留痕：静默撤销会让模型读到「消息里提了精读却没有任何动静」，
    今天就见过它为此反复推敲、原地打转。把原因递出去它才能自己改对（见 #23 的经验）。
    """
    value = str(text or "")
    # 全文精读的判定来源可以与其它意图分开：交互问题文案由助手撰写，
    # 里面复述已保存设置的「需要全文精读」不是用户诉求，不能据此强制拉起精读（见 #1）。
    full_read_value = value if full_read_text is None else str(full_read_text or "")
    required: set[str] = set()
    cancel_requested = bool(
        _EVALUATION_CANCEL_INTENT.search(value)
        and not _EVALUATION_CAPABILITY_QUESTION.search(value)
    )
    if cancel_requested:
        required.add("cancel_evaluation")
    elif _EVALUATION_EXECUTION_INTENT.search(value):
        required.add("run_evaluation")
    if _EVALUATION_RESULT_INTENT.search(value):
        required.add("get_evaluation_results")
    full_read_prohibited = bool(
        _FULL_READ_PROHIBITED.search(full_read_value)
        or _FULL_READ_PROHIBITED_BROAD.search(full_read_value)
    )
    full_read_satisfied = bool(_FULL_READ_ALREADY_SATISFIED.search(full_read_value))
    if _FULL_READ_INTENT.search(full_read_value) and not full_read_prohibited and not full_read_satisfied:
        if _FULL_READ_RULE.verdict(situation or RunSituation()) is Verdict.DENY:
            if notes is not None:
                notes.append(_FULL_READ_RULE.unmet_note)
        else:
            required.update(_full_read_required_tools(situation or RunSituation()))
    elif _MATERIAL_DIRECTIVE_INTENT.search(value):
        required.add("set_material_directive")
    if _document_rule_change_requested(value):
        # 点名文档后必须先取证并建计划；已有文档不一定有“修订影响”记录，
        # 因此不能把直接读取强制送入只适用于上传修订的工具。
        required.update({"read_document", "create_change_plan"})
    if _PACKAGE_FUSION_INTENT.search(value):
        required.update({"list_package_sources", "list_package_fusion_plans"})
    # 台账意图正则已退役（0.26.9.2 死锁事故）：措辞不再产生台账必需工具或
    # 静默策略写入。对账诉求由助手经专家确认卡启用门禁，核心侧确定性把关。
    if _approved_plan_execution_requested(value):
        required.update(APPROVED_PLAN_REQUIRED_TOOLS)
    return required


def _claims_active_evaluation(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    patterns = (
        r"(?:评测|测评|测试).{0,12}(?:仍|还|正在|当前|目前).{0,8}(?:运行|执行|处理中)",
        r"(?:评测|测评|测试).{0,12}(?:尚未|还未|没有)(?:结束|完成)",
        r"等待.{0,12}(?:评测|测评|测试|本轮).{0,12}(?:结束|完成)",
        r"(?:评测|测评|测试).{0,8}(?:异步|后台)(?:运行|执行)中",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _claims_terminal_evaluation(value: str) -> bool:
    return bool(re.search(
        r"(?:最近一轮)?正式评测(?:已完成|已停止|已结束)",
        str(value or ""),
    ))


def _evaluation_terminal_summary(evaluation: dict[str, Any]) -> str:
    status = str(evaluation.get("status") or "failed")
    done = max(0, int(evaluation.get("done") or 0))
    total = max(0, int(evaluation.get("total") or 0))
    passed = max(0, int(evaluation.get("passed") or 0))
    if status == "done":
        return f"正式评测已完成：已执行 {done}/{total} 条，通过 {passed} 条。"
    if status == "cancelled":
        return f"正式评测已停止：已执行 {done}/{total} 条，通过 {passed} 条。"
    return f"正式评测已结束，运行失败：已执行 {done}/{total} 条，通过 {passed} 条。"


def _evaluation_terminal_continuation(
    evaluation: dict[str, Any],
) -> tuple[str, set[str]] | None:
    status = str(evaluation.get("status") or "failed")
    if status == "cancelled":
        return None
    round_id = str(evaluation.get("roundId") or "").strip()
    done = max(0, int(evaluation.get("done") or 0))
    total = max(0, int(evaluation.get("total") or 0))
    passed = max(0, int(evaluation.get("passed") or 0))
    evaluation_completed = status == "done" and total > 0
    all_cases_passed = done >= total and passed >= total
    if evaluation_completed and all_cases_passed:
        return (
            "服务端已收到正式评测全量通过的终态事件。"
            "这不是用户对话消息，不得伪造用户发言，也不得重新运行或轮询评测。"
            "立即发起正式发布确认，让用户决定是否发布；确认前不得自行发布。"
            "不要向用户复述工具名、参数、轮次编号或其他内部标识。",
            {"request_release"},
        )
    return (
        "服务端已收到正式评测未全量通过或运行失败的终态事件。"
        "这不是用户对话消息，不得伪造用户发言，也不得重新运行或轮询评测。"
        "只读取一次事件指定轮次的真实评测结果并据此归因；如果全部用例都是运行错误，"
        "只能报告环境阻断，不得修改知识包、映射、用例或参考答案。"
        "不要向用户复述工具名、参数、轮次编号或其他内部标识。\n"
        f"指定轮次：{round_id}",
        {"get_evaluation_results"},
    )


def _evaluation_event_is_stale(
    incoming: dict[str, Any],
    latest: dict[str, Any],
) -> bool:
    incoming_at = _evaluation_finished_at(incoming)
    latest_at = _evaluation_finished_at(latest)
    if latest_at is None:
        return False
    if incoming_at is None:
        return True
    return incoming_at <= latest_at


def _evaluation_finished_at(evaluation: dict[str, Any]) -> datetime | None:
    value = str(evaluation.get("finishedAt") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
