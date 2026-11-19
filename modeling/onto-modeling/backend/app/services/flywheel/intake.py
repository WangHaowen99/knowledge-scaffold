"""Turning the user's evaluation results into flywheel runs.

Evaluation-service notifications land in the user's local index (package_evaluations.receive); each
sync reads that index. A run is keyed by the evaluation run, so duplicate or late notifications change
nothing, and a regenerated revision of the suggestions updates the run it belongs to.

Feedback runs (consumer users' feedback, confirmed by a package owner for modeling) come in the same way.
They may concern an earlier version: they do not expire, the optimization first checks which problems the
current content still has. They have no evaluation set, so nothing is evaluated after their publication.
"""
import math
import re

from .. import package_evaluations
from ..workspace import WorkspaceError
from . import evaluation, store

SUGGESTION_ROW = re.compile(r'^\|\s*\d+\s*\|')
CATEGORY_PREFIX = re.compile(r'^[A-E]\s+')
# 一次同步最多读取几份新到的优化建议（各一次评测服务请求），其余留给下次同步
SUMMARY_READS = 3


def version_label(value):
    return f'v{value}' if value else '未知版本'


FEEDBACK = 'consumer_feedback'


def is_feedback(item):
    return (item.get('origin') or 'evaluation') == FEEDBACK


def handed_over(entry):
    """A feedback run a package owner confirmed for modeling, which modeling has not handled yet."""
    return (entry.get('optimization') or {}).get('status') == 'notified'


def base_release(run):
    """The release the run's content builds on: the one current when its optimization started."""
    return run.get('base_release_id') or run.get('release_id')


def resolved_unreported(run):
    """Feedback the latest version already resolves, not yet told to the evaluation service."""
    return (is_feedback(run) and run['status'] == 'ignored' and run.get('reason') == 'already_resolved'
            and bool(run.get('eval_run_id')) and not run.get('writeback'))


def ready(entry):
    """Report and suggestions have both settled; suggestions still generating are waited for."""
    artifacts = entry['artifacts']
    return (artifacts['status'] != 'generating' and artifacts['report'] == 'ready'
            and artifacts['suggestions'] in ('ready', 'failed'))


def unseen(state, overview):
    """Results not taken in yet: evaluation runs, and feedback runs handed over to modeling."""
    found = [*overview['releases'].values(), *(overview.get('feedback') or {}).values()]
    entries = [entry for entry in found
               if ready(entry) and state['seen'].get(entry['run_id'], -1) < entry['artifacts']['revision']]
    return sorted(entries, key=lambda entry: entry.get('finished_at') or entry['artifacts'].get('generated_at') or 0)


def parse_suggestions(content):
    """Count the rows of the suggestions table and name their categories (A 知识缺失 → 知识缺失)."""
    rows = [line for line in content.splitlines() if SUGGESTION_ROW.match(line)]
    categories = []
    for line in rows:
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        name = CATEGORY_PREFIX.sub('', cells[2]) if len(cells) > 2 else ''
        if name and name not in categories:
            categories.append(name)
    return {'count': len(rows), 'categories': categories[:4]}


def suggestion_summary(pid, subject, token, run_id):
    try:
        value = package_evaluations.document(pid, subject, token, run_id, 'suggestions')
    except WorkspaceError:
        return None  # 读不到时卡片只写“优化建议已就绪”，不影响建立运行
    return parse_suggestions(value['content']) if value.get('content') else None


def _needs_summary(entry, current_release):
    wanted = handed_over(entry) if is_feedback(entry) else entry['release_id'] == current_release
    return entry['artifacts']['suggestions'] == 'ready' and wanted


def summaries(pid, subject, token, entries, current_release):
    wanted = [entry['run_id'] for entry in entries if _needs_summary(entry, current_release)][:SUMMARY_READS]
    return {run_id: suggestion_summary(pid, subject, token, run_id) for run_id in wanted}


def arrive(state, run):
    """Decide at arrival whether the run may start on its own; held runs wait for a confirmation."""
    settings = state['settings']
    others = any(item['status'] == 'waiting_optimize' and item['id'] != run['id'] for item in state['runs'])
    if not settings['optimize']:
        run.update(human_hold=True, reason='automation_off')
    elif store.active(state) or store.candidate(state) or others:
        run.update(human_hold=True, reason='project_busy')
    elif not is_feedback(run) and store.rounds_exhausted(state):
        # 轮数上限防的是“优化→发布→评测”无休止循环；用户反馈发布后不评测，不在其中
        run.update(human_hold=True, reason='round_limit')
    else:
        run.update(human_hold=False, reason='queued')


