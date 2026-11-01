import asyncio
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import config
from ..services import ownership, package_archive, solver_contract
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


class TagBody(BaseModel):
    commit: str
    label: str


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
    """同名校验的查重范围：当前用户在列表里看得到的任务；鉴权关闭时为 None（全部任务）。"""
    user = ownership.current_user(request)
    if user is None:
        return None
    return user.get("sub", ""), ownership.is_admin(user)


def require_owner(project_id: str, request: Request) -> Path:
    """项目内容守卫：读写、会话、导出发布与改名/归档/删除一律只归所有者（鉴权关闭时放开）。"""
    return ownership.require_owner(workspaces, project_id, request)


@router.get("")
def list_projects(request: Request):
    """本人项目给完整信息；管理员另外看到其他项目的元数据（含无主项目），不含项目内容。"""
    projects = workspaces.list_projects()
    return {"projects": ownership.list_view(projects, ownership.current_user(request))}


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
    """项目元数据：所有者拿完整元数据，管理员只拿元数据白名单。"""
    return ownership.require_meta_view(workspaces, project_id, request)


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameBody, request: Request):
    """重命名任务。新名称与其他任务重名时返回 409 与候选名；名称没变则不校验。"""
    require_owner(project_id, request)
    meta = workspaces.get_project(project_id)
    if body.name.strip() == meta.get("name"):
        return meta
    with workspaces.name_claim(body.name, _name_viewer(request), exclude_id=project_id):
        return workspaces.rename_project(project_id, body.name)


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request, background: BackgroundTasks):
    """永久删除项目（不进回收站）。不看项目状态，二次确认由前端弹窗完成。

    响应前只做快的：硬杀会话（含排队）、把工作区移出列表、清掉 Studio 草稿。
    目录递归删除与发布留存的删除要等磁盘，放到响应之后（_finish_delete）。
    """
    from ..services import studio
    from ..services.agent_sessions import manager

    require_owner(project_id, request)
    await manager.discard(project_id)
    staged = await asyncio.to_thread(workspaces.stage_delete, project_id)
    await manager.discard(project_id)  # 移走期间并发发消息新建的会话一并清掉
    studio.purge_project(project_id)
    background.add_task(_finish_delete, project_id, staged)
    return {"deleted": project_id}


