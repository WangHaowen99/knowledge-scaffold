"""版本与标签（API 契约 3.9）：V-01 版本列表、V-02 详情、V-03 某版本的文件、V-04 对比、V-05 回退预览、
V-06 回退、V-07 标记、V-08 删除标记。

主线版本只计合入主线的提交（--first-parent），版本号 n 从 1 开始，界面显示 v0.{n}。
`/diff`、`/revert-preview` 先于 `/{commit}` 登记。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..api import deps
from ..api.errors import ApiError
from ..services import mainline, version_views
from ..services.workspace.errors import WorkspaceError
from ..services.workspace.layout import _content_revision

router = APIRouter(prefix="/api/v1/projects/{project_id}/versions", tags=["versions"])

SOURCES = ("assistant", "web", "studio", "mcp", "autobuild", "import", "plugin", "revert")
SOURCE_LABELS = {
    "assistant": "助手", "web": "网页", "studio": "Studio", "mcp": "MCP", "autobuild": "自主构建",
    "import": "导入", "plugin": "系统", "revert": "回退",
}
LABEL_MAX = 120


def _version(entry: dict, head: str) -> dict:
    trailer = entry["trailer"]
    return {
        "commit": entry["commit"],
        "n": entry["n"],
        "version_label": mainline.label(entry["n"]),
        "at": deps.iso(entry["time"]),
        "source": entry["source"],
        "source_label": SOURCE_LABELS.get(entry["source"], entry["source"]),
        "author": entry["author"],
        "summary": entry["message"],
        "tags": entry["tags"],
        "release": entry["release"],
        "current": entry["commit"] == head,
        "changeset_id": trailer.get("Onto-Changeset"),
        "run": None,
        "needs_review": 0,
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
    items = [_version(entry, state["head"]) for entry in entries if not wanted or entry["source"] in wanted]
    page = deps.page(items, cursor, limit, {"source": sorted(wanted)})
    return {
        "items": page["items"], "next_cursor": page["next_cursor"], "total": page["total"],
        "head": state["head"], "current_version_label": state["version_label"],
    }


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
        raise ApiError("nothing_to_revert", "当前内容与该版本一致，无需回退", 400)
    try:
        return version_views.revert_preview(root, target, head)
    except WorkspaceError as exc:
        raise ApiError(exc.code or "invalid_argument", str(exc), exc.status) from exc


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
                while chunk := archive.read(1024 * 1024):
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
    view = _version(entry, mainline.state(root)["head"])
    return {**view, "changes": version_views.changes(deps.workspaces, root, target), "run": None}


@router.post("/revert")
async def revert(project_id: str, request: Request):
    """整树恢复到目标版本，之后新增的文件一并删除；生成新版本，历史保留。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = deps.workspaces.revert_to(project_id, str(body.get("commit") or ""), author=deps.actor(request))
    target = result["reverted_to"]
    return {
        "commit": result["commit"],
        "version_label": mainline.state(root)["version_label"],
        "reverted_to": {"commit": target, "version_label": mainline.label(mainline.count(root, target))},
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
