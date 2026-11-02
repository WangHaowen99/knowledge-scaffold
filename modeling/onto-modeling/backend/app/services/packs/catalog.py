"""插件目录与详情（契约 PL-01、PL-02）：包仓库里每个包的最新可用版本，带本项目视角的状态。"""

from __future__ import annotations

from . import lockfile
from . import manifest as mf
from .service import PackError, _ui, project_packs

CATEGORY_KEYS = ("capability", "industry", "system", "provider", "node", "exporter")
# 「包含什么」的分组用界面叫法（契约 PL-02 contents）
CONTENT_GROUPS = (("commands", "命令"), ("agents", "子代理"), ("skills", "技能"), ("scripts", "脚本"),
                  ("templates", "模板"), ("reference", "参考指南"))


def _latest(repository, name: str) -> tuple[str | None, str]:
    stable = repository.latest(name, "stable")
    if stable:
        return stable, "stable"
    beta = repository.channel(name, "beta")
    return beta, "beta"


def _publisher(repository, name: str, version: str, manifest) -> dict:
    """发布方：官方只认镜像内置的包；上传的包清单里写了 official 也按租户显示。"""
    if repository.is_builtin(name, version):
        return {"kind": "official", "label": manifest.publisher.get("label") or "平台"}
    return {"kind": "tenant", "label": manifest.publisher.get("label") if manifest.publisher.get("kind") == "tenant" else ""}


def card(manager, name: str, project: dict | None) -> dict | None:
    """契约 7.11 PackCard；project 是 project_packs 的结果（不带项目时为 None）。"""
    repository = manager.packs.repository
    latest, channel = _latest(repository, name)
    if latest is None:
        return None
    manifest = repository.manifest(name, latest)
    locked = None
    status = None
    if project is not None:
        item = next((item for item in project["items"] if item["name"] == name), None)
        locked = item["locked_version"] if item else None
        if item is None or item["status"] == "disabled":
            status = "available" if manifest.hot_level != "H2" else "platform_installed"
        elif item["status"] == "pending":
            status = "pending"
        elif item["latest_available"]:
            status = "update"
        else:
            status = item["status"]
    return {
        "name": name, "version": latest, "label": manifest.label, "description": manifest.description,
        "category": manifest.category, "publisher": _publisher(repository, name, latest, manifest), "ui": _ui(manifest),
        "batch": manifest.raw.get("batch"), "badges": list(manifest.raw.get("badges") or []),
        "latest": latest, "channel": channel,
        "project_status": status, "locked_version": locked, "usage_note": None,
        "default_on": bool(manifest.raw.get("default_on") or manifest.required),
    }


def catalog(manager, *, category: str = "", q: str = "", project_id: str = "",
            installed_only: bool = False, updatable_only: bool = False) -> dict:
    """PL-01：插件目录，不分页（平台包总数上限 500）。"""
    repository = manager.packs.repository
    manager.packs.core_version()
    project = project_packs(manager, project_id) if project_id else None
    wanted = {value for value in category.split(",") if value} if category else set()
    if wanted - set(CATEGORY_KEYS):
        raise PackError("invalid_argument", f"category 只能取 {'|'.join(CATEGORY_KEYS)}", 400,
                        {"fields": {"category": "enum"}})
    # 证据源提供方本期不开放（ADR-0056）：目录里不列，计数里也不出这一类
    cards = [item for item in (card(manager, name, project) for name in repository.names()[:500])
             if item and item["category"] != "provider"]
    counts = {key: sum(1 for item in cards if item["category"] == key) for key in CATEGORY_KEYS if key != "provider"}
    counts.update(all=len(cards),
                  enabled=sum(1 for item in cards if item["project_status"] in ("required", "enabled", "update", "pending")),
                  updatable=sum(1 for item in cards if item["project_status"] == "update"))
    needle = q.strip().lower()
    items = [
        item for item in cards
        if (not wanted or item["category"] in wanted)
        and (not needle or needle in f"{item['name']} {item['label']} {item['description']}".lower())
        and (not installed_only or item["project_status"] in ("required", "enabled", "update", "pending"))
        and (not updatable_only or item["project_status"] == "update")
    ]
    body = {"items": items, "counts": counts}
    if project_id:
        # 按资料、数据源与切片进度推荐要等流程运行时的状态计算（P2）
        body["recommendations"] = []
    return body


def detail(manager, name: str, *, project_id: str = "", version: str = "") -> dict:
    """PL-02：插件详情。版本缺省取项目锁定的版本，没有则取 stable 最新版。"""
    repository = manager.packs.repository
    if not repository.versions(name):
        raise PackError("pack_not_found", f"包 {name} 不存在", 404)
    project = project_packs(manager, project_id) if project_id else None
    if not version and project_id:
        lock = lockfile.read(manager.project_root(project_id))
        entry = lock.get(name) if lock else None
        version = entry.version if entry else ""
    version = version or _latest(repository, name)[0] or repository.versions(name)[0]
    if not repository.has(name, version):
        raise PackError("pack_version_not_found", f"包 {name} 没有版本 {version}", 404)
    manifest = repository.manifest(name, version)
    meta = repository.meta(name, version)
    size, files = repository.size(name, version)
    base = card(manager, name, project) or {}
    item = next((item for item in (project or {}).get("items", []) if item["name"] == name), None)
    changelog = []
    for other in repository.versions(name):
        other_manifest = repository.manifest(name, other)
        other_meta = repository.meta(name, other)
        tags = []
        if item and item["locked_version"] == other:
            tags.append("in_use")
        needs_core = other_manifest.depends.get("core")
        if needs_core and project and not any(
            entry["name"] == "core" and mf.satisfies(entry["locked_version"], needs_core) for entry in project["items"]
        ):
            tags.append("needs_core_upgrade")
        changelog.append({"version": other, "date": other_meta.get("released_at"),
                          "notes": str(other_manifest.raw.get("changelog") or other_meta.get("note") or ""), "tags": tags})
    return {
        **base,
        "version": version,
        "size": size, "files": files,
        "released_at": meta.get("released_at"),
        "released_by": meta.get("released_by") or None,
        "enabled_at": item["enabled_at"] if item else None,
        "requires": {"depends": dict(manifest.depends), "kernel_api": manifest.kernel_api,
                     "page_api": manifest.page_api or None},
        "flow_insertions": [{"id": node, "title": node} for node in manifest.items("nodes")],
        "project_stats": None,
        "contents": [{"group": label, "items": manifest.items(key)} for key, label in CONTENT_GROUPS
                     if manifest.items(key)],
        "config": list(manifest.contributes.get("config") or []),
        "changelog": changelog,
        "hot_level": manifest.hot_level,
        "effective": manifest.effective if manifest.effective != "restart" else "next_message",
    }
