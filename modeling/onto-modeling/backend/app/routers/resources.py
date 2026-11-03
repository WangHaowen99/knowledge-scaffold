"""资料（API 契约 3.6）：R-01 列表、R-02 上传、R-03 改用途、R-04 删除、R-05 被谁引用、R-06 原文预览、
R-11 / R-12 外部反馈；R-13 查看文本、R-14 重新转换（资料的文本版，ADR-0073）。

资料区对 agent 只读，只能由用户上传、替换、删除，每次都保存版本。资料分析（R-07～R-10）在 `materials.py`。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..services import mainline, material_conversion, material_refs, material_text, research_view, uploads, workspace_labels
from ..services.workspace.layout import (
    RESOURCE_DIRECTIVES, RESOURCE_SUBDIRS, _content_revision, is_auto_detected, is_resource_text,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}/resources", tags=["resources"])

GROUP_LABELS = (("diagrams", "流程图"), ("excel", "表格"), ("others", "文档与其他"))
# 用途的说明文字不下发（UI 文案口径 ⑤，contract-changes「UI 文案」第 9 条）
PURPOSES = (
    {"value": "goal", "label": "建模目标依据"},
    {"value": "schema", "label": "数据 Schema"},
    {"value": "term", "label": "术语表"},
    {"value": "evidence", "label": "通用业务依据"},
    {"value": "auto", "label": "由助手判断"},
)
MAX_MATERIALS = 500
# 查看文本一次最多给这么多字（全文在工作区里，助手读全文）
TEXT_VIEW_CHARS = 300_000


def _material(project_id: str, root: Path, rel: str, directives: dict, referenced: int = 0,
              coverage: dict | None = None) -> dict:
    path = root / rel
    kind, preview = research_view.kind_of(rel)
    directive = directives.get(rel)
    purpose = directive.get("purpose", "auto") if isinstance(directive, dict) else "auto"
    data = path.read_bytes()
    return {
        "path": rel,
        "name": path.name,
        "title": workspace_labels.material_name(rel),
        "type_label": workspace_labels.material_type(rel),
        "group": rel.split("/")[1],
        "kind": kind,
        "size": len(data),
        "uploaded_at": deps.iso(path.stat().st_mtime),
        "purpose": purpose,
        "purpose_auto": directive is None or is_auto_detected(directive) or purpose == "auto",
        "reading_mode": (directive or {}).get("reading_mode", "on_demand") if isinstance(directive, dict) else "on_demand",
        "is_revision": False,
        "revision_task": None,
        "is_template": research_view.is_template(rel),
        "referenced": referenced,
        "coverage": coverage,
        "preview": preview,
        "revision": _content_revision(data),
        "text": material_conversion.view(project_id, root, rel, data),
    }


def _material_paths(root: Path) -> list[str]:
    return research_view.material_paths(root)[:MAX_MATERIALS]


@router.get("")
def list_resources(project_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    directives = deps.workspaces.resource_directives(project_id)
    paths = _material_paths(root)
    referenced = material_refs.counts(root, paths)
    coverage = research_view.material_coverage(deps.workspaces, project_id)
    items = [_material(project_id, root, rel, directives, referenced.get(rel, 0), coverage.get(rel)) for rel in paths]
    groups = [
        {"key": key, "label": label, "items": [item for item in items if item["group"] == key]}
        for key, label in GROUP_LABELS
    ]
    from ..services import feedback_inbox

    feedback_on = research_view.pack_enabled(root, feedback_inbox.EVOLUTION_PACK)
    return {"groups": groups,
            "feedback_group": {"enabled": feedback_on, "count": feedback_inbox.open_count(root) if feedback_on else 0},
            "purposes": list(PURPOSES)}


@router.post("")
async def upload_resource(project_id: str, request: Request):
    """multipart（file）或 JSON（upload_id，先经 U-01 暂存）；同名文件直接替换。"""
    root = deps.require_writable(project_id, request)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise ApiError("invalid_argument", "要上传一个文件", 400, {"fields": {"file": "required"}})
        filename = uploads.check_filename(upload.filename or "")
        uploads.check_type(filename, "material")
        data = await upload.read(uploads.LIMITS["material_upload_bytes"] + 1)
        uploads.check_size(len(data), "material")
        fields, upload_id = dict(form), ""
    else:
        fields = await deps.json_body(request)
        upload_id = str(fields.get("upload_id") or "")
        meta = uploads.get(upload_id, deps.actor(request), "material")
        filename, data = meta["filename"], uploads.read(upload_id)
    subdir = str(fields.get("subdir") or "") or uploads.resource_subdir(filename)
    if subdir not in RESOURCE_SUBDIRS:
        raise ApiError("invalid_argument", f"资料分类必须是 {sorted(RESOURCE_SUBDIRS)} 之一", 400, {"fields": {"subdir": "enum"}})
    purpose = str(fields.get("purpose") or "auto")
    reading_mode = str(fields.get("reading_mode") or "on_demand")
    import asyncio

    # 大文件同步写盘会卡住事件循环（SSE 全体停摆），挪到线程执行
    saved = await asyncio.to_thread(
        deps.workspaces.save_resource, project_id, subdir, filename, data, purpose, reading_mode, deps.username(request),
    )
    if upload_id:
        uploads.consume(upload_id)
    state = mainline.state(root)
    directives = deps.workspaces.resource_directives(project_id)
    detected = saved.get("detected")
    return JSONResponse(status_code=201, content={
        "material": _material(project_id, root, saved["path"], directives,
                              material_refs.counts(root, [saved["path"]]).get(saved["path"], 0)),
        "replaced": saved["replaced"],
        "commit": saved["commit"] or state["head"],
        "version_label": state["version_label"],
        "detected": {"purpose": detected["purpose"], "tables": detected.get("tables", [])} if detected else None,
        # 「修订资料」整批发起增量更新要经页面命令（S-20），命令任务属于以后阶段
        "command_task": None,
    })


@router.patch("/purpose")
async def set_purpose(project_id: str, request: Request):
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    path = str(body.get("path") or "")
    if not path.startswith("resource/") or path == RESOURCE_DIRECTIVES or path.startswith("resource/systems/") \
            or is_resource_text(path):
        raise ApiError("path_forbidden", "只能为资料区的资料设置用途", 403, {"path": path})
    if not (root / path).is_file():
        raise ApiError("file_not_found", "文件不存在", 404, {"path": path})
    deps.workspaces.set_resource_directive(
        project_id, path, str(body.get("purpose") or ""), str(body.get("reading_mode") or "on_demand"),
        author=deps.username(request),
    )
    state = mainline.state(root)
    return {
        "material": _material(project_id, root, path, deps.workspaces.resource_directives(project_id),
                              material_refs.counts(root, [path]).get(path, 0)),
        "commit": state["head"],
        "version_label": state["version_label"],
        "reread_task": None,
    }


@router.delete("")
def delete_resource(project_id: str, request: Request, path: str = ""):
    """删除保存为新版本，可从版本历史恢复。"""
    root = deps.require_writable(project_id, request)
    if path == RESOURCE_DIRECTIVES or path.startswith("resource/systems/") or not path.startswith("resource/") \
            or is_resource_text(path):
        raise ApiError("path_forbidden", "只能删除资料区的资料", 403, {"path": path})
    # 引用它的出处删除后变成「来源无法核验」：删之前数一遍
    broken = material_refs.counts(root, [path]).get(path, 0) if material_text.is_material(path) else 0
    result = deps.workspaces.delete_resource(project_id, path, author=deps.username(request))
    state = mainline.state(root)
    return {
        "deleted": result["deleted"],
        "commit": result["commit"] or state["head"],
        "version_label": state["version_label"],
        "broken_references": broken,
    }


def _material_target(root: Path, path: str) -> str:
    rel = (path or "").strip().lstrip("/")
    if not rel:
        raise ApiError("invalid_argument", "要指定 path", 400, {"fields": {"path": "required"}})
    if "\x00" in rel:
        raise ApiError("invalid_argument", "path 不合法", 400, {"fields": {"path": "pattern"}})
    if not material_text.is_material(rel):
        raise ApiError("path_forbidden", "只能查看资料区的资料", 403, {"path": rel})
    target = (root / rel).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ApiError("file_not_found", "资料不存在", 404, {"path": rel})
    return rel


@router.get("/preview")
def preview(project_id: str, request: Request, path: str = "", around: str = "", limit: int = 0, cursor: str = ""):
    """R-06：文档类资料按 source_extract 的锚点抽成块；抽取器读不了的格式（pdf、doc）没有块。"""
    root = deps.require_owner(project_id, request)
    rel = _material_target(root, path)
    try:
        data = material_text.blocks(root, rel, around=around.strip(), limit=limit, cursor=cursor)
    except ValueError:
        raise ApiError("invalid_cursor", "游标无法解析", 400) from None
    return {
        "path": rel,
        "format": Path(rel).suffix.lower().lstrip("."),
        "coverage": research_view.coverage(deps.workspaces, project_id, rel),
        "blocks": data["blocks"],
        "next_cursor": data["next_cursor"],
    }


@router.get("/references")
def references(project_id: str, request: Request, path: str = ""):
    """R-05：本体条目、插件产物里引用这份资料的地方（出处路径或文件名）。"""
    root = deps.require_owner(project_id, request)
    return material_refs.references(root, _material_target(root, path))


def _document(root: Path, path: str) -> str:
    """R-13、R-14 的资料：资料分组里的原文件（不收文本版本身）。"""
    rel = _material_target(root, path)
    if is_resource_text(rel) or rel.split("/")[1] not in RESOURCE_SUBDIRS:
        raise ApiError("path_forbidden", "只能查看资料区的资料", 403, {"path": rel})
    return rel


@router.get("/text")
def text(project_id: str, request: Request, path: str = ""):
    """R-13 查看文本：资料的文本版（Markdown，位置标记是 HTML 注释），太长时截断；没转好时 markdown 为 null。"""
    root = deps.require_owner(project_id, request)
    rel = _document(root, path)
    state = material_conversion.view(project_id, root, rel)
    if state is None:
        raise ApiError("not_convertible", "这份资料没有文本版", 409, {"path": rel})
    markdown = (root / rel).read_text(encoding="utf-8", errors="replace") if state["state"] == "native" \
        else material_conversion.text_markdown(root, rel)
    truncated = bool(markdown) and len(markdown) > TEXT_VIEW_CHARS
    if truncated:
        cut = markdown.rfind("\n", 0, TEXT_VIEW_CHARS)
        markdown = markdown[: cut if cut > 0 else TEXT_VIEW_CHARS]
    return {"path": rel, "text": state, "markdown": markdown, "truncated": truncated}


@router.post("/convert")
async def reconvert(project_id: str, request: Request):
    """R-14 重新转换（也是失败后的重试）：用当前默认的转换器再转一次，后台执行，状态经 EP-25 推送。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    rel = _document(root, str(body.get("path") or ""))
    if material_conversion.kind_of(rel) != "document" or not material_conversion.reconvert(project_id, rel):
        raise ApiError("not_convertible", "这份资料不能转成文本", 409, {"path": rel})
    return JSONResponse(status_code=202, content={
        "material": _material(project_id, root, rel, deps.workspaces.resource_directives(project_id),
                              material_refs.counts(root, [rel]).get(rel, 0)),
    })
