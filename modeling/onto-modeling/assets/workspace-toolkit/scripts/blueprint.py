#!/usr/bin/env python3
"""
建模蓝图 —— 资料分析产出的分段规划（详设第 9 章建议 2、6.8；契约 BP-01～BP-03、7.8 Blueprint）。

文件 `research/blueprint.json`（JSON，进 git），只由本文件的函数写：
- 资料分析时助手先写一份草稿 JSON，`blueprint.py write <草稿>` 校验、编号后落盘；
- 网页上的调整（BP-02）、确认（BP-03）由后端经契约库调本文件同一组函数；
- 一轮结束时后端把这一段新建的条目记进这一段（`assign`）。

格式（`format: onto-blueprint/1`）：

    version_no / state           草案号与状态（draft | confirmed）；界面显示「草案 v2」或「v2」
    goal_md / goal_anchor(_label) 建模目标与出处
    out_of_scope                 范围外
    slices[]                     数组顺序就是建议顺序：
        id                       s<n>（全量模式只有一段 all「全部」）
        name / core_flow         段名、核心链路
        depends_on               依赖的段（只能依赖排在前面、没排除的段）
        excluded                 排除（不建）
        merged_from              合并来源（只作记录）
        objects / operations / rules   这一段包含的条目（业务名，即条目文件名解码后的名字）
        sources[]                {anchor, label}：这一段依据的资料位置
    terms                        {aligned: [叫法], ambiguous: [{id, variants, chosen}]}
    questions[]                  {id, title, background, options: [{id, label}], answer}
    next_ids                     下一个编号 {slice, term, question}
    updated_at / confirmed_at

全量模式：没有蓝图，或没排除的分段只有一段（小项目、存量项目）——建模节点按整类条目走，不按段收窄。

只用标准库，不调用模型。子命令：

    write <草稿.json>      校验草稿、编号后写 research/blueprint.json（有旧蓝图时草案号加 1）
    full [目标]            写只有一段「全部」的蓝图（全量模式）
    check                  校验现有蓝图，有问题时退出码 1
    show [--json]          打印蓝图（--json 为契约 Blueprint 的形状）
    slices [--json]        按顺序列出没排除的分段
    brief [分段]           一段的说明（建模命令注入用）；不给时取当前执行的分段
    assign <分段> <类型:名称>…   把条目记进这一段（已经在别的段里的不动）
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
import onto_metamodel  # noqa: E402

FORMAT = 'onto-blueprint/1'
BLUEPRINT_REL = 'research/blueprint.json'
EXEC_MARKER = Path('.onto') / 'run' / 'exec.json'
FULL_SLICE_ID = 'all'
FULL_SLICE_NAME = '全部'
MEMBER_KINDS = ('object', 'operation', 'rule')
MEMBER_FIELDS = {'object': 'objects', 'operation': 'operations', 'rule': 'rules'}
NAME_MAX = 40
FLOW_MAX = 80
TEXT_MAX = 2000
LIST_MAX = 200
SLICES_MAX = 50
_SLICE_ID_RE = re.compile(r'^(s[1-9][0-9]{0,3}|all)$')


class BlueprintError(ValueError):
    """蓝图不合法：reason 对应契约 `slice_invalid.details.reason`（或 field 级的原因）。"""

    def __init__(self, reason: str, message: str, **details):
        super().__init__(message)
        self.reason = reason
        self.details = details


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------


def path_of(root: Path) -> Path:
    return Path(root) / BLUEPRINT_REL


def parse(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BlueprintError('format', f'蓝图不是合法的 JSON：{exc}') from exc
    if not isinstance(data, dict) or data.get('format') != FORMAT:
        raise BlueprintError('format', f'蓝图格式不对（要 {FORMAT}）')
    return data


def load(root: Path) -> dict | None:
    """读蓝图；没有时 None，读出来不合法时抛 BlueprintError。符号链接当没有（可以指到工作区外）。"""
    if path_of(root).is_symlink() or path_of(root).parent.is_symlink():
        return None
    try:
        text = path_of(root).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    return parse(text)


_KEY_ORDER = ('format', 'version_no', 'state', 'goal_md', 'goal_anchor', 'goal_anchor_label', 'out_of_scope',
              'slices', 'terms', 'questions', 'next_ids', 'updated_at', 'confirmed_at')
_SLICE_ORDER = ('id', 'name', 'core_flow', 'depends_on', 'excluded', 'merged_from', 'objects', 'operations',
                'rules', 'sources')


def dumps(doc: dict) -> str:
    """固定键序、缩进两格：改一处只动一处，版本历史里看得清。"""
    ordered = {key: doc[key] for key in _KEY_ORDER if key in doc}
    ordered['slices'] = [{key: item[key] for key in _SLICE_ORDER if key in item} for item in doc.get('slices') or []]
    return json.dumps(ordered, ensure_ascii=False, indent=2) + '\n'


def revision(text: str | bytes) -> str:
    data = text.encode('utf-8') if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()[:16]


def save(root: Path, doc: dict) -> str:
    """校验后原子写入；返回写入的文本。"""
    problems = validate(doc)
    if problems:
        raise problems[0]
    text = dumps(doc)
    target = path_of(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f'.{target.name}.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(target)
    return text


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec='seconds')


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def _text(value, limit: int = TEXT_MAX) -> str:
    return str(value or '').strip()[:limit]


def _names(values, limit: int = LIST_MAX) -> list:
    out = []
    for value in values or []:
        name = _text(value.get('name') if isinstance(value, dict) else value, NAME_MAX * 2)
        if name and name not in out:
            out.append(name)
    return out[:limit]


def _sources(values) -> list:
    out = []
    for value in values or []:
        if isinstance(value, dict):
            anchor, label = _text(value.get('anchor'), 400), _text(value.get('label'), NAME_MAX * 2)
        else:
            anchor, label = _text(value, 400), ''
        if anchor and all(item['anchor'] != anchor for item in out):
            out.append({'anchor': anchor, 'label': label})
    return out[:LIST_MAX]


def _option_id(index: int) -> str:
    return chr(ord('A') + index) if index < 26 else f'O{index + 1}'


def _next(doc: dict, kind: str) -> int:
    ids = doc.setdefault('next_ids', {})
    value = int(ids.get(kind) or 1)
    ids[kind] = value + 1
    return value


def empty(goal_md: str = '') -> dict:
    return {
        'format': FORMAT, 'version_no': 1, 'state': 'draft', 'goal_md': _text(goal_md), 'goal_anchor': None,
        'goal_anchor_label': None, 'out_of_scope': [], 'slices': [], 'terms': {'aligned': [], 'ambiguous': []},
        'questions': [], 'next_ids': {'slice': 1, 'term': 1, 'question': 1}, 'updated_at': _now(),
        'confirmed_at': None,
    }


def full(goal_md: str = '', previous: dict | None = None) -> dict:
    """只有一段「全部」的蓝图（全量模式）。"""
    doc = empty(goal_md or (previous or {}).get('goal_md') or '')
    if previous:
        doc['version_no'] = int(previous.get('version_no') or 0) + 1
        doc['next_ids'] = dict(previous.get('next_ids') or doc['next_ids'])
    doc['slices'] = [_slice(FULL_SLICE_ID, FULL_SLICE_NAME)]
    return doc


def _slice(slice_id: str, name: str, **fields) -> dict:
    item = {'id': slice_id, 'name': name, 'core_flow': '', 'depends_on': [], 'excluded': False, 'merged_from': [],
            'objects': [], 'operations': [], 'rules': [], 'sources': []}
    item.update(fields)
    return item


def from_draft(draft: dict, previous: dict | None = None) -> dict:
    """助手写的草稿 → 蓝图：编号、去重、依赖按名字或编号解析；有旧蓝图时草案号加 1，同名分段沿用旧编号。

    草稿字段：goal_md、goal_anchor、goal_anchor_label、out_of_scope、slices[{name, core_flow, depends_on,
    objects, operations, rules, sources}]、terms{aligned, ambiguous[{variants, chosen}]}、
    questions[{title, background, options}]。
    """
    if not isinstance(draft, dict):
        raise BlueprintError('format', '草稿要是一个 JSON 对象')
    doc = empty(draft.get('goal_md') or draft.get('goal') or '')
    if previous:
        doc['version_no'] = int(previous.get('version_no') or 0) + 1
        doc['next_ids'] = {key: int(value) for key, value in (previous.get('next_ids') or {}).items()}
    doc['goal_anchor'] = _text(draft.get('goal_anchor'), 400) or None
    doc['goal_anchor_label'] = _text(draft.get('goal_anchor_label'), NAME_MAX * 2) or None
    doc['out_of_scope'] = _names(draft.get('out_of_scope'))
    old_ids = {item['name']: item['id'] for item in (previous or {}).get('slices') or []}
    raw_slices = [item for item in draft.get('slices') or [] if isinstance(item, dict)][:SLICES_MAX]
    by_ref: dict[str, str] = {}
    for item in raw_slices:
        name = _text(item.get('name'), NAME_MAX * 2)
        if not name:
            raise BlueprintError('name_required', '每一段都要有名字')
        slice_id = old_ids.get(name) or f"s{_next(doc, 'slice')}"
        if any(existing['id'] == slice_id for existing in doc['slices']):
            slice_id = f"s{_next(doc, 'slice')}"
        for ref in (name, _text(item.get('id'), NAME_MAX), slice_id):
            if ref:
                by_ref[ref] = slice_id
        doc['slices'].append(_slice(
            slice_id, name, core_flow=_text(item.get('core_flow'), FLOW_MAX),
            excluded=bool(item.get('excluded')),
            objects=_names(item.get('objects')), operations=_names(item.get('operations')),
            rules=_names(item.get('rules')), sources=_sources(item.get('sources')),
            depends_on=[str(ref) for ref in item.get('depends_on') or []]))
    for item in doc['slices']:
        resolved = []
        for ref in item['depends_on']:
            target = by_ref.get(_text(ref, NAME_MAX * 2))
            if target is None:
                raise BlueprintError('unknown_dependency', f'「{item["name"]}」依赖的分段「{ref}」不在草稿里',
                                     slice_id=item['id'])
            if target not in resolved:
                resolved.append(target)
        item['depends_on'] = resolved
    if len(doc['slices']) == 1 and not previous:
        # 只有一段就是全量模式
        doc['slices'][0]['id'] = FULL_SLICE_ID
        doc['next_ids']['slice'] = 1
    terms = draft.get('terms') or {}
    doc['terms']['aligned'] = _names(terms.get('aligned'))
    for item in terms.get('ambiguous') or []:
        variants = _names((item or {}).get('variants') if isinstance(item, dict) else item)
        if len(variants) < 2:
            continue
        chosen = _text((item or {}).get('chosen') if isinstance(item, dict) else '', NAME_MAX * 2) or None
        doc['terms']['ambiguous'].append({'id': f"t{_next(doc, 'term')}", 'variants': variants,
                                          'chosen': chosen if chosen in variants else None})
    for item in draft.get('questions') or []:
        if not isinstance(item, dict) or not _text(item.get('title')):
            continue
        options = []
        for index, option in enumerate(item.get('options') or []):
            label = _text(option.get('label') if isinstance(option, dict) else option, 200)
            if label:
                options.append({'id': _option_id(len(options)), 'label': label})
        doc['questions'].append({'id': f"q{_next(doc, 'question')}", 'title': _text(item['title'], 200),
                                 'background': _text(item.get('background'), 400), 'options': options[:8],
                                 'answer': None})
    problems = validate(doc)
    if problems:
        raise problems[0]
    return doc


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def live_slices(doc: dict | None) -> list:
    return [item for item in (doc or {}).get('slices') or [] if not item.get('excluded')]


def mode(doc: dict | None) -> str:
    """full：没有蓝图或只有一段；sliced：按段走。"""
    return 'sliced' if len(live_slices(doc)) > 1 else 'full'


def validate(doc: dict) -> list:
    """全部问题（BlueprintError 列表）；空列表表示合法。"""
    problems = []

    def bad(reason: str, message: str, **details):
        problems.append(BlueprintError(reason, message, **details))

    if doc.get('format') != FORMAT:
        bad('format', f'蓝图格式不对（要 {FORMAT}）')
    if doc.get('state') not in ('draft', 'confirmed'):
        bad('format', '蓝图状态只能是 draft 或 confirmed')
    slices = doc.get('slices') or []
    if len(slices) > SLICES_MAX:
        bad('too_many', f'分段不能超过 {SLICES_MAX} 段')
    if not live_slices(doc):
        bad('empty', '至少要留一段')
    ids, names = [], []
    for index, item in enumerate(slices):
        slice_id = item.get('id')
        if not isinstance(slice_id, str) or not _SLICE_ID_RE.match(slice_id):
            bad('format', f'分段编号 {slice_id} 不合法', slice_id=slice_id)
        if slice_id in ids:
            bad('duplicate_id', f'分段编号 {slice_id} 重复', slice_id=slice_id)
        ids.append(slice_id)
        name = item.get('name')
        if not isinstance(name, str) or not name.strip():
            bad('name_required', '每一段都要有名字', slice_id=slice_id)
        elif len(name) > NAME_MAX:
            bad('name_too_long', f'段名不能超过 {NAME_MAX} 个字', slice_id=slice_id)
        elif not item.get('excluded'):
            if name in names:
                bad('duplicate_name', f'段名「{name}」重复', slice_id=slice_id)
            names.append(name)
        for field in MEMBER_FIELDS.values():
            for member in item.get(field) or []:
                if not isinstance(member, str) or onto_metamodel.key_problem(member):
                    bad('member_invalid', f'「{item.get("name")}」里的条目名「{member}」不合法', slice_id=slice_id)
    position = {item.get('id'): index for index, item in enumerate(slices)}
    excluded = {item.get('id') for item in slices if item.get('excluded')}
    for index, item in enumerate(slices):
        if item.get('excluded'):
            continue
        for dep in item.get('depends_on') or []:
            if dep == item.get('id'):
                bad('dependency_cycle', f'「{item.get("name")}」不能依赖自己', slice_id=item.get('id'))
            elif dep not in position:
                bad('unknown_dependency', f'「{item.get("name")}」依赖的分段不存在', slice_id=item.get('id'))
            elif dep in excluded:
                bad('dependency_excluded', f'「{item.get("name")}」依赖的分段已排除', slice_id=item.get('id'))
            elif position[dep] > index:
                bad('dependency_order', f'「{item.get("name")}」依赖的分段排在它后面', slice_id=item.get('id'))
    for term in (doc.get('terms') or {}).get('ambiguous') or []:
        chosen = term.get('chosen')
        if chosen is not None and (not isinstance(chosen, str) or not chosen.strip() or len(chosen) > NAME_MAX):
            bad('term_invalid', '统一叫法不合法', term_id=term.get('id'))
    return problems


# ---------------------------------------------------------------------------
# 调整（BP-02）与确认（BP-03）
# ---------------------------------------------------------------------------


def _input_slices(value) -> list:
    if not isinstance(value, list) or not value:
        raise BlueprintError('format', 'slices 要是非空的分段列表')
    out = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'].strip():
            raise BlueprintError('format', '每一段都要有 id')
        name = item.get('name')
        if not isinstance(name, str):
            raise BlueprintError('name_required', '每一段都要有名字', slice_id=item['id'])
        deps = item.get('depends_on') or []
        merged = item.get('merged_from') or []
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            raise BlueprintError('format', 'depends_on 要是分段编号列表', slice_id=item['id'])
        if not isinstance(merged, list) or not all(isinstance(ref, str) for ref in merged):
            raise BlueprintError('format', 'merged_from 要是分段编号列表', slice_id=item['id'])
        out.append({'id': item['id'].strip(), 'name': name.strip(), 'core_flow': _text(item.get('core_flow'), FLOW_MAX),
                    'depends_on': list(dict.fromkeys(deps)), 'excluded': bool(item.get('excluded')),
                    'merged_from': list(dict.fromkeys(merged))})
    return out


def apply_slices(doc: dict, inputs: list) -> dict:
    """完整分段列表（契约 BlueprintSliceInput[]）：合并、排除、改名、调序、新增都表达成这份列表。

    - 已有编号：改名、核心链路、依赖、排除，条目原样保留；
    - 新编号带 merged_from：合并这几段（至少 2 段），条目、出处取并集，被合并的段从列表里去掉；
    - 新编号不带 merged_from：新增一段空的；
    - 新编号一律换成 s<n>，别的段对它（以及对被合并的段）的依赖跟着换；
    - 原有的段既不在列表里、也没被合并：拒绝（排除用 excluded，不删除）。
    """
    doc = copy.deepcopy(doc)
    items = _input_slices(inputs)
    old = {item['id']: item for item in doc.get('slices') or []}
    seen = [item['id'] for item in items]
    if len(seen) != len(set(seen)):
        raise BlueprintError('duplicate_id', '分段编号重复')
    remap: dict[str, str] = {}
    merged_away: set[str] = set()
    for item in items:
        if item['id'] in old:
            if item['merged_from'] and item['merged_from'] != old[item['id']].get('merged_from'):
                raise BlueprintError('merge_invalid', '合并要用新的分段编号', slice_id=item['id'])
            continue
        if item['merged_from']:
            if len(item['merged_from']) < 2:
                raise BlueprintError('merge_too_few', '合并至少要 2 段', slice_id=item['id'])
            unknown = [ref for ref in item['merged_from'] if ref not in old]
            if unknown:
                raise BlueprintError('merge_unknown', '要合并的分段不存在', slice_id=item['id'], unknown=unknown)
            kept = [ref for ref in item['merged_from'] if ref in seen]
            if kept:
                raise BlueprintError('merge_invalid', '被合并的分段不能同时留在列表里', slice_id=item['id'], kept=kept)
            merged_away.update(item['merged_from'])
        # 新段（前端给的是临时编号）一律换成下一个 s<n>
        remap[item['id']] = f"s{_next(doc, 'slice')}"
    missing = [slice_id for slice_id in old if slice_id not in seen and slice_id not in merged_away]
    if missing:
        raise BlueprintError('missing', '分段不能直接删掉，要排除就标成排除', slice_ids=missing)
    for item in items:
        for ref in item['merged_from']:
            remap.setdefault(ref, remap[item['id']])
    result = []
    for item in items:
        new_id = remap.get(item['id'], item['id'])
        deps = []
        for dep in item['depends_on']:
            if dep in item['merged_from']:
                continue  # 合并后的段对原来那几段的依赖没有意义
            target = remap.get(dep, dep)
            if target not in deps:
                deps.append(target)  # 自己依赖自己留给校验报错
        if item['id'] in old:
            base = copy.deepcopy(old[item['id']])
        else:
            sources = [old[ref] for ref in item['merged_from']]
            base = _slice(new_id, item['name'], merged_from=[ref for source in sources
                                                           for ref in (source.get('merged_from') or [source['id']])])
            for field in (*MEMBER_FIELDS.values(), 'sources'):
                values = []
                for source in sources:
                    for value in source.get(field) or []:
                        if value not in values:
                            values.append(value)
                base[field] = values
        base.update(id=new_id, name=item['name'], core_flow=item['core_flow'], depends_on=deps,
                    excluded=item['excluded'])
        result.append(base)
    doc['slices'] = result
    return doc


def apply_edit(doc: dict, edit: dict) -> dict:
    """BP-02：mode=draft 草案号不变，adjust 草案号加 1；其余字段只带改过的部分。"""
    if edit.get('mode') not in ('draft', 'adjust'):
        raise BlueprintError('format', 'mode 只能是 draft 或 adjust', field='mode')
    result = copy.deepcopy(doc)
    if 'out_of_scope' in edit:
        if not isinstance(edit['out_of_scope'], list):
            raise BlueprintError('format', 'out_of_scope 要是文字列表', field='out_of_scope')
        result['out_of_scope'] = _names(edit['out_of_scope'])
    if edit.get('slices') is not None:
        result = apply_slices(result, edit['slices'])
    terms = {item['id']: item for item in (result.get('terms') or {}).get('ambiguous') or []}
    for choice in edit.get('term_choices') or []:
        if not isinstance(choice, dict) or choice.get('term_id') not in terms:
            raise BlueprintError('term_invalid', '要统一的叫法不存在', field='term_choices')
        chosen = _text(choice.get('chosen'), NAME_MAX + 1)
        if not chosen or len(chosen) > NAME_MAX:
            raise BlueprintError('term_invalid', '统一叫法不能为空，也不能超过 40 个字', field='term_choices')
        terms[choice['term_id']]['chosen'] = chosen
    questions = {item['id']: item for item in result.get('questions') or []}
    for answer in edit.get('answers') or []:
        question = questions.get((answer or {}).get('question_id') if isinstance(answer, dict) else None)
        if question is None:
            raise BlueprintError('answer_invalid', '要回答的问题不存在', field='answers')
        option_id, custom = answer.get('option_id'), _text(answer.get('custom_text'), 500)
        if option_id and option_id not in {option['id'] for option in question.get('options') or []}:
            raise BlueprintError('answer_invalid', '选项不存在', field='answers', question_id=question['id'])
        if not option_id and not custom:
            raise BlueprintError('answer_invalid', '要选一个选项或写下回答', field='answers', question_id=question['id'])
        question['answer'] = {key: value for key, value in (('option_id', option_id), ('custom_text', custom)) if value}
    if edit['mode'] == 'adjust':
        result['version_no'] = int(result.get('version_no') or 1) + 1
    result['updated_at'] = _now()
    problems = validate(result)
    if problems:
        raise problems[0]
    return result


def confirm(doc: dict) -> dict:
    result = copy.deepcopy(doc)
    if result.get('state') != 'confirmed':
        result['state'] = 'confirmed'
        result['confirmed_at'] = _now()
        result['updated_at'] = result['confirmed_at']
    return result


# ---------------------------------------------------------------------------
# 条目归属（写入范围按段收窄、按段算状态用）
# ---------------------------------------------------------------------------


def find_slice(doc: dict | None, slice_id: str | None) -> dict | None:
    return next((item for item in (doc or {}).get('slices') or [] if item.get('id') == slice_id), None)


def members(item: dict) -> dict:
    """一段包含的条目 {object: [名称], operation: [...], rule: [...]}。"""
    return {kind: list(item.get(field) or []) for kind, field in MEMBER_FIELDS.items()}


def member_paths(item: dict, kinds=MEMBER_KINDS) -> list:
    """一段包含的条目在工作区里的路径（操作含旧目录 ontology/logic）。"""
    paths = []
    for kind in kinds:
        entity = onto_metamodel.BY_KIND.get(kind)
        if entity is None:
            continue
        for name in item.get(MEMBER_FIELDS.get(kind, '')) or []:
            stem = onto_metamodel.encode_filename(name)
            paths += [f'{directory}/{stem}{entity.suffix}' for directory in entity.directories]
    return paths


def owner_of(doc: dict | None, kind: str, name: str) -> str | None:
    field = MEMBER_FIELDS.get(kind)
    for item in live_slices(doc):
        if field and name in (item.get(field) or []):
            return item['id']
    return None


def assign(doc: dict, slice_id: str, entries) -> tuple[dict, list]:
    """把条目 [(类型, 名称)] 记进一段；已经在别的（没排除的）段里的不动。返回 (新蓝图, 记进去的)。"""
    result = copy.deepcopy(doc)
    target = find_slice(result, slice_id)
    if target is None or target.get('excluded'):
        raise BlueprintError('unknown_slice', f'分段 {slice_id} 不存在或已排除')
    added = []
    for kind, name in entries:
        field = MEMBER_FIELDS.get(kind)
        if not field or not name or onto_metamodel.key_problem(name):
            continue
        if owner_of(result, kind, name) is not None:
            continue
        target.setdefault(field, []).append(name)
        added.append((kind, name))
    if added:
        result['updated_at'] = _now()
    return result, added


# ---------------------------------------------------------------------------
# 对外视图（契约 7.8 Blueprint、BlueprintSlice）
# ---------------------------------------------------------------------------


def version_label(doc: dict) -> str:
    number = int(doc.get('version_no') or 1)
    return f'v{number}' if doc.get('state') == 'confirmed' else f'草案 v{number}'


def order_label(index: int, total: int, item: dict) -> str:
    """推荐顺序的标签：第一段「推荐先做」，依赖多段的最后一段「建议最后做」，其余「第 n 个」。"""
    if item.get('excluded') or total <= 1:
        return ''
    if index == 0 and not item.get('depends_on'):
        return '推荐先做'
    if index == total - 1 and len(item.get('depends_on') or []) > 1:
        return '建议最后做'
    return f'第 {index + 1} 个'


def slice_views(doc: dict) -> list:
    live = live_slices(doc)
    out = []
    for item in doc.get('slices') or []:
        index = live.index(item) if item in live else -1
        out.append({
            'id': item['id'], 'name': item['name'], 'core_flow': item.get('core_flow') or '',
            'scale': {'objects': len(item.get('objects') or []), 'rules': len(item.get('rules') or [])},
            'depends_on': list(item.get('depends_on') or []), 'order': index + 1 if index >= 0 else 0,
            'order_label': order_label(index, len(live), item), 'excluded': bool(item.get('excluded')),
        })
    return out


def view(doc: dict) -> dict:
    """契约 Blueprint 里本文件算得出的部分（revision、updated_at、research、evidence_hint 由调用方补）。"""
    return {
        'version_no': int(doc.get('version_no') or 1),
        'version_label': version_label(doc),
        'state': doc.get('state') or 'draft',
        'mode': mode(doc),
        'goal_md': doc.get('goal_md') or '',
        'goal_anchor': doc.get('goal_anchor'),
        'goal_anchor_label': doc.get('goal_anchor_label'),
        'out_of_scope': list(doc.get('out_of_scope') or []),
        'slices': slice_views(doc),
        'terms': {'aligned': list((doc.get('terms') or {}).get('aligned') or []),
                  'ambiguous': [dict(item) for item in (doc.get('terms') or {}).get('ambiguous') or []]},
        'questions': [dict(item) for item in doc.get('questions') or []],
        'updated_at': doc.get('updated_at'),
    }


def brief(doc: dict | None, slice_id: str | None) -> str:
    """一段的说明（Markdown）：建模命令按它只做这一段；自主构建（B5b）逐段注入同一份。"""
    if doc is None or mode(doc) == 'full':
        return '全量模式：没有分段规划，按全部资料建模。'
    item = find_slice(doc, slice_id)
    if item is None or item.get('excluded'):
        return f'分段 {slice_id} 不存在或已排除。'
    names = {other['id']: other['name'] for other in doc.get('slices') or []}
    lines = [f"分段：{item['name']}（{item['id']}）"]
    if item.get('core_flow'):
        lines.append(f"核心链路：{item['core_flow']}")
    if item.get('depends_on'):
        lines.append('依赖：' + '、'.join(names.get(dep, dep) for dep in item['depends_on']))
    labels = {'objects': '对象', 'operations': '操作', 'rules': '规则'}
    for field, label in labels.items():
        values = item.get(field) or []
        lines.append(f"{label}：{'、'.join(values) if values else '（无）'}")
    if item.get('sources'):
        lines.append('依据：' + '；'.join(source.get('label') or source['anchor'] for source in item['sources']))
    if doc.get('out_of_scope'):
        lines.append('范围外：' + '、'.join(doc['out_of_scope']))
    chosen = [f"{term['chosen']}（不用 {'、'.join(v for v in term['variants'] if v != term['chosen'])}）"
              for term in (doc.get('terms') or {}).get('ambiguous') or [] if term.get('chosen')]
    if chosen:
        lines.append('统一叫法：' + '；'.join(chosen))
    others = [f"{other['name']}：{'、'.join(other.get('objects') or [])}" for other in live_slices(doc)
              if other['id'] != item['id'] and other.get('objects')]
    if others:
        lines.append('别的段的对象（只引用、不改）：' + '；'.join(others))
    return '\n'.join(lines)


def current_slice(root: Path = Path('.')) -> str | None:
    """当前执行记录的分段（后端开始执行时写进 `.onto/run/exec.json`）。"""
    try:
        data = json.loads((Path(root) / EXEC_MARKER).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    value = (data or {}).get('slice') if isinstance(data, dict) else None
    if isinstance(value, dict):
        value = value.get('id')
    return str(value) if value else None


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _fail(message: str) -> int:
    print(f'error:{message}', file=sys.stderr)
    return 1


def main(argv: list) -> int:
    root = Path('.')
    if len(argv) < 2:
        print(__doc__)
        return 1
    command, args = argv[1], argv[2:]
    try:
        if command == 'write':
            if not args:
                return _fail('用法：blueprint.py write <草稿.json>')
            draft = json.loads(Path(args[0]).read_text(encoding='utf-8'))
            doc = from_draft(draft, load(root))
            save(root, doc)
            print(f"ok:{BLUEPRINT_REL}:{version_label(doc)}:{len(live_slices(doc))} 段")
            return 0
        if command == 'full':
            doc = full(' '.join(args), load(root))
            save(root, doc)
            print(f'ok:{BLUEPRINT_REL}:{version_label(doc)}:全量模式')
            return 0
        doc = load(root)
        if command == 'check':
            if doc is None:
                return _fail('没有建模蓝图')
            problems = validate(doc)
            for problem in problems:
                print(f'- {problem}')
            print('ok' if not problems else f'{len(problems)} 个问题')
            return 1 if problems else 0
        if command == 'show':
            if doc is None:
                return _fail('没有建模蓝图')
            if '--json' in args:
                print(json.dumps(view(doc), ensure_ascii=False, indent=2))
            else:
                print(dumps(doc), end='')
            return 0
        if command == 'slices':
            items = [{'id': item['id'], 'name': item['name'], 'depends_on': item.get('depends_on') or []}
                     for item in live_slices(doc)]
            if '--json' in args:
                print(json.dumps({'mode': mode(doc), 'slices': items}, ensure_ascii=False))
            else:
                for item in items:
                    print(f"{item['id']}\t{item['name']}")
            return 0
        if command == 'brief':
            print(brief(doc, args[0] if args else current_slice(root)))
            return 0
        if command == 'assign':
            if doc is None or len(args) < 2:
                return _fail('用法：blueprint.py assign <分段> <类型:名称>…')
            entries = [tuple(arg.split(':', 1)) for arg in args[1:] if ':' in arg]
            doc, added = assign(doc, args[0], entries)
            if added:
                save(root, doc)
            print(f'ok:{len(added)}')
            return 0
    except BlueprintError as exc:
        return _fail(str(exc))
    except (OSError, ValueError) as exc:
        return _fail(str(exc))
    return _fail(f'未知子命令 {command}')


if __name__ == '__main__':
    sys.exit(main(sys.argv))
