"""Evaluation optimization of a flywheel run: import the artifacts, snapshot, let the assistant revise.

The assistant runs /ontology-evolution in flywheel mode: the confirmation in the flywheel (or the
automation switch) stands for the expert's "fix all" answer, so nobody has to answer in the chat. Feedback
about an earlier version is handed over with --flywheel-recheck: the assistant first checks which of its
problems the current content still has, and changes only those. It says that none remain by writing
RECHECK_MARK; changing nothing is not enough, as it also happens when items wait for an expert.
"""
import json
import threading
import time

from .. import hub_packages, package_evaluations
from ..workspace import WorkspaceError
from . import checks, intake, snapshots, store

COMMAND = '/ontology-evolution {report} --flywheel'
RECHECK_COMMAND = '/ontology-evolution {report} --flywheel-recheck'
# 助手核对后确认反馈的问题在最新版本都已不存在时写下的结论（.ontology-state 不入库，也不算知识包修改）
RECHECK_MARK = ('.ontology-state', 'flywheel-recheck.json')
RESUMABLE = ('optimization_interrupted', 'optimization_failed', 'assistant_unavailable')
STOP_WAIT_SECONDS = 15
_handoff_guard = threading.Lock()
_handoffs = {}


def handoff(pid):
    # 跨用户串行：两个人的飞轮不能同时把命令交给同一个项目会话，也不能在别人开始优化的同时发布或回退
    with _handoff_guard:
        return _handoffs.setdefault(pid, threading.Lock())


def iteration_label(snapshot):
    return f"iteration-{snapshot['iteration']:03d}" if snapshot else '尚未生成'


def optimizable(run):
    return (run['status'] in ('waiting_optimize', 'interrupted')
            or (run['status'] == 'blocked' and run['reason'] in RESUMABLE))


def blocker(state, run, linked):
    """Why the run cannot start optimizing now; empty when it can. A busy assistant is checked apart (start)."""
    if not optimizable(run):
        return '本次建议已处理，可在运行记录中查看结果'
    if run.get('release_id') != linked.get('release_id') and not intake.is_feedback(run):
        return f"该版本与当前建模项目版本 {linked.get('version') or '未发布'} 不一致，不能评测优化"
    if store.active(state):
        return '项目正在执行任务，请等待本次任务完成'
    current = store.candidate(state)
    if current and current['id'] != run['id']:
        return '请先处理当前待发布候选内容'
    return ''


def start(caller, run_id, automatic):
    manager, pid = caller.manager, caller.pid
    # 从检查别人的飞轮到记为优化中都持有这把锁：别人飞轮的发布、回退插不进来，读到的版本也不会被它们改掉
    with handoff(pid):
        linked = hub_packages.association(pid) or {}
        state = store.load(pid, caller.subject)
        run = store.find(state, run_id)
        reason = blocker(state, run, linked)
        if reason:
            raise WorkspaceError(reason, 409, 'flywheel_blocked')
        if caller.assistant.busy(pid):
            # 助手正忙（成员的对话，或别人飞轮刚开始的修订）：自动执行等下一轮，不转为人工确认
            raise WorkspaceError('助手正在运行，请等本轮结束后再优化', 409, store.ASSISTANT_BUSY_CODE)
        if store.others_working(pid, caller.subject):
            # 别人正在修订或发布工作区：现在开始，两次修订会混进同一份快照和候选内容
            raise store.others_working_error('优化')
        feedback = intake.is_feedback(run)
        earlier = run.get('release_id') != linked.get('release_id')
        imported = package_evaluations.optimize(manager, pid, caller.subject, caller.token, run['eval_run_id'],
                                                any_version=feedback, flywheel_run=run_id)
        try:
            taken = snapshots.take(manager, pid, f'chore: 数据飞轮 {run_id} 执行前快照')
            # 中断后重试沿用最初的快照：回退与泛化检查都以本次运行开始前的内容为准
            snapshot = run.get('snapshot') or taken
            # 优化开始前的内容指纹：与上次发布的不同，说明此前另有未发布的修改，会随本次一起发布
            base_inputs = run.get('base_inputs') or hub_packages.workspace_inputs(manager, pid)
            command = RECHECK_COMMAND if earlier else COMMAND
            # 上一次核对留下的结论不能算到这一次
            manager.project_root(pid).joinpath(*RECHECK_MARK).unlink(missing_ok=True)
            session = caller.assistant.start(pid, manager.project_root(pid),
                                             command.format(report=imported['report']), caller.username)
        except Exception:
            # 没能交给助手：飞轮没有修改内容，与“没有修改”同样收尾，否则这次评测优化会一直记为修订中
            end_revision(caller, run, own_release=True)
            raise
        with store.update(pid, caller.subject) as state:
            run = store.find(state, run_id)
            if not feedback:
                state['auto_rounds'] = state['auto_rounds'] + 1 if automatic else 0
            for key in ('checks', 'candidate', 'error'):
                run.pop(key, None)
            run.update(status='optimizing', reason='', note='', mode='auto' if automatic else 'manual',
                       human_hold=False, step=2, started_at=time.time(), snapshot=snapshot, base_inputs=base_inputs,
                       report=imported['report'], suggestions_file=imported['suggestions'],
                       base_release_id=linked.get('release_id'), base_version=linked.get('version'), **session)
            count = (run.get('suggestions') or {}).get('count')
            store.note(run, '保存执行前快照', f'已保留 {iteration_label(snapshot)}，本次优化可以回退。')
            found = f'{count} 条建议' if count else '优化建议'
            if earlier:
                text = (f"{found}针对 {intake.version_label(run.get('version'))}，当前为 "
                        f"{intake.version_label(linked.get('version'))}：先核对当前版本是否仍存在这些问题，仍存在的才修改。")
            else:
                text = f'{found}均来自当前版本，报告修订号 r{run["revision"]}。'
            store.note(run, '用户反馈报告与建议已就绪' if feedback else '评测报告与建议已就绪', text)
            store.note(run, '开始修订知识内容', '已交给建模助手执行 /ontology-evolution，过程可在右侧对话中查看。', 'active')
            return run


