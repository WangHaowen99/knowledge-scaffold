# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""MCP search tool descriptions and input contract helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


SEARCH_TOOL_NAMES = [
    "get_object_types",
    "get_relation_types",
    "get_nodes_schema",
    "get_edges_schema",
    "property_filter",
    "pattern_search",
    "count_search",
    "aggregate_search",
    "sorted_search",
    "get_element_info",
    "hop_search",
    "shortest_path",
    "list_logic_titles",
    "get_logic_content",
]

PATH_PATTERN_SHAPE_EXAMPLE = (
    '[[节点选择器, {}], [关系类型, "方向"], [节点选择器, {}]]'
)

SEARCH_TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_object_types": (
        "获取当前本体绑定图数据中的全部实体/节点类型。适合在不知道图谱结构时先探索，"
        "或在调用 property_filter、count_search 等工具前确认 element_class 可选值。"
    ),
    "get_relation_types": (
        "获取当前本体绑定图数据中的全部语义关系类型。适合在构造 pattern_search 前确认可用 relation_type；"
        "当物理边类型固定为 D2O_RELATION 时，此工具返回真正有业务意义的关系类型。"
    ),
    "get_nodes_schema": (
        "获取节点类型及其属性字段 schema。可传 element_types 只查看指定节点类型；不传时返回全部类型。"
        "此工具只返回字段结构，不返回实例数据。"
    ),
    "get_edges_schema": (
        "获取关系 schema 和实体类型间关系三元组 relation_triples。路径类问题应先查看 relation_triples，"
        "再据此构造 pattern_search，避免只靠属性盲查。"
    ),
    "property_filter": (
        "按属性过滤节点或关系，支持一次提交多个独立子查询。单个 filter_dict 内多个字段表示同一对象的 AND 条件；"
        "queries 表示多个独立问题，结果按 query_id 分组。return_properties 只能省略或传非空字段名数组，"
        "字段必须来自 schema。"
    ),
    "pattern_search": (
        "按图结构路径匹配子图数据。适合用户描述了明确实体关系路径、需要查询 A 经某关系到 B 的场景。"
        "建议先调用 get_edges_schema，依据 relation_triples 构造 path_pattern。path_pattern 每次只描述一条路径，"
        "固定骨架为 [[节点选择器, 节点属性?], [关系选择器, 方向], [节点选择器, 节点属性?], ...]；"
        "一跳路径必须正好包含起点节点段、关系段、终点节点段 3 个顶层元素。每个节点段和关系段都必须是数组，"
        "不能把对象或字符串直接放在 path_pattern 顶层，也不要多包一层数组。方向只支持 '->'、'<-'、'--'；"
        "relation_triples 中 source_label=S, relation_type=R, target_label=T 表示 S -[R]-> T，"
        "从 S 查 T 用 [R,'->']，从 T 查回 S 用 [R,'<-']，不关心方向用 [R,'--']。"
        "return_vars 按当前 path_pattern 自动编号，一跳路径只有 a、r1、b。"
    ),
    "count_search": (
        "统计满足条件的节点或关系数量，支持批量子查询。每个子查询包含 element_class、element_type 和可选 filter_dict。"
    ),
    "aggregate_search": (
        "对节点或关系属性做 SUM/AVG/MIN/MAX/COUNT 聚合。element_class 是具体节点 label 或语义关系类型，"
        "element_type 只能是 NODE 或 EDGE；target_property 应来自 schema。"
    ),
    "sorted_search": (
        "查询节点或关系并按指定字段排序返回。适合最大/最小/最新/排名类问题；可配合 filter_dict 先过滤，"
        "再用 sort_by、ascending、limit、offset 控制排序和分页。"
    ),
    "get_element_info": (
        "按节点或关系实例 ID 获取单个元素完整详情。通常在 property_filter、pattern_search、hop_search 返回实例 ID 后进一步查看。"
    ),
    "hop_search": (
        "从指定节点实例出发查询 N 度邻域。entity_instance_id 应来自其他查询工具返回的实例 ID；"
        "hop_num 建议不超过 3，过大可能返回大量数据。"
    ),
    "shortest_path": (
        "查询两个节点之间的最短关联路径。用 start_label/start_props 和 end_label/end_props 定位起止节点；"
        "directed=false 时忽略方向，directed=true 时严格按关系方向搜索。"
    ),
    "list_logic_titles": (
        "列出当前本体绑定图数据中的业务逻辑标题目录。适合先查看有哪些业务规则/脚本/操作说明，再按标题加载内容。"
    ),
    "get_logic_content": (
        "按实体实例 ID 和可选 logic_titles 读取业务逻辑内容。常见路径是先调用 list_logic_titles，"
        "再将目录中的 entity_instance_id 和 logic_title 传入本工具。"
    ),
}

CHAT_TOOL_DESCRIPTION = (
    "与 MetaWise 主 Agent 进行单轮知识问答。适合需要自然语言规划、调用工具并整理答案的场景；"
    "如只需要确定性图数据检索，应优先使用知识检索工具集。"
)