def _fields(entry, summary):
    artifacts = entry['artifacts']
    cases = {key: entry['summary'].get(key, 0) for key in ('total', 'pass', 'partial', 'fail', 'error', 'manual')}
    return dict(eval_run_id=entry['run_id'], release_id=entry['release_id'], version=entry['version'],
                revision=artifacts['revision'], score=None if is_feedback(entry) else strict_rate(entry),
                stem=artifacts.get('stem'), suggestions=summary, suggestions_error=artifacts.get('suggestions_error'),
                cases=cases, origin=entry.get('origin') or 'evaluation')


def _settle(run, entry, summary):
    """Status of a run whose evaluation result is new or was regenerated."""
    if entry['artifacts']['suggestions'] == 'failed':
        store.block(run, 'suggestions_failed', '优化建议尚未就绪', '评测报告已完成，建议生成失败；未触发任何知识内容修改。')
    elif summary and summary['count'] == 0:
        run.update(status='ignored', reason='nothing_to_optimize')
        store.note(run, '无需优化', '优化建议没有需要修订的条目，不修改知识包。' if is_feedback(run)
                   else '本轮评测没有失分或异常用例，不修改知识包。', 'wait')
    else:
        run.update(status='waiting_optimize', reason='')


def _revise(run, entry, summary):
    revision = entry['artifacts']['revision']
    pending = run['status'] == 'waiting_optimize' or (run['status'] == 'blocked' and run['reason'] == 'suggestions_failed')
    run.update({key: value for key, value in _fields(entry, summary or run.get('suggestions')).items()
                if key not in ('release_id', 'version')})
    if not pending:
        return  # 已处理的运行保留结论，不因新的建议修订重新处理；全部通过的由 close_passed 收尾
    if not is_feedback(entry) and passed(entry):
        mark_passed(run, f'报告修订 r{revision}：全部 {entry["summary"]["total"]} 个用例通过，无需优化。')
        return
    _settle(run, entry, summary)
    if run['status'] == 'waiting_optimize':
        run.update(human_hold=True)
        store.note(run, '优化建议重新生成完成', f'报告与建议已更新为 r{revision}，等待人工确认。')


def all_passed(cases):
    """Every case judged a pass; cases waiting for a manual verdict do not count as passed."""
    return bool(cases) and cases.get('total', 0) > 0 and cases.get('pass') == cases['total']


def passed(entry):
    return all_passed(entry['summary'])


def mark_passed(run, text):
    run.update(status='passed', reason='', human_hold=True)
    store.note(run, '评测集全部通过', text)


# 评测结果全部通过时，停在优化这一侧的运行已无事可做：不再挂在“需处理”
SETTLED_BY_PASS = ('suggestions_failed', 'no_changes', 'optimization_failed', 'optimization_interrupted',
                   'assistant_unavailable')


def close_passed(state, current_release):
    """Runs whose latest evaluation passes every case need no handling, whatever happened to them before.

    Returns the runs closed by this call."""
    closed = []
    for run in state['runs']:
        waiting = run['status'] == 'waiting_optimize' or (
            run['status'] == 'blocked' and run['reason'] in SETTLED_BY_PASS)
        if waiting and not is_feedback(run) and all_passed(run.get('cases')):
            mark_passed(run, f"最新评测结果全部 {run['cases']['total']} 个用例通过，无需继续处理。")
            closed.append(run)
            if run.get('release_id') == current_release:
                state['auto_rounds'] = 0  # 这一段连续处理已成功结束
    return closed


def strict_rate(entry):
    """The evaluation score: the strict pass rate of the evaluation page's result overview, in percent.

    Passed cases of all planned ones; partial, failed, errored and manual cases all count against it. A whole
    percent computed as the page does (its Math.round of the passed share times 100, same double arithmetic:
    2/3 is 67, 1/16 is 6), and 100 exactly when ``passed``."""
    cases = entry['summary']
    if not cases['total']:
        return None
    return math.floor(cases['pass'] / cases['total'] * 100 + 0.5)


def rate_label(value):
    return f'{math.floor(value + 0.5)}%' if value is not None else '—'


def _rescore(state, entry):
    """The run that published this release, finished by its evaluation arriving; None when there is none."""
    score = strict_rate(entry)
    origin = None
    for run in state['runs']:
        if run.get('published_release_id') != entry['release_id']:
            continue
        origin = run
        if run.get('after_score') is None and score is not None:
            run['after_score'] = score
        if run['status'] in ('waiting_evaluation', 'evaluating') or (
                run['status'] == 'blocked' and run['reason'] in evaluation.RETRY):
            evaluation.finish(run, entry, passed(entry))
    return origin


