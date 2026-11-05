"""项目（API 契约 3.4）：P-01～P-11、P-16。引擎、构建方式、MCP 服务器见 project_settings。"""

from __future__ import annotations

import asyncio
import tempfile
import unicodedata
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Body, Request
from fastapi.responses import FileResponse, JSONResponse

from ..api import deps, views
from ..api.errors import ApiError
from ..services import audit, ownership, package_archive, project_events, uploads
from ..services.name_conflict import MAX_DISPLAY_NAME_LENGTH
from ..services.workspace import NameConflictError, WorkspaceError, remove_tree
from ..services.workspace.layout import _slugify

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

GOAL_MAX = 2000  # 与 modeling 创建页一致；超长报错，不截断
STATUSES = ("all", "active", "autobuild", "publishable", "archived")
SORTS = ("recent", "name", "created")
BUILD_MODES = ("L2", "L4")
# 导入包格式（package_archive 的判别结果）→ 契约 P-10 的 kind
_KINDS = {
    package_archive.KIND_WORKSPACE_TRANSFER: ("transfer", "项目迁移包"),
    package_archive.KIND_KNOWLEDGE_PACKAGE: ("published_package", "已发布知识包"),
    package_archive.KIND_WORKSPACE_ONTOLOGY: ("workspace_archive", "工作区压缩包"),
    package_archive.KIND_UNKNOWN: ("unknown", "无法识别"),
}


def _viewer(request: Request) -> tuple[str, bool] | None:
    """同名校验的查重范围：同一用户下（含已归档）；鉴权关闭时为 None（全部项目）。"""
    user = deps.user(request)
    if user is None:
        return None
    return user.get("sub", ""), False


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ApiError("project_name_required", "项目名称不能为空", 400)
    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise ApiError("project_name_too_long", f"项目名称不能超过 {MAX_DISPLAY_NAME_LENGTH} 个字符", 400,
                       {"max": MAX_DISPLAY_NAME_LENGTH})
    return name


def _meta(project_id: str) -> dict:
    root = deps.workspaces.project_root(project_id)
    return views.project_meta(deps.workspaces.get_project(project_id), root)


def _slices(root) -> dict | None:
    """按段建模的进度；全量模式（分段待规划）或算不出时 None。"""
    from ..services.flow import model as flow_model

    try:
        return flow_model.slice_summary(root)
    except Exception:  # noqa: BLE001 - 列表上的概览，不挡列项目
        return None


_CARD_RUN_STATE = {"stopping": "running", "converting": "running", "interrupted": "paused",
                   "budget_exhausted": "stopped", "failed": "stopped", "converted": "completed"}


def _autobuild_card(project_id: str) -> tuple[dict | None, str | None]:
    """项目卡片上的自主构建（契约 7.3 ProjectCard.autobuild）：没结束的运行，或结束了还有要复核的那次。
    返回 (卡片字段, 状态标签 autobuild|autobuild_done)。"""
    from ..services.autobuild import store, views

    runs = store.runs(project_id)
    run = next((item for item in runs if item["state"] in store.ACTIVE), None)
    tag = "autobuild"
    if run is None:
        run = runs[0] if runs else None
        if run is None or not views.summary(run)["needs_review"]:
            return None, None
        tag = "autobuild_done"
    summary = views.summary(run)
    done, total = summary["slices"]["done"], summary["slices"]["total"]
    eta = summary["est_total_ms"] - summary["elapsed_ms"] if summary["est_total_ms"] else None
    return {"run_id": run["run_id"], "run_no": run["run_no"], "state": _CARD_RUN_STATE.get(run["state"], run["state"]),
            "slice_done": done, "slice_total": total, "progress_pct": round(done * 100 / total) if total else 0,
            "eta_ms": max(0, eta) if eta is not None else None, "decisions": summary["decisions"],
            "suspended": summary["suspended"], "needs_review": summary["needs_review"], "duration_ms": summary["elapsed_ms"]}, tag


def _card(meta: dict) -> dict:
    root = deps.workspaces.workspaces_dir / meta["id"]
    card = views.project_meta(meta, root)
    archived = bool(meta.get("archived"))
    autobuild, tag = (None, None) if archived else _autobuild_card(meta["id"])
    card.update({
        "industry_label": None,
        "slices": _slices(root),
        "inbox_count": 0,
        "status": ["archived"] if archived else ["active", *([tag] if tag else [])],
        "autobuild": autobuild,
        "publishable": None,
        "landing_page": "l4-run" if tag == "autobuild" else "wb-l2",
    })
    return card


