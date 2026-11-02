"""Studio 结构化编辑的草稿（契约 ST-07 `form`、7.5 EntryForm）：表单 → 条目 Markdown。

以条目当前的原文为底，只改表单里和「当前版本读出来的表单」（ST-04 → 前端 formFromEntry 同一口径）不一样的部分；
没动的地方逐字节保留——表外的段落、属性表多出来的列（描述、主键、属性别名、属性规则）、表单里没有的章节都不碰。
解析口径与 ST-04 一致：章节按 `studio.parse_sections` 的层级判定，描述取描述章节第一行正文（`_plain_summary`），
属性取「对象属性清单」里英文名称不是占位的行，数据来源按「# 数据来源」首行（改写复用发布修复入口的
`package_mapping.set_declared_source`）。key 就是文件名、也是别处 [[链接]] 的目标，表单里改 key 不支持。
"""

from __future__ import annotations

import re

from . import package_mapping
from .document_metadata import name_frontmatter
from .studio import SECTION, SECTION_ALIASES, _plain_summary, _table_cells

# 条目名：路径分隔、通配与链接语法里的字符都不行（同 ST-09）
_NAME_BAD = re.compile(r"[\\/:*?\"<>|#\[\]\x00-\x1f]")
_NAME_MAX = 120

DESCRIPTION_SECTIONS = {"object": "对象描述", "operation": "业务描述", "rule": "规则描述", "metric": "指标描述"}
FIELDS_SECTION = "对象属性清单"
FIELD_COLUMNS = ("中文名称", "英文名称", "描述", "主键", "类型", "属性别名", "属性规则")

# 表单里加一条关联写到哪一节、写成什么样（按「本条目类型 → 目标类型」）；写法要让 ST-04 的关系投影认得出来
_REL_RULES = {
    ("object", "object"): ("关联对象", "- 当前对象 → 关联 → [[{t}]]"),
    ("operation", "object"): ("与object的关联", "- 支撑数据的对象：[[{t}]]"),
    ("operation", "operation"): ("与operation的关联", "- [[{t}]]"),
    ("operation", "rule"): ("逻辑描述", "- [[{t}]]"),
    ("operation", "metric"): ("逻辑描述", "- [[{t}]]"),
    ("rule", "object"): ("适用对象", "- [[{t}]]"),
    ("rule", "operation"): ("适用对象", "- [[{t}]]"),
    ("rule", "metric"): ("适用对象", "- [[{t}]]"),
    ("metric", "object"): ("数据来源", "- [[{t}]]"),
    ("metric", "operation"): ("数据来源", "- [[{t}]]"),
    ("metric", "rule"): ("数据来源", "- [[{t}]]"),
}
_LINK = re.compile(r"\[\[([^\[\]\n]+?)\]\]")
_EMPTY = {"", "无", "-", "—"}


class FormError(Exception):
    """表单写不进文档：field 是出错的字段，code 是原因（rename_unsupported、required、pattern、unsupported）。"""

    def __init__(self, field: str, code: str, message: str):
        super().__init__(message)
        self.field = field
        self.code = code


# ---- 当前版本读出来的表单（与前端 studioModel.formFromEntry 同一口径） ----


def form_from_entry(entry: dict) -> dict:
    back = {(item.get("source") or {}).get("id") for item in entry.get("backlinks") or []}
    relations: list[str] = []
    for item in (entry.get("relations") or {}).get("declared") or []:
        target = (item.get("target") or {}).get("id")
        if target and target not in relations and target not in back:
            relations.append(target)
    source = entry.get("data_source") or {}
    data_source = ({"kind": source["kind"], "value": source.get("value")}
                   if source.get("kind") and source["kind"] != "unset" else {"kind": "none"})
    return {
        "name": entry.get("name") or "",
        "key": entry.get("key") or "",
        "description": entry.get("description") or "",
        "fields": [{"name": f.get("name") or "", "column": f.get("column") or "", "type": f.get("type") or "",
                    "unit": f.get("unit")} for f in entry.get("fields") or []],
        "relations": relations,
        "data_source": data_source,
        "sections": {s["title"]: s.get("content_md") or "" for s in entry.get("sections") or [] if s.get("level", 0) >= 2},
    }


