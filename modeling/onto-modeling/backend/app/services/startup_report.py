"""启动自检报告（详设 4.9 目标 4、8.3，P0 验收；API 契约 O-35）。

启动时、任何会话开始之前：清掉各项目 .git 下残留的锁，列出各项目没提交的改动（不替用户提交，
等所有者决定），写进服务日志，并留存到数据目录供运维接口读取。只记路径与计数，不读文件内容。
启动恢复注册表（方案 B8）落地后，这里的步骤并进注册表。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)

REPORT_FILE = "startup-report.json"
MAX_PATHS_PER_PROJECT = 10
MAX_PROJECTS_LISTED = 50


def _path() -> Path:
    return Path(config.DATA_DIR) / REPORT_FILE


def _lock_item(manager, removed: list[Path]) -> dict:
    if not removed:
        return {
            "id": "index_lock", "title": "残留的 git 锁", "detail": "没有残留的锁",
            "result": "ok", "tone": "ok", "locks": [],
        }
    locks = sorted(str(path.relative_to(manager.workspaces_dir)) for path in removed)
    projects = sorted({lock.split("/", 1)[0] for lock in locks})
    return {
        "id": "index_lock",
        "title": "残留的 git 锁",
        "detail": f"已清理 {len(locks)} 个残留锁，涉及 {len(projects)} 个项目",
        "result": "cleared",
        "tone": "fix",
        "locks": locks[: MAX_PROJECTS_LISTED * 3],
    }


def _uncommitted_item(manager) -> dict:
    from .workspace import _changed_paths

    projects, failed = [], []
    for root in manager._project_dirs():
        try:
            changed = _changed_paths(root, ["."])
        except Exception as exc:  # 单个项目读不出状态不挡启动，记进报告
            failed.append(root.name)
            _log.warning("启动自检读取 %s 的未提交改动失败：%s", root.name, exc)
            continue
        if changed:
            projects.append({
                "project_id": root.name,
                "count": len(changed),
                "paths": sorted(changed)[:MAX_PATHS_PER_PROJECT],
            })
    if not projects and not failed:
        return {
            "id": "uncommitted", "title": "未提交的改动", "detail": "各项目都没有未提交的改动",
            "result": "ok", "tone": "ok", "projects": [],
        }
    total = sum(item["count"] for item in projects)
    detail = f"{len(projects)} 个项目有 {total} 处未提交的改动，保留原样等所有者处理"
    if failed:
        detail += f"；{len(failed)} 个项目读取失败"
    return {
        "id": "uncommitted",
        "title": "未提交的改动",
        "detail": detail,
        "result": "failed" if failed and not projects else "awaiting_user",
        "tone": "warn",
        "projects": projects[:MAX_PROJECTS_LISTED],
        "failed": failed[:MAX_PROJECTS_LISTED],
    }


def run(manager) -> dict:
    """清残留锁、列未提交改动，写日志并留存；返回报告。只在启动时、会话开始之前调用。"""
    from ..governance import lifecycle
    from ..kernel.writepath import recover_intents

    last_exit = lifecycle.mark_started()
    removed = manager.clear_git_locks()
    # 写入事务留下的意图：锁清掉之后补提交，再列剩下的未提交改动（详设 8.3 措施 2）
    items = [_lock_item(manager, removed), recover_intents(manager), _uncommitted_item(manager)]
    report = {"at": time.time(), "last_exit": last_exit, "items": items}
    for item in items:
        level = logging.WARNING if item["tone"] != "ok" else logging.INFO
        _log.log(level, "启动自检 · %s：%s", item["title"], item["detail"])
        for project in item.get("projects") or []:
            _log.log(level, "  %s：%d 处，例如 %s", project["project_id"], project["count"], "、".join(project["paths"][:3]))
    try:
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        _log.warning("启动自检报告写盘失败：%s", exc)
    return report


def latest() -> dict | None:
    """最近一次启动自检报告（时间仍是 epoch 秒，接口层转 RFC 3339）。"""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
