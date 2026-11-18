"""运行记录 → 契约形状（7.15 AutobuildRunSummary / AutobuildRun / AutobuildReport / LedgerEntry / Suspension）与
对话里的四种卡片（4.7 autobuild_start、slice_progress、run_report；自动决定、挂起嵌在单元进展块的时间线里）。

一个单元一行：新运行的单元是节点，老运行（这一版之前开始的）还有按段的「全部」× 几步，只有它们的流水线有步骤列
（`stages`）。契约里的 `slice*` 字段名不变，装的是单元。界面文字只给功能必需的：状态标签、数字、单元名、问题与选择本身。
"""

from __future__ import annotations

import time

from .. import model_pricing
from . import budget, policy, store

STEP_CELL = {"done": "auto", "running": "running", "queued": "queued", "skipped": "skipped", "partial": "auto"}
CARD_STATE = {"queued": "queued", "running": "running", "done": "done", "partial": "partial", "skipped": "queued"}
LEDGER_FIELDS = ("id", "run_id", "at", "slice", "question", "chosen", "options", "confidence", "threshold",
                 "assumption", "anchors", "affected", "review_state", "rejudged_to", "reason_text", "summary")
SUSPENSION_FIELDS = ("id", "run_id", "at", "slice", "title", "reason", "handled_as", "kind", "options",
                     "allow_custom", "actions", "state")
# 还没解决的挂起问题：没回答的、留到下一次还没补做的
UNRESOLVED = ("open", "deferred")
KINDS = {"object": "objects", "operation": "operations", "rule": "rules"}
KIND_LABELS = {"objects": "对象", "operations": "操作", "rules": "规则"}
# 老运行按段的流水线的列：按这个顺序排，名字按这个叫（F8 之前还有规则、写入即查、场景）
STAGE_LABELS = {"object": "对象", "operation": "操作", "rule": "规则", "check": "写入即查", "scenario": "场景"}


def iso(value) -> str | None:
    from ..session_host.views import iso as _iso

    return _iso(value)


def money(amount, currency: str) -> dict:
    return {"amount": None if amount is None else round(float(amount), 2), "currency": currency}


def currency_of(run: dict) -> str:
    """费用的币种跟模型单价表走（SY-18）；F6 之前的运行记在策略的预算里。"""
    old = ((run.get("policy") or {}).get("budget") or {}).get("max_cost") or {}
    return old.get("currency") or model_pricing.load()["currency"]


def _reverted(run: dict) -> set[str]:
    return {unit_id for unit_id, info in store.unit_infos(run).items() if info.get("reverted")}


def live_ledger(run: dict) -> list[dict]:
    """撤回了的单元上的决定不再算。"""
    reverted = _reverted(run)
    return [entry for entry in run.get("ledger") or [] if (entry.get("slice") or {}).get("id") not in reverted]


def live_suspensions(run: dict) -> list[dict]:
    reverted = _reverted(run)
    return [item for item in run.get("suspensions") or [] if (item.get("slice") or {}).get("id") not in reverted]


def ledger_entry(entry: dict) -> dict:
    data = {key: entry.get(key) for key in LEDGER_FIELDS}
    data["at"] = iso(entry.get("at"))
    return data


def suspension(item: dict) -> dict:
    data = {key: item.get(key) for key in SUSPENSION_FIELDS}
    data["at"] = iso(item.get("at"))
    if item.get("material"):
        data["material"] = item["material"]
    return data


def _units(run: dict, kind: str | None = None) -> list[dict]:
    return [unit for unit in run.get("units") or [] if kind is None or unit["kind"] == kind]


def _info(run: dict, unit_id: str) -> dict:
    return store.unit_info(run, unit_id)


def _checks(run: dict, infos: list[dict]) -> dict:
    """错误与提醒数：运行结束后是最终版本相对运行开始时的结果；前面单元的提醒常在校验里修掉，加起来会多算。
    还在跑、老运行按各单元合入时的结果相加。"""
    final = run.get("final_checks")
    if final:
        return {"errors": int(final.get("errors") or 0), "warnings": int(final.get("warnings") or 0)}
    return {key: sum(int((info.get("checks") or {}).get(key) or 0) for info in infos) for key in ("errors", "warnings")}


def _current(run: dict) -> dict | None:
    cursor = run.get("cursor") or {}
    unit = next((item for item in _units(run) if item["id"] == cursor.get("unit")), None)
    if unit is None or run.get("state") in store.ENDED:
        return None
    step = next((item for item in unit["steps"] if item["id"] == cursor.get("step")), None)
    return {"slice_id": unit["id"], "slice_name": unit["name"], "step_label": (step or {}).get("action") or ""}


