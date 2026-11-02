"""自主构建策略（详设 8.5 策略表；契约 AB-01、AB-02、7.15 AutobuildPolicy）。

范围、预算、决定策略、资料优先级、生产库授权按项目存在数据目录；硬性不做的几项（发布、删除已确认内容、改资料区、
采集未授权的生产库）由守卫与写入路径保证，不在策略里。

- 预算只能设在平台上限（SY-17）与剩余配额（4.10.1，用户当日与项目当月 token 剩余的较小者）之内；
- 生产库授权保留字段、原样存取，本期不生效（证据源只留接口，ADR-0056）：`not_enforced` 里标出来。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging

from ...api.errors import ApiError
from . import limits, store

_log = logging.getLogger(__name__)

BELOW_THRESHOLD = ("conservative_assumption", "suspend")
BELOW_TEXT = {"conservative_assumption": "采用保守选项并记为假设", "suspend": "挂起这一项，跳到下一步"}
SCOPE_MODES = ("all_pending", "selected")
NOT_ENFORCED = ("production_collection",)
DEFAULT_BUDGET = {"max_duration_min": 120, "max_tokens": 3_000_000, "max_cost": 30.0}
LIST_MAX = 20
TEXT_MAX = 60
# 做到哪一步：这些节点不能作为终点（发布是硬性不做；资料分析在建模之前）
NOT_UNTIL = ("core.publish", "core.research", "core.update")


def _path(project_id: str):
    return store.project_dir(project_id) / "policy.json"


def revision(policy: dict) -> str:
    raw = json.dumps({key: policy.get(key) for key in ("scope", "budget", "production_collection", "advanced")},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _quota_tokens(project_id: str, owner: str) -> int | None:
    """剩余配额（token）；不限或配额自身出错时 None。"""
    from ...governance import quota

    try:
        return quota.service().remaining(project_id, owner)["tokens"]
    except Exception:  # noqa: BLE001 - 配额自身出错放行（ADR-0043）
        _log.warning("取剩余配额失败，按不限处理（项目 %s）", project_id, exc_info=True)
        return None


def caps(project_id: str, owner: str) -> dict:
    """预算的上限：平台限制与剩余配额。"""
    platform = limits.platform_limits()
    remaining = _quota_tokens(project_id, owner)
    tokens = platform["max_tokens"] if remaining is None else min(platform["max_tokens"], remaining)
    return {"max_duration_min": platform["max_duration_min"], "max_tokens": tokens,
            "max_cost": platform["max_cost"]["amount"], "currency": platform["max_cost"]["currency"],
            "quota_tokens": remaining}


def defaults(project_id: str, owner: str) -> dict:
    limit = caps(project_id, owner)
    return {
        "scope": {"mode": "all_pending", "slice_ids": [], "refresh_stale_first": True, "generate_scenarios": True,
                  "include_optional": False, "until_node": None},
        "budget": {"max_duration_min": min(DEFAULT_BUDGET["max_duration_min"], limit["max_duration_min"]),
                   "max_tokens": max(1, min(DEFAULT_BUDGET["max_tokens"], limit["max_tokens"])),
                   "max_cost": {"amount": min(DEFAULT_BUDGET["max_cost"], limit["max_cost"]),
                                "currency": limit["currency"]}},
        "production_collection": [],
        "advanced": {"confidence_threshold": 0.8, "below_threshold": "conservative_assumption",
                     "must_suspend_kinds": [], "source_priority": [], "max_fix_rounds": 2},
    }


def load(project_id: str, owner: str) -> dict:
    """本项目的策略（没存过时是默认值）；金额的币种跟单价表走。"""
    stored = store.read_json(_path(project_id))
    policy = stored if stored and isinstance(stored.get("budget"), dict) else defaults(project_id, owner)
    policy = {key: copy.deepcopy(policy.get(key)) for key in ("scope", "budget", "production_collection", "advanced")}
    policy["budget"]["max_cost"]["currency"] = limits.platform_limits()["max_cost"]["currency"]
    return policy


def _last_run(project_id: str) -> dict | None:
    from ...api.deps import iso

    for record in store.runs(project_id):
        if record.get("state") in store.ENDED:
            pending = any(entry.get("review_state") == "pending" for entry in record.get("ledger") or [])
            return {"run_id": record["run_id"], "run_no": record["run_no"], "ended_at": iso(record.get("ended_at")),
                    "duration_ms": int((record.get("budget") or {}).get("active_ms") or 0), "reviewed": not pending}
    return None


def view(project_id: str, owner: str, policy: dict | None = None) -> dict:
    """AB-01。"""
    policy = policy or load(project_id, owner)
    limit = caps(project_id, owner)
    return {
        **policy,
        "platform_limits": limits.platform_limits(),
        "quota_remaining": {"tokens": limit["quota_tokens"], "cost": {"amount": None, "currency": limit["currency"]}},
        "last_run": _last_run(project_id),
        "revision": revision(policy),
        "not_enforced": list(NOT_ENFORCED),
    }


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _invalid(fields: dict, message: str = "自主构建策略不合法") -> ApiError:
    return ApiError("invalid_argument", message, 400, {"fields": fields})


def _strings(value, field: str, fields: dict) -> list[str]:
    if not isinstance(value, list) or len(value) > LIST_MAX or not all(
            isinstance(item, str) and 0 < len(item.strip()) <= TEXT_MAX for item in value):
        fields[field] = "list"
        return []
    return [item.strip() for item in value]


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def merge(current: dict, body: dict | None) -> dict:
    """AB-02 的请求体、AB-03 的 policy_overrides 并进当前策略：给了哪一组改哪一组，组内给了哪项改哪项。"""
    merged = copy.deepcopy(current)
    for group in ("scope", "budget", "advanced"):
        part = (body or {}).get(group)
        if isinstance(part, dict):
            merged[group] = {**merged[group], **part}
        elif part is not None:
            merged[group] = part
    if "production_collection" in (body or {}):
        merged["production_collection"] = body["production_collection"]
    return merged


def normalize(project_id: str, owner: str, policy: dict, *, slice_ids: list[str], node_ids: list[str]) -> dict:
    """校验并规整；不合法抛 400 invalid_argument（`fields`）或 422 budget_exceeds_quota（`dimension`、`max`）。"""
    fields: dict[str, str] = {}
    scope, budget, advanced = policy.get("scope"), policy.get("budget"), policy.get("advanced")
    if not isinstance(scope, dict) or not isinstance(budget, dict) or not isinstance(advanced, dict):
        raise _invalid({key: "object" for key in ("scope", "budget", "advanced")
                        if not isinstance(policy.get(key), dict)})
    out_scope = {
        "mode": scope.get("mode"), "slice_ids": [], "refresh_stale_first": scope.get("refresh_stale_first", True),
        "generate_scenarios": scope.get("generate_scenarios", True),
        "include_optional": scope.get("include_optional", False), "until_node": scope.get("until_node"),
    }
    if out_scope["mode"] not in SCOPE_MODES:
        fields["scope.mode"] = "enum"
    picked = scope.get("slice_ids") or []
    if not isinstance(picked, list) or any(item not in slice_ids for item in picked):
        fields["scope.slice_ids"] = "unknown"
    else:
        out_scope["slice_ids"] = [item for item in slice_ids if item in picked]
    if out_scope["mode"] == "selected" and not out_scope["slice_ids"] and "scope.slice_ids" not in fields:
        fields["scope.slice_ids"] = "required"
    for key in ("refresh_stale_first", "generate_scenarios", "include_optional"):
        if not isinstance(out_scope[key], bool):
            fields[f"scope.{key}"] = "bool"
    until = out_scope["until_node"]
    if until is not None and (until not in node_ids or until in NOT_UNTIL):
        fields["scope.until_node"] = "unknown"
    cost = budget.get("max_cost")
    amount = cost.get("amount") if isinstance(cost, dict) else None
    duration, tokens = budget.get("max_duration_min"), budget.get("max_tokens")
    for key, value in (("budget.max_duration_min", duration), ("budget.max_tokens", tokens),
                       ("budget.max_cost", amount)):
        if not _positive(value):
            fields[key] = "positive"
    threshold = advanced.get("confidence_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0.5 <= threshold <= 1.0:
        fields["advanced.confidence_threshold"] = "out_of_range"
    if advanced.get("below_threshold") not in BELOW_THRESHOLD:
        fields["advanced.below_threshold"] = "enum"
    if advanced.get("max_fix_rounds") not in (1, 2, 3):
        fields["advanced.max_fix_rounds"] = "enum"
    kinds = _strings(advanced.get("must_suspend_kinds", []), "advanced.must_suspend_kinds", fields)
    priority = _strings(advanced.get("source_priority", []), "advanced.source_priority", fields)
    production = policy.get("production_collection", [])
    if not isinstance(production, list) or len(production) > LIST_MAX or not all(
            isinstance(item, dict) and isinstance(item.get("source_id"), str) for item in production):
        fields["production_collection"] = "list"
        production = []
    if fields:
        raise _invalid(fields)
    limit = caps(project_id, owner)
    for dimension, value, cap in (("duration", duration, limit["max_duration_min"]),
                                  ("tokens", tokens, limit["max_tokens"]), ("cost", amount, limit["max_cost"])):
        if value > cap:
            raise ApiError("budget_exceeds_quota", "预算超出平台上限或剩余配额", 422,
                           {"dimension": dimension, "max": cap})
    return {
        "scope": out_scope,
        "budget": {"max_duration_min": int(duration), "max_tokens": int(tokens),
                   "max_cost": {"amount": float(amount), "currency": limit["currency"]}},
        # 本期不生效（证据源只留接口）：原样存，只留认得的字段
        "production_collection": [{"source_id": item["source_id"], "allowed": bool(item.get("allowed")),
                                   "window": item.get("window") if isinstance(item.get("window"), dict) else None,
                                   "scope": [str(value) for value in item.get("scope") or []][:LIST_MAX]}
                                  for item in production],
        "advanced": {"confidence_threshold": round(float(threshold), 2), "below_threshold": advanced["below_threshold"],
                     "must_suspend_kinds": kinds, "source_priority": priority,
                     "max_fix_rounds": advanced["max_fix_rounds"]},
    }


def save(project_id: str, policy: dict) -> None:
    store.write_json(_path(project_id), policy)
