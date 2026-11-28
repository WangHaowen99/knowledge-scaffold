"""知识包导出的 operation 目录行与调用关系（operations/index.md、frontmatter uses.operations）。

consumer 选 operation 时逐行比对 operations/index.md 的「输出」列，并优先选输出覆盖完整结论的
主操作（consumer/backend/agent/prompts/universal.py）；它按 frontmatter 的 uses.operations
识别主操作与子操作，没有自己脚本的子操作借用主操作的脚本跑完整流程。表头为
operation key / 标题 / 输入 / 输出，不能加列。
"""

from __future__ import annotations

from .toolkit import operation_relations


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
    """操作目录的一行。

    实现在工具箱 operation_relations：consumer 对 operations/index.md 也查单文件字符上限，
    规则检查按同一份实现估算目录大小。
    """
    return operation_relations.index_row(key, body, parents, package_key=package_key, execution=execution)