# ---- P-01 列出项目 ----


@router.get("")
def list_projects(
    request: Request, status: str = "all", q: str = "", sort: str = "recent", cursor: str = "", limit: int = 0,
):
    """只列自己的项目（admin、ops 看别人的项目用 O-01）；本地模式列全部。"""
    if status not in STATUSES:
        raise ApiError("invalid_argument", "status 取值不合法", 400, {"fields": {"status": "enum"}})
    if sort not in SORTS:
        raise ApiError("invalid_argument", "sort 取值不合法", 400, {"fields": {"sort": "enum"}})
    claims = deps.user(request)
    metas = deps.workspaces._project_metas()
    if claims is not None:
        metas = [meta for meta in metas if ownership.owns(meta, claims.get("sub", ""))]
    cards = [_card(meta) for meta in metas]
    counts = {key: 0 for key in STATUSES}
    for card in cards:
        counts["archived" if card["archived"] else "all"] += 1
        if not card["archived"]:
            counts["active"] += 1
        if "autobuild" in card["status"]:
            counts["autobuild"] += 1
    needle = q.strip().lower()

    def wanted(card: dict) -> bool:
        if status == "archived":
            hit = card["archived"]
        elif status in ("all", "active"):
            hit = not card["archived"]
        else:
            hit = status in card["status"]
        if needle:
            hit = hit and (needle in (card["name"] or "").lower() or needle in (card["goal"] or "").lower())
        return hit

    items = [card for card in cards if wanted(card)]
    if sort == "name":
        items.sort(key=lambda card: card["name"] or "")
    elif sort == "created":
        items.sort(key=lambda card: card["created_at"] or "", reverse=True)
    else:
        items.sort(key=lambda card: card["last_activity_at"] or "", reverse=True)
    page = deps.page(items, cursor, limit, {"status": status, "q": needle, "sort": sort})
    return {"items": page["items"], "next_cursor": page["next_cursor"], "counts": counts}


# ---- P-02 新建项目、P-03 名称检查 ----


def _engine_or_default(engine: str | None) -> str | None:
    if not engine:
        return None
    from ..services.engines import ENGINE_NAMES, get_engine_class

    if engine not in ENGINE_NAMES or not get_engine_class(engine).available():
        raise ApiError("engine_unavailable", "所选引擎在当前部署不可用", 409, {"engine": engine})
    return engine


def _selected_packs(body: dict, viewer: str) -> list[str] | None:
    """新建向导选的包（契约 P-02）：行业包、系统知识包按名字启用（不存在 404 pack_not_found）；
    能力包 `capabilities: {包名: bool}` 没写到的取推荐值（清单 default_on），包仓库里没有的能力名忽略
    （前端目录可能比本部署多）。都没传时返回 None：锁文件按缺省能力包写。"""
    runtime = deps.workspaces.packs
    if runtime.core_version() is None:
        return None
    repository = runtime.repository
    selected: list[str] = []
    for field, category in (("industry_packs", "industry"), ("system_packs", "system")):
        items = body.get(field) or []
        if not isinstance(items, list):
            raise ApiError("invalid_argument", f"{field} 必须是列表", 400, {"fields": {field: "list"}})
        for item in items:
            name = str(item.get("name") if isinstance(item, dict) else item or "")
            version = repository.latest(name, "stable") if repository.versions(name) else None
            if not version or not repository.visible(name, viewer) or repository.manifest(name, version).category != category:
                raise ApiError("pack_not_found", f"包「{name}」不存在", 404, {"pack": name})
            selected.append(name)
    capabilities = body.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, dict):
        raise ApiError("invalid_argument", "capabilities 必须是「包名: 是否启用」", 400, {"fields": {"capabilities": "object"}})
    if capabilities is None and not selected:
        return None
    chosen = set(runtime.default_capabilities())
    for name, on in (capabilities or {}).items():
        version = repository.latest(str(name), "stable") if repository.versions(str(name)) else None
        if not version or not repository.visible(str(name), viewer) \
                or repository.manifest(str(name), version).category != "capability":
            continue
        (chosen.add if on else chosen.discard)(str(name))
    return sorted(chosen - {"core"}) + selected


def _pack_enabled(project_id: str, name: str) -> bool:
    from ..services.packs import lockfile

    try:
        lock = lockfile.read(deps.workspaces.project_root(project_id))
    except (ValueError, OSError):
        return False
    entry = lock.get(name) if lock else None
    return bool(entry and entry.enabled)


