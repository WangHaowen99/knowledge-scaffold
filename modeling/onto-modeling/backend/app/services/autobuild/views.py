"""运行记录 → 契约形状（7.15 AutobuildRunSummary / AutobuildRun / AutobuildReport / LedgerEntry / Suspension）与
对话里的四种卡片（4.7 autobuild_start、slice_progress、run_report；自动决定、挂起嵌在切片进展块的时间线里）。

界面文字只给功能必需的：状态标签、数字、片名、问题与选择本身。
"""

from __future__ import annotations

import time

from . import budget, policy, store

STEP_CELL = {"done": "auto", "running": "running", "queued": "queued", "skipped": "skipped", "partial": "auto"}
CARD_STATE = {"queued": "queued", "running": "running", "done": "done", "partial": "partial", "skipped": "queued"}
LEDGER_FIELDS = ("id", "run_id", "at", "slice", "question", "chosen", "options", "confidence", "threshold",
                 "assumption", "anchors", "affected", "review_state", "rejudged_to", "reason_text", "summary")
SUSPENSION_FIELDS = ("id", "run_id", "at", "slice", "title", "reason", "handled_as", "kind", "options",
                     "allow_custom", "actions", "state")
KINDS = {"object": "objects", "operation": "operations", "rule": "rules"}
KIND_LABELS = {"objects": "对象", "operations": "操作", "rules": "规则"}


def iso(value) -> str | None:
    from ..session_host.views import iso as _iso

    return _iso(value)


def money(amount, currency: str) -> dict:
    return {"amount": None if amount is None else round(float(amount), 2), "currency": currency}


def currency_of(run: dict) -> str:
    return ((run.get("policy") or {}).get("budget") or {}).get("max_cost", {}).get("currency") or "USD"


def live_ledger(run: dict) -> list[dict]:
    """撤回了的片上的决定不再算。"""
    reverted = {unit_id for unit_id, info in (run.get("slices") or {}).items() if info.get("reverted")}
    return [entry for entry in run.get("ledger") or [] if (entry.get("slice") or {}).get("id") not in reverted]


def live_suspensions(run: dict) -> list[dict]:
    reverted = {unit_id for unit_id, info in (run.get("slices") or {}).items() if info.get("reverted")}
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
    return (run.get("slices") or {}).get(unit_id) or {}


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
    limits = (run.get("policy") or {}).get("budget") or {}
    slices = _units(run, "slice")
    done_slices = sum(1 for unit in slices if _info(run, unit["id"]).get("state") in ("done", "partial"))
    elapsed = budget.elapsed_ms(run, now)
    steps_done, steps_total = _step_counts(run)
    estimate = None
    if run.get("state") not in store.ENDED and steps_done and steps_total:
        estimate = int(elapsed * steps_total / steps_done)
    ledger = live_ledger(run)
    return {
        "run_id": run["run_id"], "run_no": run["run_no"], "state": run["state"], "current": _current(run),
        "elapsed_ms": elapsed, "est_total_ms": estimate,
        "tokens": {"used": int((run.get("budget") or {}).get("tokens") or 0), "limit": int(limits.get("max_tokens") or 0)},
        "cost": {"used": money((run.get("budget") or {}).get("cost") or 0, currency),
                 "budget": money((limits.get("max_cost") or {}).get("amount"), currency)},
        "slices": {"done": done_slices, "total": len(slices)},
        "decisions": len(ledger),
        "suspended": sum(1 for item in live_suspensions(run) if item.get("state") == "open"),
        "needs_review": sum(1 for entry in ledger if entry.get("review_state") == "pending"),
        "started_at": iso(run.get("started_at") or run.get("created_at")), "ended_at": iso(run.get("ended_at")),
    }


def _row(run: dict, unit: dict, stage_ids: list[str]) -> dict:
    info = _info(run, unit["id"])
    steps = {step["id"]: (info.get("steps") or {}).get(step["id"], "queued") for step in unit["steps"]}
    before = unit.get("before") or {}
    merged = bool(info.get("commit"))
    cells = {}
    for stage in stage_ids:
        if stage in steps:
            value = STEP_CELL.get(steps[stage], "queued")
            cells[stage] = "skipped" if value == "auto" and steps[stage] == "partial" and not merged else value
        elif before.get(stage) in ("confirmed", "auto"):
            cells[stage] = "confirmed"
        elif before.get(stage) == "skipped":
            cells[stage] = "skipped"
        elif merged:
            cells[stage] = "auto"
        else:
            cells[stage] = "queued"
    return {"slice_id": unit["id"], "slice_name": unit["name"], "cells": cells, "result": result_text(run, unit["id"])}


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
        return "检查没通过" if info.get("gate_failed") else "未开始"
    text = "部分完成" if state == "partial" else "完成"
    return f"{text} · 挂起 {suspended} 项" if suspended else text


