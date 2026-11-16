"""Suggestions modeling has not handled yet, carried into the next push of the same source.

Modeling keeps only the newest run per version and source, so an earlier run still waiting for modeling would be
hidden by a newer one. When a run is handed to modeling, its report, answers and suggestions therefore also carry the
questions and suggestions of every earlier run still waiting (``notified``) on the same version of the same package:
evaluation runs of the same user, or confirmed feedback runs. Runs on other versions are never mixed in; they keep
waiting and are carried by a later push of their own version. Runs modeling failed to handle are not carried. The number of runs
is not limited; only when a file would exceed its size limit are the runs handed over earliest left out, and they keep
waiting. Going from the newest run back, the newest conclusion about a question wins: a question this run passed, or
that this run or a newer earlier run already covers, is not carried again; questions only attributed to runtime errors
are not carried either.

The run's own files are kept unspliced (``suggestions.own``, ``suggestions.items``) so a later rebuild splices them
again instead of nesting. When nothing is carried, all files are byte for byte the run's own. When modeling writes back
the result of a run, the runs carried in its push get the same result.
"""
import json
import re
import time
from datetime import datetime

from . import artifacts as output, modeling_result
from .core import Failure
from .feedback.pipeline import fingerprint
from .feedback.report import render as render_feedback
from .suggestions import row

# The run's own suggestions and their rows, before anything was carried; internal, not downloadable.
OWN, ITEMS = 'suggestions.own', 'suggestions.items'
# Earlier runs looked at, newest first; the same bound modeling's per-release view scans.
SCANNED = 1000
HEADER = '| # | 用例 | 归因 | 依据 | 建议修订 | 落点 |'


def feedback(run):
    return run.get('origin') == 'consumer_feedback'


def kind(run):
    return '反馈轮次' if feedback(run) else '评测轮次'


def version(run):
    return (run.get('base_ref') or {}).get('version') or (run.get('package_ref') or {}).get('version') or '—'


def name(run, source):
    return f"{kind(source)} {run['id'][:12]}（版本 {version(run)}）"


def handed(state, run):
    return state.get('notified_at') or state.get('updated_at') or run.get('created_at') or 0


def clock(seconds):
    return datetime.fromtimestamp(seconds, output.CHINA).strftime('%m-%d %H:%M')


def release(record):
    """The Hub release a run is about, as (release_id, version): the release chosen, not an evaluation snapshot."""
    ref = record.get('base_ref') or record.get('package_ref') or {}
    if ref.get('kind') == 'evaluation_snapshot':
        return ref.get('base_release_id') or '', ''
    return ref.get('release_id') or '', ref.get('version') or ''


def same_version(left, right):
    # Release IDs when both are known; feedback runs may still carry only the version consumer reported.
    (left_id, left_version), (right_id, right_version) = release(left), release(right)
    if left_id and right_id:
        return left_id == right_id
    return bool(left_version) and left_version == right_version


def waiting(store, jobs, owner, run):
    """Earlier runs of the same source and version still waiting for modeling, newest handed first: (row, state).

    Rows carry the run's ID, references and creation time only; the runs carried are loaded in full."""
    if not modeling_result.applies(run):
        return []
    package = (run.get('package_ref') or {}).get('ontology_id')
    found = []
    for row in store.package_run_rows(owner, package, SCANNED):
        if row['id'] == run['id'] or row['status'] in output.ACTIVE or (row['created_at'] or 0) >= (run.get('created_at') or 0):
            continue
        if not same_version(row, run):
            continue
        current = modeling_result.state(store, jobs, owner, {'id': row['id'], 'origin': run.get('origin'),
                                                             'run_type': row.get('run_type') or 'data'})
        meta = jobs.meta(owner, row['id']) or {}
        if (current or {}).get('status') != 'notified' or meta.get('report') != 'ready':
            continue
        found.append((row, current))
    found.sort(key=lambda pair: -handed(pair[1], pair[0]))
    return found


def items(store, owner, run_id):
    """A run's own suggestion rows, or None for runs from before they were kept."""
    _, body = store.artifact(owner, run_id, ITEMS)
    return json.loads(body) if body else None


