"""工作台插件开发模式（ADR-0087，契约 F17）：在对话里写插件草稿，一键装到本人账号、启用到当前项目。

- `assembly`：插件开发装配目录 `<数据目录>/plugin-dev/<哈希>/`，插件对话那一轮助手副本的 `.claude` 指向它；
- `drafts`：主线 `plugins/<包名>/` 下的草稿：列表、详情（平台预检、和已装版本的差异）、读写文件、安装并启用；
- 宿主接入（agent_sessions、fingerprint、injection、turns）按对话类型分流，判断都经这里的 `is_plugin`。
"""

from __future__ import annotations

DRAFTS_DIR = "plugins"
KIND = "plugin"


def is_plugin(project_id: str, conversation_id: str | None) -> bool:
    from .. import conversation_store

    return bool(conversation_id) and conversation_store.kind_of(project_id, conversation_id) == KIND


def empty_suggestions() -> dict:
    """插件对话没有建议条（契约 F17 第 9 条）。"""
    from ..flow import view

    return {"mode": "normal", "primary": None, "secondary": [], "all_commands": False, "note": None,
            "rev": view.next_rev()}


def owner_of(project_id: str) -> str:
    """装插件、预检包名用的身份：项目所有者；本地模式（不鉴权）同接口层记 local。"""
    from ...api import deps
    from .. import auth
    from ..agent_sessions import project_owner

    return project_owner(project_id) or ("" if auth.ENABLED else deps.LOCAL_USER)


def injection_text(project_id: str, conversation_id: str) -> str:
    """插件对话每条消息前的草稿状态（替代建模的阶段、下一步、目标、包提示、自主构建、资料）。"""
    from ...api import deps
    from .. import conversation_store
    from . import drafts

    name = (conversation_store.entry(project_id, conversation_id) or {}).get("plugin")
    if not name:
        return (f"插件开发：这条对话还没有草稿。新草稿建在 {DRAFTS_DIR}/<包名>/（包名 2～40 位小写字母、数字、连字符，"
                "字母开头），本体只读。")
    manager = deps.workspaces
    lines = [f"插件开发：这条对话的草稿是 {DRAFTS_DIR}/{name}/，只改这里，本体只读。"]
    try:
        view = drafts.view(manager, project_id, name, owner_of(project_id), full=False)
    except drafts.DraftError:
        return lines[0] + "主线上还没有这份草稿（上一轮可能撤回了），需要时重新建。"
    install = view["install"]
    enabled = install["enabled_version"]
    lines.append(f"清单版本 {view['version'] or '（清单读不出）'}；本项目"
                 + (f"启用的版本 {enabled}" + ("，草稿已改过，还没重新安装" if install["state"] == "outdated" else "")
                    if enabled else "还没装") + "。")
    failed = drafts.failed_items(manager, project_id, name, owner_of(project_id))
    if failed:
        lines.append("上一次平台预检没过的项（先修这些）：" + "；".join(
            f"{item['label']}：{item['detail']}" if item.get("detail") else item["label"] for item in failed) + "。")
    return "\n".join(lines)
