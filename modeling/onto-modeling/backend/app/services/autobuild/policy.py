"""自主构建策略（详设 8.5 策略表；契约 AB-01、AB-02、7.15 AutobuildPolicy）。

范围、决定策略、资料优先级按项目存在数据目录；硬性不做的几项（发布、删除已确认内容、改资料区）由守卫与写入路径
保证，不在策略里。

- 没有预算（F6）：运行在做完、叫停、出错时结束；花费的硬上限是平台配额（4.10.1），用完时运行暂停。老请求带的
  `budget` 忽略；
- 生产库采集授权随证据采集删除（ADR-0101）：老策略文件里的 `production_collection` 不再读出来，请求里带了也忽略；
- 自主构建按节点排（节点模型重构 1B）：`until_node` 写的是旧节点编号（如 `core.model`）时按流程的别名认；按段时代的
  `slice_ids` 原样存着、不再起作用，「只跑选中的段」按「全部没做完的」跑；`generate_scenarios` 存着、不再起作用。
"""

from __future__ import annotations

import copy
import hashlib
import json

from ...api.errors import ApiError
from . import store

BELOW_THRESHOLD = ("conservative_assumption", "suspend")
# 「拿不准时」怎么办的说法（预览、开始卡、设置页）：用户看得懂的话，不用「保守选项」「假设」「挂起」
BELOW_TEXT = {"conservative_assumption": "先按最稳妥的做法往下做，做完列出来给你复核",
              "suspend": "先跳过这一项，留给你来定"}
SCOPE_MODES = ("all_pending", "selected")
# 存着、但本期不生效的组（AB-01 的 not_enforced）；现在没有
NOT_ENFORCED: tuple[str, ...] = ()
LIST_MAX = 20
TEXT_MAX = 60
# 做到哪一步：这些节点不能作为终点（发布是硬性不做；资料分析在建模之前）
NOT_UNTIL = ("core.publish", "core.research", "core.update")
# 删掉的节点：老策略写到这里，等于跑完主线（全局一致性，11-05 删）
REMOVED_UNTIL = ("core.consistency",)
GROUPS = ("scope", "advanced")


def _path(project_id: str):
    return store.project_dir(project_id) / "policy.json"


