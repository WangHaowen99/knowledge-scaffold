"""Local MCP tool execution with content revisions and isolated file commits."""
import asyncio
import base64
import copy
import json
import re
from pathlib import Path

from .. import studio
from ..workspace import _git
from . import documents as doc
from .definitions import TOOLS, OPERATION, RULE, validate_input
from .errors import McpError
from .mutations import patch_object, patch_resource


def revision(root):
    return doc.revision(doc.snapshot(root))


def summary(record, records):
    value = doc.public(record, records)
    fields = ("key", "title", "description", "source_path", "relation_count")
    return {f"{record['kind']}_id": f"{record['kind']}:{record['key']}",
            **{key: value[key] for key in fields if key in value}}


def command(name, args):
    if name == "validate_ontology_operation_rule_change":
        return args["resource_type"], args["action"], doc.key(args["resource_id"], args["resource_type"]), args.get("payload", {})
    kind = "object" if "object" in name else "operation" if "operation" in name else "rule"
    action = "update" if name.startswith("validate_") else name.split("_", 1)[0]
    key = doc.key(args["key"]) if action == "create" else doc.key(args[kind + "_id"], kind)
    payload = ({k: v for k, v in args.items() if k not in ("key", "expected_revision")} if action == "create"
               else args if kind == "object" else args.get("changes", {}))
    return kind, action, key, payload


def issue(code, message, target="", **extra):
    return {"code": code, "message": message, "level": "blocking", "target": target, **extra}


def integrity(records):
    """Compare before/after problems; existing unrelated incomplete modeling is not a gate."""
    issues = {}
    by_key = {r["key"]: r for r in records.values()}
    for identity, record in records.items():
        for target in doc.links(record["text"]):
            if target not in by_key:
                key = (identity, "REFERENCE_NOT_FOUND", target)
                issues[key] = issue("REFERENCE_NOT_FOUND", "引用的本体元素不存在", record["path"], resource_id=target)
        kind = record["kind"]
        if kind not in ("object", "operation", "rule"):
            continue
        value = doc.public(record, records)
        relationships = {"related_objects": "object", "uses_rules": "rule", "uses_metrics": "metric",
                         "applies_to_objects": "object", "applies_to_operations": "operation"}
        for field, target_kind in relationships.items():
            for target in value.get(field, []):
                if f"{target_kind}:{target}" not in records:
                    issues[(identity, field, target)] = issue("REFERENCE_TYPE_INVALID", f"{field} 的引用类型不匹配", record["path"])
        if kind == "object":
            for relation in value["relations"]:
                if not relation["target_exists"]:
                    issues[(identity, "relation", relation["target_key"])] = issue("RELATION_TARGET_INVALID", "关系目标对象不存在", record["path"])
    return issues


def editing_issue(project_id, path):
    from ..agent_sessions import manager
    state = manager.live_status(project_id)
    if state and state.get("busy"):
        return issue("PROJECT_EDITING_BUSY", "助手正在修改该项目，请等待当前任务结束", path)
    if studio.lease_status(project_id, path) or studio.get_draft(project_id, path):
        return issue("PROJECT_EDITING_BUSY", "条目存在 Studio 租约或未提交草稿，请先完成或放弃编辑", path)
    return None


