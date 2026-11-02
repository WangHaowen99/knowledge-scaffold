"""Studio 条目（API 契约 3.7）：ST-01 统计、ST-02 关系图、ST-03 条目表、ST-04 条目详情、ST-05 操作流程、
ST-07 草稿、ST-08 提交、ST-09 新建、ST-10 删除。条目按 entry_id（`<kind>:<key>`）定位；30 分钟租约取消，
条目在写入范围锁（C10）里时只读。
"""

from __future__ import annotations

import asyncio

import re
from pathlib import Path

from fastapi import APIRouter, Request, Response

from ..api import deps
from ..api.errors import ApiError
from ..governance import telemetry
from ..kernel.metamodel import mm
from ..services import mainline
from ..services import studio as studio_service
from ..services.workspace.layout import _content_revision

router = APIRouter(prefix="/api/v1/projects/{project_id}/studio", tags=["studio"])

# 条目类型、中文名与目录取元模型（新条目写到主目录）
KINDS = mm.ENTITY_KINDS
KIND_LABELS = {entity.kind: entity.label for entity in mm.ENTITY_TYPES}
KIND_PATHS = {entity.kind: (entity.directory, entity.suffix) for entity in mm.ENTITY_TYPES}
_DATA_SOURCE_RE = re.compile(r"^#\s*数据来源\s*\n+([^\n#]*)", re.M)


def _count_check_failure(check: str, result: str) -> str:
    """确定性检查拒绝保存：计入「检查失败」指标（O-02），原样返回结果值。"""
    telemetry.count("onto_check_failures_total", check=check)
    return result


def entry_path(root: Path, entry_id: str) -> tuple[str, str, str]:
    """entry_id → (仓库相对路径, kind, key)；流程图条目已改为插件产物（410，见 AR-03）。"""
    kind, _, key = (entry_id or "").partition(":")
    if kind == "flowchart":
        raise ApiError("endpoint_removed", "流程图改由插件产物接口读写", 410,
                       {"successor": "/api/v1/projects/{id}/artifacts/diagram.flowchart/{key}"})
    if kind not in KIND_PATHS or not key or "/" in key or "\\" in key or key.startswith("."):
        raise ApiError("not_found", "条目不存在", 404)
    directory, suffix = KIND_PATHS[kind]
    rel = f"{directory}/{key}{suffix}"
    if kind == "operation" and not (root / rel).is_file() and (root / f"ontology/logic/{key}.md").is_file():
        rel = f"ontology/logic/{key}.md"  # operation 的旧目录（ADR-0004）
    return rel, kind, key


def entry_ref(kind: str, key: str, name: str | None = None) -> dict:
    return {"kind": kind, "id": f"{kind}:{key}", "key": key, "name": name or key}


def _graph(root: Path) -> tuple[dict, dict[str, dict]]:
    data = studio_service.graph(root)
    by_name = {}
    for node in data["nodes"]:
        by_name.setdefault(node["id"], node)
    return data, by_name


def _edge_kind(relation: str) -> str:
    return "mention" if relation == "references" else "declared"


# ---- ST-01 统计、ST-02 关系图、ST-03 条目表 ----


