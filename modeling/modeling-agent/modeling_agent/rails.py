from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail, InterruptRequest

from .tools import AskTool, EventSink, RequestConfirmationTool, RequestReleaseTool, ToolContext, input_dict, tool_spec


_EXPLICIT_DECISION_REQUESTS = (
    re.compile(r"请(?:从[^。！？\n]{0,80}中)?选(?:择)?"),
    re.compile(r"供(?:您|你)?选择"),
    re.compile(r"需要(?:您|你)(?:来)?选择"),
    re.compile(r"以下(?:有|提供)?(?:[0-9一二三四五六七八九十两]+个?)?(?:互斥)?选项"),
)

_CONFIRMATION_CARD_CLAIMS = (
    re.compile(r"确认卡(?:片)?[^。！？\n]{0,8}(?:已|已经|现已)(?:成功)?(?:生成|创建|展示|发出|提交)"),
    re.compile(r"(?:已|已经|现已)(?:成功)?(?:生成|创建|展示|发出|提交)[^。！？\n]{0,8}确认卡(?:片)?"),
)


def requests_explicit_decision(text: str) -> bool:
    compact = str(text or "").strip()
    return bool(compact and any(pattern.search(compact) for pattern in _EXPLICIT_DECISION_REQUESTS))


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


class WriteSafetyRail(AgentRail):
    priority = 100
    WRITE_TOOLS = frozenset({
        "write_file", "delete_file", "import_zip", "stage_package_merge",
        "compile_package_fusion_plan", "discard_rejected_candidate_changes",
    })

    def __init__(self, context: ToolContext) -> None:
        self._context = context
        self._emit = context.emit

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        name = _tool_name(ctx)
        if name not in self.WRITE_TOOLS:
            return
        args = _call_args(ctx)
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
        goal_evidence_problems = _goal_evidence_problems(name, args, goal_documents)
        if goal_evidence_problems:
            result = {
                "error": "GOAL_EVIDENCE_LINK_REQUIRED",
                "reason": "；".join(goal_evidence_problems),
                "instruction": (
                    "先调用 list_documents，使用资料 id 组成 material:<资料 id>#<原文锚点>；"
                    "把 evidence 中每个完整 token 原样写进建模目标里对应的来源或事实正文，再重新写入。"
                    "资料 id 只能出现在 material token 内，不能写成标题或正文。"
                    "不要只写文件名，也不要编造原文位置；未完成精读的资料请先精读定位。"
                ),
                "display": {"text": "建模目标中的资料依据还不能打开原文"},
            }
            self._emit("tool", {"name": name, "phase": "rejected", "summary": result["display"]["text"]})
            _skip_tool(ctx, result)
            return
        diagram_evidence_problems = _diagram_evidence_problems(name, args)
        if diagram_evidence_problems:
            result = {
                "error": "DIAGRAM_EVIDENCE_SPREAD_REQUIRED",
                "reason": "；".join(diagram_evidence_problems),
                "instruction": (
                    "按关键分区检索原文，把每个分区实际命中的 material:<资料 id>#<原文位置> "
                    "同时写入紧邻分区的 evidence 注释和 write_file.evidence。"
                    "多分区流程不能全部引用同一个开头位置；找不到时明确标为待定位。"
                ),
                "display": {"text": "流程图中的资料依据还不足以逐段复核"},
            }
            self._emit("tool", {"name": name, "phase": "rejected", "summary": result["display"]["text"]})
            _skip_tool(ctx, result)
            return
        problems = check_write_safety(name, args)
        unsafe = bool(problems)
        if not problems and self._context.state_action is not None:
            state = await self._context.state_action("get_project_state", {})
            phase_problem = _phase_write_problem(name, args, str(state.get("phase") or ""))
            if phase_problem:
                problems.append(phase_problem)
        if not problems:
            return
        result = {
            "error": "WRITE_UNSAFE" if unsafe else "WRITE_PHASE_MISMATCH",
            "reason": "；".join(problems),
            "next": [
                "改用不含特殊字符的包内相对路径或资源 key 后重试"
                if unsafe else "先完成当前阶段的人工确认；Agent 会在进入对应业务阶段后自动继续"
            ],
            "display": {"text": "已阻止不安全的候选写入" if unsafe else "这项修改需要先完成当前阶段"},
        }
        self._emit("tool", {"name": name, "phase": "rejected", "summary": result["reason"]})
        _skip_tool(ctx, result)


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
        result = {
            "error": "INTERJECTION_READ_ONLY",
            "reason": "回答用户插话的小回合不能改变候选或项目状态",
            "instruction": "直接以最终自然语言回答并结束本轮；服务端随后恢复原问题。",
            "display": {"text": "插话回答保持只读，原问题仍然有效"},
        }
        self._context.emit("tool", {"name": name, "phase": "rejected", "summary": result["reason"]})
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
                and not self._context.decision_request_in_text
            ):
                return self.reject(tool_result=json.dumps({
                    "error": "OVERALL_CONFIRMATION_REQUIRED",
                    "reason": "用户已经提交这批修改进入整体复核，不需要再回答普通问题",
                    "instruction": "不要提问；完成真实差异核对后立即发起整体确认。",
                }, ensure_ascii=False))
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
            if self._context.decision_request_in_text and not self._context.decision_card_requested:
                return self.reject(tool_result=json.dumps({
                    "error": "BUSINESS_DECISION_CARD_REQUIRED",
                    "reason": "本轮正文正在要求用户从互斥业务选项中选择，不能同时改用候选确认承载这个决定",
                    "instruction": (
                        "立即调用 ask，把同一问题整理成少量互斥的业务选项；保留自由输入。"
                        "request_confirmation 只用于接受或退回已经冻结的整体候选，不能代替业务选项卡。"
                    ),
                }, ensure_ascii=False))
            if self._context.confirmation_guard is not None and not self._context.confirmation_guard():
                return self.reject(tool_result=json.dumps({
                    "error": "CONFIRMATION_EVIDENCE_REQUIRED",
                    "reason": "整体变更对照尚未完成",
                    "instruction": "先读取真实候选差异并定位整体对照，再重新发起确认。",
                }, ensure_ascii=False))
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
            groups = prepared.get("change_groups") if isinstance(prepared, Mapping) else None
            if not isinstance(groups, list) or not groups:
                return self.reject(tool_result=json.dumps({
                    "error": "CONFIRMATION_GROUPS_UNAVAILABLE",
                    "reason": "没有可供确认的候选修改",
                    "instruction": "先完成候选写入，再重新发起确认；不要改用普通提问代替候选确认。",
                }, ensure_ascii=False))
            normalized_arguments = {**arguments, "change_groups": groups}
            preflight = await self._context.gateway.call(
                project_id=self._context.project_id,
                run_id=self._context.run_id,
                token=self._context.token,
                tool="request_confirmation_preflight",
                arguments=normalized_arguments,
            )
            if preflight.get("error"):
                return self.reject(tool_result=json.dumps(preflight, ensure_ascii=False))
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
                return self.approve(new_args=json.dumps({**normalized_arguments, "review": automatic_review}, ensure_ascii=False))
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
                    },
                )
            )
        answer = user_input if isinstance(user_input, Mapping) else {"approved": False, "feedback": str(user_input)}
        if answer.get("action") == "settled_confirmation":
            settled = answer.get("result")
            result = dict(settled) if isinstance(settled, Mapping) else {
                "error": "CONFIRMATION_RESULT_MISSING",
                "reason": "候选结算结果没有恢复",
            }
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

    def __init__(self, emit: EventSink) -> None:
        self._emit = emit

    async def before_model_call(self, _ctx: AgentCallbackContext) -> None:
        self._emit("progress", {"text": "助手正在整理下一步", "counters": {}})

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        if ctx.extra.get("_skip_tool"):
            return
        name = _tool_name(ctx)
        spec = tool_spec(name)
        display = _display_text(spec.display if spec else "正在处理", _call_args(ctx))
        self._emit("progress", {"text": display, "counters": {}})
        self._emit("tool", {"name": name, "phase": "started", "summary": display})

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        name = _tool_name(ctx)
        result = _result_dict(getattr(ctx.inputs, "tool_result", None))
        display = result.get("display") if isinstance(result.get("display"), dict) else {}
        summary = str(display.get("text") or result.get("reason") or "处理完成")
        phase = "failed" if result.get("error") else "completed"
        self._emit("tool", {"name": name, "phase": phase, "summary": summary})


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


