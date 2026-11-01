"""运维管控状态（详设 4.10.3、方案 E8、契约 1.13、O-16、O-18～O-24、O-29）。

- 会话池配置：运维面可在线改，覆盖环境变量默认值，存数据目录，启动时套用到 config 上；
- 维护模式：拒绝新会话 / 只读 → 排空 → 升级 → 恢复，状态存数据目录（重启后仍生效）；
- 排空：拒绝新会话，在线会话跑完当前这一轮后回收，超时的强制断开并标中断；
- 熔断：按（包, 版本）记 closed / open / half_open，插件运行时据此跳过节点（B3a 接入）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from .. import config

_log = logging.getLogger(__name__)
_lock = threading.Lock()

POOL_LIMITS = {"max_sessions": (1, 64), "idle_ttl_s": (60, 86400)}
OVERFLOW_POLICIES = ("evict_idle_then_queue", "queue_only")
MAINTENANCE_MODES = ("reject_new_sessions", "read_only")
STEPS = (
    ("restrict", "限制写入与新会话"),
    ("drain", "排空在线会话"),
    ("upgrade", "升级"),
    ("resume", "恢复"),
)
DEFAULT_DRAIN_WAIT_S = 600


def _dir() -> Path:
    return Path(config.DATA_DIR) / "ops"


def _load(name: str) -> dict:
    try:
        data = json.loads((_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(name: str, data: dict | None) -> None:
    path = _dir() / name
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 会话池配置（O-16）
# ---------------------------------------------------------------------------


def pool_config() -> dict:
    """运维保存的覆盖值（可能为空字典）。"""
    return _load("pool.json")


def effective_pool() -> dict:
    return {
        "max_sessions": config.MAX_ACTIVE_SESSIONS,
        "idle_ttl_s": config.SESSION_IDLE_TTL_SECONDS,
        "overflow_policy": config.SESSION_OVERFLOW_POLICY,
    }


def save_pool_config(max_sessions: int, idle_ttl_s: int, overflow_policy: str) -> dict:
    errors = {}
    for name, value in (("max_sessions", max_sessions), ("idle_ttl_s", idle_ttl_s)):
        low, high = POOL_LIMITS[name]
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            errors[name] = f"整数 {low}–{high}"
    if overflow_policy not in OVERFLOW_POLICIES:
        errors["overflow_policy"] = "enum"
    if errors:
        raise ValueError(errors)
    data = {"max_sessions": max_sessions, "idle_ttl_s": idle_ttl_s, "overflow_policy": overflow_policy}
    with _lock:
        _save("pool.json", data)
        apply_pool_config()
    return effective_pool()


def apply_pool_config() -> None:
    """把运维的覆盖值套到 config 上（会话池按属性现读，下一个会话起生效）。"""
    stored = pool_config()
    if isinstance(stored.get("max_sessions"), int):
        config.MAX_ACTIVE_SESSIONS = stored["max_sessions"]
    if isinstance(stored.get("idle_ttl_s"), int):
        config.SESSION_IDLE_TTL_SECONDS = stored["idle_ttl_s"]
    if stored.get("overflow_policy") in OVERFLOW_POLICIES:
        config.SESSION_OVERFLOW_POLICY = stored["overflow_policy"]


# ---------------------------------------------------------------------------
# 排空（O-19）与强制断开（O-18、O-23）
# ---------------------------------------------------------------------------

_drain: dict = {"active": False}
_drain_task: asyncio.Task | None = None


def draining() -> bool:
    return bool(_drain.get("active"))


def _sessions():
    from ..services.agent_sessions import manager

    return manager


def _is_running(session) -> bool:
    from ..services.agent_sessions import _busy

    return _busy(session) or bool(session.pending_prompts)


def online_counts() -> dict:
    manager = _sessions()
    sessions = list(manager._sessions.values())
    running = sum(1 for session in sessions if _is_running(session))
    return {"online": len(sessions), "running": running, "autobuild_running": 0}


async def force_disconnect(project_id: str, reason: str) -> bool:
    """强制断开：打断正在进行的一轮（历史标「上一轮未正常结束」），再回收会话。"""
    from ..services import error_log
    from ..services.agent_sessions import _mark_interrupted

    manager = _sessions()
    session = manager.get(project_id)
    if session is None:
        return False
    if _is_running(session):
        _mark_interrupted(session, "ops_disconnect", f"运维强制断开：{reason}"[:200])
        try:
            await manager.interrupt(project_id, session.conversation_id)
        except Exception:
            _log.exception("强制断开时打断失败：%s", project_id)
    error_log.log(project_id, "ops_disconnect", f"运维强制断开会话：{reason}"[:400])
    session.pending_prompts.clear()
    try:
        await manager.recycle_project(project_id)
    except Exception:
        _log.exception("强制断开时回收失败：%s", project_id)
    return True


async def _drain_loop(deadline: float) -> None:
    manager = _sessions()
    while True:
        remaining = [pid for pid, session in list(manager._sessions.items())]
        if not remaining:
            break
        for project_id in remaining:
            session = manager.get(project_id)
            if session is not None and not _is_running(session):
                try:
                    await manager.recycle_project(project_id)
                except Exception:
                    _log.exception("排空时回收失败：%s", project_id)
        if time.time() >= deadline:
            for project_id in [pid for pid in list(manager._sessions)]:
                await force_disconnect(project_id, "排空超时")
            break
        await asyncio.sleep(1)
    _drain["finished_at"] = time.time()
    if not maintenance_active():
        _drain["active"] = False


def start_drain(max_wait_s: int = DEFAULT_DRAIN_WAIT_S) -> dict:
    """开始排空（排空期间拒绝新会话）；返回当时的在线数与预计耗时。"""
    global _drain_task
    counts = online_counts()
    deadline = time.time() + max(1, int(max_wait_s))
    _drain.update(active=True, started_at=time.time(), deadline=deadline, finished_at=None)
    loop = asyncio.get_running_loop()
    if _drain_task is None or _drain_task.done():
        _drain_task = loop.create_task(_drain_loop(deadline))
    eta = 0 if not counts["online"] else int((deadline - time.time()) * 1000) if counts["running"] else 1000
    return {**counts, "eta_ms": eta}


def stop_drain() -> None:
    global _drain_task
    if _drain_task is not None and not _drain_task.done():
        _drain_task.cancel()
    _drain_task = None
    _drain["active"] = False


# ---------------------------------------------------------------------------
# 维护模式（1.13、O-20～O-24）
# ---------------------------------------------------------------------------


class MaintenanceError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def _maintenance() -> dict:
    return _load("maintenance.json")


def maintenance_active() -> bool:
    return bool(_maintenance().get("active"))


def maintenance_mode() -> str | None:
    state = _maintenance()
    return state.get("mode") if state.get("active") else None


def maintenance_view() -> dict | None:
    """MaintenanceState（契约 7.16）；没在维护时 None。时间为 epoch 秒，接口层转 RFC 3339。"""
    state = _maintenance()
    if not state.get("active"):
        return None
    current = state.get("step", "restrict")
    order = [step for step, _label in STEPS]
    index = order.index(current) if current in order else 0
    counts = online_counts() if current == "drain" else None
    steps = []
    for position, (step, label) in enumerate(STEPS):
        status = "done" if position < index else "current" if position == index else "pending"
        detail = ""
        progress = None
        if step == "restrict":
            detail = "只读：拒绝一切写请求" if state.get("mode") == "read_only" else "拒绝新会话"
        elif step == "drain" and counts is not None:
            detail = f"在线 {counts['online']} 个会话，运行中 {counts['running']} 个"
            total = state.get("drain_total") or counts["online"] or 1
            progress = int(100 * (1 - counts["online"] / total)) if total else 100
        elif step == "upgrade" and state.get("target"):
            target = state["target"]
            detail = f"{target.get('pack')} → {target.get('version')}"
        steps.append({"id": step, "label": label, "state": status, "detail": detail, "progress_pct": progress})
    return {
        "active": True,
        "mode": state.get("mode"),
        "message": state.get("message", ""),
        "started_at": state.get("started_at"),
        "started_by": state.get("started_by", ""),
        "until": state.get("until"),
        "target": state.get("target"),
        "steps": steps,
    }


def _notify_maintenance() -> None:
    """维护状态变了：推给已打开的项目流与用户流（契约 EP-24、EU-04）。"""
    try:
        from ..services import project_events

        project_events.maintenance_changed()
    except Exception:  # noqa: BLE001 - 推送失败不影响维护操作本身
        _log.exception("推送维护状态失败")


def enter_maintenance(mode: str, message: str, by: str, target: dict | None = None) -> dict:
    if mode not in MAINTENANCE_MODES:
        raise MaintenanceError("invalid_argument", "维护模式只能是 reject_new_sessions 或 read_only", 400,
                               {"fields": {"mode": "enum"}})
    with _lock:
        if maintenance_active():
            raise MaintenanceError("maintenance_active", "已在维护模式")
        _save("maintenance.json", {
            "active": True, "mode": mode, "message": (message or "")[:500], "started_at": time.time(),
            "started_by": by, "target": target, "step": "restrict",
        })
    _notify_maintenance()
    return maintenance_view()


def advance_maintenance(by: str) -> dict:
    with _lock:
        state = _maintenance()
        if not state.get("active"):
            raise MaintenanceError("maintenance_not_active", "不在维护模式")
        step = state.get("step", "restrict")
        if step == "restrict":
            state["step"] = "drain"
            state["drain_total"] = online_counts()["online"]
            _save("maintenance.json", state)
            start_drain(DEFAULT_DRAIN_WAIT_S)
        elif step == "drain":
            counts = online_counts()
            if counts["online"]:
                raise MaintenanceError("maintenance_step_blocked", "还有在线会话没排空", 409,
                                       {"step": "drain", "remaining": counts["online"]})
            state["step"] = "upgrade"
            _save("maintenance.json", state)
        elif step == "upgrade":
            state["step"] = "resume"
            _save("maintenance.json", state)
        else:
            raise MaintenanceError("maintenance_step_blocked", "已到最后一步，请退出维护模式", 409,
                                   {"step": step, "remaining": 0})
    _notify_maintenance()
    return maintenance_view()


async def force_disconnect_remaining(reason: str) -> dict:
    if not maintenance_active():
        raise MaintenanceError("maintenance_not_active", "不在维护模式")
    for project_id in list(_sessions()._sessions):
        await force_disconnect(project_id, reason or "维护：强制断开剩余会话")
    return maintenance_view()


def exit_maintenance(by: str) -> None:
    with _lock:
        if not maintenance_active():
            raise MaintenanceError("maintenance_not_active", "不在维护模式")
        _save("maintenance.json", None)
    stop_drain()
    _notify_maintenance()


def rejects_new_sessions() -> bool:
    return maintenance_active() or draining()


# ---------------------------------------------------------------------------
# 熔断（O-29）
# ---------------------------------------------------------------------------

BREAKER_STATES = ("closed", "half_open", "open")


def breakers() -> dict:
    return _load("breakers.json")


def breaker_state(pack: str, version: str) -> str:
    return (breakers().get(f"{pack}@{version}") or {}).get("state", "closed")


def set_breaker(pack: str, version: str, action: str, by: str, reason: str = "") -> str:
    """open：熔断（节点暂时跳过）；close：先进入半开试探，半开时再 close 才恢复。"""
    key = f"{pack}@{version}"
    with _lock:
        data = breakers()
        current = (data.get(key) or {}).get("state", "closed")
        if action == "open":
            if current == "open":
                raise MaintenanceError("breaker_state_invalid", "已经熔断", 409, {"state": current})
            new = "open"
        elif action == "close":
            if current == "closed":
                raise MaintenanceError("breaker_state_invalid", "没有熔断", 409, {"state": current})
            new = "half_open" if current == "open" else "closed"
        else:
            raise MaintenanceError("invalid_argument", "action 只能是 open 或 close", 400, {"fields": {"action": "enum"}})
        data[key] = {"state": new, "at": time.time(), "by": by, "reason": (reason or "")[:300]}
        _save("breakers.json", data)
    return new
