"""Validate and patch only requested Onto Markdown sections."""
import copy
import hashlib
import json
import re
from . import documents as doc
from .definitions import OPERATION, RULE, validate_input
from .errors import McpError


def strings(values):
    return "\n".join("- " + value for value in values) or "无"


def refs(values):
    return "\n".join("- [[" + doc.key(value) + "]]" for value in values) or "无"


def patch_resource(record, payload):
    kind = record["kind"]
    props = OPERATION if kind == "operation" else RULE
    validate_input(payload, {"type": "object", "properties": props, "additionalProperties": False})
    text = record["text"]
    if "title" in payload:
        text = doc.patch_title(text, payload["title"])
    mapping = ({"description": "业务描述", "flow": "逻辑描述", "related_objects": "与object的关联",
                "uses_rules": "使用规则", "uses_metrics": "使用指标", "notes": "备注"} if kind == "operation" else
               {"description": "规则描述", "content": "约束表达", "applies_to_objects": "适用对象",
                "applies_to_operations": "适用操作", "exceptions": "例外情况"})
    for field, section in mapping.items():
        if field not in payload:
            continue
        value = payload[field]
        if field in ("related_objects", "uses_rules", "uses_metrics", "applies_to_objects", "applies_to_operations"):
            value = refs(value)
            if field == "related_objects":
                # Preserve input/output roles for retained objects; added objects are support inputs.
                old = doc.sections(text).get(section, "")
                allowed = set(payload[field])
                lines = []
                for line in old.splitlines():
                    targets = doc.links(line)
                    if not targets:
                        lines.append(line)
                    else:
                        kept = [target for target in targets if target in allowed]
                        if kept:
                            prefix = line.split("[[", 1)[0]
                            lines.append(prefix + "、".join("[[" + target + "]]" for target in kept))
                added = allowed - set(doc.links(old))
                if added:
                    lines.append("- 支撑数据的对象：" + "、".join("[[" + target + "]]" for target in sorted(added)))
                value = "\n".join(lines).strip() or "无"
        elif isinstance(value, list):
            value = strings(value)
        text = doc.patch_section(text, section, value)
    for field, section, identifier in (("inputs", "输入", "param"), ("outputs", "输出", "field")):
        if field not in payload:
            continue
        headers = ["参数名称", "中文名称", "类型", "必填", "描述"]
        if field == "inputs":
            headers.append("输入源")
        rows = []
        names = set()
        old = {row.get("参数名称"): row for row in doc.table(doc.sections(text).get(section, ""))}
        for value in payload[field]:
            if set(value) - {identifier, "cn_name", "cnName", "type", "required", "note"}:
                raise McpError("INVALID_INPUT", f"{field} 包含未知字段")
            name = doc.key(value.get(identifier, ""))
            if name in names:
                raise McpError("INVALID_INPUT", "输入或输出参数重复")
            names.add(name)
            if "required" in value and not isinstance(value["required"], bool):
                raise McpError("INVALID_INPUT", "required 必须是布尔值")
            rows.append({"参数名称": name, "中文名称": value.get("cn_name", value.get("cnName", "")), "类型": value.get("type", "string"),
                         "必填": "是" if value.get("required") else "否", "描述": value.get("note", ""),
                         "输入源": old.get(name, {}).get("输入源", "")})
        text = doc.patch_section(text, section, doc.table_text(headers, rows) if rows else "无")
    if "conditions" in payload:
        rows = payload["conditions"]
        headers = list(dict.fromkeys(k for row in rows for k in row))
        text = doc.patch_section(text, "条件", doc.table_text(headers, rows) if rows else "无")
    return text


FIELD_COLUMNS = {"key": "英文名称", "cnName": "中文名称", "type": "类型", "description": "描述",
                 "primaryKey": "主键", "alias": "属性别名", "constraints": "属性规则",
                 "required": "必填", "note": "描述", "sourceColumn": "来源字段", "valueBindingRef": "值绑定"}


