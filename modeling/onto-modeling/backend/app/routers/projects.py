import asyncio
import json
import re
import tempfile
import urllib.error
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..services import eval_data_files, eval_data_sources, eval_import, eval_sets, package_mapping
from ..services import modeling_cleanup, modeling_mcp, package_archive, solver_contract, version_revert
from ..services.workspace import WorkspaceError, WorkspaceManager, remove_tree

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


def provision_modeling_project(meta: dict, request: Request) -> dict:
    """Associate a newly created Onto project with its own Modeling project."""
    provision = None
    try:
        provision = modeling_mcp.provision_project(meta, request)
        updated = provision.meta
        project_id = provision.modeling_project_id
        if project_id:
            updated = workspaces._update_meta(meta["id"], modeling_project_id=project_id)
            if modeling_mcp.configured():
                modeling_mcp.sync_members(updated, request)
            return updated
        return updated
    except Exception:
        if provision and provision.created and provision.modeling_project_id:
            cleanup_meta = {
                **meta,
                "modeling_project_id": provision.modeling_project_id,
            }
            try:
                modeling_mcp.delete_project(cleanup_meta, request)
            except Exception as cleanup_error:
                modeling_cleanup.enqueue(
                    cleanup_meta,
                    reason=str(cleanup_error),
                )
        # Initialization failure must not leave an apparently usable, unlinked project.
        workspaces.delete_project(meta["id"])
        raise


def associate_imported_project(meta: dict, request: Request) -> dict:
    """Best-effort association for imports; a Modeling outage must not undo import."""
    try:
        result = modeling_mcp.ensure_project_association(
            meta, request, workspaces._update_meta
        )
        updated = workspaces.get_project(meta["id"])
        updated["modeling_association"] = result
        return updated
    except modeling_mcp.ModelingMcpError as exc:
        updated = workspaces.get_project(meta["id"])
        project_id = str(updated.get("modeling_project_id") or "").strip()
        updated["modeling_association"] = {
            "linked": bool(project_id),
            "configured": modeling_mcp.configured() or modeling_mcp.association_configured(),
            "synced": False if project_id else None,
            "detail": str(exc),
        }
        return updated


def _may_auto_associate(meta: dict, request: Request) -> bool:
    user = getattr(request.state, "user", None)
    if user is None:
        return True
    role = workspaces.role_of(
        meta, user.get("sub", ""), user.get("role") == "admin"
    )
    return role in ("owner", "admin")


def ensure_mcp_association(
    meta: dict, request: Request, *, require_association: bool = False
) -> dict:
    """Lazy repair for owner/admin requests; never create a remote project for a reader."""
    if not _may_auto_associate(meta, request):
        if require_association and not modeling_mcp._modeling_project_id(meta):
            raise modeling_mcp.ModelingMcpError(
                "当前用户无权自动关联 Modeling 项目，请联系项目所有者或管理员", 403
            )
        return meta
    modeling_mcp.ensure_project_association(meta, request, workspaces._update_meta)
    return workspaces.get_project(meta["id"])


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


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request, background: BackgroundTasks):
    """永久删除项目（不进回收站）。不看项目状态，二次确认由前端弹窗完成。

    响应前只做快的：硬杀会话（含排队）、给评测编排置取消标记、把工作区移出列表、
    清掉存着凭据的配置。远端评测轮次取消、modeling 侧级联删除与目录递归删除都要等
    网络或磁盘，放到响应之后（_finish_delete）。
    """
    from ..services import modeling_eval, studio
    from ..services.agent_sessions import manager

    root = ensure_owner(project_id, request)
    meta = workspaces.get_project(project_id)  # 删除后读不到，级联清理要用，先取
    eval_round = modeling_eval.abort_for_delete(project_id, root, _modeling_token(request))
    await manager.discard(project_id)
    staged = await asyncio.to_thread(workspaces.stage_delete, project_id)
    await manager.discard(project_id)  # 移走期间并发发消息新建的会话一并清掉
    eval_data_sources.purge_project(project_id)
    studio.purge_project(project_id)
    background.add_task(_finish_delete, project_id, staged, meta, request, eval_round)
    return {"deleted": project_id}


