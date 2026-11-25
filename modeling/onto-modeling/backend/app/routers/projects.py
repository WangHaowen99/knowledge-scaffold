import asyncio
import json
import re
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..services import eval_data_sources
from ..services import knowledge_hub, package_archive, package_compare, package_evaluations
from ..services import solver_contract, version_revert
from ..services.workspace import (
    GOAL_PENDING,
    MAX_RESOURCE_FILE_SIZE,
    WorkspaceError,
    WorkspaceManager,
    remove_tree,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])
workspaces = WorkspaceManager(config.DATA_DIR, config.ASSETS_DIR)

SEARCHABLE_SUFFIXES = (".md", ".txt", ".py", ".mmd", ".yaml", ".yml", ".csv")
MAX_SEARCH_HITS = 80


class CreateProjectBody(BaseModel):
    name: str
    template: str = "blank"  # blank | example
    background: str = ""  # 一句话目标说明（项目卡片展示）


class RenameBody(BaseModel):
    name: str


class GoalBody(BaseModel):
    background: str | None = None  # 留空 = 按创建时的目标确认；给出 = 用户改写后的新目标


class CloneBody(BaseModel):
    name: str = ""  # 留空用默认的「原名-副本」；同名冲突后带选定的名称重新提交


class WriteFileBody(BaseModel):
    path: str
    content: str
    base_revision: str = ""  # 读取时拿到的 revision；不一致则 409 拒绝覆盖


class ObjectDataSourceBody(BaseModel):
    table: str = ""  # 现有数据表名；空串表示该对象不落库（「无」）
    base_revision: str = ""


class RevertBody(BaseModel):
    commit: str
    expected_head: str = ""


class TagBody(BaseModel):
    commit: str
    label: str


class DeleteProjectBody(BaseModel):
    confirmed: bool = False
    delete_hub_package: bool = False
    hub_usage_confirmed: bool = False


class MembersBody(BaseModel):
    members: dict[str, str]  # username -> editor(建模者) | viewer(只读评审)


class ArchiveBody(BaseModel):
    archived: bool


class SolverContractBody(BaseModel):
    """字段名沿用需求规格的接口定义：{ baseRevision, contract, summary }。"""

    model_config = ConfigDict(populate_by_name=True)

    base_revision: str = Field("", alias="baseRevision")  # 读取时拿到的修订；不一致则 409
    # {operations: [{binding, sidecars: {role: 原文}} 或 {binding, template: 模板参数}]}
    contract: dict = Field(default_factory=dict)
    summary: str = ""  # 写进版本点说明