def settle(run, outcome):
    """Record how the assistant turn ended; returns True when the candidate checks are due."""
    if outcome == 'running':
        return False
    if outcome == 'finished':
        if run.get('step', 0) < 3:
            run['step'] = 3
            store.note(run, '知识内容修订完成', '建模助手已结束本轮修订，开始质量与发布检查。')
        return True
    if outcome == 'failed':
        store.block(run, 'optimization_failed', '优化未完成', '建模助手本轮执行出错，已保留执行前快照。')
    else:
        store.block(run, 'optimization_interrupted', '优化已中断', '建模助手本轮未正常结束，已保留执行前快照。')
    return False


def resolved_by_recheck(root, run):
    """Whether the assistant concluded that the latest version no longer has any of this run's problems."""
    try:
        value = json.loads(root.joinpath(*RECHECK_MARK).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get('resolved') is True and value.get('report') == run.get('report')


def conclude(state, run, result, hold, resolved=False):
    """Turn checked content into a candidate, or keep the run blocked with the failing checks.

    ``resolved``: the assistant concluded the latest version already resolves the feedback (resolved_by_recheck)."""
    previous = run.get('candidate') or {}
    attempt = previous.get('attempt', 0) + (previous.get('revision') != result['revision'])
    run.update(checks=result, changed=result['changed_count'], check_groups=checks.groups(result),
               candidate={'revision': result['revision'], 'head': result['head'], 'attempt': max(attempt, 1)})
    if run.get('kind') != 'rollback' and not result['changed_count'] and intake.all_passed(run.get('cases')):
        # 优化期间评测结果已更新为全部通过（如判分超时重判后通过）：没改内容也无事可做
        intake.mark_passed(run, '建模助手没有修改知识包文档；最新评测结果全部通过，无需发布新版本。')
        state['auto_rounds'] = 0  # 这一段连续处理已成功结束
    elif intake.is_feedback(run) and not result['changed_count'] and resolved:
        # 助手核对后确认反馈的问题在最新版本已不存在：没有要发布的内容，也无需人工处理
        run.update(status='ignored', reason='already_resolved', human_hold=True)
        store.note(run, '最新版本已解决', '建模助手核对后没有修改知识包文档：反馈的问题在最新版本已不存在。', 'wait')
    elif run.get('kind') != 'rollback' and not result['changed_count']:
        run.update(status='blocked', reason='no_changes')
        store.note(run, '本次优化未修改知识内容', '建模助手没有修改知识包文档，也没有确认问题已解决；请查看演进报告后处理或忽略。'
                   if intake.is_feedback(run) else '建模助手没有修改知识包文档，无需发布新版本。', 'wait')
    elif not result['passed']:
        store.block(run, 'checks_failed', '发布检查未通过',
                    f"未通过：{'、'.join(checks.failing(result))}。已保留优化结果，修复并重新检查后才能发布。")
    else:
        run.update(status='waiting_publish', reason='', publish_hold=hold)
        store.note(run, '全部发布检查通过', '已生成待发布候选内容，发布后不发起评测。' if intake.is_feedback(run)
                   else '已生成待发布候选内容，发布后评测新版本的严格通过率。')
        if hold:
            store.note(run, '等待确认发布', '候选内容保留，尚未创建正式版本。', 'wait')


def check(caller, run, hold):
    """Commit the run's result as an iteration, check it, and record the outcome."""
    manager, pid = caller.manager, caller.pid
    snapshots.take(manager, pid, f"chore: 数据飞轮 {run['id']} 评测优化结果")
    base = (run.get('snapshot') or {}).get('commit') if run.get('kind') != 'rollback' else None
    result = checks.evaluate(manager, pid, base, run.get('report'))
    resolved = resolved_by_recheck(manager.project_root(pid), run)
    with store.update(pid, caller.subject) as state:
        current = store.find(state, run['id'])
        conclude(state, current, result, hold, resolved)
    if run.get('kind') != 'rollback':
        if result['changed_count'] or current.get('reason') == 'no_changes':
            # 修订结束，此后发布的正式版本才算处理结果；没有修改、条目留给人处理时，只算优化者本人发布的
            end_revision(caller, current, own_release=not result['changed_count'])
        else:
            forget(caller, current)  # 已全部通过或最新版本已解决
    return current


def end_revision(caller, run, own_release=False):
    package_evaluations.end_revision(caller.pid, caller.subject, run.get('eval_run_id'), run['id'], own_release)


def forget(caller, run):
    """The run's optimization stands for no release (stopped, ignored, passed, already resolved): drop the project's
    latest optimization if this run started it, so no later release is written back as its result."""
    package_evaluations.forget(caller.pid, caller.subject, run.get('eval_run_id'), run['id'])


def stop(caller, run_id):
    manager, pid = caller.manager, caller.pid
    run = store.find(store.load(pid, caller.subject), run_id)
    if run['status'] != 'optimizing':
        raise WorkspaceError('该运行不在优化中', 409, 'flywheel_blocked')
    # 助手正忙，又不能确定忙的是本次优化那一轮（那一轮已结束，或读不到对话记录）：忙的可能是别人的对话，
    # 既不打断它，也不恢复快照抹掉它尚未提交的修改
    ended = caller.assistant.turn_ended(pid, run['conversation_id'], run.get('history_start', 0))
    if caller.assistant.busy(pid) and ended is not False:
        raise WorkspaceError('建模助手仍在忙（可能在处理其他对话），请等其结束后再处理本次结果', 409, 'flywheel_blocked')
    try:
        caller.assistant.interrupt(pid, run['conversation_id'])
    except WorkspaceError:
        pass  # 会话已结束也照样恢复快照
    deadline = time.time() + STOP_WAIT_SECONDS
    while caller.assistant.busy(pid) and time.time() < deadline:
        time.sleep(0.5)
    if caller.assistant.busy(pid):
        # 打断后仍在执行：现在恢复，之后写入的修改会落在恢复的内容上。本轮结束后运行转为已中断，可再回退
        raise WorkspaceError('建模助手仍在执行，暂未恢复执行前内容：本轮结束后可在运行详情中回退', 409, 'flywheel_blocked')
    snapshots.restore(manager, pid, run['snapshot']['commit'], f'revert: 数据飞轮中止 {run_id}，恢复执行前内容')
    with store.update(pid, caller.subject) as state:
        run = store.find(state, run_id)
        run.update(status='interrupted', reason='user_stopped')
        if state['settings']['optimize']:
            state['settings']['optimize'] = False
            store.audit(state, caller.username, '自动化处理优化建议', '中止运行：开启 → 关闭')
        store.note(run, '用户中止，内容已恢复',
                   f"已恢复 {iteration_label(run['snapshot'])}，本次不生成发布候选内容。", 'wait')
    forget(caller, run)  # 内容已恢复到执行前
    return run
