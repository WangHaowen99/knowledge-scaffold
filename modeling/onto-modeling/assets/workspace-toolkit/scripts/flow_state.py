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
import logging
import re
import subprocess
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import research_review  # noqa: E402

FLOW_FILE = Path(__file__).resolve().parent.parent / 'flow.yaml'


# 命令行输出通道：独立 logger，不经根 logger，消息原样（无前缀）
_console = logging.Logger('flow_state', logging.INFO)
_console_handler = logging.StreamHandler()
_console.addHandler(_console_handler)


def _say(text: object = '', *, error: bool = False) -> None:
    """输出一行：结果写标准输出（助手读它），error=True 写标准错误；每次取当前的流，测试替换了也跟着走。"""
    _console_handler.setStream(sys.stderr if error else sys.stdout)
    _console.info(text)


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


# 未处理的条目：列表项开头的 `[ ]`（`- [ ] ❌ …`）。正文里出现的 `[ ]`（如修复说明「补 `[[ ]]` 包裹」）不算
_PENDING_ITEM = re.compile(r'^[ \t]*(?:[-*+]|\d+[.)])[ \t]+\[ \]', re.MULTILINE)


def has_pending(root: Path, spec: dict) -> bool:
    """报告里有没有未处理的 `[ ]` 条目（只看目录第一层，只认列表项开头的）。内核按平台这份算（conditions）。"""
    globs = _as_list(spec.get('glob')) or ['*.md']
    for rel in _as_list(spec.get('pending')):
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if not path.is_file() or not any(fnmatch.fnmatch(path.name, g) for g in globs):
                continue
            try:
                if _PENDING_ITEM.search(path.read_text(encoding='utf-8', errors='ignore')):
                    return True
            except OSError:
                continue
    return False


def _has_tags(root: Path, pattern: str) -> bool:
    """工作区的版本库里有没有符合 pattern 的标签（发布打的 release/*）；不是版本库时 False。"""
    try:
        result = subprocess.run(['git', 'tag', '--list', pattern],  # NOSONAR(S4721) 参数列表、不经 shell，只读查标签
                                cwd=root, capture_output=True, text=True, timeout=10)
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
            result = has_pending(root, spec)
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
# 建议条判定的版本（F7 起按规则表从上往下判、带 check 与 note）：后端只用支持这一版的 flow_state
SUGGEST_RULES = 7


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


def action_shown(action: dict, probes: dict | None) -> bool:
    """动作的 `when`（探针名列表，`!名字` 取反）都成立才出现，如「追加分析」要已有资料分析报告。
    不知道结果的探针（没给 probes，或工作区里算不了的内核探针）不过滤。"""
    for name in action.get('when') or []:
        probe = str(name).lstrip('!')
        if probes is None or probe not in probes:
            continue
        if bool(probes[probe]) == str(name).startswith('!'):
            return False
    return True


def _node_actions(node: dict, *, verify_first: bool = False, probes: dict | None = None) -> list:
    actions = [action for action in node.get('actions') or []
               if (action.get('command') or action.get('page')) and action_shown(action, probes)]
    if verify_first:
        actions = sorted(actions, key=lambda action: not action.get('verify'))
    else:
        # 「核验报告」只在报告挡着后续阶段时给：平时没有要核验的报告（F8 去掉「分段规划」后它会挤进前三个）
        actions = [action for action in actions if not action.get('verify')]
    return actions


def _slice_step(next_step: dict) -> dict:
    """按段的下一步：这一段下一步的命令带上分段（全量模式不带）。"""
    args = {'slice': next_step['slice_id']} if next_step.get('slice_id') and next_step.get('sliced') else {}
    action = {'type': 'send', 'text': '', 'command': next_step['command']}
    if args:
        action['args'] = args
    return action


