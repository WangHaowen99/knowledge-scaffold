from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from openjiuwen.core.foundation.llm import init_model
from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall


@dataclass(frozen=True)
class PlannedTurn:
    name: str = ""
    arguments: dict[str, Any] | None = None
    content: str = ""


@dataclass(frozen=True)
class CompletedCall:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    index: int


RESOURCE_KEYS = {
    "object": "member_profile",
    "rule": "discount_eligibility",
    "operation": "review_synthetic_order",
    "metric": "review_pass_rate",
}


def _acceptance_placeholder_api_key() -> str:
    """Return a deterministic non-secret value required by the model client schema."""
    return hashlib.sha256(__name__.encode("utf-8")).hexdigest()


class AcceptanceModel(Model):
    """Deterministic openJiuwen model used only by marked synthetic browser projects.

    The model emits ordinary model tool calls. It has no Core, database, filesystem or
    confirmation access of its own, so all behavior still crosses the production rails.
    """

    def __init__(self, scenario: str = "") -> None:
        template = init_model(
            provider="OpenAI",
            model_name="browser-acceptance-model",
            api_key=_acceptance_placeholder_api_key(),
            api_base="http://127.0.0.1:9/unused",
        )
        super().__init__(
            model_client_config=template.model_client_config,
            model_config=template.model_config,
        )
        self.scenario = scenario

    async def invoke(
        self,
        messages: Any,
        *,
        tools: Any = None,
        **_kwargs: Any,
    ) -> AssistantMessage:
        return _assistant_message(plan_acceptance_turn(messages, tools, self.scenario))

    async def stream(
        self,
        messages: Any,
        *,
        tools: Any = None,
        **_kwargs: Any,
    ):
        response = _assistant_message(plan_acceptance_turn(messages, tools, self.scenario))
        if response.tool_calls:
            yield AssistantMessageChunk(content="", tool_calls=response.tool_calls, finish_reason="tool_calls")
            return
        content = str(response.content or "")
        parts = [content[index:index + 16] for index in range(0, len(content), 16)] or [""]
        for index, part in enumerate(parts):
            yield AssistantMessageChunk(
                content=part,
                finish_reason="stop" if index == len(parts) - 1 else "null",
            )


def plan_acceptance_turn(messages: Any, tools: Any, scenario: str = "") -> PlannedTurn:
    available = _available_tool_names(tools)
    if not available:
        return PlannedTurn(content="本轮可见结果已经整理完成。")
    history = _message_list(messages)
    calls = _completed_calls(history)
    prompt = _business_prompt(history)
    latest = calls[-1] if calls else None

    if latest and latest.result.get("error"):
        recovered = _recover_from_error(latest, calls, prompt, available)
        if recovered is not None:
            return recovered
        return PlannedTurn(content=_result_text(latest.result, "当前步骤没有完成，已有内容保持不变。"))
    if latest and latest.name == "run_script_test" and int(latest.result.get("failed") or 0) > 0:
        return PlannedTurn(content=_result_text(latest.result, "脚本沙箱测试失败，当前源码没有进入确认。"))

    if "get_project_state" not in {call.name for call in calls}:
        return _tool("get_project_state", {}, available)

    if _needs_decision(prompt):
        return _decision_turn(prompt, available)

    if _status_only(prompt):
        return PlannedTurn(content="当前合成项目状态已经核对，本轮没有产生任何修改。")

    if _read_only_analysis(prompt):
        if not _has_call(calls, "list_documents"):
            return _tool("list_documents", {}, available)
        if not _has_call(calls, "read_documents_fully"):
            return _tool("read_documents_fully", {}, available)
        return PlannedTurn(content="合成资料已完成全文核对，本轮只做分析，没有形成候选修改。")

    if _is_script_request(prompt, scenario):
        return _script_turn(prompt, calls, available)

    if _is_binding_request(prompt, scenario):
        return _binding_turn(prompt, calls, available)

    return _domain_turn(prompt, calls, available, scenario, history)


