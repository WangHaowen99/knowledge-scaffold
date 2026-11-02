"""待处理汇总（详设 4.7、8.2 风险应对；契约 3.13 IN-01、IN-02、7.9 InboxItem、EP-02）。

待处理不另存一份状态：处理完一项，它的来源就变了，计数随之减少。唯一自存的是「忽略」记录与当天处理过的记录
（主线工作区 `.onto/run/`，不进 git）。现在汇总的来源：

| kind | 来源 |
|---|---|
| decision 决策 | 在线会话里没回答的决策卡 |
| resumable 续跑 | 中断、能续跑的执行记录 |
| pending_changeset 修改待确认 | 没确认的助手变更集 |
| changeset_conflict 合入冲突 | 合入时检测到实体冲突的变更集 |
| stale 过期 | 过期的节点 |
| review 需复核 | 节点升级、导入（自主构建那一种在 P3 由 `mark_needs_review` 带来） |
| report 报告待处理 | 报告里还有没处理的 `[ ]` 条目，一份报告一项 |
| format_migration 格式待迁移 | 插件产物的格式版本比包里登记的旧 |
| feedback 外部反馈 | 评测服务回传的反馈批次 |
| change_list 变更清单 | 增量更新识别出、还没执行的变更点清单 |

证据冲突（evidence_conflict）、挂起（suspension）随证据源与自主构建（P3）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

KIND_LABELS = {
    "decision": "决策", "evidence_conflict": "证据冲突", "change_list": "变更清单", "review": "需复核",
    "suspension": "挂起", "stale": "过期", "resumable": "续跑", "feedback": "外部反馈", "report": "报告待处理",
    "pending_changeset": "修改待确认", "changeset_conflict": "合入冲突", "format_migration": "格式待迁移",
    "notification": "通知",
}
ACTIONS = {
    "decision": ["answer"], "evidence_conflict": ["arbitrate"],
    "change_list": ["accept_points", "rewrite_point", "execute"], "review": ["accept", "rejudge", "revert_slice"],
    "suspension": ["answer", "use_generic"], "stale": ["refresh", "keep_stale"], "resumable": ["resume", "abandon"],
    "feedback": ["send_to_evolution"], "report": ["send_to_assistant"], "pending_changeset": ["merge", "discard"],
    "changeset_conflict": ["resolve"], "format_migration": ["migrate"], "notification": [],
}
FILTERS = {
    "all": None, "autobuild_review": ("review", "suspension"), "decision": ("decision",),
    "evidence_conflict": ("evidence_conflict",), "change_list": ("change_list",), "stale": ("stale",),
    "resumable": ("resumable",), "feedback": ("feedback",),
}
DISMISSED_REL = Path(".onto") / "run" / "inbox-dismissed.json"
HANDLED_REL = Path(".onto") / "run" / "inbox-handled.jsonl"
MINUTES_PER_ITEM = 2
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 忽略与处理记录
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def dismissed(root: Path) -> dict:
    return _read_json(Path(root) / DISMISSED_REL)


def dismiss(root: Path, item_id: str, *, by: str = "", fingerprint: str | None = None) -> None:
    """忽略一项：记下它当时的来源指纹，来源变了（又一次过期、复核标记重打、报告改过）就重新出现（审查第 15 条）。"""
    path = Path(root) / DISMISSED_REL
    with _lock:
        data = dismissed(root)
        data[item_id] = {"at": time.time(), "by": by, "fp": fingerprint}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def record_handled(root: Path, item_id: str, action: str) -> None:
    path = Path(root) / HANDLED_REL
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": item_id, "action": action, "at": time.time()}, ensure_ascii=False) + "\n")


def handled_today(root: Path) -> int:
    path = Path(root) / HANDLED_REL
    start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    count = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    for line in lines[-2000:]:
        try:
            if float(json.loads(line).get("at") or 0) >= start:
                count += 1
        except (ValueError, AttributeError):
            continue
    return count


# ---------------------------------------------------------------------------
# 各来源
# ---------------------------------------------------------------------------


def _iso(value) -> str | None:
    from ..session_host.views import iso

    return iso(value)


def _item(kind: str, source_id: str, title: str, *, meta: str = "", source: str = "assistant",
          created_at=None, pack: str | None = None, impact: list | None = None, slice_=None, run=None,
          confidence=None, batchable: bool = True, extra: dict | None = None, fingerprint: str = "",
          actions: list[str] | None = None) -> dict:
    item = {
        "id": f"{kind}:{source_id}", "kind": kind, "kind_label": KIND_LABELS[kind], "title": title, "meta": meta,
        "slice": slice_, "run": run, "source": source, "confidence": confidence,
        "high_confidence": bool(confidence is not None and confidence >= 0.8), "pack": pack,
        "impact": impact or [], "created_at": _iso(created_at or time.time()),
        "actions": [*(ACTIONS[kind] if actions is None else actions), "dismiss"], "batchable": batchable,
    }
    if extra:
        item["_extra"] = extra
    item["_fp"] = fingerprint or source_id
    return item


def _decisions(project_id: str) -> list[dict]:
    try:
        from ..agent_sessions import manager
    except Exception:  # noqa: BLE001
        return []
    session = manager.get(project_id)
    if session is None:
        return []
    items = []
    for request in list(session.pending_permissions.values()):
        if request.future is None or request.future.done():
            continue
        for question in request.questions or []:
            if question["question_id"] in request.answers:
                continue
            recommended = next((option for option in question.get("options") or [] if option.get("recommended")), None)
            meta = f"{len([o for o in question.get('options') or [] if not o.get('custom')])} 个选项"
            if recommended:
                meta += f" · 推荐 {recommended.get('key') or recommended.get('label')}"
            items.append(_item("decision", question["question_id"], question.get("title") or "需要你决定",
                               meta=meta, source="assistant", batchable=False,
                               impact=[{"label": "改动预览", "items": [ref.get("name") or ref.get("title") or ""
                                                                    for ref in question.get("affected") or []]}]
                               if question.get("affected") else [],
                               extra={"question": question, "conversation_id": session.conversation_id}))
    return items


def _resumables(project_id: str, root: Path, flow: dict) -> list[dict]:
    from ..session_host import exec_records

    items = []
    for record in exec_records.query(project_id, ["interrupted"]):
        view = exec_records.view(record, root=root, flow=flow)
        if not view["resumable"]:
            continue
        phase = view["phase"]["label"]
        items.append(_item("resumable", record["id"], f"「{view['command_label']}」没做完",
                           meta=(f"停在「{phase}」 · " if phase else "") + ((record.get("interrupted") or {}).get("reason_text") or ""),
                           source="restart" if (record.get("interrupted") or {}).get("reason") == "restart" else "assistant",
                           created_at=(record.get("interrupted") or {}).get("at") or record.get("ended_at"),
                           extra={"exec": view},
                           fingerprint=f"{record['id']}:{(record.get('interrupted') or {}).get('at')}"))
    return items


def _changesets(root: Path) -> list[dict]:
    from ...kernel.changesets import store

    record = store.current(root)
    if record is None or record.get("state") not in ("pending_confirm", "conflict"):
        return []
    names = [ref.get("name") or ref.get("title") or ref.get("key") for ref in
             (record.get("entries") or []) + (record.get("artifacts") or [])]
    impact = [{"label": "涉及", "items": names[:20]}] if names else []
    if record["state"] == "conflict":
        conflict = record.get("conflict") or {}
        return [_item("changeset_conflict", record["id"], "合入时有冲突，选好保留哪一边",
                      meta=f"{len(conflict.get('entities') or [])} 处冲突", created_at=conflict.get("at"),
                      impact=impact, batchable=False, extra={"changeset": record},
                      fingerprint=f"{record['id']}:{conflict.get('at')}")]
    return [_item("pending_changeset", record["id"], record.get("purpose") or "助手的修改等你确认",
                  meta=f"改了 {int(record.get('entity_count') or 0)} 项", created_at=record.get("updated_at"),
                  impact=impact, batchable=False, extra={"changeset": record},
                  fingerprint=f"{record['id']}:{record.get('tree')}")]


def _node_items(root: Path, flow: dict) -> list[dict]:
    from . import model
    from . import state as node_state

    hits = model.flow_state(root).node_hits(flow, root)
    states = node_state.compute(root, flow, hits)
    by_id = {node["id"]: node for node in flow.get("nodes") or []}
    reviews = {item["node_id"]: item for item in node_state.reviews(root)}
    items = []
    for node_id, info in states.items():
        node = by_id.get(node_id) or {}
        pack = node.get("pack") if node.get("pack") not in (None, "core") else None
        if info["state"] == "stale":
            items.append(_item("stale", node_id, f"「{node.get('title') or node_id}」过期了",
                               meta=f"上游（{'、'.join(node.get('stale_on') or [])}）改过", source="assistant",
                               pack=pack, extra={"node": node},
                               fingerprint=node_state.fingerprint(root, list(node.get("stale_on") or []))))
        review = reviews.get(node_id)
        if review and info["state"] in ("confirmed", "stale"):
            source = review.get("source") or "upgrade"
            title = {"upgrade": "这一步的方法升级了，旧结论要你看一眼", "import": "导入带进来的内容要你看一眼",
                     "autobuild": "自主构建完成的内容要你复核"}.get(source, "需要复核")
            items.append(_item("review", node_id, f"「{node.get('title') or node_id}」：{title}",
                               meta=review.get("note") or "", source=source, pack=pack, created_at=review.get("at"),
                               run={"run_id": review["run_id"], "run_no": 0, "at": _iso(review.get("at"))}
                               if review.get("run_id") else None, extra={"node": node, "review": review},
                               fingerprint=f"{source}:{review.get('at')}",
                               # 「撤回这一片」只对自主构建合入的片
                               actions=ACTIONS["review"] if source == "autobuild" else ["accept", "rejudge"]))
    return items


def _reports(root: Path) -> list[dict]:
    from ..report_items import parse

    folder = Path(root) / "reports"
    items = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pending = sum(1 for line in text.splitlines() if line.lstrip().startswith("- [ ]"))
        if not pending:
            continue
        report_id = path.stem
        import hashlib

        items.append(_item("report", report_id, f"报告「{path.name}」还有 {pending} 条待处理",
                           meta=parse(text).get("summary", "")[:80], source="assistant",
                           created_at=path.stat().st_mtime, extra={"path": f"reports/{path.name}", "pending": pending},
                           fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]))
    return items


def _feedback(root: Path) -> list[dict]:
    from .. import feedback_inbox

    folder = Path(root) / feedback_inbox.FEEDBACK_DIR
    items = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict) or not document.get("feedback_id"):
            continue
        view = feedback_inbox.view(f"{feedback_inbox.FEEDBACK_DIR}/{path.name}", document, "")
        failed = view["verdicts"].get("fail", 0)
        if not failed:
            continue  # 全部通过的批次没有要处理的（审查第 22 条）
        items.append(_item("feedback", document["feedback_id"], f"外部反馈：{view['item_count']} 条",
                           meta=f"{failed} 条没通过" if failed else "", source="external", pack="evo",
                           created_at=path.stat().st_mtime, extra={"feedback": view}))
    return items


def _format_migrations(manager, root: Path) -> list[dict]:
    from ...kernel.artifacts import index
    from ...kernel.artifacts.registry import types_for_project

    items = []
    try:
        types = types_for_project(manager, root)
    except Exception:  # noqa: BLE001
        return []
    for atype in types:
        if not atype.field("format_version"):
            continue
        try:
            rows = index.refresh(atype, root)
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            current = (row.get("fields") or {}).get("format_version")
            if current in (None, "") or str(current) == atype.schema_version:
                continue
            items.append(_item("format_migration", f"{atype.type}/{row['key']}",
                               f"「{row['title']}」还是旧格式（{current} → {atype.schema_version}）",
                               source="plugin", pack=atype.pack,
                               extra={"type": atype.type, "key": row["key"], "from": str(current),
                                      "to": atype.schema_version},
                               fingerprint=f"{current}->{atype.schema_version}"))
    return items


def _change_lists(root: Path) -> list[dict]:
    from . import change_lists

    items = []
    for item in change_lists.all_lists(root):
        if item.get("state") == "executed":
            continue
        low = sum(1 for point in item.get("points") or [] if point.get("confidence") == "low")
        items.append(_item("change_list", item["id"], f"变更清单：{item.get('source', {}).get('label') or item['id']}",
                           meta=f"{len(item.get('points') or [])} 个变更点" + (f" · {low} 个把握低" if low else ""),
                           source="materials", created_at=item.get("updated_at"), batchable=False,
                           extra={"change_list": item}, fingerprint=str(item.get("revision") or "")))
    return items


def collect(manager, project_id: str) -> list[dict]:
    """全部事项（含被忽略的，带 `_dismissed`），新的在前；单个来源出错只跳过这一类。"""
    import logging

    from . import model

    root = manager.project_root(project_id)
    flow = model.load_for(root)
    sources = [
        lambda: _decisions(project_id),
        lambda: _changesets(root),
        lambda: _resumables(project_id, root, flow),
        lambda: _node_items(root, flow),
        lambda: _reports(root),
        lambda: _feedback(root),
        lambda: _format_migrations(manager, root),
        lambda: _change_lists(root),
    ]
    items: list[dict] = []
    for source in sources:
        try:
            items += source()
        except Exception:  # noqa: BLE001 - 一类来源出错不挡别的
            logging.getLogger(__name__).exception("待处理来源出错（项目 %s）", project_id)
    ignored = dismissed(root)
    for item in items:
        record = ignored.get(item["id"])
        # 来源变了（指纹不同）就不再算忽略；旧版没记指纹的忽略照旧生效
        item["_dismissed"] = record is not None and (not record.get("fp") or record.get("fp") == item.get("_fp"))
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def public(item: dict) -> dict:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def counts(items: list[dict]) -> dict:
    active = [item for item in items if not item.get("_dismissed")]
    by_filter = {name: sum(1 for item in active if kinds is None or item["kind"] in kinds)
                 for name, kinds in FILTERS.items()}
    by_pack: dict[str, int] = {}
    for item in active:
        if item.get("pack"):
            by_pack[item["pack"]] = by_pack.get(item["pack"], 0) + 1
    return {"total": len(active), "by_filter": by_filter, "by_pack": by_pack}


def listing(manager, project_id: str, *, filter_: str = "all", kinds: list[str] | None = None,
            pack: str | None = None, run_id: str | None = None) -> tuple[list[dict], dict, dict]:
    """IN-01：(筛过的事项, counts, today)。"""
    items = collect(manager, project_id)
    total = counts(items)
    wanted = FILTERS.get(filter_)
    picked = [item for item in items if not item.get("_dismissed")
              and (wanted is None or item["kind"] in wanted)
              and (not kinds or item["kind"] in kinds)
              and (not pack or item.get("pack") == pack)
              and (not run_id or (item.get("run") or {}).get("run_id") == run_id)]
    root = manager.project_root(project_id)
    handled = handled_today(root)
    remaining = total["by_filter"]
    today = {"handled": handled, "total": handled + total["total"],
             "eta_minutes": total["total"] * MINUTES_PER_ITEM, "remaining_by_filter": remaining}
    return picked, total, today


def find(manager, project_id: str, item_id: str) -> dict | None:
    return next((item for item in collect(manager, project_id) if item["id"] == item_id), None)


# ---------------------------------------------------------------------------
# 项目流 inbox.updated（EP-02）与左栏角标
# ---------------------------------------------------------------------------

_last: dict[str, dict] = {}
_last_lock = threading.Lock()


def refresh(manager, project_id: str) -> dict | None:
    """重算待处理，和上次比：有增减、内容变化就发 inbox.updated。"""
    from .. import streams

    items = collect(manager, project_id)
    current = {item["id"]: json.dumps(public(item), ensure_ascii=False, sort_keys=True)
               for item in items if not item.get("_dismissed")}
    with _last_lock:
        previous = _last.get(project_id)
        _last[project_id] = current
    if previous is not None and previous == current:
        return None
    previous = previous or {}
    data = {"counts": counts(items),
            "added": [key for key in current if key not in previous],
            "removed": [key for key in previous if key not in current],
            "changed": [key for key in current if key in previous and previous[key] != current[key]]}
    return streams.publish_project(project_id, "inbox.updated", data)


def badge(manager, project_id: str, pack: str) -> int:
    """左栏插件页角标（B4c 的 register_badge_provider）：待处理里属于这个包的事项数。"""
    return counts(collect(manager, project_id))["by_pack"].get(pack, 0)


def forget(project_id: str) -> None:
    with _last_lock:
        _last.pop(project_id, None)