def _step_counts(run: dict) -> tuple[int, int]:
    total = done = 0
    for unit in _units(run):
        info = _info(run, unit["id"])
        for step in unit["steps"]:
            total += 1
            if (info.get("steps") or {}).get(step["id"]) in ("done", "partial", "skipped"):
                done += 1
    return done, total


def summary(run: dict, now: float | None = None) -> dict:
    """AutobuildRunSummary（EP-14 的载荷、AB-18 的一行）。"""
    now = now or time.time()
    currency = currency_of(run)
    units = _units(run)
    done_units = sum(1 for unit in units if _info(run, unit["id"]).get("state") in ("done", "partial"))
    elapsed = budget.elapsed_ms(run, now)
    steps_done, steps_total = _step_counts(run)
    estimate = None
    if run.get("state") not in store.ENDED and steps_done and steps_total:
        estimate = int(elapsed * steps_total / steps_done)
    ledger = live_ledger(run)
    return {
        "run_id": run["run_id"], "run_no": run["run_no"], "state": run["state"], "current": _current(run),
        "elapsed_ms": elapsed, "est_total_ms": estimate,
        "tokens": {"used": int((run.get("budget") or {}).get("tokens") or 0)},
        "cost": {"used": money((run.get("budget") or {}).get("cost") or 0, currency)},
        "slices": {"done": done_units, "total": len(units)},
        "decisions": len(ledger),
        "suspended": sum(1 for item in live_suspensions(run) if item.get("state") in UNRESOLVED),
        "needs_review": sum(1 for entry in ledger if entry.get("review_state") == "pending"),
        "started_at": iso(run.get("started_at") or run.get("created_at")), "ended_at": iso(run.get("ended_at")),
        # 配额用完而暂停（补足后续跑）；结束后看过、确认了（AB-19，运行条收起）
        "pause_reason": run.get("pause_reason") if run.get("state") == "paused" else None,
        "acknowledged": bool(run.get("acknowledged_at")),
    }


def stages(run: dict) -> list[dict]:
    """老运行按段的流水线的列 [{id, label}]：按段的单元里的步骤，加上开始时记下的各段各步的状态（`before`、
    `before_rows`，只有老运行记）里出现的；新运行没有。"""
    labels: dict[str, str] = {}
    for unit in _units(run, "slice"):
        for step in unit.get("steps") or []:
            labels.setdefault(step["id"], STAGE_LABELS.get(step["id"]) or step.get("label") or step["id"])
        for key in unit.get("before") or {}:
            labels.setdefault(key, STAGE_LABELS.get(key, key))
    for row in run.get("before_rows") or []:
        for key in row.get("stages") or {}:
            labels.setdefault(key, STAGE_LABELS.get(key, key))
    order = list(STAGE_LABELS)
    keys = sorted(labels, key=lambda key: order.index(key) if key in order else len(order))
    return [{"id": key, "label": labels[key]} for key in keys]


def _row(run: dict, unit: dict, stage_ids: list[str]) -> dict:
    """一个单元一行；按段的单元按步骤列填格子，节点单元没有格子。"""
    info = _info(run, unit["id"])
    row = {"slice_id": unit["id"], "slice_name": unit["name"], "state": info.get("state") or "queued", "cells": {},
           "result": result_text(run, unit["id"])}
    if unit["kind"] != "slice":
        return row
    steps = {step["id"]: (info.get("steps") or {}).get(step["id"], "queued") for step in unit["steps"]}
    before = unit.get("before") or {}
    merged = bool(info.get("commit"))
    cells = row["cells"]
    for stage in stage_ids:
        if stage in steps:
            value = STEP_CELL.get(steps[stage], "queued")
            cells[stage] = "skipped" if value == "auto" and steps[stage] == "partial" and not merged else value
        elif before.get(stage) in ("confirmed", "auto"):
            cells[stage] = "confirmed"
        elif before.get(stage) == "skipped" or stage in (unit.get("left_out") or ()):
            cells[stage] = "skipped"
        elif merged:
            cells[stage] = "auto"
        else:
            cells[stage] = "queued"
    return row


def result_text(run: dict, unit_id: str) -> str:
    info = _info(run, unit_id)
    state = info.get("state") or "queued"
    suspended = sum(1 for item in live_suspensions(run) if (item.get("slice") or {}).get("id") == unit_id)
    if info.get("reverted"):
        return f"已回退到运行前 · {run.get('base_version_label') or ''}".rstrip(" ·")
    if state == "running":
        return "进行中"
    if state == "queued":
        return "未开始" if run.get("state") in store.ENDED else "排队"
    if state == "skipped":
        if info.get("delete_forbidden"):
            return "要删除已确认的内容"
        return "检查没通过" if info.get("gate_failed") else "未开始"
    text = "部分完成" if state == "partial" else "完成"
    return f"{text} · 挂起 {suspended} 项" if suspended else text


