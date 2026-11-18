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


# ---- 节点什么时候要做（need）----
#
# 节点只用 `need` 表达：required、optional、{when: <内核条件>}。装配时内核已经补上（旧写法折算、旧字段补齐）；
# 这里再按同一张映射表兜一次底：直接读工具箱源目录里没组装过的 flow.yaml、老装配格式时也认得出。
# 映射表和内核 services/flow/need.py 的同一份，测试对齐两边。


def need_of(node: dict):
    """节点的 need：写了合法的 need 就用它，否则按旧写法折算（自上而下取第一条命中的）。"""
    need = node.get('need')
    if need in ('required', 'optional'):
        return need
    if isinstance(need, dict) and set(need) == {'when'} and isinstance(need['when'], str) and need['when'].strip():
        return {'when': need['when'].strip()}
    condition = node.get('visible_if')
    if isinstance(condition, str) and condition.strip():
        return {'when': condition.strip()}
    if node.get('placement') == 'on_demand' or node.get('section') == 'on_demand':
        return 'optional'
    if (node.get('optional') is True or node.get('section') == 'optional' or node.get('placement') == 'branch'
            or node.get('progress') is False):
        return 'optional'
    return 'required'


def need_kind(node: dict) -> str:
    """required、optional 或 when。"""
    need = need_of(node)
    return 'when' if isinstance(need, dict) else need


def primary_command(node: dict) -> str:
    """主命令：actions 里第一个带 command 或 page 的动作；是发到对话的命令时返回整条命令，否则空串。"""
    action = next((item for item in node.get('actions') or []
                   if isinstance(item, dict) and (item.get('command') or item.get('page'))), {})
    command = str(action.get('command') or '').strip()
    return command if command.startswith('/') else ''


def node_hits(flow: dict, root: Path, probes: dict | None = None) -> dict:
    """节点 ID → 完成判定是否满足（done 为空的按需节点恒为 False）。"""
    probes = probes if probes is not None else evaluate(flow, root)
    return {
        node['id']: bool(_as_list(node.get('done'))) and _holds(node.get('done'), probes)
        for node in flow.get('nodes') or []
    }


def progress_nodes(flow: dict) -> list:
    """计入进度（x/N）的节点：必需节点（可选、条件节点不算）。后端另外去掉可补做的（overlay.progress_ids）。"""
    return [node for node in flow.get('nodes') or [] if need_kind(node) == 'required']


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


# ---- 统一下一步（详设 6.10；方案 D16；节点模型重构 2b 推荐五级）----
#
# 后端（FL-01/FL-02、宿主注入）与 state_helper suggest 共用这一份：输入是节点状态（后端按执行记录、变更集、
# 指纹算好，写进 `.onto/run/flow-state.json`）与上下文，输出契约 7.8 的 Suggestions。
#
# 从上往下判，命中就停：
# 1. 对话状态：等回答（含目标问题）、出错、被打断、没收尾、合入冲突、命令等你打字回复；自主构建在跑时给运行控制；
# 2. 条件已成立、没处理完的节点，以及过期且没交给下游核对的必需节点（ADR-0090），取流程里最靠前的一个；
#    范围限定生效时（confine_active）不出过期项；过期项的说明行写哪类变了；
# 3. 第一个没做完的必需节点；它在跑或待确认时建议条为空；
# 4. 必需节点都做完时：没做也没跳过的可选节点、过期的可选节点、可补做的必需节点，取流程里最靠前的一个；
# 5. 不显示。
# 第 2～4 级一次只推一个节点：主推是节点主命令（叫节点名），次选是同一节点的变体，最多 2 个。节点已有产出、还没完成时，
# 核验动作（verify）排第一。`done` 为空的节点判断不了做没做完，第 3、4 级不推（条件节点做没做完由条件说了算）。

SUGGESTIONS_MAX = 3
# 建议条判定的版本（9 起按五级推荐判，一次只推一个节点，上下文多 produced）：后端只用支持这一版的 flow_state
SUGGEST_RULES = 9

# 条件 → 建议条上的说明（条件节点出现的原因）
CONDITION_REASONS = {'materials_added': '上传了新资料'}
_OPEN = ('running', 'pending_confirm')