def _request_user(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return (user or {}).get("sub", "")


def _hub_subject(request: Request) -> str | None:
    """登录用户的 Hub 主体（与 Hub 路由同一口径）；未登录或鉴权关闭时为 None。"""
    from ..services import auth
    user = getattr(request.state, "user", None)
    if not user or not user.get("sub"):
        return None
    return json.dumps([user.get("iss", auth.ISSUER), user["sub"]], ensure_ascii=False)


def _name_viewer(request: Request) -> tuple[str, bool] | None:
    """同名校验的查重范围：当前用户可见的任务；鉴权关闭时为 None（全部任务）。"""
    user = getattr(request.state, "user", None)
    if user is None:
        return None
    return user.get("sub", ""), user.get("role") == "admin"


def ensure_access(project_id: str, request: Request, write: bool = False):
    """项目级访问守卫：校验存在性 + 角色（鉴权关闭时放开）。

    write=True 时只读评审（viewer）被拒绝，覆盖文件写入与会话操作。
    """
    root = workspaces.project_root(project_id)
    user = getattr(request.state, "user", None)
    if user is None:
        return root
    meta = workspaces.get_project(project_id)
    username, is_admin = user.get("sub", ""), user.get("role") == "admin"
    if not workspaces.can_access(meta, username, is_admin):
        raise WorkspaceError("无权访问该任务", 403)
    if write and not workspaces.can_write(meta, username, is_admin):
        raise WorkspaceError("只读评审角色不能修改内容", 403)
    return root


def ensure_owner(project_id: str, request: Request):
    """项目管理操作（重命名/删除/归档/成员）：仅 owner 或管理员。"""
    root = workspaces.project_root(project_id)
    user = getattr(request.state, "user", None)
    if user is None:
        return root
    meta = workspaces.get_project(project_id)
    role = workspaces.role_of(meta, user.get("sub", ""), user.get("role") == "admin")
    if role not in ("admin", "owner") and meta.get("owner", ""):
        raise WorkspaceError("仅项目所有者或管理员可执行该操作", 403)
    if role is None:
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
    with workspaces.name_claim(body.name, _name_viewer(request)):
        meta = workspaces.create_project(
            body.name, owner=_request_user(request), background=body.background
        )
    if body.template == "example":
        workspaces.seed_example(meta["id"])
    return meta


@router.post("/import")
async def import_project(
    request: Request,
    name: str = Form(...),
    file: UploadFile = File(...),
    dry_run: bool = Form(False),
):
    """dry_run=true 时只返回压缩包摘要（导入向导的确认步骤），不落盘。

    预览与正式导入共用 inspect_archive 的同一份判别结果：已发布知识包按工作区布局
    转换，工作区内容原样铺开，不支持的格式在建项目之前就拒掉，不留空任务。
    任务名称与已有任务重名时返回 409 与候选名；预览阶段就校验，不等用户确认完内容。
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        summary = package_archive.inspect_archive(tmp_path)
        viewer = _name_viewer(request)
        if dry_run:
            # 有阻断项时导入本就不可用，先让用户看到阻断原因
            if not summary["blockers"]:
                workspaces.check_name_available(name, viewer, importing=True)
            return summary
        if summary["blockers"]:
            raise WorkspaceError(
                "；".join(summary["blockers"]), 400, "unsupported_archive_kind"
            )
        owner = _request_user(request)

        def claim_and_import() -> dict:
            # 查重与落盘在名称锁内串行；放进线程，等锁时不卡住事件循环
            with workspaces.name_claim(name, viewer, importing=True):
                if summary["kind"] == package_archive.KIND_KNOWLEDGE_PACKAGE:
                    return workspaces.import_package_zip(name, tmp_path, owner=owner)
                return workspaces.import_zip(name, tmp_path, owner=owner)

        meta = await asyncio.to_thread(claim_and_import)
        return meta
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/transfer-import")
async def transfer_import(
    request: Request,
    file: UploadFile = File(...),
    project_name: str = Form(""),
):
    """导入项目迁移包（导出的完整工作区 zip，含版本历史）。

    project_name 为空时沿用迁移包里的任务名；与已有任务重名时返回 409 与候选名，
    前端改名后带 project_name 重新提交。
    """
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        original = workspaces.transfer_archive_name(tmp_path)
        requested = project_name.strip()
        viewer, owner = _name_viewer(request), _request_user(request)

        def claim_and_import() -> dict:
            with workspaces.name_claim(
                requested or original,
                viewer,
                original_name=original,
                importing=True,
                check_length=bool(requested),  # 包里的原名不受长度上限约束（历史行为）
            ):
                return workspaces.import_transfer_zip(tmp_path, owner=owner, name=requested)

        meta = await asyncio.to_thread(claim_and_import)
        return meta
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.get_project(project_id)


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameBody, request: Request):
    """重命名任务。新名称与其他任务重名时返回 409 与候选名；名称没变则不校验。"""
    ensure_owner(project_id, request)
    meta = workspaces.get_project(project_id)
    if body.name.strip() == meta.get("name"):
        return meta
    with workspaces.name_claim(body.name, _name_viewer(request), exclude_id=project_id):
        return workspaces.rename_project(project_id, body.name)


@router.put("/{project_id}/goal")
def confirm_project_goal(project_id: str, body: GoalBody, request: Request):
    """开始建模前的目标确认卡：确认原目标或提交改写后的目标，之后才允许发起对话。"""
    ensure_access(project_id, request, write=True)
    return workspaces.confirm_goal(project_id, body.background)


def ensure_goal_confirmed(project_id: str) -> None:
    """创建时填了目标的项目，须先在确认卡上确认，助手才拿着确认后的目标开始建模。"""
    if workspaces.get_project(project_id).get("goal_status") == GOAL_PENDING:
        raise WorkspaceError("请先确认建模目标", 409, "goal_confirmation_required")


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request, background: BackgroundTasks, body: DeleteProjectBody = DeleteProjectBody()):
    """永久删除项目（不进回收站）。不看项目状态，二次确认由前端弹窗完成。

    选择同时删除 Hub 包时，先同步完成远端物理清理；失败保留项目。
    本地清理响应前执行：硬杀会话（含排队）、把工作区移出列表、清掉存着凭据的配置。
    目录递归删除要等磁盘，放到响应之后（_finish_delete）。
    """
    from ..services import studio
    from ..services.agent_sessions import manager

    ensure_owner(project_id, request)
    if body.delete_hub_package:
        if not body.confirmed or not body.hub_usage_confirmed:
            raise WorkspaceError("请确认消费和评测不再使用这些知识包", 422)
        from ..services import hub_members
        from .hub_packages import identity
        token, _, _ = identity(request)
        def delete_remote():
            with knowledge_hub.lock(project_id):
                hub_members.delete_package(workspaces, project_id, token)
        await asyncio.to_thread(delete_remote)
    await manager.discard(project_id)
    staged = await asyncio.to_thread(knowledge_hub.stage_delete, workspaces, project_id)
    await manager.discard(project_id)  # 移走期间并发发消息新建的会话一并清掉
    eval_data_sources.purge_project(project_id)
    studio.purge_project(project_id)
    background.add_task(_finish_delete, project_id, staged)
    return {"deleted": project_id}


async def _finish_delete(project_id: str, staged: Path) -> None:
    """删除收尾（响应之后执行）。各项互不依赖，并发进行，一项失败不拖累其余；
    没清干净的记入运维错误日志（工作区目录的残留由下次启动清扫兜底）。"""
    from ..services import error_log
    from ..services.engines import jiuwen

    jobs = {
        "工作区目录": asyncio.to_thread(workspaces.purge_staged, staged),
        "引擎工件区": asyncio.to_thread(remove_tree, jiuwen.shadow_dir(project_id)),
    }
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    for label, result in zip(jobs, results):
        problem = _cleanup_problem(result)
        if problem:
            error_log.log(project_id, "project_delete", f"{label}未清理完成：{problem}")


def _cleanup_problem(result) -> str:
    """收尾结果 → 未完成的原因（空串表示已完成）。"""
    if isinstance(result, BaseException):
        return str(result) or type(result).__name__
    return "仍有文件被占用" if result is False else ""


@router.post("/{project_id}/archive")
def archive_project(project_id: str, body: ArchiveBody, request: Request):
    ensure_owner(project_id, request)
    return workspaces.set_archived(project_id, body.archived)


@router.post("/{project_id}/clone")
def clone_project(project_id: str, request: Request, body: CloneBody | None = None):
    """克隆任务，默认命名「原名-副本」；与已有任务重名时返回 409 与候选名，
    前端改名后带 name 重新提交。"""
    ensure_access(project_id, request)
    default_name = workspaces.clone_name(project_id)
    name = (body.name.strip() if body else "") or default_name
    with workspaces.name_claim(name, _name_viewer(request), original_name=default_name):
        meta = workspaces.clone_project(project_id, owner=_request_user(request), name=name)
    return meta


@router.get("/{project_id}/hub-delete-preview")
def hub_delete_preview(project_id: str, request: Request):
    from ..services import hub_members
    ensure_owner(project_id, request)
    with knowledge_hub.lock(project_id):
        return hub_members.delete_preview(workspaces, project_id, knowledge_hub.token_of(request))


@router.get("/{project_id}/members")
def get_members(project_id: str, request: Request):
    from ..services import hub_members
    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    return {"owner": meta.get("owner", ""), "members": meta.get("members") or {},
            "hub_sync": hub_members.status(project_id)}


@router.post("/{project_id}/members/hub-retry")
def retry_member_sharing(project_id: str, request: Request, username: str | None = None):
    from ..services import hub_members
    ensure_owner(project_id, request)
    with knowledge_hub.lock(project_id):
        return hub_members.sync(workspaces, project_id, knowledge_hub.token_of(request), username=username)


@router.put("/{project_id}/members")
def put_members(project_id: str, body: MembersBody, request: Request):
    from ..services import hub_members, hub_packages
    ensure_owner(project_id, request)
    with knowledge_hub.lock(project_id):
        hub_members.writable(project_id)
        previous = workspaces.get_project(project_id)
        meta = workspaces.set_members(project_id, body.members)
        if hub_packages.association(project_id):
            hub_members.queue(project_id, previous, meta)
        sharing = hub_members.sync(workspaces, project_id, knowledge_hub.token_of(request))
        return {"owner": meta.get("owner", ""), "members": meta.get("members") or {}, "hub_sync": sharing}


@router.get("/{project_id}/export")
def export_project(
    project_id: str, request: Request, scope: str = "ontology", allow_unbound: bool = False
):
    ensure_access(project_id, request)
    if scope not in ("ontology", "workspace", "package"):
        raise WorkspaceError("scope 必须是 ontology、workspace 或 package")
    if scope == "package":
        raise WorkspaceError('请先发布，再从已发布版本列表下载指定版本', 400)
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    workspaces.export_zip(project_id, scope, dest)
    import shutil
    from starlette.background import BackgroundTask
    return FileResponse(dest, filename=dest.name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, dest.parent))


class ReleaseBody(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: str = Field(min_length=1, max_length=128)
    allow_unbound: bool = False
    # 启用了求解契约的知识包须确认求解能力尚未经端到端验证；验证在发布后的独立评测中进行
    solver_unverified_acknowledged: bool = False


class PackageCompareUploadSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["upload"]
    input_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class PackageCompareReleaseSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["release"]
    ontology_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
    )
    hub_package_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$",
    )
    release_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


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


def _compare_subject(request: Request) -> tuple[str, str]:
    from .hub_packages import identity
    token, subject, _ = identity(request)
    return token, subject


@router.post("/{project_id}/package-comparisons/inputs", status_code=201)
def package_compare_input(project_id: str, request: Request, file: UploadFile = File(...)):
    token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    # The archive is isolated from the project and remains private to this login subject.
    request_id = request.headers.get("x-compare-upload-id", "")
    return package_compare.create_input(
        subject, file.file, file.filename or "package.zip", project_id, request_id,
    )


@router.delete("/{project_id}/package-comparisons/uploads/{request_id}")
def package_compare_upload_cancel(project_id: str, request_id: str, request: Request):
    _token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    package_compare.cancel_upload(subject, request_id)
    return {"cancelled": True}


@router.delete("/{project_id}/package-comparisons/inputs/{input_id}")
def package_compare_input_delete(project_id: str, input_id: str, request: Request):
    _token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    package_compare.delete_input(subject, project_id, input_id)
    return {"deleted": True}


@router.post("/{project_id}/package-comparisons", status_code=202)
def package_compare_create(project_id: str, body: PackageCompareBody, request: Request):
    token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    return package_compare.start_comparison(
        project_id, subject, token,
        body.left.model_dump(exclude_none=True),
        body.right.model_dump(exclude_none=True),
        body.allow_cross_package, body.request_id,
    )


@router.get("/{project_id}/package-comparisons/{comparison_id}")
def package_compare_get(project_id: str, comparison_id: str, request: Request):
    token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    package_compare.ensure_sources_readable(subject, project_id, token, comparison_id)
    return package_compare.get_comparison(subject, project_id, comparison_id)


@router.get("/{project_id}/package-comparisons/{comparison_id}/files")
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
    token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    if offset < 0 or not 1 <= limit <= 200 or len(q) > 200:
        raise WorkspaceError("分页参数无效")
    package_compare.ensure_sources_readable(subject, project_id, token, comparison_id)
    return package_compare.list_files(
        subject, project_id, comparison_id, status, category, q, offset, limit,
    )


@router.get("/{project_id}/package-comparisons/{comparison_id}/file-diff")
def package_compare_file_diff(project_id: str, comparison_id: str, request: Request, file_id: str):
    token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    return package_compare.file_diff(project_id, subject, token, comparison_id, file_id)


@router.delete("/{project_id}/package-comparisons/{comparison_id}")
def package_compare_delete(project_id: str, comparison_id: str, request: Request):
    _token, subject = _compare_subject(request)
    ensure_access(project_id, request)
    package_compare._owned_result(subject, project_id, comparison_id)
    package_compare.delete(subject, comparison_id)
    return {"deleted": True}


@router.post("/{project_id}/package/releases")
def publish_release(project_id: str, body: ReleaseBody, request: Request):
    from .hub_packages import identity
    from ..services import hub_packages
    from ..services import package_evaluations
    token, subject, _ = identity(request)
    ensure_access(project_id, request, write=True)
    check = workspaces.solver_check(project_id)
    verified_revision = ""
    if check.needs_validation and not body.solver_unverified_acknowledged:
        # 当前修订已在早先版本通过求解验证时不必再确认；发布冻结时再核对修订没有变化
        if not package_evaluations.verified_solver_release(project_id, subject, check.revision):
            raise WorkspaceError(
                "求解能力尚未经端到端验证：请在发布检查中确认后再发布，发布后在「已发布版本（知识包评测）」中发起求解验证",
                409, "solver_validation_acknowledgement_required",
            )
        verified_revision = check.revision
    receipt = hub_packages.publish(
        workspaces, project_id, body.request_id, token, subject, body.allow_unbound,
        acknowledged_by=_request_user(request) if body.solver_unverified_acknowledged else "",
        verified_revision=verified_revision,
    )
    # 评测优化之后发布的新版本，随即告诉评测服务；失败时下次打开已发布版本再补，不影响发布结果
    writeback = package_evaluations.after_publish(project_id, subject, token, receipt)
    return {**receipt, 'evaluation_writeback': writeback} if writeback else receipt


@router.get("/{project_id}/package/releases")
def package_releases(project_id: str, request: Request, offset: int = 0, limit: int = 20):
    from .hub_packages import identity
    from ..services import hub_packages
    token, subject, _ = identity(request)
    ensure_access(project_id, request)
    if offset < 0 or not 1 <= limit <= 100:
        raise WorkspaceError('分页参数无效')
    return hub_packages.releases(project_id, subject, token, offset, limit)


@router.get("/{project_id}/package/releases/{release_id}/download")
def download_release(project_id: str, release_id: str, request: Request):
    from .hub_packages import identity, download
    from ..services import hub_packages
    _, subject, _ = identity(request)
    ensure_access(project_id, request)
    linked = hub_packages.association(project_id, subject)
    if not linked:
        raise WorkspaceError('尚未关联个人 Hub 包', 404)
    return download(linked['ontology_id'], release_id, request)


@router.get("/{project_id}/package/resources/status")
def hub_resource_status(project_id: str, request: Request):
    ensure_access(project_id, request)
    return knowledge_hub.resource_status(project_id, workspaces)


class SharedResourcesBody(BaseModel):
    model_config = {"extra": "forbid"}
    paths: list[str] = Field(max_length=10000)
    confirm_shared: bool


@router.put("/{project_id}/package/resources")
def share_hub_resources(project_id: str, body: SharedResourcesBody, request: Request):
    ensure_access(project_id, request, write=True)
    if not body.confirm_shared:
        raise WorkspaceError('请确认所选资料可向全部 Hub 授权用户共享', 422)
    return knowledge_hub.sync_resources(workspaces, project_id, knowledge_hub.token_of(request), body.paths)


@router.get("/{project_id}/package/resources")
def hub_resources(project_id: str, request: Request, offset: int = 0, limit: int = 20):
    ensure_access(project_id, request)
    return knowledge_hub.call('GET', f'/api/packages/{hub_ontology_id(project_id)}/resources/current?offset={offset}&limit={limit}',
                              knowledge_hub.token_of(request))


@router.get("/{project_id}/package/resources/{file_id}/download")
def hub_resource_download(project_id: str, file_id: str, request: Request):
    import shutil
    from starlette.background import BackgroundTask
    ensure_access(project_id, request)
    knowledge_hub.identifier(file_id)
    directory = Path(tempfile.mkdtemp(prefix='onto-hub-resource-'))
    dest = directory / 'resource'
    try:
        knowledge_hub.call('GET', f'/api/packages/{hub_ontology_id(project_id)}/resources/current/{file_id}',
                           knowledge_hub.token_of(request), download=dest)
    except Exception:
        shutil.rmtree(directory)
        raise
    return FileResponse(dest, filename=file_id, background=BackgroundTask(shutil.rmtree, directory))


@router.get("/{project_id}/package/check")
def package_check(project_id: str, request: Request):
    """发布前数据绑定体检：阻断项、无从核对项、提示，以及修复所需的表清单与文档版本。"""
    ensure_access(project_id, request)
    check = workspaces.package_binding_check(project_id)
    solver = check.get("solver") or {}
    if solver.get("requires_validation_after_publish"):
        verified = package_evaluations.verified_solver_release(project_id, _hub_subject(request), solver.get("revision"))
        if verified:
            check["solver"] = {**solver, "requires_validation_after_publish": False, "verified_release": verified}
    return check


@router.get("/{project_id}/package/solver-contract")
def get_solver_contract(project_id: str, request: Request):
    """求解模型面板：当前修订、契约原文、静态检查与验证门禁。"""
    ensure_access(project_id, request)
    return workspaces.solver_contract_view(project_id)


@router.put("/{project_id}/package/solver-contract")
def save_solver_contract(project_id: str, body: SolverContractBody, request: Request):
    """整体保存求解契约；结构问题 400 并逐条返回 findings，修订冲突 409。"""
    ensure_access(project_id, request, write=True)
    try:
        operations = body.contract.get("operations")
        if not isinstance(operations, list):
            raise WorkspaceError("contract.operations 必须是列表", code=solver_contract.CODE_INVALID)
        return workspaces.save_solver_contract(
            project_id, operations, body.base_revision,
            author=_request_user(request), summary=body.summary,
        )
    except solver_contract.SolverContractRejected as exc:
        findings = [solver_contract.finding(problem) for problem in exc.problems]
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "code": findings[0]["code"], "findings": findings},
        )


@router.post("/{project_id}/package/solver-contract/check")
def check_solver_contract(project_id: str, body: SolverContractBody, request: Request):
    """即时检查面板草稿（不保存）：rejected 为保存会被拒的问题，findings 为发布会被拦截的问题，
    rendered 为模板契约将生成的 sidecar 原文。"""
    ensure_access(project_id, request)
    operations = body.contract.get("operations")
    if not isinstance(operations, list):
        raise WorkspaceError("contract.operations 必须是列表", code=solver_contract.CODE_INVALID)
    return workspaces.check_solver_contract_draft(project_id, operations)


@router.get("/{project_id}/package/solver-validations/cases")
def get_solver_validation_cases(project_id: str, request: Request):
    """只读导出建模侧原有的验证用例，供导入独立评测的求解验证集。"""
    from ..services import solver_validation

    root = ensure_access(project_id, request)
    return {"cases": solver_validation.list_cases(root)}


@router.get("/{project_id}/package/solver-validations/latest")
def latest_solver_validation(project_id: str, request: Request):
    """当前修订最后一次验证：状态、断言、候选数、权威结果摘要、错误码与诊断引用。"""
    from ..services import solver_validation

    root = ensure_access(project_id, request)
    revision = workspaces.solver_check(project_id).revision
    validation = solver_validation.latest(root, revision) if revision else None
    return {"revision": revision, "validation": validation}


@router.post("/{project_id}/objects/{key}/data-source")
def set_object_data_source(project_id: str, key: str, body: ObjectDataSourceBody, request: Request):
    """发布前修复：把对象「# 数据来源」改成现有数据表之一或「无」（Web 保存同款版本点）。"""
    ensure_access(project_id, request, write=True)
    return workspaces.set_object_data_source(
        project_id, key, body.table,
        base_revision=body.base_revision, author=_request_user(request),
    )


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
    ensure_access(project_id, request, write=True)
    return workspaces.write_file(
        project_id,
        body.path,
        body.content,
        base_revision=body.base_revision,
        author=_request_user(request),
    )


@router.get("/{project_id}/excel")
def excel_preview(project_id: str, path: str, request: Request):
    from ..services import excel_preview as excel_service

    root = ensure_access(project_id, request)
    target = workspaces._resolve_inside(root, path)
    if not target.is_file():
        raise WorkspaceError("文件不存在", 404)
    if target.suffix.lower() not in (".xlsx", ".xlsm"):
        raise WorkspaceError("仅支持 xlsx/xlsm 预览；xls 请转存后上传", 415)
    try:
        return excel_service.preview(target)
    except excel_service.ExcelPreviewError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.get("/{project_id}/source-preview")
def source_preview(project_id: str, path: str, request: Request, anchor: str = ""):
    """原始资料分段预览（文本、docx），返回锚点附近的分段与是否命中。"""
    from ..services import source_preview as source_service

    root = ensure_access(project_id, request)
    target = workspaces._resolve_inside(root, path)
    if not target.is_file():
        raise WorkspaceError("文件不存在", 404)
    try:
        return source_service.preview(target, anchor)
    except source_service.SourcePreviewError as exc:
        raise WorkspaceError(str(exc), exc.status)


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
    background: BackgroundTasks,
    subdir: str = Form(...),
    file: UploadFile = File(...),
    purpose: str = Form("auto"),
    reading_mode: str = Form("on_demand"),
):
    ensure_access(project_id, request, write=True)
    # 多读一个字节即可判断越界，避免把任意大的请求文件完整加载进应用内存。
    data = await file.read(MAX_RESOURCE_FILE_SIZE + 1)
    if len(data) > MAX_RESOURCE_FILE_SIZE:
        filename = file.filename or ""
        raise WorkspaceError(
            f"文件“{filename}”超过 20 MB，无法上传",
            413,
            "resource_file_too_large",
        )
    # 大文件同步写盘会卡住事件循环（SSE 全体停摆），挪到线程执行
    result = await asyncio.to_thread(
        workspaces.save_resource,
        project_id,
        subdir,
        file.filename or "",
        data,
        purpose,
        reading_mode,
        _request_user(request),
    )
    return result


