"""知识包版本对比（API 契约 3.20 PC 组，ADR-0095）：PC-01 上传对比输入、PC-02 取消上传、PC-03 删除输入、
PC-04 发起对比、PC-05 对比结果、PC-06 差异文件、PC-07 单文件差异、PC-08 删除对比。

参数、分页与响应照 develop（routers/projects.py 的 package-comparisons/*），路径挂在 /api/v1 下。要统一账号登录
（Hub 身份，主体 = [签发方, sub]），再过项目所有者守卫；比较数据按 Hub 主体隔离。
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..api import deps
from ..services import package_compare, publish
from ..services.workspace import WorkspaceError
from .hub_packages import identity

router = APIRouter(prefix="/api/v1/projects/{project_id}/package-comparisons", tags=["package-compare"])

_ID = r"^[A-Za-z0-9_-]+$"


class PackageCompareUploadSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["upload"]
    input_id: str = Field(min_length=1, max_length=128, pattern=_ID)


class PackageCompareReleaseSource(BaseModel):
    """develop 的 Hub 引用 {ontology_id?/hub_package_id?, release_id}；本项目的版本也可以只写 version。"""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["release"]
    ontology_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_ID)
    hub_package_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_ID)
    release_id: str | None = Field(default=None, min_length=1, max_length=128, pattern=_ID)
    version: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")

    @model_validator(mode="after")
    def _one_reference(self):
        if (self.release_id is None) == (self.version is None):
            raise ValueError("release_id 与 version 二选一")
        if self.version is not None and (self.ontology_id or self.hub_package_id):
            raise ValueError("version 只指本项目的发布版本")
        return self


PackageCompareSource = Annotated[
    PackageCompareUploadSource | PackageCompareReleaseSource,
    Field(discriminator="kind"),
]


class PackageCompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: PackageCompareSource
    right: PackageCompareSource
    allow_cross_package: bool = False
    request_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")


def _compare_subject(project_id: str, request: Request) -> tuple[str, str]:
    token, subject, _ = identity(request)
    deps.require_owner(project_id, request)
    return token, subject


def _source(project_id: str, value: BaseModel) -> dict:
    """本项目版本号的简写换成 develop 的 Hub 引用：发布记录的标签里记着 Hub 包身份。"""
    ref = value.model_dump(exclude_none=True)
    if ref.get("kind") != "release" or "version" not in ref:
        return ref
    entry = next((item for item in publish.releases(deps.workspaces, project_id) if item["version"] == ref["version"]), None)
    if entry is None:
        raise WorkspaceError(f"版本 {ref['version']} 不存在", 404, "release_not_found")
    if not entry.get("hub"):
        raise WorkspaceError("发布版本缺少 Hub 包或 release 标识", 400)
    return {"kind": "release", "ontology_id": entry["hub"]["ontology_id"], "release_id": entry["hub"]["release_id"]}


@router.post("/inputs", status_code=201)
def package_compare_input(project_id: str, request: Request, file: UploadFile = File(...)):
    """PC-01：上传的包只属于当前登录主体，不进项目工作区。"""
    _token, subject = _compare_subject(project_id, request)
    request_id = request.headers.get("x-compare-upload-id", "")
    return package_compare.create_input(subject, file.file, file.filename or "package.zip", project_id, request_id)


@router.delete("/uploads/{request_id}")
def package_compare_upload_cancel(project_id: str, request_id: str, request: Request):
    """PC-02：取消还在上传、校验的输入；取消可以先于上传请求到达。"""
    _token, subject = _compare_subject(project_id, request)
    package_compare.cancel_upload(subject, request_id)
    return {"cancelled": True}


@router.delete("/inputs/{input_id}")
def package_compare_input_delete(project_id: str, input_id: str, request: Request):
    """PC-03"""
    _token, subject = _compare_subject(project_id, request)
    package_compare.delete_input(subject, project_id, input_id)
    return {"deleted": True}


@router.post("")
def package_compare_create(project_id: str, body: PackageCompareBody, request: Request):
    """PC-04：发布版本当场冻结，比较在后台跑；同一 request_id 重放先回 Hub 复核读权限再返回同一任务。"""
    token, subject = _compare_subject(project_id, request)
    value = package_compare.start_comparison(
        project_id, subject, token,
        _source(project_id, body.left), _source(project_id, body.right),
        body.allow_cross_package, body.request_id,
    )
    return JSONResponse(status_code=202, content=value)


@router.get("/{comparison_id}")
def package_compare_get(project_id: str, comparison_id: str, request: Request):
    """PC-05"""
    token, subject = _compare_subject(project_id, request)
    package_compare.ensure_sources_readable(subject, project_id, token, comparison_id)
    return package_compare.get_comparison(subject, project_id, comparison_id)


@router.get("/{comparison_id}/files")
def package_compare_files(
    project_id: str,
    comparison_id: str,
    request: Request,
    status: str = "",
    category: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = 50,
):
    """PC-06"""
    token, subject = _compare_subject(project_id, request)
    if offset < 0 or not 1 <= limit <= 200 or len(q) > 200:
        raise WorkspaceError("分页参数无效")
    package_compare.ensure_sources_readable(subject, project_id, token, comparison_id)
    return package_compare.list_files(subject, project_id, comparison_id, status, category, q, offset, limit)


@router.get("/{comparison_id}/file-diff")
def package_compare_file_diff(project_id: str, comparison_id: str, request: Request, file_id: str):
    """PC-07"""
    token, subject = _compare_subject(project_id, request)
    return package_compare.file_diff(project_id, subject, token, comparison_id, file_id)


@router.delete("/{comparison_id}")
def package_compare_delete(project_id: str, comparison_id: str, request: Request):
    """PC-08：还在跑的一并取消。"""
    _token, subject = _compare_subject(project_id, request)
    package_compare.remove(subject, project_id, comparison_id)
    return {"deleted": True}
