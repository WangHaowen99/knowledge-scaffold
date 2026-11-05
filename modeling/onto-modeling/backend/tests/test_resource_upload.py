"""领域资料上传接口的大小边界与错误契约。"""

import asyncio
import io

import pytest
from fastapi import UploadFile

from app.routers import projects
from app.services.workspace import MAX_RESOURCE_FILE_SIZE, WorkspaceError


def upload(data: bytes, monkeypatch):
    captured = {}

    def save_resource(project_id, subdir, filename, content, purpose, reading_mode):
        captured.update(
            project_id=project_id,
            subdir=subdir,
            filename=filename,
            content=content,
            purpose=purpose,
            reading_mode=reading_mode,
        )
        return {"path": f"resource/{subdir}/{filename}", "size": len(content)}

    monkeypatch.setattr(projects, "ensure_access", lambda *args, **kwargs: None)
    monkeypatch.setattr(projects.workspaces, "save_resource", save_resource)
    file = UploadFile(filename="资料.pdf", file=io.BytesIO(data))
    result = asyncio.run(
        projects.upload_resource(
            "project-alpha",
            object(),
            subdir="others",
            file=file,
            purpose="auto",
            reading_mode="on_demand",
        )
    )
    return result, captured


def test_upload_resource_accepts_exact_size_limit(monkeypatch):
    result, captured = upload(b"x" * MAX_RESOURCE_FILE_SIZE, monkeypatch)
    assert result["size"] == MAX_RESOURCE_FILE_SIZE
    assert len(captured["content"]) == MAX_RESOURCE_FILE_SIZE


def test_upload_resource_rejects_size_limit_plus_one(monkeypatch):
    with pytest.raises(WorkspaceError) as error:
        upload(b"x" * (MAX_RESOURCE_FILE_SIZE + 1), monkeypatch)
    assert error.value.status == 413
    assert error.value.code == "resource_file_too_large"
