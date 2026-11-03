"""自主构建的预算（方案 B14、详设 8.5「预算」）：token、费用、时长三个维度。

- token：运行对话里的用量事件（`conversation_store.counted_usage`，与配额台账同一口径：输入 + 缓存读 + 输出），
  只读运行开始之后新增的部分，按事件序号增量累加，不每次全量扫历史；
- 费用：token × 模型单价表（`model_pricing`）；没配单价时用引擎报的费用，Jiuwen 不报费用、只能标估算；
- 时长：墙钟，只算跑着的时间（暂停、排队不算）；
- 用到 80% 提醒一次（EP-16），用到 100% 停在最近的安全点。
"""

from __future__ import annotations

import time

from .. import conversation_store, model_pricing

WARN_PCT = 80
DIMENSIONS = ("duration", "tokens", "cost")


def empty() -> dict:
    return {"tokens": 0, "cost": 0.0, "estimated": False, "seq": 0, "active_ms": 0, "resumed_at": None,
            "warned": [], "exhausted": None}


def _engine(project_id: str) -> str:
    from ..agent_sessions import manager

    try:
        return manager._engine_class_for(project_id).name
    except Exception:  # noqa: BLE001
        return "claude"


def add_usage(project_id: str, run: dict, events: list[dict]) -> None:
    """把运行对话里第 seq 条之后的用量事件记进 run["budget"]（原地改）。"""
    budget = run.setdefault("budget", empty())
    start = int(budget.get("seq") or 0)
    if start >= len(events):
        return
    engine = _engine(project_id)
    model = model_pricing.current_model(engine, run.get("owner") or "")
    table = model_pricing.load()
    for event in events[start:]:
        usage = conversation_store.counted_usage(event)
        if usage is None:
            continue
        budget["tokens"] = int(budget.get("tokens") or 0) + model_pricing.tokens_of(usage)
        reported = event.get("total_cost_usd") if event.get("type") == "result" else None
        amount, estimated = model_pricing.cost(usage, engine=engine, model=model, reported=reported, table=table)
        if not usage and reported is None:
            continue  # 逐次记账引擎的 result 不再计费
        budget["cost"] = round(float(budget.get("cost") or 0) + amount, 6)
        budget["estimated"] = bool(budget.get("estimated")) or estimated
    budget["seq"] = len(events)


def elapsed_ms(run: dict, now: float | None = None) -> int:
    budget = run.get("budget") or {}
    total = int(budget.get("active_ms") or 0)
    if budget.get("resumed_at"):
        total += int(((now or time.time()) - float(budget["resumed_at"])) * 1000)
    return max(0, total)


def start_clock(run: dict, now: float | None = None) -> None:
    budget = run.setdefault("budget", empty())
    if not budget.get("resumed_at"):
        budget["resumed_at"] = now or time.time()


def stop_clock(run: dict, now: float | None = None) -> None:
    budget = run.setdefault("budget", empty())
    budget["active_ms"] = elapsed_ms(run, now)
    budget["resumed_at"] = None


def used_pct(run: dict, now: float | None = None) -> dict[str, float]:
    limits = (run.get("policy") or {}).get("budget") or {}
    budget = run.get("budget") or {}
    pairs = {
        "duration": (elapsed_ms(run, now), float(limits.get("max_duration_min") or 0) * 60_000),
        "tokens": (float(budget.get("tokens") or 0), float(limits.get("max_tokens") or 0)),
        "cost": (float(budget.get("cost") or 0), float(((limits.get("max_cost") or {}).get("amount")) or 0)),
    }
    return {key: (used * 100 / cap if cap else 0.0) for key, (used, cap) in pairs.items()}


def check(run: dict, now: float | None = None) -> tuple[list[tuple[str, float]], str | None]:
    """返回 (新到 80% 的维度与百分比, 用完的维度)；原地记下已提醒的维度与用完的维度。"""
    budget = run.setdefault("budget", empty())
    warned = list(budget.get("warned") or [])
    fresh = []
    exhausted = budget.get("exhausted")
    for dimension, pct in used_pct(run, now).items():
        if pct >= WARN_PCT and dimension not in warned:
            warned.append(dimension)
            fresh.append((dimension, round(pct, 1)))
        if pct >= 100 and not exhausted:
            exhausted = dimension
    budget["warned"] = warned
    budget["exhausted"] = exhausted
    return fresh, exhausted