def _materials(body: dict, owner: str) -> list[dict]:
    items = []
    for item in body.get("materials") or []:
        if not isinstance(item, dict) or not item.get("upload_id"):
            raise ApiError("invalid_argument", "materials 每项要有 upload_id", 400, {"fields": {"materials": "upload_id"}})
        meta = uploads.get(item["upload_id"], owner, "material")
        purpose = item.get("purpose") or "auto"
        if purpose not in ("auto", "goal", "schema", "term", "evidence"):
            raise ApiError("invalid_argument", "资料用途不合法", 400, {"fields": {"materials.purpose": "enum"}})
        items.append({
            "upload_id": meta["upload_id"],
            "subdir": uploads.resource_subdir(meta["filename"]),
            "filename": meta["filename"],
            "data": uploads.read(meta["upload_id"]),
            "purpose": purpose,
        })
    return items


@router.post("")
def create_project(request: Request, body: dict = Body(...)):
    name = _check_name(body.get("name", ""))
    goal = str(body.get("goal") or "").strip()
    if not 1 <= len(goal) <= GOAL_MAX:
        raise ApiError("invalid_argument", f"目标须为 1–{GOAL_MAX} 个字符", 400, {"fields": {"goal": "length"}})
    build_mode = body.get("build_mode") or "L2"
    if build_mode not in BUILD_MODES:
        raise ApiError("invalid_argument", "build_mode 取值不合法", 400, {"fields": {"build_mode": "enum"}})
    packs = _selected_packs(body, deps.actor(request))
    engine = _engine_or_default(body.get("engine"))
    owner = deps.actor(request) if deps.user(request) is not None else ""
    materials = _materials(body, deps.actor(request))
    example = body.get("example")
    extra = {"goal": goal, "build_mode": build_mode, "created_from": "example" if example else "wizard"}
    if engine:
        extra["engine"] = engine
    with deps.workspaces.name_claim(name, _viewer(request)):
        meta = deps.workspaces.create_project(name, owner=owner, background=goal, packs=packs, **extra)
    if example:
        deps.workspaces.seed_example(meta["id"], author=deps.actor(request))
    if materials:
        deps.workspaces.import_resources(meta["id"], materials, "import: 新建项目资料", deps.username(request))
        for item in materials:
            uploads.consume(item["upload_id"])
    return JSONResponse(status_code=201, content={**_meta(meta["id"]), "landing_page": "wb-materials"})


def name_check(name: str, request: Request, exclude_project_id: str = "") -> dict:
    normalized = unicodedata.normalize("NFKC", (name or "").strip())
    result = {"available": True, "normalized": normalized, "package_name": _slugify(normalized), "conflict": None, "reason": None}
    if not normalized:
        return {**result, "available": False, "reason": "empty"}
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        return {**result, "available": False, "reason": "too_long"}
    try:
        deps.workspaces.check_name_available(name, _viewer(request), exclude_id=exclude_project_id)
    except NameConflictError as exc:
        return {**result, "available": False, "conflict": exc.conflict, "reason": "conflict"}
    return result


@router.get("/name-check")
def check_project_name(request: Request, name: str = "", exclude_project_id: str = ""):
    return name_check(name, request, exclude_project_id)


# ---- P-10 检查导入包、P-11 导入项目 ----


def _suggested_name(summary: dict, archive: Path, filename: str) -> str:
    if summary["kind"] == package_archive.KIND_WORKSPACE_TRANSFER:
        try:
            return deps.workspaces.transfer_archive_name(archive)
        except WorkspaceError:
            pass
    if summary.get("package_name"):
        return summary["package_name"]
    return Path(filename).stem or "导入项目"