def _goal_items(flow: dict, nodes: list, states: dict, context: dict) -> list:
    """6. 资料和建模目标对不上的那个问题没答：主推去回答，后面跟主线下一步——不拦着开始建模（F8，没回答按原目标建）。"""
    if not (context.get('blueprint') or {}).get('goal_pending'):
        return []
    answer = _item('answer_goal', '回答目标问题',
                   {'type': 'navigate', 'page': 'wb-blueprint', 'params': {'dialog': 'blueprint'}},
                   reason='资料和建模目标对不上，先定以哪个为准', priority=4, icon='target')
    return [answer, *_mainline_items(flow, nodes, states, context)]


MAINLINE_PLACEMENTS = ('spine', 'step', 'gate')


def _required_on_mainline(node: dict) -> bool:
    """主线上的必做节点：不是可选的，摆在主线上，属于必做区。"""
    return (not node.get('optional') and node.get('placement', 'spine') in MAINLINE_PLACEMENTS
            and node.get('section', 'required') == 'required')


def _covers(patterns: list, wanted: list) -> bool:
    """写入范围 patterns 盖得住 wanted 里每一个产出（ontology/* 盖得住 ontology/operation/*）。"""
    return bool(wanted) and all(any(fnmatch.fnmatch(item, pattern) for pattern in patterns) for item in wanted)


def _outputs(node: dict) -> list:
    declared = [*(node.get('produces') or []), *(node.get('writes') or [])]
    return [str(item.get('path') if isinstance(item, dict) else item) for item in declared]


# 可选的步骤（规则补抽、指标）过期时，后面的核对节点只要还会跑（没跳过）就交给它：可选步骤不是正确性必需的，
# 核对会查出缺的；建模还没做完（核对节点 not_ready）时也不插到主线前面
LENIENT_STATES = ('confirmed', 'stale', 'ready', 'running', 'pending_confirm', 'not_ready', 'blocked')


def _checked_downstream(node: dict, stale_on: list, nodes: list, state_of: dict, states=('confirmed',)) -> bool:
    """过期的 node（或它的某一步，stale_on 是那一步的）后面有已确认的必经节点 M 能核对并修复同一批改动：M.stale_on 盖住
    这些类别、M 的写入范围盖住 node 的产出。M 已确认说明它是在这批改动之后跑的、核对过了（典型：校验自己的修复改了对象），
    上游不再推刷新。M 没跑过或也过期时照旧推上游刷新——改动可能要补提取，M 核对不出来（ADR-0090）。"""
    if not stale_on:
        return False
    after = False
    for other in nodes:
        if other is node:
            after = True
            continue
        if not after or state_of.get(other['id']) not in states or not _required_on_mainline(other):
            continue
        if set(stale_on) <= set(other.get('stale_on') or []) and _covers(_outputs(other), _outputs(node)):
            return True
    return False


def _step_stale_on(node: dict, step_id) -> list:
    return next((list(step.get('stale_on') or []) for step in node.get('steps') or [] if step.get('id') == step_id), [])


def _step_optional(node: dict, step_id) -> bool:
    return any(step.get('optional') for step in node.get('steps') or [] if step.get('id') == step_id)


def _step_handed_down(node: dict, step_id, nodes: list, state_of: dict) -> bool:
    """按段节点过期的一步已交给后面的核对节点：可选的一步只要核对节点还会跑就算，必经的要核对节点已确认。"""
    states = LENIENT_STATES if _step_optional(node, step_id) else ('confirmed',)
    return _checked_downstream(node, _step_stale_on(node, step_id), nodes, state_of, states)


def handed_down(node: dict, nodes: list, state_of: dict, slice_stale: list = ()) -> bool:
    """过期的节点已经全交给后面的核对节点（第 7 条不推刷新）时返回 True。

    按段的节点要过期的每一步都交出去才算。待处理（服务端）用同一个判定，不列建议条不推的刷新——两边口径一致（ADR-0090）。"""
    if node.get('per_slice'):
        steps = list(slice_stale or [])
        return bool(steps) and all(_step_handed_down(node, step.get('step'), nodes, state_of) for step in steps)
    states = LENIENT_STATES if node.get('optional') else ('confirmed',)
    return _checked_downstream(node, list(node.get('stale_on') or []), nodes, state_of, states)


