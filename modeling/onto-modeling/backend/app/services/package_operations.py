"""知识包导出的 operation 目录行与调用关系（operations/index.md、frontmatter uses.operations）。

consumer 选 operation 时逐行比对 operations/index.md 的「输出」列，并优先选输出覆盖完整结论的
主操作（consumer/backend/agent/prompts/universal.py）；它按 frontmatter 的 uses.operations
识别主操作与子操作，没有自己脚本的子操作借用主操作的脚本跑完整流程。表头沿用 modeling 知识包
契约（operation key / 标题 / 输入 / 输出），不能加列。
"""

from __future__ import annotations

import re

from .toolkit import operation_relations

_DIVIDER_CELL_RE = re.compile(r":?-{3,}:?")
_EMPTY_CELLS = {"", "无", "—", "-"}


def relation_view(operations: list[tuple[str, str]]) -> tuple[dict, dict[str, list[str]]]:
    """(各 operation 的调用关系, 子 operation → 包里存在的父 operation)。"""
    relations = operation_relations.relations_from_documents(dict(operations))
    keys = set(relations)
    parents = {
        key: sorted(parent for parent in found if parent in keys)
        for key, found in operation_relations.parent_map(relations).items()
        if key in keys
    }
    return relations, {key: value for key, value in parents.items() if value}


def uses_operations(key: str, relations: dict) -> list[str]:
    """本 operation 按顺序调用、且包里存在的子 operation。"""
    relation = relations.get(key)
    if relation is None:
        return []
    return [callee for callee in relation.callees if callee in relations and callee != key]


def index_row(
    key: str, body: str, parents: list[str], *, package_key: str = "", execution: str | None = None,
) -> str:
    """操作目录的一行。package_key 是包内 operation key（求解 operation 用 solver_key）；
    execution 不为 None 时追加 execution 列（包里有求解契约时整张表都有这一列）。"""
    sections = operation_relations.sections(body)
    inputs = "、".join(_parameter_names(sections.get("输入", ""))) or "无"
    outputs = "、".join(_parameter_names(sections.get("输出", ""))) or "无"
    if parents:
        outputs += "（" + "、".join(f"「{parent}」" for parent in parents) + "流程中的一步）"
    cells = [package_key or key, key, inputs, outputs]
    if execution is not None:
        cells.append(execution)
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |\n"


def _parameter_names(section_text: str) -> list[str]:
    """operation 文档输入/输出表的「参数名称」列；没有这一列时取第一列。"""
    rows = [line.strip() for line in section_text.split("\n") if line.strip().startswith("|")]
    if len(rows) < 2:
        return []
    headers = [cell.strip() for cell in rows[0].strip("|").split("|")]
    column = headers.index("参数名称") if "参数名称" in headers else 0
    names: list[str] = []
    for row in rows[1:]:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if all(_DIVIDER_CELL_RE.fullmatch(cell.replace(" ", "")) for cell in cells if cell):
            continue
        name = cells[column] if column < len(cells) else ""
        if name not in _EMPTY_CELLS and name not in names:
            names.append(name)
    return names


def _cell(text: str) -> str:
    return " ".join(str(text or "").split()).replace("|", "，")