def apply_ops(values, ops, identity, update_identity=None):
    validate_input(ops, {"type": "object", "properties": {name: {"type": "array"} for name in ("add", "update", "remove")},
                         "additionalProperties": False})
    result = copy.deepcopy(values)
    seen = set()
    for action in ("remove", "update", "add"):
        for value in ops.get(action, []):
            if action == "remove":
                target = value.get("relationId") if isinstance(value, dict) and identity == "id" else value
            elif not isinstance(value, dict):
                raise McpError("INVALID_INPUT", "新增/修改项必须是对象")
            else:
                target = value.get(update_identity or identity) if action == "update" else value.get(identity)
            if action != "add":
                if not isinstance(target, str) or not target or target in seen:
                    raise McpError("INVALID_INPUT", "重复或冲突的修改项")
                seen.add(target)
                existing = next((item for item in result if item.get(identity) == target), None)
                if existing is None:
                    raise McpError("RESOURCE_NOT_FOUND", "要修改或删除的字段/关系不存在", 422)
                if action == "remove":
                    result.remove(existing)
                else:
                    if set(value) - {update_identity or identity, "changes"} or not isinstance(value.get("changes"), dict):
                        raise McpError("INVALID_INPUT", "更新项应包含标识和 changes 对象")
                    changes = dict(value["changes"])
                    if identity == "key":
                        if "description" in changes:
                            changes["note"] = changes["description"]
                        if "note" in changes:
                            changes["description"] = changes["note"]
                    existing.update(changes)
            else:
                result.append(copy.deepcopy(value))
    return result


