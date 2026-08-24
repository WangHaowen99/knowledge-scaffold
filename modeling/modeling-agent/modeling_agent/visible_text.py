from __future__ import annotations

import re


_TOKEN_CHARACTERS = frozenset("_:/?&.=%-#")
_INTERNAL_ID = re.compile(
    r"^(rd|tc|run|session|interaction)[_:-][A-Za-z0-9:_-]+$",
    re.IGNORECASE,
)
_INTERNAL_RESOURCE = re.compile(r"^(?:mv_shadow_)?pj[_:-][A-Za-z0-9:_-]+$", re.IGNORECASE)
_CHECKPOINT_ID = re.compile(r"^cp[-_][A-Za-z0-9-]+$", re.IGNORECASE)
_HEX_REVISION = re.compile(r"^(?=[a-f0-9]{7,64}$)(?=.*[a-f])[a-f0-9]+$", re.IGNORECASE)
_STATE_CODE = re.compile(
    r"^(intake|goal|process|knowledge|increment|validation|evaluation|release):"
    r"(?:idle|queued|running|waiting|suspended|completed|failed|cancelled)$",
    re.IGNORECASE,
)

_PHASE_LABELS = {
    "intake": "资料准备",
    "goal": "目标确认",
    "process": "流程建模",
    "knowledge": "知识建模",
    "increment": "增量建模",
    "validation": "确定性验证",
    "evaluation": "正式评测",
    "release": "发布交付",
}

_TOKEN_LABELS = {
    "focus": "定位内容",
    "notify": "更新进度",
    "ask": "业务问题",
    "list_documents": "查看资料清单",
    "read_document": "读取资料",
    "set_material_directive": "保存资料设置",
    "read_documents_fully": "全文精读资料",
    "search_documents": "检索资料",
    "list_tables": "查看数据表",
    "describe_table": "分析数据表",
    "read_table_rows": "读取表格样例",
    "stage_tabular_dataset": "同步完整数据表",
    "list_dir": "查看知识包目录",
    "read_file": "读取知识内容",
    "write_file": "整理待确认修改",
    "delete_file": "整理候选删除",
    "discard_rejected_candidate_changes": "放弃已退回的修改",
    "regroup_changes": "整理确认分组",
    "grep_package": "检索知识包",
    "request_release": "发布确认",
    "release_preflight": "发布条件核对",
    "get_project_state": "当前进展核对",
    "transition_project_state": "进入下一阶段",
    "update_project_context": "保存当前进展",
    "update_plan": "更新计划",
    "run_evaluation": "运行评测",
    "get_evaluation_results": "查看评测结果",
    "gate_status": "条件核对",
    "who_references": "核对引用关系",
    "analyze_material_impact": "核对资料变更影响",
    "list_package_sources": "查看来源知识包",
    "list_package_fusion_plans": "查看融合计划",
    "get_package_fusion_plan": "读取融合计划",
    "create_package_fusion_plan": "建立融合计划",
    "update_package_fusion_plan": "登记元素去向",
    "compile_package_fusion_plan": "生成融合候选",
    "analyze_package_merge": "核对融合范围",
    "read_package_source_file": "读取来源包内容",
    "stage_package_merge": "生成融合候选",
    "diff": "查看变更对照",
    "request_confirmation": "内容确认",
    "claim_candidate_workspace": "准备本次修改",
    "evaluation_passed": "正式评测",
    "ready_for_eval": "评测准备",
    "releasable": "发布条件",
    "revision": "版本",
    "query_eval_db": "查询评测数据",
    "run_script_test": "测试确定性脚本",
    "agent_project_workflow_state": "项目进展",
    "project_agent_state": "项目进展",
    "modeling_leader": "建模助手",
    "material_analyst": "资料分析",
    "knowledge_extractor": "知识分析",
    "merge_analyst": "融合分析",
    "consumer": "下游应用",
}


def _is_token_character(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character in _TOKEN_CHARACTERS)


def _sanitize_token(token: str) -> str:
    if _INTERNAL_RESOURCE.fullmatch(token):
        return "当前处理"
    internal_id = _INTERNAL_ID.fullmatch(token)
    if internal_id:
        prefix = internal_id.group(1).lower()
        if prefix == "rd":
            return "本次评测"
        if prefix == "tc":
            return "该用例"
        return "当前处理"
    if _CHECKPOINT_ID.fullmatch(token):
        return "最近保存点"
    if _HEX_REVISION.fullmatch(token):
        return "当前版本"
    state = _STATE_CODE.fullmatch(token)
    if state:
        return _PHASE_LABELS[state.group(1).lower()]

    lowered = token.lower()
    if lowered in _TOKEN_LABELS:
        return _TOKEN_LABELS[lowered]
    if lowered.startswith(("/api/", "api/")) or (
        lowered.startswith(("http://", "https://"))
        and ("/api/" in lowered or "//localhost" in lowered or "//127.0.0.1" in lowered)
    ):
        return "内部链接"
    if lowered == ".modeling" or lowered.startswith(".modeling/"):
        return "项目工作记录"
    return token


class VisibleTextSanitizer:
    """Sanitize completed ASCII tokens while preserving live Chinese output."""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, text: str) -> str:
        visible: list[str] = []
        for character in text:
            if _is_token_character(character):
                self._pending += character
                continue
            if self._pending:
                visible.append(_sanitize_token(self._pending))
                self._pending = ""
            visible.append(character)
        return "".join(visible)

    def finish(self) -> str:
        if not self._pending:
            return ""
        visible = _sanitize_token(self._pending)
        self._pending = ""
        return visible


def sanitize_visible_text(text: str) -> str:
    sanitizer = VisibleTextSanitizer()
    return sanitizer.feed(text) + sanitizer.finish()