def _inspection(archive: Path, filename: str) -> dict:
    summary = package_archive.inspect_archive(archive)
    kind, label = _KINDS[summary["kind"]]
    # 迁移包在 v1 里由导入接口自动识别（P-11），旧导入向导的"请走另一个入口"不再是阻断项
    blockers = [] if kind == "transfer" else [
        {"code": "unknown_format" if kind == "unknown" else ("no_objects" if "objects" in text else "solver_contract_invalid"),
         "message": text}
        for text in summary["blockers"]
    ]
    package = None
    if kind == "published_package":
        package = {"name": summary.get("package_name") or "", "version": summary.get("package_version") or "",
                   "schema_version": "0.2" if summary.get("solver_operations") else "0.1"}
    return {
        "summary": summary,
        "kind": kind,
        "kind_label": label,
        "root_dir": (summary.get("package_root") or "").rstrip("/"),
        "files": summary["files"],
        "total_size": summary["total_size"],
        "counts": {key: summary.get(key, 0) for key in ("objects", "operations", "rules", "metrics", "scripts", "solver_operations")},
        "package": package,
        "blockers": blockers,
        "warnings": [{"code": "skipped_entries", "message": f"这些顶层条目不会导入：{'、'.join(summary['skipped'])}"}]
        if summary.get("skipped") else [],
        "suggested_name": _suggested_name(summary, archive, filename),
    }


# 导入预览：包里的交付物落到哪些目录（P-10 counts 的键 → 工作区目录）
_COUNT_DIRS = {"objects": "ontology/object", "operations": "ontology/operation", "rules": "ontology/rule",
               "metrics": "ontology/metric", "scripts": "ontology/scripts", "solver_operations": "ontology/solver"}


def _start_node(counts: dict) -> dict | None:
    """导入后从哪个节点开始：把包里有交付物的目录当成主线上已有的文件，套一遍镜像主流程的完成判定与可补做
    （ADR-0084）。只看目录有没有文件，资料、报告这些包里不带的都算没有。"""
    from ..services.flow import model as flow_model
    from ..services.flow import overlay
    from ..services.packs import contracts

    flow = flow_model.image_flow()
    present = {directory for key, directory in _COUNT_DIRS.items() if counts.get(key)}
    probes = {name: "files" in spec and any(str(item) in present for item in spec["files"])
              for name, spec in (flow.get("probes") or {}).items()}
    hits = contracts.default().load("flow_state").node_hits(flow, Path("."), probes)
    return overlay.first_open(flow, hits)


def _staged_archive(upload_id: str, owner: str) -> tuple[dict, Path]:
    meta = uploads.get(upload_id, owner, "project_import")
    return meta, uploads.path(upload_id)


@router.post("/import/inspect")
def inspect_import(request: Request, body: dict = Body(...)):
    meta, archive = _staged_archive(str(body.get("upload_id") or ""), deps.actor(request))
    result = _inspection(archive, meta["filename"])
    name = (body.get("name") or "").strip() or result["suggested_name"]
    result.pop("summary")
    result["name_check"] = name_check(name, request)
    # 迁移包原样恢复进度，没有要推算的起点
    start = None if result["kind"] == "transfer" else _start_node(result["counts"])
    result["after_import"] = {
        "capabilities": [],
        "start_node": start["id"] if start else None,
        "start_title": start["title"] if start else None,
        "mark_needs_review": result["kind"] == "published_package",
    }
    result["evidence_sources"] = []
    return result


@router.post("/import")
async def import_project(request: Request, body: dict = Body(...)):
    """格式自动识别：迁移包原样恢复（含版本历史），已发布知识包反向转换，工作区压缩包原样导入。"""
    upload_id = str(body.get("upload_id") or "")
    meta, archive = _staged_archive(upload_id, deps.actor(request))
    name = _check_name(body.get("name", ""))
    strategy = body.get("name_strategy") or "exact"
    if strategy not in ("exact", "suggested"):
        raise ApiError("invalid_argument", "name_strategy 取值不合法", 400, {"fields": {"name_strategy": "enum"}})
    inspected = _inspection(archive, meta["filename"])
    if inspected["kind"] == "unknown" or inspected["blockers"]:
        raise ApiError("unsupported_archive_kind", "这个压缩包不能导入", 400, {"blockers": inspected["blockers"]})
    viewer = _viewer(request)
    if strategy == "suggested":
        check = name_check(name, request)
        if check["conflict"]:
            name = check["conflict"]["candidates"][0]
    owner = deps.username(request)
    kind = inspected["summary"]["kind"]
    build_mode = body.get("build_mode") or None
    if build_mode is not None and build_mode not in BUILD_MODES:
        raise ApiError("invalid_argument", "build_mode 取值不合法", 400, {"fields": {"build_mode": "enum"}})

    # 迁移包重名时候选名以包里记录的原名为基础编号
    original = inspected["suggested_name"] if kind == package_archive.KIND_WORKSPACE_TRANSFER else ""

    def claim_and_import() -> dict:
        # 查重与落盘在名称锁内串行；放进线程，等锁时不卡住事件循环
        with deps.workspaces.name_claim(name, viewer, importing=True, original_name=original):
            if kind == package_archive.KIND_WORKSPACE_TRANSFER:
                created = deps.workspaces.import_transfer_zip(archive, owner=owner, name=name)
            elif kind == package_archive.KIND_KNOWLEDGE_PACKAGE:
                created = deps.workspaces.import_package_zip(name, archive, owner=owner)
            else:
                created = deps.workspaces.import_zip(name, archive, owner=owner)
        deps.workspaces._update_meta(created["id"], created_from="import", **({"build_mode": build_mode} if build_mode else {}))
        return created

    created = await asyncio.to_thread(claim_and_import)
    uploads.consume(upload_id)
    if kind == package_archive.KIND_KNOWLEDGE_PACKAGE:
        # 导入已发布的知识包：带进来的产物没有经过本项目确认，已满足完成判定的节点标「需复核」（详设 6.6）
        from ..services.flow import state as node_state

        await asyncio.to_thread(node_state.mark_imported, deps.workspaces.project_root(created["id"]))
    return JSONResponse(status_code=201, content={**_meta(created["id"]), "landing_page": "wb-l2"})


