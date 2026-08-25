from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail, InterruptRequest

from .tools import (
    AskTool,
    CandidateValidationRepairState,
    EventSink,
    RequestConfirmationTool,
    RequestReleaseTool,
    ToolContext,
    input_dict,
    tool_spec,
    tool_work_detail,
)


_EXPLICIT_DECISION_REQUESTS = (
    re.compile(r"请(?:从[^。！？\n]{0,80}中)?选(?:择)?"),
    re.compile(r"供(?:您|你)?选择"),
    re.compile(r"需要(?:您|你)(?:来)?选择"),
    re.compile(r"以下(?:有|提供)?(?:[0-9一二三四五六七八九十两]+个?)?(?:互斥)?选项"),
)

_FREE_TEXT_REQUESTS = (
    re.compile(r"请(?:您|你)?(?:直接)?(?:告诉|告知|描述|说明|补充|输入|填写|提供)"),
    re.compile(r"(?:您|你)可以直接(?:告诉|告知|描述|说明|补充|输入|填写|提供)"),
    re.compile(r"等待(?:您|你)(?:进一步)?(?:描述|说明|补充|输入|填写|提供)"),
)

_CONFIRMATION_CARD_CLAIMS = (
    re.compile(r"确认卡(?:片)?[^。！？\n]{0,8}(?:已|已经|现已)(?:成功)?(?:生成|创建|展示|发出|提交)"),
    re.compile(r"(?:已|已经|现已)(?:成功)?(?:生成|创建|展示|发出|提交)[^。！？\n]{0,8}确认卡(?:片)?"),
)

MERMAID_SOURCE_VALIDATOR_VERSION = "mermaid-source-v1"


