"""Shared version listing, rollback preview and rollback orchestration."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid
from pathlib import Path

from .workspace import WorkspaceError, _git

PREVIEW_TTL_SECONDS = 600
APPLIED_PREVIEW_TTL_SECONDS = 3600
MAX_PREVIEW_RECORDS = 1000
_previews: dict[str, dict] = {}
_previews_lock = threading.RLock()


def _error(code: str, message: str, status: int = 400) -> WorkspaceError:
    return WorkspaceError(message, status, code)


def _role_allowed(manager, project_id: str, username: str) -> bool:
    meta = manager.get_project(project_id)
    role = manager.role_of(meta, username, False)
    return role in ("owner", "admin") or (not meta.get("owner") and role is not None)


def require_owner(manager, project_id: str, username: str) -> None:
    if not _role_allowed(manager, project_id, username):
        raise _error("PROJECT_ROLE_FORBIDDEN", "仅项目所有者或管理员可执行版本回退", 403)


def _head(manager, project_id: str) -> str:
    return _git(manager.project_root(project_id), "rev-parse", "HEAD").strip()


def _version_entry(manager, project_id: str, version: int) -> dict:
    if type(version) is not int or version < 1:
        raise _error("VERSION_NOT_FOUND", "目标版本不存在", 404)
    latest = manager.versions(project_id, limit=1)
    total = latest[0]["version"] if latest else 0
    if version > total:
        raise _error("VERSION_NOT_FOUND", "目标版本不存在", 404)
    entries = manager.versions(project_id, limit=1, offset=total - version)
    if not entries or entries[0]["version"] != version:
        raise _error("VERSION_NOT_FOUND", "目标版本不存在", 404)
    return entries[0]


def _encode_cursor(head: str, offset: int) -> str:
    raw = json.dumps({"head": head, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(value, dict) or not isinstance(value.get("head"), str):
            raise ValueError
        if type(value.get("offset")) is not int or value["offset"] < 0:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise _error("INVALID_CURSOR", "版本游标格式无效") from None


def list_versions(manager, project_id: str, limit: int = 50, cursor: str = "") -> dict:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise _error("INVALID_INPUT", "版本页大小无效") from None
    if limit < 1 or limit > 100:
        raise _error("INVALID_INPUT", "版本页大小必须在 1 至 100 之间")
    head = _head(manager, project_id)
    offset = 0
    if cursor:
        data = _decode_cursor(cursor)
        if data["head"] != head:
            raise _error("CURSOR_REVISION_MISMATCH", "项目版本已变化，请重新读取第一页", 409)
        offset = data["offset"]
    entries = manager.versions(project_id, limit=limit, offset=offset)
    latest = manager.versions(project_id, limit=1)
    total = latest[0]["version"] if latest else 0
    next_cursor = _encode_cursor(head, offset + len(entries)) if offset + len(entries) < total else None
    return {
        "revision": head,
        "current_version": total,
        "current_head": head,
        "items": entries,
        "total": total,
        "next_cursor": next_cursor,
        "display": {"text": f"已返回 {len(entries)} 个版本，共 {total} 个版本"},
    }


def _blocking_issues(project_id: str, root: Path) -> list[dict]:
    from .agent_sessions import manager as sessions
    from . import modeling_eval, solver_validation_run, studio

    issues = []
    if sessions.workspace_busy(project_id):
        issues.append({"code": "PROJECT_EDITING_BUSY", "message": "助手正在修改该项目，请等待当前任务结束"})
    if studio.project_editing_status(project_id):
        issues.append({"code": "PROJECT_EDITING_BUSY", "message": "Studio 存在租约或未提交草稿，请先完成或放弃编辑"})
    if modeling_eval.is_active(project_id):
        issues.append({"code": "EVALUATION_ACTIVE", "message": "数据评测正在运行或排队，请等待完成或先停止"})
    if solver_validation_run.is_active(project_id, root):
        issues.append({"code": "SOLVER_VALIDATION_ACTIVE", "message": "求解验证正在运行，请等待完成"})
    if manager_for_dirty(root):
        issues.append({"code": "WORKSPACE_DIRTY", "message": "项目存在未提交或暂存的修改，请先完成当前编辑"})
    return issues


def manager_for_dirty(root: Path) -> bool:
    """Check managed and staged paths without coupling the orchestrator to a manager instance."""
    from .workspace import WorkspaceManager
    managed = []
    status = _git(root, "status", "--porcelain")
    for line in status.splitlines():
        if len(line) < 3:
            continue
        path = line[3:].split(" -> ", 1)[-1].strip().strip('"')
        if WorkspaceManager._rollback_path_allowed(path):
            managed.append(path)
    return bool(managed)


def _ensure_idle(project_id: str, root: Path) -> None:
    issues = _blocking_issues(project_id, root)
    if issues:
        first = issues[0]
        raise _error(first["code"], first["message"], 409)


def _preview_payload(project_id: str, entry: dict, result: dict, head: str, token_id: str, username: str) -> dict:
    preview_id = uuid.uuid4().hex
    now = time.time()
    record = {
        "project_id": project_id,
        "token_id": token_id,
        "username": username,
        "target_version": entry["version"],
        "target_commit": entry["commit"],
        "expected_head": head,
        "changes": result["changes"],
        "created": now,
        "expires": now + PREVIEW_TTL_SECONDS,
        "state": "pending",
        "result": None,
    }
    with _previews_lock:
        _purge_locked(now)
        if len(_previews) >= MAX_PREVIEW_RECORDS:
            removable = sorted(
                (
                    (key, value)
                    for key, value in _previews.items()
                    if value["state"] != "executing"
                ),
                key=lambda item: item[1]["created"],
            )
            if removable:
                del _previews[removable[0][0]]
            else:
                raise _error(
                    "REVERT_PREVIEW_CAPACITY_REACHED",
                    "版本回退请求较多，请稍后重试",
                    503,
                )
        _previews[preview_id] = record
    return {
        "ready": True,
        "preview_id": preview_id,
        "expires_at": record["expires"],
        "target": {"version": entry["version"], "commit": entry["commit"], "message": entry["message"]},
        "current": {"version": result["current_version"], "commit": head},
        "expected_head": head,
        "changes": result["changes"],
        "display": {"text": f"将回退到版本 {entry['version']}，并创建新的版本 {result['current_version'] + 1}"},
    }


def _purge_locked(now: float) -> None:
    for key, value in list(_previews.items()):
        if value["expires"] <= now and value["state"] != "executing":
            del _previews[key]


def preview_by_version(manager, project_id: str, version: int, token_id: str, username: str) -> dict:
    require_owner(manager, project_id, username)
    root = manager.project_root(project_id)
    entry = _version_entry(manager, project_id, version)
    issues = _blocking_issues(project_id, root)
    if issues:
        return {
            "ready": False,
            "target": {"version": entry["version"], "commit": entry["commit"], "message": entry["message"]},
            "current": {"commit": _head(manager, project_id)},
            "blockers": issues,
            "display": {"text": "当前项目存在阻断项，暂不能回退"},
        }
    result = manager.preview_revert(project_id, entry["commit"])
    current = manager.versions(project_id, limit=1)
    result["current_version"] = current[0]["version"] if current else 0
    return _preview_payload(project_id, entry, result, result["expected_head"], token_id, username)


def preview_by_commit(manager, project_id: str, commit: str, username: str = "") -> dict:
    root = manager.project_root(project_id)
    require_owner(manager, project_id, username) if username else None
    resolved = manager._validate_revert_target(root, commit)
    current = manager.versions(project_id, limit=1)
    total = current[0]["version"] if current else 0
    entry = next((item for item in manager.versions(project_id, limit=100) if item["commit"] == resolved), None)
    if entry is None:
        target_version = int(_git(root, "rev-list", "--count", resolved).strip() or "0")
        entry = {"version": target_version, "commit": resolved, "message": ""}
    issues = _blocking_issues(project_id, root)
    if issues:
        return {"ready": False, "blockers": issues, "target": entry, "current": {"version": total, "commit": _head(manager, project_id)}}
    result = manager.preview_revert(project_id, resolved)
    result["current_version"] = total
    return {"ready": True, **result}


def _take_preview(preview_id: str, project_id: str, token_id: str, username: str) -> dict:
    now = time.time()
    with _previews_lock:
        _purge_locked(now)
        record = _previews.get(preview_id)
        if record is None:
            raise _error("REVERT_PREVIEW_EXPIRED", "回退预览不存在或已过期", 409)
        if record["project_id"] != project_id or record["token_id"] != token_id or record["username"] != username:
            raise _error("REVERT_PREVIEW_FORBIDDEN", "回退预览不属于当前项目或 Token", 403)
        if record["state"] == "applied":
            return {"record": record, "replay": True}
        if record["state"] == "executing":
            raise _error("REVERT_IN_PROGRESS", "该回退正在执行，请稍后查询", 409)
        record["state"] = "executing"
        return {"record": record, "replay": False}


def _mark_preview(preview_id: str, state: str, result=None) -> None:
    with _previews_lock:
        record = _previews.get(preview_id)
        if record:
            record["state"] = state
            record["result"] = result
            if state == "applied":
                record["expires"] = time.time() + APPLIED_PREVIEW_TTL_SECONDS


async def _apply_commit(manager, project_id: str, commit: str, expected_head: str, sessions) -> dict:
    from . import modeling_eval

    result = None
    try:
        with modeling_eval.block_launch(project_id):
            async with sessions._lock:
                root = manager.project_root(project_id)
                _ensure_idle(project_id, root)
                if sessions.workspace_busy(project_id):
                    raise _error("PROJECT_EDITING_BUSY", "助手正在修改该项目，请等待当前任务结束", 409)
                if expected_head and _head(manager, project_id) != expected_head:
                    raise _error("REVISION_CONFLICT", "项目已产生新版本，请重新预览后再回退", 409)

                def ensure_idle(current_root: Path) -> None:
                    _ensure_idle(project_id, current_root)

                result = await asyncio.to_thread(
                    manager.revert_to, project_id, commit, expected_head, ensure_idle
                )
                session = sessions.get(project_id)
                if session is not None:
                    session.pending_prompts.clear()
                    await sessions._recycle(session)
                from . import conversation_store
                conversation_store.clear_all_resume_tokens(project_id)
    except modeling_eval.EvaluationLaunchBlocked as exc:
        raise _error("EVALUATION_ACTIVE", str(exc), 409) from exc
    await sessions._drain_waiters()
    return result


async def apply_preview(manager, project_id: str, preview_id: str, token_id: str, username: str) -> dict:
    require_owner(manager, project_id, username)
    taken = _take_preview(preview_id, project_id, token_id, username)
    record = taken["record"]
    if taken["replay"]:
        return record["result"]
    try:
        result = await _apply_commit(manager, project_id, record["target_commit"], record["expected_head"], _sessions())
        result.update({
            "reverted_to_version": record["target_version"],
            "created_version": (manager.versions(project_id, limit=1) or [{}])[0].get("version"),
            "previous_head": record["expected_head"],
            "display": {"text": f"已恢复版本 {record['target_version']} 的业务内容，并创建新版本"},
        })
        _mark_preview(preview_id, "applied", result)
        return result
    except Exception:
        _mark_preview(preview_id, "pending")
        raise


def _sessions():
    from .agent_sessions import manager
    return manager


async def apply_direct(manager, project_id: str, commit: str, expected_head: str = "") -> dict:
    return await _apply_commit(manager, project_id, commit, expected_head, _sessions())