async def _finish_delete(
    project_id: str,
    staged: Path,
    meta: dict,
    request: Request,
    eval_round: tuple[str, str, str] | None,
) -> None:
    """删除收尾（响应之后执行）。各项互不依赖，并发进行，一项失败不拖累其余；
    没清干净的记入运维错误日志（工作区目录的残留由下次启动清扫兜底）。"""
    from ..services import error_log, modeling_eval
    from ..services.engines import jiuwen

    jobs = {
        "工作区目录": asyncio.to_thread(workspaces.purge_staged, staged),
        "引擎工件区": asyncio.to_thread(remove_tree, jiuwen.shadow_dir(project_id)),
    }
    if eval_round:
        jobs["评测轮次取消"] = asyncio.to_thread(modeling_eval.cancel_round, *eval_round)
    if meta.get("modeling_project_id"):
        jobs["modeling 侧项目"] = asyncio.to_thread(_cascade_modeling_delete, meta, request)
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)
    for label, result in zip(jobs, results):
        problem = _cleanup_problem(result)
        if problem:
            error_log.log(project_id, "project_delete", f"{label}未清理完成：{problem}")


def _cleanup_problem(result) -> str:
    """收尾结果 → 未完成的原因（空串表示已完成）。"""
    if isinstance(result, BaseException):
        return str(result) or type(result).__name__
    if isinstance(result, tuple):  # cancel_round：（是否已取消，未确认的原因）
        return result[1]
    return "仍有文件被占用" if result is False else ""


def _cascade_modeling_delete(meta: dict, request: Request) -> None:
    """级联删除 modeling 侧关联项目；失败转入持久化重试队列。"""
    try:
        modeling_mcp.delete_project(meta, request, operation="project_delete")
    except Exception as exc:
        if isinstance(exc, modeling_mcp.ModelingMcpError) and exc.status == 404:
            return  # 那边早已不在
        modeling_cleanup.enqueue(meta, reason=str(exc), operation="project_delete")


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


@router.get("/{project_id}/members")
def get_members(project_id: str, request: Request):
    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    return {"owner": meta.get("owner", ""), "members": meta.get("members") or {}}


@router.put("/{project_id}/members")
def put_members(project_id: str, body: MembersBody, request: Request):
    ensure_owner(project_id, request)
    previous = workspaces.get_project(project_id)
    meta = workspaces.set_members(project_id, body.members)
    try:
        modeling_mcp.sync_members(meta, request)
    except modeling_mcp.ModelingMcpError as exc:
        workspaces._update_meta(project_id, members=previous.get("members") or {})
        return JSONResponse(status_code=exc.status, content={"error": str(exc)})
    return {"owner": meta.get("owner", ""), "members": meta.get("members") or {}}


@router.get("/{project_id}/export")
def export_project(
    project_id: str, request: Request, scope: str = "ontology", allow_unbound: bool = False
):
    ensure_access(project_id, request)
    if scope not in ("ontology", "workspace", "package"):
        raise WorkspaceError("scope 必须是 ontology、workspace 或 package")
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    if scope == "package":
        workspaces.publish_package(
            project_id, dest, allow_unbound=allow_unbound, author=_request_user(request)
        )
    else:
        workspaces.export_zip(project_id, scope, dest)
    return FileResponse(dest, filename=dest.name, media_type="application/zip")


@router.get("/{project_id}/package/check")
def package_check(project_id: str, request: Request):
    """发布前数据绑定体检：阻断项、无从核对项、提示，以及修复所需的表清单与文档版本。"""
    ensure_access(project_id, request)
    return workspaces.package_binding_check(project_id)


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


class SolverValidationCasesBody(BaseModel):
    cases: list  # [{id, name, solver_key, arguments, expected_candidate_count?}]


class SolverValidationStartBody(BaseModel):
    revision: str  # 面板读取到的求解契约修订；与当前不一致返回 409
    case_ids: list[str] = Field(default_factory=list)  # 空表示全部已保存用例
    data_source_id: str = ""  # 已保存的评测数据源；空表示不使用数据源
    allow_unbound: bool = False  # 与发布一致：用户确认后数据绑定对不上的对象按未绑定处理


@router.get("/{project_id}/package/solver-validations/cases")
def get_solver_validation_cases(project_id: str, request: Request):
    from ..services import solver_validation_run

    root = ensure_access(project_id, request)
    return {"cases": solver_validation_run.list_cases(root)}