# ---- P-04～P-09 ----


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    """所有者拿 ProjectMeta；admin、ops 看别人的项目只拿 ProjectMetaView（契约 1.4）。"""
    root = deps.workspaces.project_root(project_id)
    meta = deps.workspaces.get_project(project_id)
    claims = deps.user(request)
    if claims is None or ownership.owns(meta, claims.get("sub", "")):
        return views.project_meta(meta, root)
    if deps.roles(request) & {"admin", "ops"}:
        return views.project_meta_view(meta, root)
    raise ownership.denied(meta)


@router.patch("/{project_id}")
def update_project(project_id: str, request: Request, body: dict = Body(...)):
    """改名、改目标、调研范围；元数据不进工作区 git，不产生版本。"""
    deps.require_writable(project_id, request)
    meta = deps.workspaces.get_project(project_id)
    fields = {}
    if "goal" in body:
        goal = str(body.get("goal") or "").strip()
        if not 1 <= len(goal) <= GOAL_MAX:
            raise ApiError("invalid_argument", f"目标须为 1–{GOAL_MAX} 个字符", 400, {"fields": {"goal": "length"}})
        fields["goal"] = goal
    if "research_mode" in body:
        mode = body.get("research_mode")
        if mode not in ("web", "local"):
            raise ApiError("invalid_argument", "research_mode 取值不合法", 400, {"fields": {"research_mode": "enum"}})
        if mode == "web" and not _pack_enabled(project_id, "websearch"):
            # 联网调研是可选包（websearch）：没启用时不能切到联网模式
            raise ApiError("pack_not_enabled", "联网调研能力没有启用", 409, {"pack": "websearch"})
        fields["research_mode"] = mode
    if fields:
        deps.workspaces._update_meta(project_id, **fields)
    if "name" in body:
        name = _check_name(body.get("name", ""))
        if name != meta.get("name"):
            with deps.workspaces.name_claim(name, _viewer(request), exclude_id=project_id):
                deps.workspaces.rename_project(project_id, name)
    project_events.project_changed(project_id, sorted(key for key in ("name", "goal", "research_mode") if key in body))
    return _meta(project_id)


@router.delete("/{project_id}", status_code=202)
async def delete_project(
    project_id: str, request: Request, background: BackgroundTasks, body: dict | None = Body(None),
):
    """永久删除（不进回收站）：名称逐字确认；会话、排队消息立即硬杀，MCP token 全部吊销。

    响应前只做快的：硬杀会话、把工作区移出列表、清掉 Studio 草稿、吊销 token；
    目录递归删除与发布留存的删除放到响应之后（_finish_delete）。
    """
    from ..services import studio
    from ..services.agent_sessions import manager
    from ..services.onto_mcp import tokens

    deps.require_owner(project_id, request)
    meta = deps.workspaces.get_project(project_id)
    if (body or {}).get("confirm_name") != meta.get("name"):
        raise ApiError("confirm_name_mismatch", "输入的名称与项目名不一致", 400)
    from ..services.autobuild import store as autobuild_store

    autobuild_store.ensure_no_run(project_id)
    await manager.discard(project_id)
    staged = await asyncio.to_thread(deps.workspaces.stage_delete, project_id)
    await manager.discard(project_id)  # 移走期间并发发消息新建的会话一并清掉
    studio.purge_project(project_id)
    await asyncio.to_thread(tokens.revoke_project, deps.workspaces, project_id)
    background.add_task(_finish_delete, project_id, staged)
    project_events.project_removed(meta.get("owner") or "", project_id, "deleted")
    return JSONResponse(status_code=202, content={"deleted": project_id})