@router.get("/stats")
def stats(project_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    data, _ = _graph(root)
    counts = {kind: 0 for kind in KINDS}
    for node in data["nodes"]:
        counts[node["kind"]] = counts.get(node["kind"], 0) + 1
    state = mainline.state(root)
    return {
        "objects": counts["object"], "operations": counts["operation"], "rules": counts["rule"],
        "metrics": counts["metric"], "scripts": counts["script"],
        "relations": sum(1 for edge in data["edges"] if _edge_kind(edge["kind"]) == "declared"),
        "flowcharts": None, "scenarios": None,
        "version_label": state["version_label"], "head": state["head"],
    }


def _scope(project_id: str) -> dict:
    """写入范围锁（方案 C10）：条目在范围里就只读，图谱、表格、详情都标出来。"""
    from ..kernel.changesets import service as changesets

    return changesets.scope_lock(deps.workspaces, project_id)


def _locked(scope: dict, entry_id: str, rel: str | None = None) -> bool:
    from ..kernel.changesets import service as changesets

    return changesets.locked(scope, entry=entry_id, path=rel)


@router.get("/graph")
def graph(project_id: str, request: Request, include_mentions: bool = True):
    root = deps.require_owner(project_id, request)
    data, by_name = _graph(root)
    scope = _scope(project_id)

    def node_id(name: str) -> str:
        node = by_name[name]
        return f"{node['kind']}:{name}"

    nodes = [
        {
            "id": f"{node['kind']}:{node['id']}", "kind": node["kind"], "key": node["id"], "label": node["label"],
            "description": node["description"], "status": "confirmed",
            "locked": _locked(scope, f"{node['kind']}:{node['id']}", node.get("path")), "slice_ids": [],
        }
        for node in data["nodes"]
    ]
    edges = []
    for index, edge in enumerate(data["edges"]):
        kind = _edge_kind(edge["kind"])
        if kind == "mention" and not include_mentions:
            continue
        edges.append({
            "id": f"e{index}", "source": node_id(edge["source"]), "target": node_id(edge["target"]),
            "kind": kind, "relation": edge["kind"], "label": edge.get("label") or "",
        })
    chains = {}
    for kind in ("rule", "metric"):
        members = {node["id"] for node in nodes if node["kind"] == kind}
        linked = {edge["source"] for edge in edges if edge["target"] in members}
        linked |= {edge["target"] for edge in edges if edge["source"] in members}
        chains[kind] = sorted(members | linked)
    return {"head": mainline.state(root)["head"], "nodes": nodes, "edges": edges, "clusters": [], "chains": chains}


@router.get("/entries")
def entries(
    project_id: str, request: Request, kind: str = "", q: str = "", status: str = "", sort: str = "name",
    cursor: str = "", limit: int = 0,
):
    root = deps.require_owner(project_id, request)
    data, _ = _graph(root)
    drafts = studio_service._load(project_id, "drafts")
    scope = _scope(project_id)
    rows = []
    for node in data["nodes"]:
        rel = node["path"]
        rows.append({
            "id": f"{node['kind']}:{node['id']}", "kind": node["kind"], "key": node["id"], "name": node["label"],
            "refs_out": node["out"], "refs_in": node["in"],
            "status": "editing" if rel in drafts else "confirmed",
            "needs_review": False, "locked": _locked(scope, f"{node['kind']}:{node['id']}", rel), "plugin": None,
        })
    counts = {"all": len(rows), **{kind_: sum(1 for row in rows if row["kind"] == kind_) for kind_ in KINDS}, "flowchart": None}
    kinds = set(filter(None, kind.split(",")))
    needle = q.strip().lower()
    statuses = set(filter(None, status.split(",")))
    items = [
        row for row in rows
        if (not kinds or row["kind"] in kinds)
        and (not needle or needle in row["name"].lower() or needle in row["key"].lower())
        and (not statuses or row["status"] in statuses)
    ]
    keys = {"name": lambda row: row["name"], "refs_in": lambda row: -row["refs_in"], "refs_out": lambda row: -row["refs_out"]}
    items.sort(key=keys.get(sort, keys["name"]))
    page = deps.page(items, cursor, limit, {"kind": kind, "q": needle, "status": status, "sort": sort})
    return {
        "items": page["items"], "next_cursor": page["next_cursor"], "counts": counts,
        "kinds": [{"kind": kind_, "label": KIND_LABELS[kind_], "plugin": None} for kind_ in KINDS],
    }


# ---- ST-04 条目详情、ST-05 操作流程 ----


def _draft_view(draft: dict | None) -> dict | None:
    if not draft:
        return None
    return {
        "author": draft.get("author", ""), "updated_at": deps.iso(draft.get("updated_at")),
        "base_revision": draft.get("base_revision", ""), "form": draft.get("form"), "content": draft.get("content", ""),
    }


def _data_source(text: str) -> dict:
    match = _DATA_SOURCE_RE.search(text)
    if not match:
        return {"kind": "unset", "value": None, "label": "未声明"}
    value = match.group(1).strip()
    if not value or value == "无":
        return {"kind": "none", "value": None, "label": "不落库"}
    if value.startswith("db:"):
        return {"kind": "anchor", "value": value, "label": value}
    return {"kind": "table", "value": value, "label": value}


@router.get("/entries/{entry_id}")
def entry_detail(project_id: str, entry_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    rel, kind, key = entry_path(root, entry_id)
    target = deps.workspaces._resolve_inside(root, rel)
    if not target.is_file():
        raise ApiError("not_found", "条目不存在", 404)
    element = studio_service.element(root, rel)
    data, by_name = _graph(root)
    node = by_name.get(key) or {}
    text = target.read_text(encoding="utf-8")
    declared, mentions = [], []
    for edge in data["edges"]:
        if edge["source"] != key or edge["target"] not in by_name:
            continue
        other = by_name[edge["target"]]
        ref = entry_ref(other["kind"], other["id"], other["label"])
        if _edge_kind(edge["kind"]) == "declared":
            declared.append({"target": ref, "label": edge.get("label") or ""})
        else:
            mentions.append({"target": ref, "where": "在正文中提到"})
    backlinks = [
        {"source": entry_ref(item["kind"], item["name"], item["label"]), "relation": "references", "locked": False}
        for item in element["backlinks"]
    ]
    fields = [
        {"name": item["label"], "column": item["key"], "type": item["type"], "primary_key": item["primaryKey"],
         "evidence": {"anchor": None, "label": "", "scheme": None, "confidence": None,
                      "expert_confirmed": False, "missing": True}}
        for item in node.get("fields") or []
    ]
    draft = studio_service.get_draft(project_id, rel)
    scope = _scope(project_id)
    backlinks = [{**item, "locked": _locked(scope, item["source"]["id"])} for item in backlinks]
    return {
        "id": f"{kind}:{key}", "kind": kind, "key": key, "name": element["label"], "path": rel,
        "description": node.get("description", ""),
        "status": "editing" if draft else "confirmed", "needs_review": None,
        "sections": [{"level": item["level"], "title": item["title"], "content_md": item["content"]} for item in element["sections"]],
        "fields": fields,
        "relations": {"declared": declared, "mentions": mentions},
        "backlinks": backlinks,
        "data_source": _data_source(text) if kind == "object" else {"kind": "unset", "value": None, "label": "不适用"},
        "evidence_snapshots": {},
        "counts": {"refs_out": node.get("out", 0), "refs_in": node.get("in", 0), "fields": len(fields), "evidence": 0},
        "revision": _content_revision(target.read_bytes()),
        "head": mainline.state(root)["head"],
        "lock": scope if _locked(scope, f"{kind}:{key}", rel) else None,
        "draft": _draft_view(draft),
    }


@router.get("/entries/{entry_id}/flow")
def entry_flow(project_id: str, entry_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    kind, _, key = entry_id.partition(":")
    if not key:  # 旧接口按名称定位（兼容期 /studio/elements/{名称}/flow）
        kind, key = "operation", entry_id
    if kind != "operation":
        raise ApiError("invalid_argument", "只有操作条目可以查看操作流程", 400)
    flow = studio_service.operation_flow(root, key)
    return {
        "operation": entry_ref("operation", key),
        "business_flow": {"steps": [], "main_operation": None, "sub_operations": 0, "referenced_by_scenarios": 0},
        "steps": [{"index": index + 1, "text_md": text} for index, text in enumerate(flow["steps"])],
        "diagrams": [],
    }


@router.get("/entries/{entry_id}/evidence")
def entry_evidence(project_id: str, entry_id: str, request: Request):
    """ST-06：条目正文里上传资料的出处（`file:` 锚点，同 EV-29 解析）；业务系统证据快照本期没有。"""
    from ..services import material_text

    root = deps.require_owner(project_id, request)
    rel, _, _ = entry_path(root, entry_id)
    target = deps.workspaces._resolve_inside(root, rel)
    if not target.is_file():
        raise ApiError("not_found", "条目不存在", 404)
    items: dict[str, dict] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.lstrip().startswith("|") else []
        for path, fragment, _ in material_text.find_anchors(line):
            anchor = material_text.file_anchor(path, fragment)
            item = items.setdefault(anchor, {"anchor": anchor, "scheme": "file", "title": path.rsplit("/", 1)[-1],
                                             "confidence": None, "used_for": []})
            # 属性表里的出处：这一行的第一格是属性名
            if cells and cells[0] and cells[0] not in item["used_for"]:
                item["used_for"].append(cells[0])
    return {"snapshot_basis": [],
            "items": [{**item, "resolved": material_text.resolve(root, item["anchor"])} for item in items.values()]}


# ---- ST-07 草稿、ST-08 提交 ----


@router.get("/entries/{entry_id}/draft")
def get_draft(project_id: str, entry_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    rel, _, _ = entry_path(root, entry_id)
    return {"draft": _draft_view(studio_service.get_draft(project_id, rel))}


@router.put("/entries/{entry_id}/draft")
async def save_draft(project_id: str, entry_id: str, request: Request):
    """自动暂存：原文 content，或结构化编辑器的表单 form（以当前原文为底只改表单里变了的部分，services/studio_form）。"""
    from ..services import studio_form

    root = deps.require_owner(project_id, request)
    rel, kind, key = entry_path(root, entry_id)
    body = await deps.json_body(request)
    content, form = body.get("content"), body.get("form")
    if form is not None:
        target = deps.workspaces._resolve_inside(root, rel)
        if not target.is_file():
            raise ApiError("not_found", "条目不存在", 404)
        try:
            base = studio_form.form_from_entry(entry_detail(project_id, entry_id, request))
            form = studio_form.normalize(form, base)
            content = studio_form.render(target.read_text(encoding="utf-8"), kind, key, base, form)
        except studio_form.FormError as exc:
            raise ApiError("invalid_argument", str(exc), 400, {"fields": {exc.field: exc.code}}) from exc
    elif not isinstance(content, str):
        raise ApiError("invalid_argument", "要提供草稿原文 content 或表单 form", 400,
                       {"fields": {"content": "required"}})
    studio_service.save_draft(project_id, rel, content, str(body.get("base_revision") or ""), deps.actor(request),
                              form=form if isinstance(form, dict) else None)
    draft = studio_service.get_draft(project_id, rel)
    return {"saved_at": deps.iso(draft["updated_at"]), "draft": _draft_view(draft)}


@router.delete("/entries/{entry_id}/draft", status_code=204)
def discard_draft(project_id: str, entry_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    rel, _, _ = entry_path(root, entry_id)
    studio_service.discard_draft(project_id, rel, deps.actor(request))
    return Response(status_code=204)


@router.post("/entries/{entry_id}/submit")
async def submit(project_id: str, entry_id: str, request: Request):
    """草稿提交分流：确定性检查通过直接落版本；超范围转助手复核；硬伤拒绝（三路都是 200）。"""
    from ..services.agent_sessions import manager

    root = deps.require_writable(project_id, request)
    rel, kind, key = entry_path(root, entry_id)
    body = await deps.json_body(request)
    user = deps.actor(request) or "本机用户"
    draft = studio_service.get_draft(project_id, rel)
    if not draft:
        raise ApiError("not_found", "没有待保存的草稿", 404)
    checks = studio_service.deterministic_check(root, rel, draft["content"])
    if checks["errors"]:
        return {
            "result": _count_check_failure("studio", "rejected"),
            "errors": [{"code": "check_rejected", "level": "error", "message": text, "path": rel} for text in checks["errors"]],
            "warnings": [{"code": "needs_review", "level": "warning", "message": text, "path": rel} for text in checks["review"]],
        }
    if checks["review"]:
        # 复核草稿放进助手的变更集副本（方案 C15）：放主线工作区助手看不到
        from ..kernel.changesets import service as changesets

        staging = await asyncio.to_thread(changesets.staging_dir, deps.workspaces, project_id)
        staged = studio_service.stage_for_review(staging, rel, draft["content"])
        points = "；".join(checks["review"])
        session = await manager.send(
            project_id,
            root,
            f"用户在 Studio 修改了 {rel}，改动超出确定性检查范围，需要你复核。"
            f"草稿全文在 {staged}。检查发现的疑点：{points}。"
            f"请对照现有文档评估草稿：合理则把草稿内容合并进正式文档并保存版本，"
            f"有问题则说明原因；缺失的关联文档如确有必要可一并创建。",
            username=deps.username(request),
        )
        return {
            "result": "needs_review",
            "review_points": checks["review"],
            "conversation_id": session.conversation_id,
            # 页面命令任务（S-19/S-20）属于以后阶段，复核结果在对话里
            "command_task": None,
        }
    saved = deps.workspaces.write_file(
        project_id, rel, draft["content"], base_revision=draft.get("base_revision", ""),
        author=deps.username(request), summary=str(body.get("summary") or "")[:80], source="studio",
    )
    studio_service.discard_draft(project_id, rel, user)
    state = mainline.state(root)
    return {
        "result": "committed",
        "entry": entry_detail(project_id, f"{kind}:{key}", request),
        "revision": saved["revision"],
        "commit": saved["commit"] or state["head"],
        "version_label": state["version_label"],
    }


# ---- ST-09 新建、ST-10 删除 ----

NEW_KINDS = ("object", "operation", "rule", "metric", "flowchart")
NAME_MAX = 80
# 条目名就是文件名、也是 [[链接]] 的目标：路径分隔、通配与链接语法里的字符都不行
_NAME_BAD = re.compile(r"[\\/:*?\"<>|#\[\]\x00-\x1f]")


def _template(root: Path, kind: str, from_template: bool) -> str:
    """新条目的正文：项目锁定的 core 版本的条目模板（装配目录 .claude/templates），取不到时用镜像里的。"""
    from .. import config

    name = f"{kind}-template.md"
    for candidate in (root / ".claude" / "templates" / name,
                      Path(config.ASSETS_DIR) / "workspace-toolkit" / "templates" / name):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            break
    else:
        text = ""
    if from_template:
        return text
    # 只要章节骨架：留下标题行
    return "".join(f"{line}\n\n" for line in text.splitlines() if re.match(r"^#{1,2}\s", line))


def _name_taken(root: Path, name: str, key: str) -> bool:
    data, _ = _graph(root)
    for node in data["nodes"]:
        if name in (node.get("id"), node.get("label")) or key in (node.get("id"), node.get("label")):
            return True
    return False


def _flowchart_titles(project_id: str) -> set[str]:
    from ..kernel.artifacts import service as artifact_service

    try:
        rows = artifact_service.list_rows(deps.workspaces, project_id, "diagram.flowchart", filters={},
                                          limit=500, page=deps.page, iso=deps.iso)["items"]
    except artifact_service.ArtifactError:
        return set()
    return {value for row in rows for value in (row.get("key"), row.get("title")) if value}


def _new_flowchart(project_id: str, name: str, key: str, actor: str) -> dict:
    from ..kernel.artifacts import service as artifact_service

    if name in _flowchart_titles(project_id) or (key and key in _flowchart_titles(project_id)):
        raise ApiError("revision_conflict", "已经有同名的流程图", 409, {"kind": "name", "target": name})
    try:
        result = artifact_service.create(deps.workspaces, project_id, "diagram.flowchart",
                                         {"key": key, "fields": {"title": name}} if key else {"fields": {"title": name}},
                                         actor=actor, iso=deps.iso)
    except artifact_service.ArtifactError as exc:
        if exc.code == "artifact_type_unknown":
            raise ApiError("pack_not_enabled", "图示插件没有启用", 409, {"pack": "diagram"}) from exc
        if (exc.details or {}).get("fields", {}).get("key") == "exists":
            raise ApiError("revision_conflict", "已经有同名的流程图", 409, {"kind": "name", "target": key}) from exc
        raise ApiError(exc.code, str(exc), exc.status, exc.details) from exc
    return result["artifact"]


@router.post("/entries")
async def create_entry(project_id: str, request: Request):
    """ST-09：按 core 的条目模板新建对象、操作、规则、指标（经写入路径保存为一个版本）；流程图建成图示插件的产物。

    key 缺省用名称（条目名就是文件名）；key 与名称不同时名称写进文档头 report_name。"""
    from fastapi.responses import JSONResponse

    from ..kernel.writepath import ChangeTx, Origin
    from ..services.document_metadata import name_frontmatter

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    kind = str(body.get("kind") or "")
    name = " ".join(str(body.get("name") or "").split())
    key = str(body.get("key") or "").strip()
    if kind not in NEW_KINDS:
        raise ApiError("invalid_argument", "kind 取值不合法", 400, {"fields": {"kind": "enum"}})
    if not name:
        raise ApiError("invalid_argument", "名称不能为空", 400, {"fields": {"name": "required"}})
    if len(name) > NAME_MAX or _NAME_BAD.search(name):
        raise ApiError("invalid_argument", "名称不合法", 400, {"fields": {"name": "pattern"}})
    if key and (len(key) > NAME_MAX or _NAME_BAD.search(key) or key.startswith(".")):
        raise ApiError("invalid_argument", "key 不合法", 400, {"fields": {"key": "pattern"}})
    actor = deps.username(request)
    if kind == "flowchart":
        artifact = await asyncio.to_thread(_new_flowchart, project_id, name, key, actor)
        return JSONResponse(status_code=201, content=artifact)
    key = key or name
    directory, suffix = KIND_PATHS[kind]
    rel = f"{directory}/{key}{suffix}"
    if _name_taken(root, name, key) or (root / rel).exists():
        raise ApiError("revision_conflict", "已经有同名的条目", 409, {"kind": "name", "target": name})
    text = name_frontmatter(name if name != key else None) + _template(root, kind, bool(body.get("from_template", True)))
    with ChangeTx(deps.workspaces, project_id, Origin("studio", actor), summary=f"新建 {rel}") as tx:
        if (root / rel).exists():  # 拿到项目锁之后再判一次：并发新建同名
            raise ApiError("revision_conflict", "已经有同名的条目", 409, {"kind": "name", "target": name})
        tx.write(rel, text)
        tx.commit(f"studio: 新建{KIND_LABELS.get(kind, '条目')}「{name}」 by {actor or '本机用户'}")
    return JSONResponse(status_code=201, content=entry_detail(project_id, f"{kind}:{key}", request))


@router.delete("/entries/{entry_id}")
async def delete_entry(project_id: str, entry_id: str, request: Request):
    """ST-10：删除条目，保存为一个版本（可回退）。被别的条目引用时 409 entry_referenced；
    流程图走图示插件的产物删除，引用它的操作只是提示「流程图缺失」，不挡删除。"""
    from ..kernel.writepath import ChangeTx, Origin

    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    base_revision = str(body.get("base_revision") or "")
    actor = deps.username(request)
    kind, _, key = (entry_id or "").partition(":")
    if kind == "flowchart":
        from ..kernel.artifacts import service as artifact_service

        try:
            return await asyncio.to_thread(artifact_service.delete, deps.workspaces, project_id, "diagram.flowchart",
                                           key, {"base_revision": base_revision}, actor=actor)
        except artifact_service.ArtifactError as exc:
            code = "pack_not_enabled" if exc.code == "artifact_type_unknown" else exc.code
            raise ApiError(code, str(exc), 409 if code == "pack_not_enabled" else exc.status,
                           {"pack": "diagram"} if code == "pack_not_enabled" else exc.details) from exc
    rel, kind, key = entry_path(root, entry_id)
    if not (root / rel).is_file():
        raise ApiError("not_found", "条目不存在", 404)
    referenced = [entry_ref(item["kind"], item["name"], item["label"])
                  for item in studio_service.element(root, rel)["backlinks"] if item["kind"] != "script"]
    if referenced:
        raise ApiError("entry_referenced", "还有条目引用它", 409, {"references": referenced})
    with ChangeTx(deps.workspaces, project_id, Origin("studio", actor), summary=f"删除 {rel}") as tx:
        tx.check_revision(rel, base_revision)
        tx.remove(rel)
        result = tx.commit(f"studio: 删除{KIND_LABELS.get(kind, '条目')}「{key}」 by {actor or '本机用户'}")
    studio_service.discard_draft(project_id, rel, actor or "本机用户")
    state = mainline.state(root)
    return {"commit": result.commit or state["head"], "version_label": state["version_label"]}