def patch_object(record, args, records):
    current = doc.public(record, records)
    text = record["text"]
    if "metadata" in args:
        metadata = args["metadata"]
        validate_input(metadata, {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}}, "queryBoundary": {"type": "string"}},
            "additionalProperties": False})
        if "title" in metadata:
            text = doc.patch_title(text, metadata["title"])
        for field, section in (("description", "对象描述"), ("aliases", "对象别名"), ("queryBoundary", "查询边界")):
            if field in metadata:
                value = "，".join(metadata[field]) if field == "aliases" else metadata[field]
                text = doc.patch_section(text, section, value)
    if "field_ops" in args:
        fields = apply_ops(current["fields"], args["field_ops"], "key")
        old_rows = doc.table(doc.sections(text).get("对象属性清单", ""))
        headers = list(old_rows[0]) if old_rows else list(FIELD_COLUMNS.values())
        headers = list(dict.fromkeys([*headers, *FIELD_COLUMNS.values()]))
        seen, rows = set(), []
        for field in fields:
            if set(field) - set(FIELD_COLUMNS):
                raise McpError("INVALID_INPUT", "字段包含不支持的属性")
            key = doc.key(field.get("key", ""))
            if key.casefold() in seen:
                raise McpError("FIELD_DUPLICATE", "字段 key 重复", 422)
            seen.add(key.casefold())
            row = dict(next((row for row in old_rows if row.get("英文名称") == key), {}))
            for name, column in FIELD_COLUMNS.items():
                value = field.get(name, "")
                if name == "alias":
                    if not isinstance(value, str):
                        raise McpError("INVALID_INPUT", "alias 必须是字符串")
                elif name in ("primaryKey", "required"):
                    if not isinstance(value, bool) and value != "":
                        raise McpError("INVALID_INPUT", "primaryKey 必须是布尔值")
                    value = "是" if value else "否"
                elif not isinstance(value, str):
                    raise McpError("INVALID_INPUT", f"{name} 必须是字符串")
                row[column] = value
            rows.append(row)
        # Preserve non-table prose in this section.
        original = doc.sections(text).get("对象属性清单", "")
        lines = original.splitlines()
        indexes = [i for i, line in enumerate(lines) if line.strip().startswith("|")]
        rendered = doc.table_text(headers, rows)
        if indexes:
            rendered = "\n".join(lines[:indexes[0]] + [rendered] + lines[indexes[-1]+1:])
        text = doc.patch_section(text, "对象属性清单", rendered)
    if "relation_ops" in args:
        originals = current["relations"]
        values = [{"id": value["id"], "kind": value["kind"], "targetId": value["target_id"],
                   "label": value["label"], "description": value.get("description", ""),
                   "direction": value.get("direction", "outgoing"),
                   **({"sourceJoinProperty": value["sourceJoinProperty"]} if value.get("sourceJoinProperty") else {}),
                   **({"targetJoinProperty": value["targetJoinProperty"]} if value.get("targetJoinProperty") else {})} for value in originals]
        relations = apply_ops(values, args["relation_ops"], "id", "relationId")
        original_section = doc.sections(text).get("关联对象", "")
        relation_table = doc.table(doc.sections(text).get("关系", ""))
        original_lines = {"relation-" + hashlib.sha256(line.strip().encode()).hexdigest()[:16]: line
                          for line in original_section.splitlines() if doc.links(line)}
        lines = [line for line in original_section.splitlines() if not doc.links(line)]
        seen = set()
        for relation in relations:
            if set(relation) - {"id", "kind", "targetId", "label", "description", "sourceJoinProperty", "targetJoinProperty", "direction"}:
                raise McpError("INVALID_INPUT", "关系包含不支持的属性")
            target = doc.key(relation.get("targetId", ""), "object")
            label = relation.get("label", "关联对象")
            if not isinstance(label, str) or not label.strip() or "\n" in label:
                raise McpError("INVALID_INPUT", "关系 label 无效")
            if relation.get("kind", "object_relation") not in ("object_relation", "association"):
                raise McpError("INVALID_INPUT", "Onto 对象关系仅支持 object_relation")
            if target == record["key"] or "object:" + target not in records or (target, label) in seen:
                raise McpError("RELATION_TARGET_INVALID", "关系目标不存在、自引用或关系重复", 422)
            seen.add((target, label))
            old = next((r for r in values if r.get("id") == relation.get("id")), None)
            if old == relation and relation["id"] in original_lines:
                lines.append(original_lines[relation["id"]])
            else:
                description = relation.get("description", "")
                if not isinstance(description, str) or "\n" in description:
                    raise McpError("INVALID_INPUT", "关系说明必须是单行字符串")
                annotated = label + ("（" + description + "）" if description else "")
                if relation.get("direction", "outgoing") not in ("outgoing", "incoming"):
                    raise McpError("INVALID_INPUT", "关系方向无效")
                line = ("- [[" + target + "]] → " + annotated + " → 当前对象" if relation.get("direction") == "incoming"
                        else "- 当前对象 → " + annotated + " → [[" + target + "]]")
                lines.append(line)
        relation_section = "关系" if "关系" in doc.sections(text) else "关联对象"
        if relation_section == "关系":
            rows = []
            for relation in relations:
                original = next((value for value in values if value.get("id") == relation.get("id")), None)
                retained = {}
                if original:
                    target = doc.key(original["targetId"], "object")
                    for row in relation_table:
                        row_target = row.get("目标对象") or row.get("目标")
                        row_label = row.get("关系") or row.get("关系名称")
                        if row_target == target and row_label == original["label"]:
                            retained = row
                            break
                rows.append({**retained, "关系": relation["label"], "目标对象": doc.key(relation["targetId"], "object"),
                             "说明": relation.get("description", ""),
                             "源关联字段": relation.get("sourceJoinProperty", ""),
                             "目标关联字段": relation.get("targetJoinProperty", "")})
            headers = list(dict.fromkeys([*(relation_table[0] if relation_table else []),
                                         "关系", "目标对象", "说明", "证据", "源关联字段", "目标关联字段"]))
            text = doc.patch_section(text, relation_section, doc.table_text(headers, rows))
        else:
            text = doc.patch_section(text, relation_section, "\n".join(lines))
    return text