def refresh_label(cause: str, title: str) -> str:
    """过期步骤的按钮：「对象有变化，刷新校验」（cause 是确认之后变了的那几类）；说不清哪类变了时只写「刷新校验」。"""
    return f"{cause}有变化，刷新{title}" if cause else f"刷新{title}"


REFRESH_REASON = '按改动后的内容重新做这一步'


def _stale_items(nodes: list, state_of: dict, context: dict) -> list:
    """7. 过期的步骤：刷新。按段的节点一段一步地列（按钮写清哪一段哪一步），其余节点整个刷新；必经的排在可选的前面。

    可选的（规则补抽、指标这类旁支）过期时，后面的核对节点（校验）还会跑就交给它，不单独推：可选步骤不是
    正确性必需的，校验会查出缺的；单独推还会和建模来回兜圈子——指标补对象的关联让「操作」过期，重抽操作又让
    指标过期（ADR-0090）。

    范围限定生效时（限定范围的包这一轮进行中，ADR-0085）不出：改动的传播由那一轮的核对节点处理，整段重新提取
    会去写范围外的条目、被守卫拦下。"""
    if context.get('confine_active'):
        return []
    required, optional = [], []
    upstream = context.get('stale_upstream') or {}
    for node in nodes:
        if state_of.get(node['id']) != 'stale':
            continue
        if node.get('per_slice'):
            for step in context.get('slice_stale') or []:
                lenient = _step_optional(node, step.get('step'))
                if _step_handed_down(node, step.get('step'), nodes, state_of):
                    continue
                where = f"「{step['slice_name']} · {step['step_label']}」" if step.get('sliced') else step['step_label']
                (optional if lenient else required).append(
                    _item(f"refresh:{node['id']}:{step.get('slice_id')}:{step['step']}",
                          refresh_label(step.get('upstream') or '', where), _slice_step(step),
                          reason=REFRESH_REASON, priority=2, icon='refresh'))
            continue
        lenient = bool(node.get('optional'))
        if handed_down(node, nodes, state_of):
            continue
        entry = (node.get('entry') or {}).get('command') or next(
            (action['command'] for action in _node_actions(node, probes=context.get('probes')) if action.get('command')), None)
        scoped = ((context.get('stale_scope') or {}).get(node['id']) or {}).get('command')
        if entry and isinstance(scoped, str) and scoped.split()[:1] == entry.split()[:1]:
            entry = scoped  # 声明了刷新范围：只查确认之后改动的条目（后端算好的同一条命令）
        if entry:
            (optional if lenient else required).append(
                _item(f"refresh:{node['id']}", refresh_label(upstream.get(node['id']) or '', node['title']),
                      _send(entry), reason=REFRESH_REASON, priority=2, icon='refresh'))
    return required + optional


def _check_items(nodes: list, state_of: dict, context: dict) -> list:
    """8. 报告里还有待处理的问题、挡住发布的关卡没通过：继续校验或关卡的操作。"""
    out = []
    if (context.get('stage') or '') == '校验待审阅' or context.get('report_pending'):
        out.append(_item('core.validate', '继续校验', _send('/ontology-validate'),
                         reason='报告里还有待处理的问题', priority=3))
    for node in nodes:
        if node.get('placement') == 'gate' and state_of.get(node['id']) in ('ready', 'not_ready') and node.get('blocks'):
            for action in _node_actions(node, probes=context.get('probes'))[:1]:
                out.append(_item(f"gate:{node['id']}", action['label'], _action(action),
                                 reason='挡住发布的关卡还没通过', priority=3))
    return out