def requests_explicit_decision(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(compact and any(pattern.search(compact) for pattern in _EXPLICIT_DECISION_REQUESTS))


def requests_user_input(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(compact and (
        requests_explicit_decision(compact)
        or any(pattern.search(compact) for pattern in _FREE_TEXT_REQUESTS)
    ))


def claims_confirmation_card(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(compact and any(pattern.search(compact) for pattern in _CONFIRMATION_CARD_CLAIMS))


def _response_text(response: Any) -> str:
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, Mapping):
            parts.append(str(item.get("text") or item.get("content") or ""))
        else:
            parts.append(str(getattr(item, "text", "") or getattr(item, "content", "")))
    return "".join(parts)


def _call_args(ctx: AgentCallbackContext) -> dict[str, Any]:
    raw = getattr(ctx.inputs, "tool_args", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    call = getattr(ctx.inputs, "tool_call", None)
    return input_dict(getattr(call, "arguments", {}))


def _tool_name(ctx: AgentCallbackContext) -> str:
    return str(getattr(ctx.inputs, "tool_name", "") or "")


def _tool_call_id(ctx: AgentCallbackContext) -> str:
    call = getattr(ctx.inputs, "tool_call", None)
    return str(getattr(call, "id", "") or "")


def _skip_tool(ctx: AgentCallbackContext, result: dict[str, Any]) -> None:
    rendered = json.dumps(result, ensure_ascii=False)
    ctx.extra["_skip_tool"] = True
    ctx.inputs.tool_result = rendered
    ctx.inputs.tool_msg = ToolMessage(content=rendered, tool_call_id=_tool_call_id(ctx))


def _finding(
    rule_id: str,
    path: str,
    message: str,
    suggestion: str,
    *,
    actual: Any = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ruleId": rule_id,
        "path": path,
        "message": message,
        "suggestion": suggestion,
    }
    if actual is not None:
        result["actual"] = actual
    return result


def _validation_report(
    error: str,
    validator_version: str,
    findings: list[dict[str, Any]],
    *,
    instruction: str,
    display: str,
    retryable: bool = True,
    next_steps: list[str] | None = None,
    repair_scope: str = "candidate",
) -> dict[str, Any]:
    return {
        "error": error,
        "reason": "；".join(str(item.get("message") or "") for item in findings if item.get("message")),
        "retryable": retryable,
        "validatorVersion": validator_version,
        "repairScope": repair_scope,
        "findingCount": len(findings),
        "findings": findings,
        "instruction": instruction,
        "next": list(next_steps or []),
        "tracePhase": "repairing" if retryable else "blocked",
        "display": {"text": display},
    }


def _ensure_validation_report(
    raw: Mapping[str, Any],
    *,
    validator_version: str,
    rule_id: str,
    path: str,
    suggestion: str,
    retryable: bool,
    repair_scope: str = "candidate",
) -> dict[str, Any]:
    result = dict(raw)
    fallback_message = str(result.get("reason") or result.get("error") or "确定性校验未通过")
    raw_findings = result.get("findings")
    findings: list[dict[str, Any]] = []
    for raw_finding in raw_findings if isinstance(raw_findings, list) else []:
        source = dict(raw_finding) if isinstance(raw_finding, Mapping) else {}
        normalized = {
            **source,
            "ruleId": str(source.get("ruleId") or rule_id),
            "path": str(source.get("path") or path),
            "message": str(source.get("message") or raw_finding or fallback_message),
            "suggestion": str(source.get("suggestion") or suggestion),
        }
        findings.append(normalized)
    if not findings:
        findings = [_finding(
            rule_id,
            path,
            fallback_message,
            suggestion,
        )]
    raw_retryable = result.get("retryable")
    effective_retryable = raw_retryable if isinstance(raw_retryable, bool) else retryable
    result.update({
        "retryable": effective_retryable,
        "validatorVersion": str(result.get("validatorVersion") or validator_version),
        "repairScope": str(result.get("repairScope") or repair_scope),
        "findingCount": len(findings),
        "findings": findings,
        "tracePhase": str(
            result.get("tracePhase")
            or ("repairing" if effective_retryable else "blocked")
        ),
    })
    return result


def _contract_validator_version(
    context: ToolContext | None,
    name: str,
    fallback: str,
) -> str:
    validators = context.package_contract.get("validators") if context else None
    if not isinstance(validators, Mapping):
        return fallback
    return str(validators.get(name) or fallback)


class WriteSafetyRail(AgentRail):
    priority = 100
    WRITE_TOOLS = frozenset({
        "write_file", "delete_file", "import_zip", "stage_package_merge",
        "compile_package_fusion_plan", "discard_rejected_candidate_changes",
    })
    RECOVERY_TOOLS = frozenset({"delete_file", "discard_rejected_candidate_changes"})

    def __init__(self, context: ToolContext) -> None:
        self._context = context
        self._emit = context.emit

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        name = _tool_name(ctx)
        if name not in self.WRITE_TOOLS:
            return
        repair_recovery = (
            self._context.validation_repair.stopped and name in self.RECOVERY_TOOLS
        )
        if self._context.validation_repair.stopped and not repair_recovery:
            result = self._context.validation_repair.blocked_result()
            if not self._context.validation_repair.claim_stop_notice():
                result["suppressTrace"] = True
            _skip_tool(ctx, result)
            return
        args = _call_args(ctx)
        mermaid_findings = _mermaid_source_findings(name, args)
        if mermaid_findings:
            result = self._context.validation_repair.decorate({
                "error": "MERMAID_CONTENT_INVALID",
                "reason": "Mermaid 候选源码校验未通过",
                "retryable": True,
                "validatorVersion": MERMAID_SOURCE_VALIDATOR_VERSION,
                "findingCount": len(mermaid_findings),
                "findings": mermaid_findings,
                "next": ["按 findings 删除代码围栏并修正流程图声明后，重新写入同一候选文件"],
                "display": {"text": "流程图源码格式需要自动修正"},
            })
            self._emit("tool", {"name": name, "phase": "rejected", "summary": result["display"]["text"]})
            _skip_tool(ctx, result)
            return
        goal_documents: list[Mapping[str, Any]] = []
        if name == "write_file" and str(args.get("path") or "") == ".modeling/goal.md":
            inventory = await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="list_documents",
                arguments={},
            )
            raw_documents = inventory.get("documents") if isinstance(inventory, Mapping) else None
            goal_documents = [item for item in raw_documents or [] if isinstance(item, Mapping)]
        goal_evidence_findings = _goal_evidence_findings(name, args, goal_documents)
        if goal_evidence_findings:
            result = _validation_report(
                "GOAL_EVIDENCE_LINK_REQUIRED",
                _contract_validator_version(self._context, "goalEvidence", "goal-evidence-v2"),
                goal_evidence_findings,
                instruction=(
                    "先调用 list_documents，使用资料 id 组成 material:<资料 id>#<原文锚点>；"
                    "把 evidence 中每个完整 token 原样写进建模目标里对应的来源或事实正文，再重新写入。"
                    "资料 id 只能出现在 material token 内，不能写成标题或正文。"
                    "不要只写文件名，也不要编造原文位置；未完成精读的资料请先精读定位。"
                ),
                display="建模目标中的资料依据还不能打开原文",
                next_steps=["逐项处理 findings 后重新写入 .modeling/goal.md"],
            )
            result = self._context.validation_repair.decorate(result)
            _skip_tool(ctx, result)
            return
        diagram_evidence_findings = _diagram_evidence_findings(name, args)
        if diagram_evidence_findings:
            result = _validation_report(
                "DIAGRAM_EVIDENCE_SPREAD_REQUIRED",
                _contract_validator_version(self._context, "diagramEvidence", "diagram-evidence-v2"),
                diagram_evidence_findings,
                instruction=(
                    "按关键分区检索原文，把每个分区实际命中的 material:<资料 id>#<原文位置> "
                    "同时写入紧邻分区的 evidence 注释和 write_file.evidence。"
                    "多分区流程不能全部引用同一个开头位置；找不到时明确标为待定位。"
                ),
                display="流程图中的资料依据还不足以逐段复核",
                next_steps=["逐项处理 findings 后重新写入流程图"],
            )
            result = self._context.validation_repair.decorate(result)
            _skip_tool(ctx, result)
            return
        problems = check_write_safety(name, args)
        unsafe = bool(problems)
        if not problems and not repair_recovery and self._context.state_action is not None:
            state = await self._context.state_action("get_project_state", {})
            phase_problem = _phase_write_problem(name, args, str(state.get("phase") or ""))
            if phase_problem:
                problems.append(phase_problem)
        if not problems:
            return
        path = str(args.get("path") or "")
        findings = [
            _finding(
                _write_problem_rule_id(problem, unsafe=unsafe),
                path,
                problem,
                (
                    "改用符合工具 schema 的安全包内路径或资源 key"
                    if unsafe else
                    "先完成当前阶段的确认或状态转换，再修改该路径"
                ),
                actual={"tool": name, "phase": str(state.get("phase") or "") if not unsafe else None},
            )
            for problem in problems
        ]
        result = _validation_report(
            "WRITE_UNSAFE" if unsafe else "WRITE_PHASE_MISMATCH",
            _contract_validator_version(self._context, "writeBoundary", "write-boundary-v2"),
            findings,
            instruction=(
                "修正 findings 中的路径或参数后重试。"
                if unsafe else
                "先完成对应阶段转换，再重试同一候选写入。"
            ),
            display="已阻止不安全的候选写入" if unsafe else "这项修改需要先完成当前阶段",
            next_steps=[
                "改用不含特殊字符的包内相对路径或资源 key 后重试"
                if unsafe else "先完成当前阶段的人工确认；Agent 会在进入对应业务阶段后自动继续"
            ],
            repair_scope="recovery" if repair_recovery else "candidate",
        )
        if not repair_recovery:
            result = self._context.validation_repair.decorate(result)
        _skip_tool(ctx, result)


def _mermaid_source_findings(tool: str, arguments: dict[str, Any]) -> list[dict[str, str]]:
    path = str(arguments.get("path") or "").strip()
    if tool != "write_file" or not re.fullmatch(r"diagrams/[^/]+\.mmd", path):
        return []
    content = str(arguments.get("content") or "").lstrip("\ufeff")
    lines = content.splitlines()
    findings: list[dict[str, str]] = []
    if any(re.match(r"^\s*(?:`{3,}|~{3,})", line) for line in lines):
        findings.append({
            "ruleId": "mermaid.no-markdown-fence",
            "path": path,
            "message": f"{path} 必须保存裸 Mermaid 源码，不能包含 Markdown 代码围栏",
            "suggestion": "删除开头的 ```mermaid 和结尾的 ``` 等代码围栏，只保留 Mermaid 源码正文",
        })
    declaration = next((line.strip() for line in lines if line.strip() and not line.strip().startswith("%%")), "")
    if declaration not in {"flowchart TD", "flowchart LR"}:
        findings.append({
            "ruleId": "mermaid.flowchart-declaration",
            "path": path,
            "message": f"{path} 的首个有效行必须是 flowchart TD 或 flowchart LR",
            "suggestion": "保留前置 %% 注释，并让首个非空、非注释行使用 flowchart TD 或 flowchart LR",
        })
    return findings


def _phase_write_problem(tool: str, arguments: dict[str, Any], phase: str) -> str:
    if tool == "compile_package_fusion_plan":
        # 批量融合只消费只读来源和已裁决账本，并生成整体待确认候选；
        # 它不是资料驱动的分阶段建模，不能要求用户先补文档走完整流程。
        return ""
    if tool == "stage_package_merge":
        return "来源知识包只能在知识建模阶段生成融合候选" if phase != "knowledge" else ""
    if tool == "import_zip":
        return ""
    path = str(arguments.get("path") or "").strip()
    if not path or (path.startswith(".modeling/") and path != ".modeling/goal.md"):
        return ""
    if path == ".modeling/goal.md":
        expected = "goal"
        label = "目标确认"
    elif path.startswith("diagrams/"):
        expected = "process"
        label = "流程建模"
    elif path.startswith("scripts/"):
        expected = "validation"
        label = "确定性验证"
    elif re.match(r"^(?:objects|operations|rules|metrics|mappings)/", path) or path in {"manifest.yaml", "index.md"}:
        expected = "knowledge"
        label = "知识建模"
    else:
        return f"候选路径不属于已注册的业务目录：{path}"
    return "" if phase == expected else f"《{path}》只能在{label}阶段修改"


class InterjectionSafetyRail(AgentRail):
    priority = 110
    MUTATING_TOOLS = frozenset({
        "write_file", "delete_file", "stage_package_merge", "regroup_changes", "update_plan",
        "discard_rejected_candidate_changes",
        "create_package_fusion_plan", "update_package_fusion_plan", "compile_package_fusion_plan",
        "update_project_context", "transition_project_state",
        "run_script_test", "run_evaluation",
        "ask", "request_confirmation", "request_release",
    })

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        name = _tool_name(ctx)
        if not self._context.interjection_reply_required or name not in self.MUTATING_TOOLS:
            return
        result = _validation_report(
            "INTERJECTION_READ_ONLY",
            _contract_validator_version(self._context, "interactionBoundary", "interaction-boundary-v1"),
            [_finding(
                "interaction.interjection.read_only",
                "",
                "回答用户插话的小回合不能改变候选或项目状态",
                "直接以最终自然语言回答并结束本轮；服务端随后恢复原问题",
                actual=name,
            )],
            instruction="直接以最终自然语言回答并结束本轮；服务端随后恢复原问题。",
            display="插话回答保持只读，原问题仍然有效",
            retryable=False,
            repair_scope="interaction",
        )
        result["suppressTrace"] = True
        _skip_tool(ctx, result)


class ToolBoundaryRail(AgentRail):
    """Replay a completed OpenJiuwen tool boundary without submitting it twice."""

    priority = 105

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.extra.get("_skip_tool") or self._context.tool_receipt_lookup is None:
            return
        call_id = _tool_call_id(ctx)
        if not call_id:
            return
        result = self._context.tool_receipt_lookup(_tool_name(ctx), call_id, _call_args(ctx))
        if result is None:
            return
        ctx.extra["_tool_receipt_replayed"] = True
        _skip_tool(ctx, result)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        if (
            ctx.extra.get("_tool_receipt_replayed")
            or self._context.tool_receipt_save is None
        ):
            return
        call_id = _tool_call_id(ctx)
        if not call_id:
            return
        result = _result_dict(getattr(ctx.inputs, "tool_result", None))
        if result.get("error"):
            return
        self._context.tool_receipt_save(_tool_name(ctx), call_id, _call_args(ctx), result)


class DecisionCardSafetyRail(AgentRail):
    """Remember whether this invoke has visibly asked the user to choose."""

    priority = 95

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        response = getattr(ctx.inputs, "response", None)
        text = _response_text(response)
        if requests_explicit_decision(text):
            self._context.decision_request_in_text = True
        if requests_user_input(text):
            self._context.user_input_request_in_text = True
        if claims_confirmation_card(text):
            self._context.confirmation_claimed_in_text = True


class AskRail(BaseInterruptRail):
    priority = 90

    def __init__(self, context: ToolContext | None = None) -> None:
        super().__init__(["ask"])
        self._context = context

    async def resolve_interrupt(
        self,
        _ctx: AgentCallbackContext,
        tool_call: Any,
        user_input: Any,
        auto_confirm_config: dict | None = None,
    ):
        del auto_confirm_config
        arguments = _arguments_from_call(tool_call)
        if user_input is None:
            if (
                self._context
                and self._context.confirmation_required
                and not self._context.user_input_request_in_text
            ):
                result = _validation_report(
                    "OVERALL_CONFIRMATION_REQUIRED",
                    _contract_validator_version(self._context, "interactionBoundary", "interaction-boundary-v1"),
                    [_finding(
                        "interaction.overall_confirmation.required",
                        "",
                        "用户已经提交这批修改进入整体复核，不需要再回答普通问题",
                        "完成真实差异核对后立即发起整体确认",
                    )],
                    instruction="不要提问；完成真实差异核对后立即发起整体确认。",
                    display="正在转入整体修改确认",
                    repair_scope="interaction",
                )
                return self.reject(tool_result=json.dumps(result, ensure_ascii=False))
            if self._context and self._context.interjection_reply_required:
                return self.reject(tool_result=json.dumps({
                    "direct_answer_required": True,
                    "instruction": "不要再次提问。请直接以最终自然语言回答用户插话并结束本轮，服务端会恢复原问题。",
                }, ensure_ascii=False))
            if self._context:
                self._context.decision_card_requested = True
            kind = str(arguments.get("kind") or "clarification")
            options = _normalized_options(arguments.get("options"), kind)
            request = InterruptRequest(
                message=str(arguments.get("question") or "需要你补充一个决定"),
                payload_schema={
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "selected": {"type": "array", "items": {"type": "string"}},
                        "action": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": True,
                },
                ui_options=options,
                metadata={
                    "kind": kind,
                    "question": arguments.get("question"),
                    "evidence": arguments.get("evidence") or [],
                    "allowFreeText": True,
                    "overallConfirmationRequired": bool(
                        self._context and self._context.confirmation_required
                    ),
                },
            )
            return self.interrupt(request)
        if isinstance(user_input, Mapping) and user_input.get("action") == "interject" and self._context:
            self._context.interjection_reply_required = True
        return self.reject(
            tool_result=json.dumps(
                {
                    "answer": user_input,
                    "instruction": "若这是插话，请直接以最终自然语言回答并结束本轮；服务端会恢复原问题。",
                },
                ensure_ascii=False,
            )
        )


class ConfirmationRail(BaseInterruptRail):
    priority = 90

    def __init__(self, context: ToolContext) -> None:
        super().__init__(["request_confirmation"])
        self._context = context

    async def resolve_interrupt(
        self,
        _ctx: AgentCallbackContext,
        tool_call: Any,
        user_input: Any,
        auto_confirm_config: dict | None = None,
    ):
        del auto_confirm_config
        arguments = _arguments_from_call(tool_call)
        if user_input is None:
            if self._context.user_input_request_in_text and not self._context.decision_card_requested:
                free_text = not self._context.decision_request_in_text
                message = (
                    "本轮正文正在要求用户补充说明，不能同时改用候选确认承载这个输入"
                    if free_text else
                    "本轮正文正在要求用户从互斥业务选项中选择，不能同时改用候选确认承载这个决定"
                )
                suggestion = (
                    "立即调用 ask，把同一补充请求放入可输入的问题卡"
                    if free_text else
                    "立即调用 ask，把同一问题整理成少量互斥的业务选项，并保留自由输入"
                )
                result = _validation_report(
                    "USER_INPUT_CARD_REQUIRED" if free_text else "BUSINESS_DECISION_CARD_REQUIRED",
                    _contract_validator_version(self._context, "interactionBoundary", "interaction-boundary-v1"),
                    [_finding(
                        "interaction.input_card.required" if free_text else "interaction.decision_card.required",
                        "",
                        message,
                        suggestion,
                    )],
                    instruction=f"{suggestion}。",
                    display="正在改用合适的问题卡",
                    repair_scope="interaction",
                )
                return self.reject(tool_result=json.dumps(result, ensure_ascii=False))
            if self._context.confirmation_guard is not None and not self._context.confirmation_guard():
                result = _validation_report(
                    "CONFIRMATION_EVIDENCE_REQUIRED",
                    _contract_validator_version(self._context, "confirmationBoundary", "confirmation-boundary-v1"),
                    [_finding(
                        "confirmation.diff_review.required",
                        "candidate",
                        "整体变更对照尚未完成",
                        "读取真实候选差异并定位整体对照后重新发起确认",
                    )],
                    instruction="先读取真实候选差异并定位整体对照，再重新发起确认。",
                    display="正在补充整体变更对照",
                    repair_scope="interaction",
                )
                return self.reject(tool_result=json.dumps(result, ensure_ascii=False))
            if self._context.interjection_reply_required:
                return self.reject(tool_result=json.dumps({
                    "direct_answer_required": True,
                    "instruction": "不要再次发起确认。请直接以最终自然语言回答用户插话并结束本轮，服务端会恢复原确认。",
                }, ensure_ascii=False))
            prepared = await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="prepare_confirmation_groups",
                arguments={},
            )
            if prepared.get("error"):
                prepared = _ensure_validation_report(
                    prepared,
                    validator_version="confirmation-groups-v1",
                    rule_id="confirmation.groups.prepare_failed",
                    path="candidate",
                    suggestion="按 findings 修正候选分组后重新准备确认",
                    retryable=True,
                )
                if prepared.get("retryable") is True:
                    prepared = self._context.validation_repair.decorate(prepared)
                return self.reject(tool_result=json.dumps(prepared, ensure_ascii=False))
            groups = prepared.get("change_groups") if isinstance(prepared, Mapping) else None
            if not isinstance(groups, list) or not groups:
                result = _validation_report(
                    "CONFIRMATION_GROUPS_UNAVAILABLE",
                    "confirmation-groups-v1",
                    [_finding(
                        "confirmation.groups.required",
                        "candidate",
                        "没有可供确认的候选修改",
                        "先完成候选写入，再重新发起确认",
                    )],
                    instruction="先完成候选写入，再重新发起确认；不要改用普通提问代替候选确认。",
                    display="还没有可供确认的候选修改",
                    repair_scope="interaction",
                )
                return self.reject(tool_result=json.dumps(result, ensure_ascii=False))
            normalized_arguments = {**arguments, "change_groups": groups}
            preflight = await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="request_confirmation_preflight",
                arguments=normalized_arguments,
            )
            if preflight.get("error"):
                preflight = _ensure_validation_report(
                    preflight,
                    validator_version="confirmation-preflight-v1",
                    rule_id="confirmation.preflight.failed",
                    path="candidate",
                    suggestion="完整处理 findings 后重新发起确认",
                    retryable=True,
                )
                if preflight.get("retryable") is True:
                    preflight = self._context.validation_repair.decorate(preflight)
                return self.reject(tool_result=json.dumps(preflight, ensure_ascii=False))
            self._context.validation_repair.reset()
            validation_receipt = preflight.get("validation_receipt")
            receipt_arguments = {
                "expected_candidate_revision": preflight.get("candidate_revision"),
                "expected_candidate_hash": (
                    validation_receipt.get("candidateHash")
                    if isinstance(validation_receipt, Mapping) else ""
                ),
                "expected_validator_version": (
                    validation_receipt.get("validatorVersion")
                    if isinstance(validation_receipt, Mapping) else ""
                ),
            }
            if preflight.get("auto_approved") is True:
                automatic_review = {
                    "approved": True,
                    "review_mode": "automatic",
                    "selected_groups": [
                        str(group.get("id"))
                        for group in preflight.get("change_groups") or []
                        if isinstance(group, Mapping) and group.get("id")
                    ],
                    "sampled_groups": [],
                }
                return self.approve(new_args=json.dumps({
                    **normalized_arguments,
                    **receipt_arguments,
                    "review": automatic_review,
                }, ensure_ascii=False))
            groups = preflight.get("change_groups") or groups
            focus = preflight.get("focus")
            if isinstance(focus, Mapping):
                self._context.emit("focus", {"target": dict(focus)})
            return self.interrupt(
                InterruptRequest(
                    message=str(arguments.get("summary") or "请确认这批待修改内容"),
                    payload_schema={
                        "type": "object",
                        "properties": {
                            "approved": {"type": "boolean"},
                            "review_mode": {"type": "string", "enum": ["full", "sequential", "sampled"]},
                            "selected_groups": {"type": "array", "items": {"type": "string"}},
                            "sampled_groups": {"type": "array", "items": {"type": "string"}},
                            "feedback": {"type": "string"},
                        },
                        "required": ["approved"],
                        "additionalProperties": False,
                    },
                    metadata={
                        "kind": "confirmation",
                        "summary": arguments.get("summary"),
                        "changeGroups": groups,
                        "candidateRevision": preflight.get("candidate_revision"),
                        "validationReceipt": validation_receipt,
                    },
                )
            )
        answer = user_input if isinstance(user_input, Mapping) else {"approved": False, "feedback": str(user_input)}
        if answer.get("action") == "settled_confirmation":
            settled = answer.get("result")
            result = dict(settled) if isinstance(settled, Mapping) else _validation_report(
                "CONFIRMATION_RESULT_MISSING",
                "confirmation-settlement-v1",
                [_finding(
                    "confirmation.result.missing",
                    "candidate",
                    "候选结算结果没有恢复",
                    "重新校验当前候选并生成新的确认卡",
                )],
                instruction="重新校验当前候选并生成新的确认卡。",
                display="确认结果需要重新校验",
                retryable=False,
                repair_scope="interaction",
            )
            if result.get("error"):
                result = _ensure_validation_report(
                    result,
                    validator_version="confirmation-settlement-v1",
                    rule_id="confirmation.settlement.failed",
                    path="candidate",
                    suggestion="根据 findings 重新校验候选并发起新的确认",
                    retryable=False,
                    repair_scope="interaction",
                )
            return self.reject(tool_result=json.dumps(result, ensure_ascii=False))
        if answer.get("action") == "interject":
            # 自由输入是对确认问题的插话，不是拒绝。候选仍然冻结，
            # 模型只回答这次插话；服务端随后恢复原确认。
            self._context.interjection_reply_required = True
            return self.reject(
                tool_result=json.dumps(
                    {
                        "interjection": str(answer.get("text") or ""),
                        "instruction": "直接以最终自然语言回答用户插话并结束本轮；不要再次调用 request_confirmation，服务端会恢复仍然有效的原确认。",
                    },
                    ensure_ascii=False,
                )
            )
        if answer.get("approved") is not True:
            feedback = str(answer.get("feedback") or "专家退回了这批候选，请根据反馈调整后重新确认")
            await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="request_confirmation_rejected",
                arguments={**arguments, "review": dict(answer)},
            )
            return self.reject(tool_result=json.dumps({"approved": False, "feedback": feedback}, ensure_ascii=False))
        merged = {**arguments, "review": dict(answer)}
        return self.approve(new_args=json.dumps(merged, ensure_ascii=False))


