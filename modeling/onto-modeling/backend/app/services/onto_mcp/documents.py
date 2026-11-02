"""Lossless section patches for Onto's native ontology Markdown files."""
import copy
import hashlib
import json
import re
from ...kernel import metamodel
from ...kernel.metamodel import mm
from ..document_metadata import report_name, name_frontmatter
from ..package_mapping import parse_relation_definitions
from .errors import McpError

# 实体类型与目录（含只读别名）、链接语法都取元模型：模块级的 KINDS 是镜像版本，读写时按本项目锁定的版本
# （MCP 执行入口 `metamodel.using(root)`，B3 合并遗留）
KINDS = {entity.kind: entity.directories for entity in mm.ENTITY_TYPES}


def kinds() -> dict:
    return {entity.kind: entity.directories for entity in metamodel.current().ENTITY_TYPES}
HEADING = re.compile(r"^(#{1,2})[ \t]+([^\n]+)$", re.M)
ALIASES = {"属性": "对象属性清单", "字段": "对象属性清单", "描述": "对象描述", "与logic的关联": "与operation的关联"}


def key(value, kind=None):
    if not isinstance(value, str):
        raise McpError("INVALID_INPUT", "资源标识必须是字符串")
    if kind:
        if not value.startswith(kind + ":"):
            raise McpError("INVALID_INPUT", f"资源 ID 必须以 {kind}: 开头")
        value = value[len(kind) + 1:]
    if metamodel.current().key_problem(value):  # key 规则取元模型（方案 C5）
        raise McpError("INVALID_INPUT", "资源 key 格式无效")
    return value


def snapshot(root):
    records = {}
    stems = {}
    for kind, dirs in kinds().items():
        for directory in dirs:
            base = root / directory
            if base.is_symlink():
                raise McpError("UNSAFE_PATH", "本体目录不能是符号链接", 409)
            for path in sorted(base.glob("*.py" if kind == "script" else "*.md")):
                if path.name == "index.md":
                    continue
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise McpError("UNSAFE_PATH", "本体文件路径不安全", 409)
                if path.stem.casefold() in stems:
                    raise McpError("RESOURCE_NAME_CONFLICT", "本体文档存在同名标识，请先消除歧义", 409)
                stems[path.stem.casefold()] = path
                records[f"{kind}:{path.stem}"] = {"kind": kind, "key": path.stem,
                    "path": path.relative_to(root).as_posix(), "text": path.read_text(encoding="utf-8")}
    return records


def revision(records):
    digest = hashlib.sha256()
    for item in sorted(records.values(), key=lambda item: item["path"]):
        for value in (item["path"], item["text"]):
            data = value.encode()
            digest.update(str(len(data)).encode() + b":" + data)
    return digest.hexdigest()


def spans(text):
    matches = list(HEADING.finditer(text))
    ones = sum(len(m[1]) == 1 for m in matches)
    level = 2 if ones == 1 and any(len(m[1]) == 2 for m in matches) else 1
    selected = [m for m in matches if len(m[1]) == level]
    return [(ALIASES.get(m[2].strip(), m[2].strip()), m,
             selected[i + 1].start() if i + 1 < len(selected) else len(text))
            for i, m in enumerate(selected)]


def sections(text):
    return {title: text[m.end():end].strip() for title, m, end in spans(text)}


def patch_section(text, name, content):
    # Patch only the requested section; preserve unrelated prose/frontmatter verbatim.
    found = [(m, end) for title, m, end in spans(text) if title == name]
    if len(found) > 1:
        raise McpError("AMBIGUOUS_SECTION", f"{name} 章节重复，无法安全修改", 422)
    if found:
        m, end = found[0]
        return text[:m.end()] + "\n\n" + content.rstrip() + "\n\n" + text[end:]
    existing = spans(text)
    hashes = existing[0][1][1] if existing else "#"
    return text.rstrip() + f"\n\n{hashes} {name}\n\n{content.rstrip()}\n"


def patch_title(text, title):
    if not isinstance(title, str):
        raise McpError("INVALID_INPUT", "title 必须是非空单行字符串")
    if not title.strip() or title != title.strip() or "\n" in title:
        raise McpError("INVALID_INPUT", "title 必须是非空单行字符串")
    original = report_name(text)
    if text.startswith("---\n"):
        header, separator, body = text[4:].partition("\n---\n")
        if not separator:
            raise McpError("INVALID_METADATA", "文档头不完整", 422)
        lines = [line for line in header.splitlines() if not line.startswith("report_name:")]
        lines.append("report_name: " + json.dumps(title, ensure_ascii=False))
        text = "---\n" + "\n".join(lines) + "\n---\n" + body
    else:
        text = name_frontmatter(title) + text
    # A standalone document title can also be updated; never replace a section heading.
    matches = list(HEADING.finditer(text))
    if sum(len(m[1]) == 1 for m in matches) == 1 and any(len(m[1]) == 2 for m in matches):
        m = next(m for m in matches if len(m[1]) == 1)
        text = text[:m.start()] + "# " + title + text[m.end():]
    return text


def title(record):
    explicit = report_name(record["text"])
    if explicit:
        return explicit
    matches = list(HEADING.finditer(record["text"]))
    if sum(len(m[1]) == 1 for m in matches) == 1 and any(len(m[1]) == 2 for m in matches):
        return next(m[2].strip() for m in matches if len(m[1]) == 1)
    return record["key"]