async def _finish_delete(project_id: str, staged: Path) -> None:
    """删除收尾（响应之后执行）。各项互不依赖，并发进行，一项失败不拖累其余；
    没清干净的记入运维错误日志（工作区目录的残留由下次启动清扫兜底）。"""
    from ..services import error_log, release_store
    from ..services.engines import jiuwen

    jobs = {
        "工作区目录": asyncio.to_thread(workspaces.purge_staged, staged),
        "引擎工件区": asyncio.to_thread(remove_tree, jiuwen.shadow_dir(project_id)),
        "发布留存": asyncio.to_thread(release_store.purge_project, workspaces.data_dir, project_id),
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
    require_owner(project_id, request)
    return workspaces.set_archived(project_id, body.archived)


@router.post("/{project_id}/clone")
def clone_project(project_id: str, request: Request, body: CloneBody | None = None):
    """克隆任务，默认命名「原名-副本」；与已有任务重名时返回 409 与候选名，
    前端改名后带 name 重新提交。"""
    require_owner(project_id, request)
    default_name = workspaces.clone_name(project_id)
    name = (body.name.strip() if body else "") or default_name
    with workspaces.name_claim(name, _name_viewer(request), original_name=default_name):
        meta = workspaces.clone_project(project_id, owner=_request_user(request), name=name)
    return meta


@router.get("/{project_id}/export")
def export_project(
    project_id: str, request: Request, scope: str = "ontology", allow_unbound: bool = False
):
    """scope=package 是用户发布：按 HEAD 提交打包，版本号与来源提交放在响应头里带回
    （同源部署，前端 fetch 直接读得到，不需要 CORS expose）。"""
    require_owner(project_id, request)
    if scope not in ("ontology", "workspace", "package"):
        raise WorkspaceError("scope 必须是 ontology、workspace 或 package")
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    headers = {}
    if scope == "package":
        record = workspaces.publish_package(
            project_id, dest, allow_unbound=allow_unbound, author=_request_user(request)
        )
        headers = {
            "X-Package-Version": record["version"],
            "X-Package-Commit": record["commit"],
            "X-Package-Uncommitted": str(record["uncommitted_count"]),
        }
    else:
        workspaces.export_zip(project_id, scope, dest)
    return FileResponse(dest, filename=dest.name, media_type="application/zip", headers=headers)


@router.get("/{project_id}/package/check")
def package_check(project_id: str, request: Request):
    """发布前体检（基于 HEAD 提交，与发布同口径）：阻断项、无从核对项、提示、修复所需的
    表清单与文档版本，以及包相关目录里不会进包的未提交改动。"""
    require_owner(project_id, request)
    return workspaces.package_binding_check(project_id)


@router.get("/{project_id}/package/solver-contract")
def get_solver_contract(project_id: str, request: Request):
    """求解模型面板：当前修订、契约原文、静态检查与验证门禁。"""
    require_owner(project_id, request)
    return workspaces.solver_contract_view(project_id)


@router.put("/{project_id}/package/solver-contract")
def save_solver_contract(project_id: str, body: SolverContractBody, request: Request):
    """整体保存求解契约；结构问题 400 并逐条返回 findings，修订冲突 409。"""
    require_owner(project_id, request)
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
    require_owner(project_id, request)
    operations = body.contract.get("operations")
    if not isinstance(operations, list):
        raise WorkspaceError("contract.operations 必须是列表", code=solver_contract.CODE_INVALID)
    return workspaces.check_solver_contract_draft(project_id, operations)


@router.get("/{project_id}/package/solver-validations/latest")
def latest_solver_validation(project_id: str, request: Request):
    """当前修订最后一次验证回执：状态、断言、候选数、权威结果摘要、错误码与诊断引用。

    回执由外部验证方经 POST /api/v1/projects/{id}/solver/receipts 写入（ADR-0020）。"""
    from ..services import solver_validation

    root = require_owner(project_id, request)
    revision = workspaces.solver_check(project_id).revision
    validation = solver_validation.latest(root, revision) if revision else None
    return {"revision": revision, "validation": validation}


@router.post("/{project_id}/objects/{key}/data-source")
def set_object_data_source(project_id: str, key: str, body: ObjectDataSourceBody, request: Request):
    """发布前修复：把对象「# 数据来源」改成现有数据表之一或「无」（Web 保存同款版本点）。"""
    require_owner(project_id, request)
    return workspaces.set_object_data_source(
        project_id, key, body.table,
        base_revision=body.base_revision, author=_request_user(request),
    )


@router.get("/{project_id}/files")
def file_tree(project_id: str, request: Request):
    require_owner(project_id, request)
    return {"tree": workspaces.file_tree(project_id)}


@router.get("/{project_id}/file")
def read_file(project_id: str, path: str, request: Request):
    require_owner(project_id, request)
    return workspaces.read_file(project_id, path, config.MAX_FILE_PREVIEW_BYTES)


@router.put("/{project_id}/file")
def write_file(project_id: str, body: WriteFileBody, request: Request):
    require_owner(project_id, request)
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

    root = require_owner(project_id, request)
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
    require_owner(project_id, request)
    return workspaces.file_at_revision(project_id, rev, path)


@router.get("/{project_id}/raw")
def raw_file(project_id: str, path: str, request: Request):
    """原样返回文件（HTML 报告 iframe 预览等）。"""
    root = require_owner(project_id, request)
    target = workspaces._resolve_inside(root, path)
    if not target.is_file():
        raise WorkspaceError("文件不存在", 404)
    return FileResponse(target)


@router.get("/{project_id}/search")
def search(project_id: str, q: str, request: Request):
    """产物全文检索（命中行，上限 80 条）。"""
    root = require_owner(project_id, request)
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

    root = require_owner(project_id, request)
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
    require_owner(project_id, request)
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
        _request_user(request),
    )


@router.get("/{project_id}/resource-meta")
def resource_meta(project_id: str, request: Request):
    require_owner(project_id, request)
    return {
        "directives": workspaces.resource_directives(project_id),
        "purposes": list(workspaces.MATERIAL_PURPOSES),
        "schema_candidates": workspaces.schema_candidates(project_id),
    }


class ResourceDirectiveBody(BaseModel):
    path: str
    purpose: str
    reading_mode: str = "on_demand"