def revision(policy: dict) -> str:
    raw = json.dumps({key: policy.get(key) for key in GROUPS},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def defaults() -> dict:
    return {
        "scope": {"mode": "all_pending", "slice_ids": [], "refresh_stale_first": True, "generate_scenarios": True,
                  "include_optional": False, "until_node": None},
        "advanced": {"confidence_threshold": 0.8, "below_threshold": "conservative_assumption",
                     "must_suspend_kinds": [], "source_priority": [], "max_fix_rounds": 2},
    }


def load(project_id: str, aliases: dict | None = None) -> dict:
    """本项目的策略（没存过时是默认值）；F6 之前存的 `budget`、证据采集删除前存的 `production_collection` 不再读出来。
    `until_node` 按流程的别名认成现在的节点（aliases 不给时读本项目的流程）。"""
    stored = store.read_json(_path(project_id))
    policy = stored if stored and isinstance(stored.get("scope"), dict) else defaults()
    out = {key: copy.deepcopy(policy.get(key)) for key in GROUPS}
    until = out["scope"].get("until_node")
    if until and until not in REMOVED_UNTIL:
        out["scope"]["until_node"] = (_aliases(project_id) if aliases is None else aliases).get(until, until)
    return out


def _aliases(project_id: str) -> dict:
    """本项目流程的旧节点编号 → 现在的节点；流程读不出时为空（策略照原样读）。"""
    from ...api import deps
    from . import plan

    try:
        return dict(plan.flow(deps.workspaces.project_root(project_id)).get("aliases") or {})
    except Exception:  # noqa: BLE001 - 项目还没建好、流程装配不出：别名不认，校验时再报
        return {}


def advanced(project_id: str) -> dict:
    """决定策略（高级设置）；没存过时是空的，按 decide 里的默认值（阈值 0.8、保守项记为假设）。"""
    stored = store.read_json(_path(project_id)) or {}
    return dict(stored.get("advanced") or {})


def _last_run(project_id: str) -> dict | None:
    from ...api.deps import iso

    for record in store.runs(project_id):
        if record.get("state") in store.ENDED:
            pending = any(entry.get("review_state") == "pending" for entry in record.get("ledger") or [])
            return {"run_id": record["run_id"], "run_no": record["run_no"], "ended_at": iso(record.get("ended_at")),
                    "duration_ms": int((record.get("budget") or {}).get("active_ms") or 0), "reviewed": not pending}
    return None


def view(project_id: str, policy: dict | None = None) -> dict:
    """AB-01。"""
    policy = policy or load(project_id)
    return {
        **policy,
        "platform_limits": {},
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


def merge(current: dict, body: dict | None) -> dict:
    """AB-02 的请求体、AB-03 的 policy_overrides 并进当前策略：给了哪一组改哪一组，组内给了哪项改哪项。"""
    merged = copy.deepcopy(current)
    for group in ("scope", "advanced"):
        part = (body or {}).get(group)
        if isinstance(part, dict):
            merged[group] = {**merged[group], **part}
        elif part is not None:
            merged[group] = part
    return merged


def normalize(policy: dict, *, node_ids: list[str], aliases: dict | None = None) -> dict:
    """校验并规整；不合法抛 400 invalid_argument（`fields`）。`until_node` 先按流程的别名（旧节点编号 → 现在的）认。"""
    fields: dict[str, str] = {}
    scope, advanced = policy.get("scope"), policy.get("advanced")
    if not isinstance(scope, dict) or not isinstance(advanced, dict):
        raise _invalid({key: "object" for key in ("scope", "advanced") if not isinstance(policy.get(key), dict)})
    out_scope = {
        "mode": scope.get("mode"), "slice_ids": [], "refresh_stale_first": scope.get("refresh_stale_first", True),
        "generate_scenarios": scope.get("generate_scenarios", True),
        "include_optional": scope.get("include_optional", False), "until_node": scope.get("until_node"),
    }
    if out_scope["mode"] not in SCOPE_MODES:
        fields["scope.mode"] = "enum"
    picked = scope.get("slice_ids") or []
    if not isinstance(picked, list) or len(picked) > LIST_MAX or not all(isinstance(item, str) for item in picked):
        fields["scope.slice_ids"] = "unknown"
    else:
        out_scope["slice_ids"] = list(picked)  # 按段时代存的：原样存着，不再起作用
    if out_scope["mode"] == "selected":
        # 不再分段：「只跑选中的段」按「全部没做完的」跑，不报错
        out_scope["mode"] = "all_pending"
    for key in ("refresh_stale_first", "generate_scenarios", "include_optional"):
        if not isinstance(out_scope.get(key), bool):
            fields[f"scope.{key}"] = "bool"
    if out_scope["until_node"] in REMOVED_UNTIL:
        out_scope["until_node"] = None
    elif out_scope["until_node"]:
        out_scope["until_node"] = (aliases or {}).get(out_scope["until_node"], out_scope["until_node"])
    until = out_scope["until_node"]
    if until is not None and (until not in node_ids or until in NOT_UNTIL):
        fields["scope.until_node"] = "unknown"
    threshold = advanced.get("confidence_threshold")
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0.5 <= threshold <= 1.0:
        fields["advanced.confidence_threshold"] = "out_of_range"
    if advanced.get("below_threshold") not in BELOW_THRESHOLD:
        fields["advanced.below_threshold"] = "enum"
    if advanced.get("max_fix_rounds") not in (1, 2, 3):
        fields["advanced.max_fix_rounds"] = "enum"
    kinds = _strings(advanced.get("must_suspend_kinds", []), "advanced.must_suspend_kinds", fields)
    priority = _strings(advanced.get("source_priority", []), "advanced.source_priority", fields)
    if fields:
        raise _invalid(fields)
    return {
        "scope": out_scope,
        "advanced": {"confidence_threshold": round(float(threshold), 2), "below_threshold": advanced["below_threshold"],
                     "must_suspend_kinds": kinds, "source_priority": priority,
                     "max_fix_rounds": advanced["max_fix_rounds"]},
    }


def save(project_id: str, policy: dict) -> None:
    store.write_json(_path(project_id), policy)
