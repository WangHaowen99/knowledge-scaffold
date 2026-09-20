"""运行时行为设置（当前仅权限模式），存数据目录，优先于环境变量默认值。"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config

ALLOWED_PERMISSION_MODES = ("bypassPermissions", "default", "acceptEdits", "plan")


def _file() -> Path:
    return Path(config.DATA_DIR) / "runtime-settings.json"


def load() -> dict:
    path = _file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save(permission_mode: str) -> dict:
    if permission_mode not in ALLOWED_PERMISSION_MODES:
        raise ValueError(f"权限模式必须是 {ALLOWED_PERMISSION_MODES} 之一")
    data = load()
    data["permission_mode"] = permission_mode
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def permission_mode() -> str:
    return load().get("permission_mode") or config.PERMISSION_MODE