@router.put("/{project_id}/package/solver-validations/cases")
def save_solver_validation_cases(project_id: str, body: SolverValidationCasesBody, request: Request):
    from ..services import solver_validation_run

    root = ensure_access(project_id, request, write=True)
    return {"cases": solver_validation_run.save_cases(root, body.cases)}


@router.post("/{project_id}/package/solver-validations")
async def start_solver_validation(
    project_id: str, body: SolverValidationStartBody, request: Request
):
    """对当前修订启动 Consumer 端到端验证（后台执行，经 latest 轮询）。

    请求只含修订、用例与已保存的数据源 ID：Solver 地址与凭据只存在于 Consumer 侧。
    """
    from ..services import solver_validation_run

    ensure_access(project_id, request, write=True)
    def reserve_validation():
        with workspaces._project_lock(project_id):
            return solver_validation_run.start(
                workspaces,
                project_id,
                revision=body.revision,
                case_ids=body.case_ids,
                token=_modeling_token(request),
                data_source_id=body.data_source_id,
                allow_unbound=body.allow_unbound,
                by=_request_user(request),
            )

    record, plan = await asyncio.to_thread(reserve_validation)
    try:
        solver_validation_run.launch(plan)
    except Exception:
        solver_validation_run.release_reservation(project_id)
        raise
    return {"validation": record}


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
    purpose: str = Form("auto"),
    reading_mode: str = Form("on_demand"),
):
    ensure_access(project_id, request, write=True)
    data = await file.read()
    # 大文件同步写盘会卡住事件循环（SSE 全体停摆），挪到线程执行
    return await asyncio.to_thread(
        workspaces.save_resource,
        project_id,
        subdir,
        file.filename or "",
        data,
        purpose,
        reading_mode,
    )


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
def set_resource_meta(project_id: str, body: ResourceDirectiveBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.set_resource_directive(
        project_id, body.path, body.purpose, body.reading_mode,
        body.schema_selection.model_dump() if body.schema_selection is not None else None,
    )


@router.delete("/{project_id}/resources")
def delete_resource(project_id: str, path: str, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.delete_resource(project_id, path)


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


@router.get("/{project_id}/eval/data-source")
def eval_data_source_options(project_id: str, request: Request):
    """评测向导·数据源选项：mock 产物可用性（业务数据表在数据源表单里上传）。"""
    root = ensure_access(project_id, request)
    return _eval_data_options(root)


def _eval_data_options(root: Path) -> dict:
    mock_dir = root / "ontology" / "data-mock"
    mock_tables = (
        sum(1 for f in mock_dir.glob("*.md") if f.name.lower() != "index.md")
        if mock_dir.is_dir() else 0
    )
    return {"mock_tables": mock_tables}


@router.post("/{project_id}/eval/data-files")
async def upload_eval_data_file(project_id: str, request: Request, file: UploadFile = File(...)):
    """数据源表单上传业务数据表（Excel）：返回 {id, name, size}，保存配置时引用 ID。"""
    root = ensure_access(project_id, request, write=True)
    data = await file.read()
    try:
        # 大文件写盘挪到线程，避免卡住事件循环
        entry = await asyncio.to_thread(eval_data_files.save, root, file.filename or "", data)
    except eval_data_files.DataFileError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"file": entry}


# ---- 评测数据源配置（保存后评测时直接选用；连接密钥只进不出）----


class EvalDataSourceBody(BaseModel):
    name: str = ""
    mode: str = "none"  # none | dataset | connection
    provider: str = ""  # connection：nl2sql_template | nl2sql_http | neo4j
    config: dict = {}
    files: list[str] = []  # dataset / neo4j 导入：上传的业务数据表文件 ID
    include_mock: bool = False  # dataset / neo4j 导入：mock 种子


def _default_eval_workspace_id(project_id: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", project_id.lower()).strip("-")
    return f"onto-eval-{slug}"[:64].rstrip("-_")


@router.get("/{project_id}/eval/data-sources")
def list_eval_data_sources(project_id: str, request: Request):
    """已保存的数据源配置 + 新建配置所需的选项与默认值。"""
    root = ensure_access(project_id, request)
    return {
        "sources": [
            eval_data_sources.public_view(s)
            for s in eval_data_sources.list_sources(project_id, root)
        ],
        "options": _eval_data_options(root),
        "defaults": {
            "evaluation_workspace_id": _default_eval_workspace_id(project_id),
            # Neo4j Ontology 隔离值 = 知识包 ID（发起评测时自动填写）
            "ontology_id": project_id,
        },
    }


@router.post("/{project_id}/eval/data-sources")
def create_eval_data_source(project_id: str, body: EvalDataSourceBody, request: Request):
    root = ensure_access(project_id, request, write=True)
    try:
        source = eval_data_sources.create_source(project_id, body.model_dump(), root)
    except eval_data_sources.DataSourceError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"source": eval_data_sources.public_view(source)}


@router.put("/{project_id}/eval/data-sources/{source_id}")
def update_eval_data_source(
    project_id: str, source_id: str, body: EvalDataSourceBody, request: Request
):
    """更新配置；Token / Password 留空表示沿用已保存的值。"""
    root = ensure_access(project_id, request, write=True)
    try:
        source = eval_data_sources.update_source(project_id, source_id, body.model_dump(), root)
    except eval_data_sources.DataSourceError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "数据源配置不存在或已删除"})
    return {"source": eval_data_sources.public_view(source)}