def normalize(form: dict, base: dict) -> dict:
    """前端送来的表单：缺的键按当前版本补，类型不对的报 400。"""
    if not isinstance(form, dict):
        raise FormError("form", "invalid", "form 要是对象")
    out = dict(base)
    for key in ("name", "key", "description"):
        if key in form:
            if not isinstance(form[key], str):
                raise FormError(key, "invalid", f"{key} 要是字符串")
            out[key] = form[key]
    if "fields" in form:
        if not isinstance(form["fields"], list) or not all(isinstance(f, dict) for f in form["fields"]):
            raise FormError("fields", "invalid", "fields 要是数组")
        out["fields"] = [{"name": str(f.get("name") or ""), "column": str(f.get("column") or ""),
                          "type": str(f.get("type") or ""), "unit": f.get("unit") or None,
                          "evidence_anchor": f.get("evidence_anchor") or None,
                          "expert_confirmed": bool(f.get("expert_confirmed"))} for f in form["fields"]]
    if "relations" in form:
        if not isinstance(form["relations"], list) or not all(isinstance(r, str) for r in form["relations"]):
            raise FormError("relations", "invalid", "relations 要是条目编号数组")
        out["relations"] = list(dict.fromkeys(form["relations"]))
    if "data_source" in form:
        source = form["data_source"]
        if not isinstance(source, dict) or source.get("kind") not in ("table", "anchor", "none"):
            raise FormError("data_source", "invalid", "data_source.kind 取值不合法")
        out["data_source"] = {"kind": source["kind"], "value": source.get("value") or None}
    if "sections" in form:
        if not isinstance(form["sections"], dict) or not all(isinstance(v, str) for v in form["sections"].values()):
            raise FormError("sections", "invalid", "sections 要是「章节名 → 正文」")
        out["sections"] = dict(form["sections"])
    return out


# ---- 章节定位（层级判定同 studio.parse_sections） ----


def _spans(text: str) -> tuple[int, list[dict]]:
    matches = list(SECTION.finditer(text))
    level1 = sum(1 for m in matches if len(m.group(1)) == 1)
    level2 = sum(1 for m in matches if len(m.group(1)) == 2)
    level = 1 if (level1 > 1 or (level1 >= 1 and level2 == 0)) else 2
    tops = [m for m in matches if len(m.group(1)) <= level]
    spans = []
    for index, match in enumerate(tops):
        if len(match.group(1)) != level:
            continue
        end = tops[index + 1].start() if index + 1 < len(tops) else len(text)
        title = match.group(2).strip()
        spans.append({"title": SECTION_ALIASES.get(title, title), "head": match.start(), "start": match.end(), "end": end})
    return level, spans


def _find(text: str, title: str) -> dict | None:
    return next((span for span in _spans(text)[1] if span["title"] == title), None)


def _replace_content(text: str, span: dict, content: str) -> str:
    """换掉一节的正文，保留标题行与这一节前后原来的空行写法。"""
    segment = text[span["start"]:span["end"]]
    if segment.strip():
        lead = segment[:len(segment) - len(segment.lstrip())]
        trail = segment[len(segment.rstrip()):]
    else:
        lead, trail = "\n\n", "\n\n" if span["end"] < len(text) else "\n"
    if not lead.startswith("\n"):
        lead = "\n" + lead
    body = content.strip()
    return text[:span["start"]] + (lead + body + trail if body else "\n\n" if span["end"] < len(text) else "\n") + text[span["end"]:]


def _set_section(text: str, title: str, content: str) -> str:
    span = _find(text, title)
    if span is not None:
        return _replace_content(text, span, content)
    level, _ = _spans(text)
    return text.rstrip("\n") + f"\n\n{'#' * level} {title}\n\n{content.strip()}\n"


def _placeholder_only(content: str) -> bool:
    """模板占位（`{待填写…}`，可以跨行）或「无」之外没有别的内容。"""
    rest = re.sub(r"\{[^{}]*\}", "", content, flags=re.S).strip()
    return rest in _EMPTY


# ---- 各部分 ----


def _apply_name(text: str, key: str, old: str, new: str) -> str:
    """名称写进文档头 report_name（和 key 一样时去掉，同 ST-09）；「# 文档名 + ## 章节」写法里的文档名一行跟着改。"""
    name = " ".join(new.split())
    if not name:
        raise FormError("name", "required", "名称不能为空")
    if len(name) > _NAME_MAX or _NAME_BAD.search(name):
        raise FormError("name", "pattern", "名称不合法")
    header_lines: list[str] = []
    body = text
    if text.startswith("---\n"):
        header, separator, rest = text[4:].partition("\n---\n")
        if separator:
            header_lines = [line for line in header.splitlines() if not line.startswith("report_name:")]
            body = rest
    if name != key:
        header_lines.insert(0, name_frontmatter(name).splitlines()[1])
    if _spans(body)[0] == 2:
        body = re.sub(rf"^#[ \t]+{re.escape(old)}[ \t]*$", f"# {name}", body, count=1, flags=re.M)
    return ("---\n" + "\n".join(header_lines) + "\n---\n" if header_lines else "") + body