def _mainline_items(flow: dict, nodes: list, states: dict, context: dict) -> list:
    """9、10. 主线下一步：第一个没完成的必经步骤（按段的节点给这一段的下一步，带段名）；必经都做完时整体视图，
    再加扫描脚本候选、补做越过插入位置的插件步骤。"""
    state_of = {node_id: (item or {}).get('state') for node_id, item in states.items()}
    flags_of = {node_id: (item or {}).get('flags') or {} for node_id, item in states.items()}
    stage = context.get('stage') or ''
    slice_next = context.get('slice_next')
    def pending_required(node: dict) -> bool:
        return (node.get('section', 'required') == 'required'
                and state_of.get(node['id']) not in ('confirmed', 'stale', 'skipped')
                and not flags_of.get(node['id'], {}).get('backfill'))

    required = [node for node in nodes if pending_required(node)]
    out = []
    if required:
        current = required[0]
        if current.get('per_slice') and slice_next and state_of.get(current['id']) not in ('pending_confirm', 'stale'):
            label = slice_next.get('label') or current['title']
            if slice_next.get('sliced') and slice_next.get('slice_name'):
                label = f"{label}「{slice_next['slice_name']}」"
            out.append(_item(f"{current['id']}:{slice_next.get('slice_id')}:{slice_next.get('step')}", label,
                             _slice_step(slice_next), reason='下一步', priority=4))
        elif state_of.get(current['id']) not in ('running', 'pending_confirm') and not current.get('per_slice'):
            actions = _node_actions(current, verify_first=stage == '资料分析待核验', probes=context.get('probes'))
            for index, action in enumerate(actions[:SUGGESTIONS_MAX]):
                out.append(_item(f"{current['id']}#{index}", action['label'], _action(action),
                                 reason='下一步', priority=4))
        return out
    out.append(_item('overview', '查看整体视图', _send('/ontology-overview'), reason='必经步骤都已完成', priority=5))
    available = flow.get('available_commands')
    if available is None or 'ontology-codegen-scan' in available:
        out.append(_item('codegen-scan', '扫描脚本候选', _send('/ontology-codegen-scan'),
                         reason='可选步骤', priority=5))
    for node in nodes:
        if flags_of.get(node['id'], {}).get('backfill'):
            for action in _node_actions(node, probes=context.get('probes'))[:1]:
                out.append(_item(f"backfill:{node['id']}", f"补做「{node['title']}」", _action(action),
                                 reason='可补做', priority=5))
    return out


def _session_rules(context: dict) -> list:
    """2～4. 会话侧：等你回答的问题、这一轮出错、刚被打断（或没收尾）的那一步。按顺序 (mode, 条目, 要不要说明)。"""
    rules = []
    question = context.get('question')
    if question is None and context.get('waiting_decision'):
        question = {}
    if question is not None:
        action = {'type': 'focus_question'}
        question_id = question.get('id')
        if question_id:
            action['question_id'] = question_id
        label = '去确认' if question.get('kind') == 'permission' else '去回答'
        rules.append(('waiting_decision', [_item('answer', label, action, reason='有等你回答的问题', priority=1,
                                                 icon='hand')], False))
    failed = context.get('failed')
    if failed is not None:
        rules.append(('failed', [_item('retry', '重试这一轮', {'type': 'retry'},
                                       reason=failed.get('reason') or '这一轮出错了', priority=1, icon='refresh')], True))
    interrupted = context.get('interrupted') or {}
    if interrupted.get('id'):
        cont = _item('resume', f"继续「{interrupted.get('label') or '这一步'}」",
                     {'type': 'resume_exec', 'exec_id': interrupted['id']},
                     reason=_continue_reason(interrupted), priority=1, icon='redo')
        abandon = _item('abandon', '放弃这一步', {'type': 'abandon_exec', 'exec_id': interrupted['id']},
                        reason='已经保存的修改不回退', priority=1)
        rules.append(('interrupted', [cont, abandon], True))
    unfinished = context.get('unfinished') or {}
    if unfinished.get('id'):
        # 这一轮结束了，命令锁还在：主推「继续」让助手做完收尾
        cont = _item('continue', f"继续「{unfinished.get('label') or '这一步'}」", {'type': 'send', 'text': '继续'},
                     reason='这一步还没收尾', priority=1, icon='redo')
        abandon = _item('abandon', '放弃这一步', {'type': 'abandon_exec', 'exec_id': unfinished['id']},
                        reason='已经保存的修改不回退', priority=1)
        rules.append(('normal', [cont, abandon], True))
    return rules