def history(store, jobs, owner, run, ids=None):
    """Runs to carry and runs left out: every waiting run, or again the runs ``ids`` names, newest handed first."""
    pairs = waiting(store, jobs, owner, run)
    if ids is not None:
        chosen = {rid: i for i, rid in enumerate(ids)}
        taken = sorted((p for p in pairs if p[0]['id'] in chosen), key=lambda p: chosen[p[0]['id']])
        outside = [p for p in pairs if p[0]['id'] not in chosen]
    else:
        taken, outside = pairs, []
    return ([(store.get(owner, row['id'], 'runs'), items(store, owner, row['id']), state) for row, state in taken],
            list(outside))


def plan(own, taken, outside=()):
    """What to carry from the earlier runs, newest first: this run's conclusions win, then newer earlier runs'."""
    # Questions with a conclusion: judged in this run, passed or carried by a newer earlier run. Errors and cases
    # waiting for a human say nothing about a question, so an earlier failure of it is still carried.
    decided = {fingerprint(case.get('question')) for case in own['cases']
               if output.outcome(case)[0] in ('pass', 'partial', 'fail')}
    seen = {fingerprint(item.get('scope') or item.get('content')) for item in output.user_suggestions(own)}
    start, keep, proposals, rounds = len(own['cases']), [], [], []
    for earlier, rows, state in taken:
        listed = rows or []
        runtime_cases, other_cases = set(), set()
        for row in listed:
            if row['category'] == 'E':
                runtime_cases.update(row['cases'])
            else:
                other_cases.update(row['cases'])
        runtime = runtime_cases - other_cases
        mapping = {}
        for number, case in enumerate(earlier['cases'], 1):
            verdict, mark = output.outcome(case)[0], fingerprint(case.get('question'))
            execution_blocked = (case.get('execution_verification') or {}).get('status') in ('missing_query','query_failed','unverifiable','unsupported')
            eligible = verdict in ('partial', 'fail') and mark not in decided and number not in runtime
            if eligible and not execution_blocked:
                keep.append((earlier, case))
                mapping[number] = start + len(keep)
            if verdict in ('pass', 'partial', 'fail'):
                decided.add(mark)
        suggested = {}
        for number, item in enumerate(output.user_suggestions(earlier), 1):
            mark = fingerprint(item.get('scope') or item.get('content'))
            if mark not in seen:
                seen.add(mark)
                proposals.append((earlier, item))
                suggested[number] = len(output.user_suggestions(own)) + len(proposals)
        kept = []
        for row in listed:
            cases = [mapping[n] for n in row['cases'] if n in mapping]
            cited = [suggested[n] for n in row.get('suggestions') or [] if n in suggested]
            if row['category'] == 'E' or not (cases or cited):
                continue
            dropped = len(row['cases']) - len(cases)
            note = (f'（原来引用的另外 {dropped} 条问题已由本轮或较新的轮次覆盖，这里的文字可能还提到它们，复核时请留意）'
                    if dropped else '')
            kept.append(dict(row, cases=cases, suggestions=cited, evidence=note + row['evidence']))
        rounds.append(dict(run=earlier, state=state, rows=kept, legacy=rows is None,
                           cases=sum(1 for r, _ in keep if r is earlier)))
    return dict(start=start, keep=keep, proposals=proposals, rounds=rounds, outside=list(outside))


def head_note(value, source):
    if not value['outside']:
        return ''
    return (f"另有 {len(value['outside'])} 个更早的未处理{kind(source)}因文件超过大小上限没有带上，仍待处理："
            + '、'.join(name(r, source) for r, _ in value['outside']) + '。')


