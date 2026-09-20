"""审计日志：记录谁在何时做了什么写操作（读操作不记，避免噪音）。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config

MAX_PATH_LENGTH = 200


def _file() -> Path:
    return Path(config.DATA_DIR) / "audit.jsonl"


def log(user: str, method: str, path: str) -> None:
    entry = {
        "ts": time.time(),
        "user": user or "anonymous",
        "method": method,
        "path": path[:MAX_PATH_LENGTH],
    }
    try:
        target = _file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(limit: int = 200) -> list[dict]:
    path = _file()
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries
