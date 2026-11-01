"""启动自检报告（详设 4.9 目标 4、8.3，P0 验收；API 契约 O-35）与内核这一侧的启动恢复步骤。

步骤登记在启动恢复注册表（governance/startup_recovery，方案 B8），这里给出工作区、写入路径、插件运行时
与外部依赖几步的实现：清残留 git 锁、补提交写入意图、列未提交改动（不替用户提交，等所有者决定）、
存量数据迁移、核对装配目录、探测外部依赖、清扫删到一半的目录。只记路径与计数，不读文件内容。
报告写进服务日志，并留存到数据目录供运维接口读取。
"""

from __future__ import annotations

import json
import logging
import threading
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


def _assembly_item(manager) -> dict:
    """插件运行时启动核对（方案 D4）：镜像自带的 core 入库，逐个项目按锁文件核对、修复装配目录。"""
    from .packs import service as packs_service

    result = packs_service.reconcile_all(manager)
    repaired, failed, locked = result.get("repaired") or [], result.get("failed") or {}, result.get("locked") or []
    if failed:
        return {"id": "assembly", "title": "装配目录", "result": "failed", "tone": "warn",
                "detail": f"{len(failed)} 个项目装配不了，保持原装配" + (f"；重新装配了 {len(repaired)} 个" if repaired else ""),
                "projects": [{"project_id": pid, "count": 1, "paths": [str(reason)[:200]]} for pid, reason in
                             sorted(failed.items())][:MAX_PROJECTS_LISTED]}
    if repaired or locked:
        return {"id": "assembly", "title": "装配目录", "result": "reassembled", "tone": "fix",
                "detail": f"重新装配了 {len(repaired)} 个项目" + (f"，为 {len(locked)} 个存量项目写了锁文件" if locked else ""),
                "projects": [{"project_id": pid, "count": 1, "paths": []} for pid in sorted(set(repaired) | set(locked))]}
    return {"id": "assembly", "title": "装配目录", "result": "ok", "tone": "ok", "detail": "各项目的装配目录与锁文件一致"}


def _dependencies_item(_manager) -> dict:
    """外部依赖（模型网关、认证、沙箱、磁盘、git）启动时探测一次；结果同 O-13，缓存 30 秒。"""
    from ..governance import health

    checks = health.probe(force=True)
    failed = [item for item in checks if item["status"] == "fail"]
    warned = [item for item in checks if item["status"] in ("warn", "slow")]
    if failed:
        names = "、".join(item["name"] for item in failed)
        return {"id": "dependencies", "title": "外部依赖", "result": "failed", "tone": "warn",
                "detail": f"{len(failed)} 项不可达：{names}", "checks": checks}
    detail = f"{len(checks)} 项可达" + (f"，{len(warned)} 项有提醒" if warned else "")
    return {"id": "dependencies", "title": "外部依赖", "result": "ok", "tone": "ok", "detail": detail, "checks": checks}


def _purge_item(manager) -> dict:
    """上次删除项目时没删净的目录（文件被占用、删到一半停机）。"""
    count = manager.purge_leftovers()
    if count:
        return {"id": "purge", "title": "删到一半的项目", "result": "cleared", "tone": "fix",
                "detail": f"清掉了 {count} 个残留目录"}
    return {"id": "purge", "title": "删到一半的项目", "result": "ok", "tone": "ok", "detail": "没有残留目录"}


def _quiet(func):
    """只做事、不进报告的步骤（存量数据迁移）。"""
    def step(manager):
        func(manager)
        return None

    return step


def register_steps(register) -> None:
    """内核这一侧的启动恢复步骤（顺序：先清锁再动 git；迁移在会话开始之前；核对装配最后）。"""
    from ..kernel.writepath import recover_intents
    from . import eval_retirement, ownership

    register("index_lock", "残留的 git 锁", lambda manager: _lock_item(manager, manager.clear_git_locks()), order=10)
    # 写入事务留下的意图：锁清掉之后补提交，再列剩下的未提交改动（详设 8.3 措施 2）
    register("write_intents", "写入意图", recover_intents, order=20)
    register("uncommitted", "未提交的改动", _uncommitted_item, order=30)
    # 元数据迁移要改各项目的真实索引，不能和 agent 的 git 并发
    register("state_tracking", "工具状态停止跟踪", _quiet(lambda manager: manager.migrate_state_tracking()), order=40)
    # 单人所有权：删掉存量项目的成员表（原值记审计），日志报告待管理员指定所有者的无主项目
    register("single_owner", "单人所有权迁移", _quiet(ownership.migrate_single_owner), order=50)
    # 发布按提交打包：旧版上传没进 git 的资料补提交一次
    register("pending_uploads", "补提交旧版上传", _quiet(lambda manager: manager.commit_pending_uploads()), order=60)
    # 评测移出（ADR-0020）：幂等，迁过的项目什么都不做
    register("eval_retirement", "评测数据迁移", _quiet(eval_retirement.migrate), order=70)
    register("assembly", "装配目录", _assembly_item, order=80)
    # 耗时、不挡就绪的放后台
    register("dependencies", "外部依赖", _dependencies_item, order=300, background=True)
    register("purge", "删到一半的项目", _purge_item, order=310, background=True)


def run(manager) -> dict:
    """跑全部启动恢复步骤（含后台的，按顺序同步跑完）并留存报告；返回报告。"""
    from ..governance import startup_recovery

    return startup_recovery.run(manager)


_report_lock = threading.Lock()


def save(report: dict) -> None:
    try:
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        _log.warning("启动自检报告写盘失败：%s", exc)


def add_item(item: dict) -> None:
    """后台步骤做完后补进报告（同 ID 的替换）。"""
    with _report_lock:
        report = latest() or {"at": time.time(), "last_exit": None, "items": []}
        report["items"] = [existing for existing in report.get("items") or [] if existing.get("id") != item.get("id")]
        report["items"].append(item)
        save(report)


def latest() -> dict | None:
    """最近一次启动自检报告（时间仍是 epoch 秒，接口层转 RFC 3339）。"""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
