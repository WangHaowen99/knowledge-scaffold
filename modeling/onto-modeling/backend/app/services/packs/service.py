"""插件运行时里要动工作区 git 的操作：持项目锁，只按路径提交锁文件（不带 agent 的其他改动）。

锁文件的改动作为一次提交合入主线，带 `Onto-Source: plugin`（契约 PL-06；变更集在 P2，
那时改成自动合入的变更集，可撤回）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..workspace.gitops import _commit_paths
from . import lockfile
from .assembler import AssemblyError

_log = logging.getLogger(__name__)

PLUGIN_TRAILER = "Onto-Source: plugin"


def commit_lock(root: Path, message: str, actor: str = "") -> str:
    """提交锁文件（调用方持项目锁）；没有改动返回空串。"""
    trailer = f"\n\n{PLUGIN_TRAILER}" + (f"\nOnto-Author: {actor}" if actor else "")
    return _commit_paths(Path(root), [lockfile.LOCK_REL], message + trailer)


def reconcile_all(manager) -> dict:
    """启动核对（方案 D4、8.1「启动恢复」）：逐个项目核对装配目录，不对就重新装配。

    在会话开始之前调用：清掉入库、装配中途停机留下的临时目录；没有锁文件的存量项目写一份
    并作为一次系统提交（方案 2.3 第 2 步）；`.claude` 指错、缺失、是旧版绝对链接，或装配目录
    内容被改过，都按锁文件重新装配并切换。单个项目失败只记日志，不挡启动。
    """
    runtime = manager.packs
    report = {"repaired": [], "locked": [], "failed": {}}
    runtime.repository.cleanup_staging()
    runtime.assembler.cleanup_staging()
    if runtime.core_version() is None:
        return report
    for root in manager._project_dirs():
        project_id = root.name
        try:
            with manager._project_lock(project_id):
                before = runtime.assembler.active(root)
                _, wrote = runtime.ensure_lock(root)
                if wrote and (root / ".git").is_dir() and commit_lock(root, "chore: 写入能力包锁文件"):
                    report["locked"].append(project_id)
                status = runtime.reconcile(root)
                if status and (before or {}).get("assembly_hash") != status.get("active_hash"):
                    report["repaired"].append(project_id)
        except (AssemblyError, OSError, ValueError, RuntimeError) as exc:
            report["failed"][project_id] = str(exc)
            _log.warning("项目 %s 启动核对装配目录失败：%s", project_id, exc)
        except Exception as exc:  # noqa: BLE001 - git 超时等，单个项目失败不能挡住服务启动
            report["failed"][project_id] = str(exc)
            _log.warning("项目 %s 启动核对装配目录失败：%s", project_id, exc)
    if report["repaired"]:
        _log.warning("启动时重新装配了项目：%s", ", ".join(report["repaired"]))
    return report


def prepare_for_message(manager, project_id: str, *, follow: bool = True) -> dict:
    """会话宿主在下一条消息送达前调用（这个项目没有正在跑的一轮）：跟随通道、装配并切换。

    装配失败不挡消息：保持原装配目录，错误记进 `.onto/run/assembly.json`，PL-11 能看到。
    """
    runtime = manager.packs
    with manager._project_lock(project_id):
        root = manager.project_root(project_id)
        if runtime.core_version() is None:
            return {}
        _, wrote = runtime.ensure_lock(root)
        changed = runtime.follow(root) if follow else False
        if (wrote or changed) and (root / ".git").is_dir():
            commit_lock(root, "chore: 跟随通道升级能力包" if changed else "chore: 写入能力包锁文件")
        try:
            return runtime.apply(root)
        except AssemblyError as exc:
            _log.warning("项目 %s 装配失败，保持原版本：%s", project_id, exc)
            return runtime.status(root)


def desired_parts(manager, project_id: str) -> dict:
    """锁文件要求的装配对会话指纹的贡献：H1 内容哈希与流程哈希（只读，不切换）。"""
    runtime = manager.packs
    try:
        root = manager.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目已删除等：不参与指纹
        return {}
    try:
        lock = lockfile.read(root)
        if lock is None:
            active = runtime.assembler.active(root) or {}
            return {"assembly": active.get("h1_hash") or "", "flow": active.get("flow_hash") or ""}
        plan = runtime.assembler.plan(lock)
    except (AssemblyError, ValueError, OSError):
        # 锁文件要求的版本装不起来：按当前生效的算，不因为一次失败的切换反复重连
        active = runtime.assembler.active(root) or {}
        return {"assembly": active.get("h1_hash") or "", "flow": active.get("flow_hash") or ""}
    built = runtime.assembler.dir_for(plan.hash) / "MANIFEST.json"
    flow = ""
    try:
        import json

        flow = json.loads(built.read_text(encoding="utf-8")).get("flow_hash") or ""
    except (OSError, ValueError):
        pass
    return {"assembly": plan.h1_hash, "flow": flow}
