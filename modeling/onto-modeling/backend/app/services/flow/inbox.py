"""待处理汇总（详设 4.7、8.2 风险应对；契约 3.13 IN-01、IN-02、7.9 InboxItem、EP-02）。

待处理不另存一份状态：处理完一项，它的来源就变了，计数随之减少。唯一自存的是「忽略」记录与当天处理过的记录
（主线工作区 `.onto/run/`，不进 git）。现在汇总的来源：

| kind | 来源 |
|---|---|
| decision 决策 | 在线会话里没回答的决策卡；人工节点出的、没回答的决策卡（节点不在当前流程里的不列）；建模蓝图里没回答的问题 |
| resumable 续跑 | 中断的执行记录（能续跑的接着做，能力升级、输入改过的按新版重新执行） |
| changeset_conflict 合入冲突 | 合入时检测到实体冲突的变更集（F3 起助手每轮自动合入，这是用户唯一会看到变更集的情况） |
| stale 过期 | 过期的节点（所属包版本被熔断的不列） |
| review 需复核 | 节点升级、导入；熔断的同上。自主构建的按决策台账出：低于阈值的一条一项，高置信的同一次运行合成一项 |
| suspension 挂起 | 自主构建挂起的问题（撤回了的段上的不列） |
| report 报告待处理 | 报告里还有没处理的 `[ ]` 条目，一份报告一项 |
| format_migration 格式待迁移 | 插件产物的格式版本比包里登记的旧 |
| change_list 变更清单 | 增量更新识别出、还没执行的变更点清单 |
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

KIND_LABELS = {
    "decision": "决策", "change_list": "变更清单", "review": "需复核",
    "suspension": "挂起", "stale": "过期", "resumable": "续跑", "report": "报告待处理",
    "changeset_conflict": "冲突", "format_migration": "格式待迁移",
    "notification": "通知", "check_findings": "检查未过",
}
ACTIONS = {
    "decision": ["answer"],
    "change_list": ["accept_points", "rewrite_point", "execute"], "review": ["accept", "rejudge", "revert_slice"],
    "suspension": ["answer", "use_generic"], "stale": ["refresh", "keep_stale"], "resumable": ["resume", "abandon"],
    "report": ["send_to_assistant"],
    "changeset_conflict": ["resolve", "discard"], "format_migration": ["migrate"], "notification": [],
    "check_findings": ["send_to_assistant"],
}
FILTERS = {
    "all": None, "autobuild_review": ("review", "suspension"), "decision": ("decision",),
    "change_list": ("change_list",), "stale": ("stale",), "resumable": ("resumable",),
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


def _item(kind: str, source_id: str, title: str, *, source: str = "assistant",
          created_at=None, pack: str | None = None, impact: list | None = None, slice_=None, run=None,
          confidence=None, batchable: bool = True, extra: dict | None = None, fingerprint: str = "",
          actions: list[str] | None = None) -> dict:
    item = {
        "id": f"{kind}:{source_id}", "kind": kind, "kind_label": KIND_LABELS[kind], "title": title,
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
            items.append(_item("decision", question["question_id"], question.get("title") or "需要你决定",
                               source="assistant", batchable=False,
                               impact=[{"label": "改动预览", "items": [ref.get("name") or ref.get("title") or ""
                                                                    for ref in question.get("affected") or []]}]
                               if question.get("affected") else [],
                               extra={"question": question, "conversation_id": session.conversation_id}))
    return items


def _human_nodes(root: Path, flow: dict) -> list[dict]:
    """人工节点（ADR-0066）的决策卡：FL-05 出过、还没回答的。包停用了、节点不在当前流程里的不列。"""
    from . import node_kinds

    by_id = {node["id"]: node for node in flow.get("nodes") or []}
    items = []
    for question_id, entry in node_kinds.open_questions(root).items():
        node = by_id.get(entry.get("node_id"))
        if node is None:
            continue
        question = entry["question"]
        pack = node.get("pack") if node.get("pack") not in (None, "core") else None
        items.append(_item("decision", question_id, question.get("title") or "需要你决定",
                           source="plugin", pack=pack,
                           created_at=entry.get("asked_at"), batchable=False,
                           extra={"question": question, "conversation_id": question.get("conversation_id"),
                                  "human_node": node["id"]}))
    return items


def _blueprint_questions(manager, project_id: str) -> list[dict]:
    """建模蓝图里还没回答的问题（契约 BP-03「没答的问题留在待处理」）：编号 `decision:bq-<问题>`。"""
    from . import blueprint

    doc = blueprint.effective_doc(manager, project_id)
    try:
        updated = datetime.fromisoformat(str((doc or {}).get("updated_at"))).timestamp()
    except ValueError:
        updated = None
    items = []
    for question in (doc or {}).get("questions") or []:
        if question.get("answer"):
            continue
        source_id = f"bq-{question['id']}"
        options = [{"id": option["id"], "key": option["id"], "label": option["label"], "consequence": "",
                    "recommended": False, "preview": None, "custom": False} for option in question.get("options") or []]
        detail = {"question_id": source_id, "kind": "decision", "conversation_id": None, "exec_id": None,
                  "step_label": "建模蓝图", "eyebrow": "", "title": question.get("title") or "",
                  "background_md": question.get("background") or "", "anchors": [], "options": options,
                  "multi": False, "allow_custom": True, "custom_placeholder": "", "state": "open", "answer": None,
                  "inbox_item_id": f"decision:{source_id}", "affected": []}
        items.append(_item("decision", source_id, question.get("title") or "建模蓝图的问题",
                           source="assistant", batchable=False,
                           created_at=updated, extra={"question": detail, "blueprint_question": question["id"]},
                           fingerprint=f"{source_id}:{doc.get('version_no')}"))
    return items


def _resumables(project_id: str, root: Path, flow: dict) -> list[dict]:
    from ..session_host import exec_records

    items = []
    for record in exec_records.query(project_id, ["interrupted"]):
        if record.get("run_id"):
            continue  # 自主构建的一步由运行器按执行记录续跑（AB-09）
        # 能力升级、输入改过的也列：「继续」按新版重新执行（F7），不再只剩「放弃」
        view = exec_records.view(record, root=root, flow=flow)
        items.append(_item("resumable", record["id"], f"「{view['command_label']}」没做完",
                           source="restart" if (record.get("interrupted") or {}).get("reason") == "restart" else "assistant",
                           created_at=(record.get("interrupted") or {}).get("at") or record.get("ended_at"),
                           extra={"exec": view},
                           fingerprint=f"{record['id']}:{(record.get('interrupted') or {}).get('at')}"))
    return items


def _changesets(root: Path) -> list[dict]:
    """合入冲突的变更集（F3：没有「修改待确认」，助手每一轮结束就合入）。"""
    from ...kernel.changesets import store

    record = store.current(root)
    if record is None or record.get("state") != "conflict":
        return []
    refs = (record.get("entries") or []) + (record.get("artifacts") or [])
    names = [ref.get("name") or ref.get("title") or ref.get("key") for ref in refs]
    impact = [{"label": "涉及", "items": names[:20]}] if names else []
    conflict = record.get("conflict") or {}
    return [_item("changeset_conflict", record["id"], "这一轮和你的修改有冲突，选好保留哪一边",
                  created_at=conflict.get("at"),
                  impact=impact, batchable=False, extra={"changeset": record},
                  fingerprint=f"{record['id']}:{conflict.get('at')}")]


def _check_findings(root: Path) -> list[dict]:
    """平台代存的那一轮（助手没提交完）检查没过的错误（F8）：只看最近一个合入的变更集，最多列 20 条。"""
    from ...kernel.changesets import store

    record = next(iter(store.all_records(root)), None)
    checks = (record or {}).get("checks") or {}
    if not record or record.get("state") != "merged":
        return []
    if not record.get("collected") or not int(checks.get("errors") or 0):
        return []
    errors = [item for item in checks.get("findings") or [] if item.get("level") == "error"]
    messages = [str(item.get("message") or item.get("label") or "")[:200] for item in errors][:20]
    impact = [{"label": "问题", "items": [text for text in messages if text]}] if messages else []
    return [_item("check_findings", record["id"], f"平台代存的这一轮有 {int(checks['errors'])} 处检查没过",
                  source="assistant", created_at=record.get("closed_at"), impact=impact, batchable=False,
                  extra={"messages": messages}, fingerprint=f"{record['id']}:{checks.get('errors')}")]


def _node_items(root: Path, flow: dict) -> list[dict]:
    from . import model
    from . import state as node_state

    from .view import node_states

    module = model.flow_state(root)
    hits = module.node_hits(flow, root)
    # 所属包版本被熔断的节点在流程里算跳过、开始不了（FL-05 回 409）：不列，免得 IN-04 选中它；
    # 状态与 FL-01 同一份（熔断的节点不挡别的节点）
    states, _context, broken = node_states(root, flow, hits)
    handed_down = _handed_down_judge(root, module)
    flow_nodes = [node for node in flow.get("nodes") or [] if node["id"] in states]
    state_of = {node_id: info["state"] for node_id, info in states.items()}
    by_id = {node["id"]: node for node in flow.get("nodes") or []}
    reviews = {item["node_id"]: item for item in node_state.reviews(root)}
    items = []
    for node_id, info in states.items():
        if node_id in broken:
            continue
        node = by_id.get(node_id) or {}
        pack = node.get("pack") if node.get("pack") not in (None, "core") else None
        # 已交给后面的核对节点的过期（校验自己修对象让建模、指标过期）：建议条不推刷新，这里也不列（ADR-0090）
        if info["state"] == "stale" and not handed_down(node, flow_nodes, state_of):
            items.append(_item("stale", node_id, f"「{node.get('title') or node_id}」过期了",
                               source="assistant",
                               pack=pack, extra={"node": node},
                               fingerprint=node_state.fingerprint(root, list(node.get("stale_on") or []))))
        review = reviews.get(node_id)
        if review and review.get("source") == "autobuild":
            review = None  # 自主构建的需复核按决策台账出（_autobuild）
        if review and info["state"] in ("confirmed", "stale"):
            source = review.get("source") or "upgrade"
            title = {"upgrade": "这一步的方法升级了，旧结论要你看一眼", "import": "导入带进来的内容要你看一眼",
                     "autobuild": "自主构建完成的内容要你复核"}.get(source, "需要复核")
            items.append(_item("review", node_id, f"「{node.get('title') or node_id}」：{title}",
                               source=source, pack=pack, created_at=review.get("at"),
                               run={"run_id": review["run_id"], "run_no": 0, "at": _iso(review.get("at"))}
                               if review.get("run_id") else None, extra={"node": node, "review": review},
                               fingerprint=f"{source}:{review.get('at')}",
                               # 「撤回这一段」只对自主构建合入的段
                               actions=ACTIONS["review"] if source == "autobuild" else ["accept", "rejudge"]))
    return items


def _handed_down_judge(root: Path, module):
    """flow_state 的「已交给核对节点」判定，和建议条用同一个版本（项目锁定的旧 core 不够新时用平台镜像版本）；
    再没有就都列。"""
    from .view import _suggest_module

    return getattr(_suggest_module(root, module), "handed_down", None) or (lambda *_args: False)


def _unit_name(slice_ref: dict) -> str:
    """不分段时那一段的名字是「全部」，给人看的时候叫建模（同前端 unitTitle）。"""
    return "建模" if slice_ref.get("id") == "all" else slice_ref.get("name") or ""


def _autobuild(project_id: str) -> list[dict]:
    """自主构建复核（详设 4.7、8.5；契约 F2 补充第 12 条）：低于阈值的自动决定一条一项；同一次运行里高置信的
    合成一项（`review:<运行>:high`）；挂起的问题一个一项。撤回了的段上的不列。"""
    from ..autobuild import store as runs
    from ..autobuild import views

    items = []
    for run in runs.runs(project_id):
        threshold = float(((run.get("policy") or {}).get("advanced") or {}).get("confidence_threshold") or 0.8)
        ref = {"run_id": run["run_id"], "run_no": run["run_no"]}
        ended = run.get("state") in runs.ENDED
        slices = run.get("slices") or {}
        pending = [entry for entry in views.live_ledger(run) if entry.get("review_state") == "pending"]
        sliced = sum(1 for unit in run.get("units") or [] if unit.get("kind") == "slice") > 1
        impact_label = "分段" if sliced else "步骤"
        high = [entry for entry in pending if float(entry.get("confidence") or 0) >= threshold]
        for entry in pending:
            if entry in high:
                continue
            slice_ref = entry.get("slice") or {}
            revertible = ended and bool((slices.get(slice_ref.get("id")) or {}).get("commit"))
            items.append(_item("review", entry["id"], entry.get("summary") or entry.get("question") or "",
                               source="autobuild", created_at=entry.get("at"), slice_=slice_ref,
                               run={**ref, "at": _iso(entry.get("at"))}, confidence=entry.get("confidence"),
                               batchable=False, impact=[{"label": impact_label, "items": [_unit_name(slice_ref)]}],
                               extra={"entry": entry, "run": run},
                               actions=["accept", "rejudge", "revert_slice"] if revertible else ["accept", "rejudge"]))
            items[-1]["high_confidence"] = False  # 低于这次运行的阈值
        if high:
            names = list(dict.fromkeys(_unit_name(entry.get("slice") or {}) for entry in high))
            item = _item("review", f"{run['run_id']}:high", f"高置信的 {len(high)} 个自动决定",
                         source="autobuild", created_at=high[-1].get("at"),
                         slice_={"id": None, "name": f"{len(names)} 段" if sliced else "、".join(names)},
                         run={**ref, "at": None},
                         confidence=min(float(entry.get("confidence") or 0) for entry in high),
                         impact=[{"label": impact_label, "items": names}], extra={"group": high, "run": run},
                         fingerprint=f"high:{','.join(entry['id'] for entry in high)}", actions=["accept"])
            item["high_confidence"] = True
            items.append(item)
        for suspended in views.live_suspensions(run):
            # 留到下一次的也列着，直到下一次运行带着回答补做完（审查 I）；这时还可以改成现在回答
            if suspended.get("state") not in views.UNRESOLVED:
                continue
            actions = ["use_generic"] if suspended.get("kind") == "unreadable_material" else ["answer", "use_generic"]
            items.append(_item("suspension", suspended["id"], suspended.get("title") or "", source="autobuild",
                               created_at=suspended.get("at"), slice_=suspended.get("slice"),
                               run={**ref, "at": _iso(suspended.get("at"))}, batchable=False,
                               extra={"suspension": suspended, "run": run}, actions=actions))
    return items


def _pending_confident(entry: dict, threshold: float) -> bool:
    """还没复核、置信度又够高的决定。"""
    return entry.get("review_state") == "pending" and float(entry.get("confidence") or 0) >= threshold


def autobuild_summary(project_id: str, run_id: str | None = None) -> dict | None:
    """IN-01 `filter=autobuild_review` 的汇总条：指定的运行，或最近一次。"""
    from ..autobuild import store as runs
    from ..autobuild import views

    items = runs.runs(project_id)
    run = next((item for item in items if not run_id or item["run_id"] == run_id), None)
    if run is None:
        return None
    report = views.report(run)
    ledger = views.live_ledger(run)
    threshold = float(((run.get("policy") or {}).get("advanced") or {}).get("confidence_threshold") or 0.8)
    return {
        "run_id": run["run_id"], "run_no": run["run_no"], "started_at": report["started_at"],
        "ended_at": report["ended_at"], "state": run["state"], "cost": report["metrics"]["cost"],
        "decisions": len(ledger), "high_confidence": report["metrics"]["decisions"]["high_confidence"],
        "needs_look": report["metrics"]["decisions"]["needs_look"], "suspended": report["metrics"]["suspended"],
        "assumptions": sum(1 for entry in ledger if entry.get("assumption")),
        "high_confidence_pending": sum(1 for entry in ledger if _pending_confident(entry, threshold)),
    }


def _reports(root: Path) -> list[dict]:
    folder = Path(root) / "reports"
    items = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() and not folder.is_symlink() else []:
        if path.is_symlink():  # 符号链接不读（可以指到工作区外）
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        pending = sum(1 for line in text.splitlines() if line.lstrip().startswith("- [ ]"))
        if not pending:
            continue
        report_id = path.stem
        import hashlib

        heading = re.search(r"^#\s+(.+)$", text, re.M)
        title = heading.group(1).strip()[:60] if heading else "检查报告"
        items.append(_item("report", report_id, f"「{title}」还有 {pending} 条待处理",
                           source="assistant",
                           created_at=path.stat().st_mtime, extra={"path": f"reports/{path.name}", "pending": pending},
                           fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]))
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
        items.append(_item("change_list", item["id"], f"变更清单：{item.get('source', {}).get('label') or item['id']}",
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
        lambda: _human_nodes(root, flow),
        lambda: _blueprint_questions(manager, project_id),
        lambda: _changesets(root),
        lambda: _check_findings(root),
        lambda: _resumables(project_id, root, flow),
        lambda: _node_items(root, flow),
        lambda: _reports(root),
        lambda: _format_migrations(manager, root),
        lambda: _change_lists(root),
        lambda: _autobuild(project_id),
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
    by_filter = {name: sum(1 for item in active if FILTERS[name] is None or item["kind"] in FILTERS[name])
                 for name in FILTERS}
    by_pack: dict[str, int] = {}
    for item in active:
        if item.get("pack"):
            by_pack[item["pack"]] = by_pack.get(item["pack"], 0) + 1
    return {"total": len(active), "by_filter": by_filter, "by_pack": by_pack}


def _listed(item: dict, wanted, kinds, pack: str | None, run_id: str | None) -> bool:
    """IN-01 的筛选：没被忽略，且符合分类、类型、插件、运行。"""
    if item.get("_dismissed"):
        return False
    if wanted is not None and item["kind"] not in wanted:
        return False
    if kinds and item["kind"] not in kinds:
        return False
    if pack and item.get("pack") != pack:
        return False
    return not run_id or (item.get("run") or {}).get("run_id") == run_id


def listing(manager, project_id: str, *, filter_: str = "all", kinds: list[str] | None = None,
            pack: str | None = None, run_id: str | None = None) -> tuple[list[dict], dict, dict]:
    """IN-01：(筛过的事项, counts, today)。"""
    items = collect(manager, project_id)
    total = counts(items)
    wanted = FILTERS.get(filter_)
    picked = [item for item in items if _listed(item, wanted, kinds, pack, run_id)]
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


def badges(manager, project_id: str) -> dict[str, int]:
    """左栏插件页角标（B4c 的 register_badge_provider）：待处理里各包的事项数，汇总一次给全部包。"""
    return dict(counts(collect(manager, project_id))["by_pack"])


def forget(project_id: str) -> None:
    with _last_lock:
        _last.pop(project_id, None)