def _domain_turn(
    prompt: str,
    calls: list[CompletedCall],
    available: set[str],
    scenario: str,
    history: list[Any],
) -> PlannedTurn:
    latest = calls[-1] if calls else None
    if latest and latest.name == "discard_rejected_candidate_changes":
        return PlannedTurn(content=_result_text(
            latest.result,
            "已放弃退回修改，当前已确认版本保持不变。",
        ))
    if latest and latest.name == "request_confirmation" and latest.result.get("rejected") is True:
        feedback = str(latest.result.get("feedback") or "")
        if _requests_discard(feedback):
            return _tool("discard_rejected_candidate_changes", {}, available)
        return PlannedTurn(content="候选已经退回，后续只会按你的反馈调整，不会写入已确认版本。")
    plan = _latest_plan(calls)
    if _approval_message_present(history) and not plan:
        return _tool("get_change_plan", {}, available)
    effective_prompt = str(plan.get("goal") or prompt)
    generation = str(plan.get("mode") or "") == "generation" or _is_generation(effective_prompt, scenario)
    task_kinds = [
        str(task.get("resource_kind") or "")
        for task in plan.get("tasks", [])
        if isinstance(task, Mapping) and str(task.get("resource_kind") or "") in RESOURCE_KEYS
    ]
    targets = task_kinds or _target_kinds(effective_prompt, generation)

    if generation and ("全文" in effective_prompt or "资料" in effective_prompt):
        if not _has_call(calls, "list_documents"):
            return _tool("list_documents", {}, available)
        if not _has_call(calls, "read_documents_fully"):
            return _tool("read_documents_fully", {}, available)

    if plan and plan.get("approval_status") == "pending":
        if _approval_message_present(history):
            last_approval = _last_user_index(history, "我已在页面批准当前建模计划")
            if not any(call.name == "get_change_plan" and call.index > last_approval for call in calls):
                return _tool("get_change_plan", {"plan_id": plan.get("id", "")}, available)
        return PlannedTurn(content="建模计划已经保存，批准前不会生成候选修改。")

    schema_tasks = [
        task for task in plan.get("tasks", [])
        if isinstance(task, Mapping) and "sync_schema_objects" in (task.get("allowed_tools") or [])
    ]
    schema_syncs = [call for call in calls if call.name == "sync_schema_objects"]
    if schema_tasks and not schema_syncs:
        return _tool("sync_schema_objects", {
            "plan_task_id": schema_tasks[0].get("id", ""),
            "expected_revision": plan.get("expected_revision", ""),
            "mutation_id": "acceptance-schema-sync-1",
            "reason": "按全部合成 Schema Sheet 确定性同步物理对象",
        }, available)

    for kind in targets:
        if not _has_resource_list(calls, kind):
            return _tool("list_domain_resources", {"kind": kind}, available)

    if not generation:
        for kind in targets:
            resource_id = f"{kind}:{RESOURCE_KEYS[kind]}"
            if not _has_resource_read(calls, resource_id):
                return _tool("get_domain_resource", {"resource_id": resource_id}, available)

    if not plan:
        revision = _working_revision(calls)
        tasks = [_plan_task(kind, generation) for kind in targets]
        return _tool("create_change_plan", {
            "goal": prompt[:4000] or "完成合成浏览器验收修改",
            "mode": "generation" if generation else "expert_edit",
            "expected_revision": revision,
            "tasks": tasks,
        }, available)

    if plan.get("approval_status") == "pending":
        return PlannedTurn(content="建模计划正在等待批准，当前没有写入候选。")

    staged = [call for call in calls if call.name == "stage_change_set"]
    receipts = [call for call in calls if call.name == "get_change_set_receipt"]
    staged_indexes: list[int] = []
    for call in staged:
        if call.result.get("status") in {"staged", "validated", "waiting_user"}:
            staged_indexes.append(call.index)
    for call in receipts:
        change_set = call.result.get("change_set")
        if isinstance(change_set, Mapping) and change_set.get("status") in {
            "staged", "validated", "waiting_user",
        }:
            staged_indexes.append(call.index)
    for call in schema_syncs:
        if call.result.get("ok") is True:
            staged_indexes.append(call.index)
    staged_boundary = max(staged_indexes, default=-1)

    pending_domain_tasks: list[Mapping[str, Any]] = []
    for task in plan.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        resource_kind = str(task.get("resource_kind") or "")
        status = str(task.get("status") or "pending")
        if resource_kind in RESOURCE_KEYS and status == "pending":
            pending_domain_tasks.append(task)
    if staged_boundary < 0 or (pending_domain_tasks and not staged):
        return _tool("stage_change_set", _change_set_arguments(effective_prompt, plan, calls, generation), available)

    validated = [call for call in calls if call.name == "validate_change_set"]
    if not validated or validated[-1].index < staged_boundary:
        return _tool("validate_change_set", {
            "plan_id": plan.get("id", ""),
            "summary": "核对完整合成建模计划",
        }, available)

    if not _has_call_after(calls, "request_confirmation", validated[-1].index):
        return _tool("request_confirmation", {"summary": "请复核本轮合成知识修改"}, available)
    return PlannedTurn(content="待修改内容已经提交人工复核。")