@router.delete("/{project_id}/eval/data-sources/{source_id}")
def delete_eval_data_source(project_id: str, source_id: str, request: Request):
    root = ensure_access(project_id, request, write=True)
    try:
        eval_data_sources.delete_source(project_id, source_id, root)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "数据源配置不存在或已删除"})
    return {"deleted": True}


class EvalImportPreviewBody(BaseModel):
    data_source_id: str = ""  # 已保存的配置；缺省时按下面的内联选择预览
    files: list[str] = []  # 上传的业务数据表文件 ID
    include_mock: bool = False
    graph: bool = False  # Neo4j 图导入：额外校验属性名，mapping 按图模式


@router.post("/{project_id}/eval/import-preview")
def eval_import_preview(project_id: str, body: EvalImportPreviewBody, request: Request):
    """一键导入预览：导入哪些表、日期列如何规范化、对象绑到哪张表哪些列（即 mapping）。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request)
    if body.data_source_id:
        source = eval_data_sources.get_source(project_id, body.data_source_id, root)
        if not source:
            return JSONResponse(status_code=404, content={"error": "数据源配置不存在或已删除"})
        if not modeling_eval.needs_import(source):
            return {
                "tables": [], "objects": {}, "mapped_objects": [], "relations": {},
                "problems": [], "warnings": [],
            }
        selection, graph = source, modeling_eval.is_graph(source)
    else:
        selection, graph = {"files": body.files, "include_mock": body.include_mock}, body.graph
    return eval_import.preview(root, selection, graph=graph)


# ---- 评测集（导入过的用例保存下来，评测时直接选用）----


class EvalSetBody(BaseModel):
    name: str = ""
    cases: list[dict] = []
    source: str = "manual"  # import | trace | manual


class EvalSetPatchBody(BaseModel):
    name: str | None = None
    cases: list[dict] | None = None


@router.get("/{project_id}/eval/sets")
def list_eval_sets(project_id: str, request: Request):
    root = ensure_access(project_id, request)
    return {"sets": eval_sets.list_sets(root)}


@router.get("/{project_id}/eval/sets/{set_id}")
def get_eval_set(project_id: str, set_id: str, request: Request):
    root = ensure_access(project_id, request)
    item = eval_sets.get_set(root, set_id)
    if not item:
        return JSONResponse(status_code=404, content={"error": "评测集不存在或已删除"})
    return {"set": {**eval_sets.summary(item), "cases": item["cases"]}}


@router.post("/{project_id}/eval/sets")
def create_eval_set(project_id: str, body: EvalSetBody, request: Request):
    root = ensure_access(project_id, request, write=True)
    try:
        item = eval_sets.create_set(root, body.name, body.cases, body.source)
    except eval_sets.EvalSetError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    return {"set": eval_sets.summary(item)}


@router.patch("/{project_id}/eval/sets/{set_id}")
def update_eval_set(project_id: str, set_id: str, body: EvalSetPatchBody, request: Request):
    root = ensure_access(project_id, request, write=True)
    try:
        item = eval_sets.update_set(root, set_id, name=body.name, cases=body.cases)
    except eval_sets.EvalSetError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "评测集不存在或已删除"})
    return {"set": eval_sets.summary(item)}


@router.delete("/{project_id}/eval/sets/{set_id}")
def delete_eval_set(project_id: str, set_id: str, request: Request):
    root = ensure_access(project_id, request, write=True)
    try:
        eval_sets.delete_set(root, set_id)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "评测集不存在或已删除"})
    return {"deleted": True}


@router.get("/{project_id}/eval/status")
def eval_status(project_id: str, request: Request):
    """数据评测状态：modeling 可达性 + 最近一轮进度/结果（前端阶段按钮轮询）。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request)
    # 前端轮询自带自动刷新的 token：接力给运行中的编排（评测轮常超 token 有效期）
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        modeling_eval.update_token(project_id, auth[7:].strip())
    state = modeling_eval.status_view(project_id, root)
    return {
        "enabled": modeling_eval.enabled(),
        "reachable": modeling_eval.reachable() if modeling_eval.enabled() else False,
        "project_id": project_id,
        "state": state,
        "evolution": modeling_eval.evolution_hint(state),
    }


