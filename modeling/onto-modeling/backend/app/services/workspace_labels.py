"""工作区路径 → 用户看到的业务名（F4b-paths，ADR-0074）：界面上不出工作区路径和文件名。

条目按元模型说「对象「门店」」，插件产物按它登记的叫法说「流程图「订单流程」」，资料按资料名，资料分析报告、
建模蓝图这类认得的文件按固定叫法；认不出的返回 None，由调用方合成「另有 N 处改动」。接口里的路径字段照留，
这里只算给人看的那一份。助手读的说明不走这里。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .workspace.layout import RESOURCE_DIRECTIVES, RESOURCE_TEXT_DIR, is_resource_text

FILE_TITLES = {"research/blueprint.json": "建模蓝图", RESOURCE_DIRECTIVES: "资料用途"}
RESEARCH_REPORT = "资料分析报告"
CHECK_REPORT = "检查报告"
MATERIAL = "资料"
MATERIAL_TYPES = {
    ".pdf": "PDF", ".doc": "Word", ".docx": "Word", ".xls": "Excel", ".xlsx": "Excel", ".xlsm": "Excel",
    ".csv": "CSV", ".drawio": "流程图", ".html": "网页", ".htm": "网页", ".png": "图片", ".jpg": "图片",
    ".jpeg": "图片", ".md": "Markdown", ".markdown": "Markdown", ".txt": "文本",
}
ENTRY_LABELS = {"object": "对象", "operation": "操作", "rule": "规则", "metric": "指标", "script": "脚本"}


def material_source(rel: str) -> str:
    """文本版 → 原资料路径（resource/text/others/合同.pdf.md → resource/others/合同.pdf）；其余原样。"""
    rel = str(rel or "").strip("/")
    if is_resource_text(rel) and rel.endswith(".md"):
        return "resource/" + rel[len(RESOURCE_TEXT_DIR) + 1:-len(".md")]
    return rel


def material_name(rel: str) -> str:
    """资料名：原文件名去掉扩展名。"""
    path = PurePosixPath(material_source(rel))
    return path.stem or path.name


def material_type(rel: str) -> str:
    return MATERIAL_TYPES.get(PurePosixPath(material_source(rel)).suffix.lower(), "文件")


def material(rel: str) -> dict:
    """出处、资料列表用的 {name, type, path}：path 是原资料（文本版映射回原文件）。"""
    return {"name": material_name(rel), "type": material_type(rel), "path": material_source(rel)}


def _is_material(rel: str) -> bool:
    return (rel.startswith("resource/") and rel != RESOURCE_DIRECTIVES and not is_resource_text(rel)
            and not rel.startswith("resource/systems/"))


def file_label(rel: str) -> str | None:
    """不是条目、也不是插件产物的文件：认得的给业务名，认不出给 None。"""
    rel = str(rel or "").strip("/")
    if rel in FILE_TITLES:
        return FILE_TITLES[rel]
    if rel.startswith("research/") and rel.endswith(".md"):
        return RESEARCH_REPORT
    if rel.startswith("reports/") and rel.endswith(".md") and not rel.startswith("reports/autobuild/"):
        return CHECK_REPORT
    if _is_material(rel):
        return f"{MATERIAL}「{material_name(rel)}」"
    return None


def entry_label(kind: str, name: str) -> str:
    return f"{ENTRY_LABELS.get(kind, '条目')}「{name}」"


def describe(rel: str, model=None, types=()) -> dict | None:
    """路径 → {group, target}；target 带 label（业务名）。认不出返回 None。

    group：条目类型 / scenario / artifact / other；model 是元模型（缺省用内置的），types 是本项目的产物登记。"""
    if model is None:
        from ..kernel import metamodel

        model = metamodel.mm
    entry = model.entry_key(rel)
    if entry:
        kind, _, key = entry.partition(":")
        return {"group": kind, "target": {"kind": kind, "id": entry, "key": key, "name": key,
                                          "label": entry_label(kind, key)}}
    atype = next((item for item in types if item.key_from_path(rel)), None)
    if atype is not None:
        key = atype.key_from_path(rel)
        group = "scenario" if atype.type.endswith(".scenario") else "artifact"
        return {"group": group, "target": {"type": atype.type, "key": key, "title": key,
                                           "label": f"{atype.title}「{key}」" if atype.title else f"「{key}」"}}
    label = file_label(rel)
    if label is None:
        return None
    return {"group": "other", "target": {"type": "file", "key": rel, "title": label, "label": label}}


def more_text(count: int) -> str:
    return f"另有 {count} 处改动"


def path_label(rel: str, model=None, types=()) -> str:
    """一处路径的业务名；认不出时空串（调用方不显示或合成「另有 N 处」）。"""
    info = describe(rel, model, types)
    return info["target"]["label"] if info else ""