def _item(sid: str, label: str, action: dict, *, reason: str, priority: int, primary: bool = False,
          disabled: bool = False, icon: str | None = None) -> dict:
    item = {'id': sid, 'label': label, 'primary': primary, 'disabled': disabled, 'reason': reason,
            'priority': priority, 'action': action}
    if icon:
        item['icon'] = icon
    return item


def _send(command: str) -> dict:
    """发到对话：斜杠命令带 command（走 S-20），别的文字（「核验资料分析报告」那句指令）只给 text（当普通消息发）。"""
    if not command.strip().startswith('/'):
        return {'type': 'send', 'text': command.strip()}
    return {'type': 'send', 'text': command, 'command': command.split()[0]}


def _action(action: dict) -> dict:
    """节点动作 → 建议的动作：命令发到对话，页面动作（如「发布」）跳页。"""
    if action.get('page'):
        return {'type': 'navigate', 'page': action['page']}
    return _send(action['command'])


def action_shown(action: dict, probes: dict | None) -> bool:
    """动作的 `when`（探针名列表，`!名字` 取反）都成立才出现，如「追加资料分析」要已有资料分析报告。
    不知道结果的探针（没给 probes，或工作区里算不了的内核探针）不过滤。"""
    for name in action.get('when') or []:
        probe = str(name).lstrip('!')
        if probes is None or probe not in probes:
            continue
        if bool(probes[probe]) == str(name).startswith('!'):
            return False
    return True


def _required_on_mainline(node: dict) -> bool:
    """主线上的必做节点：`need: required`。"""
    return need_kind(node) == 'required'


def _covers(patterns: list, wanted: list) -> bool:
    """写入范围 patterns 盖得住 wanted 里每一个产出（ontology/* 盖得住 ontology/operation/*）。"""
    return bool(wanted) and all(any(fnmatch.fnmatch(item, pattern) for pattern in patterns) for item in wanted)


def _outputs(node: dict) -> list:
    declared = [*(node.get('produces') or []), *(node.get('writes') or [])]
    return [str(item.get('path') if isinstance(item, dict) else item) for item in declared]


# 可选的步骤（规则补抽、指标）过期时，后面的核对节点只要还会跑（没跳过）就交给它：可选步骤不是正确性必需的，
# 核对会查出缺的；建模还没做完（核对节点 not_ready）时也不插到主线前面
LENIENT_STATES = ('confirmed', 'stale', 'ready', 'running', 'pending_confirm', 'not_ready', 'blocked')


# 盯着条目内容的类别也盖住同一目录只看清单的类别：条目增删改名时内容指纹一定变（ADR-0102）
SET_OF = {'object': 'object_set', 'operation': 'operation_set'}


def _watched(kinds) -> set:
    kinds = {str(kind) for kind in kinds or []}
    return kinds | {SET_OF[kind] for kind in kinds if kind in SET_OF}


def _is_checker(node: dict) -> bool:
    """能接手的核对节点盯着条目内容（校验）。盯清单（object_set、operation_set）的是重新提取类节点（ADR-0102）：
    提取操作盯着资料分析、也能写对象，但资料变了要重抽对象它补不出来，不算核对。"""
    return not {str(kind) for kind in node.get('stale_on') or []} & set(SET_OF.values())


def _checked_downstream(node: dict, stale_on: list, nodes: list, state_of: dict, states=('confirmed',)) -> bool:
    """过期的 node 后面有已确认的必经节点 M 能核对并修复同一批改动：M.stale_on 盖住
    这些类别、M 的写入范围盖住 node 的产出。M 已确认说明它是在这批改动之后跑的、核对过了（典型：校验自己的修复改了对象），
    上游不再推刷新。M 没跑过或也过期时照旧推上游刷新——改动可能要补提取，M 核对不出来（ADR-0090）。
    M 要是核对节点（_is_checker）：只盯清单的重新提取类节点不接手（ADR-0102）。"""
    if not stale_on:
        return False
    after = False
    for other in nodes:
        if other is node:
            after = True
            continue
        if not after or state_of.get(other['id']) not in states or not _required_on_mainline(other) \
                or not _is_checker(other):
            continue
        if set(stale_on) <= _watched(other.get('stale_on')) and _covers(_outputs(other), _outputs(node)):
            return True
    return False