def run_view(run: dict, now: float | None = None) -> dict:
    """AutobuildRun（AB-04、AB-05、AB-06～AB-09 的响应）：`matrix` 一个单元一行；`stages` 只有老运行有。"""
    info = list(store.unit_infos(run).values())
    columns = stages(run)
    stage_ids = [stage["id"] for stage in columns]
    in_run = {unit["id"] for unit in _units(run)}
    rows = []
    for row in run.get("before_rows") or []:
        # 老运行开始时就做完、没在这次运行里的段
        if not stage_ids or row.get("group") != "done" or row["id"] in in_run:
            continue
        done = row.get("stages") or {}
        cells = {stage: ("confirmed" if done.get(stage) in ("confirmed", "auto") else
                         "skipped" if done.get(stage) == "skipped" else "queued")
                 for stage in stage_ids}
        rows.append({"slice_id": row["id"], "slice_name": row["name"], "state": "done", "result": "运行前已完成",
                     "cells": cells})
    rows += [_row(run, unit, stage_ids) for unit in _units(run)]
    return {
        **summary(run, now),
        "conversation_id": run["conversation_id"],
        "policy": {key: (run.get("policy") or {}).get(key) for key in policy.GROUPS},
        "checks": _checks(run, info),
        "scenarios": {"passed": 0, "total": 0},
        "fix_rounds": {"used": sum(int(item.get("fix_rounds") or 0) for item in info),
                       "limit": int(((run.get("policy") or {}).get("advanced") or {}).get("max_fix_rounds") or 0)},
        "matrix": rows,
        "stages": columns,
        "tail_text": "",
        "scope_lock_text": "",
        "recovered_from_checkpoint": int(run.get("recovered") or 0),
        "report_available": bool(run.get("report")),
        "base_version_label": run.get("base_version_label"),
    }


# ---------------------------------------------------------------------------
# 运行报告（AB-16）
# ---------------------------------------------------------------------------


def produced_text(produced: dict) -> str:
    parts = [f"+{produced[key]} {label}" for key, label in KIND_LABELS.items() if produced.get(key)]
    return " ".join(parts) if parts else "—"


def report(run: dict, now: float | None = None) -> dict:
    """AutobuildReport：每个单元的结果、能否撤回随复核与撤回变化，每次现算。"""
    currency = currency_of(run)
    ledger = live_ledger(run)
    threshold = float(((run.get("policy") or {}).get("advanced") or {}).get("confidence_threshold") or 0.8)
    produced = {key: 0 for key in KIND_LABELS}
    rows, partial = [], []
    ended = run.get("state") in store.ENDED
    for unit in _units(run):
        info = _info(run, unit["id"])
        for key in produced:
            produced[key] += int((info.get("produced") or {}).get(key) or 0)
        state = info.get("state") or "queued"
        result = state if state in ("done", "partial") else "skipped"
        if result == "partial":
            partial.append(unit["name"])
        rows.append({
            "slice_id": unit["id"], "name": unit["name"], "result": result, "result_text": result_text(run, unit["id"]),
            "produced_text": produced_text(info.get("produced") or {}), "scenarios": {"passed": 0, "total": 0},
            "version_label": info.get("version_label") or run.get("base_version_label") or "",
            "revertible": bool(ended and info.get("commit") and not info.get("reverted")),
            "reverted": bool(info.get("reverted")),
        })
    infos = [_info(run, unit["id"]) for unit in _units(run)]
    high = sum(1 for entry in ledger if float(entry.get("confidence") or 0) >= threshold)
    return {
        "run_id": run["run_id"], "run_no": run["run_no"], "state": run["state"],
        "started_at": iso(run.get("started_at") or run.get("created_at")), "ended_at": iso(run.get("ended_at")),
        "recovered_restarts": int(run.get("recovered") or 0),
        "base_version_label": run.get("base_version_label"),
        "metrics": {
            "duration_ms": budget.elapsed_ms(run, now),
            "slices": {"done": sum(1 for row in rows if row["result"] != "skipped"), "total": len(rows),
                       "partial": partial},
            "produced": produced,
            "checks": _checks(run, infos),
            "scenarios": {"passed": 0, "total": 0, "suspended_failures": 0},
            "decisions": {"total": len(ledger), "high_confidence": high, "needs_look": len(ledger) - high},
            "suspended": sum(1 for item in live_suspensions(run) if item.get("state") in UNRESOLVED),
            "cost": money((run.get("budget") or {}).get("cost") or 0, currency),
            "tokens": int((run.get("budget") or {}).get("tokens") or 0),
        },
        "slices": rows,
        "note": "",
    }


