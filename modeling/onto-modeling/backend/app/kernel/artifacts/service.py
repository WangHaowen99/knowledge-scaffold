"""插件产物的通用读写（契约 3.17 AR-01～AR-09；MCP artifact_list / artifact_get；详设 5.9.4、5.9.5）。

- 只认已启用包登记的类型（跟锁文件走）；包停用后类型不再出现，工作区里的产物文件原样保留。
- 保存走写入路径（ChangeTx，提交带 `Onto-Source: web`）：先按登记的字段定义校验（jsonschema），
  再过包声明的门禁规则；只读类型（求解契约、知识条目这类有专用接口的）返回 artifact_readonly。
- 插件产物不是本体实体：这里不解析维基链接、不建关系边；引用本体条目只认登记的 refs 字段。
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Callable

import jsonschema

from ...kernel import project_events
from . import index, parsers
from .registry import ArtifactType, get_type, types_for_project

SOURCE_LABELS = {"assistant": "助手", "agent": "助手", "web": "网页", "studio": "Studio", "mcp": "MCP",
                 "plugin": "插件", "import": "导入", "revert": "回退", "autobuild": "自主构建"}
ROWS_LIMIT_DEFAULT, ROWS_LIMIT_MAX = 20, 500
TREND_POINTS = 6
_KEY_RE = re.compile(r"^[^/\\\x00-\x1f]{1,120}$")
_SORTABLE = ("key", "title", "updated_at")


class ArtifactError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


# 写入范围锁（方案 C10，P2 由变更集一侧实现）：登记 provider(root) -> ScopeLock | None
_lock_provider: Callable[[Path], dict | None] | None = None


def register_lock_provider(provider: Callable[[Path], dict | None]) -> None:
    global _lock_provider
    _lock_provider = provider


def _scope_lock(root: Path) -> dict | None:
    if _lock_provider is None:
        return None
    try:
        lock = _lock_provider(root)
    except Exception:  # noqa: BLE001 - 锁取不到按没有锁处理，写入时写入路径还会再查
        return None
    return lock if lock and lock.get("state") not in (None, "none") else None


def _locked(lock: dict | None, art_type: str, key: str, rel: str) -> bool:
    if not lock:
        return False
    if any(item.get("type") == art_type and item.get("key") == key for item in lock.get("artifacts") or []):
        return True
    import fnmatch

    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in lock.get("paths") or [])


# ---- 类型 ----


def _require_type(manager, root: Path, art_type: str) -> ArtifactType:
    found = get_type(manager, root, art_type)
    if found is None:
        raise ArtifactError("artifact_type_unknown", f"产物类型 {art_type} 没有登记，或登记它的包没启用", 404)
    return found


def list_types(manager, project_id: str) -> dict:
    """AR-01。"""
    root = manager.project_root(project_id)
    return {"items": [item.public() for item in types_for_project(manager, root)]}


# ---- 行视图 ----


def _status(atype: ArtifactType, fields: dict) -> dict | None:
    if not atype.status:
        return None
    value = fields.get(atype.status["field"])
    if value in (None, ""):
        return None
    value = str(value)
    spec = atype.field(atype.status["field"]) or {}
    tone = next((item.get("tone") for item in spec.get("enum") or [] if item["value"] == value), None)
    tone = tone or (atype.status.get("tones") or {}).get(value) or "gray"
    return {"value": value, "label": atype.status["map"].get(value, value), "tone": tone}


def _public_fields(atype: ArtifactType, fields: dict) -> dict:
    out = dict(fields)
    for spec in atype.fields:
        if spec["type"] != "ref" or spec.get("kind") is None:
            continue
        value = out.get(spec["name"])
        if isinstance(value, list):
            out[spec["name"]] = [ref for ref in (index.entry_ref(spec["kind"], item) for item in value) if ref]
        elif value not in (None, "") and not isinstance(value, dict):
            out[spec["name"]] = index.entry_ref(spec["kind"], value)
    return out


def _row_view(atype: ArtifactType, row: dict, lock: dict | None, iso) -> dict:
    return {
        "key": row["key"], "title": row["title"],
        "fields": _public_fields(atype, row["fields"]),
        "status": _status(atype, row["fields"]),
        "stale": bool(row.get("stale")),
        "locked": _locked(lock, atype.type, row["key"], row["rel"]),
        "updated_at": iso(row.get("updated_at")),
        "revision": row.get("revision") or "",
        "path": row["rel"],
    }


def _enrich_history(atype: ArtifactType, root: Path, rows: list[dict]) -> None:
    """derive 里要「最近一次提交的来源 / 提交人」的类型：一次 git log 拿到每个文件最近的提交。"""
    wanted = {name: how for name, how in atype.derive.items() if how in ("last_source_label", "last_author")}
    if not wanted or not rows:
        return
    latest = _latest_commits(root, atype.glob_root() or ".", 300)
    for row in rows:
        info = latest.get(row["rel"])
        for name, how in wanted.items():
            if row["fields"].get(name) not in (None, ""):
                continue
            if info is None:
                row["fields"][name] = "未保存" if how == "last_source_label" else ""
            elif how == "last_source_label":
                row["fields"][name] = SOURCE_LABELS.get(info["source"], info["source"])
            else:
                row["fields"][name] = info["author"]


def _latest_commits(root: Path, pathspec: str, limit: int) -> dict[str, dict]:
    """路径 → 最近一次改它的提交的来源与提交人（trailer 优先，存量提交按说明前缀推断）。"""
    from ...services.workspace.versions import _subject_author, version_source

    fmt = ("%x1e%H%x1f%s%x1f%an%x1f%(trailers:key=Onto-Source,valueonly,separator=%x2c)"
           "%x1f%(trailers:key=Onto-Author,valueonly,separator=%x2c)%x1d")
    try:
        out = subprocess.run(["git", "log", f"-n{limit}", f"--format={fmt}", "--name-only", "--", pathspec],
                             cwd=root, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    latest: dict[str, dict] = {}
    for chunk in out.split("\x1e")[1:]:
        header, _, names = chunk.partition("\x1d")
        commit, subject, author, source, onto_author = (header.split("\x1f") + [""] * 5)[:5]
        source = source.strip()
        info = {"commit": commit, "source": version_source(subject, {"Onto-Source": source} if source else {}),
                "author": onto_author.strip() or _subject_author(subject) or author}
        for name in names.splitlines():
            name = name.strip()
            if name and name not in latest:
                latest[name] = info
    return latest


def _rows(manager, root: Path, atype: ArtifactType) -> list[dict]:
    rows = index.refresh(atype, root)
    _enrich_history(atype, root, rows)
    return rows


# ---- AR-02 列表 ----


def list_rows(manager, project_id: str, art_type: str, *, filters: dict, q: str = "", stale: bool | None = None,
              sort: str = "", order: str = "", cursor: str = "", limit: int = 0, page, iso) -> dict:
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    allowed = set(atype.view["list"].get("filters") or [])
    bad = sorted(set(filters) - allowed)
    if bad:
        raise ArtifactError("invalid_argument", f"不能按 {'、'.join(bad)} 筛选（视图只声明了 {'、'.join(sorted(allowed)) or '无'}）",
                            400, {"fields": {f"filter[{name}]": "not_filterable" for name in bad}})
    if order and order not in ("asc", "desc"):
        raise ArtifactError("invalid_argument", "order 只能是 asc 或 desc", 400, {"fields": {"order": "enum"}})
    names = {spec["name"] for spec in atype.fields}
    if sort and sort not in names and sort not in _SORTABLE:
        raise ArtifactError("invalid_argument", f"不能按 {sort} 排序", 400, {"fields": {"sort": "unknown"}})
    lock = _scope_lock(root)
    rows = _rows(manager, root, atype)
    views = [_row_view(atype, row, lock, iso) for row in rows]
    facets = _facets(atype, views)
    hits = index.search(root, atype.type, q) if q.strip() else None

    def keep(view: dict) -> bool:
        if hits is not None and view["key"] not in hits:
            return False
        if stale is not None and view["stale"] != stale:
            return False
        for name, value in filters.items():
            current = view["fields"].get(name)
            current = current.get("key") if isinstance(current, dict) else current
            if str(current if current is not None else "") not in str(value).split(","):
                return False
        return True

    items = [view for view in views if keep(view)]
    if sort:
        def sort_key(view):
            value = view.get(sort) if sort in _SORTABLE else view["fields"].get(sort)
            value = value.get("name") if isinstance(value, dict) else value
            return (value is None, str(value) if not isinstance(value, (int, float)) else f"{value:020.6f}")
        items.sort(key=sort_key, reverse=order == "desc")
    result = page(items, cursor, limit, {"type": art_type, "filters": filters, "q": q, "stale": stale,
                                         "sort": sort, "order": order})
    body = {"items": result["items"], "next_cursor": result["next_cursor"], "total": len(items), "facets": facets}
    for name, rel in atype.extras.items():
        body[name] = [_extra(record) for record in index.read_extras(root, rel)]
    return body


def _extra(record: dict) -> dict:
    out = dict(record)
    if isinstance(out.get("entry"), str):
        out["entry"] = index.entry_ref(str(out.pop("kind", "object")), out["entry"])
    return out


def _facets(atype: ArtifactType, views: list[dict]) -> dict:
    out = {}
    for name in atype.view["list"].get("filters") or []:
        spec = atype.field(name) or {}
        labels = {item["value"]: item["label"] for item in spec.get("enum") or []}
        if atype.status and atype.status["field"] == name:
            labels = {**atype.status["map"], **labels}
        counts: dict[str, int] = {}
        for view in views:
            value = view["fields"].get(name)
            value = value.get("key") if isinstance(value, dict) else value
            if value in (None, ""):
                continue
            counts[str(value)] = counts.get(str(value), 0) + 1
        ordered = [value for value in labels if value in counts] + sorted(value for value in counts if value not in labels)
        out[name] = [{"value": value, "label": labels.get(value, value), "count": counts[value]} for value in ordered]
    return out


# ---- AR-03 详情 ----


def _find(manager, root: Path, atype: ArtifactType, key: str) -> dict:
    row = next((row for row in _rows(manager, root, atype) if row["key"] == key), None)
    if row is None:
        raise ArtifactError("artifact_not_found", f"{atype.title}「{key}」不存在", 404)
    return row


def _parsed(atype: ArtifactType, root: Path, row: dict) -> parsers.Parsed | None:
    try:
        items = parsers.parse_file(atype, root, row["rel"])
    except (OSError, parsers.ParseError):
        return None
    return next((item for item in items if item.key == row["key"]), None)


def detail(manager, project_id: str, art_type: str, key: str, *, rows_limit: int = 0, iso) -> dict:
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    row = _find(manager, root, atype, key)
    return _detail_view(atype, root, row, rows_limit=rows_limit, iso=iso)


def _detail_view(atype: ArtifactType, root: Path, row: dict, *, rows_limit: int, iso) -> dict:
    lock = _scope_lock(root)
    parsed = _parsed(atype, root, row)
    limit = max(1, min(int(rows_limit or ROWS_LIMIT_DEFAULT), ROWS_LIMIT_MAX))
    table = None
    if parsed is not None and parsed.table is not None:
        table = {**parsed.table, "rows": parsed.table["rows"][:limit]}
    content = parsed.content if parsed is not None else None
    if atype.format == "jsonl" and isinstance(content, list):
        content = content[:limit]
    sections = dict(row.get("sections") or {})
    ordered = {name: sections.get(name, "") for name in atype.sections}
    ordered.update({name: value for name, value in sections.items() if name not in ordered})
    refs = []
    for ref in atype.refs:
        value = row["fields"].get(ref["field"])
        for item in value if isinstance(value, list) else [value]:
            entry = index.entry_ref(ref["kind"], item) if item not in (None, "") else None
            if entry:
                refs.append({**entry, "field": ref["field"]})
    if atype.format == "drawio" and isinstance(content, dict):
        for node in content.get("nodes") or []:
            refs += [{**ref, "field": f"nodes/{node['id']}"} for ref in node.get("refs") or []]
    view = _row_view(atype, row, lock, iso)
    return {
        **view,
        "type": atype.type, "format": atype.format,
        "format_version": str(row["fields"].get("format_version") or atype.schema_version),
        "sections": ordered, "table": table, "content": content, "refs": refs,
        "stale_reason": row.get("stale_reason"),
        "lock": lock if view["locked"] else None,
    }


# ---- AR-04 历史 ----


def history(manager, project_id: str, art_type: str, key: str, *, cursor: str = "", limit: int = 0, page, iso) -> dict:
    from ...services import mainline
    from ...services.workspace.versions import _trailers, version_source

    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    row = _find(manager, root, atype, key)
    out = subprocess.run(
        ["git", "log", "-n200", "--format=%x1e%H%x1f%ct%x1f%s%x1f%b", "--", row["rel"]],
        cwd=root, capture_output=True, text=True, timeout=30,
    ).stdout
    numbers = mainline.first_parent_numbers(root)
    items = []
    for chunk in out.split("\x1e")[1:]:
        parts = chunk.split("\x1f")
        if len(parts) < 4:
            continue
        commit, ts, subject, body = parts[0], parts[1], parts[2], parts[3]
        number = numbers.get(commit)
        items.append({"commit": commit, "version_label": mainline.label(number) if number else None,
                      "at": iso(int(ts)) if ts.isdigit() else None,
                      "source": version_source(subject, _trailers(body)), "summary": subject.strip()})
    result = page(items, cursor, limit, {"type": art_type, "key": key})
    return {"items": result["items"], "next_cursor": result["next_cursor"]}


# ---- 校验：字段定义、包的门禁 ----


_JSON_TYPES = {"text": "string", "markdown": "string", "code": "string", "anchor": "string", "date": "string",
               "number": "number", "list": "array", "table": "array"}


def json_schema(atype: ArtifactType) -> dict:
    """登记的字段定义 → JSON Schema（保存前校验用）。未声明的字段放行（frontmatter 里常有附加项）。"""
    properties = {}
    for spec in atype.fields:
        if spec["type"] == "enum":
            properties[spec["name"]] = {"enum": [item["value"] for item in spec.get("enum") or []] + [None, ""]}
        elif spec["type"] == "ref":
            properties[spec["name"]] = {"type": ["string", "object", "array", "null"]}
        else:
            properties[spec["name"]] = {"type": [_JSON_TYPES.get(spec["type"], "string"), "null"]}
    required = [spec["name"] for spec in atype.fields if spec["required"]]
    return {"type": "object", "properties": properties, "required": required}


def schema_errors(atype: ArtifactType, fields: dict) -> list[dict]:
    errors = []
    validator = jsonschema.Draft202012Validator(json_schema(atype))
    for error in validator.iter_errors(fields):
        field = error.path[0] if error.path else (error.message.split("'")[1] if "'" in error.message else "")
        errors.append({"field": str(field), "message": _schema_message(atype, error)})
    for spec in atype.fields:
        if spec["required"] and spec["name"] in fields and fields[spec["name"]] in (None, ""):
            errors.append({"field": spec["name"], "message": f"「{spec['label']}」不能为空"})
    return errors


def _schema_message(atype: ArtifactType, error) -> str:
    name = error.path[0] if error.path else ""
    spec = atype.field(str(name)) or {}
    label = spec.get("label") or name
    if error.validator == "required":
        missing = error.message.split("'")[1] if "'" in error.message else ""
        return f"缺少必填字段「{(atype.field(missing) or {}).get('label') or missing}」"
    if error.validator == "enum":
        allowed = "、".join(item["label"] for item in spec.get("enum") or [])
        return f"「{label}」只能取 {allowed}"
    return f"「{label}」类型不对"


def _gates(manager, root: Path, atype: ArtifactType) -> list[dict]:
    from ...services.packs import registry

    for view in registry.desired_packs(manager, root):
        if view.name == atype.pack:
            return [gate for gate in view.manifest.declared("gates") if gate.get("artifact") == atype.type]
    return []


def gate_findings(manager, root: Path, atype: ArtifactType, fields: dict) -> list[dict]:
    """包声明的门禁规则（声明式，详设 5.6）：required、in、max_length、ref_exists。"""
    failed = []
    for gate in _gates(manager, root, atype):
        for rule in gate.get("rules") or []:
            name = str(rule.get("field") or "")
            value = fields.get(name)
            label = (atype.field(name) or {}).get("label") or name
            problem = None
            if rule.get("required") and value in (None, "", []):
                problem = f"「{label}」必须填写"
            elif rule.get("in") and value not in (None, "") and str(value) not in [str(item) for item in rule["in"]]:
                problem = f"「{label}」只能是 {'、'.join(str(item) for item in rule['in'])}"
            elif rule.get("max_length") and isinstance(value, str) and len(value) > int(rule["max_length"]):
                problem = f"「{label}」不能超过 {rule['max_length']} 个字符"
            elif rule.get("ref_exists") and value not in (None, ""):
                kind = rule.get("kind") or next((ref["kind"] for ref in atype.refs if ref["field"] == name), "object")
                if index.entity_path(root, kind, value if not isinstance(value, dict) else value.get("key", "")) is None:
                    problem = f"「{label}」引用的条目不存在"
            if problem:
                failed.append({"id": str(gate.get("id") or ""), "label": str(gate.get("label") or gate.get("id") or ""),
                               "field": name, "message": str(rule.get("message") or problem)})
    return failed


# ---- AR-05～AR-08 写入与校验 ----


def _require_writable(atype: ArtifactType) -> None:
    if atype.readonly:
        raise ArtifactError("artifact_readonly", f"{atype.title}不能在这里修改", 403, {"reason": atype.readonly})
    if atype.index and atype.format not in ("md-frontmatter", "md-sections", "yaml", "json", "csv", "jsonl", "drawio"):
        raise ArtifactError("artifact_readonly", "这类产物只能由包的命令生成", 403, {"reason": "index"})


def _ref_value(value):
    if isinstance(value, dict):
        return value.get("key") or value.get("name") or ""
    if isinstance(value, list):
        return [_ref_value(item) for item in value]
    return value


def _incoming_fields(atype: ArtifactType, fields: dict | None) -> dict:
    """前端回传的字段：EntryRef 还原成条目名；只收登记过的字段（计算出来的派生字段不写回）。"""
    if not fields:
        return {}
    known = {spec["name"]: spec for spec in atype.fields}
    derived = set(atype.derive)
    out = {}
    for name, value in fields.items():
        if name in derived or name not in known:
            continue
        out[name] = _ref_value(value) if known[name]["type"] == "ref" else value
    return out


def _content_for_save(atype: ArtifactType, root: Path, rel: str, body: dict, previous: str | None):
    """流程图：标准编辑器提交 FlowchartModel、drawio 高级编辑提交 drawio_xml（契约 F2 第 6 条）。"""
    from . import flowchart

    content = body.get("content")
    if not isinstance(content, dict):
        raise ArtifactError("artifact_schema_invalid", "流程图要提交 content（流程图模型）", 422,
                            {"errors": [{"field": "content", "message": "缺少流程图模型"}]})
    xml = content.get("drawio_xml")
    try:
        if xml:
            previous_model = flowchart.parse(previous, key=Path(rel).stem) if previous else None
            if previous_model and previous_model.get("storage") == "drawio_compressed":
                return xml, flowchart.parse(xml, key=Path(rel).stem)
            model = flowchart.from_drawio_edit(xml, previous_model)
        else:
            model = flowchart.normalize(content)
        return flowchart.render(model, previous=previous), model
    except flowchart.FlowchartError as exc:
        raise ArtifactError("artifact_schema_invalid", str(exc), 422,
                            {"errors": [{"field": "content", "message": str(exc)}]}) from exc


def _write_text(atype: ArtifactType, root: Path, rel: str, body: dict, previous: str | None, item_key: str,
                existing_fields: dict) -> tuple[str, dict]:
    """算出要写的文件内容与校验用的字段。"""
    if atype.format == "drawio":
        text, model = _content_for_save(atype, root, rel, body, previous)
        fields = {"title": model["process"].get("name") or Path(rel).stem}
        return text, fields
    incoming = _incoming_fields(atype, body.get("fields"))
    fields = {**{key: value for key, value in existing_fields.items() if key not in atype.derive}, **incoming}
    sections = body.get("sections") if isinstance(body.get("sections"), dict) else None
    content = body.get("content")
    if atype.format in ("csv", "jsonl") and content is None and previous is not None:
        raise ArtifactError("invalid_argument", "表格类产物要提交 content", 400, {"fields": {"content": "required"}})
    try:
        text = parsers.render(atype, previous, fields=incoming, sections=sections, content=content, item_key=item_key)
    except (parsers.ParseError, ValueError, TypeError) as exc:
        raise ArtifactError("artifact_schema_invalid", str(exc), 422,
                            {"errors": [{"field": "content", "message": str(exc)}]}) from exc
    if atype.format in ("yaml", "json") and isinstance(content, dict):
        fields = {**fields, **{key: value for key, value in content.items() if not isinstance(value, (dict, list))}}
    return text, fields


def _check(manager, root: Path, atype: ArtifactType, fields: dict) -> None:
    errors = schema_errors(atype, fields)
    if errors:
        raise ArtifactError("artifact_schema_invalid", errors[0]["message"], 422, {"errors": errors})
    failed = gate_findings(manager, root, atype, fields)
    if failed:
        raise ArtifactError("gate_failed", failed[0]["message"], 422, {"failed": failed})


def _commit(manager, project_id: str, rel: str, text: str | None, message: str, actor: str) -> dict:
    from ...kernel.writepath import ChangeTx, Origin
    from ...services import mainline

    with ChangeTx(manager, project_id, Origin("web", actor), summary=message) as tx:
        if text is None:
            tx.remove(rel)
        else:
            tx.write(rel, text)
        result = tx.commit(message)
    state = mainline.state(manager.project_root(project_id))
    return {"commit": result.commit or state["head"], "version_label": state["version_label"]}


def _check_format_version(atype: ArtifactType, fields: dict) -> None:
    current = fields.get("format_version")
    if current not in (None, "") and str(current) != atype.schema_version:
        raise ArtifactError("format_migration_required", "这份产物还是旧格式，要先用包的迁移命令迁移", 409,
                            {"from": str(current), "to": atype.schema_version})


def _locked_or_raise(root: Path, atype: ArtifactType, key: str, rel: str) -> None:
    lock = _scope_lock(root)
    if _locked(lock, atype.type, key, rel):
        raise ArtifactError("write_scope_locked", "助手正在改这一片，确认或撤回之前只读", 423, lock)


def save(manager, project_id: str, art_type: str, key: str, body: dict, *, actor: str, iso) -> dict:
    """AR-05：保存。base_revision 必填（与契约 1.7 一致：不一致 409 revision_conflict）。"""
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    _require_writable(atype)
    row = _find(manager, root, atype, key)
    base = str(body.get("base_revision") or "")
    if not base:
        raise ArtifactError("invalid_argument", "base_revision 必填", 400, {"fields": {"base_revision": "required"}})
    if base != row.get("revision"):
        raise ArtifactError("revision_conflict", "产物已被他人或助手修改，请刷新后再保存", 409,
                            {"target": f"{art_type}/{key}", "expected": base, "actual": row.get("revision")})
    _check_format_version(atype, row["fields"])
    _locked_or_raise(root, atype, key, row["rel"])
    previous = (root / row["rel"]).read_text(encoding="utf-8")
    text, fields = _write_text(atype, root, row["rel"], body, previous, key, row["fields"])
    _check(manager, root, atype, fields)
    summary = str(body.get("summary") or "").strip()[:80]
    note = f"：{summary}" if summary else ""
    outcome = _commit(manager, project_id, row["rel"], text, f"web: 保存{atype.title}「{row['title']}」{note} by {actor}", actor)
    project_events.emit(project_id, "artifacts.updated", {"type": art_type, "change": "updated", "keys": [key]})
    saved = _find(manager, root, atype, key) if atype.format != "md-sections" else _find_by_file(manager, root, atype, row["rel"], key)
    return {"artifact": _detail_view(atype, root, saved, rows_limit=0, iso=iso), **outcome}


def _find_by_file(manager, root: Path, atype: ArtifactType, rel: str, key: str) -> dict:
    rows = _rows(manager, root, atype)
    return next((row for row in rows if row["key"] == key), None) or next(row for row in rows if row["rel"] == rel)


def _new_key(atype: ArtifactType, body: dict) -> str:
    title = str((body.get("fields") or {}).get("title") or "").strip()
    slug = re.sub(r"[\\/:*?\"<>|#\[\]&\s.]+", "-", title).strip("-")[:60]
    return slug or f"{atype.type.split('.', 1)[1]}-{uuid.uuid4().hex[:6]}"


def create(manager, project_id: str, art_type: str, body: dict, *, actor: str, iso) -> dict:
    """AR-06：新建。路径按登记的 path 模板算出来；md-sections 是往文件里追加一项。"""
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    _require_writable(atype)
    key = str(body.get("key") or "").strip() or _new_key(atype, body)
    if not _KEY_RE.match(key) or key.startswith("."):
        raise ArtifactError("invalid_argument", "key 不合法", 400, {"fields": {"key": "pattern"}})
    file_key = key.split("--", 1)[0] if atype.format == "md-sections" else key
    rel = atype.path_for(file_key)
    target = root / rel
    if atype.format != "md-sections" and target.exists():
        raise ArtifactError("invalid_argument", f"{atype.title}「{key}」已存在", 400, {"fields": {"key": "exists"}})
    _locked_or_raise(root, atype, key, rel)
    if atype.format == "md-sections":
        previous = target.read_text(encoding="utf-8") if target.exists() else f"# {file_key}\n\n"
        existing = [item.key for item in parsers.parse(atype, rel, previous.encode(), file_key=file_key)]
        number = max([int(item.rsplit("--", 1)[1]) for item in existing if item.rsplit("--", 1)[1].isdigit()] or [0]) + 1
        title = str((body.get("fields") or {}).get("title") or f"新{atype.title}")
        previous = previous.rstrip("\n") + f"\n\n## {atype.title} {number}：{title}\n\n"
        key = f"{file_key}--{number}"
        text, fields = _write_text(atype, root, rel, body, previous, key, {})
        fields.setdefault("title", title)
    else:
        if atype.format == "drawio" and not isinstance(body.get("content"), dict):
            body = {**body, "content": {"process": {"name": (body.get("fields") or {}).get("title") or key},
                                        "nodes": [], "edges": []}}
        text, fields = _write_text(atype, root, rel, body, None, key, {})
        if atype.format in ("md-frontmatter", "md-report") and atype.schema_version and "format_version" in {
                spec["name"] for spec in atype.fields}:
            fields.setdefault("format_version", atype.schema_version)
    _check(manager, root, atype, fields)
    outcome = _commit(manager, project_id, rel, text, f"web: 新建{atype.title}「{key}」 by {actor}", actor)
    project_events.emit(project_id, "artifacts.updated", {"type": art_type, "change": "created", "keys": [key]})
    row = _find(manager, root, atype, key)
    return {"artifact": _detail_view(atype, root, row, rows_limit=0, iso=iso), **outcome}


def delete(manager, project_id: str, art_type: str, key: str, body: dict, *, actor: str) -> dict:
    """AR-07：删除。md-sections 删掉文件里的那一项，删空了连文件一起删。"""
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    _require_writable(atype)
    row = _find(manager, root, atype, key)
    base = str(body.get("base_revision") or "")
    if base and base != row.get("revision"):
        raise ArtifactError("revision_conflict", "产物已被他人或助手修改，请刷新后再删除", 409,
                            {"target": f"{art_type}/{key}", "expected": base, "actual": row.get("revision")})
    _locked_or_raise(root, atype, key, row["rel"])
    text = None
    if atype.format == "md-sections":
        previous = (root / row["rel"]).read_text(encoding="utf-8")
        text = _remove_section(atype, previous, row["rel"], key)
    outcome = _commit(manager, project_id, row["rel"], text, f"web: 删除{atype.title}「{row['title']}」 by {actor}", actor)
    project_events.emit(project_id, "artifacts.updated", {"type": art_type, "change": "deleted", "keys": [key]})
    return outcome


def _remove_section(atype: ArtifactType, text: str, rel: str, key: str) -> str | None:
    lines = text.splitlines()
    for position, (heading, _body, start, end) in enumerate(parsers._section_blocks(text), 1):
        found = re.search(r"(\d+)", heading.split("：")[0].split(":")[0])
        if f"{key.split('--', 1)[0]}--{found.group(1) if found else position}" == key:
            rest = lines[:start] + lines[end + 1:]
            remaining = parsers._section_blocks("\n".join(rest))
            return None if not remaining else "\n".join(rest).rstrip("\n") + "\n"
    raise ArtifactError("artifact_not_found", f"{atype.title}「{key}」不存在", 404)


def validate(manager, project_id: str, art_type: str, key: str, body: dict) -> dict:
    """AR-08：按字段定义、包的门禁和格式自带的确定性检查校验；不调模型、不落盘。"""
    from . import flowchart

    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    row = _find(manager, root, atype, key)
    errors: list[dict] = []
    warnings: list[dict] = []
    if atype.format == "drawio":
        content = body.get("content")
        try:
            if isinstance(content, dict):
                model = (flowchart.from_drawio_edit(content["drawio_xml"], None) if content.get("drawio_xml")
                         else flowchart.normalize(content))
            else:
                model = flowchart.parse((root / row["rel"]).read_bytes(), key=key)
        except flowchart.FlowchartError as exc:
            return {"ok": False, "errors": [{"code": "FLOW-FORMAT", "level": "error", "message": str(exc)}], "warnings": []}
        found_errors, found_warnings = flowchart.validate(model)
        errors += found_errors
        warnings += found_warnings
        fields = {"title": model["process"].get("name") or key}
    else:
        fields = {**row["fields"], **_incoming_fields(atype, body.get("fields"))}
    for problem in schema_errors(atype, fields):
        errors.append({"code": "artifact_schema_invalid", "level": "error", "message": problem["message"],
                       "field": problem["field"]})
    for problem in gate_findings(manager, root, atype, fields):
        errors.append({"code": "gate_failed", "level": "error", "message": problem["message"], "field": problem["field"]})
    if row.get("stale"):
        warnings.append({"code": "artifact_stale", "level": "warning", "message": row.get("stale_reason") or "已过期"})
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---- AR-09 汇总 ----


def summary(manager, project_id: str, art_type: str, *, group_by: str = "", trend: bool = False) -> dict:
    root = manager.project_root(project_id)
    atype = _require_type(manager, root, art_type)
    field = group_by or (atype.status or {}).get("field") or ""
    if field and not atype.field(field) and field != (atype.status or {}).get("field"):
        raise ArtifactError("invalid_argument", f"不能按 {field} 分组", 400, {"fields": {"group_by": "unknown"}})
    rows = _rows(manager, root, atype)
    counts: dict[str, int] = {}
    for row in rows:
        value = row["fields"].get(field) if field else None
        value = parsers.plain_ref(value) if isinstance(value, str) else value
        counts[str(value if value not in (None, "") else "")] = counts.get(str(value if value not in (None, "") else ""), 0) + 1
    labels = dict((atype.status or {}).get("map") or {}) if field == (atype.status or {}).get("field") else {}
    spec = atype.field(field) or {}
    labels.update({item["value"]: item["label"] for item in spec.get("enum") or []})
    ordered = [value for value in labels if value in counts] + sorted(value for value in counts if value not in labels)
    by = [{"value": value, "label": labels.get(value, value or "未填"), "count": counts[value]} for value in ordered]
    points = _trend(atype, root) if trend else None
    delta = None
    if points and len(points) >= 2 and points[-1]["value_pct"] is not None and points[-2]["value_pct"] is not None:
        delta = round(points[-1]["value_pct"] - points[-2]["value_pct"], 1)
    return {"total": len(rows), "by": by, "stale": sum(1 for row in rows if row.get("stale")),
            "trend": points, "delta_vs_previous_pct": delta}


def _positive(atype: ArtifactType) -> str | None:
    if not atype.status:
        return None
    return str(atype.raw.get("status", {}).get("positive") or next(iter(atype.status["map"]), "")) or None


def _trend(atype: ArtifactType, root: Path) -> list[dict]:
    """最近几个改动过这类产物的主线版本上，状态为「正向值」（如通过）的占比。"""
    from ...services import mainline

    positive = _positive(atype)
    if not positive:
        return []
    paths = [atype.glob_root() or "."] + ([atype.index] if atype.index else [])
    log = subprocess.run(["git", "log", "--first-parent", f"-n{TREND_POINTS}", "--format=%H", "--", *paths],
                         cwd=root, capture_output=True, text=True, timeout=30).stdout.split()
    numbers = mainline.first_parent_numbers(root)
    points = []
    for commit in reversed(log):
        rows = _rows_at(atype, root, commit)
        if not rows:
            continue
        hits = sum(1 for row in rows if str(row["fields"].get(atype.status["field"]) or "") == positive)
        number = numbers.get(commit)
        points.append({"version_label": mainline.label(number) if number else commit[:7],
                       "value_pct": round(hits * 100.0 / len(rows), 1)})
    return points


def _rows_at(atype: ArtifactType, root: Path, commit: str) -> list[dict]:
    listing = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit, "--", atype.glob_root() or "."],
                             cwd=root, capture_output=True, text=True, timeout=30).stdout.splitlines()
    wanted = [rel for rel in listing if atype.key_from_path(rel) is not None and Path(rel).name not in ("index.md", "INDEX.md")]
    if atype.index:
        wanted.append(atype.index)
    blobs = _cat_files(root, commit, wanted)
    records = {}
    if atype.index and blobs.get(atype.index):
        for line in blobs[atype.index].decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("key"):
                records[str(record["key"])] = record
    rows = []
    for rel in wanted:
        if rel == atype.index or rel not in blobs:
            continue
        try:
            items = parsers.parse(atype, rel, blobs[rel], file_key=atype.key_from_path(rel) or Path(rel).stem)
        except parsers.ParseError:
            continue
        for item in items:
            fields = dict(item.fields)
            for name, value in ((records.get(item.key) or {}).get("fields") or {}).items():
                fields.setdefault(name, value)
                if fields.get(name) in (None, ""):
                    fields[name] = value
            rows.append({"key": item.key, "fields": fields})
    return rows


def _cat_files(root: Path, commit: str, rels: list[str]) -> dict[str, bytes]:
    if not rels:
        return {}
    request = "".join(f"{commit}:{rel}\n" for rel in rels).encode("utf-8")
    try:
        proc = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input=request, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict[str, bytes] = {}
    data = proc.stdout
    position = 0
    for rel in rels:
        newline = data.find(b"\n", position)
        if newline < 0:
            break
        header = data[position:newline].split()
        position = newline + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        out[rel] = data[position:position + size]
        position += size + 1
    return out


# ---- MCP 只读工具（契约 M-08 artifact_list / artifact_get）----

MCP_LIST_MAX = 50
MCP_SECTION_CHARS = 4000
MCP_TABLE_ROWS = 20


def mcp_list(manager, project_id: str, args: dict, *, page, iso) -> dict:
    """artifact_list：结果最多 50 行一页，不带分段正文；要正文用 artifact_get。"""
    limit = max(1, min(int(args.get("limit") or 20), MCP_LIST_MAX))
    body = list_rows(manager, project_id, str(args.get("type") or ""), filters=dict(args.get("filters") or {}),
                     q=str(args.get("query") or ""), stale=args.get("stale"), cursor=str(args.get("cursor") or ""),
                     limit=limit, page=page, iso=iso)
    return {"type": args.get("type"), "items": body["items"], "total": body["total"],
            "next_cursor": body["next_cursor"],
            "display": {"text": f"已返回 {len(body['items'])}/{body['total']} 个插件产物"}}


def mcp_get(manager, project_id: str, args: dict, *, iso) -> dict:
    """artifact_get：分段每段最多 4000 字、表格最多 20 行；流程图只给节点与连线摘要。"""
    view = detail(manager, project_id, str(args.get("type") or ""), str(args.get("key") or ""),
                  rows_limit=MCP_TABLE_ROWS, iso=iso)
    truncated = False
    sections = {}
    for name, text in (view.get("sections") or {}).items():
        text = str(text or "")
        if len(text) > MCP_SECTION_CHARS:
            truncated = True
            text = text[:MCP_SECTION_CHARS]
        sections[name] = text
    content = view.get("content")
    if view["format"] == "drawio" and isinstance(content, dict):
        content = {"process": content.get("process"), "operation": content.get("operation"),
                   "nodes": [{key: node.get(key) for key in ("id", "type", "label", "pending")} for node in content.get("nodes") or []][:200],
                   "edges": [{key: edge.get(key) for key in ("id", "type", "from", "to", "label")} for edge in content.get("edges") or []][:300]}
    elif content is not None and len(json.dumps(content, ensure_ascii=False, default=str)) > 20000:
        content, truncated = None, True
    return {"type": view["type"], "key": view["key"], "artifact": {**view, "sections": sections, "content": content,
                                                                   "lock": None},
            "truncated": truncated, "display": {"text": f"已读取插件产物「{view['title']}」"}}
