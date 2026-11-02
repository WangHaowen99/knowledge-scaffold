"""实体级差异（详设 4.4、8.2「合入时按实体检测冲突」；方案 C9；契约 CS-03）。

条目文档按元模型（`onto_metamodel.parse_document`，契约库按项目锁定版本加载）拆成章节、表格行与关系，
不再写一份解析。两个用途：

- `changes(model, kind, old, new)`：一个条目改了什么（哪些章节、哪些字段行、哪些关系），给「查看改动」
  （CS-03 的 `sections[].lines`）与变更卡的一句话说明；
- `conflicts(...)`：合入时主线与变更集改到同一实体即冲突（两边改成一样的不算）。

只看条目（对象、操作、规则、指标、脚本）；别的文件按整份文本比，冲突交给 git 的三方合并判断。
"""

from __future__ import annotations

import difflib
from types import ModuleType

MAX_LINES_PER_SECTION = 80
MAX_TEXT_LINES = 200
_VERB = {"add": "新增", "mod": "修改", "del": "删除"}


def _decode(data: bytes | str | None) -> str | None:
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _text_lines(old: str, new: str, limit: int) -> list[dict]:
    lines = []
    for line in difflib.unified_diff(old.split("\n"), new.split("\n"), lineterm="", n=1):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            lines.append({"kind": "note", "text": "…"})
        elif line.startswith("-"):
            lines.append({"kind": "del", "text": line[1:]})
        elif line.startswith("+"):
            lines.append({"kind": "add", "text": line[1:]})
        else:
            lines.append({"kind": "ctx", "text": line[1:] if line.startswith(" ") else line})
        if len(lines) >= limit:
            lines.append({"kind": "note", "text": "（后面还有改动，没有全部列出）"})
            break
    if lines and lines[0] == {"kind": "note", "text": "…"}:
        lines = lines[1:]
    return lines


def _row_text(row: dict) -> str:
    return " | ".join(f"{key}：{value}" for key, value in row.items() if value not in ("", None))


def changes(model: ModuleType, kind: str, old: bytes | str | None, new: bytes | str | None) -> dict:
    """一个条目从 old 到 new 改了什么。

    返回 {change: add|mod|del|none, sections: [{title, change, lines: DiffLine[]}],
    fields: {added, removed, changed}（表格行键）, relations: {added, removed}, summary}。
    """
    old_text, new_text = _decode(old), _decode(new)
    if old_text is None and new_text is None:
        return {"change": "none", "sections": [], "fields": {}, "relations": {}, "summary": ""}
    change = "add" if old_text is None else "del" if new_text is None else "mod"
    before = model.parse_document(kind, old_text or "")
    after = model.parse_document(kind, new_text or "")
    sections = []
    titles = list(dict.fromkeys([*before["order"], *after["order"]]))
    fields = {"added": [], "removed": [], "changed": []}
    for title in titles:
        a, b = before["sections"].get(title), after["sections"].get(title)
        if a == b:
            continue
        section_change = "add" if a is None else "del" if b is None else "mod"
        lines = [{"kind": "header", "text": title or "（正文开头）"}]
        rows_a, rows_b = before["rows"].get(title), after["rows"].get(title)
        if rows_a is not None or rows_b is not None:
            rows_a, rows_b = rows_a or {}, rows_b or {}
            for key in rows_a:
                if key not in rows_b:
                    fields["removed"].append(key)
                    lines.append({"kind": "del", "text": _row_text(rows_a[key])})
                elif rows_a[key] != rows_b[key]:
                    fields["changed"].append(key)
                    lines.append({"kind": "del", "text": _row_text(rows_a[key])})
                    lines.append({"kind": "add", "text": _row_text(rows_b[key])})
            for key in rows_b:
                if key not in rows_a:
                    fields["added"].append(key)
                    lines.append({"kind": "add", "text": _row_text(rows_b[key])})
            if len(lines) == 1:  # 行没变、表格外的说明文字变了
                lines += _text_lines(a or "", b or "", MAX_LINES_PER_SECTION)
            lines = lines[:MAX_LINES_PER_SECTION + 1]
        else:
            lines += _text_lines(a or "", b or "", MAX_LINES_PER_SECTION)
        sections.append({"title": title, "change": section_change, "lines": lines})
    rel_before, rel_after = set(before["relations"]), set(after["relations"])
    relations = {"added": sorted(rel_after - rel_before), "removed": sorted(rel_before - rel_after)}
    if change == "mod" and not sections:
        change = "none"
    return {"change": change, "sections": sections, "fields": fields, "relations": relations,
            "summary": summary(change, sections, fields, relations)}


def summary(change: str, sections: list[dict], fields: dict, relations: dict) -> str:
    """一句话：「改了「对象属性清单」：新增 2 个字段、改 1 个」。"""
    if change in ("add", "del"):
        return _VERB[change]
    parts = []
    counted = []
    if fields.get("added"):
        counted.append(f"新增 {len(fields['added'])} 行")
    if fields.get("changed"):
        counted.append(f"改 {len(fields['changed'])} 行")
    if fields.get("removed"):
        counted.append(f"删 {len(fields['removed'])} 行")
    titles = [item["title"] for item in sections if item["title"]]
    if titles:
        shown = "、".join(f"「{title}」" for title in titles[:3]) + ("等" if len(titles) > 3 else "")
        parts.append(f"改了{shown}" + (f"：{'、'.join(counted)}" if counted else ""))
    if relations.get("added") or relations.get("removed"):
        parts.append(f"关系 +{len(relations.get('added') or [])} / -{len(relations.get('removed') or [])}")
    return "；".join(parts) or "改了正文"


def file_changes(old: bytes | str | None, new: bytes | str | None) -> dict:
    """不是条目的文件：按整份文本比。"""
    old_text, new_text = _decode(old), _decode(new)
    change = "add" if old_text is None else "del" if new_text is None else ("mod" if old_text != new_text else "none")
    lines = _text_lines(old_text or "", new_text or "", MAX_TEXT_LINES) if change != "none" else []
    return {"change": change, "sections": [{"title": "", "change": change, "lines": lines}] if lines else [],
            "fields": {}, "relations": {}, "summary": _VERB.get(change, "")}


def entity_touched(model: ModuleType, kind: str, old, new) -> bool:
    """这次改动有没有改到实体本身（章节、字段、关系）；只动了空白之类的不算。"""
    return changes(model, kind, old, new)["change"] != "none"


def conflicts(model: ModuleType, entries: dict[str, tuple[str, bytes | None, bytes | None, bytes | None]]) -> list[str]:
    """合入冲突：entries 是 {路径: (实体类型, 基准内容, 主线内容, 变更集内容)}，只含两边都改了的路径。

    两边都改到了这个实体（章节、字段、关系任一处）且改成的不一样，就是冲突。返回冲突的路径。
    """
    found = []
    for rel, (kind, base, main, changeset) in entries.items():
        if main == changeset:
            continue
        if entity_touched(model, kind, base, main) and entity_touched(model, kind, base, changeset):
            found.append(rel)
    return found