def links(text, names=None):
    """正文链接到的条目名（按首次出现去重）；给了 names 时按元模型解析（`#` 是锚点，全名优先）。"""
    return metamodel.current().link_names(text, names)


def cells(line):
    return [s.strip().replace(r"\|", "|") for s in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def table(text):
    rows = [cells(line) for line in text.splitlines() if line.strip().startswith("|")]
    if len(rows) < 2:
        return []
    return [dict(zip(rows[0], row)) for row in rows[2:] if len(row) == len(rows[0])]


def table_text(headers, rows):
    def cell(value):
        value = str(value)
        if "\n" in value or "\r" in value:
            raise McpError("INVALID_INPUT", "表格字段不能包含换行")
        return value.replace("|", r"\|")
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
                      + ["| " + " | ".join(cell(row.get(h, "")) for h in headers) + " |" for row in rows])


def array_section(value):
    if not value or value in ("无", "none"):
        return []
    return [re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
            for line in value.splitlines() if line.strip() and not line.startswith(">")]


def public(record, records):
    kind, stem = record["kind"], record["key"]
    s = sections(record["text"])
    names = {item["key"] for item in records.values()}

    model = metamodel.current()

    def links(text):
        return model.link_names(text, names)

    result = {"key": stem, "title": title(record),
              "description": s.get({"object": "对象描述", "operation": "业务描述", "rule": "规则描述"}.get(kind, ""), "")}
    if kind == "object":
        fields = []
        for row in table(s.get("对象属性清单", "")):
            fields.append({"key": row.get("英文名称") or row.get("属性名") or row.get("中文名称", ""),
                           "cnName": row.get("中文名称", ""), "type": row.get("类型", "字符串"),
                           "description": row.get("描述", ""), "primaryKey": row.get("主键") in ("是", "true"),
                           "required": row.get("必填") in ("是", "true"), "note": row.get("描述", ""),
                           "alias": row.get("属性别名", ""), "sourceColumn": row.get("来源字段") or row.get("英文名称", ""),
                            "constraints": row.get("属性规则", ""), "valueBindingRef": row.get("值绑定", "")})
        relations = []
        relation_table = table(s.get("关系", ""))
        for row in relation_table:
            target = row.get("目标对象") or row.get("目标") or ""
            label = row.get("关系") or row.get("关系名称") or ""
            if not target or not label:
                continue
            identity = "|".join((stem, label, target, row.get("源关联字段", ""), row.get("目标关联字段", "")))
            relations.append({"id": "edge-" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                "kind": "object_relation", "label": label, "target_id": "object:" + target,
                "target_kind": "object", "target_key": target, "target_exists": "object:" + target in records,
                **({"sourceJoinProperty": row["源关联字段"]} if row.get("源关联字段") else {}),
                **({"targetJoinProperty": row["目标关联字段"]} if row.get("目标关联字段") else {}),
                **({"description": row["说明"]} if row.get("说明") else {})})
        for line in s.get("关联对象", "").splitlines():
            if relation_table:
                break
            for target in links(line):
                parsed = parse_relation_definitions("# 关联对象\n" + line, [])
                relation = parsed[0] if len(parsed) == 1 else None
                label = relation["relation"] if relation else "关联对象"
                rid = "relation-" + hashlib.sha256(line.strip().encode()).hexdigest()[:16]
                relations.append({"id": rid, "kind": "object_relation", "label": label, "target_id": "object:" + target,
                    "target_kind": "object", "target_key": target, "target_exists": "object:" + target in records,
                    "direction": "incoming" if relation and relation["source"] == target else "outgoing",
                    **({"description": relation["note"]} if relation and relation["note"] else {})})
        result.update(id="object:" + stem, source_path=record["path"], relation_count=len(relations), fields=fields,
                      metadata={"title": result["title"], "description": result["description"],
                          "aliases": [v.strip() for v in re.split(r"[,，]", s.get("对象别名", "")) if v.strip() and v.strip() != "无"],
                          "queryBoundary": s.get("查询边界", "")}, relations=relations,
                      sources=[s["数据来源"]] if s.get("数据来源") not in (None, "无", "") else [])
    elif kind == "operation":
        result.update(inputs=[{"param": r.get("参数名称", ""), "cn_name": r.get("中文名称", ""), "type": r.get("类型", "string"),
                               "required": r.get("必填", "否") == "是", "note": r.get("描述", "")} for r in table(s.get("输入", ""))],
                      outputs=[{"field": r.get("参数名称", ""), "cn_name": r.get("中文名称", ""), "type": r.get("类型", "string"),
                                "note": r.get("描述", "")} for r in table(s.get("输出", ""))],
                      flow=array_section(s.get("逻辑描述", "")), related_objects=links(s.get("与object的关联", "")),
                      uses_rules=links(s.get("使用规则", "")), uses_metrics=links(s.get("使用指标", "")),
                      notes=array_section(s.get("备注", "")), script=None, evidence=array_section(s.get("来源", "")))
    elif kind == "rule":
        result.update(applies_to_objects=links(s.get("适用对象", "")), applies_to_operations=links(s.get("适用操作", "")),
                      content=s.get("约束表达", ""), conditions=table(s.get("条件", "")),
                      exceptions=array_section(s.get("例外情况", "")), evidence=array_section(s.get("来源", "")))
    return result
