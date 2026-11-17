"""子任务步骤（契约 S-21）：从对话历史里的 subagent_* 记录还原一个子任务的名称、任务说明、状态与逐步明细。

时间线上的子任务卡只带当前一步（EC-22），面板打开时按这里分页取全部步骤。步骤数在写入时已封顶（每个子任务
200 步，engines/jiuwen_progress 与 timeline.SUBAGENT_STEPS_MAX），一步不超过约 500 字节。
"""

from __future__ import annotations

from .. import subagent_result
from .timeline import _SUBAGENT_STATUS, _sub_step, subagent_title
from .views import iso

_KINDS = ("subagent_started", "subagent_activity", "subagent_finished")


def steps_of(history: list[dict], task_id: str) -> tuple[dict, list[dict]] | None:
    """(子任务, 按出现顺序的步骤)；历史里没有这个子任务时返回 None。「思考中」是瞬时事件，不在历史里。"""
    records = [record for record in history
               if record.get("type") in _KINDS and str(record.get("task_id") or "") == task_id]
    if not records:
        return None
    steps: dict[str, dict] = {}
    opened: dict[str, float] = {}
    for record in records:
        if record["type"] != "subagent_activity" or record.get("kind") == "thinking":
            continue
        step = _sub_step(record)
        if step is None:
            continue
        ts = record.get("ts")
        current = steps.get(step["id"])
        if current is None:
            current = steps[step["id"]] = {**step, "started_at": iso(ts), "ended_at": None, "duration_ms": None}
            if isinstance(ts, (int, float)):
                opened[step["id"]] = ts
        else:
            current.update(state=step.get("state"), text=step.get("text") or current.get("text"))
        tool_finished = step.get("state") != "running" and step.get("kind") == "tool"
        if tool_finished and step["id"] in opened and current.get("ended_at") is None:
            current["ended_at"] = iso(ts)
            current["duration_ms"] = max(0, int((ts - opened[step["id"]]) * 1000))
    return _task(task_id, records, steps), list(steps.values())


def _task(task_id: str, records: list[dict], steps: dict[str, dict]) -> dict:
    started = next((record for record in records if record["type"] == "subagent_started"), records[0])
    finished = next((record for record in reversed(records) if record["type"] == "subagent_finished"), None)
    titled = next((record for record in reversed(records) if record.get("title")), None)
    if finished is not None:
        status = _SUBAGENT_STATUS.get(str(finished.get("status") or ""), "failed")
    else:
        status = "running" if any(record["type"] == "subagent_activity" for record in records) else "started"
    return {
        "task_id": task_id,
        "title": str(titled["title"]) if titled else subagent_title(started.get("description"), started.get("brief"),
                                                                    started.get("agent_type")),
        "brief": started.get("brief") or None,
        "status": status,
        "started_at": iso(started.get("ts")),
        "ended_at": iso(finished.get("ts")) if finished else None,
        "step_count": sum(1 for step in steps.values() if step["kind"] == "tool"),
        "summary": subagent_result.display((finished or {}).get("summary")),
    }
