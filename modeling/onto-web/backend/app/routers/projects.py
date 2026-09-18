import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import config
from ..services.workspace import WorkspaceError, WorkspaceManager

router = APIRouter(prefix="/api/projects", tags=["projects"])
workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)


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


@router.get("")
def list_projects():
    return {"projects": workspaces.list_projects()}


@router.post("")
def create_project(body: CreateProjectBody):
    meta = workspaces.create_project(body.name)
    if body.template == "example":
        workspaces.seed_example(meta["id"])
    return meta


@router.post("/import")
async def import_project(name: str = Form(...), file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return workspaces.import_zip(name, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{project_id}")
def get_project(project_id: str):
    return workspaces.get_project(project_id)


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameBody):
    return workspaces.rename_project(project_id, body.name)


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    from ..services.agent_sessions import manager

    workspaces.project_root(project_id)
    await manager.close(project_id)
    return workspaces.delete_project(project_id)


@router.get("/{project_id}/export")
def export_project(project_id: str, scope: str = "ontology"):
    if scope not in ("ontology", "workspace"):
        raise WorkspaceError("scope 必须是 ontology 或 workspace")
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    workspaces.export_zip(project_id, scope, dest)
    return FileResponse(dest, filename=dest.name, media_type="application/zip")


@router.get("/{project_id}/files")
def file_tree(project_id: str):
    return {"tree": workspaces.file_tree(project_id)}


@router.get("/{project_id}/file")
def read_file(project_id: str, path: str):
    return workspaces.read_file(project_id, path, config.MAX_FILE_PREVIEW_BYTES)


@router.put("/{project_id}/file")
def write_file(project_id: str, body: WriteFileBody):
    return workspaces.write_file(project_id, body.path, body.content)


@router.get("/{project_id}/file-at")
def file_at_revision(project_id: str, rev: str, path: str):
    return workspaces.file_at_revision(project_id, rev, path)


@router.post("/{project_id}/resources")
async def upload_resource(
    project_id: str,
    subdir: str = Form(...),
    file: UploadFile = File(...),
):
    data = await file.read()
    return workspaces.save_resource(project_id, subdir, file.filename or "", data)


@router.delete("/{project_id}/resources")
def delete_resource(project_id: str, path: str):
    return workspaces.delete_resource(project_id, path)


@router.get("/{project_id}/versions")
def versions(project_id: str):
    return {"versions": workspaces.versions(project_id)}


@router.post("/{project_id}/versions/revert")
def revert(project_id: str, body: RevertBody):
    return workspaces.revert_to(project_id, body.commit)