def _binding_turn(prompt: str, calls: list[CompletedCall], available: set[str]) -> PlannedTurn:
    del prompt
    if not _has_resource_list(calls, "object"):
        return _tool("list_domain_resources", {"kind": "object", "query": "synthetic_member"}, available)
    if not _has_resource_read(calls, "object:synthetic_member"):
        return _tool("get_domain_resource", {"resource_id": "object:synthetic_member"}, available)
    if not _has_call(calls, "list_tables"):
        return _tool("list_tables", {}, available)
    table = _latest_result(calls, "list_tables").get("tables", [])
    selected = next((item for item in table if isinstance(item, Mapping) and item.get("name") == "synthetic_members"), {})
    document_id = str(selected.get("documentId") or "")
    if not _has_call(calls, "describe_table"):
        return _tool("describe_table", {"document_id": document_id, "table": "synthetic_members"}, available)
    plan = _latest_plan(calls)
    revision = _working_revision(calls)
    if not plan:
        return _tool("create_change_plan", {
            "goal": "把合成测试对象完整重绑到合成数据表",
            "mode": "expert_edit",
            "expected_revision": revision,
            "tasks": [{
                "client_key": "bind-synthetic-member",
                "title": "重绑测试对象数据源",
                "resource_kind": "binding",
                "target_key": "synthetic_member",
                "allowed_tools": ["patch_object_source_binding"],
                "depends_on": [],
                "acceptance": "三个字段一一映射且主键为 member_id",
                "required": True,
            }],
        }, available)
    direct = [call for call in calls if call.name == "patch_object_source_binding"]
    if not direct:
        resource = _resource(calls, "object:synthetic_member")
        return _tool("patch_object_source_binding", {
            "plan_task_id": plan["tasks"][0]["id"],
            "resource_id": "object:synthetic_member",
            "expected_revision": revision,
            "expected_resource_hash": resource.get("resource_hash", ""),
            "mutation_id": "acceptance-binding-1",
            "reason": "按合成表完整重绑测试对象",
            "document_id": document_id,
            "table": "synthetic_members",
            "field_bindings": [
                {"field_key": "member_id", "source_column": "member_id"},
                {"field_key": "member_name", "source_column": "member_name"},
                {"field_key": "member_level", "source_column": "member_level"},
            ],
            "primary_key": ["member_id"],
            "evidence": ["material:synthetic_members.xlsx"],
        }, available)
    validated = [call for call in calls if call.name == "validate_change_set"]
    if not validated or validated[-1].index < direct[-1].index:
        return _tool("validate_change_set", {"plan_id": plan.get("id", ""), "summary": "核对对象完整重绑"}, available)
    return _tool("request_confirmation", {"summary": "请复核测试对象的数据源重绑"}, available)