def _continue_reason(interrupted: dict) -> str:
    """「继续「X」」那一行说明：能续跑说从哪里接着做；能力升级或输入改过说要按新版从头做。"""
    files = int(interrupted.get('saved_files') or 0)
    kept = f"已保存的 {files} 处修改保留" if files else ''
    if interrupted.get('mode') == 'rerun':
        head = '能力已升级' if interrupted.get('why') == 'node_upgraded' else '输入改过了'
        return '，'.join(part for part in (head, '按新版从头做', kept) if part)
    phase = interrupted.get('phase')
    return '，'.join(part for part in (f"从「{phase}」接着做" if phase else '从打断的地方接着做', kept) if part)


def _flow_rules(flow: dict, states: dict, context: dict) -> list:
    """6～10，按顺序：(规则号, 条目)。"""
    nodes = [node for node in flow.get('nodes') or [] if node.get('id') in states]
    state_of = {node_id: (item or {}).get('state') for node_id, item in states.items()}
    return [
        (6, _goal_items(flow, nodes, states, context)),
        (6.5, _preempt_items(nodes, state_of)),
        (7, _stale_items(nodes, state_of, context)),
        (8, _check_items(nodes, state_of, context)),
        (9, _mainline_items(flow, nodes, states, context)),
    ]


def _preempt_items(nodes: list, state_of: dict) -> list:
    """触发后抢先的步骤（`preempt`，ADR-0091）：排在主线前面，没做完不推别的。`preempt` 是字符串时当说明行。"""
    for node in nodes:
        if node.get('preempt') and state_of.get(node['id']) in ('ready', 'not_ready', 'blocked'):
            reason = node['preempt'] if isinstance(node['preempt'], str) else '下一步'
            return [_item(f"{node['id']}#{index}", action['label'], _action(action), reason=reason, priority=4)
                    for index, action in enumerate(_node_actions(node)[:SUGGESTIONS_MAX])]
    return []


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


