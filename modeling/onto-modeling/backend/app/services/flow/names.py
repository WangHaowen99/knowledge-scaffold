"""按钮和命令的名字（节点模型重构 2b）：节点名就是主命令名。导航栏（FL-01 节点动作）、全部命令（S-18）、建议条、
对话气泡、排队条、执行记录的 `command_label` 都按这一份取名，同一个按钮在哪里都叫同一个名字。

`name_for(flow, text)` 看 text 的第一行，按顺序取名，命中就停：

1. 动作文本完全匹配：节点动作（含节点钩子）的命令文本就是这一行，或是这一行开头的几个词（后面跟着参数），取词数最多的
   那个 → 动作的名字；主命令的名字是节点名（`need.normalize_node` 装配时已经对齐，这里按节点名取）；
2. 某个节点的主命令（按命令名比，`/ontology-trace 订单` 认作场景模拟的主命令）→ 节点名；
3. 变体（按命令名比）→ 变体名；
4. 命令表（flow.yaml 的 `commands`）里登记的节点 → 节点名（场景模拟页直接发的 `/ontology-trace-batch`）；
5. 都认不出：调用方给的 default（页面按钮的叫法），再没有就是命令名（去掉斜杠）。

不以 `/` 开头的文本（「核验资料分析报告」那句指令）只认第 1 条的整行相同。
流程要用条件过滤之前的那份（`model.assembled_for`）：没出现的增量更新，手打 `/ontology-update` 也叫「增量更新」。
"""

from __future__ import annotations

from pathlib import Path

from .need import primary_action

STATE_HELPER_PREFIX = "/ontology-"


def _line(text: str) -> str:
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")


def _buttons(flow: dict) -> list[tuple[dict, str, list[str], bool]]:
    """(节点, 名字, 命令词, 是不是主命令)：按流程顺序，节点自己的动作在前、钩子在后。"""
    hooks: dict[str, list[dict]] = {}
    for hook in flow.get("hooks") or []:
        if isinstance(hook, dict):
            hooks.setdefault(str(hook.get("node") or ""), []).append(hook)
    out = []
    for node in flow.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        primary = primary_action(node)
        title = str(node.get("title") or "").strip()
        for action in node.get("actions") or []:
            words = str((action or {}).get("command") or "").split() if isinstance(action, dict) else []
            if not words:
                continue
            is_primary = action is primary
            label = (title if is_primary else "") or str(action.get("label") or "").strip()
            if label:
                out.append((node, label, words, is_primary))
        for hook in hooks.get(str(node.get("id") or ""), []):
            words = str(hook.get("command") or "").split()
            label = str(hook.get("title") or "").strip()
            if words and label:
                out.append((node, label, words, False))
    return out


def lookup(flow: dict, text: str) -> str | None:
    """名字表认得出就返回名字（第 1～4 条），认不出返回 None。"""
    line = _line(text)
    words = line.split()
    if not words:
        return None
    buttons = _buttons(flow or {})
    if not words[0].startswith("/"):
        return next((label for _node, label, tokens, _primary in buttons if tokens == words), None)
    best: tuple[int, str] | None = None
    for _node, label, tokens, _primary in buttons:
        if words[:len(tokens)] == tokens and (best is None or len(tokens) > best[0]):
            best = (len(tokens), label)
    if best is not None:
        return best[1]
    head = words[0]
    for want_primary in (True, False):
        found = next((label for _node, label, tokens, primary in buttons
                      if primary is want_primary and tokens[0] == head), None)
        if found:
            return found
    name = head[len(STATE_HELPER_PREFIX):] if head.startswith(STATE_HELPER_PREFIX) else head[1:]
    spec = ((flow or {}).get("commands") or {}).get(name)
    node_id = spec.get("node") if isinstance(spec, dict) else None
    node = next((item for item in (flow or {}).get("nodes") or [] if isinstance(item, dict) and item.get("id") == node_id),
                None)
    if node is None:
        return None
    return str(node.get("title") or "").strip() or None


def name_for(flow: dict, text: str, default: str | None = None) -> str:
    """按钮或命令的名字：名字表认不出时用 default（页面按钮的叫法），再没有就是命令名（去掉斜杠）。"""
    found = lookup(flow, text)
    if found:
        return found
    if default and str(default).strip():
        return str(default).strip()
    words = _line(text).split()
    return words[0].lstrip("/") if words else ""


def name_at(root: Path, text: str, default: str | None = None) -> str:
    """项目里这条命令的名字：用本项目装配的流程（条件过滤之前的）；不是本体项目工作区时只能给命令名。"""
    from . import model

    try:
        flow = model.assembled_for(Path(root))
    except Exception:  # noqa: BLE001 - 流程读不出来：按认不出处理
        flow = {}
    return name_for(flow, text, default)