def _script_turn(prompt: str, calls: list[CompletedCall], available: set[str]) -> PlannedTurn:
    if not _has_resource_list(calls, "operation"):
        return _tool("list_domain_resources", {"kind": "operation", "query": "synthetic_summary"}, available)
    if not _has_resource_read(calls, "operation:synthetic_summary"):
        return _tool("get_domain_resource", {"resource_id": "operation:synthetic_summary"}, available)
    plan = _latest_plan(calls)
    revision = _working_revision(calls)
    if not plan:
        return _tool("create_change_plan", {
            "goal": prompt[:4000] or "生成并验证合成汇总脚本",
            "mode": "expert_edit",
            "expected_revision": revision,
            "tasks": [{
                "client_key": "synthetic-summary-script",
                "title": "生成合成汇总脚本",
                "resource_kind": "script",
                "target_key": "synthetic_summary_script",
                "allowed_tools": ["stage_script_patch"],
                "depends_on": [],
                "acceptance": "源码通过安全检查及 1+2、0+0 两个契约用例",
                "required": True,
            }],
        }, available)
    resource = _resource(calls, "operation:synthetic_summary")
    if not _has_call(calls, "define_script_contract"):
        return _tool("define_script_contract", {
            "expected_revision": revision,
            "mutation_id": "acceptance-script-contract-1",
            "reason": "定义合成汇总脚本执行契约",
            "script_key": "synthetic_summary_script",
            "title": "合成汇总脚本",
            "description": "把两个非负合成整数汇总为 JSON。",
            "operation_id": "operation:synthetic_summary",
            "expected_operation_hash": resource.get("operation_contract_hash", ""),
            "entrypoint": "synthetic_summary.py",
            "timeout_seconds": 10,
            "parameters": [
                {"name": "left", "flag": "--left", "type": "integer", "required": True},
                {"name": "right", "flag": "--right", "type": "integer", "required": True},
            ],
            "outputs": [{"key": "total", "type": "integer", "description": "两个输入之和"}],
            "boundaries": ["left 和 right 必须是非负整数"],
            "evidence": ["合成浏览器脚本验收"],
        }, available)
    context_calls = [call for call in calls if call.name == "get_script_edit_context"]
    if not context_calls:
        return _tool("get_script_edit_context", {"script_key": "synthetic_summary_script"}, available)
    source_calls = [call for call in calls if call.name == "stage_script_patch"]
    test_calls = [call for call in calls if call.name == "run_script_test"]
    needs_second_source = "测试通过后" in prompt and len(source_calls) == 1 and len(test_calls) >= 1
    latest_context = context_calls[-1]
    context_result = latest_context.result
    context_has_source = any(item.get("source") for item in context_result.get("symbols", []))
    latest_source_index = source_calls[-1].index if source_calls else -1
    if not context_has_source or latest_context.index < latest_source_index:
        return _tool("get_script_edit_context", {
            "script_key": "synthetic_summary_script",
            "symbols": ["run_business_logic"],
        }, available)
    if not source_calls or needs_second_source:
        second = bool(source_calls)
        business_source = [
            "def run_business_logic(inputs):",
            "    left = int(inputs[\"left\"])",
            "    right = int(inputs[\"right\"])",
            "    if left < 0 or right < 0:",
            "        raise ValueError(\"left and right must be non-negative\")",
            f"    return {{\"total\": left + right{(' + 0' if second else '')}}}",
        ]
        target = next(item for item in context_result.get("symbols", []) if item.get("qualified_name") == "run_business_logic")
        return _tool("stage_script_patch", {
            "script_key": "synthetic_summary_script",
            "plan_task_id": plan["tasks"][0]["id"],
            "expected_revision": _working_revision(calls),
            "expected_contract_hash": context_result.get("contract_hash", ""),
            "base_source_hash": context_result.get("base_source_hash", ""),
            "mutation_id": f"acceptance-script-patch-{2 if second else 1}",
            "reason": "提交当前合成汇总脚本业务函数补丁",
            "edits": [{
                "kind": "replace_function",
                "symbol": "run_business_logic",
                "expected_node_hash": target.get("node_hash", ""),
                "source": "\n".join(business_source),
            }],
        }, available)
    if len(test_calls) < len(source_calls):
        return _tool("run_script_test", {
            "script": "scripts/synthetic_summary.py",
            "cases": [
                {"name": "1+2", "input": {"left": 1, "right": 2}, "expected": {"total": 3}},
                {"name": "0+0", "input": {"left": 0, "right": 0}, "expected": {"total": 0}},
            ],
        }, available)
    validated = [call for call in calls if call.name == "validate_change_set"]
    if not validated or validated[-1].index < test_calls[-1].index:
        return _tool("validate_change_set", {"plan_id": plan.get("id", ""), "summary": "核对脚本契约、源码和沙箱回执"}, available)
    return _tool("request_confirmation", {"summary": "请复核合成汇总脚本及测试回执"}, available)


