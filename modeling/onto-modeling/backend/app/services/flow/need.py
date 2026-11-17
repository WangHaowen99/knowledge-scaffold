"""节点挂法 `need`（节点模型重构 2a）：节点什么时候需要做，只用这一个字段表达。

- `required`：上导航栏，计入进度，没做时推荐，自主构建做；
- `optional`：不上导航栏，挂在它前面最近的必需节点下面，可以跳过，不计入进度；
- `{when: <内核条件>}`：条件成立时出现，处理完就隐藏（条件见 `conditions.py`，`activated` 按包声明的触发算）。

旧写法装配时折算成 need，自上而下取第一条命中的：

| 旧写法 | need |
|---|---|
| `visible_if: X` | `{when: X}`（`preempt` 不带过来） |
| `placement` 或 `section` 是 `on_demand` | `optional`，加 `autobuild: false` |
| `optional: true`、`section: optional`、`placement: branch`、`progress: false` | `optional` |
| 其余（`spine`、`step`、`gate`、`pre_publish`、什么都没写） | `required` |

同时写了 need 和旧字段时以 need 为准。旧字段不删，按 need 补齐（`normalize_node`）：锁定旧 core 的项目、助手副本里的
旧脚本还读 placement、section、optional、progress、visible_if、preempt、entry。

主命令是 actions 里第一个带 command 或 page 的动作；`entry` 只做兼容，没有这样的动作时由它补一个，缺省时取主命令。
第三方包（不是镜像内置入库、也不是 core）的节点没写 autobuild 时装配成 `autobuild: false`。

只用标准库：插件开发 skill 的离线检查器原样带着这个文件（`packs/dev_skill.CHECKER_SOURCES`）。
助手副本的 `flow_state.need_of` 是同一张映射表的另一份（工作区脚本不能引用内核），两边由测试对齐。
"""

from __future__ import annotations

REQUIRED = "required"
OPTIONAL = "optional"
WHEN = "when"
# FL-01、S-18 的分组：必需 → 必经步骤，可选 → 可选步骤，条件 → 有需要时（「发布前检查」不再有节点，分组为兼容留着）
SECTION_OF_NEED = {REQUIRED: "required", OPTIONAL: "optional", WHEN: "on_demand"}
# 分段遗留，装配时不起作用，静态检查给提示
LEGACY_DROPPED = ("per_slice", "steps", "slice_steps", "node_titles", "blocks_in")
_MAINLINE_PLACEMENTS = ("spine", "step", "gate")
_OPTIONAL_PLACEMENTS = ("branch", "on_demand")
# 旧写法能写的值：写了别的装配失败
PLACEMENTS = (*_MAINLINE_PLACEMENTS, *_OPTIONAL_PLACEMENTS)
_REQUIRED_SECTIONS = ("required", "pre_publish")
_OPTIONAL_SECTIONS = ("optional", "on_demand")
SECTIONS = (*_REQUIRED_SECTIONS, *_OPTIONAL_SECTIONS)


def valid(value) -> bool:
    """need 的取值合法：required、optional，或只有一个非空 when 的映射。"""
    if value in (REQUIRED, OPTIONAL):
        return True
    return (isinstance(value, dict) and set(value) == {WHEN} and isinstance(value[WHEN], str)
            and bool(value[WHEN].strip()))


def legacy_need(node: dict) -> tuple:
    """旧写法 → (need, 是不是 on_demand)：映射表自上而下取第一条命中的。"""
    condition = node.get("visible_if")
    if isinstance(condition, str) and condition.strip():
        return {WHEN: condition.strip()}, False
    if node.get("placement") == "on_demand" or node.get("section") == "on_demand":
        return OPTIONAL, True
    if (node.get("optional") is True or node.get("section") == "optional" or node.get("placement") == "branch"
            or node.get("progress") is False):
        return OPTIONAL, False
    return REQUIRED, False


def need_of(node: dict):
    """节点的 need：写了合法的 need 就用它，否则按旧写法折算。"""
    declared = node.get("need")
    if valid(declared):
        return {WHEN: declared[WHEN].strip()} if isinstance(declared, dict) else declared
    return legacy_need(node)[0]


