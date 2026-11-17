"""子代理交回的结果 → 给人看的说法（子任务卡第二行、进度面板的「结果」、EC-22 与 S-21 的 summary）。

检查、修复、影响分析这类子代理按约定只交回 JSON（规则检查没问题时就是 `[]`），那是给主助手解析的，原样显示
就是一串符号。这里按条目的字段认出是哪类结果，换成第一行结论、其后每行「· 」开头一条明细；认不出、或截断了
解析不了的给空串，界面用默认说法。引擎拿到完整结果时换一次（截断前），读历史时再换一次（兼容已存的旧记录）；
不是 JSON 的话原样不动，已换好的说法再过一遍也不变。
"""

from __future__ import annotations

import json
import re

from . import workspace_labels

DETAIL_MAX = 5
_LINE_MAX = 80
_FENCE = re.compile(r"^```[A-Za-z]*\s*\n?(.*?)\n?```$", re.S)
# 截断了也要认得：开头是 `[{`、`[]`、`["` 或 `{"`；`[[链接]]` 开头的话不算
_MACHINE_HEAD = re.compile(r'^(?:\[\s*(?:[{\]"]|$)|\{\s*")')

_SEVERITY = {"错误": "错误", "error": "错误", "critical": "错误", "警告": "警告", "warning": "警告", "warn": "警告",
             "建议": "建议", "提示": "建议", "info": "建议", "suggestion": "建议"}
_SEVERITY_ORDER = ("错误", "警告", "建议", "其他")
_IMPACT = {"create": "新建", "modify": "修改", "delete": "删除"}
_NOT_APPLIED = {"needs_human_judgment": "要人工判断", "file_not_found": "找不到文件",
                "business_knowledge_conflict": "和业务知识冲突", "proposal_ambiguous": "改法不明确"}


def _unfence(text) -> str:
    body = str(text or "").strip()
    match = _FENCE.match(body)
    return match.group(1).strip() if match else body


def is_machine(text) -> bool:
    """这段是不是交给主助手解析的 JSON（含截断了的）。"""
    return bool(_MACHINE_HEAD.match(_unfence(text)))


def summarize(text) -> str:
    """JSON 结果 → 说法；认不出给空串。`[]` 后面跟一句说明的（影响分析无需改动时）用那句说明。"""
    body = _unfence(text)
    try:
        value, end = json.JSONDecoder().raw_decode(body)
    except ValueError:
        return ""
    if isinstance(value, dict):
        return _extraction(value)
    if not isinstance(value, list):
        return ""
    if not value:
        return " ".join(body[end:].split())[:200] or "没有发现问题"
    if not all(isinstance(item, dict) for item in value):
        return f"交回 {len(value)} 条结果"
    for marker, render in _LIST_KINDS:
        if any(marker(item) for item in value):
            return render(value)
    return f"交回 {len(value)} 条结果"


def display(text) -> str | None:
    """读历史时用：存的是 JSON 原文的换成说法（换不出给 None）；别的原样。"""
    if not text:
        return None
    if not is_machine(text):
        return text
    return summarize(text) or None


def _clip(text) -> str:
    line = " ".join(str(text or "").split())
    return line[:_LINE_MAX] + ("…" if len(line) > _LINE_MAX else "")


def _compose(head: str, details: list[str]) -> str:
    details = [_clip(line) for line in details if str(line or "").strip()]
    shown = [f"· {line}" for line in details[:DETAIL_MAX]]
    if len(details) > DETAIL_MAX:
        shown.append(f"· …另有 {len(details) - DETAIL_MAX} 条")
    return "\n".join([head, *shown])


def _label(path) -> str:
    rel = str(path or "").strip().lstrip("./")
    if not rel:
        return ""
    return workspace_labels.path_label(rel) or workspace_labels.path_label(f"ontology/{rel}")


def _counts(groups: dict[str, list], order, unit: str = "") -> str:
    return " · ".join(f"{key} {len(groups[key])}{unit}" for key in order if groups.get(key))