def _recover_from_error(
    latest: CompletedCall,
    calls: list[CompletedCall],
    prompt: str,
    available: set[str],
) -> PlannedTurn | None:
    code = str(latest.result.get("error") or latest.result.get("code") or "")
    if code == "CHANGE_SET_RESULT_UNKNOWN":
        mutation_id = str(latest.arguments.get("mutation_id") or "")
        return _tool("get_change_set_receipt", {"mutation_id": mutation_id}, available)
    if code == "CHANGE_SET_TEMPORARILY_UNAVAILABLE":
        plan = _latest_plan(calls)
        if plan:
            args = _change_set_arguments(prompt, plan, calls, _is_generation(prompt, ""))
            args["mutation_id"] = "acceptance-change-retry-2"
            return _tool("stage_change_set", args, available)
    if code == "CANDIDATE_VALIDATION_FAILED" and "同一项校验问题" in _result_text(latest.result, ""):
        plan = _latest_plan(calls)
        if plan:
            return _tool("validate_change_set", {"plan_id": plan.get("id", ""), "summary": "有限重试同一校验问题"}, available)
    if code == "MANAGED_FILE_REQUIRES_DOMAIN_TOOL":
        required_tool = str(latest.result.get("requiredTool") or "")
        if (
            latest.result.get("requiredAction") == "redirect_tool"
            and required_tool in available
        ):
            return _tool(required_tool, dict(latest.arguments), available)
    return None


