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
    r"(?:idle|queued|running|stopping|waiting|suspended|completed|failed|cancelled)$",
    re.IGNORECASE,
)
_PRIVATE_REASONING = re.compile(
    r"(?:system\s+prompt|developer\s+message|api[_-]?key|authorization|bearer\s+[A-Za-z0-9._-]+|"
    r"password|service[_-]?token|run[_-]?credential|token[_-]?fingerprint|sk-[A-Za-z0-9_-]{8,}|"
    r"tool\s*(?:call|arguments?)|工具参数|系统指令|系统提示|系统消息|开发者消息|密钥|认证信息|访问令牌|"
    r"工具集中没有|工具集没有|没有(?:该|读取)能力|本会话工具|平台侧能力缺口|平台能力缺口|平台是否开放|"
    r"[\"']?(?:arguments|path|projectId|runId|sessionId)[\"']?\s*[:=])",
    re.IGNORECASE,
)
_REASONING_BOUNDARY = frozenset("\n。！？；")

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
    "create_change_plan": "建立建模计划",
    "get_change_plan": "核对建模计划",
    "revise_change_plan": "调整建模计划",
    "stage_change_set": "原子生成待确认修改",
    "get_change_set_receipt": "核对修改回执",
    "validate_change_set": "校验完整建模计划",
    "discard_change_set": "放弃待确认修改",
    "list_domain_resources": "查看领域资源",
    "get_domain_resource": "读取资源快照",
    "create_object": "创建业务对象",
    "create_rule": "创建业务规则",
    "create_operation": "创建业务操作",
    "create_metric": "创建业务指标",
    "patch_object": "增量修改对象",
    "patch_rule": "增量修改规则",
    "patch_operation": "增量修改操作",
    "patch_metric": "增量修改指标",
    "sync_schema_objects": "按数据 Schema 同步对象",
    "suggest_object_relations": "推荐对象关系",
    "patch_object_source_binding": "更新对象数据绑定",
    "preview_domain_delete": "核对删除影响",
    "delete_domain_resource": "删除领域资源",
    "define_script_contract": "定义脚本契约",
    "get_script_contract": "读取脚本契约",
    "get_script_edit_context": "读取脚本符号上下文",
    "stage_script_patch": "应用脚本符号补丁",
    "stage_script_source": "校验脚本源码",
    "write_file": "整理待确认修改",
    "upsert_object": "整理业务对象",
    "delete_file": "整理候选删除",
    "upsert_rule": "整理业务规则",
    "upsert_operation": "整理业务操作",
    "bind_object_source": "绑定对象数据来源",
    "bind_object_sources": "绑定跨 Sheet 对象来源",
    "get_schema_mapping_plan": "核对数据 Schema 映射计划",
    "get_product_ledger": "读取产品清单台账",
    "stage_product_ledger": "校验产品清单台账",
    "required_text_constraint": "绑定补充说明",
    "get_validation_summary": "查看校验摘要",
    "get_validation_findings": "查看校验明细",
    "discard_rejected_candidate_changes": "放弃已退回的修改",
    "regroup_changes": "整理确认分组",
    "grep_package": "检索知识包",
    "request_release": "发布确认",
    "release_preflight": "发布条件核对",
    "get_project_state": "当前进展核对",
    "transition_project_state": "进入下一阶段",
    "update_project_context": "保存当前进展",
    "update_work_notes": "更新工作笔记",
    "update_plan": "更新历史工作笔记",
    "run_evaluation": "运行评测",
    "get_evaluation_results": "查看评测结果",
    "gate_status": "条件核对",
    "analyze_deterministic_capabilities": "分析确定性计算范围",
    "confirm_deterministic_scope": "确认脚本范围",
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
    "deterministic_confirmed": "确定性计算范围确认",
    "product_ledger_verified": "产品清单台账",
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


class VisibleReasoningSanitizer:
    """Stream public reasoning while dropping lines that may expose runtime secrets."""

    def __init__(self) -> None:
        self._visible = VisibleTextSanitizer()
        self._pending = ""

    def feed(self, text: str) -> str:
        self._pending += self._visible.feed(text)
        return self._drain(complete=False)

    def finish(self) -> str:
        self._pending += self._visible.finish()
        return self._drain(complete=True)

    def _drain(self, *, complete: bool) -> str:
        parts: list[str] = []
        start = 0
        for index, character in enumerate(self._pending):
            if character not in _REASONING_BOUNDARY:
                continue
            parts.append(_sanitize_reasoning_segment(self._pending[start:index + 1]))
            start = index + 1
        if complete:
            if start < len(self._pending):
                parts.append(_sanitize_reasoning_segment(self._pending[start:]))
            self._pending = ""
        else:
            self._pending = self._pending[start:]
            if len(self._pending) > 800:
                parts.append(_sanitize_reasoning_segment(self._pending))
                self._pending = ""
        return "".join(parts)


def _sanitize_reasoning_segment(segment: str) -> str:
    if not segment:
        return ""
    if _PRIVATE_REASONING.search(segment):
        suffix = "\n" if segment.endswith("\n") else ""
        return f"（内部配置已隐藏）{suffix}"
    if len(segment) > 600:
        suffix = "\n" if segment.endswith("\n") else ""
        return f"（较长的资料摘录已隐藏）{suffix}"
    return segment
