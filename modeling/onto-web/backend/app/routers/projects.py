from fastapi import APIRouter, File, Form, UploadFile
from pydantic import BaseModel

from .. import config
from ..services.workspace import WorkspaceManager

router = APIRouter(prefix="/api/projects", tags=["projects"])
workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)


class CreateProjectBody(BaseModel):
    name: str


@router.get("")
def list_projects():
    return {"projects": workspaces.list_projects()}


@router.post("")
def create_project(body: CreateProjectBody):
    return workspaces.create_project(body.name)


@router.get("/{project_id}")
def get_project(project_id: str):
    return workspaces.get_project(project_id)


@router.get("/{project_id}/files")
def file_tree(project_id: str):
    return {"tree": workspaces.file_tree(project_id)}


@router.get("/{project_id}/file")
def read_file(project_id: str, path: str):
    return workspaces.read_file(project_id, path, config.MAX_FILE_PREVIEW_BYTES)


@router.post("/{project_id}/resources")
async def upload_resource(
    project_id: str,
    subdir: str = Form(...),
    file: UploadFile = File(...),
):
    data = await file.read()
    return workspaces.save_resource(project_id, subdir, file.filename or "", data)


@router.get("/{project_id}/versions")
def versions(project_id: str):
    return {"versions": workspaces.versions(project_id)}