async def _finish_delete(project_id: str, staged: Path) -> None:
    """删除收尾（响应之后执行）。各项互不依赖，并发进行，一项失败不拖累其余；
    没清干净的记入运维错误日志（工作区目录的残留由下次启动清扫兜底）。"""
    from ..services import error_log, release_store
    from ..services.engines import jiuwen

    manager = deps.workspaces
    jobs = {
        "工作区目录": asyncio.to_thread(manager.purge_staged, staged),
        "引擎工件区": asyncio.to_thread(remove_tree, jiuwen.shadow_dir(project_id)),
        "发布留存": asyncio.to_thread(release_store.purge_project, manager.data_dir, project_id),
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
async def archive_project(project_id: str, request: Request, body: dict = Body(...)):
    """归档后只读：在线会话关闭，MCP change 工具集拒绝写入；取消归档不受只读限制。"""
    import time

    from ..services.agent_sessions import manager

    deps.require_owner(project_id, request)
    archived = bool(body.get("archived"))
    if archived:
        from ..services.autobuild import store as autobuild_store

        autobuild_store.ensure_no_run(project_id)
    deps.workspaces._update_meta(project_id, archived=archived, archived_at=time.time() if archived else None)
    if archived:
        await manager.close(project_id, all_conversations=True)
    project_events.project_changed(project_id, ["archived"])
    return _meta(project_id)


@router.post("/{project_id}/clone")
async def clone_project(project_id: str, request: Request, body: dict | None = Body(None)):
    """克隆项目：新项目只归调用者；缺省名「原名（副本）」，重名返回 409 与候选名。"""
    deps.require_owner(project_id, request)
    body = body or {}
    default_name = deps.workspaces.clone_name(project_id)
    name = _check_name(body.get("name") or default_name)
    owner = deps.username(request)
    include_history = body.get("include_history", True) is not False
    include_conversations = bool(body.get("include_conversations", False))

    def claim_and_clone() -> dict:
        with deps.workspaces.name_claim(name, _viewer(request), original_name=default_name):
            return deps.workspaces.clone_project(project_id, owner=owner, name=name, include_history=include_history)

    meta = await asyncio.to_thread(claim_and_clone)
    if include_conversations:
        _copy_conversations(project_id, meta["id"])
    return JSONResponse(status_code=201, content=_meta(meta["id"]))


def _copy_conversations(source: str, dest: str) -> None:
    """对话记录随克隆复制（续接点不复制：新项目的引擎从历史回放续上）。"""
    import shutil

    from ..services import conversation_store

    source_dir = conversation_store.history_path(source).parent
    if source_dir.is_dir():
        target = conversation_store.history_path(dest).parent
        shutil.copytree(source_dir, target, dirs_exist_ok=True)
        conversation_store.clear_all_resume_tokens(dest)


@router.get("/{project_id}/transfer-package")
def export_transfer_package(project_id: str, request: Request):
    """P-09：完整工作区（含版本历史）与项目元数据；不含工具状态、运行期文件与对话记录。"""
    deps.require_owner(project_id, request)
    meta = deps.workspaces.get_project(project_id)
    dest = Path(tempfile.mkdtemp(prefix="onto-transfer-")) / f"{meta['name']}-transfer.zip"
    deps.workspaces.export_zip(project_id, "workspace", dest)
    audit.log(deps.actor(request), "GET", request.url.path, {"project.export": project_id})
    return FileResponse(dest, filename=dest.name, media_type="application/zip")


# ---- P-16 项目用量 ----


@router.get("/{project_id}/usage")
def project_usage(project_id: str, request: Request):
    """全部对话（含已删除对话留存的用量）的累计；费用来自引擎回报的美元金额，单价表（B14）之后换算。"""
    from ..services.agent_sessions import usage_summary

    deps.require_owner(project_id, request)
    usage = usage_summary(project_id)
    cost = usage.get("total_cost_usd")
    return {
        "calls": usage.get("calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost": {"amount": round(cost, 2) if cost else (0.0 if cost == 0 else None), "currency": "USD"},
        "last_activity_at": deps.iso(usage.get("last_activity")),
    }