def analyze(project_id, records, current, name, args):
    kind, action, key, payload = command(name, args)
    identity = f"{kind}:{key}"
    old = records.get(identity)
    if action != "create" and old is None:
        raise McpError("RESOURCE_NOT_FOUND", "本体资源不存在", 404, resource_id=identity)
    path = old["path"] if old else f"ontology/{kind}/{key}.md"
    resource = {"id": identity, "kind": kind, "key": key, "path": path}
    result = {"ok": False, "revision": current, "resource": resource, "changed_paths": [],
              "affected_resources": [resource], "historical_problem_count": 0, "field_errors": {}, "issues": [],
              "warnings": [], "display": {"text": "修改存在阻塞项，请修正后重试"}}
    next_records = copy.deepcopy(records)
    problem = editing_issue(project_id, path)
    if problem:
        result["issues"].append(problem)
    try:
        if action == "create":
            if any(r["key"].casefold() == key.casefold() for r in records.values()) or key.casefold() == "index":
                raise McpError("RESOURCE_ALREADY_EXISTS", "本体文件标识已存在", 409)
            create_schema = TOOLS[f"create_ontology_{kind}"]["inputSchema"]
            validate_input({"expected_revision": current, "key": key, **payload}, create_schema)
            seed = {"kind": kind, "key": key, "path": path, "text": ""}
            text = patch_resource(seed, payload)
            next_records[identity] = {**seed, "text": text}
        elif action == "delete":
            references = [r for rid, r in records.items() if rid != identity and key in doc.links(r["text"])]
            if references:
                raise McpError("RESOURCE_REFERENCED", "资源仍被其他文档引用", 422,
                               references=[r["path"] for r in references])
            del next_records[identity]
        else:
            text = patch_object(old, payload, records) if kind == "object" else patch_resource(old, payload)
            if text == old["text"]:
                raise McpError("NO_CHANGES", "没有产生有效修改", 422)
            next_records[identity]["text"] = text
        baseline = integrity(records)
        after = integrity(next_records)
        result["historical_problem_count"] = len(baseline)
        result["issues"].extend(value for key, value in after.items() if key not in baseline)
        # Field removal/rename cannot invalidate explicit field references in other documents.
        if kind == "object" and action == "update" and "field_ops" in payload:
            before = {f["key"] for f in doc.public(old, records)["fields"]}
            after_fields = {f["key"] for f in doc.public(next_records[identity], next_records)["fields"]}
            for removed in before - after_fields:
                pattern = re.compile(r"(?<![\w])" + re.escape(key + "." + removed) + r"(?![\w])")
                if any(pattern.search(r["text"]) for rid, r in records.items() if rid != identity):
                    result["issues"].append(issue("FIELD_REFERENCED", "字段仍被其他文档引用，不能移除或改名", removed))
        result["changed_paths"] = [path]
        if action != "delete":
            result["next_" + kind] = doc.public(next_records[identity], next_records)
    except McpError as exc:
        result["issues"].append(issue(exc.code, str(exc), identity, **exc.details))
    result["ok"] = not result["issues"]
    result["field_errors"] = {item["target"]: item["message"] for item in result["issues"]}
    if result["ok"]:
        result["display"] = {"text": "修改通过确定性检查，可以提交"}
    return result, next_records, identity


def persist(manager, project_id, root, before, after, identity, expected, username):
    record = after.get(identity) or before[identity]
    path = manager._resolve_inside(root, record["path"])
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
        raise McpError("UNSAFE_PATH", "本体路径不能为符号链接", 409)
    if revision(root) != expected:
        raise McpError("REVISION_CONFLICT", "工作区内容已更新", 409)
    busy = editing_issue(project_id, record["path"])
    if busy:
        raise McpError("PROJECT_EDITING_BUSY", busy["message"], 409)
    # Do not include staged edits belonging to the user/agent in this commit.
    if _git(root, "diff", "--cached", "--name-only").strip():
        raise McpError("PROJECT_EDITING_BUSY", "项目有正在暂存的修改，请稍后重试", 409)
    previous = manager._snapshot(path)
    try:
        if identity in after:
            manager._atomic_write_text(path, after[identity]["text"])
        else:
            path.unlink()
        _git(root, "add", "--", record["path"])
        _git(root, "commit", "--only", "-m", f"mcp: 更新 {identity}", "--", record["path"])
    except Exception:
        manager._restore_snapshot(path, previous)
        # Reset only our index entry; leave unrelated staged/worktree changes intact.
        _git(root, "reset", "HEAD", "--", record["path"])
        raise
    from .. import audit
    audit.log(username, "MCP", f"/projects/{project_id}/ontology/{identity}")


