"""Studio 条目（API 契约 3.7）：ST-01 统计、ST-02 关系图、ST-03 条目表、ST-04 条目详情、ST-05 操作流程、
ST-07 草稿、ST-08 提交。条目按 entry_id（`<kind>:<key>`）定位；30 分钟租约取消，写入范围锁（C10）
之前 lock 恒为 null。证据侧栏（ST-06）、新建与删除条目（ST-09、ST-10）属于以后阶段。
"""

from __future__ import annotations

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


@router.get("/graph")
def graph(project_id: str, request: Request, include_mentions: bool = True):
    root = deps.require_owner(project_id, request)
    data, by_name = _graph(root)

    def node_id(name: str) -> str:
        node = by_name[name]
        return f"{node['kind']}:{name}"

    nodes = [
        {
            "id": f"{node['kind']}:{node['id']}", "kind": node["kind"], "key": node["id"], "label": node["label"],
            "description": node["description"], "status": "confirmed", "locked": False, "slice_ids": [],
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
    rows = []
    for node in data["nodes"]:
        rel = node["path"]
        rows.append({
            "id": f"{node['kind']}:{node['id']}", "kind": node["kind"], "key": node["id"], "name": node["label"],
            "refs_out": node["out"], "refs_in": node["in"],
            "status": "editing" if rel in drafts else "confirmed",
            "needs_review": False, "locked": False, "plugin": None,
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
        "base_revision": draft.get("base_revision", ""), "form": None, "content": draft.get("content", ""),
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
        "lock": None,
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


# ---- ST-07 草稿、ST-08 提交 ----


@router.get("/entries/{entry_id}/draft")
def get_draft(project_id: str, entry_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    rel, _, _ = entry_path(root, entry_id)
    return {"draft": _draft_view(studio_service.get_draft(project_id, rel))}


@router.put("/entries/{entry_id}/draft")
async def save_draft(project_id: str, entry_id: str, request: Request):
    """自动暂存：结构化编辑（form）由元模型渲染（C3，P1），现在只收原文 content。"""
    root = deps.require_owner(project_id, request)
    rel, _, _ = entry_path(root, entry_id)
    body = await deps.json_body(request)
    content = body.get("content")
    if not isinstance(content, str):
        raise ApiError("invalid_argument", "要提供草稿原文 content（结构化表单 form 还不支持）", 400,
                       {"fields": {"content": "required"}})
    studio_service.save_draft(project_id, rel, content, str(body.get("base_revision") or ""), deps.actor(request))
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
        raise ApiError("not_found", "没有待提交的草稿", 404)
    checks = studio_service.deterministic_check(root, rel, draft["content"])
    if checks["errors"]:
        return {
            "result": _count_check_failure("studio", "rejected"),
            "errors": [{"code": "check_rejected", "level": "error", "message": text, "path": rel} for text in checks["errors"]],
            "warnings": [{"code": "needs_review", "level": "warning", "message": text, "path": rel} for text in checks["review"]],
        }
    if checks["review"]:
        staged = studio_service.stage_for_review(root, rel, draft["content"])
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
