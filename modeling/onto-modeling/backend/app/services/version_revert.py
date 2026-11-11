"""版本回退的并发保护与 MCP 回退凭据（ADR-0094）。

恢复哪些内容由工作区定（`workspace/versions.py` 的 rollback_managed、revert_to）；这里管什么时候不能回退、
回退前后对助手会话的处理，以及 MCP 的「预览 → 确认」两步：

- 挡住回退的情况（预览时列出，执行时持会话锁、项目锁再查一次）：写入范围锁（助手这一轮没结束或合入有冲突）、
  助手还有没跑完的消息、自主构建在跑、Studio 有暂存草稿、主线工作区有没提交的业务改动；
  预览之后主线前进了也不回退（head_moved）。
- 回退之后回收助手会话、全部对话的续接点作废：模型上下文里还是回退前的内容。
- MCP 预览凭据绑定项目、Token、用户、目标版本与预览时的主线版本，只存在进程内存（不把待执行的破坏性操作落盘），
  十分钟过期、服务重启作废；执行过的一小时内重放拿到同一结果。
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid

from .onto_mcp.errors import McpError
from .workspace.errors import WorkspaceError
from .workspace.versions import managed_dirty_paths, rollback_changes

PREVIEW_TTL_SECONDS = 600
APPLIED_TTL_SECONDS = 3600
MAX_PREVIEWS = 1000
PAGE_MAX = 100
_STATUS = {"write_scope_locked": 423}
# 接口错误码 → MCP 大写错误码（沿用 develop 的 MCP 版本工具，客户端按它判断）
_MCP_CODES = {
    "write_scope_locked": "PROJECT_EDITING_BUSY",
    "session_busy": "PROJECT_EDITING_BUSY",
    "studio_draft_pending": "PROJECT_EDITING_BUSY",
    "autobuild_running": "AUTOBUILD_ACTIVE",
    "workspace_dirty": "WORKSPACE_DIRTY",
    "head_moved": "REVISION_CONFLICT",
    "nothing_to_revert": "NOTHING_TO_REVERT",
    "invalid_commit": "VERSION_NOT_FOUND",
}
_previews: dict[str, dict] = {}
_previews_lock = threading.Lock()


# ---- 挡住回退的情况 ----


def _session_busy(project_id: str) -> bool:
    from .agent_sessions import _busy, manager as sessions

    session = sessions.get(project_id)
    return bool(session and (_busy(session) or session.pending_prompts or session.chaining))


def blockers(manager, project_id: str) -> list[dict]:
    """现在不能回退的原因 [{code, message, details}]，空表示可以回退。"""
    from ..kernel import metamodel
    from ..kernel.changesets import service as changesets
    from . import studio
    from .autobuild import store as autobuild_store

    root = manager.project_root(project_id)
    out = []
    lock = changesets.scope_lock(manager, project_id)
    if lock.get("state") not in (None, "none"):
        out.append({"code": "write_scope_locked", "message": lock.get("reason_text") or "助手这一轮还没结束",
                    "details": lock})
    elif _session_busy(project_id):
        out.append({"code": "session_busy", "message": "助手还有没跑完的消息", "details": {}})
    run = autobuild_store.active(project_id)
    if run is not None:
        out.append({"code": "autobuild_running", "message": "自主构建运行中", "details": {"run_id": run["run_id"]}})
    drafts = studio.draft_paths(project_id)
    if drafts:
        out.append({"code": "studio_draft_pending", "message": "Studio 里有没保存的草稿", "details": {
            "path": drafts[0], "entry_id": metamodel.for_project(root).entry_key(drafts[0]) or None,
            "count": len(drafts)}})
    dirty = managed_dirty_paths(root)
    if dirty:
        out.append({"code": "workspace_dirty", "message": "项目里有没保存成版本的修改",
                    "details": {"paths": dirty[:20]}})
    return out


def _raise_first(found: list[dict]) -> None:
    if found:
        first = found[0]
        raise WorkspaceError(first["message"], _STATUS.get(first["code"], 409), first["code"], first["details"])


# ---- 执行 ----


async def apply(manager, project_id: str, commit: str, *, expected_head: str = "", author: str = "") -> dict:
    """回退到 commit：持会话锁（这期间不会开新的一轮）查一遍、在项目锁里回退时再查一遍，之后回收会话。"""
    from . import conversation_store
    from .agent_sessions import manager as sessions

    def busy_check(_root) -> None:
        _raise_first(blockers(manager, project_id))

    async with sessions._lock:
        _raise_first(await asyncio.to_thread(blockers, manager, project_id))
        result = await asyncio.to_thread(manager.revert_to, project_id, commit, author, expected_head, busy_check)
        session = sessions.get(project_id)
        if session is not None:
            session.pending_prompts.clear()
            await sessions._recycle(session, reason="user")
    conversation_store.clear_all_resume_tokens(project_id)
    await sessions._drain_waiters()
    return result


# ---- MCP：版本列表、预览、确认回退 ----


def _mcp_error(exc: WorkspaceError) -> McpError:
    return McpError(_MCP_CODES.get(exc.code, exc.code.upper()), str(exc), exc.status)


def _mcp_blockers(found: list[dict]) -> list[dict]:
    return [{"code": _MCP_CODES.get(item["code"], item["code"].upper()), "message": item["message"]} for item in found]


def _mainline(manager, project_id: str) -> list[dict]:
    """主线版本，从新到旧（MCP 的版本号就是界面上 v0.N 的 N）。"""
    return [{"version": entry["n"], "version_label": f"v0.{entry['n']}", "commit": entry["commit"],
             "time": entry["time"], "source": entry["source"], "author": entry["author"],
             "message": entry["message"], "release": entry["release"]}
            for entry in manager.versions(project_id)]


def _cursor(head: str, offset: int) -> str:
    raw = json.dumps({"head": head, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _offset(cursor: str, head: str) -> int:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        offset = value["offset"]
        if not isinstance(value.get("head"), str) or type(offset) is not int or offset < 0:
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise McpError("INVALID_CURSOR", "版本游标格式无效") from None
    if value["head"] != head:
        raise McpError("CURSOR_REVISION_MISMATCH", "项目版本已变化，请重新读取第一页", 409)
    return offset


def list_versions(manager, project_id: str, limit: int = 50, cursor: str = "") -> dict:
    """list_project_versions：主线版本分页，游标绑定主线版本，主线前进后要从第一页重读。"""
    if not 1 <= limit <= PAGE_MAX:
        raise McpError("INVALID_INPUT", f"版本页大小必须在 1 至 {PAGE_MAX} 之间")
    entries = _mainline(manager, project_id)
    head = entries[0]["commit"] if entries else ""
    offset = _offset(cursor, head) if cursor else 0
    page = entries[offset:offset + limit]
    more = offset + len(page) < len(entries)
    return {
        "revision": head, "current_head": head, "current_version": entries[0]["version"] if entries else 0,
        "items": page, "total": len(entries), "next_cursor": _cursor(head, offset + len(page)) if more else None,
        "display": {"text": f"已返回 {len(page)} 个版本，共 {len(entries)} 个版本"},
    }


def _purge(now: float) -> None:
    for key, record in list(_previews.items()):
        if record["expires"] <= now and record["state"] != "executing":
            del _previews[key]


def _remember(record: dict) -> str:
    preview_id = uuid.uuid4().hex
    with _previews_lock:
        _purge(record["created"])
        if len(_previews) >= MAX_PREVIEWS:
            idle = sorted((item for item in _previews.items() if item[1]["state"] != "executing"),
                          key=lambda item: item[1]["created"])
            if not idle:
                raise McpError("REVERT_PREVIEW_CAPACITY_REACHED", "版本回退请求较多，请稍后重试", 503)
            del _previews[idle[0][0]]
        _previews[preview_id] = record
    return preview_id


def preview_by_version(manager, project_id: str, version: int, token_id: str, username: str) -> dict:
    """preview_project_version_revert：算出要动的文件、列出挡住回退的情况；能回退时给 preview_id。"""
    from . import version_views

    entries = _mainline(manager, project_id)
    entry = next((item for item in entries if item["version"] == version), None)
    if entry is None:
        raise McpError("VERSION_NOT_FOUND", "目标版本不存在", 404)
    current = entries[0]
    target = {key: entry[key] for key in ("version", "version_label", "commit", "message")}
    now = {key: current[key] for key in ("version", "version_label", "commit")}
    if entry is current:
        raise McpError("NOTHING_TO_REVERT", "当前已是该版本，无需回退")
    found = blockers(manager, project_id)
    if found:
        return {"ready": False, "target": target, "current": now, "blockers": _mcp_blockers(found),
                "display": {"text": "当前项目有挡住回退的情况，暂不能回退"}}
    root = manager.project_root(project_id)
    try:
        view = version_views.revert_preview(root, entry["commit"], current["commit"])
    except WorkspaceError as exc:
        raise _mcp_error(exc) from exc
    paths = rollback_changes(root, entry["commit"], current["commit"])
    created = set(paths["create"])
    changes = {"added": paths["create"], "modified": [rel for rel in paths["restore"] if rel not in created],
               "deleted": paths["delete"]}
    changes["total"] = sum(len(value) for value in changes.values())
    stamp = time.time()
    preview_id = _remember({
        "project_id": project_id, "token_id": token_id, "username": username, "target_version": version,
        "target_commit": entry["commit"], "expected_head": current["commit"], "created": stamp,
        "expires": stamp + PREVIEW_TTL_SECONDS, "state": "pending", "result": None,
    })
    return {
        "ready": True, "preview_id": preview_id, "expires_at": stamp + PREVIEW_TTL_SECONDS,
        "target": target, "current": now, "expected_head": current["commit"], "changes": changes,
        "undone_versions": view["undone_versions"], "packs_lock_changes": view["packs_lock_changes"],
        "display": {"text": f"将回退到 {entry['version_label']}，并生成新版本 {view['next_version_label']}"},
    }


def _take(preview_id: str, project_id: str, token_id: str, username: str) -> dict:
    with _previews_lock:
        _purge(time.time())
        record = _previews.get(preview_id)
        if record is None:
            raise McpError("REVERT_PREVIEW_EXPIRED", "回退预览不存在或已过期", 409)
        if (record["project_id"], record["token_id"], record["username"]) != (project_id, token_id, username):
            raise McpError("REVERT_PREVIEW_FORBIDDEN", "回退预览不属于当前项目或 Token", 403)
        if record["state"] == "executing":
            raise McpError("REVERT_IN_PROGRESS", "该回退正在执行，请稍后查询", 409)
        if record["state"] == "pending":
            record["state"] = "executing"
        return record


def _settle(preview_id: str, state: str, result: dict | None = None) -> None:
    with _previews_lock:
        record = _previews.get(preview_id)
        if record is not None:
            record["state"], record["result"] = state, result
            if state == "applied":
                record["expires"] = time.time() + APPLIED_TTL_SECONDS


async def apply_preview(manager, project_id: str, preview_id: str, token_id: str, username: str) -> dict:
    """revert_project_version：按预览执行；执行过的同一 preview_id 再调直接返回上次的结果。"""
    record = _take(preview_id, project_id, token_id, username)
    if record["state"] == "applied":
        return record["result"]
    try:
        done = await apply(manager, project_id, record["target_commit"], expected_head=record["expected_head"],
                           author=username)
    except WorkspaceError as exc:
        _settle(preview_id, "pending")
        raise _mcp_error(exc) from exc
    except BaseException:
        _settle(preview_id, "pending")
        raise
    latest = _mainline(manager, project_id)[0]
    result = {
        "commit": done["commit"], "reverted_to_version": record["target_version"],
        "created_version": latest["version"], "version_label": latest["version_label"],
        "previous_head": record["expected_head"],
        "display": {"text": f"已回退到 v0.{record['target_version']}，生成新版本 {latest['version_label']}"},
    }
    _settle(preview_id, "applied", result)
    return result