@dataclass(frozen=True)
class ToolInputIssue:
    field: str
    detail: str
    hint: str
    error_code: str = "INVALID_TOOL_ARGUMENTS"


@dataclass(frozen=True)
class PatternInputs:
    path_pattern: list[Any]
    return_vars: list[str] | None


def list_search_tools() -> list[dict[str, str]]:
    return [
        {"name": name, "description": SEARCH_TOOL_DESCRIPTIONS.get(name, "")}
        for name in SEARCH_TOOL_NAMES
    ]


def normalize_search_tool_args(
    tool_name: str,
    args: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, ToolInputIssue | None]:
    name = str(tool_name or "").strip()
    normalized = dict(args or {})

    if name == "get_nodes_schema":
        return _normalize_optional_list_arg(normalized, "element_types")
    if name == "get_edges_schema":
        return _normalize_optional_list_arg(normalized, "relation_types")
    if name == "property_filter":
        return _normalize_queries_arg(
            normalized,
            "queries",
            hint="请将 queries 改为合法 JSON 数组，且每项包含 element_class/element_type/filter_dict。",
        )
    if name == "count_search":
        return _normalize_queries_arg(
            normalized,
            "queries",
            hint="请将 queries 改为合法 JSON 数组，且每项包含 element_class/element_type/filter_dict。",
        )
    if name == "aggregate_search":
        return _normalize_dict_arg(normalized, "filter_dict", default={})
    if name == "sorted_search":
        normalized, issue = _normalize_dict_arg(normalized, "filter_dict", default={})
        if issue:
            return None, issue
        normalized, issue = _normalize_optional_list_arg(normalized, "return_properties")
        if issue:
            return None, issue
        return _normalize_optional_list_arg(normalized, "sort_by")
    if name == "pattern_search":
        pattern, issue = normalize_pattern_inputs(
            normalized.get("path_pattern"),
            normalized.get("return_vars"),
        )
        if issue:
            return None, issue
        normalized["path_pattern"] = pattern.path_pattern
        normalized["return_vars"] = pattern.return_vars
        return normalized, None
    if name == "shortest_path":
        normalized, issue = _normalize_dict_arg(normalized, "start_props", default={})
        if issue:
            return None, issue
        return _normalize_dict_arg(normalized, "end_props", default={})
    if name == "get_logic_content":
        ids, issue = _normalize_string_list(
            normalized.get("entity_instance_ids"),
            field="entity_instance_ids",
            required=True,
        )
        if issue:
            return None, issue
        titles, issue = _normalize_string_list(
            normalized.get("logic_titles"),
            field="logic_titles",
            required=False,
        )
        if issue:
            return None, issue
        normalized["entity_instance_ids"] = ids
        normalized["logic_titles"] = titles or []
        return normalized, None
    if name == "hop_search":
        if not str(normalized.get("entity_instance_id") or "").strip():
            return None, ToolInputIssue(
                field="entity_instance_id",
                detail="entity_instance_id 不能为空",
                hint="请传入 property_filter、pattern_search 或 get_element_info 返回的节点实例 ID。",
            )
    if name == "get_element_info":
        if not str(normalized.get("element_instance_id") or "").strip():
            return None, ToolInputIssue(
                field="element_instance_id",
                detail="element_instance_id 不能为空",
                hint="请传入其他检索工具返回的 entity_instance_id 或 relation_instance_id。",
            )
    return normalized, None


def normalize_pattern_inputs(
    path_pattern: Any,
    return_vars: Any,
) -> tuple[PatternInputs | None, ToolInputIssue | None]:
    normalized_pattern, pattern_issue = _normalize_path_pattern(path_pattern)
    if pattern_issue:
        return None, pattern_issue

    normalized_return_vars, return_vars_issue = _normalize_return_vars(return_vars)
    if return_vars_issue:
        return None, return_vars_issue

    return PatternInputs(normalized_pattern, normalized_return_vars), None


def _normalize_path_pattern(raw_pattern: Any) -> tuple[list[Any] | None, ToolInputIssue | None]:
    parsed_pattern, parse_issue = _parse_json_text(raw_pattern, field="path_pattern")
    if parse_issue:
        return None, parse_issue
    if not isinstance(parsed_pattern, list):
        return None, _path_issue(
            detail="path_pattern 必须是数组结构",
            hint=(
                "请把 path_pattern 改为原生数组参数；一跳路径形如 "
                "{}。".format(PATH_PATTERN_SHAPE_EXAMPLE)
            ),
        )

    shape_issue = _detect_common_path_shape_issue(parsed_pattern)
    if shape_issue:
        return None, shape_issue
    return parsed_pattern, None