def _issues(items: list[dict]) -> str:
    """规则检查、语义检查、脚本复核、知识冲突：按严重程度计数，明细按严重程度排。"""
    groups: dict[str, list] = {}
    for item in items:
        level = _SEVERITY.get(str(item.get("severity") or "").strip().lower(), "其他")
        groups.setdefault(level, []).append(item)
    details = []
    for level in _SEVERITY_ORDER:
        for item in groups.get(level, []):
            where = _label(item.get("file"))
            message = workspace_labels.humanize(str(item.get("message") or item.get("category") or ""))
            details.append(f"{level} · {where}：{message}" if where else f"{level} · {message}")
    return _compose(f"发现 {len(items)} 个问题：{_counts(groups, _SEVERITY_ORDER)}", details)


def _fixes(items: list[dict]) -> str:
    """修复代理：改好几条、几条没改；没改的排前面（要人接着处理）。"""
    done = [item for item in items if item.get("applied") is True]
    left = [item for item in items if item.get("applied") is not True]
    head = "，".join(part for part in (f"改好 {len(done)} 条" if done else "", f"{len(left)} 条没改" if left else "")
                    if part)
    details = [f"没改：{item.get('proposed_change') or _NOT_APPLIED.get(str(item.get('reason') or ''), '')}"
               for item in left if item.get("proposed_change") or str(item.get("reason") or "") in _NOT_APPLIED]
    details += [f"已改：{item['change_summary']}" for item in done if item.get("change_summary")]
    return _compose(head, details)


def _impact(items: list[dict]) -> str:
    """影响分析：要新建、修改、删除哪些条目。"""
    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(_IMPACT.get(str(item.get("operation") or ""), "其他"), []).append(item)
    details = []
    for item in items:
        verb = _IMPACT.get(str(item.get("operation") or ""), "改动")
        where = _label(item.get("file"))
        details.append(f"{verb}{where}：{item.get('change_summary') or ''}".rstrip("："))
    return _compose(f"涉及 {len(items)} 处：{_counts(groups, (*_IMPACT.values(), '其他'))}", details)


def _verdicts(items: list[dict]) -> str:
    """生成脚本前的判断：每个操作适不适合写成脚本。"""
    groups: dict[str, list] = {}
    for item in items:
        groups.setdefault(str(item.get("verdict") or "其他"), []).append(item)
    return f"判断了 {len(items)} 个操作：{_counts(groups, list(groups), ' 个')}"


def _scenarios(items: list[dict]) -> str:
    """场景推演：走通几个，没走通的说第一个问题。"""
    stuck = [item for item in items if item.get("issues") or item.get("reached_end") is False]
    head = f"推演 {len(items)} 个场景：走通 {len(items) - len(stuck)} 个" + (f"，{len(stuck)} 个有问题" if stuck else "")
    details = []
    for item in stuck:
        first = next((issue for issue in item.get("issues") or [] if isinstance(issue, dict)), {})
        details.append(f"{item.get('scenario') or '场景'}：{first.get('message') or '没有走到终点'}")
    return _compose(head, details)


def _extraction(value: dict) -> str:
    """规则与指标抽取：抽出几条，跳过的逐条说；别的对象认不出。"""
    if not any(isinstance(value.get(key), list) for key in ("rules", "metrics")):
        return ""
    parts = [f"规则 {len(value.get('rules') or [])} 条", f"指标 {len(value.get('metrics') or [])} 个"]
    skipped = [str(item) for item in value.get("skipped") or [] if str(item).strip()]
    return _compose(f"抽出{' · '.join(parts)}", [f"跳过：{item}" for item in skipped])


_LIST_KINDS = (
    (lambda item: "severity" in item, _issues),
    (lambda item: "applied" in item, _fixes),
    (lambda item: str(item.get("operation") or "") in _IMPACT and "file" in item, _impact),
    (lambda item: "verdict" in item, _verdicts),
    (lambda item: "scenario" in item, _scenarios),
)
