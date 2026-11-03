#!/usr/bin/env python3
"""
流程探针 —— 按主流程定义（flow.yaml）计算各节点是否完成、旧版线性阶段、命令前置与下一步建议条目。

主流程只有一份定义：`.claude/flow.yaml`。state_helper.py（detect-stage / check-prereq /
next-step）与 Web 后端（项目进度、GET /flow）共用本文件的实现，不再各写一份阶段表。

装配目录里的 flow.yaml 是组装后的 JSON 写法（也是合法 YAML），这里只用标准库 json 读；
直接指向工具箱源目录时是 YAML 源文件，退回 PyYAML 读。

只用标准库（PyYAML 仅作源文件的退路），不调用模型。
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import research_review  # noqa: E402

FLOW_FILE = Path(__file__).resolve().parent.parent / 'flow.yaml'


def load_flow(path: Path | None = None) -> dict:
    """读主流程定义；装配后的 JSON 写法优先，YAML 源文件用 PyYAML。"""
    text = Path(path or FLOW_FILE).read_text(encoding='utf-8')
    try:
        return json.loads(text)
    except ValueError:
        import yaml  # 只有直接指向工具箱源目录时才会走到这里

        return yaml.safe_load(text) or {}


# ---- 探针 ----


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _visible(path: Path) -> bool:
    return not path.name.startswith('.') and path.name.lower() != 'index.md'


def _has_files(root: Path, spec: dict) -> bool:
    """目录里有没有符合 glob 的条目：隐藏文件与 index.md 不算；recursive 时只认文件。"""
    globs = _as_list(spec.get('glob')) or ['*']
    exclude = _as_list(spec.get('exclude'))
    recursive = bool(spec.get('recursive'))
    for rel in _as_list(spec.get('files')):
        base = root / rel
        if not base.is_dir():
            continue
        entries = base.rglob('*') if recursive else base.iterdir()
        for path in entries:
            if not _visible(path) or (recursive and not path.is_file()):
                continue
            if recursive and any(part.startswith('.') for part in path.relative_to(base).parts):
                continue
            name = path.name
            if any(fnmatch.fnmatch(name, pattern) for pattern in globs) and not any(
                fnmatch.fnmatch(name, pattern) for pattern in exclude
            ):
                return True
    return False


def _has_pending(root: Path, spec: dict) -> bool:
    """报告里有没有未处理的 `[ ]` 条目（只看目录第一层）。"""
    globs = _as_list(spec.get('glob')) or ['*.md']
    for rel in _as_list(spec.get('pending')):
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not path.is_file() or not any(fnmatch.fnmatch(path.name, g) for g in globs):
                continue
            try:
                if '[ ]' in path.read_text(encoding='utf-8', errors='ignore'):
                    return True
            except OSError:
                continue
    return False


def _has_tags(root: Path, pattern: str) -> bool:
    """工作区的版本库里有没有符合 pattern 的标签（发布打的 release/*）；不是版本库时 False。"""
    try:
        result = subprocess.run(['git', 'tag', '--list', pattern], cwd=root, capture_output=True, text=True,
                                timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def evaluate(flow: dict, root: Path) -> dict:
    """全部命名探针 → bool。"""
    root = Path(root)
    probes = flow.get('probes') or {}
    results: dict = {}
    review = None

    def research_status():
        nonlocal review
        if review is None:
            review = research_review.review_status(root)
        return review['status']

    def value(name: str) -> bool:
        if name in results:
            return results[name]
        spec = probes.get(name) or {}
        if 'files' in spec:
            result = _has_files(root, spec)
        elif 'pending' in spec:
            result = _has_pending(root, spec)
        elif 'tags' in spec:
            result = _has_tags(root, str(spec['tags']))
        elif spec.get('research') == 'accepted':
            result = research_review.accepted(root)
        elif spec.get('research') == 'done':
            status = research_status()
            blocking = status in (research_review.PENDING, research_review.FAILED, research_review.UNVERIFIABLE)
            objects = value(spec['objects']) if spec.get('objects') else False
            result = status != research_review.NONE and not (blocking and not objects)
        else:
            result = False
        results[name] = result
        return result

    for name in probes:
        value(name)
    return results


def _holds(conditions, probes: dict) -> bool:
    for condition in _as_list(conditions):
        name = str(condition)
        negate = name.startswith('!')
        if bool(probes.get(name.lstrip('!'))) == negate:
            return False
    return True


def node_hits(flow: dict, root: Path, probes: dict | None = None) -> dict:
    """节点 ID → 完成判定是否满足（done 为空的按需节点恒为 False）。"""
    probes = probes if probes is not None else evaluate(flow, root)
    return {
        node['id']: bool(_as_list(node.get('done'))) and _holds(node.get('done'), probes)
        for node in flow.get('nodes') or []
    }


def progress_nodes(flow: dict) -> list:
    """计入进度（x/N）的节点：按需节点（progress: false）不算。"""
    return [node for node in flow.get('nodes') or [] if node.get('progress', True)]


def progress(flow: dict, root: Path, probes: dict | None = None) -> dict:
    hits = node_hits(flow, root, probes)
    nodes = progress_nodes(flow)
    return {'done': sum(1 for node in nodes if hits[node['id']]), 'total': len(nodes), 'hits': hits}


# ---- 旧版线性阶段（state_helper 兼容层）----


def stage_names(flow: dict) -> list:
    return [stage['name'] for stage in flow.get('stages') or []]


def stage_level(flow: dict, name: str) -> int:
    names = stage_names(flow)
    return names.index(name) if name in names else 0


def legacy_stage(flow: dict, root: Path, probes: dict | None = None) -> str:
    """从高往低找第一个条件全满足的阶段（已到达的最高阶段优先）。"""
    probes = probes if probes is not None else evaluate(flow, root)
    stages = flow.get('stages') or []
    for stage in reversed(stages):
        if _holds(stage.get('when'), probes):
            return stage['name']
    return stages[0]['name'] if stages else ''


def missing_prereqs(flow: dict, command: str, stage: str) -> list:
    """命令前置：每个要求的阶段都要已到达；返回没到达的阶段。"""
    spec = (flow.get('commands') or {}).get(command) or {}
    level = stage_level(flow, stage)
    return [req for req in _as_list(spec.get('requires')) if level < stage_level(flow, req)]


def command_node(flow: dict, command: str) -> str | None:
    """命令（不带斜杠与 ontology- 前缀）所属的节点。"""
    spec = (flow.get('commands') or {}).get(command) or {}
    return spec.get('node')


def next_step_lines(flow: dict, stage: str) -> list:
    """/ontology 在某个阶段给出的「下一步建议」条目，逐行原样输出。"""
    lines = []
    for item in (flow.get('next_steps') or {}).get(stage) or []:
        if 'line' in item:
            lines.append(f"- {item['line']}")
        elif 'after' in item:
            lines.append(str(item['after']))
        elif item.get('command'):
            lines.append(f"- 回复 **{item['reply']}**（{item['command']}）→ {item['text']}")
        else:
            lines.append(f"- 回复 **{item['reply']}** → {item['text']}")
    return lines


# ---- 统一下一步（详设 6.10；方案 D16）----
#
# 后端（FL-01/FL-02、宿主注入）与 state_helper suggest 共用这一份：输入是节点状态（后端按执行记录、变更集、
# 指纹算好，写进 `.onto/run/flow-state.json`）与上下文，输出契约 7.8 的 Suggestions。

SUGGESTIONS_MAX = 3


def _item(sid: str, label: str, action: dict, *, reason: str, priority: int, primary: bool = False,
          disabled: bool = False, icon: str | None = None) -> dict:
    item = {'id': sid, 'label': label, 'primary': primary, 'disabled': disabled, 'reason': reason,
            'priority': priority, 'action': action}
    if icon:
        item['icon'] = icon
    return item


def _send(command: str) -> dict:
    return {'type': 'send', 'text': command, 'command': command.split()[0]}


def _action(action: dict) -> dict:
    """节点动作 → 建议的动作：命令发到对话，页面动作（如「去发布」）跳页。"""
    if action.get('page'):
        return {'type': 'navigate', 'page': action['page']}
    return _send(action['command'])


def _node_actions(node: dict, *, verify_first: bool = False) -> list:
    actions = [action for action in node.get('actions') or [] if action.get('command') or action.get('page')]
    if verify_first:
        actions = sorted(actions, key=lambda action: not action.get('verify'))
    return actions


def _slice_step(next_step: dict) -> dict:
    """按段的下一步：这一段下一步的命令带上分段（全量模式不带）。"""
    args = {'slice': next_step['slice_id']} if next_step.get('slice_id') and next_step.get('sliced') else {}
    action = {'type': 'send', 'text': '', 'command': next_step['command']}
    if args:
        action['args'] = args
    return action


def _blueprint_candidates(context: dict) -> list:
    """蓝图草案等你确认：确认蓝图、调整分段，再加重新划分（还在资料分析那一轮里时换成撤回这一轮）。"""
    blueprint = context.get('blueprint') or {}
    if blueprint.get('goal_pending'):
        # 资料和建模目标对不上的那个问题没答：确认会被拦（blueprint_question_pending），先去回答（F4b）
        confirm = _item('answer_goal', '回答目标问题',
                        {'type': 'navigate', 'page': 'wb-blueprint', 'params': {'dialog': 'blueprint'}},
                        reason='资料和建模目标对不上，先定以哪个为准', priority=4, icon='target')
    else:
        confirm = _item('confirm_blueprint', '确认蓝图', {'type': 'confirm_blueprint', 'next': blueprint.get('next') or 'L2'},
                        reason='建模蓝图等你确认', priority=4, icon='check')
    adjust = _item('adjust_blueprint', '调整分段',
                   {'type': 'navigate', 'page': 'wb-blueprint', 'params': {'dialog': 'blueprint'}},
                   reason='建模蓝图等你确认', priority=4, icon='layers')
    redo = _item('replan', '重新划分', _send('/ontology-research phase=blueprint'), reason='建模蓝图等你确认',
                 priority=4, icon='refresh')
    return [confirm, adjust, redo]


def _candidates(flow: dict, states: dict, context: dict) -> list:
    """按 6.10 的优先级排好的候选（还没分主推、次选）。"""
    nodes = [node for node in flow.get('nodes') or [] if node.get('id') in states]
    state_of = {node_id: (item or {}).get('state') for node_id, item in states.items()}
    stage = context.get('stage') or ''
    out = []
    # 2. 过期的节点：刷新（按段的节点刷新过期的那一段那一步）
    slice_next = context.get('slice_next')
    for node in nodes:
        if state_of.get(node['id']) == 'stale':
            if node.get('per_slice'):
                if slice_next:
                    out.append(_item(f"refresh:{node['id']}", f"刷新「{slice_next.get('slice_name') or node['title']}」",
                                     _slice_step(slice_next), reason='上游改过，这一步要刷新', priority=2,
                                     icon='refresh'))
                continue
            entry = (node.get('entry') or {}).get('command') or next(
                (action['command'] for action in _node_actions(node) if action.get('command')), None)
            if entry:
                out.append(_item(f"refresh:{node['id']}", f"刷新「{node['title']}」", _send(entry),
                                 reason='上游改过，这一步要刷新', priority=2, icon='refresh'))
    # 3. 报告里还有待处理的错误项、挡住发布的关卡没通过：修复
    if stage == '校验待审阅' or context.get('report_pending'):
        out.append(_item('core.validate', '继续校验', _send('/ontology-validate'),
                         reason='报告里还有待处理的问题', priority=3))
    for node in nodes:
        if node.get('placement') == 'gate' and state_of.get(node['id']) in ('ready', 'not_ready') and node.get('blocks'):
            for index, action in enumerate(_node_actions(node)[:1]):
                out.append(_item(f"gate:{node['id']}", action['label'], _action(action),
                                 reason='挡住发布的关卡还没通过', priority=3))
    # 4. 主线下一步：第一个没完成的必经步骤；建模蓝图草案等你确认时先确认蓝图；按段的节点给这一段的下一步；
    # 已经越过插入位置的插件节点（可补做）不抢主线，放到第 5 级
    flags_of = {node_id: (item or {}).get('flags') or {} for node_id, item in states.items()}
    backfill = [node for node in nodes if flags_of.get(node['id'], {}).get('backfill')]
    required = [node for node in nodes if node.get('section', 'required') == 'required'
                and state_of.get(node['id']) not in ('confirmed', 'stale', 'skipped')
                and not flags_of.get(node['id'], {}).get('backfill')]
    blueprint = context.get('blueprint') or {}
    if blueprint.get('state') == 'draft':
        out += _blueprint_candidates(context)
    elif required:
        current = required[0]
        if current.get('per_slice') and slice_next and state_of.get(current['id']) not in ('pending_confirm', 'stale'):
            out.append(_item(f"{current['id']}:{slice_next.get('slice_id')}:{slice_next.get('step')}",
                             slice_next.get('label') or current['title'], _slice_step(slice_next),
                             reason='下一步', priority=4))
        elif state_of.get(current['id']) not in ('running', 'pending_confirm') and not current.get('per_slice'):
            actions = _node_actions(current, verify_first=stage == '资料分析待核验')
            for index, action in enumerate(actions[:SUGGESTIONS_MAX]):
                out.append(_item(f"{current['id']}#{index}", action['label'], _action(action),
                                 reason='下一步', priority=4))
        if blueprint.get('state') == 'none' and state_of.get('core.research') in ('confirmed', 'stale'):
            out.append(_item('plan_slices', '分段规划', _send('/ontology-research phase=blueprint'),
                             reason='可选步骤', priority=5, icon='layers'))
    else:
        out.append(_item('overview', '整体视图', _send('/ontology-overview'), reason='必经步骤都已完成', priority=5))
        available = flow.get('available_commands')
        if available is None or 'ontology-codegen-scan' in available:
            out.append(_item('codegen-scan', '扫描脚本候选', _send('/ontology-codegen-scan'),
                             reason='可选步骤', priority=5))
    for node in backfill:
        for action in _node_actions(node)[:1]:
            out.append(_item(f"backfill:{node['id']}", f"补做「{node['title']}」", _action(action),
                             reason='可补做', priority=5))
    return out


def overlay_pending(flow: dict, states: dict, local_hits: dict, locked: set | None = None) -> dict:
    """助手副本里这一轮还没合入的改动叠到节点状态上（后端与 state_helper 同一份规则）。

    - 副本里满足完成判定、主线上还没做的节点 → 待确认；
    - locked 给出时（state_helper 在副本里跑）：记为进行中、但命令锁已经释放的节点，命令做完了 → 有改动
      待确认、没改动可开始。
    """
    for node in flow.get('nodes') or []:
        item = states.setdefault(node['id'], {'state': 'not_ready', 'flags': {}})
        if item.get('state') == 'running' and locked is not None and node['id'] not in locked:
            item['state'] = 'pending_confirm' if local_hits.get(node['id']) else 'ready'
        elif local_hits.get(node['id']) and item.get('state') in ('ready', 'not_ready', 'blocked'):
            item['state'] = 'pending_confirm'
    return states


def suggest(flow: dict, states: dict, context: dict | None = None) -> dict:
    """统一下一步：1 个主推、最多 2 个次选（契约 7.8 Suggestions）。

    context：
    - interrupted：刚被打断的执行 {id}（没有执行记录时给 {}）→ 打断后的三项；
    - autobuild：自主构建在跑 → 运行控制；
    - waiting_decision：有没回答的决策卡 → 主推禁用「先做上面的决定」；
    - changeset：当前的变更集 {id, state}：合入冲突时主推去待处理选保留哪边（助手每一轮结束自动合入，没有「确认生效」）；
    - stage：旧版线性阶段（资料分析待核验时核验排第一、校验待审阅时继续校验）；report_pending：报告有待处理项；
    - blueprint：建模蓝图 {state: none|draft|confirmed, where: mainline|changeset, next}，草案时主推「确认蓝图」；
    - slice_next：按段的节点这一段的下一步 {slice_id, slice_name, step, label, command, sliced}。
    """
    context = context or {}
    if context.get('autobuild'):
        controls = [
            _item('pause', '暂停', {'type': 'autobuild', 'op': 'pause'}, reason='自主构建运行中', priority=1, primary=True),
            _item('safe_point', '在安全点停下', {'type': 'autobuild', 'op': 'stop_at_safe_point'},
                  reason='自主构建运行中', priority=1),
            _item('convert', '转为陪伴', {'type': 'autobuild', 'op': 'convert_to_l2'}, reason='自主构建运行中', priority=1),
        ]
        return {'mode': 'autobuild_controls', 'primary': controls[0], 'secondary': controls[1:], 'all_commands': True}
    interrupted = context.get('interrupted')
    if interrupted is not None:
        cont = _item('continue', '继续', {'type': 'send', 'text': '继续'}, reason='刚被打断', priority=1)
        redirect = _item('redirect', '换个方向', {'type': 'clear_input'}, reason='刚被打断', priority=1)
        if interrupted.get('id'):
            primary = _item('resume', '从打断处继续', {'type': 'resume_exec', 'exec_id': interrupted['id']},
                            reason='刚被打断', priority=1, primary=True, icon='redo')
            return {'mode': 'interrupted', 'primary': primary, 'secondary': [cont, redirect], 'all_commands': True}
        return {'mode': 'interrupted', 'primary': {**cont, 'primary': True}, 'secondary': [redirect],
                'all_commands': True}
    candidates = _candidates(flow, states, context)
    changeset = context.get('changeset')
    mode = 'normal'
    if changeset and changeset.get('state') == 'conflict':
        # 一轮结束就合入，只有和你的修改冲突时停下来等你选（待处理）
        resolve = _item('resolve', '处理冲突', {'type': 'navigate', 'page': 'inbox'},
                        reason='这一轮和你的修改有冲突', priority=1, icon='merge')
        candidates = [resolve, *candidates[:SUGGESTIONS_MAX - 1]]
    if context.get('waiting_decision'):
        mode = 'waiting_decision'
        waiting = _item('decide', '先做上面的决定', {'type': 'navigate', 'page': 'wb-l2'},
                        reason='有等你回答的问题', priority=1, disabled=True)
        candidates = [waiting, *candidates]
    if not candidates:
        return {'mode': mode, 'primary': None, 'secondary': [], 'all_commands': True}
    primary = {**candidates[0], 'primary': True}
    secondary = [{**item, 'primary': False} for item in candidates[1:SUGGESTIONS_MAX]]
    return {'mode': mode, 'primary': primary, 'secondary': secondary, 'all_commands': True}


def main(argv: list) -> int:
    """调试入口：python3 flow_state.py [项目目录] → 打印阶段、进度与各节点完成情况。"""
    root = Path(argv[1]) if len(argv) > 1 else Path('.')
    flow = load_flow()
    probes = evaluate(flow, root)
    info = progress(flow, root, probes)
    print(json.dumps({'stage': legacy_stage(flow, root, probes), 'done': info['done'],
                      'total': info['total'], 'nodes': info['hits']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