def handed_down(node: dict, nodes: list, state_of: dict, slice_stale: list = ()) -> bool:
    """过期的节点已经交给后面的核对节点（建议条不推它）时返回 True：可选的只要核对节点还会跑就算，必经的要核对节点
    已确认。待处理（服务端）用同一个判定，不列建议条不推的刷新——两边口径一致（ADR-0090）。

    slice_stale 是按段节点时的参数，签名留着让旧调用方照样能调，不再使用。"""
    states = LENIENT_STATES if need_kind(node) == 'optional' else ('confirmed',)
    return _checked_downstream(node, list(node.get('stale_on') or []), nodes, state_of, states)


def stale_reason(cause: str) -> str:
    """过期节点那一条的说明：「对象有变化」（cause 是确认之后变了的那几类）；说不清哪类变了时只说前面变了。"""
    return f"{cause}有变化" if cause else '前面的内容有变化'


# ---- 节点已有产出（核验排第一用）----


def _has_output(node: dict, root: Path) -> bool:
    """节点 produces 声明的路径下有文件（隐藏文件、index.md 不算）。"""
    for item in node.get('produces') or []:
        pattern = str(item.get('path') if isinstance(item, dict) else item).strip('/')
        parts = pattern.split('/')
        fixed = []
        for part in parts:
            if any(ch in part for ch in '*?['):
                break
            fixed.append(part)
        base = root.joinpath(*fixed) if fixed else root
        if len(fixed) == len(parts):
            if base.is_file() and _visible(base):
                return True
            continue
        if not base.is_dir():
            continue
        for path in base.rglob('*'):
            if not path.is_file() or not _visible(path) or any(
                    part.startswith('.') for part in path.relative_to(base).parts):
                continue
            if fnmatch.fnmatch(path.relative_to(root).as_posix(), pattern):
                return True
    return False


def produced(flow: dict, root: Path) -> list:
    """带核验动作（`verify: true`）的节点里，工作区已经有产出的：给这些节点推荐时没完成就先核验（上下文 produced）。
    只看带核验动作的节点，不扫整个工作区。"""
    root = Path(root)
    return [node['id'] for node in flow.get('nodes') or []
            if any(isinstance(action, dict) and action.get('verify') for action in node.get('actions') or [])
            and _has_output(node, root)]


# ---- 第 1 级：对话状态 ----


def _session_rules(context: dict) -> list:
    """第 1 级，按顺序 (mode, 条目, 要不要说明)：等你回答（去回答、回答目标问题）、这一轮出错、刚被打断、没收尾、合入冲突。
    命令等你打字回复、只有目标问题没答的两种在 suggest 里接着判。"""
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
        answers = [_item('answer', label, action, reason='有等你回答的问题', priority=1, icon='hand')]
        answers += _goal_items(context)
        rules.append(('waiting_decision', answers, False))
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
    changeset = context.get('changeset')
    if changeset and changeset.get('state') == 'conflict':
        # 一轮结束就合入，只有和你的修改冲突时停下来等你选（待处理）
        rules.append(('normal', [_item('resolve', '处理冲突', {'type': 'navigate', 'page': 'inbox'},
                                       reason='这一轮和你的修改有冲突', priority=1, icon='merge')], False))
    return rules


def _goal_items(context: dict) -> list:
    """资料和建模目标对不上的那个问题没答：去回答（F8 起没回答按原目标建，不拦着别的操作）。"""
    if not (context.get('blueprint') or {}).get('goal_pending'):
        return []
    return [_item('answer_goal', '回答目标问题',
                  {'type': 'navigate', 'page': 'wb-blueprint', 'params': {'dialog': 'blueprint'}},
                  reason='资料和建模目标对不上，先定以哪个为准', priority=1, icon='target')]


