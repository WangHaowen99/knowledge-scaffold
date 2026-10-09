"""原始错误留档：用户界面只显示友好提示，原始信息进运维页。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config

MAX_RAW_LENGTH = 4000


def _file() -> Path:
    return Path(config.DATA_DIR) / "errors.jsonl"


def log(project_id: str, source: str, raw: str) -> None:
    entry = {
        "ts": time.time(),
        "project_id": project_id,
        "source": source,
        "raw": str(raw)[:MAX_RAW_LENGTH],
    }
    try:
        path = _file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(limit: int = 100) -> list[dict]:
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
    entries.reverse()  # 最新在前
    return entries
