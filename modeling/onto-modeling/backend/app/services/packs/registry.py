"""扩展项注册表（详设 4.6 第 6 项、5.6、5.9.2、5.9.7；方案 D17；契约 PL-12）。

包在清单 `contributes` 里声明的页面、子页、界面入口、产物格式、视图、门禁、配置项、提示规则，
都在这里按项目汇总。包的内容从包仓库里不可修改的版本目录读，不跑包的代码。

生效口径（详设 5.9.6）：
- 左栏、子页、产物类型、视图跟锁文件走：启用后左栏马上出现、能看到已有产物；停用后马上消失，
  产物留在工作区；
- 页面上的按钮要等新的装配目录生效（下一条消息）才能用，在那之前 `pages[].pending=true`；
- 停用过的包（锁文件里 enabled=false）的页面列在 `disabled_pages`，旧链接显示「插件已停用」。

左栏 = 内核 4 项 + 已启用包声明的页面 ∩ 平台内置页面（且页面接口版本满足），按声明顺序排，
插件页面超过 5 个的进「更多」。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import extensions as extension_items
from . import lockfile
from . import manifest as mf
from .manifest import Manifest

_log = logging.getLogger(__name__)

# 内核能力 4 项（详设 5.9.7：固定在上面）
KERNEL_NAV = (
    ("wb-l2", "工作台", "layout"),
    ("studio", "Studio", "graph"),
    ("resources", "资料", "folder"),
    ("publish", "发布", "upload"),
)
# 平台内置页面的默认标题与图标：包没写 nav 时用它
PAGE_DEFAULTS = {
    "scenarios": ("场景模拟", "flask"),
    "scripts": ("代码生成与求解", "code"),
}
NAV_VISIBLE = 5
PLUGIN_ORDER_BASE = 10

# 左栏角标的数据来源（待处理里各包的事项数）：待处理汇总落地后由它登记
_badge_provider: Callable[[str], dict[str, int]] | None = None


def register_badge_provider(provider: Callable[[str], dict[str, int]]) -> None:
    """登记角标计数 `provider(project_id) -> {包名: 事项数}`：一次汇总给全部包，不按包各算一遍。"""
    global _badge_provider
    _badge_provider = provider


@dataclass(frozen=True)
class PackView:
    """项目视角下的一个包：名字、版本、清单与包仓库里的目录。"""

    name: str
    version: str
    manifest: Manifest
    directory: Path
    official: bool = False  # 镜像自带的官方包（入库来源 builtin）


def _views(repository, entries) -> list[PackView]:
    out = []
    for name, version in entries:
        if not repository.has(name, version):
            continue
        try:
            out.append(PackView(name, version, repository.manifest(name, version), repository.version_dir(name, version),
                                official=repository.is_builtin(name, version)))
        except Exception as exc:  # noqa: BLE001 - 一个包的清单坏了不挡别的包
            _log.warning("读取包 %s %s 的清单失败：%s", name, version, exc)
    # core 在最前，其余按名字（与锁文件、装配顺序一致）
    return sorted(out, key=lambda view: (view.name != "core", view.name))


def desired_packs(manager, root: Path) -> list[PackView]:
    """锁文件里启用的包（左栏、产物类型按它算）。"""
    try:
        lock = lockfile.read(root)
    except (ValueError, OSError):
        lock = None
    if lock is None:
        active = manager.packs.assembler.active(root) or {}
        return _views(manager.packs.repository, [(item["name"], item["version"]) for item in active.get("packs") or []])
    return _views(manager.packs.repository, [(entry.name, entry.version) for entry in lock.enabled()])


def active_packs(manager, root: Path) -> list[PackView]:
    """生效中的装配目录里的包（按钮、提示规则按它算）。"""
    active = manager.packs.assembler.active(root) or {}
    return _views(manager.packs.repository, [(item["name"], item["version"]) for item in active.get("packs") or []])


def disabled_packs(manager, root: Path) -> list[PackView]:
    try:
        lock = lockfile.read(root)
    except (ValueError, OSError):
        return []
    if lock is None:
        return []
    return _views(manager.packs.repository, [(entry.name, entry.version) for entry in lock.packs if not entry.enabled])


def _custom_page_ok(view: PackView, page: dict) -> bool:
    """上传包自己的页面（ADR-0103）：id 合规、ui 指向本包声明的编辑器；官方包不走 iframe，暂不支持。"""
    page_id, ui = page.get("id"), page.get("ui")
    return (not view.official and isinstance(page_id, str) and bool(mf.PAGE_ID_RE.match(page_id))
            and any(editor["id"] == ui for editor in view.manifest.editors()))


def _page_ok(page: dict, view: PackView) -> bool:
    if page.get("id") not in mf.BUILTIN_PAGES and not _custom_page_ok(view, page):
        return False
    spec = str(page.get("page_api") or view.manifest.page_api or "")
    return not spec or mf.satisfies(mf.PAGE_API, spec)


def page_key(view: PackView, page: dict) -> str:
    """左栏与路由里的页面 id：内置页就是页面 id，包自己的页面是「包名:页面 id」。"""
    return page["id"] if page.get("id") in mf.BUILTIN_PAGES else f"{view.name}:{page['id']}"


def _action_id(index: int, action: dict) -> str:
    raw = str(action.get("id") or "")
    return raw if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", raw) else f"a{index + 1}"


def page_view(view: PackView, page: dict, *, pending: bool) -> dict:
    """契约 7.11 Extensions.pages 的一项。"""
    data = page.get("data") or {}
    actions = []
    for index, action in enumerate(page.get("actions") or []):
        if not isinstance(action, dict) or not action.get("command"):
            continue
        actions.append({
            "id": _action_id(index, action), "label": str(action.get("label") or action["command"]),
            "command": str(action["command"]), "hint": str(action.get("hint") or ""),
        })
    custom = page.get("id") not in mf.BUILTIN_PAGES
    return {
        "id": page_key(view, page), "pack": view.name,
        **({"page": page["id"], "ui": str(page["ui"])} if custom else {}),
        "page_api": str(page.get("page_api") or view.manifest.page_api or ""),
        "data": {key: [str(item) for item in data.get(key) or []] for key in ("artifacts", "reports", "nodes")},
        "actions": actions,
        "pending": pending,
    }


def nav_item(view: PackView, page: dict, index: int) -> dict:
    nav = page.get("nav") or {}
    title, icon = PAGE_DEFAULTS.get(page["id"], (page["id"], "layers"))
    icon = str(nav.get("icon") or icon)
    if icon not in mf.NAV_ICONS:
        icon = PAGE_DEFAULTS.get(page["id"], (page["id"], "layers"))[1]
    order = nav.get("order")
    return {
        "id": page_key(view, page), "title": str(nav.get("title") or title), "icon": icon, "kind": "plugin",
        "pack": view.name, "pack_label": view.manifest.label,
        "order": PLUGIN_ORDER_BASE + (int(order) if isinstance(order, int) else 100 + index),
        "overflow": False, **({"badge_source": page["badge"]} if page.get("badge") else {}),
    }


def _badge_counts(project_id: str) -> dict[str, int]:
    if _badge_provider is None:
        return {}
    try:
        return dict(_badge_provider(project_id) or {})
    except Exception:  # noqa: BLE001 - 角标取不到不影响左栏
        return {}


def extensions(manager, project_id: str) -> dict:
    """PL-12：本项目的扩展项注册表（契约 7.11 Extensions）。"""
    from ...kernel.artifacts import registry as artifact_registry

    runtime = manager.packs
    root = manager.project_root(project_id)
    runtime.ensure_lock(root)
    status = runtime.status(root)
    desired = desired_packs(manager, root)
    active = {(view.name, view.version) for view in active_packs(manager, root)}

    kernel = [{"id": page_id, "title": title, "icon": icon, "kind": "kernel", "order": order, "overflow": False}
              for order, (page_id, title, icon) in enumerate(KERNEL_NAV)]
    plugin_nav: list[dict] = []
    pages: list[dict] = []
    subpages: list[dict] = []
    entries: list[dict] = []
    gates: list[dict] = []
    config: list[dict] = []
    view_choices: list[dict] = []
    seen_pages: set[str] = set()
    badges: dict[str, int] | None = None
    for view in desired:
        pending = (view.name, view.version) not in active
        for index, page in enumerate(view.manifest.pages()):
            if not _page_ok(page, view) or page_key(view, page) in seen_pages:
                continue
            seen_pages.add(page_key(view, page))
            item = nav_item(view, page, index)
            if item.pop("badge_source", None):
                if badges is None:
                    badges = _badge_counts(project_id)
                item["badge"] = {"source": "inbox", "count": int(badges.get(view.name) or 0)}
            plugin_nav.append(item)
            pages.append(page_view(view, page, pending=pending))
        for sub in view.manifest.subpages():
            if sub.get("parent") in mf.KERNEL_PAGES and sub.get("id") in mf.BUILTIN_SUBPAGES:
                subpages.append({"parent": sub["parent"], "id": sub["id"], "title": str(sub.get("title") or sub["id"]),
                                 "pack": view.name, "pack_label": view.manifest.label})
        entries += extension_items.entries(view)
        gates += [extension_items.gate_view(view, gate) for gate in view.manifest.declared("gates")]
        view_choices += extension_items.view_choices(view)
        for item in view.manifest.declared("config"):
            key = str(item.get("key") or item.get("id") or "")
            config.append({"key": extension_items.config_key(view.name, key), "pack": view.name})
    plugin_nav.sort(key=lambda item: (item["order"], item["pack"], item["id"]))
    for index, item in enumerate(plugin_nav):
        item["order"] = PLUGIN_ORDER_BASE + index
        item["overflow"] = index >= NAV_VISIBLE

    types = artifact_registry.types_for_packs(desired)
    disabled = []
    for view in disabled_packs(manager, root):
        for page in view.manifest.pages():
            if (page.get("id") in mf.BUILTIN_PAGES or _custom_page_ok(view, page)) and page_key(view, page) not in seen_pages:
                title = (page.get("nav") or {}).get("title") or PAGE_DEFAULTS.get(page["id"], (page["id"],))[0]
                disabled.append({"id": page_key(view, page), "pack": view.name, "pack_label": view.manifest.label,
                                 "title": title})
    return {
        "active_hash": status.get("active_hash") or "",
        "pending_hash": status.get("pending_hash"),
        "page_api": mf.PAGE_API,
        "nav": kernel + plugin_nav,
        "subpages": subpages,
        "pages": pages,
        "entries": entries,
        "artifact_types": [item.type for item in types],
        "views": [item.type for item in types if item.view_declared],
        "gates": gates,
        "config": config,
        "view_choices": view_choices,
        "disabled_pages": disabled,
    }


# 每个项目上一次比较时的注册表：项目流 extensions.updated 的 changed 拿它和新算的比
_LAST_VIEWS: dict[str, dict] = {}
_LAST_LOCK = threading.Lock()


def remember_baseline(project_id: str, view: dict | None) -> None:
    """改锁文件之前的注册表（packs.service 切换前调）：这个进程里还没有比较基准时记下它。"""
    if view is not None:
        with _LAST_LOCK:
            _LAST_VIEWS.setdefault(project_id, view)


def extension_differ(manager=None) -> Callable[[str, str | None, str | None], list[str] | None]:
    """给项目流登记的比较函数 `differ(项目, 旧装配哈希, 新装配哈希) -> 变了的类别`。

    哈希只说明装配变了；内容和上一次比较时（或改锁文件之前记下）的注册表比，比完换成新的。
    这个进程里还没有基准的项目返回 None（前端整份重拉）。manager 缺省取接口层的工作区管理器。
    """

    def differ(project_id: str, before: str | None, after: str | None) -> list[str] | None:
        target = manager
        if target is None:
            from ...api import deps

            target = deps.workspaces
        try:
            current = extensions(target, project_id)
        except Exception:  # noqa: BLE001 - 算不出来就整份重拉
            _log.warning("项目 %s 的扩展项比较失败", project_id, exc_info=True)
            return None
        with _LAST_LOCK:
            previous = _LAST_VIEWS.get(project_id)
            _LAST_VIEWS[project_id] = current
        return None if previous is None else changed_parts(previous, current)

    return differ


def resolve_action(manager, project_id: str, action: dict) -> dict | None:
    """S-20 的 `action: {pack, page, action_id}` 换成命令：{command, pack, page, label}；认不出返回 None。

    只按锁文件里启用的包查（和左栏同一口径）；命令在不在生效中的装配里由 S-20 自己判断
    （待生效时回 `pack_not_enabled`、`effective=next_message`）。会话宿主把它登记成 S-20 的解析函数。
    """
    if not isinstance(action, dict):
        return None
    pack, page_id, action_id = (str(action.get(key) or "") for key in ("pack", "page", "action_id"))
    root = manager.project_root(project_id)
    for view in desired_packs(manager, root):
        if view.name != pack:
            continue
        for page in view.manifest.pages():
            if page.get("id") != page_id or not _page_ok(page, view):
                continue
            for item in page_view(view, page, pending=False)["actions"]:
                if item["id"] == action_id:
                    return {"command": item["command"], "pack": pack, "page": page_id, "label": item["label"]}
    return None


_DIGEST_KEYS = ("nav", "pages", "subpages", "entries", "artifact_types", "views", "gates", "view_choices")


def digest(view: dict) -> dict:
    """extensions.updated 的比较基准：各部分的内容（不含哈希）。"""
    return {key: view.get(key) for key in _DIGEST_KEYS}


def changed_parts(before: dict | None, after: dict) -> list[str]:
    old = digest(before or {})
    new = digest(after)
    return [key for key in ("nav", "pages", "subpages", "entries", "artifact_types", "views", "gates", "view_choices")
            if old.get(key) != new.get(key)]


# ---- 提示规则（详设 5.6「提示规则」：两轮对话之间由会话宿主注入）----


def prompt_rules(manager, root: Path) -> list[dict]:
    """生效中的装配里各包声明的提示规则：{id, pack, match: [正则], text}。

    会话宿主（详设 4.2 ㊶）在两轮之间按用户消息匹配并注入 text；领域包扩展的「结束本轮」就是一条。
    """
    from ..flow import conditions

    rules = []
    for view in active_packs(manager, root):
        for index, rule in enumerate(view.manifest.declared("prompts")):
            patterns = [str(item) for item in rule.get("match") or [] if str(item)]
            text = str(rule.get("text") or "").strip()
            if not patterns or not text:
                continue
            if rule.get("visible_if") and not conditions.holds(str(rule["visible_if"]), root):
                continue  # 按项目条件注入（ADR-0083），如有系统快照时的提醒
            compiled = []
            for pattern in patterns:
                try:
                    compiled.append(re.compile(pattern))
                except re.error:
                    _log.warning("包 %s 的提示规则正则不合法：%s", view.name, pattern)
            if compiled:
                rules.append({"id": str(rule.get("id") or f"{view.name}-{index + 1}"), "pack": view.name,
                              "match": compiled, "text": text})
    return rules


def prompt_hints(manager, root: Path, user_text: str) -> list[str]:
    """用户这条消息命中的提示规则文本（去重，按包顺序），加上这条命令所在节点上挂的节点钩子。"""
    hints = []
    for rule in prompt_rules(manager, root):
        if any(pattern.search(user_text or "") for pattern in rule["match"]) and rule["text"] not in hints:
            hints.append(rule["text"])
    for text in hook_hints(root, user_text):
        if text not in hints:
            hints.append(text)
    return hints


def hook_hints(root: Path, user_text: str) -> list[str]:
    """节点钩子（详设 6.4）：这条消息是某个节点的命令、节点上挂了钩子时，提示助手在之前或之后做那一步。
    宿主在两轮之间注入（两个引擎都能拿到），不改命令文档。"""
    command = str(user_text or "").strip().split(" ")[0].split("\n")[0]
    if not command.startswith("/"):
        return []
    from ..flow import model

    try:
        flow = model.load_for(root)
    except Exception:  # noqa: BLE001 - 流程读不出来就不提示
        return []
    if not flow.get("hooks"):
        return []
    nodes = {}
    for node in flow.get("nodes") or []:
        for action in node.get("actions") or []:
            text = str(action.get("command") or "").split(" ")[0]
            if text.startswith("/"):
                nodes.setdefault(text, node)
    node = nodes.get(command)
    if node is None:
        return []
    out = []
    for hook in flow["hooks"]:
        if hook.get("node") != node["id"]:
            continue
        if hook.get("when") == "before":
            out.append(f"插件 {hook['pack']} 在「{node['title']}」之前加了一步：先执行 {hook['command']}（{hook['title']}），再做这一步。")
        else:
            out.append(f"插件 {hook['pack']} 在「{node['title']}」之后加了一步：这一步做完，接着执行 {hook['command']}（{hook['title']}）。")
    return out
