#!/usr/bin/env python3
"""operation 之间的调用关系：主操作、子操作与「可单独调用」。

唯一事实源是 operation 文档「# 与operation的关联」一节：

    - 被operation调用 > [[父operation]]          有值：本 operation 是子操作
    - 调用operation > [[子operation]]、[[...]]    有值：本 operation 是主操作（编排者）
    - 可单独调用 > 是 / 否                        只对子操作有意义，缺省为「否」

规则检查（ontology_checker.py）、脚本绑定规则（script_descriptor.py）与知识包导出
（operation frontmatter 的 uses.operations）共用本模块。仅依赖标准库。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# 同目录的兄弟脚本；后端经共享契约库加载时，由契约库解析到同一版本
import onto_metamodel as mm

RELATION_SECTION = "与operation的关联"
LEGACY_RELATION_SECTION = "与logic的关联"
LOGIC_SECTION = "逻辑描述"
STANDALONE_YES = "是"
STANDALONE_NO = "否"
OPERATION_DIRS = ("operation", "logic")  # logic 为旧目录兼容（ADR-0004）

_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)")
_CALLEE_LINE_RE = re.compile(r"^\s*[-•]?\s*调用\s*operation\b", re.IGNORECASE)
_CALLER_LINE_RE = re.compile(r"^\s*[-•]?\s*被\s*operation\s*调用\b", re.IGNORECASE)
_STANDALONE_LINE_RE = re.compile(r"^\s*[-•]?\s*可单独调用\s*[>＞:：]\s*(.*)$")
_CALL_SITE_LINK_RE = re.compile(r"调用\[\[([^\]]+)\]\]逻辑文档")
_CALL_SITE_QUOTED_RE = re.compile(r'调用\s*"([^"]+)"\s*逻辑文档')
# 规范写法「不满足前提时终止流程并输出异常说明」（OPERATION_STYLE_GUIDE 前置校验）；排除「结束日期」这类字段名
_STOP_CONDITION_RE = re.compile(r"(终止|结束|停止)(?!日期|时间|日|时)|不再执行|跳过后续")


@dataclass(frozen=True)
class OperationRelation:
    callers: tuple[str, ...] = ()  # 「被operation调用」声明的父 operation
    callees: tuple[str, ...] = ()  # 「调用operation」与逻辑描述调用点里的子 operation，按出现顺序
    standalone: str = ""  # 「可单独调用」原文取值；未声明为空串


def sections(markdown: str) -> dict[str, str]:
    """按 1~3 级标题切分章节；与 ontology_checker.extract_sections 同一口径。"""
    result: dict[str, str] = {}
    current = None
    buffer: list[str] = []
    for line in str(markdown or "").split("\n"):
        match = _HEADING_RE.match(line)
        if match:
            if current is not None:
                result[current] = "\n".join(buffer).strip()
            current = match.group(1).strip().rstrip("：:").strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        result[current] = "\n".join(buffer).strip()
    if LEGACY_RELATION_SECTION in result and RELATION_SECTION not in result:
        result[RELATION_SECTION] = result.pop(LEGACY_RELATION_SECTION)
    return result


def _link_stem(link) -> str:
    """链接（或链接内文）按元模型解析（onto_metamodel）；保留 `名称#锚点` 全名，由调用方按条目名解析。"""
    if isinstance(link, str):
        link = mm.parse_link_text(link)
    stem = link.full_name
    return "" if link.name in mm.EMPTY_TARGETS else stem


def _ordered_link_stems(text: str) -> list[str]:
    stems: list[str] = []
    for link in mm.parse_links(text):
        stem = _link_stem(link)
        if stem and stem not in stems:
            stems.append(stem)
    return stems


def link_stems(text: str) -> set[str]:
    """[[stem]] 目标集合：去掉别名、路径前缀与 .md 后缀，忽略「无」。"""
    return set(_ordered_link_stems(text))


def called_operation_stems(relation_section: str) -> set[str]:
    """「调用operation」行声明的子 operation。"""
    stems: set[str] = set()
    for line in relation_section.split("\n"):
        if _CALLEE_LINE_RE.match(line):
            stems |= link_stems(line)
    return stems


def caller_operation_stems(relation_section: str) -> set[str]:
    """「被operation调用」行声明的父 operation。"""
    stems: set[str] = set()
    for line in relation_section.split("\n"):
        if _CALLER_LINE_RE.match(line):
            stems |= link_stems(line)
    return stems


def sub_operation_call_sites(logic_text: str) -> list[str]:
    """逻辑描述中的子 operation 调用点（调用[[X]]逻辑文档），按出现顺序、保留重复。"""
    refs = _CALL_SITE_LINK_RE.findall(logic_text)
    refs += _CALL_SITE_QUOTED_RE.findall(logic_text)
    return refs


def parse_relation(markdown: str) -> OperationRelation:
    doc_sections = sections(markdown)
    relation_section = doc_sections.get(RELATION_SECTION, "")
    callers: list[str] = []
    callees: list[str] = []
    standalone = ""
    for line in relation_section.split("\n"):
        if _CALLEE_LINE_RE.match(line):
            callees += [stem for stem in _ordered_link_stems(line) if stem not in callees]
        elif _CALLER_LINE_RE.match(line):
            callers += [stem for stem in _ordered_link_stems(line) if stem not in callers]
        else:
            match = _STANDALONE_LINE_RE.match(line)
            if match:
                standalone = match.group(1).strip()
    for ref in sub_operation_call_sites(doc_sections.get(LOGIC_SECTION, "")):
        stem = _link_stem(ref)
        if stem and stem not in callees:
            callees.append(stem)
    return OperationRelation(tuple(callers), tuple(callees), standalone)


def standalone_flag(relation: OperationRelation) -> bool | None:
    """「可单独调用」：是 → True，否或未填 → False，其他取值 → None（规则检查报错）。"""
    value = relation.standalone
    if not value or value.startswith(STANDALONE_NO):
        return False
    if value.startswith(STANDALONE_YES):
        return True
    return None


def stops_before_call(parent_markdown: str, child: str) -> bool:
    """父 operation 的逻辑描述在第一次提到子 operation 之前有没有停止条件（输入检查、前置判断等）。

    有的话，子 operation 的结论依赖这些检查：单独执行它会跳过检查，结论可能和走完整流程不一致。
    按 `[[子operation]]` 第一次出现的位置算，「依次调用[[A]]、[[B]]逻辑文档」这类写法也能定位。
    """
    logic = sections(parent_markdown).get(LOGIC_SECTION, "")
    first = next((link.span[0] for link in mm.parse_links(logic) if child in (link.full_name, link.name)), None)
    return first is not None and bool(_STOP_CONDITION_RE.search(logic[:first]))


def load_operation_documents(ontology_dir: Path) -> dict[str, str]:
    """ontology/operation（含旧 ontology/logic）下的 operation 文档：文件名 → 原文。同名取先读到的。"""
    documents: dict[str, str] = {}
    for subdir in OPERATION_DIRS:
        directory = Path(ontology_dir) / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.md")):
            if path.name.lower() == "index.md" or path.stem in documents:
                continue
            try:
                documents[path.stem] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return documents


def relations_from_documents(documents: dict[str, str]) -> dict[str, OperationRelation]:
    return {key: parse_relation(text) for key, text in documents.items()}


def load_operation_relations(ontology_dir: Path) -> dict[str, OperationRelation]:
    return relations_from_documents(load_operation_documents(ontology_dir))


def parent_map(relations: dict[str, OperationRelation]) -> dict[str, set[str]]:
    """子 operation → 父 operation：「被operation调用」与父文档「调用operation」两边的声明取并集。"""
    parents: dict[str, set[str]] = {}
    for key, relation in relations.items():
        for callee in relation.callees:
            if callee != key:
                parents.setdefault(callee, set()).add(key)
        for caller in relation.callers:
            if caller != key:
                parents.setdefault(key, set()).add(caller)
    return parents


def ancestors(key: str, parents: dict[str, set[str]]) -> list[str]:
    """沿父 operation 逐层向上，近的在前；有环时每个 operation 只走一次。"""
    found: list[str] = []
    frontier = sorted(parents.get(key, ()))
    while frontier:
        next_frontier: list[str] = []
        for parent in frontier:
            if parent == key or parent in found:
                continue
            found.append(parent)
            next_frontier += sorted(parents.get(parent, ()))
        frontier = next_frontier
    return found