def take_in(state, entries, current_release, summaries):
    """Apply new evaluation results; returns the runs created by this call."""
    created = []
    for entry in entries:
        run_id, revision = entry['run_id'], entry['artifacts']['revision']
        if _needs_summary(entry, current_release) and run_id not in summaries:
            continue  # 本次没读到建议摘要的结果留到下次同步
        state['seen'][run_id] = revision
        feedback = is_feedback(entry)
        origin = None if feedback else _rescore(state, entry)
        summary = summaries.get(run_id)
        existing = next((run for run in state['runs'] if run.get('eval_run_id') == run_id), None)
        if existing:
            _revise(existing, entry, summary)
            continue
        if feedback:
            if handed_over(entry):
                created.append(_take_feedback(state, entry, summary, current_release))
            continue  # 已由建模处理过、或还没交给建模的用户反馈不建运行
        if not state['baseline'] and entry['release_id'] != current_release:
            continue
        run = store.new_run(state, **_fields(entry, summary))
        if origin:
            started = (origin.get('evaluation') or {}).get('mode')
            run.update(source_id=origin['id'], trigger='flywheel' if started else 'evaluation_page', evaluation_mode=started)
        store.note(run, '收到评测服务通知',
                   f"通知基于 {version_label(entry['version'])}，报告修订号 r{revision}，已建立运行记录。"
                   + (f"评测由 {origin['id']} 发布的版本发起。" if origin else ''))
        if entry['release_id'] != current_release:
            _close(run, '版本校验未通过', f"该通知基于 {version_label(entry['version'])}，与当前项目版本不一致，不再处理。")
        elif passed(entry):
            state['auto_rounds'] = 0  # 这一段连续处理已成功结束，之后的失分重新计轮
            mark_passed(run, f"全部 {entry['summary']['total']} 个用例通过，循环结束，不再继续优化。")
        else:
            _settle(run, entry, summary)
            if run['status'] == 'waiting_optimize':
                arrive(state, run)
        created.append(run)
    state['baseline'] = True
    return created


def _take_feedback(state, entry, summary, current_release):
    run = store.new_run(state, **_fields(entry, summary))
    total = entry['summary'].get('total', 0)
    store.note(run, '收到用户反馈', f"包负责人已把 {version_label(entry['version'])} 的用户反馈交给建模"
               f"（{total} 个问题），报告修订号 r{entry['artifacts']['revision']}，已建立运行记录。")
    if entry['release_id'] != current_release:
        store.note(run, '反馈针对旧版本', '优化时先核对当前版本是否仍存在这些问题，仍存在的才修改知识包。')
    _settle(run, entry, summary)
    if run['status'] == 'waiting_optimize':
        arrive(state, run)
    return run


def _close(run, title, text):
    """A run of a version the project has left can neither be optimized nor published: it closes, nothing to handle."""
    run.update(status='expired', reason='version_mismatch', human_hold=True)
    store.note(run, title, text, 'wait')


def _superseded(run, current_release, current_committed):
    """Whether the project has a newer release than the one the run published. A run's own release committed
    after the current one is not superseded: the project's record of it is only late (a sync that read the
    record before the publication ended, or a record left for maintenance)."""
    if run['published_release_id'] == current_release:
        return False
    # 早于记下提交时间的运行无从比较：版本不同即视为已有更新的版本
    return not run.get('published_committed_at') or current_committed > run['published_committed_at']


def expire(state, current_release, current_committed=''):
    """Runs of an older version cannot go on once the project moved to another one.

    ``current_committed``: when the current release was committed in Hub."""
    for run in state['runs']:
        published = run.get('published_release_id')
        if published:
            # 已发布的运行只剩评测这一步：有了更新的版本就不再评测它
            waiting = run['status'] == 'waiting_evaluation' or (
                run['status'] == 'blocked' and run['reason'] in evaluation.RETRY)
            if waiting and _superseded(run, current_release, current_committed):
                run.update(status='published', reason='')
                store.note(run, '跳过评测', '项目已有更新的正式版本，不再评测这个版本。', 'wait')
            continue
        if is_feedback(run) and not run.get('candidate'):
            continue  # 用户反馈不随版本过期：优化时按最新版本核对
        # 早先因版本不一致停在“需处理”的运行也在这里关闭：它们同样已无事可做
        stale = run['status'] in ('waiting_optimize', 'waiting_publish', 'blocked')
        if stale and base_release(run) != current_release:
            _close(run, '通知已过期', '项目已有新的正式版本，该运行不再满足当前基线，不再处理。')