@router.get("/{project_id}/eval/answers/{result_id}")
def eval_full_answer(project_id: str, result_id: str, request: Request):
    """按需读取评测结果的完整 finalAnswer；列表状态中的 answer 仅是摘要。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request)
    auth = request.headers.get("Authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    try:
        return modeling_eval.full_answer(project_id, root, token, result_id)
    except urllib.error.HTTPError as exc:
        return JSONResponse(status_code=exc.code, content={"error": "完整回答暂时无法读取"})
    except ValueError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})


@router.get("/{project_id}/eval/cases")
def eval_cases_preview(project_id: str, request: Request):
    """trace 场景 → 评测用例的转换预览（运行前供用户复核修订）。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request)
    return {"cases": modeling_eval.trace_cases(root)}


@router.post("/{project_id}/eval/cases/import")
async def eval_cases_import(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    save: bool = Form(True),
    name: str = Form(""),
):
    """用例 JSON 导入（与 modeling 的用例导入模板同一契约）。

    save=True（默认）时同时保存为评测集（名称缺省取文件名），下次评测可直接选用。
    """
    from ..services import modeling_eval

    root = ensure_access(project_id, request, write=save)
    if not (file.filename or "").lower().endswith(".json"):
        return JSONResponse(status_code=400, content={"error": "仅支持 JSON 用例文件（可先下载模板）"})
    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        return JSONResponse(status_code=400, content={"error": "用例文件不超过 2MB"})
    try:
        cases = modeling_eval.parse_case_json(content.decode("utf-8-sig", errors="replace"))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not save:
        return {"cases": cases, "set": None}
    set_name = name.strip() or Path(file.filename or "").stem or "导入的评测集"
    try:
        item = eval_sets.create_set(root, set_name, cases, source="import")
    except eval_sets.EvalSetError as exc:
        # 用例本身合法，只是评测集存不下（如数量上限）：照常返回用例，附带原因
        return {"cases": cases, "set": None, "warning": str(exc)}
    return {"cases": cases, "set": eval_sets.summary(item)}


@router.get("/{project_id}/eval/cases/template")
def eval_cases_template(project_id: str, request: Request):
    """用例导入模板下载（modeling 同款）。"""
    from fastapi.responses import Response

    from ..services import modeling_eval

    ensure_access(project_id, request)
    return Response(
        content=modeling_eval.case_template_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''%E8%AF%84%E6%B5%8B%E7%94%A8%E4%BE%8B%E5%AF%BC%E5%85%A5%E6%A8%A1%E6%9D%BF.json"},
    )


class EvalRunBody(BaseModel):
    cases: list[dict]  # [{question, groundTruth}]，用户复核后的用例
    # {"mode": "none"} 或 {"mode": "dataset", "files": [文件 ID...], "include_mock": bool}
    data_source: dict | None = None
    # 选用已保存的数据源配置：优先于内联 data_source；外部连接（带密钥）只能走这里
    data_source_id: str = ""
    # 选用的评测集：复核时对用例的修改同步回该评测集
    eval_set_id: str = ""
    # 用户已确认降级：数据绑定对不上的对象不阻断评测，按未绑定处理
    allow_unbound: bool = False
    # 当前登录的 refresh token：转交 modeling 侧为本轮评测续期 access token（轮次常超
    # access token 有效期）。只在内存里透传一次，不落状态、不进日志（repr=False）
    refresh_token: str = Field(default="", repr=False)