def _goal_evidence_problems(
    tool: str,
    arguments: dict[str, Any],
    documents: list[Mapping[str, Any]] | None = None,
) -> list[str]:
    if tool != "write_file" or str(arguments.get("path") or "") != ".modeling/goal.md":
        return []
    evidence = arguments.get("evidence")
    evidence_items = evidence if isinstance(evidence, list) else []
    material_tokens = [
        str(item).strip()
        for item in evidence_items
        if str(item).strip().startswith("material:")
    ]
    if documents and not material_tokens:
        return ["项目已有资料，建模目标至少需要一处可定位的资料原文依据"]
    if not material_tokens:
        return []
    content = str(arguments.get("content") or "")
    problems: list[str] = []
    sources_with_body_tokens: set[str] = set()
    visible_content = content
    for token in material_tokens:
        source, separator, anchor = token.partition("#")
        if not separator or not source.removeprefix("material:").strip() or not anchor.strip():
            problems.append(f"资料证据缺少可定位的原文锚点：{token}")
        elif token not in content:
            problems.append(f"建模目标正文缺少完整资料证据：{token}")
        else:
            sources_with_body_tokens.add(source.removeprefix("material:").strip())
            visible_content = visible_content.replace(token, "")
    for document in documents or []:
        filename = str(document.get("filename") or "").strip()
        document_id = str(document.get("id") or "").strip()
        if filename and filename in content and not ({document_id, filename} & sources_with_body_tokens):
            problems.append(f"目标中引用了《{filename}》，但没有标注它的原文位置")
        if document_id and document_id in visible_content:
            problems.append(f"目标正文暴露了《{filename or '项目资料'}》的内部资料编号")
    return problems