def _continue_reason(interrupted: dict) -> str:
    """「继续「X」」那一行说明：能续跑说从哪里接着做；能力升级或输入改过说要按新版从头做。"""
    files = int(interrupted.get('saved_files') or 0)
    kept = f"已保存的 {files} 处修改保留" if files else ''
    if interrupted.get('mode') == 'rerun':
        head = '能力已升级' if interrupted.get('why') == 'node_upgraded' else '输入改过了'
        return '，'.join(part for part in (head, '按新版从头做', kept) if part)
    phase = interrupted.get('phase')
    return '，'.join(part for part in (f"从「{phase}」接着做" if phase else '从打断的地方接着做', kept) if part)


# ---- 第 2～4 级：按节点 ----


def _primary(node: dict) -> dict | None:
    """主命令：actions 里第一个带 command 或 page 的动作（同内核 need.primary_action）。"""
    return next((item for item in node.get('actions') or []
                 if isinstance(item, dict) and (item.get('command') or item.get('page'))), None)


def _node_items(node: dict, context: dict, *, level: int, reason: str, state: str | None, stale: bool = False) -> list:
    """一个节点的建议：主推是主命令（叫节点名），次选是变体；已有产出、没完成时核验动作排第一，其余时候不推核验。
    过期的主命令声明了刷新范围时发只查改动条目的那条命令（后端算好放在 stale_scope）。"""
    if state in _OPEN:
        return []
    probes = context.get('probes')
    primary = _primary(node)
    if primary is None:
        return []
    shown = [action for action in node.get('actions') or []
             if isinstance(action, dict) and (action.get('command') or action.get('page')) and action_shown(action, probes)]
    verify = [action for action in shown if action.get('verify') and action is not primary]
    variants = [action for action in shown if not action.get('verify') and action is not primary]
    head = [primary] if any(action is primary for action in shown) else []
    verify_first = (not stale and state != 'confirmed' and node['id'] in (context.get('produced') or ()))
    ordered = [*verify, *head, *variants] if verify_first else [*head, *variants]
    out = []
    for index, action in enumerate(ordered[:SUGGESTIONS_MAX]):
        is_primary = action is primary
        target = _action(action)
        if is_primary and stale and target.get('command'):
            scoped = ((context.get('stale_scope') or {}).get(node['id']) or {}).get('command')
            if isinstance(scoped, str) and scoped.split()[:1] == [target['command']]:
                target = _send(scoped)
        label = str(node.get('title') or action.get('label') or '') if is_primary else str(action.get('label') or '')
        sid = f"refresh:{node['id']}" if is_primary and stale else f"{node['id']}#{index}"
        out.append(_item(sid, label, target, reason=reason, priority=level,
                         icon='refresh' if is_primary and stale else None))
    return out


def _condition_reason(flow: dict, node: dict) -> str:
    """条件节点为什么出现（条目的 reason）：按条件，按触发出现的看包声明的事件。旧写法 preempt 的说法不再用。"""
    need = need_of(node)
    condition = need.get('when') if isinstance(need, dict) else ''
    if condition == 'activated':
        condition = ((flow.get('activations') or {}).get(node.get('pack')) or {}).get('event') or condition
    return CONDITION_REASONS.get(condition, '有新情况要处理')


