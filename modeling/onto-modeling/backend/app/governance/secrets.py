"""密钥目录（方案 E10、第 0 章问题 5、第 6 章第 4、5 项）：移出 agent 可读范围，主密钥与密文分开备份。

放进密钥目录的：管理员模型 Key（model-settings.json）、个人 Key 密文（user-model-settings.json）、
个人 Key 主密钥（只在没配 ONTO_WEB_MODEL_SECRET_KEY 时自动生成，.model-secret-key）、MCP token 库
（onto-mcp.sqlite）、服务令牌库（service-tokens.json）。

- 目录 0700、文件 0600，归后端进程的用户；agent 以独立的非 root 用户运行（agent_identity），读不到；
- 默认在数据目录下的 `secrets/`，可用 ONTO_WEB_SECRETS_DIR 指到别的卷；
- 启动时把老位置（数据目录根）的这些文件搬进来；
- 没配 ONTO_WEB_MODEL_SECRET_KEY 时启动告警：自动生成的主密钥只在本机，异机恢复后个人 Key 解不开；
- 备份只带密文，自动生成的主密钥另备到 ONTO_WEB_KEY_BACKUP_DIR（与密文分开放）。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)

DIR_MODE = 0o700
FILE_MODE = 0o600
MODEL_SETTINGS = "model-settings.json"
PERSONAL_SETTINGS = "user-model-settings.json"
MASTER_KEY = ".model-secret-key"
MCP_TOKENS = "onto-mcp.sqlite"
SERVICE_TOKENS = "service-tokens.json"
SECRET_FILES = (MODEL_SETTINGS, PERSONAL_SETTINGS, MASTER_KEY, MCP_TOKENS, SERVICE_TOKENS)
_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")

_warnings: list[str] = []


def secrets_dir(data_dir: Path | str | None = None) -> Path:
    """密钥目录：ONTO_WEB_SECRETS_DIR，没配时是数据目录下的 secrets/（跟随 data_dir）。"""
    if config.SECRETS_DIR:
        return Path(config.SECRETS_DIR)
    return Path(data_dir or config.DATA_DIR) / "secrets"


def ensure_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(DIR_MODE)
    except OSError:
        pass  # 独立挂载的卷根目录可能改不了权限，文件本身仍是 0600
    return directory


def path(name: str, data_dir: Path | str | None = None) -> Path:
    """密钥文件的路径（目录不存在时建出、收紧权限）。"""
    return ensure_dir(secrets_dir(data_dir)) / name


def _move(source: Path, target: Path) -> bool:
    if not source.exists() or source.is_symlink():
        return False
    if target.exists():
        _log.warning("密钥目录里已有 %s，保留它，旧位置的 %s 没有搬", target.name, source)
        return False
    try:
        os.replace(source, target)
    except OSError:
        shutil.move(str(source), str(target))
    return True


def migrate(data_dir: Path | str | None = None) -> list[str]:
    """把数据目录根下的老密钥文件搬进密钥目录（含 SQLite 伴生文件），返回搬了哪些。"""
    root = Path(data_dir or config.DATA_DIR)
    target_dir = ensure_dir(secrets_dir(root))
    moved = []
    for name in SECRET_FILES:
        names = [name, *(name + suffix for suffix in _SQLITE_SIDECARS)] if name.endswith(".sqlite") else [name]
        for item in names:
            source, target = root / item, target_dir / item
            if source != target and _move(source, target):
                moved.append(item)
    for item in target_dir.iterdir():
        if item.is_file() and not item.is_symlink():
            try:
                item.chmod(FILE_MODE)
            except OSError:
                pass
    if moved:
        _log.warning("已把 %s 移进密钥目录 %s（agent 读不到）", "、".join(moved), target_dir)
    return moved


def master_key_configured() -> bool:
    return bool(os.environ.get("ONTO_WEB_MODEL_SECRET_KEY", "").strip())


def backup_master_key() -> Path | None:
    """自动生成的主密钥单独备一份到 ONTO_WEB_KEY_BACKUP_DIR（与密文分开放）；没配目录或没有自动主密钥时 None。"""
    source = secrets_dir() / MASTER_KEY
    if not config.KEY_BACKUP_DIR or not source.is_file():
        return None
    directory = Path(config.KEY_BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(DIR_MODE)
    except OSError:
        pass
    target = directory / "model-secret-key"
    data = source.read_bytes()
    if not target.is_file() or target.read_bytes() != data:
        fd = os.open(target.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(target.with_suffix(".tmp"), target)
    return target


def prepare() -> list[str]:
    """启动时：建好密钥目录、搬老文件、主密钥告警；返回告警文字（运维总览的建议里也显示）。"""
    _warnings.clear()
    migrate()
    if not master_key_configured():
        message = (
            "没有配置 ONTO_WEB_MODEL_SECRET_KEY：个人 Key 的主密钥自动生成在本机密钥目录，"
            "备份里不带它，异机恢复后个人 Key 解不开。请配置至少 32 字节的主密钥"
        )
        if not config.KEY_BACKUP_DIR:
            message += "，或配置 ONTO_WEB_KEY_BACKUP_DIR 单独备份自动生成的主密钥"
        _warnings.append(message)
        _log.warning(message)
    try:
        backup_master_key()
    except OSError as exc:
        _log.warning("主密钥单独备份失败：%s", exc)
    return list(_warnings)


def warnings() -> list[str]:
    return list(_warnings)
