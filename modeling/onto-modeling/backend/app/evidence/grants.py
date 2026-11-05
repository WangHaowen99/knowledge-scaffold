"""项目对数据源的授权（契约 EV-11；方案 E11）：存数据目录，不放工作区（智能体能改工作区）。

文件 `DATA_DIR/evidence/grants/<项目>.json`：`{"items": [{source_id, authorized, granted_at, granted_by}]}`。
取证节点是否出现（`rev.evidence`）、采集前的授权检查都读这里。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .. import config

_lock = threading.Lock()


def _path(project_id: str) -> Path:
    return Path(config.DATA_DIR) / "evidence" / "grants" / f"{project_id}.json"


def items(project_id: str) -> list[dict]:
    try:
        data = json.loads(_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [dict(item) for item in data.get("items") or [] if isinstance(item, dict) and item.get("source_id")]


def authorized_ids(project_id: str) -> list[str]:
    return [item["source_id"] for item in items(project_id) if item.get("authorized")]


def set_grant(project_id: str, source_id: str, authorized: bool, *, by: str) -> dict:
    with _lock:
        current = {item["source_id"]: item for item in items(project_id)}
        entry = {"source_id": source_id, "authorized": bool(authorized),
                 "granted_at": time.time() if authorized else None, "granted_by": by if authorized else None}
        current[source_id] = entry
        path = _path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"items": list(current.values())}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return entry


def drop_source(source_id: str) -> None:
    """删除登记时，把各项目里这个源的授权一起撤掉。"""
    root = Path(config.DATA_DIR) / "evidence" / "grants"
    if not root.is_dir():
        return
    for path in root.glob("*.json"):
        project_id = path.stem
        if any(item["source_id"] == source_id for item in items(project_id)):
            set_grant(project_id, source_id, False, by="")