def _diagram_evidence_problems(tool: str, arguments: dict[str, Any]) -> list[str]:
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
        return ["业务流程图至少需要一处可定位的资料原文依据"]
    content = str(arguments.get("content") or "")
    problems: list[str] = []
    anchors: set[str] = set()
    for token in material_tokens:
        source, separator, anchor = token.partition("#")
        if not separator or not source.removeprefix("material:").strip() or not anchor.strip():
            problems.append(f"流程图资料依据缺少可定位的原文位置：{token}")
        elif token not in content:
            problems.append(f"流程图正文缺少完整资料依据：{token}")
        else:
            anchors.add(f"{source}#{anchor.strip()}")
    subgraphs = len(re.findall(r"^\s*subgraph\b", content, flags=re.MULTILINE | re.IGNORECASE))
    evidence_comments = len(re.findall(r"^\s*%%\s*evidence:\s*material:", content, flags=re.MULTILINE | re.IGNORECASE))
    if subgraphs >= 2 and evidence_comments < 2:
        problems.append("多分区流程图需要在至少两个关键分区旁分别标注资料依据")
    if subgraphs >= 3 and len(anchors) < 2:
        problems.append("多分区流程图不能让所有分区共用同一个原文位置")
    return problems


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
        return options
    # 上传已有独立的文件选择器；资料请求里的判断动作必须互斥、稳定，
    # 不让模型生成的同义选项增加用户的判断成本。
    return [
        {"label": "我口述", "description": "直接说明做法，作为 user-decision 证据继续", "value": "oral"},
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