def suggest(flow: dict, states: dict, context: dict | None = None, check=None) -> dict:
    """统一下一步（详设 6.10）：从上往下判，命中第一条就停；1 个主推、最多 2 个次选（契约 7.8 Suggestions）。

    1 运行中不显示；2 有等你回答的问题；3 这一轮出错；4 刚被打断（或没收尾）的那一步；5 合入冲突（其余按 6～10
    补齐）；6 建模蓝图草案待确认；6.5 包触发后抢先的步骤（preempt）；7 过期的步骤；8 报告待处理、关卡没过；9 主线下一个必经步骤；10 必经都做完。

    check(action) → bool：这个动作此刻能不能执行（后端给，和接口校验同一份）；不能的条目不出现，整条规则没有
    能执行的条目时往下一条判。不给时都算能执行（state_helper 在助手副本里算）。

    context：
    - autobuild：自主构建在跑 → 运行控制；running：这条对话在跑；
    - question：等你回答的问题 {id, kind: question|permission}（旧写法 waiting_decision: True）；
    - failed：这一轮出错 {reason}；
    - interrupted：刚被打断的执行 {id, label, mode: resume|rerun, why, saved_files, phase}；
    - unfinished：这一轮结束了、命令没收尾的执行 {id, label}；
    - changeset：当前的变更集 {id, state}：合入冲突时主推去待处理选保留哪边；
    - stage：旧版线性阶段（资料分析待核验时核验排第一、校验待审阅时继续校验）；report_pending：报告有待处理项；
    - blueprint：建模蓝图 {state: none|draft|confirmed, where: mainline|changeset, next, goal_pending}，目标问题没答时主推「回答目标问题」（F8 起草案不用确认）；
    - slice_next：按段的节点这一段的下一步 {slice_id, slice_name, step, label, command, sliced}；
    - slice_stale：按段的节点过期的那几段那几步 [{slice_id, slice_name, step, step_label, command, sliced, upstream}]；
    - stale_upstream：其余过期节点 → 上游的说法（「对象、操作」）；
    - stale_scope：声明了 refresh_scope 的过期节点 → {since, names, command}，第 7 条的刷新发这条只查改动条目的命令
      （`/ontology-validate 门店 受理申请 --since <提交>`）；没有这一项的照旧发整条入口命令；
    - probes：探针结果 {名字: bool}，按动作的 `when` 筛动作（不给时不筛）；
    - confine_active：范围限定生效（限定范围的包这一轮进行中），这时第 7 条不出；
    - awaiting_reply：这条对话里的命令停下来等你在对话里回复（锁在命令声明的等回复阶段）{id, label}：这时不推别的命令
      （点了会把命令丢在半路），只留一行说明。
    """
    context = context or {}
    allowed = check or (lambda action: True)
    if context.get('autobuild'):
        controls = [
            _item('pause', '暂停', {'type': 'autobuild', 'op': 'pause'}, reason='自主构建运行中', priority=1, primary=True),
            _item('safe_point', '在安全点停下', {'type': 'autobuild', 'op': 'stop_at_safe_point'},
                  reason='自主构建运行中', priority=1),
            _item('convert', '转为陪伴', {'type': 'autobuild', 'op': 'convert_to_l2'}, reason='自主构建运行中', priority=1),
        ]
        return {'mode': 'autobuild_controls', 'primary': controls[0], 'secondary': controls[1:], 'all_commands': True,
                'note': None}
    if context.get('running'):
        return _shaped('running', [], False)
    for mode, items, explain in _session_rules(context):
        items = [item for item in items if allowed(item['action'])]
        if items:
            return _shaped(mode, items, explain)
    rules = [(rule, [item for item in found if allowed(item['action'])])
             for rule, found in _flow_rules(flow, states, context)]
    changeset = context.get('changeset')
    if changeset and changeset.get('state') == 'conflict':
        # 一轮结束就合入，只有和你的修改冲突时停下来等你选（待处理）
        resolve = _item('resolve', '处理冲突', {'type': 'navigate', 'page': 'inbox'},
                        reason='这一轮和你的修改有冲突', priority=1, icon='merge')
        rest = next((found for _rule, found in rules if found), [])
        return _shaped('normal', [resolve, *rest], False)
    waiting = context.get('awaiting_reply')
    if waiting:
        label = (waiting.get('label') if isinstance(waiting, dict) else '') or ''
        return {'mode': 'normal', 'primary': None, 'secondary': [], 'all_commands': True,
                'note': f"「{label}」在等你回复上面的问题" if label else '助手在等你回复上面的问题'}
    for rule, found in rules:
        if found:
            # 过期那条按钮上已经写了哪类变了，不再另起一行说明；抢先的步骤用包写的 preempt 当说明行
            return _shaped('normal', found, rule == 6.5 and found[0]['reason'] != '下一步')
    return _shaped('normal', [], False)


def _shaped(mode: str, items: list, explain: bool) -> dict:
    """条目 → Suggestions：第一个主推，后面最多 2 个次选；explain 时主推的 reason 作为说明行（note）。"""
    if not items:
        return {'mode': mode, 'primary': None, 'secondary': [], 'all_commands': True, 'note': None}
    primary = {**items[0], 'primary': True}
    secondary = [{**item, 'primary': False} for item in items[1:SUGGESTIONS_MAX]]
    return {'mode': mode, 'primary': primary, 'secondary': secondary, 'all_commands': True,
            'note': primary.get('reason') if explain else None}


def main(argv: list) -> int:
    """调试入口：python3 flow_state.py [项目目录] → 打印阶段、进度与各节点完成情况。"""
    root = Path(argv[1]) if len(argv) > 1 else Path('.')
    flow = load_flow()
    probes = evaluate(flow, root)
    info = progress(flow, root, probes)
    _say(json.dumps({'stage': legacy_stage(flow, root, probes), 'done': info['done'],
                     'total': info['total'], 'nodes': info['hits']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