def need_kind(node: dict) -> str:
    """required、optional 或 when。"""
    need = need_of(node)
    return WHEN if isinstance(need, dict) else need


def when_of(node: dict) -> str | None:
    """条件节点的条件名；不是条件节点时 None。"""
    need = need_of(node)
    return need[WHEN] if isinstance(need, dict) else None


def need_problems(node: dict) -> list[str]:
    """need 写错，或旧写法的 placement、section 写了不认识的值。"""
    node_id = node.get("id") or "（空）"
    problems = []
    if "need" in node and not valid(node["need"]):
        problems.append(f"节点 {node_id} 的 need 只能是 required、optional 或 {{when: <条件>}}：{node['need']}")
    if node.get("placement", "spine") not in PLACEMENTS:
        problems.append(f"节点 {node_id} 的挂法 {node.get('placement')} 不认识（改写成 need）")
    if node.get("section", "required") not in SECTIONS:
        problems.append(f"节点 {node_id} 的分组 {node.get('section')} 不认识（改写成 need）")
    return problems


def primary_action(node: dict) -> dict | None:
    """主命令：actions 里第一个带 command 或 page 的动作。"""
    return next((action for action in node.get("actions") or []
                 if isinstance(action, dict) and (action.get("command") or action.get("page"))), None)


def primary_command(node: dict) -> str:
    """主命令是发到对话的命令（以 / 开头）时返回整条命令（去掉首尾空白），否则空串（页面动作、没有动作）。"""
    action = primary_action(node) or {}
    command = str(action.get("command") or "").strip()
    return command if command.startswith("/") else ""


def _entry_command(node: dict) -> str:
    entry = node.get("entry")
    return str(entry.get("command") or "").strip() if isinstance(entry, dict) else ""


def normalize_node(node: dict, *, trusted: bool = True) -> dict:
    """装配时的节点：补上 need，按 need 补齐旧字段，补主命令与 entry；返回新的映射，做几遍结果都一样。

    trusted：节点来自官方包（镜像内置入库）或 core。第三方包的节点没写 autobuild 时默认不进自主构建：
    自主构建无人值守，第三方包的输入不可信。可选节点同理（「包括可选步骤」打开时也不做）。"""
    out = dict(node)
    declared = out.get("need")
    if declared is not None and not valid(declared):
        return out  # 写错了：组装检查会报，这里不猜
    need = need_of(out)
    if not trusted or (declared is None and legacy_need(out)[1]):  # 旧写法的按需节点同样不进自主构建
        out.setdefault("autobuild", False)
    out["need"] = need
    kind = need_kind(out)
    if kind == REQUIRED:
        _keep_or(out, "placement", _MAINLINE_PLACEMENTS, "spine")
        _keep_or(out, "section", _REQUIRED_SECTIONS, "required")
        for key in ("optional", "progress", "visible_if", "preempt"):
            out.pop(key, None)
    elif kind == OPTIONAL:
        _keep_or(out, "placement", _OPTIONAL_PLACEMENTS, "branch")
        _keep_or(out, "section", _OPTIONAL_SECTIONS, "optional")
        out.update(optional=True, progress=False)
        out.pop("visible_if", None)
        out.pop("preempt", None)
    else:
        # 条件节点：旧脚本按 visible_if 定出不出现、按 preempt 先推它（新脚本按 need.when 先推）
        _keep_or(out, "placement", PLACEMENTS, "spine")
        _keep_or(out, "section", SECTIONS, "required")
        out.pop("optional", None)
        out.update(visible_if=need[WHEN], progress=False, preempt=out.get("preempt") or True)
    entry = _entry_command(out)
    if primary_action(out) is None and entry.startswith("/"):
        # 只写了 entry 的老节点：入口当主命令
        out["actions"] = [{"label": str(out.get("title") or entry), "command": entry}, *(out.get("actions") or [])]
    if not entry and primary_command(out):
        out["entry"] = {"command": primary_command(out)}
    return out


def _keep_or(node: dict, key: str, allowed: tuple, default: str) -> None:
    if node.get(key) not in allowed:
        node[key] = default