@router.post("/{project_id}/eval/run")
async def eval_run(project_id: str, body: EvalRunBody, request: Request):
    """启动数据评测（后台编排，状态经 /eval/status 轮询）。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request, write=True)
    try:
        modeling_eval.evaluation_object_snapshot(root)
    except modeling_eval.EvaluationPackageInvalid as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": str(exc), "code": exc.error_code, **exc.error_details,
            },
        )
    if not modeling_eval.enabled():
        return JSONResponse(status_code=400, content={"error": "未配置 modeling 评测服务（ONTO_WEB_MODELING_URL）"})
    if not modeling_eval.reachable():
        return JSONResponse(status_code=503, content={"error": "modeling 评测服务不可达，请先启动该服务"})
    cases = [
        {"question": str(c.get("question", "")).strip(), "groundTruth": str(c.get("groundTruth", "")).strip()}
        for c in body.cases
        if str(c.get("question", "")).strip()
    ]
    if not cases:
        return JSONResponse(status_code=400, content={"error": "没有可用的评测用例（先完成场景验证或手工补充）"})
    if len(cases) > 500:
        return JSONResponse(status_code=400, content={"error": "一次最多 500 条用例"})
    # 鉴权：共享账号中心下透传当前用户 token；否则用部署级兜底令牌
    auth = request.headers.get("authorization") or ""
    token = config.MODELING_TOKEN or (auth[7:] if auth.lower().startswith("bearer ") else "")
    if not token:
        return JSONResponse(status_code=400, content={"error": "缺少可用于 modeling 的令牌"})
    if body.data_source_id:
        data_source = eval_data_sources.get_source(project_id, body.data_source_id, root)
        if not data_source:
            return JSONResponse(status_code=404, content={"error": "数据源配置不存在或已删除，请重新选择"})
    else:
        data_source = body.data_source or {"mode": "none"}
        if str(data_source.get("mode") or "none") not in ("none", "dataset"):
            return JSONResponse(
                status_code=400,
                content={"error": "内联数据源仅支持 none 或 dataset；外部数据源请先保存配置再选用"},
            )
    if modeling_eval.needs_import(data_source):
        plan = eval_import.collect(root, data_source)
        problems = plan["problems"] + (
            eval_import.graph_problems(plan["binding"]) if modeling_eval.is_graph(data_source) else []
        )
        if problems:
            return JSONResponse(
                status_code=400,
                content={"error": "导入数据未通过检查：" + "；".join(problems[:3]), "problems": problems},
            )
    elif not body.allow_unbound:
        binding = package_mapping.resolve_object_tables(root)
        if binding["problems"]:
            blocking = [item for item in binding["issues"] if item["blocking"]]
            return JSONResponse(
                status_code=400,
                content={
                    "error": "数据绑定未通过检查：" + "；".join(binding["problems"][:3]),
                    "code": "binding_problems",
                    "issues": blocking[: workspaces.PACKAGE_CHECK_LIMIT],
                },
            )

    eval_set_name = ""
    if body.eval_set_id:
        item = eval_sets.get_set(root, body.eval_set_id)
        if not item:
            return JSONResponse(status_code=404, content={"error": "评测集不存在或已删除，请重新选择"})
        if item["cases"] != cases:
            item = eval_sets.update_set(root, body.eval_set_id, cases=cases)
        eval_sets.mark_used(root, body.eval_set_id)
        eval_set_name = item["name"]
    if body.data_source_id:
        eval_data_sources.mark_used(project_id, body.data_source_id)

    # 部署级兜底令牌不属于当前用户：此时不转交用户的 refresh token，避免身份错配
    refresh_token = "" if config.MODELING_TOKEN else body.refresh_token.strip()
    try:
        modeling_eval.launch(
            project_id,
            root,
            token,
            cases,
            data_source,
            eval_set_name,
            allow_unbound=body.allow_unbound,
            refresh_token=refresh_token,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    return {"started": True, "cases": len(cases)}


def _modeling_token(request: Request) -> str:
    """modeling 调用令牌：共享账号中心下透传当前用户 token，否则用部署级兜底。"""
    auth = request.headers.get("authorization") or ""
    return config.MODELING_TOKEN or (auth[7:] if auth.lower().startswith("bearer ") else "")


@router.post("/{project_id}/eval/stop")
def eval_stop(project_id: str, request: Request):
    """停止评测：取消 modeling 侧轮次并把状态收敛到可重跑的终态。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request, write=True)
    if not modeling_eval.enabled():
        return JSONResponse(status_code=400, content={"error": "未配置 modeling 评测服务（ONTO_WEB_MODELING_URL）"})
    try:
        return modeling_eval.request_stop(project_id, root, _modeling_token(request))
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})


