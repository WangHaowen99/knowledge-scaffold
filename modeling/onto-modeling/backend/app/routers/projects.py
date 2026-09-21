import asyncio
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
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
    background: str = ""  # 一句话目标说明（项目卡片展示）


class RenameBody(BaseModel):
    name: str


class WriteFileBody(BaseModel):
    path: str
    content: str
    base_revision: str = ""  # 读取时拿到的 revision；不一致则 409 拒绝覆盖


class RevertBody(BaseModel):
    commit: str


class TagBody(BaseModel):
    commit: str
    label: str


class MembersBody(BaseModel):
    members: dict[str, str]  # username -> editor(建模者) | viewer(只读评审)


class ArchiveBody(BaseModel):
    archived: bool


def _request_user(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return (user or {}).get("sub", "")


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
    """dry_run=true 时只返回压缩包摘要（导入向导的确认步骤），不落盘。"""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        if dry_run:
            return workspaces.import_summary(tmp_path)
        return workspaces.import_zip(name, tmp_path, owner=_request_user(request))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/transfer-import")
async def transfer_import(request: Request, file: UploadFile = File(...)):
    """导入项目迁移包（导出的完整工作区 zip，含版本历史）。"""
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return workspaces.import_transfer_zip(tmp_path, owner=_request_user(request))
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.get_project(project_id)


@router.patch("/{project_id}")
def rename_project(project_id: str, body: RenameBody, request: Request):
    ensure_owner(project_id, request)
    return workspaces.rename_project(project_id, body.name)


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request):
    from ..services.agent_sessions import manager

    ensure_owner(project_id, request)
    await manager.close(project_id)
    return workspaces.delete_project(project_id)


@router.post("/{project_id}/archive")
def archive_project(project_id: str, body: ArchiveBody, request: Request):
    ensure_owner(project_id, request)
    return workspaces.set_archived(project_id, body.archived)


@router.post("/{project_id}/clone")
def clone_project(project_id: str, request: Request):
    ensure_access(project_id, request)
    return workspaces.clone_project(project_id, owner=_request_user(request))


@router.get("/{project_id}/members")
def get_members(project_id: str, request: Request):
    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    return {"owner": meta.get("owner", ""), "members": meta.get("members") or {}}


@router.put("/{project_id}/members")
def put_members(project_id: str, body: MembersBody, request: Request):
    ensure_owner(project_id, request)
    meta = workspaces.set_members(project_id, body.members)
    return {"owner": meta.get("owner", ""), "members": meta.get("members") or {}}


@router.get("/{project_id}/export")
def export_project(project_id: str, request: Request, scope: str = "ontology"):
    ensure_access(project_id, request)
    if scope not in ("ontology", "workspace", "package"):
        raise WorkspaceError("scope 必须是 ontology、workspace 或 package")
    meta = workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp()) / f"{meta['name']}-{scope}.zip"
    if scope == "package":
        workspaces.export_package_zip(project_id, dest)
    else:
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
    }


class ResourceDirectiveBody(BaseModel):
    path: str
    purpose: str
    reading_mode: str = "on_demand"


@router.patch("/{project_id}/resource-meta")
def set_resource_meta(project_id: str, body: ResourceDirectiveBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.set_resource_directive(
        project_id, body.path, body.purpose, body.reading_mode
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


class ProjectEngineBody(BaseModel):
    engine: str  # claude | jiuwen


@router.get("/{project_id}/engine")
def get_project_engine(project_id: str, request: Request):
    from ..services.engines import get_engine_class
    from ..services.engines.jiuwen import JIUWEN_AVAILABLE

    ensure_access(project_id, request)
    meta = workspaces.get_project(project_id)
    current = meta.get("engine") or "claude"
    return {
        "engine": get_engine_class(current).name,
        "available": {"claude": True, "jiuwen": JIUWEN_AVAILABLE},
    }


@router.put("/{project_id}/engine")
async def put_project_engine(project_id: str, body: ProjectEngineBody, request: Request):
    """切换会话引擎（灰度开关）。切换即终止当前会话：界面历史保留可回放，
    下一条消息用新引擎启动（Jiuwen 经历史回放续接上下文，Claude 全新会话）。"""
    from ..services.agent_sessions import manager
    from ..services.engines import get_engine_class

    ensure_access(project_id, request, write=True)
    name = (body.engine or "").strip()
    if name not in ("claude", "jiuwen"):
        return JSONResponse(status_code=400, content={"error": "未知引擎"})
    engine_cls = get_engine_class(name)
    if not engine_cls.available():
        return JSONResponse(
            status_code=400,
            content={"error": getattr(engine_cls, "UNAVAILABLE_MESSAGE", "引擎不可用")},
        )
    workspaces._update_meta(project_id, engine=name)
    await manager.close(project_id)
    return {"engine": name}


@router.get("/{project_id}/studio/graph")
def studio_graph(project_id: str, request: Request):
    from ..services import studio

    root = ensure_access(project_id, request)
    return studio.graph(root)


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
        return studio.acquire_lease(project_id, body.path, _request_user(request) or "本机用户")
    except studio.StudioError as exc:
        raise WorkspaceError(str(exc), exc.status)


@router.delete("/{project_id}/studio/lease")
def studio_release_lease(project_id: str, path: str, request: Request):
    from ..services import studio

    ensure_access(project_id, request, write=True)
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
def versions(project_id: str, request: Request):
    ensure_access(project_id, request)
    return {"versions": workspaces.versions(project_id)}


@router.post("/{project_id}/versions/revert")
def revert(project_id: str, body: RevertBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.revert_to(project_id, body.commit)


@router.post("/{project_id}/versions/tag")
def tag_version(project_id: str, body: TagBody, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.tag_version(project_id, body.commit, body.label)


@router.delete("/{project_id}/versions/tag/{tag_name}")
def delete_tag(project_id: str, tag_name: str, request: Request):
    ensure_access(project_id, request, write=True)
    return workspaces.delete_tag(project_id, tag_name)