@router.get("/{project_id}/resource-meta")
def resource_meta(project_id: str, request: Request):
    ensure_access(project_id, request)
    return {
        "directives": workspaces.resource_directives(project_id),
        "purposes": list(workspaces.MATERIAL_PURPOSES),
        "schema_candidates": workspaces.schema_candidates(project_id),
        "schema_scopes": workspaces.schema_scopes(project_id),
    }


class SchemaPreviewBody(BaseModel):
    path: str
    force: bool = False


@router.post("/{project_id}/resource-meta/schema-preview")
def preview_resource_schema(project_id: str, body: SchemaPreviewBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.preview_schema(project_id, body.path, force=body.force)


class SchemaSelectionBody(BaseModel):
    mode: Literal["auto", "manual"]
    included_sheets: list[str]
    file_sha256: str


class ResourceDirectiveBody(BaseModel):
    path: str
    purpose: str
    reading_mode: str = "on_demand"
    schema_selection: SchemaSelectionBody | None = None


@router.patch("/{project_id}/resource-meta")
def set_resource_meta(project_id: str, body: ResourceDirectiveBody, request: Request, background: BackgroundTasks):
    ensure_access(project_id, request, write=True)
    result = workspaces.set_resource_directive(
        project_id, body.path, body.purpose, body.reading_mode,
        body.schema_selection.model_dump() if body.schema_selection is not None else None,
        author=_request_user(request),
    )
    return result


@router.delete("/{project_id}/resources")
def delete_resource(project_id: str, path: str, request: Request, background: BackgroundTasks):
    ensure_access(project_id, request, write=True)
    result = workspaces.delete_resource(
        project_id, path, author=_request_user(request)
    )
    return result


class ProjectMcpBody(BaseModel):
    disabled: list[str]  # 本项目停用的全局 MCP 服务器名


@router.get("/{project_id}/mcp")
def get_project_mcp(project_id: str, request: Request):
    from ..services import mcp_settings

    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    disabled = set(meta.get("mcp_disabled") or [])
    servers = [
        {
            "name": s["name"],
            "transport": s["transport"],
            "enabled_globally": bool(s.get("enabled", True)),
            "enabled": s.get("enabled", True) and s["name"] not in disabled,
        }
        for s in mcp_settings.load()
    ]
    return {"servers": servers, "disabled": sorted(disabled)}


@router.put("/{project_id}/mcp")
def put_project_mcp(project_id: str, body: ProjectMcpBody, request: Request):
    ensure_access(project_id, request, write=True)
    names = [str(n)[:32] for n in body.disabled][:64]
    workspaces._update_meta(project_id, mcp_disabled=names)
    return {"disabled": names}


class ProjectEngineBody(BaseModel):
    engine: str  # claude | jiuwen


@router.get("/{project_id}/engine")
def get_project_engine(project_id: str, request: Request):
    from ..services.engines import available_engines, get_engine_class

    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    current = meta.get("engine") or "claude"
    return {
        "engine": get_engine_class(current).name,
        "available": available_engines(),
    }


@router.put("/{project_id}/engine")
async def put_project_engine(project_id: str, body: ProjectEngineBody, request: Request):
    """切换会话引擎（灰度开关）。切换即终止当前会话：界面历史保留可回放，
    下一条消息用新引擎启动（Jiuwen 经历史回放续接上下文，Claude 全新会话）。"""
    from ..services.agent_sessions import manager
    from ..services.engines import ENGINE_NAMES, get_engine_class

    ensure_access(project_id, request, write=True)
    name = (body.engine or "").strip()
    if name not in ENGINE_NAMES:
        return JSONResponse(status_code=400, content={"error": "未知引擎"})
    engine_cls = get_engine_class(name)
    if not engine_cls.available():
        return JSONResponse(
            status_code=400,
            content={"error": getattr(engine_cls, "UNAVAILABLE_MESSAGE", "引擎不可用")},
        )
    workspaces._update_meta(project_id, engine=name)
    # 全部会话的续接点一并作废：换引擎期间产生的轮次不在旧引擎的 transcript 里
    await manager.close(project_id, all_conversations=True)
    return {"engine": name}


@router.get("/{project_id}/studio/graph")
def studio_graph(project_id: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    return studio.graph(root)


@router.get("/{project_id}/studio/updates")
def studio_updates(project_id: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    return {"updates": studio.updates(root)}


@router.get("/{project_id}/studio/elements/{element_id}/flow")
def studio_operation_flow(project_id: str, element_id: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    try:
        return studio.operation_flow(root, element_id)
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.get("/{project_id}/studio/element")
def studio_element(project_id: str, path: str, request: Request):
    from ..services import studio
    from ..services.workspace import _content_revision

    root = ensure_access(project_id, request)
    target = workspaces._resolve_inside(root, path)
    with workspaces._project_lock(project_id):
        try:
            data = studio.element(root, path)
            current_content = target.read_bytes()
        except FileNotFoundError:
            raise WorkspaceError("条目不存在", 404)
        data["revision"] = _content_revision(current_content)
        draft = studio.reconcile_draft(
            project_id, root, path, current_content, data["revision"]
        )
        data["lease"] = studio.lease_status(project_id, path)
    data["draft"] = (
        {
            "updated_at": draft["updated_at"],
            "author": draft["author"],
            "state": draft["state"],
        }
        if draft
        else None
    )
    return data


class StudioLeaseBody(BaseModel):
    path: str


class StudioDraftBody(BaseModel):
    path: str
    content: str
    base_revision: str = ""


@router.post("/{project_id}/studio/lease")
def studio_acquire_lease(project_id: str, body: StudioLeaseBody, request: Request):
    from ..services import studio

    ensure_access(project_id, request, write=True)
    try:
        with workspaces._project_lock(project_id):
            return studio.acquire_lease(
                project_id, body.path, _request_user(request) or "本机用户"
            )
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.delete("/{project_id}/studio/lease")
def studio_release_lease(project_id: str, path: str, request: Request):
    from ..services import studio

    ensure_access(project_id, request, write=True)
    with workspaces._project_lock(project_id):
        studio.release_lease(project_id, path, _request_user(request) or "本机用户")
    return {"ok": True}


@router.get("/{project_id}/studio/draft")
def studio_get_draft(project_id: str, path: str, request: Request):
    from ..services import studio
    from ..services.workspace import _content_revision

    root = ensure_access(project_id, request)
    target = workspaces._resolve_inside(root, path)
    with workspaces._project_lock(project_id):
        current_content = target.read_bytes() if target.is_file() else b""
        draft = studio.reconcile_draft(
            project_id, root, path, current_content, _content_revision(current_content)
        )
    return {"draft": draft}


@router.put("/{project_id}/studio/draft")
def studio_save_draft(project_id: str, body: StudioDraftBody, request: Request):
    from ..services import studio
    from ..services.workspace import _content_revision

    root = ensure_access(project_id, request, write=True)
    target = workspaces._resolve_inside(root, body.path)
    try:
        with workspaces._project_lock(project_id):
            current_content = target.read_bytes() if target.is_file() else b""
            return studio.save_draft(
                project_id,
                body.path,
                body.content,
                body.base_revision,
                _request_user(request) or "本机用户",
                current_revision=_content_revision(current_content),
            )
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.delete("/{project_id}/studio/draft")
def studio_discard_draft(project_id: str, path: str, request: Request):
    from ..services import studio

    ensure_access(project_id, request, write=True)
    with workspaces._project_lock(project_id):
        studio.discard_draft(project_id, path, _request_user(request) or "本机用户")
    return {"ok": True}


@router.post("/{project_id}/studio/submit")
async def studio_submit(project_id: str, body: StudioLeaseBody, request: Request):
    """草稿提交分流：确定性检查通过直接落版本；超范围转 agent 复核；硬伤拒绝。"""
    from ..services import studio
    from ..services.agent_sessions import manager
    from ..services.workspace import _content_revision

    root = ensure_access(project_id, request, write=True)
    user = _request_user(request) or "本机用户"
    target = workspaces._resolve_inside(root, body.path)
    with workspaces._project_lock(project_id):
        current_content = target.read_bytes() if target.is_file() else b""
        current_revision = _content_revision(current_content)
        draft = studio.reconcile_draft(
            project_id, root, body.path, current_content, current_revision
        )
        if not draft:
            raise WorkspaceError("没有待提交的草稿", 404)
        if draft.get("state") == "stale":
            raise WorkspaceError("草稿基于旧版本，请刷新后基于最新内容重新编辑", 409)
        try:
            studio.ensure_draft_current(draft, current_revision)
        except studio.StudioError as exc:
            raise WorkspaceError(str(exc), exc.status)
        lease = studio.lease_status(project_id, body.path)
        if lease and lease["owner"] != user:
            raise WorkspaceError(f"该条目正由 {lease['owner']} 编辑", 409)

        checks = studio.deterministic_check(root, body.path, draft["content"])
        if checks["review"] and not checks["errors"]:
            staged = studio.stage_for_review(root, body.path, draft["content"])
        elif not checks["errors"]:
            saved = workspaces._write_file_unlocked(
                project_id,
                body.path,
                draft["content"],
                base_revision=draft.get("base_revision", ""),
                author=user,
            )
            studio.discard_draft(project_id, body.path, user)

    if checks["errors"]:
        return {"result": "rejected", "errors": checks["errors"], "review": checks["review"]}
    if checks["review"]:
        points = "；".join(checks["review"])
        await manager.send(
            project_id,
            root,
            f"用户在 Studio 修改了 {body.path}，改动超出确定性检查范围，需要你复核。"
            f"草稿全文在 {staged}。检查发现的疑点：{points}。"
            f"请对照现有文档评估草稿：合理则把草稿内容合并进正式文档并保存版本，"
            f"有问题则说明原因；缺失的关联文档如确有必要可一并创建。",
        )
        return {"result": "needs_review", "review": checks["review"], "staged": staged}
    return {"result": "committed", **saved}


@router.get("/{project_id}/report-items")
def report_items(project_id: str, path: str, request: Request):
    """报告条目化：供评审视图做编号勾选。"""
    from ..services import report_items as parser

    ensure_access(project_id, request)
    data = workspaces.read_file(project_id, path, config.MAX_FILE_PREVIEW_BYTES)
    return parser.parse(data["content"])


QUERY_KIND_ARGS = {
    "used_by": lambda term: [["--used-by", term]],
    "orphans": lambda term: [["--orphans"]],
    "search": lambda term: [["--search", term]],
    "unused": lambda term: [["--unused-objects"], ["--unused-actions"]],
    "dangling": lambda term: [["--dangling"]],
    "knowledge": lambda term: [["--knowledge", term]],
}


@router.get("/{project_id}/query")
def reverse_query(project_id: str, kind: str, request: Request, term: str = ""):
    """六种反查：直接跑工作区内的确定性脚本（不经 LLM），返回 markdown 结果。"""
    import subprocess

    root = ensure_access(project_id, request)
    if kind not in QUERY_KIND_ARGS:
        raise WorkspaceError(f"kind 必须是 {sorted(QUERY_KIND_ARGS)} 之一")
    if kind in ("used_by", "search", "knowledge") and not term.strip():
        raise WorkspaceError("该反查类型需要提供 term 参数")
    script = root / ".claude" / "scripts" / "query.py"
    if not script.exists():
        raise WorkspaceError("查询脚本不存在（资产未挂载）", 500)
    outputs = []
    for args in QUERY_KIND_ARGS[kind](term.strip()):
        result = subprocess.run(
            ["python3", str(script), *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        outputs.append(
            result.stdout if result.returncode == 0 else f"查询失败：{result.stderr[:200]}"
        )
    return {"markdown": "\n\n".join(outputs)}


class KnowledgeBody(BaseModel):
    name: str
    content: str


@router.get("/{project_id}/knowledge")
def list_knowledge(project_id: str, request: Request):
    """知识库列表（knowledge/*.md + 首段摘要）。"""
    root = ensure_access(project_id, request)
    directory = root / "knowledge"
    entries = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            body_lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            entries.append(
                {
                    "path": f"knowledge/{path.name}",
                    "name": path.stem,
                    "excerpt": (body_lines[0] if body_lines else "")[:120],
                    "updated_at": int(path.stat().st_mtime),
                }
            )
    return {"entries": entries}


@router.post("/{project_id}/knowledge")
def create_knowledge(project_id: str, body: KnowledgeBody, request: Request):
    """业务知识确认入库：写 knowledge/<name>.md 并记录版本。"""
    ensure_access(project_id, request, write=True)
    safe = re.sub(r"[\\/:*?\"<>|.]+", "-", body.name.strip())[:60].strip("-")
    if not safe:
        raise WorkspaceError("知识条目名称不能为空")
    return workspaces.write_file(
        project_id,
        f"knowledge/{safe}.md",
        body.content,
        author=_request_user(request),
    )


@router.delete("/{project_id}/knowledge")
def delete_knowledge(project_id: str, path: str, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.delete_file(project_id, path, author=_request_user(request))


@router.get("/{project_id}/versions")
def versions(project_id: str, request: Request, offset: int = 0, limit: int = 50):
    ensure_access(project_id, request)
    can_revert = _can_revert(project_id, request)
    can_tag = _can_tag(project_id, request)
    entries = workspaces.versions(project_id, limit=limit, offset=offset)
    latest = workspaces.versions(project_id, limit=1)
    total = latest[0]["version"] if latest else 0
    return {
        "versions": [
            {**entry, "can_revert": can_revert, "can_tag": can_tag} for entry in entries
        ],
        "total": total,
        "next_offset": offset + len(entries) if offset + len(entries) < total else None,
    }


def _can_revert(project_id: str, request: Request) -> bool:
    user = getattr(request.state, "user", None)
    if user is None:
        return True
    meta = workspaces.get_project(project_id)
    role = workspaces.role_of(
        meta, user.get("sub", ""), user.get("role") == "admin"
    )
    return role in ("owner", "admin") or not meta.get("owner")


def _can_tag(project_id: str, request: Request) -> bool:
    """标记与删除标记要求写权限（与 tag 接口的 ensure_access(write=True) 一致），只读评审不显示入口。"""
    user = getattr(request.state, "user", None)
    if user is None:
        return True
    meta = workspaces.get_project(project_id)
    return workspaces.can_write(meta, user.get("sub", ""), user.get("role") == "admin")


@router.get("/{project_id}/history")
def history(project_id: str, request: Request, limit: int = 50, cursor: str = ""):
    """历史时间线：迭代与发布版本按时间倒序合并分页，游标由后端生成。"""
    from .hub_packages import identity
    from ..services import auth, version_history
    # 未启用统一账号时没有个人 Hub 包，时间线只含迭代
    token, subject = identity(request)[:2] if auth.ENABLED else ("", "")
    ensure_access(project_id, request)
    if not 1 <= limit <= 50:
        raise WorkspaceError("分页参数无效")
    return version_history.page(
        workspaces,
        project_id,
        subject,
        token,
        limit,
        cursor,
        can_revert=_can_revert(project_id, request),
        can_tag=_can_tag(project_id, request),
    )


@router.post("/{project_id}/versions/revert")
async def revert(project_id: str, body: RevertBody, request: Request):
    ensure_owner(project_id, request)
    result = await version_revert.apply_direct(
        workspaces, project_id, body.commit, body.expected_head
    )
    return result


@router.get("/{project_id}/versions/revert-preview")
def revert_preview(project_id: str, commit: str, request: Request):
    ensure_owner(project_id, request)
    return version_revert.preview_by_commit(workspaces, project_id, commit)


@router.post("/{project_id}/versions/tag")
def tag_version(project_id: str, body: TagBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.tag_version(project_id, body.commit, body.label)


@router.delete("/{project_id}/versions/tag/{tag_name}")
def delete_tag(project_id: str, tag_name: str, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.delete_tag(project_id, tag_name)


def hub_ontology_id(project_id):
    from ..services.hub_packages import association
    linked = association(project_id)
    if not linked:
        raise WorkspaceError('项目尚未发布到 Hub', 404)
    return linked['ontology_id']