@router.patch("/{project_id}/resource-meta")
def set_resource_meta(project_id: str, body: ResourceDirectiveBody, request: Request):
    require_owner(project_id, request)
    return workspaces.set_resource_directive(
        project_id, body.path, body.purpose, body.reading_mode, author=_request_user(request)
    )


@router.delete("/{project_id}/resources")
def delete_resource(project_id: str, path: str, request: Request):
    require_owner(project_id, request)
    return workspaces.delete_resource(project_id, path, author=_request_user(request))


class ProjectMcpBody(BaseModel):
    disabled: list[str]  # 本项目停用的全局 MCP 服务器名


@router.get("/{project_id}/mcp")
def get_project_mcp(project_id: str, request: Request):
    from ..services import mcp_settings

    require_owner(project_id, request)
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
    require_owner(project_id, request)
    names = [str(n)[:32] for n in body.disabled][:64]
    workspaces._update_meta(project_id, mcp_disabled=names)
    return {"disabled": names}


class ProjectEngineBody(BaseModel):
    engine: str  # claude | jiuwen


@router.get("/{project_id}/engine")
def get_project_engine(project_id: str, request: Request):
    from ..services.engines import available_engines, get_engine_class

    require_owner(project_id, request)
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

    require_owner(project_id, request)
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

    root = require_owner(project_id, request)
    return studio.graph(root)


@router.get("/{project_id}/studio/elements/{element_id}/flow")
def studio_operation_flow(project_id: str, element_id: str, request: Request):
    from ..services import studio

    root = require_owner(project_id, request)
    try:
        return studio.operation_flow(root, element_id)
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.get("/{project_id}/studio/element")
def studio_element(project_id: str, path: str, request: Request):
    from ..services import studio
    from ..services.workspace import _content_revision

    root = require_owner(project_id, request)
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

    require_owner(project_id, request)
    try:
        return studio.acquire_lease(project_id, body.path, _request_user(request) or "本机用户")
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.delete("/{project_id}/studio/lease")
def studio_release_lease(project_id: str, path: str, request: Request):
    from ..services import studio

    require_owner(project_id, request)
    studio.release_lease(project_id, path, _request_user(request) or "本机用户")
    return {"ok": True}


@router.get("/{project_id}/studio/draft")
def studio_get_draft(project_id: str, path: str, request: Request):
    from ..services import studio

    require_owner(project_id, request)
    return {"draft": studio.get_draft(project_id, path)}


@router.put("/{project_id}/studio/draft")
def studio_save_draft(project_id: str, body: StudioDraftBody, request: Request):
    from ..services import studio

    require_owner(project_id, request)
    try:
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

    require_owner(project_id, request)
    studio.discard_draft(project_id, path, _request_user(request) or "本机用户")
    return {"ok": True}


@router.post("/{project_id}/studio/submit")
async def studio_submit(project_id: str, body: StudioLeaseBody, request: Request):
    """草稿提交分流：确定性检查通过直接落版本；超范围转 agent 复核；硬伤拒绝。"""
    from ..services import studio
    from ..services.agent_sessions import manager

    root = require_owner(project_id, request)
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

    require_owner(project_id, request)
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

    from ..services.script_env import minimal_env

    root = require_owner(project_id, request)
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
            env=minimal_env(),
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
    root = require_owner(project_id, request)
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
    require_owner(project_id, request)
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
    require_owner(project_id, request)
    return workspaces.delete_file(project_id, path, author=_request_user(request))


@router.get("/{project_id}/versions")
def versions(project_id: str, request: Request):
    require_owner(project_id, request)
    return {"versions": workspaces.versions(project_id)}


@router.post("/{project_id}/versions/revert")
def revert(project_id: str, body: RevertBody, request: Request):
    require_owner(project_id, request)
    return workspaces.revert_to(project_id, body.commit)


@router.post("/{project_id}/versions/tag")
def tag_version(project_id: str, body: TagBody, request: Request):
    require_owner(project_id, request)
    return workspaces.tag_version(project_id, body.commit, body.label)


@router.delete("/{project_id}/versions/tag/{tag_name}")
def delete_tag(project_id: str, tag_name: str, request: Request):
    require_owner(project_id, request)
    return workspaces.delete_tag(project_id, tag_name)
