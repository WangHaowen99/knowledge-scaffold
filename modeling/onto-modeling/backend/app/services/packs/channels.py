"""core 默认版本与包仓库备份（契约 O-30～O-34；方案 2.3 第 4、5 步）。

原来的「当前资产 + 资产备份目录」（assets_admin）改由包仓库承接：core 的每个版本都在包仓库里、
不可修改；默认版本是 stable 通道的指针，回滚只改指针（先自动备份包仓库），跟随通道的项目
下一条消息前切换，锁定了版本的项目不受影响。
"""

from __future__ import annotations

import os
import tarfile
import time
from pathlib import Path

from . import builtin
from .repository import PackRepository, RepositoryError

BACKUP_KEEP = 3
_BACKUP_DIR = "pack-repo-backups"


def core_default_view(repository: PackRepository) -> dict:
    """O-30：stable、beta 与 core 的全部版本。"""
    stable = repository.channel(builtin.CORE, "stable") or repository.latest(builtin.CORE, "stable")
    beta_entry = repository.channel_entry(builtin.CORE).get("beta")
    beta = None
    if isinstance(beta_entry, dict) and repository.has(builtin.CORE, beta_entry.get("version") or ""):
        beta = {"version": beta_entry["version"], "rollout_pct": beta_entry.get("percent", 0)}
    versions = []
    for version in repository.versions(builtin.CORE):
        channel = "stable" if version == stable else "beta" if beta and version == beta["version"] else "retired"
        versions.append({"version": version, "channel": channel,
                         "released_at": repository.meta(builtin.CORE, version).get("released_at"),
                         "is_default": version == stable})
    return {"stable": stable, "beta": beta, "versions": versions}


def set_core_default(repository: PackRepository, version: str, *, by: str, backup_first: bool = False) -> dict:
    """O-31 推广到 stable / O-32 回滚默认版本：改 stable 指针（回滚前先备份包仓库）。"""
    if not repository.has(builtin.CORE, version):
        raise RepositoryError("not_found", f"core 没有版本 {version}", 404)
    if backup_first:
        backup(repository)
    repository.set_channel(builtin.CORE, "stable", version, by=by)
    return core_default_view(repository)


def _backups_dir(repository: PackRepository) -> Path:
    return repository.root.parent / _BACKUP_DIR


def backup(repository: PackRepository) -> str:
    """O-34：把包仓库（版本目录、元数据、通道）打成一个 tar.gz，保留最近 BACKUP_KEEP 份；上传区不备份。"""
    directory = _backups_dir(repository)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"packs-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid() % 10000:04d}.tar.gz"
    target = directory / name
    tmp = target.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        if repository.root.is_dir():
            for entry in sorted(repository.root.iterdir()):
                if entry.name in ("uploads",) or entry.name.startswith(".tmp-"):
                    continue
                tar.add(entry, arcname=f"packs/{entry.name}",
                        filter=lambda info: None if "/.tmp-" in info.name else info)
    os.replace(tmp, target)
    for old in list_backups(repository)[BACKUP_KEEP:]:
        (directory / old["name"]).unlink(missing_ok=True)
    return name


def list_backups(repository: PackRepository) -> list[dict]:
    """O-33：包仓库备份，新的在前。"""
    directory = _backups_dir(repository)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.glob("packs-*.tar.gz"):
        stat = path.stat()
        items.append({"name": path.name, "size": stat.st_size, "created_at": stat.st_mtime})
    return sorted(items, key=lambda item: item["created_at"], reverse=True)
