"""自主构建的平台限制（契约 SY-17、详设 8.5 / 4.10.1）：同时运行数上限与单次预算上限，管理员在系统设置里改，立即生效。

- 存数据目录 `governance/autobuild-limits.json`；同时运行数也是配置项 `ONTO_WEB_AUTOBUILD_MAX_RUNS`
  （配置注册表，设置页保存的值优先于环境变量）；
- 金额的币种跟模型单价表走（SY-18），这里只存数额；
- `allow_production_collection`、`night_priority` 保存、回显，本期不生效（证据源只留接口；夜间优先没有调度器）。
"""

from __future__ import annotations

import re
from pathlib import Path

from ... import config
from .. import model_pricing
from . import store

MAX_RUNS_KEY = "ONTO_WEB_AUTOBUILD_MAX_RUNS"
MAX_RUNS_MIN, MAX_RUNS_CAP = 1, 16
DEFAULTS = {"max_duration_min": 240, "max_tokens": 10_000_000, "max_cost": 100.0}
NOT_ENFORCED = ("allow_production_collection", "night_priority")
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class LimitsError(ValueError):
    def __init__(self, message: str, fields: dict, status: int = 400):
        super().__init__(message)
        self.fields = fields
        self.status = status


def _file() -> Path:
    return Path(config.DATA_DIR) / "governance" / "autobuild-limits.json"


def stored() -> dict:
    return store.read_json(_file()) or {}


def max_runs() -> int:
    """同时运行数上限 1–16（配置注册表：设置页保存的 > 环境变量 > 默认 2）。0 不合法：环境变量里的 0 启动时报配置
    错误并回落，设置页不让存；万一读到也按 1。"""
    from ...governance import config_registry

    try:
        value = int(config_registry.value(MAX_RUNS_KEY))
    except (TypeError, ValueError):
        value = 2
    return max(MAX_RUNS_MIN, min(MAX_RUNS_CAP, value))


def _number(value, default):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else default


def current() -> dict:
    """SY-17 对象；`running` 是现在占着名额的运行数。"""
    data = stored()
    currency = model_pricing.load()["currency"]
    night = data.get("night_priority") if isinstance(data.get("night_priority"), dict) else {}
    return {
        "max_concurrent_runs": max_runs(),
        "max_duration_min": int(_number(data.get("max_duration_min"), DEFAULTS["max_duration_min"])),
        "max_tokens": int(_number(data.get("max_tokens"), DEFAULTS["max_tokens"])),
        "max_cost": {"amount": float(_number(data.get("max_cost"), DEFAULTS["max_cost"])), "currency": currency},
        "allow_production_collection": bool(data.get("allow_production_collection", False)),
        "night_priority": {"enabled": bool(night.get("enabled", False)),
                           "start": night.get("start") if _TIME.match(str(night.get("start") or "")) else "20:00",
                           "end": night.get("end") if _TIME.match(str(night.get("end") or "")) else "08:00"},
        "running": len(store.all_runs(store.RUNNING)),
        "not_enforced": list(NOT_ENFORCED),
    }


def _int(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def save(body: dict) -> dict:
    """SY-17 PUT：给了哪项改哪项；不合法抛 LimitsError。"""
    data = stored()
    fields: dict[str, str] = {}
    if "max_concurrent_runs" in body:
        value = body["max_concurrent_runs"]
        if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
            # 主控定（2026-11-02）：不允许 0（「关掉自主构建」不靠它），收到 0 或负数回 422
            raise LimitsError("同时运行上限至少为 1", {"max_concurrent_runs": "min"}, 422)
        if _int(value, MAX_RUNS_MIN, MAX_RUNS_CAP):
            data["max_concurrent_runs"] = value
        else:
            fields["max_concurrent_runs"] = "range"
    for key, high in (("max_duration_min", 24 * 60), ("max_tokens", 10**10)):
        if key in body:
            if _int(body[key], 1, high):
                data[key] = body[key]
            else:
                fields[key] = "range"
    if "max_cost" in body:
        amount = (body["max_cost"] or {}).get("amount") if isinstance(body["max_cost"], dict) else None
        if _number(amount, None) is None:
            fields["max_cost"] = "invalid"
        else:
            data["max_cost"] = float(amount)
    if "allow_production_collection" in body:
        if isinstance(body["allow_production_collection"], bool):
            data["allow_production_collection"] = body["allow_production_collection"]
        else:
            fields["allow_production_collection"] = "bool"
    if "night_priority" in body:
        night = body["night_priority"]
        if (isinstance(night, dict) and isinstance(night.get("enabled"), bool)
                and _TIME.match(str(night.get("start") or "")) and _TIME.match(str(night.get("end") or ""))):
            data["night_priority"] = {"enabled": night["enabled"], "start": night["start"], "end": night["end"]}
        else:
            fields["night_priority"] = "invalid"
    if fields:
        raise LimitsError("自主构建限制不合法", fields)
    store.write_json(_file(), data)
    return current()


def platform_limits() -> dict:
    """AB-01 的 platform_limits。"""
    limits = current()
    return {key: limits[key] for key in ("max_duration_min", "max_tokens", "max_cost", "allow_production_collection")}
