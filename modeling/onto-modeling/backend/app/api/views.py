"""v1 响应里跨接口复用的对象投影（API 契约第 7 章）。

只做"现有数据 → 契约形状"的转换；契约里属于以后阶段的字段（分段、自主构建、待处理计数、
包信息等）给 null 或 0，等对应能力落地再填。
"""

from __future__ import annotations

from pathlib import Path

from ..services import mainline
from .deps import iso

ENGINE_LABELS = {"claude": "稳定引擎", "jiuwen": "内测引擎"}
ENGINE_CHANNELS = {"claude": "stable", "jiuwen": "beta"}


def engine_label(engine: str) -> str:
    return ENGINE_LABELS.get(engine or "claude", engine or "")


def stage_label(meta: dict) -> str:
    """元数据视图里的阶段说明：流程运行时（P1/P2）之前按目录探针的完成数给。"""
    if meta.get("archived"):
        return "已归档"
    done, total = meta.get("stages_done"), meta.get("stages_total")
    if isinstance(done, int) and isinstance(total, int) and total:
        return f"已完成 {done}/{total} 个阶段"
    return "建模中"


def last_activity(meta: dict, root: Path | None = None) -> str | None:
    """最近活动：主线最近提交与资料更新时间取较晚者。"""
    candidates = [meta.get("created_at") or 0, meta.get("resource_updated_at") or 0]
    if root is not None:
        candidates.append(mainline.head_time(root) or 0)
    return iso(max(candidates))


def project_meta_view(meta: dict, root: Path | None = None, *, disk_bytes: int | None = None) -> dict:
    """ProjectMetaView：admin、ops 看到的元数据视图，不含目标、资料、产物（契约 1.4、7.3）。"""
    view = {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "owner": meta.get("owner") or None,
        "stage_label": stage_label(meta),
        "build_mode": meta.get("build_mode") or "L2",
        "archived": bool(meta.get("archived")),
        "created_at": iso(meta.get("created_at")),
        "last_activity_at": last_activity(meta, root),
    }
    if disk_bytes is not None:
        view["disk_bytes"] = disk_bytes
    return view


def project_meta(meta: dict, root: Path) -> dict:
    """ProjectMeta：所有者看到的完整元数据（契约 7.3）。"""
    state = mainline.state(root)
    last = meta.get("last_package_publish") or None
    engine = meta.get("engine") or "claude"
    return {
        "id": meta.get("id"),
        "name": meta.get("name"),
        "package_name": meta.get("package_name") or meta.get("id"),
        "goal": meta.get("goal") if meta.get("goal") is not None else (meta.get("background") or ""),
        "owner": meta.get("owner") or "",
        "created_at": iso(meta.get("created_at")),
        "archived": bool(meta.get("archived")),
        "archived_at": iso(meta.get("archived_at")),
        "engine": engine,
        "engine_label": engine_label(engine),
        "build_mode": meta.get("build_mode") or "L2",
        "version_label": state["version_label"],
        "head": state["head"],
        "last_release": {"version": last["version"], "at": iso(last.get("at"))} if last else None,
        "last_activity_at": last_activity(meta, root),
        "created_from": meta.get("created_from") or "wizard",
    }


# 门禁 finding 的旧码 → 契约码（2.9：求解契约回执三种状态改名）
FINDING_CODES = {
    "solver_contract_validation_required": "solver_receipt_required",
    "solver_contract_validation_stale": "solver_receipt_stale",
    "solver_contract_validation_failed": "solver_receipt_failed",
}


def finding(item: dict) -> dict:
    """检查器与门禁的结论 → Finding（契约 7.1）：severity 改叫 level，码按契约改名。"""
    view = {
        "code": FINDING_CODES.get(item.get("code", ""), item.get("code", "")),
        "level": item.get("level") or ("warning" if item.get("severity") == "warning" else "error"),
        "message": item.get("message", ""),
    }
    for key in ("target", "path", "field", "hint", "solver_key"):
        if item.get(key):
            view[key] = item[key]
    return view