def _change_set_arguments(
    prompt: str,
    plan: dict[str, Any],
    calls: list[CompletedCall],
    generation: bool,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    for task in plan.get("tasks", []):
        if not isinstance(task, Mapping):
            continue
        if task.get("status") not in {None, "", "pending"}:
            continue
        kind = str(task.get("resource_kind") or "")
        target = str(task.get("target_key") or "")
        if kind not in RESOURCE_KEYS:
            continue
        tool = f"create_{kind}" if generation else f"patch_{kind}"
        arguments = _create_arguments(kind, _material_evidence(calls)) if generation else {
            "resource_id": f"{kind}:{target}",
            "expected_resource_hash": _resource(calls, f"{kind}:{target}").get("resource_hash", ""),
            "actions": _patch_actions(kind, prompt, _resource(calls, f"{kind}:{target}")),
        }
        actions.append({"task_id": task.get("id", ""), "tool": tool, "arguments": arguments})
        if len(actions) >= 4:
            break
    prior = len([call for call in calls if call.name == "stage_change_set"])
    return {
        "plan_id": plan.get("id", ""),
        "expected_revision": plan.get("expected_revision", ""),
        "mutation_id": f"acceptance-change-{prior + 1}",
        "actions": actions,
    }


def _plan_task(kind: str, generation: bool) -> dict[str, Any]:
    target = RESOURCE_KEYS[kind]
    return {
        "client_key": f"{kind}-{target}",
        "title": f"{'生成' if generation else '修改'}合成{kind}资源 {target}",
        "resource_kind": kind,
        "target_key": target,
        "allowed_tools": [f"{'create' if generation else 'patch'}_{kind}"],
        "depends_on": [],
        "acceptance": "只改变本任务声明的合成资源，并通过完整候选校验",
        "required": True,
    }


def _create_arguments(kind: str, evidence: list[str]) -> dict[str, Any]:
    if kind == "object":
        return {
            "key": "member_profile", "title": "合成会员档案",
            "description": "只描述合成浏览器验收使用的会员基本信息。",
            "query_boundary": "不包含真实人员、联系方式或生产数据。",
            "aliases": [], "relations": [],
            "fields": [
                {"key": "member_id", "cn_name": "合成会员编号", "type": "string", "required": True},
                {"key": "member_name", "cn_name": "合成会员名称", "type": "string", "required": False},
                {"key": "status", "cn_name": "测试状态", "type": "string", "required": False},
            ],
        }
    if kind == "rule":
        return {
            "key": "discount_eligibility", "title": "折扣资格规则",
            "description": "判断合成会员是否满足测试折扣资格。",
            "content": "测试状态有效时进入折扣资格复核。",
            "conditions": [{"condition": "测试状态为有效", "conclusion": "进入折扣资格复核"}],
            "exceptions": ["测试状态缺失时转人工确认"],
            "targets": ["object:member_profile"], "evidence": evidence,
        }
    if kind == "operation":
        return {
            "key": "review_synthetic_order", "title": "复核合成订单",
            "description": "依据合成资料复核测试订单。",
            "inputs": [{"param": "order_id", "cn_name": "合成订单编号", "type": "string", "required": True}],
            "outputs": [{"field": "review_result", "cn_name": "复核结果", "type": "string"}],
            "flow": ["读取合成订单", "核对折扣资格", "返回复核结果"],
            "notes": ["仅用于合成浏览器验收。"],
            "references": ["object:member_profile", "rule:discount_eligibility", "metric:review_pass_rate"],
            "evidence": evidence,
        }
    return {
        "key": "review_pass_rate", "title": "复核通过率",
        "description": "合成订单复核通过的比例。", "unit": "%", "grain": "按验收批次",
        "caliber": "复核通过数 / 已完成复核数 * 100%",
        "boundaries": ["只统计已经完成的合成订单复核", "分母为零时不计算"],
        "null_policy": [], "dimensions": [], "object_references": ["object:member_profile"],
        "evidence": evidence,
    }


def _material_evidence(calls: list[CompletedCall]) -> list[str]:
    documents = _latest_result(calls, "list_documents").get("documents", [])
    references = [
        f"material:{item.get('filename')}#synthetic-acceptance"
        for item in documents
        if isinstance(item, Mapping) and str(item.get("filename") or "").strip()
    ]
    return references[:3] or ["material:synthetic_objects.md"]


def _patch_actions(kind: str, prompt: str, resource: dict[str, Any]) -> list[dict[str, Any]]:
    if kind == "object":
        value = _quoted_after(prompt, "会员档案说明") or _quoted_after(prompt, "说明改成")
        return [{"op": "set_description", "value": value or "合成会员档案说明已完成浏览器验收更新。"}]
    if kind == "rule":
        if "例外列表" in prompt and "改成" in prompt:
            items = _editable_items(resource, "exceptions")
            replacement = _quoted_values(prompt)[-1] if _quoted_values(prompt) else "测试状态缺失时转人工复核"
            actions = []
            if items:
                actions.append({"op": "remove_exception", "item_id": items[0].get("item_id", "")})
            actions.append({"op": "add_exception", "value": replacement})
            return actions
        if "增加例外" in prompt:
            return [{"op": "add_exception", "value": _quoted_after(prompt, "例外") or "合成浏览器验收例外"}]
        value = _quoted_after(prompt, "规则的说明") or _quoted_after(prompt, "规则说明")
        return [{"op": "set_description", "value": value or "折扣资格规则说明已完成合成浏览器验收更新。"}]
    if kind == "operation":
        if "第二步" in prompt:
            steps = _editable_items(resource, "flow")
            value = _quoted_after(prompt, "第二步") or "核对测试折扣资格"
            if len(steps) > 1:
                return [{"op": "update_step", "item_id": steps[1].get("item_id", ""), "value": value}]
        return [{"op": "set_description", "value": "复核合成订单说明已完成浏览器验收更新。"}]
    if "边界补充" in prompt:
        return [{"op": "add_boundary", "value": _quoted_after(prompt, "边界补充") or "取消的测试订单不计入"}]
    value = _quoted_after(prompt, "复核通过率说明") or _quoted_after(prompt, "说明改成")
    return [{"op": "set_description", "value": value or "复核通过率说明已完成合成浏览器验收更新。"}]


def _decision_turn(prompt: str, available: set[str]) -> PlannedTurn:
    unsafe = "TODO" in prompt or "动态执行" in prompt
    question = (
        "不能使用 TODO 占位或动态执行字符串生成脚本；需要你决定安全实现方式。"
        if unsafe else
        "需要你决定：当前目标或业务口径不唯一，不能在没有明确范围时修改。"
    )
    return _tool("ask", {
        "question": question,
        "kind": "business_decision",
        "options": [
            {"label": "保持当前内容", "description": "本轮不产生修改", "value": "keep"},
            {
                "label": "补充明确目标", "description": "说明唯一资源或业务口径后继续",
                "value": "clarify", "requiresText": True, "textPrompt": "请说明唯一目标和期望值",
            },
        ],
        "evidence": [],
    }, available)


def _needs_decision(prompt: str) -> bool:
    markers = (
        "同名测试指标", "删除被", "不存在的", "不存在锚点", "TODO", "动态执行",
        "目标不够明确", "没有给出阈值", "不能自行猜测",
    )
    for marker in markers:
        if marker in prompt:
            return True
    return False


def _requests_discard(feedback: str) -> bool:
    markers = (
        "放弃", "撤销", "不需要修改", "不要修改", "保持当前", "保持已确认",
    )
    for marker in markers:
        if marker in feedback:
            return True
    return False


def _status_only(prompt: str) -> bool:
    return "只说明当前合成项目状态" in prompt


def _read_only_analysis(prompt: str) -> bool:
    return "暂时不要生成知识资源" in prompt or ("全文读取全部" in prompt and "分析" in prompt)


def _is_script_request(prompt: str, scenario: str) -> bool:
    if "不生成脚本" in prompt:
        return False
    return scenario == "SCN-SCRIPT" or "Python 脚本" in prompt or "生成脚本" in prompt


def _is_binding_request(prompt: str, scenario: str) -> bool:
    del scenario
    return "绑定到 synthetic_members" in prompt


def _is_generation(prompt: str, scenario: str) -> bool:
    if scenario not in {"SCN-GENERATION", "SCN-LONG", "SCN-BINDING"}:
        return False
    return any(marker in prompt for marker in ("生成", "建立完整", "根据", "开始长资料建模", "启动长资料分析"))


def _target_kinds(prompt: str, generation: bool) -> list[str]:
    if generation:
        selected: list[str] = []
        target_markers = (
            ("object", "对象"), ("rule", "规则"), ("operation", "操作"), ("metric", "指标"),
        )
        for kind, marker in target_markers:
            if marker in prompt:
                selected.append(kind)
        return selected or ["object", "rule", "operation", "metric"]
    selected: list[str] = []
    if "会员档案" in prompt:
        selected.append("object")
    if "折扣资格规则" in prompt or "规则说明" in prompt:
        selected.append("rule")
    if "复核合成订单" in prompt:
        selected.append("operation")
    if "复核通过率" in prompt:
        selected.append("metric")
    return selected or ["rule"]


def _assistant_message(turn: PlannedTurn) -> AssistantMessage:
    if not turn.name:
        return AssistantMessage(content=turn.content)
    arguments = turn.arguments or {}
    digest = hashlib.sha256(
        f"{turn.name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}".encode("utf-8")
    ).hexdigest()[:12]
    return AssistantMessage(
        content="",
        tool_calls=[ToolCall(
            id=f"call_acceptance_{digest}",
            type="function",
            name=turn.name,
            arguments=json.dumps(arguments, ensure_ascii=False),
        )],
    )


def _tool(name: str, arguments: dict[str, Any], available: set[str]) -> PlannedTurn:
    if name not in available:
        return PlannedTurn(content=f"当前阶段不能执行“{name}”对应的业务步骤，本轮已停止且没有绕过门禁。")
    return PlannedTurn(name=name, arguments=arguments)


def _message_list(messages: Any) -> list[Any]:
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]
    if isinstance(messages, Sequence):
        return list(messages)
    return []