class ReleaseConfirmationRail(BaseInterruptRail):
    priority = 90

    def __init__(self, context: ToolContext) -> None:
        super().__init__(["request_release"])
        self._context = context

    async def resolve_interrupt(
        self,
        _ctx: AgentCallbackContext,
        tool_call: Any,
        user_input: Any,
        auto_confirm_config: dict | None = None,
    ):
        del auto_confirm_config
        arguments = _arguments_from_call(tool_call)
        if user_input is None:
            if self._context.release_declined:
                return self.reject(tool_result=json.dumps({
                    "approved": False,
                    "release_declined": True,
                    "instruction": "项目负责人已选择继续补充用例。不得在本轮再次发起发布确认；简短说明已暂缓发布并结束本轮。",
                }, ensure_ascii=False))
            if self._context.interjection_reply_required:
                return self.reject(tool_result=json.dumps({
                    "direct_answer_required": True,
                    "instruction": "不要再次发起发布确认。请直接回答用户插话并结束本轮，服务端会恢复原确认。",
                }, ensure_ascii=False))
            preflight = await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="release_preflight",
                arguments={},
            )
            if preflight.get("error") or not preflight.get("ready"):
                if not preflight.get("error"):
                    preflight = {
                        **preflight,
                        "error": "RELEASE_PREFLIGHT_BLOCKED",
                        "reason": str(preflight.get("reason") or "发布前置条件尚未满足"),
                    }
                preflight = _ensure_validation_report(
                    preflight,
                    validator_version="release-preflight-v1",
                    rule_id="release.preflight.blocked",
                    path="release",
                    suggestion="完成 findings 指向的评测或发布前置条件后重试",
                    retryable=False,
                    repair_scope="release",
                )
                preflight["tracePhase"] = "blocked"
                return self.reject(tool_result=json.dumps(preflight, ensure_ascii=False))
            if preflight.get("already_released") is True:
                review = {
                    "approved": True,
                    "coverage_confirmed": True,
                    "version": preflight.get("version"),
                    "restored_from_audit": True,
                }
                return self.approve(new_args=json.dumps({
                    **arguments, "version": preflight.get("version"), "review": review,
                }, ensure_ascii=False))
            self._context.emit("focus", {"target": {"kind": "evaluation", "target": "正式评测"}})
            version = str(preflight.get("version") or "")
            count = int(preflight.get("testcase_count") or 0)
            return self.interrupt(InterruptRequest(
                message=f"确认 {count} 条真实用例已覆盖发布范围，并正式发布 {version}",
                payload_schema={
                    "type": "object",
                    "properties": {
                        "approved": {"type": "boolean"},
                        "coverage_confirmed": {"type": "boolean"},
                        "version": {"type": "string"},
                        "feedback": {"type": "string"},
                    },
                    "required": ["approved"],
                    "additionalProperties": False,
                },
                metadata={
                    "kind": "release_confirmation",
                    "question": f"是否确认用例覆盖并正式发布 {version}？",
                    "summary": arguments.get("summary"),
                    "version": version,
                    "testcaseCount": count,
                    "questions": preflight.get("questions") or [],
                    "allowFreeText": True,
                },
            ))
        answer = user_input if isinstance(user_input, Mapping) else {"approved": False, "feedback": str(user_input)}
        if answer.get("action") == "interject":
            self._context.interjection_reply_required = True
            return self.reject(tool_result=json.dumps({
                "interjection": str(answer.get("text") or ""),
                "instruction": "直接回答用户插话并结束本轮；不要再次调用 request_release，服务端会恢复发布确认。",
            }, ensure_ascii=False))
        if answer.get("approved") is not True or answer.get("coverage_confirmed") is not True:
            self._context.release_declined = True
            return self.reject(tool_result=json.dumps({
                "approved": False,
                "feedback": str(answer.get("feedback") or "项目负责人选择继续补充真实用例，暂不发布"),
                "instruction": "不得在本轮再次发起发布确认；说明已暂缓发布并结束本轮。",
            }, ensure_ascii=False))
        merged = {
            **arguments,
            "version": str(answer.get("version") or arguments.get("version") or ""),
            "review": dict(answer),
        }
        return self.approve(new_args=json.dumps(merged, ensure_ascii=False))


