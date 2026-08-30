from __future__ import annotations

import json
from typing import Any

from openjiuwen.core.workflow import End, Start, Workflow, WorkflowCard, WorkflowComponent


EXECUTION_BRIEF_WORKFLOW_VERSION = "execution-brief-workflow-v1"
TOOL_REDIRECT_WORKFLOW_VERSION = "tool-redirect-workflow-v1"


class _ExecutionBriefComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, _session: Any, _context: Any) -> dict[str, str]:
        raw_payload = inputs.get("payload_json") if isinstance(inputs, dict) else None
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else None
        except json.JSONDecodeError as error:
            raise ValueError("Execution Brief 输入不是有效 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("Execution Brief 缺少结构化输入")
        return {"prompt": render_execution_brief(payload)}


class _ToolRedirectComponent(WorkflowComponent):
    async def invoke(self, inputs: Any, _session: Any, _context: Any) -> dict[str, str]:
        raw_payload = inputs.get("payload_json") if isinstance(inputs, dict) else None
        try:
            payload = json.loads(raw_payload) if isinstance(raw_payload, str) else None
        except json.JSONDecodeError as error:
            raise ValueError("工具转向输入不是有效 JSON") from error
        if not isinstance(payload, dict) or not str(payload.get("requiredTool") or ""):
            raise ValueError("工具转向缺少结构化恢复动作")
        return {"prompt": render_tool_redirect(payload)}


def build_execution_brief_workflow() -> Workflow:
    workflow = Workflow(WorkflowCard(
        id=EXECUTION_BRIEF_WORKFLOW_VERSION,
        name="建模运行恢复摘要",
        description="从持久化业务事实生成有界 Execution Brief",
        version="1",
    ))
    workflow.set_start_comp("start", Start())
    workflow.add_workflow_comp(
        "build_brief",
        _ExecutionBriefComponent(),
        inputs_schema={"payload_json": "${payload_json}"},
        timeout=5.0,
    )
    workflow.set_end_comp(
        "end",
        End(),
        inputs_schema={"prompt": "${build_brief.prompt}"},
    )
    workflow.add_connection("start", "build_brief")
    workflow.add_connection("build_brief", "end")
    return workflow


def build_tool_redirect_workflow() -> Workflow:
    workflow = Workflow(WorkflowCard(
        id=TOOL_REDIRECT_WORKFLOW_VERSION,
        name="领域工具确定性转向",
        description="把受管文件拒绝转换为有界、可检查点恢复的领域动作",
        version="1",
    ))
    workflow.set_start_comp("start", Start())
    workflow.add_workflow_comp(
        "build_redirect",
        _ToolRedirectComponent(),
        inputs_schema={"payload_json": "${payload_json}"},
        timeout=5.0,
    )
    workflow.set_end_comp(
        "end",
        End(),
        inputs_schema={"prompt": "${build_redirect.prompt}"},
    )
    workflow.add_connection("start", "build_redirect")
    workflow.add_connection("build_redirect", "end")
    return workflow


def render_execution_brief(payload: dict[str, Any]) -> str:
    return (
        "上一次模型执行没有完整结束。下面是服务端从业务事实重新生成的执行摘要；"
        "超时前的半段文字不是完成证明，不要复述或补写那段文字。\n"
        "先调用 get_project_state 和 get_change_plan 核对最新任务。已有任务状态只能以 Core 回执为准；"
        "若某个 Change Set 结果不明，必须先调用 get_change_set_receipt，禁止重复提交。"
        "只继续下一个未完成任务，不重做已完成写入。\n\n"
        "# Execution Brief\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )[:12_000]


def execution_brief_prompt(output: Any) -> str:
    result = getattr(output, "result", None)
    if isinstance(result, dict):
        nested = result.get("output")
        if isinstance(nested, dict) and isinstance(nested.get("prompt"), str):
            return nested["prompt"]
        if isinstance(result.get("prompt"), str):
            return result["prompt"]
    return ""


def render_tool_redirect(payload: dict[str, Any]) -> str:
    required_tool = str(payload.get("requiredTool") or "")
    return (
        "刚才的自由文件修改已被 Core 安全阻止，没有写入候选。"
        "这是可由系统恢复的技术路径选择，不是用户业务问题。"
        "先读取当前建模计划、目标资源和真实数据表；参数完整后立即执行指定领域动作 "
        f"{required_tool}。若字段或主键确有多个业务解释，必须附原文证据再调用 ask；"
        "否则不得声称环境无法处理、不得要求人工补写，也不得重试自由文件写入。"
        "动作成功后重新校验完整候选并恢复原计划。\n\n"
        "# Tool Redirect\n"
        f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
    )[:8_000]


def tool_redirect_prompt(output: Any) -> str:
    return execution_brief_prompt(output)
