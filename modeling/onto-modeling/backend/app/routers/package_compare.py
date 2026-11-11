"""知识包版本对比（API 契约 3.20 PC 组，ADR-0095）：PC-01 上传对比输入、PC-02 取消上传、PC-03 删除输入、
PC-04 发起对比、PC-05 对比结果、PC-06 差异文件、PC-07 单文件差异、PC-08 删除对比。

只有项目所有者能用（1.4）。比较只读，不改项目内容，已归档项目也能比；比较数据按登录用户隔离。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..api import deps
from ..api.errors import ApiError
from ..services import package_compare

router = APIRouter(prefix="/api/v1/projects/{project_id}/package-comparisons", tags=["package-compare"])

_ID = r"^[A-Za-z0-9_-]+$"


class UploadSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["upload"]
    input_id: str = Field(min_length=1, max_length=128, pattern=_ID)


class ReleaseSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["release"]
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")


Source = Annotated[UploadSource | ReleaseSource, Field(discriminator="kind")]


class PackageCompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: Source
    right: Source
    allow_cross_package: bool = False
    request_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")


def _subject(project_id: str, request: Request) -> str:
    deps.require_owner(project_id, request)
    return deps.actor(request)


def _times(value: dict) -> dict:
    return {**value, **{key: deps.iso(value[key]) for key in ("created_at", "expires_at") if key in value}}


@router.post("/inputs", status_code=201)
def create_input(project_id: str, request: Request, file: UploadFile = File(...)):
    """PC-01：上传知识包 ZIP 作对比输入。只属于上传人，24 小时过期，不进工作区。"""
    subject = _subject(project_id, request)
    return _times(package_compare.create_input(
        subject, file.file, file.filename or "package.zip", project_id,
        request.headers.get("x-compare-upload-id", ""),
    ))


@router.delete("/uploads/{request_id}", status_code=204)
def cancel_upload(project_id: str, request_id: str, request: Request):
    """PC-02：取消还在上传、校验的输入；取消可以先于上传请求到达。"""
    package_compare.cancel_upload(_subject(project_id, request), request_id)
    return Response(status_code=204)


@router.delete("/inputs/{input_id}", status_code=204)
def delete_input(project_id: str, input_id: str, request: Request):
    """PC-03：删除对比输入；还有对比在用时 409。"""
    package_compare.delete_input(_subject(project_id, request), project_id, input_id)
    return Response(status_code=204)


@router.post("", status_code=202)
def start(project_id: str, body: PackageCompareBody, request: Request):
    """PC-04：发起对比，发布版本当场冻结，比较在后台跑；同一 request_id 重放返回同一任务。"""
    subject = _subject(project_id, request)
    value = package_compare.start_comparison(
        deps.workspaces, project_id, subject,
        body.left.model_dump(), body.right.model_dump(),
        body.allow_cross_package, body.request_id,
    )
    return JSONResponse(status_code=202, content=_times(value))


@router.get("/{comparison_id}")
def get_comparison(project_id: str, comparison_id: str, request: Request):
    """PC-05：任务状态；完成后带汇总与两侧来源。"""
    return _times(package_compare.get_comparison(_subject(project_id, request), project_id, comparison_id))


@router.get("/{comparison_id}/files")
def list_files(project_id: str, comparison_id: str, request: Request, status: str = "", category: str = "",
               q: str = "", cursor: str = "", limit: int = 0):
    """PC-06：逐文件差异，按状态（changed = 新增、删除、修改）、类别、路径或业务名称筛选。"""
    subject = _subject(project_id, request)
    if len(q) > 200:
        raise ApiError("invalid_argument", "搜索词过长", 400, {"fields": {"q": "max_length"}})
    listing = package_compare.list_files(subject, project_id, comparison_id, status, category, q, 0, None)
    binding = {"comparison_id": comparison_id, "status": status, "category": category, "q": q}
    return deps.page(listing["items"], cursor, limit, binding)


@router.get("/{comparison_id}/file-diff")
def file_diff(project_id: str, comparison_id: str, path: str, request: Request):
    """PC-07：一个文件两侧的正文；二进制、过大、行过长的只给大小和原因。"""
    return package_compare.file_diff(project_id, _subject(project_id, request), comparison_id, path)


@router.delete("/{comparison_id}", status_code=204)
def delete_comparison(project_id: str, comparison_id: str, request: Request):
    """PC-08：删除对比（还在跑的一并取消）。"""
    package_compare.remove(_subject(project_id, request), project_id, comparison_id)
    return Response(status_code=204)