def _flow_levels(flow: dict, states: dict, context: dict):
    """第 2～4 级：命中就返回 (条目, 要不要说明)；都没命中返回 None。条目为空表示命中了但此刻没有可推的（节点在跑）。"""
    nodes = [node for node in flow.get('nodes') or [] if node.get('id') in states]
    state_of = {node_id: (item or {}).get('state') for node_id, item in states.items()}
    flags_of = {node_id: (item or {}).get('flags') or {} for node_id, item in states.items()}
    causes = context.get('stale_upstream') or {}
    confined = bool(context.get('confine_active'))

    def stale_hit(node: dict) -> tuple:
        reason = stale_reason(causes.get(node['id']) or '')
        return _node_items(node, context, level=2 if _required_on_mainline(node) else 4, reason=reason,
                           state='stale', stale=True), True

    # 第 2 级
    for node in nodes:
        state, kind = state_of.get(node['id']), need_kind(node)
        if kind == 'when' and state not in ('confirmed', 'skipped'):
            return _node_items(node, context, level=2, reason=_condition_reason(flow, node), state=state), False
        if kind == 'required' and state == 'stale' and not confined and not handed_down(node, nodes, state_of):
            return stale_hit(node)
    # 第 3 级
    for node in nodes:
        state = state_of.get(node['id'])
        if (need_kind(node) != 'required' or not node.get('done') or state in ('confirmed', 'stale', 'skipped')
                or flags_of.get(node['id'], {}).get('backfill')):
            continue
        return _node_items(node, context, level=3, reason='下一步', state=state), False
    # 第 4 级
    for node in nodes:
        state, kind = state_of.get(node['id']), need_kind(node)
        if not node.get('done') or _primary(node) is None:
            continue
        if kind == 'optional' and state == 'stale':
            if confined or handed_down(node, nodes, state_of):
                continue
            return stale_hit(node)
        if kind == 'optional' and state in ('ready', 'not_ready', 'blocked'):
            return _node_items(node, context, level=4, reason='可选步骤', state=state), False
        if kind == 'required' and flags_of.get(node['id'], {}).get('backfill') and state in ('ready', 'not_ready'):
            return _node_items(node, context, level=4, reason='可补做', state=state), False
    return None


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
    """统一下一步（详设 6.10；推荐五级见本节开头）：从上往下判，命中就停；1 个主推、最多 2 个次选（契约 7.8 Suggestions）。

    check(action) → bool：这个动作此刻能不能执行（后端给，和接口校验同一份）；不能的条目不出现。第 1 级整条没有能执行的
    往下判；第 2～4 级选中的节点没有能执行的条目时建议条为空（不越过它推后面的）。不给时都算能执行（state_helper 在助手副本里算）。

    context：
    - autobuild：自主构建在跑 → 运行控制；running：这条对话在跑；
    - question：等你回答的问题 {id, kind: question|permission}（旧写法 waiting_decision: True）；
    - blueprint：建模蓝图 {state, where, next, goal_pending}，目标问题没答时给「回答目标问题」；
    - failed：这一轮出错 {reason}；
    - interrupted：刚被打断的执行 {id, label, mode: resume|rerun, why, saved_files, phase}；
    - unfinished：这一轮结束了、命令没收尾的执行 {id, label}；
    - changeset：当前的变更集 {id, state}：合入冲突时主推去待处理选保留哪边；
    - awaiting_reply：这条对话里的命令停下来等你在对话里回复（锁在命令声明的等回复阶段）{id, label}：这时不推别的命令
      （点了会把命令丢在半路），只留一行说明；
    - stale_upstream：过期节点 → 确认之后变了的那几类的说法（「对象、操作」），写进那一条的说明；
    - stale_scope：声明了 refresh_scope 的过期节点 → {since, names, command}：过期的主命令发这条只查改动条目的命令；
    - probes：探针结果 {名字: bool}，按动作的 `when` 筛动作（不给时不筛）；
    - produced：已有产出的节点编号（`produced()`），没完成时核验动作排第一；
    - confine_active：范围限定生效（限定范围的包这一轮进行中），这时不推过期的节点；
    - stage：旧版线性阶段，不再参与判定（写给助手的 flow-state.json 还带着）。
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
            if mode == 'waiting_decision' and items[0]['id'] != 'answer':
                mode = 'normal'
            return _shaped(mode, items, explain)
    waiting = context.get('awaiting_reply')
    if waiting:
        label = (waiting.get('label') if isinstance(waiting, dict) else '') or ''
        return {'mode': 'normal', 'primary': None, 'secondary': [], 'all_commands': True,
                'note': f"「{label}」在等你回复上面的问题" if label else '助手在等你回复上面的问题'}
    goal = [item for item in _goal_items(context) if allowed(item['action'])]
    if goal:
        return _shaped('normal', goal, False)
    hit = _flow_levels(flow, states, context)
    if hit is None:
        return _shaped('normal', [], False)
    items, explain = hit
    return _shaped('normal', [item for item in items if allowed(item['action'])], explain)


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
