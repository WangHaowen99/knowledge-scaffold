"""自主构建的平台限制（契约 SY-17、详设 8.5 / 4.10.1）：同时运行数上限，管理员在系统设置里改，立即生效。

- 存数据目录 `governance/autobuild-limits.json`；同时运行数也是配置项 `ONTO_WEB_AUTOBUILD_MAX_RUNS`
  （配置注册表，设置页保存的值优先于环境变量）；
- 单次运行的时长、token、费用上限在 F6 去掉了（运行不按用量判停，花费的硬上限是配额）：老文件里的这几项不再读，
  请求里带了也忽略；
- `night_priority` 保存、回显，本期不生效（夜间优先没有调度器）；
- `allow_production_collection` 随证据采集删除（ADR-0100）：老文件里的不再读，请求里带了也忽略，下次保存时清掉。
"""

from __future__ import annotations

import re
from pathlib import Path

from ... import config
from . import store

MAX_RUNS_KEY = "ONTO_WEB_AUTOBUILD_MAX_RUNS"
MAX_RUNS_MIN, MAX_RUNS_CAP = 1, 16
NOT_ENFORCED = ("night_priority",)
# 去掉的项（F6 的单次上限、证据采集的生产库授权）：老文件里有，下次保存时清掉
RETIRED = ("max_duration_min", "max_tokens", "max_cost", "allow_production_collection")
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


def current() -> dict:
    """SY-17 对象；`running` 是现在占着名额的运行数。"""
    data = stored()
    night = data.get("night_priority") if isinstance(data.get("night_priority"), dict) else {}
    return {
        "max_concurrent_runs": max_runs(),
        "night_priority": {"enabled": bool(night.get("enabled", False)),
                           "start": night.get("start") if _TIME.match(str(night.get("start") or "")) else "20:00",
                           "end": night.get("end") if _TIME.match(str(night.get("end") or "")) else "08:00"},
        "running": len(store.all_runs(store.RUNNING)),
        "not_enforced": list(NOT_ENFORCED),
    }


def _int(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _valid_night(night) -> bool:
    """夜间优先：enabled 是布尔，start、end 是 HH:MM。"""
    if not isinstance(night, dict) or not isinstance(night.get("enabled"), bool):
        return False
    return bool(_TIME.match(str(night.get("start") or ""))) and bool(_TIME.match(str(night.get("end") or "")))


def save(body: dict) -> dict:
    """SY-17 PUT：给了哪项改哪项；不合法抛 LimitsError。"""
    data = {key: value for key, value in stored().items() if key not in RETIRED}
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
    if "night_priority" in body:
        night = body["night_priority"]
        if _valid_night(night):
            data["night_priority"] = {"enabled": night["enabled"], "start": night["start"], "end": night["end"]}
        else:
            fields["night_priority"] = "invalid"
    if fields:
        raise LimitsError("自主构建限制不合法", fields)
    store.write_json(_file(), data)
    return current()
