import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config
from ..services.workspace import WorkspaceError, WorkspaceManager

router = APIRouter(prefix="/api/projects", tags=["projects"])
workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)

SEARCHABLE_SUFFIXES = (".md", ".txt", ".py", ".mmd", ".yaml", ".yml", ".csv")
MAX_SEARCH_HITS = 80


class CreateProjectBody(BaseModel):
    name: str
    template: str = "blank"  # blank | example


class RenameBody(BaseModel):
    name: str


class WriteFileBody(BaseModel):
    path: str
    content: str


class RevertBody(BaseModel):
    commit: str


def _request_user(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return (user or {}).get("sub", "")


def ensure_access(project_id: str, request: Request):
    """项目级访问守卫：校验存在性 + 所有权（鉴权关闭时放开）。"""
    root = workspaces.project_root(project_id)
    user = getattr(request.state, "user", None)
    if user is None:
        return root
    meta = workspaces.get_project(project_id)
    if not workspaces.can_access(meta, user.get("sub", ""), user.get("role") == "admin"):
        raise WorkspaceError("无权访问该任务", 403)
    return root


@router.get("")
def list_projects(request: Request):
    projects = workspaces.list_projects()
    user = getattr(request.state, "user", None)
    if user is not None:
        projects = workspaces.visible_to(
            projects, user.get("sub", ""), user.get("role") == "admin"
        )
    return {"projects": projects}


@router.post("")
def create_project(body: CreateProjectBody, request: Request):
    meta = workspaces.create_project(body.name, owner=_request_user(request))
    if body.template == "example":
        workspaces.seed_example(meta["id"])
    return meta


@router.post("/import")
async def import_project(
    request: Request, name: str = Form(...), file: UploadFile = File(...)
):
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return workspaces.import_zip(name, tmp_path, owner=_request_user(request))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.get_project(project_id)


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameBody, request: Request):
    ensure_access(project_id, request)
    return workspaces.rename_project(project_id, body.name)


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request):
    from ..services.agent_sessions import manager

    ensure_access(project_id, request)
    await manager.close(project_id)
    return workspaces.delete_project(project_id)


@router.get("/{project_id}/export")
def export_project(project_id: str, request: Request, scope: str = "ontology"):
    ensure_access(project_id, request)
    if scope not in ("ontology", "workspace"):
        raise WorkspaceError("scope 必须是 ontology 或 workspace")
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    workspaces.export_zip(project_id, scope, dest)
    return FileResponse(dest, filename=dest.name, media_type="application/zip")


@router.get("/{project_id}/files")
def file_tree(project_id: str, request: Request):
    ensure_access(project_id, request)
    return {"tree": workspaces.file_tree(project_id)}


@router.get("/{project_id}/file")
def read_file(project_id: str, path: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.read_file(project_id, path, config.MAX_FILE_PREVIEW_BYTES)


@router.put("/{project_id}/file")
def write_file(project_id: str, body: WriteFileBody, request: Request):
    ensure_access(project_id, request)
    return workspaces.write_file(project_id, body.path, body.content)


@router.get("/{project_id}/file-at")
def file_at_revision(project_id: str, rev: str, path: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.file_at_revision(project_id, rev, path)


@router.get("/{project_id}/raw")
def raw_file(project_id: str, path: str, request: Request):
    """原样返回文件（HTML 报告 iframe 预览等）。"""
    root = ensure_access(project_id, request)
    target = workspaces._resolve_inside(root, path)
    if not target.is_file():
        raise WorkspaceError("文件不存在", 404)
    return FileResponse(target)


@router.get("/{project_id}/search")
def search(project_id: str, q: str, request: Request):
    """产物全文检索（命中行，上限 80 条）。"""
    root = ensure_access(project_id, request)
    query = (q or "").strip()
    if not query:
        return {"hits": []}
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0].startswith(".") or path.suffix.lower() not in SEARCHABLE_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, 1):
            if query in line:
                hits.append(
                    {"path": str(rel), "line": line_no, "text": line.strip()[:160]}
                )
                if len(hits) >= MAX_SEARCH_HITS:
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


@router.get("/{project_id}/metrics")
def metrics(project_id: str, request: Request):
    from ..services import metrics as metrics_service

    root = ensure_access(project_id, request)
    return metrics_service.coverage(root)


@router.post("/{project_id}/resources")
async def upload_resource(
    project_id: str,
    request: Request,
    subdir: str = Form(...),
    file: UploadFile = File(...),
):
    ensure_access(project_id, request)
    data = await file.read()
    return workspaces.save_resource(project_id, subdir, file.filename or "", data)


@router.delete("/{project_id}/resources")
def delete_resource(project_id: str, path: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.delete_resource(project_id, path)


@router.get("/{project_id}/studio/graph")
def studio_graph(project_id: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    return studio.graph(root)


@router.get("/{project_id}/studio/element")
def studio_element(project_id: str, path: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    workspaces._resolve_inside(root, path)
    try:
        return studio.element(root, path)
    except FileNotFoundError:
        raise WorkspaceError("条目不存在", 404)


@router.get("/{project_id}/versions")
def versions(project_id: str, request: Request):
    ensure_access(project_id, request)
    return {"versions": workspaces.versions(project_id)}


@router.post("/{project_id}/versions/revert")
def revert(project_id: str, body: RevertBody, request: Request):
    ensure_access(project_id, request)
    return workspaces.revert_to(project_id, body.commit)
