"""资料（API 契约 3.6）：R-01 列表、R-02 上传、R-03 改用途、R-04 删除。

资料区对 agent 只读，只能由用户上传、替换、删除，每次都保存版本。资料分析（R-05～R-10）、
外部反馈（R-11、R-12）属于以后阶段。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config
from ..api import deps
from ..api.errors import ApiError
from ..services import mainline, uploads
from ..services.workspace.layout import RESOURCE_DIRECTIVES, RESOURCE_SUBDIRS, _content_revision, is_auto_detected

router = APIRouter(prefix="/api/v1/projects/{project_id}/resources", tags=["resources"])

GROUP_LABELS = (("diagrams", "流程图"), ("excel", "表格"), ("others", "文档与其他"))
PURPOSES = (
    {"value": "goal", "label": "建模目标依据", "description": "说明要建什么、为谁用、做到什么程度"},
    {"value": "schema", "label": "数据 Schema", "description": "数据表结构，发布时对象按它绑定数据表"},
    {"value": "term", "label": "术语表", "description": "业务术语与同义词"},
    {"value": "evidence", "label": "通用业务依据", "description": "制度、流程、规则等业务原文"},
    {"value": "auto", "label": "由助手判断", "description": "上传时按内容识别，之后可以改"},
)
_KINDS = {
    ".doc": ("doc", "doc"), ".docx": ("doc", "doc"), ".pdf": ("pdf", "doc"),
    ".xls": ("xls", "xls"), ".xlsx": ("xls", "xls"), ".xlsm": ("xls", "xls"), ".csv": ("csv", "csv"),
    ".drawio": ("drawio", "drawio"), ".html": ("html", "html"), ".htm": ("html", "html"),
    ".png": ("image", None), ".jpg": ("image", None), ".jpeg": ("image", None),
    ".md": ("text", "text"), ".markdown": ("text", "text"), ".txt": ("text", "text"),
}
MAX_MATERIALS = 500


def _is_template(rel: str) -> bool:
    """资源模板（建项目时从 assets/resource-template 复制来的，如图示说明定义）。"""
    return (Path(config.ASSETS_DIR) / "resource-template" / rel.removeprefix("resource/")).is_file()


def _material(root: Path, rel: str, directives: dict) -> dict:
    path = root / rel
    kind, preview = _KINDS.get(path.suffix.lower(), ("text", None))
    directive = directives.get(rel)
    purpose = directive.get("purpose", "auto") if isinstance(directive, dict) else "auto"
    data = path.read_bytes()
    return {
        "path": rel,
        "name": path.name,
        "group": rel.split("/")[1],
        "kind": kind,
        "size": len(data),
        "uploaded_at": deps.iso(path.stat().st_mtime),
        "purpose": purpose,
        "purpose_auto": directive is None or is_auto_detected(directive) or purpose == "auto",
        "reading_mode": (directive or {}).get("reading_mode", "on_demand") if isinstance(directive, dict) else "on_demand",
        "is_revision": False,
        "revision_task": None,
        "is_template": _is_template(rel),
        "referenced": 0,
        "coverage": None,
        "preview": preview,
        "revision": _content_revision(data),
    }


def _material_paths(root: Path) -> list[str]:
    found = []
    for subdir in sorted(RESOURCE_SUBDIRS):
        folder = root / "resource" / subdir
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.name.startswith("."):
                found.append(path.relative_to(root).as_posix())
    return found[:MAX_MATERIALS]


@router.get("")
def list_resources(project_id: str, request: Request):
    root = deps.require_owner(project_id, request)
    directives = deps.workspaces.resource_directives(project_id)
    items = [_material(root, rel, directives) for rel in _material_paths(root)]
    groups = [
        {"key": key, "label": label, "items": [item for item in items if item["group"] == key]}
        for key, label in GROUP_LABELS
    ]
    return {"groups": groups, "feedback_group": {"enabled": False, "count": 0}, "purposes": list(PURPOSES)}


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
        "material": _material(root, saved["path"], directives),
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
    if not path.startswith("resource/") or path == RESOURCE_DIRECTIVES or path.startswith("resource/systems/"):
        raise ApiError("path_forbidden", "只能为资料区的资料设置用途", 403, {"path": path})
    if not (root / path).is_file():
        raise ApiError("file_not_found", "文件不存在", 404, {"path": path})
    deps.workspaces.set_resource_directive(
        project_id, path, str(body.get("purpose") or ""), str(body.get("reading_mode") or "on_demand"),
        author=deps.username(request),
    )
    state = mainline.state(root)
    return {
        "material": _material(root, path, deps.workspaces.resource_directives(project_id)),
        "commit": state["head"],
        "version_label": state["version_label"],
        "reread_task": None,
    }


@router.delete("")
def delete_resource(project_id: str, request: Request, path: str = ""):
    """删除保存为新版本，可从版本历史恢复。"""
    root = deps.require_writable(project_id, request)
    if path == RESOURCE_DIRECTIVES or path.startswith("resource/systems/") or not path.startswith("resource/"):
        raise ApiError("path_forbidden", "只能删除资料区的资料", 403, {"path": path})
    result = deps.workspaces.delete_resource(project_id, path, author=deps.username(request))
    state = mainline.state(root)
    return {
        "deleted": result["deleted"],
        "commit": result["commit"] or state["head"],
        "version_label": state["version_label"],
        "broken_references": 0,
    }