def report(own_text, own, value, stem, now):
    """The run's report with the carried questions after its own, numbered on; the overview stays the run's own."""
    if not public(value):
        return own_text
    source = own
    render = render_feedback if feedback(own) else output.render_report
    used = [r['run'] for r in value['rounds'] if r['cases'] or r['rows']]
    settled = [r['run'] for r in value['rounds'] if not (r['cases'] or r['rows'])]
    legacy = [r['run'] for r in value['rounds'] if r['legacy'] and r['cases']]
    count = len(value['keep'])
    head = '> 拼接历史建议：'
    if used:
        head += (f"带上 {count} 条未处理的历史问题，来自 {'、'.join(name(r, source) for r in used)}。"
                 f"「总览」{'的统计' if feedback(own) else '和得分'}只算本轮，历史问题排在「逐用例明细」后面，编号接着本轮往下排。")
    if settled:
        head += (f"{'、'.join(name(r, source) for r in settled)} 的问题本轮或较新的轮次已有结论，没有再带上，"
                 '随本轮一起交给建模。')
    if legacy:
        head += f"{'、'.join(name(r, source) for r in legacy)} 生成于保存建议条目之前，只带问题。"
    head += head_note(value, source)
    text = own_text.replace('\n\n## 总览', '\n' + head + '\n\n## 总览', 1)
    tail = ''
    if value['keep']:
        full = render(dict(own, cases=own['cases'] + [case for _, case in value['keep']]), stem, now)
        tail = full[full.index(f"### {value['start'] + 1}. "):]
        tail = re.split(r'\n## ', tail)[0].rstrip() + '\n'
        blocks = [block for block in re.split(r'(?=^### \d+\. )', tail, flags=re.M) if block.strip()]
        rounds = {r['run']['id']: r for r in value['rounds']}
        tail = ''.join(block.replace('- **判分理由**：', f"  - **历史来源**：{name(earlier, source)}，"
                                     f"{clock(handed(rounds[earlier['id']]['state'], earlier))} 交给建模，建议尚未处理\n\n"
                                     '- **判分理由**：', 1)
                       for block, (earlier, _) in zip(blocks, value['keep']))
        tail = (f"> ↓ 以下 {count} 条来自历史轮次，建议尚未处理，不计入本轮{'统计' if feedback(own) else '总览和得分'}。\n\n"
                + tail)
    if not feedback(own):
        return text.rstrip() + ('\n\n' + tail if tail else '\n')
    # Feedback reports list user suggestions and excluded feedback after the cases; carried questions go before them.
    parts = re.split(r'(?=\n## (?:用户建议|未纳入本次优化))', text, maxsplit=1)
    body = parts[0].rstrip() + ('\n\n' + tail if tail else '\n')
    rest = parts[1] if len(parts) > 1 else ''
    if value['proposals']:
        first = len(output.user_suggestions(own)) + 1
        rows = []
        for number, (earlier, item) in enumerate(value['proposals'], first):
            scope = (item.get('scope') or '—').replace('operation:', '操作：').replace('entity:', '对象：')
            content = output.clip(' '.join(str(item.get('content') or '').split()), 600).replace('|', '\\|')
            rows.append(f"| {number} | {content}（历史，版本 {version(earlier)}） | {scope} | {item.get('users', 1)} |")
        if '## 用户建议' in rest:
            lines = rest.split('\n')
            last = max(i for i, line in enumerate(lines) if re.match(r'^\| \d+ \|', line))
            rest = '\n'.join(lines[:last + 1] + rows + lines[last + 1:])
        else:
            rest = '\n## 用户建议\n\n| # | 建议 | 关联 | 用户数 |\n|---|---|---|---|\n' + '\n'.join(rows) + '\n' + rest
    return body + rest


def answers(own_text, own, value):
    if not value['keep']:
        return own_text
    return output.render_answers(dict(own, cases=own['cases'] + [case for _, case in value['keep']]))


def suggestions(own_text, own, value):
    """The run's suggestions with the carried rows in their own section, numbered on; no model is asked again."""
    rounds = [r for r in value['rounds'] if r['rows']]
    if not rounds:
        if not value['outside']:
            return own_text
        return own_text.replace('\n\n## 总体判断', f"\n> 拼接历史建议：{head_note(value, own)}\n\n## 总体判断", 1)
    own_rows = len(re.findall(r'^\| \d+ \|', own_text.split('\n## 说明')[0], flags=re.M))
    total = sum(len(r['rows']) for r in rounds)
    section, number = [f"## 历史未处理建议（共 {total} 条，来自 {len(rounds)} 个{kind(own)}）", ''], own_rows
    for r in rounds:
        section += [f"### {name(r['run'], own)}", '', HEADER, '|---|---|---|---|---|---|']
        for item in r['rows']:
            number += 1
            section.append(row(number, item))
        section.append('')
    section = '\n'.join(section) + '\n'
    if '## 总体判断' not in own_text:
        # Nothing of this run to fix: the carried suggestions are the whole push.
        head = (f"> 拼接历史建议：本轮没有失分问题，但还有 {total} 条历史建议未处理，一起带上，没有重新调用判分模型；"
                '它们基于更早的知识包版本，请对照当前工作区逐条复核。' + head_note(value, own))
        text = own_text.replace('无需评测优化。', '本轮无需评测优化；下面带上了历史轮次还没处理的建议。')
        return (text.rstrip() + '\n\n' + head + '\n\n' + section + '\n## 说明\n\n'
                + '- 用例编号对应评测报告「逐用例明细」中的序号' + ('，“建议 n”对应「用户建议」中的序号。\n' if feedback(own) else '。\n')
                + '- 修订须遵守泛化红线：只写普适业务知识，不得照抄用例问题或标准答案。\n')
    head = (f"> 拼接历史建议：带上 {len(rounds)} 个{kind(own)}未处理的 {total} 条建议，没有重新调用判分模型；"
            '它们基于更早的知识包版本，落点可能已变化，请对照当前工作区逐条复核。' + head_note(value, own))
    text = own_text.replace('\n\n## 总体判断', '\n' + head + '\n\n## 总体判断', 1)
    return text.replace('\n## 说明', '\n' + section + '\n## 说明', 1)


