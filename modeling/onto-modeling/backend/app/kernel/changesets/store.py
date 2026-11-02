"""变更集记录（契约 7.6 Changeset 的存储）：每个项目一个 JSON 文件，放数据目录 `changeset-state/<项目>.json`。

不进 git（记录的是主线之外的东西：副本里的改动、确认与撤回的过程），也不放在 agent 能写的目录里。
变更集编号按项目递增（`cs-<n>`），同一时刻最多一个没关闭的变更集（`current`：陪伴建模的一轮修改，
或自主构建的一片——同一项目同一时刻只有一个在线引擎）。插件变更集不占 current，用 `record_closed` 直接记成已关闭。
关闭的保留最近 KEEP 个，供 CS-01 列表与版本列表反查。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from .worktree import data_dir_of

STATES = ("open", "pending_confirm", "merged", "discarded", "conflict")
OPEN_STATES = ("open", "pending_confirm", "conflict")
KEEP = 200

_lock = threading.RLock()


def _path(root: Path) -> Path:
    root = Path(root)
    return data_dir_of(root) / "changeset-state" / f"{root.name}.json"


def _load(root: Path) -> dict:
    try:
        data = json.loads(_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seq": 0, "current": None, "items": []}
    if not isinstance(data, dict):
        return {"seq": 0, "current": None, "items": []}
    data.setdefault("seq", 0)
    data.setdefault("current", None)
    data.setdefault("items", [])
    return data


def _save(root: Path, data: dict) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    closed = [item for item in data["items"] if item.get("state") not in OPEN_STATES]
    if len(closed) > KEEP:
        drop = {item["id"] for item in sorted(closed, key=lambda item: item.get("updated_at") or 0)[:-KEEP]}
        data["items"] = [item for item in data["items"] if item["id"] not in drop]
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def all_records(root: Path) -> list[dict]:
    """新的在前。"""
    with _lock:
        items = list(_load(root)["items"])
    return sorted(items, key=lambda item: item.get("created_at") or 0, reverse=True)


def get(root: Path, changeset_id: str) -> dict | None:
    with _lock:
        return next((dict(item) for item in _load(root)["items"] if item["id"] == changeset_id), None)


def current(root: Path) -> dict | None:
    """没关闭的助手变更集（开着、待确认、合入冲突）。"""
    with _lock:
        data = _load(root)
        found = next((item for item in data["items"] if item["id"] == data.get("current")), None)
        return dict(found) if found and found.get("state") in OPEN_STATES else None


def _append(data: dict, fields: dict) -> dict:
    data["seq"] = int(data.get("seq") or 0) + 1
    now = time.time()
    record = {
        "id": f"cs-{data['seq']}", "source": "assistant", "build_mode": "L2", "purpose": "",
        "state": "open", "base": "", "head": "", "conversation_id": None, "run_id": None, "slice_id": None,
        "created_at": now, "updated_at": now, "merged": None, "closed_at": None, "entity_count": 0,
        "entries": [], "artifacts": [], "paths": [], "changes": [], "nodes": [], "execs": [], "cards": [],
        "conflict": None, "checks": None, "change_list_id": None, "affected": [], "needs_review": False,
        **fields,
    }
    data["items"].append(record)
    return record


def create(root: Path, **fields) -> dict:
    with _lock:
        data = _load(root)
        record = _append(data, fields)
        data["current"] = record["id"]
        _save(root, data)
        return dict(record)


def record_closed(root: Path, **fields) -> dict:
    """只追加一条已关闭（merged / discarded）的记录，不动 current：插件变更这类「校验通过即合入」的变更集，
    助手那一轮还没确认时也能记下，不挤掉它的位置。"""
    if fields.get("state") in OPEN_STATES or fields.get("state") not in STATES:
        raise ValueError(f"只能记已关闭的变更集，state 不能是 {fields.get('state')!r}")
    with _lock:
        data = _load(root)
        record = _append(data, {"closed_at": time.time(), **fields})
        _save(root, data)
        return dict(record)


def update(root: Path, changeset_id: str, **fields) -> dict | None:
    with _lock:
        data = _load(root)
        record = next((item for item in data["items"] if item["id"] == changeset_id), None)
        if record is None:
            return None
        record.update(fields)
        record["updated_at"] = time.time()
        if record.get("state") not in OPEN_STATES and data.get("current") == changeset_id:
            data["current"] = None
        _save(root, data)
        return dict(record)


def purge(root: Path) -> None:
    with _lock:
        try:
            _path(root).unlink()
        except OSError:
            pass
