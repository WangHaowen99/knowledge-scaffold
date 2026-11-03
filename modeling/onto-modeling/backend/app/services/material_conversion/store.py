"""转换状态记录（ADR-0073 第 10 条）：每个项目一个 JSON，放内核数据目录，不进工作区。

记录只是中间状态（转换中、等待、失败、暂不支持）的缓存；「已转成文本」以工作区里的文本版为准，记录对不上时按文件重判。
另存管理员按文件类型指定的默认转换器。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

from ... import config

_lock = threading.Lock()
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _dir() -> Path:
    return Path(config.DATA_DIR) / "conversions"


def _file(project_id: str) -> Path:
    if not _ID_RE.match(project_id or ""):
        raise ValueError("项目 ID 不合法")
    return _dir() / f"{project_id}.json"


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as stream:
        json.dump(data, stream, ensure_ascii=False)
        name = stream.name
    os.replace(name, path)


def items(project_id: str) -> dict[str, dict]:
    with _lock:
        return dict(_read(_file(project_id)).get("items") or {})


def get(project_id: str, rel: str) -> dict | None:
    return items(project_id).get(rel)


def put(project_id: str, rel: str, entry: dict) -> dict:
    with _lock:
        path = _file(project_id)
        data = _read(path)
        data.setdefault("items", {})[rel] = entry
        _write(path, data)
    return entry


def drop(project_id: str, rel: str) -> None:
    with _lock:
        path = _file(project_id)
        data = _read(path)
        if (data.get("items") or {}).pop(rel, None) is not None:
            _write(path, data)


def projects() -> list[str]:
    folder = _dir()
    if not folder.is_dir():
        return []
    return sorted(path.stem for path in folder.glob("*.json"))


# ---- 默认转换器（PL-21）----


def _defaults_file() -> Path:
    return Path(config.DATA_DIR) / "converter-defaults.json"


def defaults() -> dict[str, str]:
    with _lock:
        data = _read(_defaults_file())
    return {str(key): str(value) for key, value in (data.get("defaults") or {}).items()}


def set_default(file_type: str, converter: str, *, by: str) -> dict[str, str]:
    with _lock:
        path = _defaults_file()
        data = _read(path)
        data.setdefault("defaults", {})[file_type] = converter
        data.setdefault("history", []).append({"type": file_type, "converter": converter, "by": by})
        data["history"] = data["history"][-50:]
        _write(path, data)
        return dict(data["defaults"])