def _normalize_return_vars(raw_return_vars: Any) -> tuple[list[str] | None, ToolInputIssue | None]:
    parsed_return_vars, parse_issue = _parse_json_text(raw_return_vars, field="return_vars")
    if parse_issue:
        return None, parse_issue
    if parsed_return_vars is None:
        return None, None
    if not isinstance(parsed_return_vars, list):
        return None, ToolInputIssue(
            field="return_vars",
            detail="return_vars 必须是字符串数组",
            hint='请把 return_vars 改为字符串数组，例如 ["a", "r1", "b"]。',
        )
    return [str(item) for item in parsed_return_vars], None


def _normalize_queries_arg(
    args: dict[str, Any],
    field: str,
    *,
    hint: str,
) -> tuple[dict[str, Any] | None, ToolInputIssue | None]:
    parsed, issue = _parse_json_text(args.get(field), field=field)
    if issue:
        return None, issue
    if not isinstance(parsed, list) or not parsed:
        return None, ToolInputIssue(
            field=field,
            detail="{} 必须是非空数组".format(field),
            hint=hint,
        )
    args[field] = parsed
    return args, None


def _normalize_dict_arg(
    args: dict[str, Any],
    field: str,
    *,
    default: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, ToolInputIssue | None]:
    raw = args.get(field)
    if raw in (None, ""):
        args[field] = dict(default or {})
        return args, None
    parsed, issue = _parse_json_text(raw, field=field)
    if issue:
        return None, issue
    if not isinstance(parsed, Mapping):
        return None, ToolInputIssue(
            field=field,
            detail="{} 必须是 JSON 对象".format(field),
            hint="请把 {} 改为 JSON 对象，例如 {{}}。".format(field),
        )
    args[field] = dict(parsed)
    return args, None


def _normalize_optional_list_arg(
    args: dict[str, Any],
    field: str,
) -> tuple[dict[str, Any] | None, ToolInputIssue | None]:
    values, issue = _normalize_string_list(args.get(field), field=field, required=False)
    if issue:
        return None, issue
    args[field] = values
    return args, None


def _normalize_string_list(
    raw: Any,
    *,
    field: str,
    required: bool,
) -> tuple[list[str] | None, ToolInputIssue | None]:
    parsed, issue = _parse_json_text(raw, field=field)
    if issue:
        return None, issue
    if parsed in (None, ""):
        if required:
            return None, ToolInputIssue(
                field=field,
                detail="{} 不能为空".format(field),
                hint="请把 {} 改为字符串数组。".format(field),
            )
        return None, None
    if not isinstance(parsed, list):
        return None, ToolInputIssue(
            field=field,
            detail="{} 必须是字符串数组".format(field),
            hint='请把 {} 改为字符串数组，例如 ["a", "r1", "b"]。'.format(field),
        )

    result: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)

    if required and not result:
        return None, ToolInputIssue(
            field=field,
            detail="{} 不能为空".format(field),
            hint="请把 {} 改为非空字符串数组。".format(field),
        )
    return result or None, None


def _parse_json_text(value: Any, *, field: str) -> tuple[Any, ToolInputIssue | None]:
    if not isinstance(value, str):
        return value, None

    text = value.strip()
    if not text:
        return value, None
    if not ((text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}"))):
        return value, None

    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, ToolInputIssue(
            field=field,
            detail="{} 不是合法 JSON: {}".format(field, exc.msg),
            hint=(
                "不要把 {} 写成非法字符串；请直接传原生 JSON 数组/对象，"
                "或传合法 JSON 字符串。"
            ).format(field),
            error_code="INVALID_QUERY_SCHEMA",
        )


def _detect_common_path_shape_issue(path_pattern: list[Any]) -> ToolInputIssue | None:
    if _looks_like_extra_wrapped_path(path_pattern):
        return _path_issue(
            detail="path_pattern 多包了一层数组，当前顶层只有一个完整路径对象",
            hint=(
                "去掉最外层数组；pattern_search 每次只传一个路径模式，"
                "一跳路径形如 {}。"
            ).format(PATH_PATTERN_SHAPE_EXAMPLE),
        )

    for index, segment in enumerate(path_pattern):
        if not isinstance(segment, list):
            return _segment_issue(index)
    return None


def _looks_like_extra_wrapped_path(path_pattern: list[Any]) -> bool:
    if len(path_pattern) != 1 or not isinstance(path_pattern[0], list):
        return False
    inner = path_pattern[0]
    if len(inner) < 3 or len(inner) % 2 == 0:
        return False
    return all(isinstance(segment, list) for segment in inner)


def _segment_issue(index: int) -> ToolInputIssue:
    role = "节点段" if index % 2 == 0 else "关系段"
    return _path_issue(
        detail="path_pattern[{}] 必须是数组".format(index),
        hint=(
            "{}必须写成数组，不能把对象或字符串直接放在 path_pattern 顶层；"
            "一跳路径形如 {}。"
        ).format(role, PATH_PATTERN_SHAPE_EXAMPLE),
    )


def _path_issue(*, detail: str, hint: str) -> ToolInputIssue:
    return ToolInputIssue(
        field="path_pattern",
        detail=detail,
        hint=hint,
        error_code="INVALID_QUERY_SCHEMA",
    )
