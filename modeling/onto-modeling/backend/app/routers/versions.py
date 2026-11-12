"""版本与标签（API 契约 3.9）：V-01 版本列表、V-02 详情、V-03 某版本的文件、V-04 对比、V-05 回退预览、
V-06 回退、V-07 标记、V-08 删除标记。

主线版本只计合入主线的提交（--first-parent），版本号 n 从 1 开始，界面显示 v0.{n}。
`/diff`、`/revert-preview` 先于 `/{commit}` 登记。
"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..api import deps
from ..api.errors import ApiError
from ..services import mainline, version_revert, version_views, workspace_labels
from ..services.workspace.errors import WorkspaceError
from ..services.workspace.layout import _content_revision

router = APIRouter(prefix="/api/v1/projects/{project_id}/versions", tags=["versions"])
history_router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["versions"])

SOURCES = ("assistant", "web", "studio", "mcp", "autobuild", "import", "plugin", "revert")
SOURCE_LABELS = {
    "assistant": "助手", "web": "网页", "studio": "Studio", "mcp": "MCP", "autobuild": "自主构建",
    "import": "导入", "plugin": "系统", "revert": "回退",
}
LABEL_MAX = 120


def _autobuild_slices(project_id: str) -> dict[str, dict]:
    """自主构建合入的段：变更集 → {run, needs_review}（契约 7.6 Version.run，F3 补充第 19 条）。"""
    from ..services.autobuild import store, views

    index = {}
    for run in store.runs(project_id):
        pending: dict[str, int] = {}
        for entry in views.live_ledger(run):
            if entry.get("review_state") == "pending":
                slice_id = (entry.get("slice") or {}).get("id")
                pending[slice_id] = pending.get(slice_id, 0) + 1
        for unit in run.get("units") or []:
            info = (run.get("slices") or {}).get(unit["id"]) or {}
            if info.get("changeset_id") and info.get("commit"):
                index[info["changeset_id"]] = {
                    "run": {"run_id": run["run_id"], "run_no": run["run_no"], "slice_id": unit["id"],
                            "slice_name": unit["name"]},
                    "needs_review": pending.get(unit["id"], 0)}
    return index


def _summary(message: str) -> str:
    """版本说明给人看的那份（F4b-paths）：来源另有一栏，去掉提交信息里的来源前缀；路径换成业务名。"""
    text = re.sub(r"^(?:revert|web|knowledge|studio|mcp|import|clone|init|chore|feedback|autobuild|agent):\s*", "",
                  message or "")
    return workspace_labels.humanize(text) or message


def _releases(project_id: str, entries: list[dict]) -> tuple[dict[str, str], str]:
    """发布记录归到版本上（ADR-0094）：{提交: 版本号}，同一提交发过几次取最新的。记录没带提交时按发布时间
    归到当时的最新版本。读不到（知识包 Hub 不可用等）时版本照列，第二项是给界面的原因。"""
    from ..services import publish

    try:
        rows = publish.releases(deps.workspaces, project_id)
    except Exception as exc:  # noqa: BLE001 - 发布记录读不到不挡版本列表
        return {}, str(exc) if isinstance(exc, (WorkspaceError, ApiError)) else "发布记录读取失败"
    out: dict[str, str] = {}
    for row in rows:  # 从新到旧
        commit = row.get("commit")
        if not commit and isinstance(row.get("released_at"), (int, float)):
            commit = next((entry["commit"] for entry in entries  # entries 从新到旧
                           if entry["time"] is not None and entry["time"] <= row["released_at"]), None)
        if commit:
            out.setdefault(commit, row["version"])
    return out, ""


def _version(entry: dict, head: str, autobuild: dict | None = None, releases: dict | None = None) -> dict:
    trailer = entry["trailer"]
    slice_run = (autobuild or {}).get(trailer.get("Onto-Changeset") or "") if entry["source"] == "autobuild" else None
    return {
        "commit": entry["commit"],
        "n": entry["n"],
        "version_label": mainline.label(entry["n"]),
        "at": deps.iso(entry["time"]),
        "source": entry["source"],
        "source_label": SOURCE_LABELS.get(entry["source"], entry["source"]),
        "author": entry["author"],
        "summary": _summary(entry["message"]),
        "tags": entry["tags"],
        "release": (releases or {}).get(entry["commit"]) or entry["release"],
        "current": entry["commit"] == head,
        "changeset_id": trailer.get("Onto-Changeset"),
        "run": (slice_run or {}).get("run"),
        "needs_review": (slice_run or {}).get("needs_review", 0),
        "trailer": {
            "source": trailer.get("Onto-Source", ""),
            "author": trailer.get("Onto-Author", ""),
            **({"changeset": trailer["Onto-Changeset"]} if "Onto-Changeset" in trailer else {}),
            **({"packs": trailer["Onto-Packs"]} if "Onto-Packs" in trailer else {}),
            **({"node": trailer["Onto-Node"]} if "Onto-Node" in trailer else {}),
        },
    }


@router.get("")
def list_versions(project_id: str, request: Request, source: str = "", cursor: str = "", limit: int = 0):
    root = deps.require_owner(project_id, request)
    wanted = set(filter(None, source.split(",")))
    if wanted - set(SOURCES):
        raise ApiError("invalid_argument", "source 取值不合法", 400, {"fields": {"source": "enum"}})
    state = mainline.state(root)
    entries = deps.workspaces.versions(project_id)
    autobuild = _autobuild_slices(project_id)
    releases, release_error = _releases(project_id, entries)
    items = [_version(entry, state["head"], autobuild, releases)
             for entry in entries if not wanted or entry["source"] in wanted]
    page = deps.page(items, cursor, limit, {"source": sorted(wanted)})
    return {
        "items": page["items"], "next_cursor": page["next_cursor"], "total": page["total"],
        "head": state["head"], "current_version_label": state["version_label"], "release_error": release_error,
    }


@history_router.get("/history")
def history(project_id: str, request: Request, limit: int = 50, cursor: str = ""):
    """V-09 历史时间线（ADR-0094，develop /history）：迭代与 Hub 发布版本按时间从新到旧合并分页，游标由后端生成。

    迭代条目另带 V-01 的版本字段（来源、说明、标记、所属运行），给版本页左栏用。"""
    from ..services import auth, version_history
    from .hub_packages import identity

    root = deps.require_owner(project_id, request)
    # 未启用统一账号时没有个人 Hub 包，时间线只含迭代
    token, subject = identity(request)[:2] if auth.ENABLED else ("", "")
    if not 1 <= limit <= 50:
        raise ApiError("invalid_argument", "分页参数无效", 400, {"fields": {"limit": "range"}})
    try:
        result = version_history.page(deps.workspaces, project_id, subject, token, limit, cursor, can_revert=True)
    except WorkspaceError as exc:
        if exc.status == 400:
            raise ApiError("invalid_cursor", str(exc), 400) from exc
        raise
    head = mainline.state(root)["head"]
    autobuild = _autobuild_slices(project_id)
    result["items"] = [{**item, **_version(item, head, autobuild)} if item["kind"] == "iteration" else item
                       for item in result["items"]]
    return result


def _on_mainline(project_id: str, commit: str) -> dict:
    """主线上的这个版本（V-01 的一项）；不在主线上（如副本分支的提交）时 invalid_commit。"""
    entry = next((item for item in deps.workspaces.versions(project_id) if item["commit"] == commit), None)
    if entry is None:
        raise ApiError("invalid_commit", "不是这个项目版本历史里的版本", 400)
    return entry


def _resolve(root, rev: str) -> str:
    try:
        return version_views.resolve(root, rev)
    except WorkspaceError as exc:
        raise ApiError(exc.code or "invalid_commit", str(exc), exc.status) from exc


@router.get("/diff")
def diff(project_id: str, request: Request, path: str = "", entry: str = ""):
    """V-04：from 版本与 to 版本（或 WORKTREE 当前文件）里同一份文件的左右两侧与改动块。"""
    from .files import _guard_path, worktree_file
    from .studio import entry_path

    root = deps.require_owner(project_id, request)
    params = request.query_params
    if bool(path) == bool(entry):
        raise ApiError("invalid_argument", "path 与 entry 二选一", 400, {"fields": {"path": "one_of", "entry": "one_of"}})
    rel = _guard_path(path) if path else entry_path(root, entry)[0]
    source = _resolve(root, params.get("from") or "")
    to = params.get("to") or "WORKTREE"
    left = version_views.text_at(root, source, rel)
    right_revision = None
    if to == "WORKTREE":
        target = worktree_file(root, rel)
        right = None
        if target.is_file():
            data = target.read_bytes()
            right = data[:version_views.DIFF_BYTES_MAX].decode("utf-8", errors="replace")
            right_revision = _content_revision(data)
        to_view = {"commit": None, "version_label": None}
    else:
        target_commit = _resolve(root, to)
        right = version_views.text_at(root, target_commit, rel)
        to_view = {"commit": target_commit, "version_label": mainline.label(mainline.count(root, target_commit))}
    if left is None and right is None:
        raise ApiError("file_not_found", "两个版本里都没有这个文件", 404, {"path": rel})
    return {
        "from": {"commit": source, "version_label": mainline.label(mainline.count(root, source))},
        "to": to_view, "path": rel, "left": left or "", "right": right or "", "right_revision": right_revision,
        "hunks": version_views.hunks(left or "", right or ""),
    }


@router.get("/revert-preview")
def revert_preview(project_id: str, request: Request, commit: str = ""):
    """V-05：回退到这个版本会恢复、删除哪些文件，撤回哪些版本，能力锁怎么变。"""
    root = deps.require_owner(project_id, request)
    target = _resolve(root, commit)
    head = mainline.state(root)["head"]
    if target == head:
        raise ApiError("nothing_to_revert", "当前已是该版本，无需回退", 400)
    try:
        deps.workspaces.revert_target(root, target)
        view = version_views.revert_preview(root, target, head)
    except WorkspaceError as exc:
        raise ApiError(exc.code or "invalid_argument", str(exc), exc.status) from exc
    found = version_revert.blockers(deps.workspaces, project_id)
    return {**view, "ready": not found, "blockers": found}


@router.get("/{commit}/files")
def version_files(project_id: str, commit: str, request: Request, prefix: str = "ontology/", download: bool = False):
    """V-03：某个版本 prefix 下的文件列表；download=1 时打成 zip 下载（可用 ?token=）。"""
    root = deps.require_owner(project_id, request)
    target = _resolve(root, commit)
    prefix = prefix.strip().lstrip("/")
    if ".." in prefix.split("/"):
        raise ApiError("path_forbidden", "路径越界", 403, {"path": prefix})
    if download:
        # 下载只打知识包内容（ontology/）：别的目录（资料、.onto 等）不在下载范围
        if not (prefix == "ontology" or prefix.startswith("ontology/")):
            raise ApiError("invalid_argument", "只能下载 ontology/ 下的文件", 400, {"fields": {"prefix": "scope"}})
        label = mainline.label(mainline.count(root, target))
        name = f"{deps.workspaces._read_meta(root).get('name') or project_id}-{label}-ontology.zip"
        try:
            archive = version_views.zip_files(root, target, prefix)
        except WorkspaceError as exc:
            raise ApiError(exc.code or "internal_error", str(exc), exc.status, exc.details) from exc

        def chunks():
            with archive:
                while True:
                    chunk = archive.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(chunks(), media_type="application/zip",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
    return {"items": version_views.files(root, target, prefix)}


@router.get("/{commit}")
def version_detail(project_id: str, commit: str, request: Request):
    """V-02：版本加这个版本相对主线上一个版本改了什么（条目与插件产物按实体认，其他按文件）。"""
    root = deps.require_owner(project_id, request)
    target = _resolve(root, commit)
    entry = _on_mainline(project_id, target)
    view = _version(entry, mainline.state(root)["head"],
                    releases=_releases(project_id, deps.workspaces.versions(project_id))[0])
    return {**view, "changes": version_views.changes(deps.workspaces, root, target), "run": None}


@router.post("/revert")
async def revert(project_id: str, request: Request):
    """业务内容整份恢复到目标版本，之后新增的文件一并删除；生成新版本，历史保留。

    带 expected_head（V-05 给的）时，预览之后主线前进了就 409 head_moved；挡住回退的情况同 V-05 的 blockers。"""
    from ..services.autobuild import store as autobuild_store

    root = deps.require_writable(project_id, request)
    autobuild_store.ensure_no_run(project_id)
    body = await deps.json_body(request)
    result = await version_revert.apply(deps.workspaces, project_id, str(body.get("commit") or ""),
                                        expected_head=str(body.get("expected_head") or ""),
                                        author=deps.actor(request))
    target = result["reverted_to"]
    return {
        "commit": result["commit"],
        "version_label": mainline.state(root)["version_label"],
        "reverted_to": {"commit": target, "version_label": mainline.label(mainline.count(root, target))},
        "changes": result["changes"],
        **({"warning": result["warning"]} if result.get("warning") else {}),
    }


@router.post("/tags")
async def create_tag(project_id: str, request: Request):
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    label = str(body.get("label") or "").strip()
    if not 1 <= len(label) <= LABEL_MAX:
        raise ApiError("invalid_argument", f"标记名称须为 1–{LABEL_MAX} 个字符", 400, {"fields": {"label": "length"}})
    tag = deps.workspaces.tag_version(project_id, str(body.get("commit") or ""), label)
    return JSONResponse(status_code=201, content={
        **tag, "version_label": mainline.label(mainline.count(root, tag["commit"])),
    })


@router.delete("/tags/{name}", status_code=204)
def delete_tag(project_id: str, name: str, request: Request):
    """只能删用户标记（mark-*）；发布标签 release/* 不能删。"""
    deps.require_writable(project_id, request)
    deps.workspaces.delete_tag(project_id, name)
    return Response(status_code=204)