def run_view(run: dict, stage_ids: list[str], now: float | None = None) -> dict:
    """AutobuildRun（AB-04、AB-05、AB-06～AB-09 的响应）。"""
    info = list((run.get("slices") or {}).values())
    tail = [unit["name"] for unit in _units(run, "node")]
    rows = [{"slice_id": row["id"], "slice_name": row["name"], "result": "运行前已完成",
             "cells": {stage: ("confirmed" if (row.get("stages") or {}).get(stage) in ("confirmed", "auto") else
                               "skipped" if (row.get("stages") or {}).get(stage) == "skipped" else "queued")
                       for stage in stage_ids}}
            for row in run.get("before_rows") or [] if row.get("group") == "done"
            and row["id"] not in {unit["id"] for unit in _units(run)}]
    rows += [_row(run, unit, stage_ids) for unit in _units(run, "slice")]
    return {
        **summary(run, now),
        "conversation_id": run["conversation_id"],
        "policy": {key: (run.get("policy") or {}).get(key) for key in ("scope", "budget", "production_collection",
                                                                        "advanced")},
        "checks": {"errors": sum(int((item.get("checks") or {}).get("errors") or 0) for item in info),
                   "warnings": sum(int((item.get("checks") or {}).get("warnings") or 0) for item in info)},
        "scenarios": {"passed": 0, "total": 0},
        "fix_rounds": {"used": sum(int(item.get("fix_rounds") or 0) for item in info),
                       "limit": int(((run.get("policy") or {}).get("advanced") or {}).get("max_fix_rounds") or 0)},
        "matrix": rows,
        "tail_text": f"跑完全部切片后：{' → '.join(tail)}" if tail else "",
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
    """AutobuildReport：按片结果、能否撤回随复核与撤回变化，每次现算。"""
    currency = currency_of(run)
    limits = (run.get("policy") or {}).get("budget") or {}
    ledger = live_ledger(run)
    threshold = float(((run.get("policy") or {}).get("advanced") or {}).get("confidence_threshold") or 0.8)
    produced = {key: 0 for key in KIND_LABELS}
    slices, partial = [], []
    ended = run.get("state") in store.ENDED
    for unit in _units(run, "slice"):
        info = _info(run, unit["id"])
        for key in produced:
            produced[key] += int((info.get("produced") or {}).get(key) or 0)
        state = info.get("state") or "queued"
        result = state if state in ("done", "partial") else "skipped"
        if result == "partial":
            partial.append(unit["name"])
        slices.append({
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
            "budget_duration_ms": int(float(limits.get("max_duration_min") or 0) * 60_000),
            "slices": {"done": sum(1 for row in slices if row["result"] != "skipped"), "total": len(slices),
                       "partial": partial},
            "produced": produced,
            "checks": {"errors": sum(int((info.get("checks") or {}).get("errors") or 0) for info in infos),
                       "warnings": sum(int((info.get("checks") or {}).get("warnings") or 0) for info in infos)},
            "scenarios": {"passed": 0, "total": 0, "suspended_failures": 0},
            "decisions": {"total": len(ledger), "high_confidence": high, "needs_look": len(ledger) - high},
            "suspended": sum(1 for item in live_suspensions(run) if item.get("state") == "open"),
            "cost": money((run.get("budget") or {}).get("cost") or 0, currency),
            "tokens": int((run.get("budget") or {}).get("tokens") or 0),
            "budget": money((limits.get("max_cost") or {}).get("amount"), currency),
        },
        "slices": slices,
        "note": "",
    }


# ---------------------------------------------------------------------------
# 对话里的卡片（契约 4.7）
# ---------------------------------------------------------------------------


def _card(card_id: str, card_type: str, title: str, open_=None) -> dict:
    return {"card_id": card_id, "card_type": card_type, "eyebrow": "", "title": title, "subtitle": "",
            "status": None, "plugin": None, "open": open_}


def _money_text(amount, currency: str) -> str:
    return f"{'$' if currency == 'USD' else '¥'}{amount:g}"


def _duration_text(minutes: int) -> str:
    return f"{minutes // 60} 小时" if minutes % 60 == 0 and minutes >= 60 else f"{minutes} 分钟"


def start_card(run: dict) -> dict:
    pol = run.get("policy") or {}
    scope, limits = pol.get("scope") or {}, pol.get("budget") or {}
    slices = len(_units(run, "slice"))
    cost = (limits.get("max_cost") or {}).get("amount") or 0
    chips = [
        {"label": "范围", "value": f"{'选中的' if scope.get('mode') == 'selected' else '未完成的'} {slices} 片"},
        {"label": "预算", "value": f"{_money_text(cost, currency_of(run))} · "
                                  f"{_duration_text(int(limits.get('max_duration_min') or 0))}"},
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


def _slice_summary(run: dict, unit: dict) -> str:
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


def slice_card(run: dict, unit: dict) -> dict:
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
            "state": CARD_STATE.get(info.get("state") or "queued", "queued"), "summary": _slice_summary(run, unit),
            "version_label": info.get("version_label"), "timeline": timeline,
            "decision_ids": list(info.get("decisions") or []), "suspension_ids": list(info.get("suspensions") or [])}


def report_card(run: dict) -> dict:
    data = report(run)
    return {**_card(f"ab{run['run_no']}-report", "run_report", f"运行 #{run['run_no']} 报告",
                    {"kind": "page", "target": "inbox", "params": {"filter": "autobuild_review"}}),
            **data, "review_count": sum(1 for entry in live_ledger(run) if entry.get("review_state") == "pending")}