def _apply_description(text: str, kind: str, new: str) -> str:
    """描述是描述章节的第一行正文（ST-04 取 `_plain_summary`）：换掉这一行，章节里其余内容不动。"""
    title = DESCRIPTION_SECTIONS.get(kind)
    if not title:
        return text
    new = new.strip()
    span = _find(text, title)
    if span is None:
        if not new:
            return text
        level, spans = _spans(text)
        if not spans:
            return text.rstrip("\n") + f"\n\n{'#' * level} {title}\n\n{new}\n"
        at = spans[0]["head"]
        return text[:at] + f"{'#' * level} {title}\n\n{new}\n\n" + text[at:]
    content = text[span["start"]:span["end"]]
    if _placeholder_only(content):
        return _replace_content(text, span, new)
    lines = content.split("\n")
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith(("|", "```", "{待填写", ">")) or not _plain_summary(raw):
            continue
        if new:
            lines[index] = re.match(r"^\s*(?:[-*+]\s+)?", raw).group(0) + new
        else:
            del lines[index]
        return text[:span["start"]] + "\n".join(lines) + text[span["end"]:]
    return _replace_content(text, span, f"{new}\n\n{content.strip()}" if new else content)


def _cell(value: str) -> str:
    return " ".join(str(value or "").replace("|", "/").split())


def _apply_fields(text: str, new: list[dict]) -> str:
    span = _find(text, FIELDS_SECTION)
    content = text[span["start"]:span["end"]] if span else ""
    lines = content.split("\n")
    table = [i for i, line in enumerate(lines) if line.strip().startswith("|")]
    if len(table) >= 2:
        first, last = table[0], table[0]
        while last + 1 < len(lines) and lines[last + 1].strip().startswith("|"):
            last += 1
        header = _table_cells(lines[first])
        rule_line = lines[first + 1]
        data = lines[first + 2:last + 1]
    else:
        first = last = None
        header = list(FIELD_COLUMNS)
        rule_line = "| " + " | ".join("---" for _ in header) + " |"
        data = []
    # 当前版本的属性对应哪几行（跳过占位行，同 studio._object_fields）
    rows = []
    for line in data:
        cells = _table_cells(line)
        record = {column: cells[i] if i < len(cells) else "" for i, column in enumerate(header)}
        name = record.get("中文名称", "").strip()
        key = record.get("英文名称", "").strip() or name
        if not key or key.startswith("{"):
            continue
        rows.append({"line": line, "record": record, "name": name or key, "column": key, "used": False})

    def match(field: dict) -> dict | None:
        for attr in ("column", "name"):
            for row in rows:
                if not row["used"] and field[attr] and row[attr] == field[attr]:
                    row["used"] = True
                    return row
        return None

    out = []
    for field in new:
        if not (field["name"].strip() or field["column"].strip()):
            continue
        row = match(field)
        same = row is not None and (row["name"], row["column"], row["record"].get("类型", "").strip()) == (
            field["name"], field["column"], field["type"])
        if same and not field.get("unit"):
            out.append(row["line"])
            continue
        record = dict(row["record"]) if row else {"描述": "", "主键": "否", "属性别名": "无", "属性规则": "无"}
        record["中文名称"] = field["name"]
        record["英文名称"] = field["column"]
        record["类型"] = field["type"]
        if field.get("unit") and "单位" not in (record.get("属性规则") or ""):
            rule = (record.get("属性规则") or "").strip()
            record["属性规则"] = f"单位：{field['unit']}" if rule in _EMPTY else f"{rule}；单位：{field['unit']}"
        out.append("| " + " | ".join(_cell(record.get(column, "")) for column in header) + " |")
    block = [lines[first] if first is not None else "| " + " | ".join(header) + " |", rule_line, *out]
    if first is not None:
        lines[first:last + 1] = block
        new_content = "\n".join(lines)
        return text[:span["start"]] + new_content + text[span["end"]:]
    if span is not None:
        rest = content.strip()
        rest = "" if _placeholder_only(rest) else rest
        return _replace_content(text, span, "\n".join(block) + (f"\n\n{rest}" if rest else ""))
    return _set_section(text, FIELDS_SECTION, "\n".join(block))