def _content(message: Any) -> str:
    value = message.get("content", "") if isinstance(message, Mapping) else getattr(message, "content", "")
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value or "")


def _role(message: Any) -> str:
    return str(message.get("role", "") if isinstance(message, Mapping) else getattr(message, "role", ""))


def _tool_calls(message: Any) -> Iterable[Any]:
    value = message.get("tool_calls") if isinstance(message, Mapping) else getattr(message, "tool_calls", None)
    return value if isinstance(value, list) else []


def _call_field(call: Any, name: str) -> Any:
    if isinstance(call, Mapping):
        if name in call:
            return call.get(name)
        function = call.get("function")
        return function.get(name) if isinstance(function, Mapping) else None
    return getattr(call, name, None)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _completed_calls(messages: list[Any]) -> list[CompletedCall]:
    pending: dict[str, tuple[str, dict[str, Any], int]] = {}
    completed: list[CompletedCall] = []
    for index, message in enumerate(messages):
        for call in _tool_calls(message):
            call_id = str(_call_field(call, "id") or "")
            pending[call_id] = (
                str(_call_field(call, "name") or ""),
                _json_object(_call_field(call, "arguments")),
                index,
            )
        if _role(message) != "tool":
            continue
        call_id = str(message.get("tool_call_id", "") if isinstance(message, Mapping)
                      else getattr(message, "tool_call_id", ""))
        details = pending.get(call_id)
        if not details:
            continue
        name, arguments, call_index = details
        completed.append(CompletedCall(name, arguments, _json_object(_content(message)), call_index))
    return completed