def list_resources(records, kind, args, current):
    query = args.get("query", "").strip().casefold()
    sort, order = args.get("sort", "key"), args.get("order", "asc")
    if sort not in ("key", "title", "description", "relation_count"):
        raise McpError("INVALID_INPUT", "不支持的排序字段")
    values = [summary(r, records) for r in records.values() if r["kind"] == kind]
    values = [v for v in values if query in "\n".join(str(v.get(k, "")) for k in ("key", "title", "description")).casefold()]
    values.sort(key=lambda v: (v.get(sort, 0 if sort == "relation_count" else ""), v["key"]), reverse=order == "desc")
    context = {"kind": kind, "query": query, "sort": sort, "order": order}
    offset = 0
    if args.get("cursor"):
        try:
            raw = args["cursor"]
            cursor = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            if cursor["revision"] != current:
                raise McpError("CURSOR_REVISION_MISMATCH", "游标对应的版本已变化，请重新读取第一页", 409)
            if cursor["context"] != context:
                raise McpError("CURSOR_CONTEXT_MISMATCH", "游标与查询条件不匹配")
            offset = cursor["offset"]
            if type(offset) is not int or offset < 0:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise McpError("INVALID_CURSOR", "游标格式无效") from None
    limit = args.get("limit", 50)
    page = values[offset:offset + limit]
    cursor = None
    if offset + len(page) < len(values):
        cursor = base64.urlsafe_b64encode(json.dumps({"revision": current, "context": context,
                                                   "offset": offset + len(page)}).encode()).decode().rstrip("=")
    return {"revision": current, "items": page, "total": len(values), "next_cursor": cursor,
            "display": {"text": f"已返回 {len(page)}/{len(values)} 个本体元素"}}


def execute(manager, project_id, name, args, username, token_id=""):
    if name not in TOOLS:
        raise McpError("MCP_TOOL_NOT_FOUND", "未知工具", 404)
    validate_input(args, TOOLS[name]["inputSchema"])
    if name == "list_project_versions":
        from .. import version_revert
        return version_revert.list_versions(manager, project_id, args.get("limit", 50), args.get("cursor", ""))
    if name == "preview_project_version_revert":
        from .. import version_revert
        return version_revert.preview_by_version(
            manager, project_id, args["target_version"], token_id, username
        )
    if name == "revert_project_version":
        raise McpError("ASYNC_TOOL_REQUIRED", "版本回退必须通过异步 MCP 调用")
    # Shared with web writes, protects the revision check and persistence as one operation.
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        records = doc.snapshot(root)
        current = doc.revision(records)
        requested = args.get("expected_revision") if TOOLS[name]["toolset"] == "change" else args.get("revision")
        if requested is not None and requested != current:
            raise McpError("REVISION_CONFLICT", "工作区内容已变化，请重新读取当前 revision", 409,
                           expected_revision=requested, actual_revision=current)
        if name.startswith("list_"):
            kind = "object" if name.endswith("objects") else "operation" if name.endswith("operations") else "rule"
            return list_resources(records, kind, args, current)
        if name.startswith("get_"):
            kind = name.split("_")[-1]
            key = doc.key(args[kind + "_id"], kind)
            record = records.get(kind + ":" + key)
            if record is None:
                raise McpError("RESOURCE_NOT_FOUND", "本体资源不存在", 404)
            return {"revision": current, kind + "_id": kind + ":" + key, kind: doc.public(record, records),
                    "display": {"text": "已读取 Onto 当前本体资源"}}
        analysis, proposed, identity = analyze(project_id, records, current, name, args)
        if name.startswith("validate_"):
            return analysis
        if not analysis["ok"]:
            raise McpError("SUBMISSION_BLOCKED", "修改未通过确定性检查", 422, **analysis)
        persist(manager, project_id, root, records, proposed, identity, current, username)
        kind = identity.split(":", 1)[0]
        return {"resource": analysis["resource"], kind: analysis.get("next_" + kind),
                "previous_revision": current, "revision": revision(root),
                "changed_paths": analysis["changed_paths"], "affected_resources": analysis["affected_resources"],
                "warnings": [], "historical_problem_count": analysis["historical_problem_count"],
                "display": {"text": "修改已保存到 Onto 工作区"}}


async def execute_async(manager, project_id, name, args, username, token_id=""):
    if name != "revert_project_version":
        return await asyncio.to_thread(execute, manager, project_id, name, args, username, token_id)
    validate_input(args, TOOLS[name]["inputSchema"])
    if not args.get("confirm"):
        raise McpError("CONFIRMATION_REQUIRED", "必须明确确认执行版本回退")
    from .. import version_revert
    return await version_revert.apply_preview(
        manager, project_id, args["preview_id"], token_id, username
    )