def files(store, jobs, owner, run, stem, now, own, ids=None):
    """Spliced files for the run's own ``{'report.md', 'answers.json', 'suggestions.md'?}`` and the plan used.

    The runs handed over earliest are left out until every file fits its documented size."""
    taken, outside = history(store, jobs, owner, run, ids)
    while True:
        value = plan(run, taken, outside)
        result = {'report.md': report(own['report.md'], run, value, stem, now),
                  'answers.json': answers(own['answers.json'], run, value)}
        if own.get('suggestions.md') is not None:
            result['suggestions.md'] = suggestions(own['suggestions.md'], run, value)
        if not taken or all(len(text.encode()) <= output.LIMITS[key] for key, text in result.items()):
            return result, value
        earlier, _, state = taken.pop()
        outside.insert(0, (earlier, state))


def public(value):
    """The plan as kept with the artifacts and shown to users: which runs were carried and which slid out."""
    if not value or not (value['rounds'] or value['outside']):
        return None
    return dict(rounds=[dict(run_id=r['run']['id'], version=version(r['run']), handed_at=handed(r['state'], r['run']),
                             cases=r['cases'], items=len(r['rows']), legacy=r['legacy']) for r in value['rounds']],
                slid_out=[dict(run_id=earlier['id'], version=version(earlier)) for earlier, _ in value['outside']])


def mark(store, jobs, owner, run, value, now=None):
    """Note on each earlier run still waiting whether this push carries it or had to leave it out."""
    now = now or time.time()
    marks = [(r['run'], 'carried_into', 'slid_out') for r in (value or {}).get('rounds') or []]
    marks += [(earlier, 'slid_out', 'carried_into') for earlier, _ in (value or {}).get('outside') or []]
    for earlier, key, other in marks:
        state = modeling_result.state(store, jobs, owner, earlier) or {}
        if state.get('status') != 'notified':
            continue  # Handled meanwhile.
        state = {field: item for field, item in state.items() if field != other}
        store.put_optimization(owner, earlier['id'], dict(state, **{key: {'run_id': run['id'], 'at': now}}))


def propagate(store, jobs, owner, run, value):
    """Give the runs carried in this run's push the result modeling wrote back for it; runs since handled keep theirs.

    Called again when modeling retries the same write-back, so an interrupted propagation completes."""
    entry = value.get('result')
    if not entry:
        return
    for carried in ((jobs.meta(owner, run['id']) or {}).get('carryover') or {}).get('rounds') or []:
        try:
            earlier = store.get(owner, carried['run_id'], 'runs')
        except Failure:  # Deleted since; nothing to record.
            continue
        current = modeling_result.state(store, jobs, owner, earlier) or {}
        if current.get('status') != 'notified':
            continue
        # Told by when this run was handed over, not by its ID: that is what the pages show.
        when = value.get('notified_at') or (jobs.meta(owner, run['id']) or {}).get('generated_at')
        note = (f"随 {clock(when)} 交付的{kind(run)}一起处理" if when else f"随较新的{kind(run)}一起处理") \
            + (f"：{entry['note']}" if entry.get('note') else '')
        earlier_results = ([current['result']] if current.get('result') else []) + list(current.get('history') or [])
        store.put_optimization(owner, earlier['id'], {
            **current, 'status': value['status'], 'updated_at': value['updated_at'],
            'result': dict(entry, note=note[:2000], via=run['id']), 'history': earlier_results[:modeling_result.HISTORY]})
