"""变更点清单（契约 3.14 INC-01～INC-05、7.10 ChangeList；F3 补充：清单要能找到所在对话）。

增量更新（core 的按需节点 `core.update`）的影响分析由助手在副本里写进 `.ontology-state/update-plan.json`
（`/ontology-update` 第 2 步，原样是助手返回的条目数组，可能带 `base_commit`）。一轮结束时宿主把它收成一份
清单存进数据目录 `change-lists/<项目>/<清单>.json`（不进 git），记下所在对话与变更集；之后：

- 清单在执行前可以编辑（INC-02、INC-03），不受写入范围锁约束（决定 #40）；
- 确认并执行（INC-04）往对话发 `/ontology-update change_list_id=<清单>`，清单同时写进副本的
  `.onto/run/change-lists/<清单>.json`，命令按它执行、跳过影响分析与确认两步；回归结果卡的「交给助手修」发
  `/ontology-update change_list_id=<清单> fix_scenarios=[...]`（B4c 的命令名映射）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

PLAN_REL = Path(".ontology-state") / "update-plan.json"
EXPORT_DIR = Path(".onto") / "run" / "change-lists"
_KIND_CONFIDENCE = {"create": "medium", "delete": "low", "modify": "high"}
_lock = threading.RLock()


def _dir(root: Path) -> Path:
    from ...kernel.changesets.worktree import data_dir_of

    root = Path(root)
    return data_dir_of(root) / "change-lists" / root.name


def _path(root: Path, list_id: str) -> Path:
    return _dir(root) / f"{list_id}.json"


def _revision(record: dict) -> str:
    body = json.dumps(record.get("points") or [], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _write(root: Path, record: dict) -> dict:
    record["revision"] = _revision(record)
    record["updated_at"] = time.time()
    path = _path(root, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return record


def get(root: Path, list_id: str) -> dict | None:
    if not list_id or "/" in list_id or list_id.startswith("."):
        return None
    try:
        data = json.loads(_path(root, list_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def all_lists(root: Path) -> list[dict]:
    out = []
    directory = _dir(root)
    for path in sorted(directory.glob("cl-*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return sorted(out, key=lambda item: item.get("created_at") or 0, reverse=True)


def purge(root: Path) -> None:
    import shutil

    shutil.rmtree(_dir(root), ignore_errors=True)


# ---------------------------------------------------------------------------
# 从副本里的影响分析收成清单
# ---------------------------------------------------------------------------


def _plan_entries(data) -> tuple[list, str]:
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        for key in ("plan", "items", "entries", "changes"):
            if isinstance(data.get(key), list):
                return data[key], str(data.get("reference") or data.get("reference_path") or "")
    return [], ""


def _entry_ref(rel: str, model=None) -> dict | None:
    from ...kernel import metamodel

    entry = (model or metamodel.mm).entry_key(rel or "")
    if not entry:
        return None
    kind, _, key = entry.partition(":")
    return {"kind": kind, "id": entry, "key": key, "name": key}


def _points(entries: list, model=None) -> list[dict]:
    points = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("file"):
            continue
        impact = [ref for ref in [_entry_ref(entry["file"], model)] if ref]
        for item in entry.get("cascading") or []:
            if isinstance(item, dict) and item.get("needs_change"):
                ref = _entry_ref(str(item.get("file") or ""), model)
                if ref and ref not in impact:
                    impact.append(ref)
        operation = str(entry.get("operation") or "modify")
        points.append({
            "id": f"p{index + 1}", "n": index + 1,
            "condition": str(entry.get("reason") or "")[:500],
            "value": str(entry.get("change_summary") or "")[:500],
            "scope": str(entry["file"]),
            "anchor": "", "anchor_label": "",
            "confidence": _KIND_CONFIDENCE.get(operation, "medium"),
            "confidence_note": "删除已有内容，请单独确认" if operation == "delete" else None,
            "state": "pending", "rewritten_text": None, "impact": impact,
        })
    return points


def capture(root: Path, copy: Path, *, conversation_id: str | None, changeset_id: str | None) -> dict | None:
    """一轮结束时：副本里有新的影响分析（update-plan.json 变了）就收成一份清单；返回新建或更新的清单。"""
    path = Path(copy) / PLAN_REL
    if path.is_symlink() or path.parent.is_symlink():  # 助手在副本里建的链接不读
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()[:16]
    try:
        entries, reference = _plan_entries(json.loads(raw.decode("utf-8")))
    except ValueError:
        return None
    if not entries:
        return None
    from ...kernel import metamodel

    model = metamodel.for_project(Path(root))  # 条目目录按本项目锁定版本的元模型认
    with _lock:
        existing = next((item for item in all_lists(root) if item.get("plan_digest") == digest), None)
        if existing is not None:
            return None
        now = time.time()
        record = {
            "id": f"cl-{uuid.uuid4().hex[:8]}", "source": {"path": reference, "label": Path(reference).name if reference
                                                           else "这一轮的参考内容"},
            "points": _points(entries, model), "state": "editing", "changeset_id": changeset_id,
            "conversation_id": conversation_id, "plan_digest": digest, "created_at": now,
        }
        return _write(root, record)


# ---------------------------------------------------------------------------
# 编辑、接受、执行
# ---------------------------------------------------------------------------


class ChangeListError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def _require(root: Path, list_id: str) -> dict:
    record = get(root, list_id)
    if record is None:
        raise ChangeListError("not_found", "变更清单不存在", 404)
    return record


def save_points(root: Path, list_id: str, base_revision: str, points: list) -> dict:
    """INC-02：整份替换；新增条目不带 id。"""
    with _lock:
        record = _require(root, list_id)
        if record.get("state") == "executed":
            raise ChangeListError("changeset_closed", "清单已经执行", 409, {"state": "executed"})
        if base_revision != record.get("revision"):
            raise ChangeListError("revision_conflict", "清单已被修改，请刷新", 409,
                                  {"expected": base_revision, "actual": record.get("revision")})
        old = {point["id"]: point for point in record.get("points") or []}
        merged = []
        for index, item in enumerate(points or []):
            if not isinstance(item, dict):
                raise ChangeListError("invalid_argument", "points 每项要是对象", 400, {"fields": {"points": "item"}})
            base = dict(old.get(str(item.get("id") or "")) or {
                "id": f"p{uuid.uuid4().hex[:6]}", "anchor": "", "anchor_label": "", "confidence": "high",
                "confidence_note": None, "state": "pending", "rewritten_text": None, "impact": [],
                "condition": "", "value": "", "scope": "",
            })
            for key in ("condition", "value", "scope", "anchor"):
                if key in item:
                    base[key] = str(item[key] or "")[:500]
            if item.get("rewritten_text"):
                base["rewritten_text"] = str(item["rewritten_text"])[:1000]
                base["state"] = "rewritten"
            base["n"] = index + 1
            merged.append(base)
        record["points"] = merged
        return _write(root, record)


def accept(root: Path, list_id: str, *, point_ids: list[str] | None = None, high_confidence: bool = False) -> dict:
    """INC-03：低把握的条目不能出现在多于一条的批量里。"""
    with _lock:
        record = _require(root, list_id)
        points = record.get("points") or []
        chosen = [point for point in points if (high_confidence and point.get("confidence") == "high")
                  or (point_ids and point["id"] in point_ids)]
        if point_ids and len(point_ids) > 1:
            low = [point["id"] for point in chosen if point.get("confidence") == "low"]
            if low:
                raise ChangeListError("low_confidence_batch", "把握低的变更点要单独确认", 422, {"point_ids": low})
        for point in chosen:
            if point.get("state") == "pending":
                point["state"] = "accepted"
        return _write(root, record)


def ready_to_execute(record: dict) -> list[str]:
    """INC-04 之前：还没接受或改写的条目。"""
    return [point["id"] for point in record.get("points") or [] if point.get("state") == "pending"]


def export(root: Path, copy: Path, record: dict) -> str:
    """清单写进助手副本（`/ontology-update change_list_id=…` 按它执行）；返回副本里的相对路径。"""
    rel = EXPORT_DIR / f"{record['id']}.json"
    target = Path(copy) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(view(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return rel.as_posix()


def link(root: Path, conversation_id: str | None, changeset_id: str) -> str | None:
    """这条对话里还没挂变更集的清单（编辑中、已确认待执行）挂上变更集编号；返回最新那份清单的编号。"""
    if not conversation_id:
        return None
    latest = None
    with _lock:
        for record in all_lists(root):
            if record.get("conversation_id") != conversation_id or record.get("state") == "executed":
                continue
            if not record.get("changeset_id"):
                record["changeset_id"] = changeset_id
                _write(root, record)
            if record.get("changeset_id") == changeset_id and latest is None:
                latest = record["id"]
    return latest


def on_changeset_closed(root: Path, changeset_id: str, state: str) -> list[str]:
    """变更集确认生效：挂在它上面的清单记为已执行（审查第 12 条）；撤回：清单回到编辑中、摘掉编号。"""
    touched = []
    with _lock:
        for record in all_lists(root):
            if record.get("changeset_id") != changeset_id or record.get("state") == "executed":
                continue
            if state == "merged":
                record["state"] = "executed"
            elif state == "discarded":
                record.update(state="editing", changeset_id=None)
            else:
                continue
            _write(root, record)
            touched.append(record["id"])
    return touched


def mark(root: Path, list_id: str, **fields) -> dict | None:
    with _lock:
        record = get(root, list_id)
        if record is None:
            return None
        record.update(fields)
        return _write(root, record)


def view(record: dict) -> dict:
    """契约 7.10 ChangeList（加 F3 的 conversation_id）。"""
    points = record.get("points") or []
    impacted = {ref["id"] for point in points for ref in point.get("impact") or []}
    return {
        "id": record["id"], "source": record.get("source") or {"path": "", "label": ""},
        "points": points, "state": record.get("state") or "editing",
        "changeset_id": record.get("changeset_id"), "conversation_id": record.get("conversation_id"),
        "impact_summary": {"artifacts": len(impacted), "scenarios": 0, "duration_ms": 0},
        "revision": record.get("revision") or _revision(record),
    }


def impact(record: dict) -> dict:
    """INC-05：影响面（确定性）：变更点指到的条目即将标过期；回归场景要等场景走查包的索引（见 gaps）。"""
    started = time.monotonic()
    stale = []
    seen = set()
    for point in record.get("points") or []:
        for ref in point.get("impact") or []:
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            stale.append({"target": ref, "note": point.get("value") or None})
    elapsed = int((time.monotonic() - started) * 1000)
    return {
        "duration_ms": elapsed,
        "phases": [{"label": "按变更点定位产物", "duration_ms": elapsed}, {"label": "沿引用算下游", "duration_ms": 0},
                   {"label": "挑回归场景", "duration_ms": 0}],
        "stale": stale,
        "regression": {"affected": [], "guard": []},
    }
