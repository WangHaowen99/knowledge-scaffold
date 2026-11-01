"""内核守卫与 hooks 基线（详设 4.8 目标 1；方案 D8）。

随镜像发布（H3），装配时由内核注入每个项目的装配目录：脚本放进 `scripts/`（现有命令里写的
`.claude/scripts/...` 路径不用改），`settings.base.json` 是权限与 hooks 的基线，包只能在它
后面追加拒绝型 PreToolUse 守卫。包里同名的文件装配失败，包删不掉、盖不掉内核守卫。
"""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

GUARDS_DIR = Path(__file__).resolve().parent
SETTINGS_BASE = GUARDS_DIR / "settings.base.json"
# 注入装配目录 scripts/ 的文件（不含本模块与 settings 基线）
SCRIPT_SUFFIXES = (".sh", ".py")


def scripts() -> list[Path]:
    return sorted(
        path for path in GUARDS_DIR.iterdir()
        if path.is_file() and path.suffix in SCRIPT_SUFFIXES and path.name != "__init__.py"
    )


def settings_base() -> dict:
    return json.loads(SETTINGS_BASE.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def digest() -> str:
    """内核守卫的内容摘要：计入装配哈希，守卫随版本更新后各项目重新装配（随镜像发布，进程内不变）。"""
    hasher = hashlib.sha256()
    for path in [*scripts(), SETTINGS_BASE]:
        hasher.update(path.name.encode("utf-8") + b"\0")
        hasher.update(path.read_bytes())
    return hasher.hexdigest()[:16]
