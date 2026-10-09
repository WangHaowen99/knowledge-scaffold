"""提示词资产管理：查看当前资产信息、上传新资产包（产品升级通道）。"""

from __future__ import annotations

import shutil
import time
import zipfile
from pathlib import Path

from .. import config


class AssetError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _assets_dotclaude() -> Path:
    return Path(config.ASSETS_DIR) / "workspace-toolkit"


def info() -> dict:
    root = _assets_dotclaude()
    if not root.is_dir():
        return {"present": False}

    def count(sub: str, pattern: str = "*") -> int:
        directory = root / sub
        return sum(1 for _ in directory.glob(pattern)) if directory.is_dir() else 0

    latest = 0.0
    for path in root.rglob("*"):
        if path.is_file():
            latest = max(latest, path.stat().st_mtime)
    return {
        "present": True,
        "commands": count("commands", "*.md"),
        "agents": count("agents", "*.md"),
        "scripts": count("scripts"),
        "updated_at": latest,
    }


def list_backups() -> list[dict]:
    backups = Path(config.DATA_DIR) / "asset-backups"
    if not backups.is_dir():
        return []
    entries = []
    for entry in sorted(backups.iterdir(), reverse=True):
        if entry.is_dir() and entry.name.startswith("dotclaude-"):
            try:
                ts = int(entry.name.rsplit("-", 1)[1])
            except ValueError:
                ts = int(entry.stat().st_mtime)
            commands = sum(1 for _ in (entry / "commands").glob("*.md")) if (entry / "commands").is_dir() else 0
            entries.append({"name": entry.name, "time": ts, "commands": commands})
    return entries


def rollback(backup_name: str) -> dict:
    """回滚到指定备份：当前资产先入备份，再恢复所选版本。"""
    if not backup_name.startswith("dotclaude-") or "/" in backup_name or ".." in backup_name:
        raise AssetError("无效的备份名")
    backups = Path(config.DATA_DIR) / "asset-backups"
    source = backups / backup_name
    if not source.is_dir():
        raise AssetError("备份不存在", 404)
    current = _assets_dotclaude()
    if current.exists():
        shutil.move(str(current), str(backups / f"dotclaude-{int(time.time())}"))
    shutil.copytree(source, current)
    return info()


def upload(archive_path: Path) -> dict:
    """校验并替换提示词资产；旧资产备份到数据目录。"""
    staging = Path(config.DATA_DIR) / "asset-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for entry in zf.infolist():
                parts = Path(entry.filename).parts
                if not parts or entry.filename.startswith("/") or ".." in parts:
                    continue
                if (entry.external_attr >> 28) == 0xA:  # 拒绝符号链接
                    continue
                if entry.is_dir():
                    continue
                dest = staging / entry.filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile as exc:
        raise AssetError(f"压缩包无法解析：{exc}") from exc

    # 兼容两种打包方式：zip 根即 .claude 内容，或包一层 .claude/
    candidate = staging
    if not (candidate / "commands").is_dir() and (staging / ".claude" / "commands").is_dir():
        candidate = staging / ".claude"
    if not (candidate / "commands").is_dir() or not (candidate / "agents").is_dir():
        raise AssetError("资产包无效：缺少 commands/ 或 agents/ 目录")

    current = _assets_dotclaude()
    backups = Path(config.DATA_DIR) / "asset-backups"
    backups.mkdir(parents=True, exist_ok=True)
    if current.exists():
        shutil.move(str(current), str(backups / f"dotclaude-{int(time.time())}"))
    shutil.move(str(candidate), str(current))
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    return info()