# ---------------------------------------------------------------------------
# 对话里的卡片（契约 4.7）
# ---------------------------------------------------------------------------


def _card(card_id: str, card_type: str, title: str, open_=None) -> dict:
    return {"card_id": card_id, "card_type": card_type, "eyebrow": "", "title": title, "subtitle": "",
            "status": None, "plugin": None, "open": open_}


def start_card(run: dict) -> dict:
    pol = run.get("policy") or {}
    chips = [
        {"label": "范围", "value": f"{len(_units(run))} 步"},
        {"label": "拿不准时", "value": policy.BELOW_TEXT.get((pol.get("advanced") or {}).get("below_threshold"), "")},
    ]
    return {**_card(f"ab{run['run_no']}-start", "autobuild_start", f"自主构建 · 运行 #{run['run_no']}",
                    {"kind": "page", "target": "proj-settings"}),
            "run_id": run["run_id"], "run_no": run["run_no"], "chips": chips}


def decision_card(run: dict, entry: dict) -> dict:
    return {**_card(f"ab{run['run_no']}-ad-{entry['id']}", "auto_decision", entry.get("question") or "",
                    {"kind": "page", "target": "inbox"}),
            "run_id": run["run_id"], "ledger_id": entry["id"], "at": iso(entry.get("at")),
            "question": entry.get("question"), "chosen": entry.get("chosen"), "anchors": entry.get("anchors") or [],
            "confidence": entry.get("confidence"), "threshold": entry.get("threshold"),
            "assumption": bool(entry.get("assumption")), "review_state": entry.get("review_state"), "recorded": True}


def suspension_card(run: dict, item: dict) -> dict:
    return {**_card(f"ab{run['run_no']}-sp-{item['id']}", "suspension", item.get("title") or "",
                    {"kind": "page", "target": "inbox"}),
            "run_id": run["run_id"], "suspension_id": item["id"], "title": item.get("title") or "",
            "reason": item.get("reason") or "", "inbox_item_id": f"suspension:{item['id']}"}


def _unit_summary(run: dict, unit: dict) -> str:
    info = _info(run, unit["id"])
    state = info.get("state") or "queued"
    if state == "running":
        step = next((item for item in unit["steps"] if (info.get("steps") or {}).get(item["id"]) == "running"), None)
        return f"正在做 · {step['label']}" if step else "正在做"
    decisions = sum(1 for entry in live_ledger(run) if (entry.get("slice") or {}).get("id") == unit["id"])
    parts = [result_text(run, unit["id"])]
    if state in ("done", "partial"):
        parts[0] = "自主完成" if state == "done" and not info.get("reverted") else parts[0]
        text = produced_text(info.get("produced") or {})
        if text != "—":
            parts.append(text)
    if decisions:
        parts.append(f"自动决定 {decisions} 项")
    return " · ".join(parts)


def unit_card(run: dict, unit: dict) -> dict:
    """一个单元的进展块（卡片类型沿用 `slice_progress`）。"""
    info = _info(run, unit["id"])
    entries = {entry["id"]: entry for entry in run.get("ledger") or []}
    suspended = {item["id"]: item for item in run.get("suspensions") or []}
    timeline = []
    for item in info.get("timeline") or []:
        if item.get("type") == "auto_decision" and item.get("id") in entries:
            timeline.append({"type": "auto_decision", "card": decision_card(run, entries[item["id"]])})
        elif item.get("type") == "suspension" and item.get("id") in suspended:
            timeline.append({"type": "suspension", "card": suspension_card(run, suspended[item["id"]])})
        elif item.get("type") == "step":
            timeline.append({"id": item["id"], "text": item.get("text") or "", "meta": item.get("meta") or "",
                             "state": item.get("state") or "done", "duration_ms": item.get("duration_ms")})
    return {**_card(f"ab{run['run_no']}-{unit['id']}", "slice_progress", unit["name"]),
            "run_id": run["run_id"], "slice": {"id": unit["id"], "name": unit["name"]},
            "state": CARD_STATE.get(info.get("state") or "queued", "queued"), "summary": _unit_summary(run, unit),
            "version_label": info.get("version_label"), "timeline": timeline,
            "decision_ids": list(info.get("decisions") or []), "suspension_ids": list(info.get("suspensions") or [])}


def report_card(run: dict) -> dict:
    data = report(run)
    return {**_card(f"ab{run['run_no']}-report", "run_report", f"运行 #{run['run_no']} 报告",
                    {"kind": "page", "target": "inbox", "params": {"filter": "autobuild_review"}}),
            **data, "review_count": sum(1 for entry in live_ledger(run) if entry.get("review_state") == "pending")}
