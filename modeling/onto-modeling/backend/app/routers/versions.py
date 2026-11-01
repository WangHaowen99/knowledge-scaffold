"""版本与标签（API 契约 3.9）：V-01 版本列表、V-06 回退、V-07 标记、V-08 删除标记。

主线版本只计合入主线的提交（--first-parent），版本号 n 从 1 开始，界面显示 v0.{n}。
版本详情、文件、对比与回退预览（V-02～V-05）属于以后阶段。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..api import deps
from ..api.errors import ApiError
from ..services import mainline

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


@router.post("/revert")
async def revert(project_id: str, request: Request):
    """整树恢复到目标版本，之后新增的文件一并删除；生成新版本，历史保留。"""
    root = deps.require_writable(project_id, request)
    body = await deps.json_body(request)
    result = deps.workspaces.revert_to(project_id, str(body.get("commit") or ""))
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
