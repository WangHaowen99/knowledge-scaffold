"""评测集：按项目保存评测用例，导入过一次后评测时可直接选用。

用例契约同 modeling 的 testcase-import：``{question, groundTruth}``。
评测集落工作区 ``.ontology-state/eval-sets.json``——与 eval.json 同处，
不进工作区 git，也不随知识包发布。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

STORE_REL = ".ontology-state/eval-sets.json"
MAX_SETS = 50
MAX_CASES = 500
MAX_NAME_LENGTH = 60
SOURCES = ("import", "trace", "manual")


class EvalSetError(ValueError):
    """评测集输入不合法（接口层映射为 400）。"""


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def normalize_cases(cases) -> list[dict]:
    if not isinstance(cases, list):
        raise EvalSetError("cases 必须是数组")
    out = []
    for item in cases:
        if not isinstance(item, dict):
            continue
        question = _text(item.get("question"))
        if question:
            out.append({"question": question, "groundTruth": _text(item.get("groundTruth"))})
    if not out:
        raise EvalSetError("评测集至少需要一条有问题的用例")
    if len(out) > MAX_CASES:
        raise EvalSetError(f"一个评测集最多 {MAX_CASES} 条用例")
    return out


def _path(root: Path) -> Path:
    return root / STORE_REL


def _load(root: Path) -> list[dict]:
    path = _path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [s for s in data.get("sets") or [] if isinstance(s, dict) and s.get("id")]


def _save(root: Path, sets: list[dict]) -> None:
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sets": sets}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def summary(item: dict) -> dict:
    return {
        "id": item["id"],
        "name": item.get("name", ""),
        "source": item.get("source", "manual"),
        "count": len(item.get("cases") or []),
        "created_at": item.get("created_at", 0),
        "updated_at": item.get("updated_at", 0),
        "used_at": item.get("used_at", 0),
    }


def list_sets(root: Path) -> list[dict]:
    """摘要列表（不含用例正文），最近使用/更新的在前。"""
    items = [summary(s) for s in _load(root)]
    items.sort(key=lambda s: max(s["used_at"], s["updated_at"]), reverse=True)
    return items


def get_set(root: Path, set_id: str) -> dict | None:
    return next((s for s in _load(root) if s["id"] == set_id), None)


def _unique_name(sets: list[dict], name: str, exclude_id: str = "") -> str:
    taken = {s.get("name") for s in sets if s["id"] != exclude_id}
    if name not in taken:
        return name
    index = 2
    while f"{name}（{index}）" in taken:
        index += 1
    return f"{name}（{index}）"


def create_set(root: Path, name: str, cases, source: str = "manual") -> dict:
    sets = _load(root)
    if len(sets) >= MAX_SETS:
        raise EvalSetError(f"每个项目最多保存 {MAX_SETS} 个评测集，请先删除不用的")
    now = int(time.time())
    item = {
        "id": uuid.uuid4().hex[:12],
        "name": _unique_name(sets, _text(name)[:MAX_NAME_LENGTH] or "未命名评测集"),
        "source": source if source in SOURCES else "manual",
        "cases": normalize_cases(cases),
        "created_at": now,
        "updated_at": now,
    }
    sets.append(item)
    _save(root, sets)
    return item


def update_set(root: Path, set_id: str, name: str | None = None, cases=None) -> dict:
    sets = _load(root)
    for item in sets:
        if item["id"] != set_id:
            continue
        if name is not None and _text(name):
            item["name"] = _unique_name(sets, _text(name)[:MAX_NAME_LENGTH], exclude_id=set_id)
        if cases is not None:
            item["cases"] = normalize_cases(cases)
        item["updated_at"] = int(time.time())
        _save(root, sets)
        return item
    raise KeyError(set_id)


def delete_set(root: Path, set_id: str) -> None:
    sets = _load(root)
    remaining = [s for s in sets if s["id"] != set_id]
    if len(remaining) == len(sets):
        raise KeyError(set_id)
    _save(root, remaining)


def mark_used(root: Path, set_id: str) -> None:
    sets = _load(root)
    for item in sets:
        if item["id"] == set_id:
            item["used_at"] = int(time.time())
            _save(root, sets)
            return
