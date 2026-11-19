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
    """目录与详情显示的版本与通道：stable 通道设了就是它；只发布到 beta 的包显示 beta（原来 stable 没设时
    退回「最新的正式版本」，只在 beta 上的 0.1.0 也显示成 stable）。"""
    stable = repository.latest(name, "stable")
    beta = repository.channel(name, "beta")
    if stable and (repository.channel_entry(name).get("stable") or not beta):
        return stable, "stable"
    if beta:
        return beta, "beta"
    return stable, "stable"


def _publisher(repository, name: str, version: str, manifest) -> dict:
    """发布方：官方只认镜像内置的包；上传的包清单里写了 official 也按租户显示。"""
    if repository.owner(name):
        return {"kind": "private", "label": "仅自己可见"}
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
    converter = _converter(name, manifest)
    source = _source(manifest)
    if converter is not None or source is not None:
        # 多源连接器跑在连接器网关里，平台级，不按项目启用（ADR-0073、ADR-0103）
        status = "platform_installed"
    return {
        "name": name, "version": latest, "label": manifest.label, "description": manifest.description,
        "category": manifest.category, "publisher": _publisher(repository, name, latest, manifest), "ui": _ui(manifest),
        "batch": manifest.raw.get("batch"), "badges": list(manifest.raw.get("badges") or []),
        "latest": latest, "channel": channel,
        "project_status": status, "locked_version": locked, "usage_note": None,
        "default_on": bool(manifest.raw.get("default_on") or manifest.required),
        # 必装（core）：不带项目时 project_status 为空，新建向导靠它锁住开关
        "required": bool(manifest.required),
        "converter": converter,
        "source": source,
        "trust": _trust(repository, name, latest) if converter is not None or source is not None else None,
    }


def _source(manifest) -> dict | None:
    """多源连接器里的数据源连接器（清单 `source` 段，ADR-0103）。"""
    spec = manifest.raw.get("source")
    if manifest.kind != "provider" or not isinstance(spec, dict):
        return None
    return {"fields": len(spec.get("fields") or []), "license": str(spec.get("license") or "")}


def _trust(repository, name: str, version: str) -> dict | None:
    from .. import datasources

    try:
        return datasources.trust_state(repository, name, version)
    except Exception:  # noqa: BLE001 - 信任状态是附带信息
        return None


def _converter(name: str, manifest) -> dict | None:
    """多源连接器里的转换器（清单 `converter` 段）：能转的文件类型、现在是哪几种的默认。"""
    spec = manifest.raw.get("converter")
    if manifest.kind != "provider" or not isinstance(spec, dict):
        return None
    from .. import material_conversion

    types = [str(item).lower() for item in spec.get("types") or []]
    default_for = []
    for file_type in types:
        chosen = material_conversion.pick(file_type)
        if chosen and chosen["id"] == name:
            default_for.append(file_type)
    return {"types": types, "default_for": default_for, "license": str(spec.get("license") or "")}


def catalog(manager, *, category: str = "", q: str = "", project_id: str = "", viewer: str = "",
            installed_only: bool = False, updatable_only: bool = False) -> dict:
    """PL-01：插件目录，不分页（平台包总数上限 500）。viewer 自己的私有包排在公开包后面。"""
    repository = manager.packs.repository
    manager.packs.core_version()
    project = project_packs(manager, project_id) if project_id else None
    wanted = {value for value in category.split(",") if value} if category else set()
    if wanted - set(CATEGORY_KEYS):
        raise PackError("invalid_argument", f"category 只能取 {'|'.join(CATEGORY_KEYS)}", 400,
                        {"fields": {"category": "enum"}})
    # 多源连接器：包仓库里的只列转换器和数据源连接器（ADR-0073、ADR-0103）
    names = [*repository.names()[:500], *repository.private_names(viewer)]
    cards = [item for item in (card(manager, name, project) for name in names)
             if item and (item["category"] != "provider" or item["converter"] is not None or item["source"] is not None)]
    counts = {key: sum(1 for item in cards if item["category"] == key) for key in CATEGORY_KEYS}
    counts.update(all=len(cards),
                  enabled=sum(1 for item in cards if item["project_status"] in ("required", "enabled", "update", "pending")),
                  updatable=sum(1 for item in cards if item["project_status"] == "update"))
    needle = q.strip().lower()

    def matches(item: dict) -> bool:
        if wanted and item["category"] not in wanted:
            return False
        if needle and needle not in f"{item['name']} {item['label']} {item['description']}".lower():
            return False
        if installed_only and item["project_status"] not in ("required", "enabled", "update", "pending"):
            return False
        return not updatable_only or item["project_status"] == "update"

    items = [item for item in cards if matches(item)]
    body = {"items": items, "counts": counts}
    if project_id:
        # 按资料、数据源与分段进度推荐要等流程运行时的状态计算（P2）
        body["recommendations"] = []
    return body


def detail(manager, name: str, *, project_id: str = "", version: str = "") -> dict:
    """PL-02：插件详情。版本缺省取项目锁定的版本，没有则取 stable 最新版。"""
    repository = manager.packs.repository
    if not repository.versions(name) or repository.retired(name):
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
        "flow_insertions": _flow_insertions(repository, name, version, manifest),
        "project_stats": None,
        "contents": [{"group": label, "items": manifest.items(key)} for key, label in CONTENT_GROUPS
                     if manifest.items(key)],
        "config": list(manifest.contributes.get("config") or []),
        "changelog": changelog,
        "hot_level": manifest.hot_level,
        "effective": manifest.effective if manifest.effective != "restart" else "next_message",
    }


# 主流程的段（PL-02 `flow_insertions[].at`）：包节点挂在哪个 core 节点前后就归哪一段；提取对象、提取操作与指标旁支都是建模，
# 老包写的建模（core.model）照旧归建模、全局一致性归校验
_SEGMENTS = {"core.research": "research", "core.object": "model", "core.operation": "model", "core.model": "model",
             "core.metric": "model", "core.validate": "validate", "core.consistency": "validate",
             "core.publish": "publish"}


def _flow_insertions(repository, name: str, version: str, manifest) -> list[dict]:
    """包往主流程里加的节点：编号、标题与插在哪一段（按 after / before 指向的 core 节点算，指向本包别的节点时跟着它）。"""
    from .assembler import AssemblyError, flow_declaration

    try:
        declared = flow_declaration(name, manifest, repository.version_dir(name, version)) or {}
    except (AssemblyError, OSError, ValueError):
        declared = {}
    nodes = [node for node in declared.get("nodes") or [] if isinstance(node, dict) and node.get("id")]
    if not nodes:
        return [{"id": node, "title": node} for node in manifest.items("nodes") if not node.endswith((".yaml", ".yml"))]
    by_id = {str(node["id"]): node for node in nodes}

    def segment(node: dict, seen: set) -> str | None:
        for key in ("after", "before"):
            for anchor in (node.get(key) if isinstance(node.get(key), list) else [node.get(key)]):
                anchor = str(anchor or "")
                if anchor in _SEGMENTS:
                    return _SEGMENTS[anchor]
                if anchor in by_id and anchor not in seen:
                    found = segment(by_id[anchor], seen | {anchor})
                    if found:
                        return found
        return None

    out = []
    for node in nodes:
        item = {"id": str(node["id"]), "title": str(node.get("title") or node["id"])}
        at = segment(node, {str(node["id"])})
        if at:
            item["at"] = at
        out.append(item)
    return out