class ActivityRail(AgentRail):
    priority = 10
    _STATE_KEY = "_modeling_model_call_metrics"
    _INSPECTORS_KEY = "_stream_chunk_inspectors"

    def __init__(
        self,
        emit: EventSink,
        actor: str = "modeling_leader",
        validation_repair: CandidateValidationRepairState | None = None,
    ) -> None:
        self._emit = emit
        self._actor = actor
        self._validation_repair = validation_repair

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        now = time.monotonic()
        call_id = f"model_{uuid.uuid4().hex[:12]}"
        if self._validation_repair is not None:
            self._validation_repair.begin_round(call_id)
        ctx.extra[self._STATE_KEY] = {
            "callId": call_id,
            "startedAt": now,
            "chunks": 0,
            "reasoningChunks": 0,
            "reasoningChars": 0,
            "contentChunks": 0,
            "contentChars": 0,
            "firstChunkAt": None,
            "firstReasoningAt": None,
            "firstContentAt": None,
            "inputMessages": len(getattr(ctx.inputs, "messages", None) or []),
            "inputChars": _messages_size(getattr(ctx.inputs, "messages", None) or []),
            "toolDefinitions": len(getattr(ctx.inputs, "tools", None) or []),
        }
        inspectors = ctx.extra.get(self._INSPECTORS_KEY)
        if not isinstance(inspectors, list):
            inspectors = []
        inspectors = [
            inspector for inspector in inspectors
            if getattr(inspector, "__self__", None) is not self
        ]
        inspectors.append(self.inspect_stream_chunk)
        ctx.extra[self._INSPECTORS_KEY] = inspectors
        self._emit("model_call", {
            "callId": call_id,
            "actor": self._actor,
            "phase": "started",
            "inputMessages": ctx.extra[self._STATE_KEY]["inputMessages"],
            "inputChars": ctx.extra[self._STATE_KEY]["inputChars"],
            "toolDefinitions": ctx.extra[self._STATE_KEY]["toolDefinitions"],
        })

    async def inspect_stream_chunk(self, ctx: AgentCallbackContext, chunk: Any) -> None:
        state = ctx.extra.get(self._STATE_KEY)
        if not isinstance(state, dict):
            return
        now = time.monotonic()
        state["chunks"] += 1
        state["firstChunkAt"] = state["firstChunkAt"] or now
        reasoning = str(getattr(chunk, "reasoning_content", "") or "")
        content = str(getattr(chunk, "content", "") or "")
        if reasoning:
            state["reasoningChunks"] += 1
            state["reasoningChars"] += len(reasoning)
            state["firstReasoningAt"] = state["firstReasoningAt"] or now
        if content:
            state["contentChunks"] += 1
            state["contentChars"] += len(content)
            state["firstContentAt"] = state["firstContentAt"] or now

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        self._finish_model_call(ctx)

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        self._finish_model_call(ctx, error=ctx.exception)

    def _finish_model_call(self, ctx: AgentCallbackContext, error: Exception | None = None) -> None:
        state = ctx.extra.get(self._STATE_KEY)
        if not isinstance(state, dict) or state.get("finished"):
            return
        state["finished"] = True
        response = getattr(ctx.inputs, "response", None)
        messages = getattr(ctx.inputs, "messages", None) or []
        tools = getattr(ctx.inputs, "tools", None) or []
        state["inputMessages"] = len(messages)
        state["inputChars"] = _messages_size(messages)
        state["toolDefinitions"] = len(tools)
        finish_reason = str(getattr(response, "finish_reason", "") or "")
        usage = getattr(response, "usage_metadata", None)
        tool_calls = getattr(response, "tool_calls", None) or []
        if error is not None:
            outcome = "timeout" if _is_timeout_error(error) else "error"
        elif tool_calls:
            outcome = "tool_calls"
        elif state["contentChars"]:
            outcome = "content"
        elif state["reasoningChars"]:
            outcome = "reasoning_only"
        elif not state["chunks"]:
            outcome = "zero_chunk"
        else:
            outcome = "empty_content"
        started_at = float(state["startedAt"])

        def elapsed(value: Any) -> int | None:
            return round((float(value) - started_at) * 1000) if value else None

        self._emit("model_call", {
            "callId": state["callId"],
            "actor": self._actor,
            "phase": "failed" if error is not None else "completed",
            "outcome": outcome,
            "durationMs": round((time.monotonic() - started_at) * 1000),
            "firstChunkMs": elapsed(state["firstChunkAt"]),
            "firstReasoningMs": elapsed(state["firstReasoningAt"]),
            "firstContentMs": elapsed(state["firstContentAt"]),
            "chunks": state["chunks"],
            "reasoningChunks": state["reasoningChunks"],
            "reasoningChars": state["reasoningChars"],
            "contentChunks": state["contentChunks"],
            "contentChars": state["contentChars"],
            "finishReason": finish_reason,
            "inputMessages": state["inputMessages"],
            "inputChars": state["inputChars"],
            "inputTokens": _usage_value(usage, "input_tokens"),
            "outputTokens": _usage_value(usage, "output_tokens"),
            "reasoningTokens": _usage_value(usage, "reasoning_tokens"),
            "errorType": type(error).__name__ if error is not None else "",
        })

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.extra.get("_skip_tool"):
            return
        name = _tool_name(ctx)
        spec = tool_spec(name)
        display = _display_text(spec.display if spec else "正在处理", _call_args(ctx))
        tool_call_id = _tool_call_id(ctx)
        trace_group_id = tool_trace_group_id(name, _call_args(ctx))
        self._emit("progress", {
            "text": display,
            "detail": tool_work_detail(name),
            "counters": {},
        })
        payload = {
            "name": name,
            "phase": "started",
            "summary": display,
            "toolCallId": tool_call_id,
        }
        if trace_group_id:
            payload["traceGroupId"] = trace_group_id
        self._emit("tool", payload)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        name = _tool_name(ctx)
        result = _result_dict(getattr(ctx.inputs, "tool_result", None))
        if result.get("suppressTrace") is True:
            return
        display = result.get("display") if isinstance(result.get("display"), dict) else {}
        summary = str(display.get("text") or result.get("reason") or "处理完成")
        trace_phase = str(result.get("tracePhase") or "")
        if trace_phase:
            phase = trace_phase
        elif result.get("repairStopped") is True:
            phase = "blocked"
        elif result.get("error") and result.get("retryable") is True:
            phase = "repairing"
        else:
            phase = "failed" if result.get("error") else "completed"
        payload = {
            "name": name,
            "phase": phase,
            "summary": summary,
            "toolCallId": _tool_call_id(ctx),
            "code": str(result.get("error") or ""),
            "retryable": result.get("retryable") is True,
            "repairStopped": result.get("repairStopped") is True,
            "repairAttempt": result.get("repairAttempt"),
            "repairRoundId": str(result.get("repairRoundId") or ""),
            "findingCount": int(result.get("findingCount") or 0),
        }
        trace_group_id = tool_trace_group_id(name, _call_args(ctx))
        if trace_group_id:
            payload["traceGroupId"] = trace_group_id
        self._emit("tool", payload)