def _link_targets(line: str) -> list[tuple[str, tuple[int, int]]]:
    out = []
    for match in _LINK.finditer(line):
        body = match.group(1).split("|")[0].split("#")[0].strip()
        name = re.sub(r"\.md$", "", body.split("/")[-1], flags=re.I).strip()
        out.append((name, match.span()))
    return out


def _remove_relation(text: str, kind: str, target: str) -> str:
    titles = {title for (source, _), (title, _) in _REL_RULES.items() if source == kind}
    for title in sorted(titles):
        span = _find(text, title)
        if span is None:
            continue
        content = text[span["start"]:span["end"]]
        lines = content.split("\n")
        changed = False
        for index, raw in enumerate(list(lines)):
            hits = [s for name, s in _link_targets(raw) if name == target]
            if not hits:
                continue
            others = [name for name, _ in _link_targets(raw) if name != target and name not in _EMPTY]
            if not others:
                lines[index] = None
            else:
                start, end = hits[0]
                before, after = raw[:start], raw[end:]
                if after[:1] in "、，,；;":
                    after = after[1:]
                elif before[-1:] in "、，,；;":
                    before = before[:-1]
                lines[index] = before + after
            changed = True
        if changed:
            kept = [line for line in lines if line is not None]
            if not "\n".join(kept).strip():
                text = _replace_content(text, span, "无")
            else:
                text = text[:span["start"]] + "\n".join(kept) + text[span["end"]:]
    return text


def _add_relation(text: str, kind: str, target_id: str) -> str:
    target_kind, _, target = target_id.partition(":")
    rule = _REL_RULES.get((kind, target_kind))
    if rule is None or not target:
        raise FormError("relations", "unsupported", "不能加这条关联")
    title, template = rule
    line = template.format(t=target)
    span = _find(text, title)
    if span is None:
        return _set_section(text, title, line)
    content = text[span["start"]:span["end"]]
    if _placeholder_only(content):
        return _replace_content(text, span, line)
    return _replace_content(text, span, content.rstrip() + "\n" + line)


# ---- 渲染 ----


def _field_key(fields: list[dict]) -> list[tuple]:
    return [(f["name"], f["column"], f["type"], f.get("unit") or None) for f in fields]


def render(text: str, kind: str, key: str, base: dict, form: dict) -> str:
    """按表单改条目原文；base 是当前版本读出来的表单（form_from_entry）。表单和 base 一样时原样返回。"""
    if form["key"].strip() != base["key"]:
        raise FormError("key", "rename_unsupported", "Key 不能改")
    out = text
    if form["name"] != base["name"]:
        out = _apply_name(out, key, base["name"], form["name"])
    for title, content in form["sections"].items():
        if content.strip() != (base["sections"].get(title) or "").strip():
            out = _set_section(out, title, content)
    if form["description"].strip() != base["description"].strip():
        out = _apply_description(out, kind, form["description"])
    if kind == "object":
        if _field_key(form["fields"]) != _field_key(base["fields"]):
            out = _apply_fields(out, form["fields"])
        old_source = (base["data_source"]["kind"], base["data_source"].get("value") or None)
        new_source = (form["data_source"]["kind"], form["data_source"].get("value") or None)
        if new_source != old_source:
            value = "" if new_source[0] == "none" else str(new_source[1] or "").strip()
            if new_source[0] != "none" and not value:
                raise FormError("data_source", "required", "数据来源要写表名")
            # 「# 数据来源」是一级标题（发布按它绑表）：「# 文档名 + ## 章节」写法的文档里加一级标题会打乱章节层级
            if _spans(out)[0] == 2 and not re.search(r"^#[ \t]*数据来源[ \t]*$", out, re.M):
                raise FormError("data_source", "unsupported", "数据来源请在原文里改")
            out = package_mapping.set_declared_source(out, value)
    removed = [item for item in base["relations"] if item not in form["relations"]]
    added = [item for item in form["relations"] if item not in base["relations"]]
    for item in removed:
        out = _remove_relation(out, kind, item.partition(":")[2])
    for item in added:
        out = _add_relation(out, kind, item)
    return out