def _business_prompt(messages: list[Any]) -> str:
    candidates: list[str] = []
    for message in messages:
        if _role(message) != "user":
            continue
        content = _content(message)
        if "# 项目唯一 Agent 状态快照" in content:
            content = content.split("# 项目唯一 Agent 状态快照", 1)[0].strip()
        if "# Execution Brief" in content:
            raw = content.split("# Execution Brief", 1)[1].strip()
            try:
                goal = str(json.loads(raw).get("userGoal") or "")
            except (TypeError, ValueError):
                goal = ""
            if goal:
                candidates.append(goal)
            continue
        if content.startswith("服务端检测到") or content.startswith("服务端尚未观察到"):
            continue
        if content.startswith("我已在页面批准当前建模计划"):
            continue
        if content:
            candidates.append(content)
    return candidates[0] if candidates else "完成当前合成浏览器验收任务"


def _available_tool_names(tools: Any) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if isinstance(tool, Mapping):
            function = tool.get("function")
            name = tool.get("name") or (function.get("name") if isinstance(function, Mapping) else "")
        else:
            name = getattr(tool, "name", "")
            if not name:
                function = getattr(tool, "function", None)
                name = getattr(function, "name", "") if function is not None else ""
        if name:
            names.add(str(name))
    return names


def _has_call(calls: list[CompletedCall], name: str) -> bool:
    return any(call.name == name for call in calls)


def _has_call_after(calls: list[CompletedCall], name: str, index: int) -> bool:
    return any(call.name == name and call.index > index for call in calls)


def _has_resource_list(calls: list[CompletedCall], kind: str) -> bool:
    return any(call.name == "list_domain_resources" and call.arguments.get("kind") == kind for call in calls)


def _has_resource_read(calls: list[CompletedCall], resource_id: str) -> bool:
    return any(call.name == "get_domain_resource" and call.arguments.get("resource_id") == resource_id for call in calls)


def _latest_result(calls: list[CompletedCall], name: str) -> dict[str, Any]:
    return next((call.result for call in reversed(calls) if call.name == name), {})


def _latest_plan(calls: list[CompletedCall]) -> dict[str, Any]:
    for call in reversed(calls):
        plan = call.result.get("plan")
        if isinstance(plan, Mapping):
            return dict(plan)
    return {}


def _working_revision(calls: list[CompletedCall]) -> str:
    for call in reversed(calls):
        value = call.result.get("working_revision")
        if value:
            return str(value)
        plan = call.result.get("plan")
        if isinstance(plan, Mapping) and plan.get("expected_revision"):
            return str(plan["expected_revision"])
    return ""


def _resource(calls: list[CompletedCall], resource_id: str) -> dict[str, Any]:
    for call in reversed(calls):
        if call.name != "get_domain_resource" or call.arguments.get("resource_id") != resource_id:
            continue
        resource = call.result.get("resource")
        return dict(resource) if isinstance(resource, Mapping) else {}
    return {}


def _editable_items(resource: dict[str, Any], key: str) -> list[dict[str, Any]]:
    editable = resource.get("editable_items")
    items = editable.get(key, []) if isinstance(editable, Mapping) else []
    return [dict(item) for item in items if isinstance(item, Mapping)]


def _quoted_values(prompt: str) -> list[str]:
    return re.findall(r"“([^”]+)”", prompt)


def _quoted_after(prompt: str, marker: str) -> str:
    position = prompt.find(marker)
    if position < 0:
        return ""
    match = re.search(r"“([^”]+)”", prompt[position:])
    return match.group(1) if match else ""


def _result_text(result: dict[str, Any], fallback: str) -> str:
    display = result.get("display")
    if isinstance(display, Mapping) and display.get("text"):
        return str(display["text"])
    return str(result.get("reason") or result.get("error") or fallback)


def _approval_message_present(messages: list[Any]) -> bool:
    return any("我已在页面批准当前建模计划" in _content(message) for message in messages)


def _last_user_index(messages: list[Any], marker: str) -> int:
    return max((index for index, item in enumerate(messages)
                if _role(item) == "user" and marker in _content(item)), default=-1)