def _messages_size(messages: list[Any]) -> int:
    total = 0
    for message in messages:
        content = message.get("content", "") if isinstance(message, Mapping) else getattr(message, "content", "")
        if isinstance(content, list):
            total += sum(len(str(item)) for item in content)
        else:
            total += len(str(content or ""))
    return total


def _usage_value(usage: Any, name: str) -> int:
    value = usage.get(name, 0) if isinstance(usage, Mapping) else getattr(usage, name, 0)
    return int(value or 0)


def tool_trace_group_id(name: str, arguments: dict[str, Any]) -> str:
    paged_arguments = {
        "read_document": {"offset", "limit"},
        "read_table_rows": {"offset", "limit"},
    }.get(name)
    if not paged_arguments:
        return ""
    identity = {
        key: value for key, value in arguments.items()
        if key not in paged_arguments
    }
    encoded = json.dumps({"name": name, "identity": identity}, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"paged:{name}:{digest}"


def _is_timeout_error(error: BaseException) -> bool:
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


def check_write_safety(tool: str, arguments: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    path = str(arguments.get("path") or "")
    key = str(arguments.get("key") or "")
    entrypoint = str(arguments.get("entrypoint") or "")

    if path:
        parts = path.split("/")
        if path.startswith("/") or "\\" in path or ".." in parts or any(not part for part in parts):
            problems.append("path 必须是没有空段、反斜杠或 .. 的包内相对路径")
        if any(ord(char) < 32 for char in path):
            problems.append("path 不能包含控制字符")
    if key:
        if ".." in key or re.search(r"[/\\#?&|\x00-\x1f]", key):
            problems.append("资源 key 不能包含 ..、路径符号、URL 符号或控制字符")
    if tool == "import_zip":
        entries = arguments.get("entries") or []
        for entry in entries if isinstance(entries, list) else []:
            name = str(entry)
            parts = name.split("/")
            if name.startswith("/") or "\\" in name or ".." in parts or any(not part for part in parts):
                problems.append(f"zip 条目路径不安全：{name}")
                break
    if entrypoint and (not entrypoint.endswith(".py") or ".." in entrypoint):
        problems.append("脚本 entrypoint 必须是包内 .py 文件且不能包含 ..")
    return problems


def _write_problem_rule_id(problem: str, *, unsafe: bool) -> str:
    if not unsafe:
        return "write.phase.mismatch"
    if problem.startswith("path ") or problem.startswith("候选路径"):
        return "write.path.invalid"
    if problem.startswith("资源 key"):
        return "write.key.invalid"
    if problem.startswith("zip 条目"):
        return "write.zip_entry.invalid"
    if problem.startswith("脚本 entrypoint"):
        return "write.entrypoint.invalid"
    return "write.arguments.unsafe"


def _goal_evidence_findings(
    tool: str,
    arguments: dict[str, Any],
    documents: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if tool != "write_file" or str(arguments.get("path") or "") != ".modeling/goal.md":
        return []
    path = ".modeling/goal.md"
    evidence = arguments.get("evidence")
    evidence_items = evidence if isinstance(evidence, list) else []
    material_tokens = [
        str(item).strip()
        for item in evidence_items
        if str(item).strip().startswith("material:")
    ]
    if documents and not material_tokens:
        return [_finding(
            "goal.evidence.required",
            path,
            "项目已有资料，建模目标至少需要一处可定位的资料原文依据",
            "从 list_documents 返回的资料中选择真实原文位置，并同时写入正文和 evidence",
        )]
    if not material_tokens:
        return []
    content = str(arguments.get("content") or "")
    findings: list[dict[str, Any]] = []
    sources_with_body_tokens: set[str] = set()
    visible_content = content
    for token in material_tokens:
        source, separator, anchor = token.partition("#")
        if not separator or not source.removeprefix("material:").strip() or not anchor.strip():
            findings.append(_finding(
                "goal.evidence.anchor_required",
                path,
                f"资料证据缺少可定位的原文锚点：{token}",
                "使用 material:<资料 id>#<原文锚点> 的完整格式",
                actual=token,
            ))
        elif token not in content:
            findings.append(_finding(
                "goal.evidence.missing_from_body",
                path,
                f"建模目标正文缺少完整资料证据：{token}",
                "把 evidence 中的完整 token 原样放入对应事实正文",
                actual=token,
            ))
        else:
            sources_with_body_tokens.add(source.removeprefix("material:").strip())
            visible_content = visible_content.replace(token, "")
    for document in documents or []:
        filename = str(document.get("filename") or "").strip()
        document_id = str(document.get("id") or "").strip()
        if filename and filename in content and not ({document_id, filename} & sources_with_body_tokens):
            findings.append(_finding(
                "goal.evidence.source_unlocated",
                path,
                f"目标中引用了《{filename}》，但没有标注它的原文位置",
                "为该资料补充正文和 evidence 中一致的 material token",
                actual=filename,
            ))
        if document_id and document_id in visible_content:
            undeclared_tokens = list(dict.fromkeys(re.findall(
                rf"material:{re.escape(document_id)}#[^\s\)\]\}}，。；;]+",
                visible_content,
            )))
            if undeclared_tokens:
                for token in undeclared_tokens:
                    findings.append(_finding(
                        "goal.evidence.not_declared",
                        path,
                        f"正文中的资料引用没有登记到 evidence：{token}",
                        "将该完整 token 加入 write_file.evidence，或从正文删除未使用的引用",
                        actual=token,
                    ))
            else:
                findings.append(_finding(
                    "goal.evidence.internal_id_exposed",
                    path,
                    f"目标正文暴露了《{filename or '项目资料'}》的内部资料编号",
                    "资料 id 只能作为完整 material token 的一部分出现",
                    actual=document_id,
                ))
    return findings


def _diagram_evidence_findings(tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    path = str(arguments.get("path") or "")
    if tool != "write_file" or not re.match(r"^diagrams/[^/]+\.mmd$", path):
        return []
    evidence = arguments.get("evidence")
    evidence_items = evidence if isinstance(evidence, list) else []
    material_tokens = [
        str(item).strip()
        for item in evidence_items
        if str(item).strip().startswith("material:")
    ]
    if not material_tokens:
        return [_finding(
            "diagram.evidence.required",
            path,
            "业务流程图至少需要一处可定位的资料原文依据",
            "检索真实原文，并把完整 material token 同时写入 evidence 和流程图注释",
        )]
    content = str(arguments.get("content") or "")
    findings: list[dict[str, Any]] = []
    anchors: set[str] = set()
    for token in material_tokens:
        source, separator, anchor = token.partition("#")
        if not separator or not source.removeprefix("material:").strip() or not anchor.strip():
            findings.append(_finding(
                "diagram.evidence.anchor_required",
                path,
                f"流程图资料依据缺少可定位的原文位置：{token}",
                "使用 material:<资料 id>#<原文位置> 的完整格式",
                actual=token,
            ))
        elif token not in content:
            findings.append(_finding(
                "diagram.evidence.missing_from_body",
                path,
                f"流程图正文缺少完整资料依据：{token}",
                "把 evidence 中的完整 token 写入对应分区旁的 evidence 注释",
                actual=token,
            ))
        else:
            anchors.add(f"{source}#{anchor.strip()}")
    subgraphs = len(re.findall(r"^\s*subgraph\b", content, flags=re.MULTILINE | re.IGNORECASE))
    evidence_comments = len(re.findall(r"^\s*%%\s*evidence:\s*material:", content, flags=re.MULTILINE | re.IGNORECASE))
    if subgraphs >= 2 and evidence_comments < 2:
        findings.append(_finding(
            "diagram.evidence.section_coverage",
            path,
            "多分区流程图需要在至少两个关键分区旁分别标注资料依据",
            "在至少两个关键 subgraph 内分别添加 %% evidence: material:... 注释",
            actual={"subgraphs": subgraphs, "evidenceComments": evidence_comments},
        ))
    if subgraphs >= 3 and len(anchors) < 2:
        findings.append(_finding(
            "diagram.evidence.anchor_diversity",
            path,
            "多分区流程图不能让所有分区共用同一个原文位置",
            "为不同关键分区定位至少两个真实原文位置",
            actual={"subgraphs": subgraphs, "distinctAnchors": len(anchors)},
        ))
    return findings


def _arguments_from_call(tool_call: Any) -> dict[str, Any]:
    raw = getattr(tool_call, "arguments", {}) if tool_call is not None else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _normalized_options(raw: Any, kind: str) -> list[dict[str, Any]]:
    options = [dict(item) for item in raw or [] if isinstance(item, Mapping)]
    if kind != "material_request":
        return [
            {
                **option,
                **({
                    "requiresText": True,
                    "textPrompt": str(option.get("textPrompt") or "请补充具体说明"),
                } if option.get("requiresText") is True else {}),
            }
            for option in options
        ]
    # 上传已有独立的文件选择器；资料请求里的判断动作必须互斥、稳定，
    # 不让模型生成的同义选项增加用户的判断成本。
    return [
        {
            "label": "我口述",
            "description": "直接说明做法，作为 user-decision 证据继续",
            "value": "oral",
            "requiresText": True,
            "textPrompt": "请口述需要补充的业务背景或做法",
        },
        {"label": "让我先找找", "description": "保留问题，稍后在对话里补充资料", "value": "later"},
        {"label": "跳过", "description": "记录资料缺口，不阻塞其他建模工作", "value": "skip"},
    ]


def _display_text(base: str, arguments: dict[str, Any]) -> str:
    subject = arguments.get("filename") or arguments.get("path") or arguments.get("table")
    return f"{base}《{subject}》" if subject else base


def _result_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
