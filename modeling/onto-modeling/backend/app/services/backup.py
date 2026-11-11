"""数据备份：恢复一套部署所需的状态打成一个 tar.gz，保留最近 keep_last() 份（缺省 BACKUP_KEEP，O-44 可改）。

包内布局与数据目录一一对应，恢复时 `data/` 解到 ONTO_WEB_DATA_DIR、`home/` 解到 HOME：

- data/workspaces/<项目>/   各项目工作区（含 .git），按项目持锁打包
- data/sessions/            会话历史、会话索引与续接点
- data/jiuwen/              Jiuwen 存档（SQLite 用 backup API 拷贝）与框架工件区
- data/studio/              Studio 草稿与租约
- data/releases/            发布留存的知识包（按版本下载读这里）
- data/changeset-state/、flow-state/、change-lists/
                            变更集记录、节点状态的依据、变更点清单（助手副本按副本分支重建，不备）
- data/secrets/             密钥目录里的密文：管理员模型配置、个人 Key 密文、MCP token 库
                            （backup API 拷贝）、服务令牌（只有摘要）
- data/*.json、*.jsonl      各类设置与错误留档；data/audit/ 审计
- home/.claude/projects/    CLI transcript（存在才备）

个人 Key 的主密钥（密钥目录下的 .model-secret-key）不进备份：它与加密后的个人 Key 放在
一起等于没加密（方案第 6 章第 5 项）。配置 ONTO_WEB_KEY_BACKUP_DIR 时每次备份把它单独拷到
那里；推荐直接用 ONTO_WEB_MODEL_SECRET_KEY 提供主密钥，否则异机恢复后个人 Key 解不开。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)

BACKUP_KEEP = 3
BACKUP_PREFIX = "onto-modeling-backup-"
# 定时备份失败后的重试间隔（不超过备份间隔）：失败时最近一份仍是旧的，不能立刻重跑成死循环
FAILURE_RETRY_SECONDS = 3600
_PARTIAL_SUFFIX = ".partial"
_STAGING_PREFIX = ".staging-"
_SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")


def keep_last() -> int:
    """保留最近几份：运维在数据保留里改过就用改过的（O-44），否则 BACKUP_KEEP。"""
    from . import ops_center

    return ops_center.retention_value("backups")


def backups_dir() -> Path:
    if config.BACKUP_DIR:
        return Path(config.BACKUP_DIR)
    return Path(config.DATA_DIR) / "backups"


def list_backups() -> list[dict]:
    directory = backups_dir()
    if not directory.is_dir():
        return []
    entries = []
    for path in directory.glob("*.tar.gz"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append({"name": path.name, "size": stat.st_size, "ts": stat.st_mtime})
    entries.sort(key=lambda entry: (entry["ts"], entry["name"]), reverse=True)
    return entries


def seconds_until_due(interval_seconds: float, now: float | None = None) -> float:
    """距下一次定时备份还有多久；从没备份过或已超过间隔时为 0（立即补跑）。"""
    backups = list_backups()
    if not backups:
        return 0.0
    now = time.time() if now is None else now
    return max(0.0, backups[0]["ts"] + interval_seconds - now)


def _data_entries(manager) -> list[Path]:
    """数据目录下要备份的条目，路径取自各模块自己的路径函数。"""
    from . import (
        audit,
        conversation_store,
        error_log,
        mcp_settings,
        model_context_presets,
        model_settings,
        personal_model_settings,
        release_store,
        runtime_settings,
        service_tokens,
        studio,
    )
    from .engines import jiuwen
    from .onto_mcp import tokens

    return [
        conversation_store._sessions_dir(),
        jiuwen._checkpoint_db_path().parent,
        studio._state_file("backup", "probe").parent,
        release_store.root_dir(manager.data_dir),
        tokens.database_path(manager),
        service_tokens.store_path(),
        model_settings._file(),
        personal_model_settings._file(),
        runtime_settings._file(),
        mcp_settings._file(),
        model_context_presets._custom_file(),
        audit.audit_dir(),
        audit._legacy_file(),
        # 包仓库（插件运行时的 data/packs/）：包版本不可修改，项目按 packs.lock 锁定其中的版本
        Path(config.DATA_DIR) / "packs",
        # 变更集记录、节点状态的依据、变更点清单（B4b）。助手副本（data/changesets/）不备：副本分支在工作区的
        # .git 里，恢复后启动核对按分支重建副本，没提交的改动会丢
        Path(config.DATA_DIR) / "changeset-state",
        Path(config.DATA_DIR) / "flow-state",
        Path(config.DATA_DIR) / "change-lists",
        error_log._file(),
        # 个人偏好与通知（A-08、A-13）；运维的数据保留、导出器设置与演练、任务记录（诊断包不备）
        Path(config.DATA_DIR) / "me",
        *(Path(config.DATA_DIR) / "ops" / name for name in ("retention.json", "exporters.json", "drills.json", "tasks.json")),
    ]


def _is_sqlite(path: Path) -> bool:
    return path.suffix in _SQLITE_SUFFIXES


def _is_sqlite_sidecar(path: Path) -> bool:
    return any(path.name.endswith(suffix + sidecar)
               for suffix in _SQLITE_SUFFIXES for sidecar in _SQLITE_SIDECARS)


def _copy_sqlite(source: Path, staging: Path) -> Path:
    """用 SQLite 的 backup API 拷一份一致的快照：库开着 WAL，直接打包文件可能得到坏库。"""
    target = staging / f"{uuid.uuid4().hex}{source.suffix}"
    src = sqlite3.connect(source, timeout=15)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.chmod(target, 0o600)
    return target


def _add_path(tar: tarfile.TarFile, path: Path, arcname: str, staging: Path) -> None:
    """逐个条目打包：打包途中被删掉的文件跳过，SQLite 库换成快照，伴生的 -wal 等不打。"""
    try:
        if path.is_symlink():
            tar.add(path, arcname=arcname, recursive=False)
        elif path.is_file():
            if _is_sqlite_sidecar(path):
                return
            if _is_sqlite(path):
                tar.add(_copy_sqlite(path, staging), arcname=arcname, recursive=False)
            else:
                tar.add(path, arcname=arcname, recursive=False)
        elif path.is_dir():
            tar.add(path, arcname=arcname, recursive=False)
            for child in sorted(os.scandir(path), key=lambda entry: entry.name):
                _add_path(tar, Path(child.path), f"{arcname}/{child.name}", staging)
    except FileNotFoundError:
        return


def _clean_leftovers(directory: Path) -> None:
    """上次被打断（停机、强杀）留下的半成品与暂存目录。"""
    from .workspace import remove_tree

    for entry in directory.iterdir():
        if entry.name.endswith(_PARTIAL_SUFFIX):
            entry.unlink(missing_ok=True)
        elif entry.name.startswith(_STAGING_PREFIX):
            remove_tree(entry)


def _add_hub_tasks(tar: tarfile.TarFile, data_dir: Path, staging: Path) -> None:
    """知识包 Hub 的发布与导入请求（ADR-0091）：逐个持 Hub 请求锁打包，避开写到一半的请求。"""
    from . import knowledge_hub

    tasks = data_dir / "hub-tasks"
    if not tasks.is_dir():
        return
    for entry in sorted(tasks.iterdir()):
        if entry.is_dir() and entry.name == "packages":
            for project in sorted(entry.iterdir()):
                with knowledge_hub.lock(project.name):
                    if project.exists():
                        _add_path(tar, project, f"data/hub-tasks/packages/{project.name}", staging)
        elif entry.is_dir() or entry.suffix == ".json":
            _add_path(tar, entry, f"data/hub-tasks/{entry.name}", staging)


def run_backup(manager) -> str:
    """打一份备份并清理多余的旧备份，返回备份文件名；手动按钮与定时调度共用。"""
    from .workspace import META_FILE

    directory = backups_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass  # 独立挂载的卷根目录可能改不了权限；备份文件本身仍是 0600
    _clean_leftovers(directory)
    dest = directory / f"{BACKUP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    if dest.exists():
        dest = dest.with_name(f"{dest.name[:-len('.tar.gz')]}-{uuid.uuid4().hex[:6]}.tar.gz")
    partial = dest.with_name(dest.name + _PARTIAL_SUFFIX)
    data_dir = Path(config.DATA_DIR)
    # 先建空文件并收紧权限再写：包里有模型 Key、MCP token 摘要等凭据
    os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    try:
        with tempfile.TemporaryDirectory(prefix=_STAGING_PREFIX, dir=directory) as scratch, \
                tarfile.open(partial, "w:gz") as tar:
            staging = Path(scratch)
            for root in manager._project_dirs():
                # 持项目锁打包：避开 Web 保存、回退等写到一半的状态
                with manager._project_lock(root.name):
                    if (root / META_FILE).is_file():
                        _add_path(tar, root, f"data/workspaces/{root.name}", staging)
            for path in _data_entries(manager):
                if path.exists():
                    _add_path(tar, path, _arcname(path, data_dir), staging)
            _add_hub_tasks(tar, data_dir, staging)
            transcripts = Path.home() / ".claude" / "projects"
            if transcripts.is_dir():
                _add_path(tar, transcripts, "home/.claude/projects", staging)
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    for old in list_backups()[keep_last():]:
        (directory / old["name"]).unlink(missing_ok=True)
    from ..governance import secrets

    key_copy = secrets.backup_master_key()
    if key_copy is None and not secrets.master_key_configured():
        _log.warning(
            "已备份到 %s。个人 Key 主密钥（密钥目录下的 .model-secret-key）不在备份里："
            "异机恢复前须单独拷贝该文件，或用 ONTO_WEB_MODEL_SECRET_KEY 提供同一主密钥，"
            "否则恢复后的个人 Key 无法解密；配置 ONTO_WEB_KEY_BACKUP_DIR 可自动单独备份",
            dest,
        )
    return dest.name


def _arcname(path: Path, data_dir: Path) -> str:
    """包内路径：数据目录下的条目按原相对路径；密钥目录指到数据目录之外时放在 data/secrets/ 下。"""
    try:
        return f"data/{path.relative_to(data_dir).as_posix()}"
    except ValueError:
        return f"data/secrets/{path.name}"


async def scheduler_loop(manager, *, sleep=asyncio.sleep) -> None:
    """定时备份（ONTO_WEB_BACKUP_INTERVAL_HOURS，0 关闭）。

    启动时距上次备份已超过间隔就先补跑一次，再按间隔循环：先等满间隔再备份的话，
    服务重启比间隔更频繁时就永远不备份。打包放线程，不卡事件循环。
    """
    interval = config.BACKUP_INTERVAL_HOURS
    if interval <= 0:
        return
    period = interval * 3600
    while True:
        delay = seconds_until_due(period)
        if delay > 0:
            await sleep(delay)
        try:
            from . import ops_center

            # 定时备份也进任务中心（O-40），发起人是 system
            await asyncio.to_thread(ops_center.tracked, "backup", "数据备份", "system", lambda: run_backup(manager))
        except Exception:
            _log.exception("定时备份失败，稍后重试")
            await sleep(min(period, FAILURE_RETRY_SECONDS))
