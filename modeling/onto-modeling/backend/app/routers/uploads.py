"""暂存上传（API 契约 3.3）：U-01 上传、U-02 查看、U-03 删除。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from ..api import deps
from ..services import uploads

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])


@router.post("")
async def create_upload(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form(...),
    project_id: str = Form(""),
):
    """U-01：附件要带项目（所有权检查）；资料上传返回按表格结构识别出的用途。"""
    if purpose == "attachment":
        if not project_id:
            from ..api.errors import ApiError

            raise ApiError("invalid_argument", "对话附件要指定项目", 400, {"fields": {"project_id": "required"}})
        deps.require_owner(project_id, request)
    limit = uploads.PURPOSE_LIMITS.get(purpose, uploads.LIMITS["material_upload_bytes"])
    data = await file.read(limit + 1)
    meta = uploads.create(
        deps.actor(request), purpose, file.filename or "", data, file.content_type or "", project_id,
    )
    return JSONResponse(status_code=201, content=uploads.view(meta))


@router.get("/{upload_id}")
def get_upload(upload_id: str, request: Request):
    return uploads.view(uploads.get(upload_id, deps.actor(request)))


@router.delete("/{upload_id}", status_code=204)
def delete_upload(upload_id: str, request: Request):
    uploads.delete(upload_id, deps.actor(request))
    return Response(status_code=204)