@router.post("/{project_id}/eval/resume")
async def eval_resume(project_id: str, request: Request):
    """续接上一轮：只收取 modeling 侧已有轮次的结果，不重新发起评测。"""
    from ..services import modeling_eval

    root = ensure_access(project_id, request, write=True)
    if not modeling_eval.enabled():
        return JSONResponse(status_code=400, content={"error": "未配置 modeling 评测服务（ONTO_WEB_MODELING_URL）"})
    if not modeling_eval.reachable():
        return JSONResponse(status_code=503, content={"error": "modeling 评测服务不可达，请先启动该服务"})
    if not modeling_eval.can_resume(root):
        return JSONResponse(status_code=409, content={"error": "没有可续接的评测轮次，请重新运行评测"})
    token = _modeling_token(request)
    if not token:
        return JSONResponse(status_code=400, content={"error": "缺少可用于 modeling 的令牌"})
    import asyncio as _asyncio

    _asyncio.create_task(modeling_eval.resume(project_id, root, token))
    return {"resuming": True}


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
    try:
        data = studio.element(root, path)
    except FileNotFoundError:
        raise WorkspaceError("条目不存在", 404)
    data["revision"] = _content_revision(target.read_bytes())
    data["lease"] = studio.lease_status(project_id, path)
    draft = studio.get_draft(project_id, path)
    data["draft"] = (
        {"updated_at": draft["updated_at"], "author": draft["author"]} if draft else None
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

    ensure_access(project_id, request)
    return {"draft": studio.get_draft(project_id, path)}


@router.put("/{project_id}/studio/draft")
def studio_save_draft(project_id: str, body: StudioDraftBody, request: Request):
    from ..services import studio

    ensure_access(project_id, request, write=True)
    try:
        with workspaces._project_lock(project_id):
            return studio.save_draft(
                project_id,
                body.path,
                body.content,
                body.base_revision,
                _request_user(request) or "本机用户",
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

    root = ensure_access(project_id, request, write=True)
    user = _request_user(request) or "本机用户"
    draft = studio.get_draft(project_id, body.path)
    if not draft:
        raise WorkspaceError("没有待提交的草稿", 404)
    lease = studio.lease_status(project_id, body.path)
    if lease and lease["owner"] != user:
        raise WorkspaceError(f"该条目正由 {lease['owner']} 编辑", 409)

    checks = studio.deterministic_check(root, body.path, draft["content"])
    if checks["errors"]:
        return {"result": "rejected", "errors": checks["errors"], "review": checks["review"]}
    if checks["review"]:
        staged = studio.stage_for_review(root, body.path, draft["content"])
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
    with workspaces._project_lock(project_id):
        # Re-read under the project lock shared with rollback/MCP writes.
        draft = studio.get_draft(project_id, body.path)
        if not draft:
            raise WorkspaceError("没有待提交的草稿", 404)
        saved = workspaces.write_file(
            project_id,
            body.path,
            draft["content"],
            base_revision=draft.get("base_revision", ""),
            author=user,
        )
        studio.discard_draft(project_id, body.path, user)
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
    user = getattr(request.state, "user", None)
    can_revert = True
    if user is not None:
        meta = workspaces.get_project(project_id)
        role = workspaces.role_of(
            meta, user.get("sub", ""), user.get("role") == "admin"
        )
        can_revert = role in ("owner", "admin") or not meta.get("owner")
    entries = workspaces.versions(project_id, limit=limit, offset=offset)
    latest = workspaces.versions(project_id, limit=1)
    total = latest[0]["version"] if latest else 0
    return {
        "versions": [
            {**entry, "can_revert": can_revert}
            for entry in entries
        ],
        "total": total,
        "next_offset": offset + len(entries) if offset + len(entries) < total else None,
    }


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
