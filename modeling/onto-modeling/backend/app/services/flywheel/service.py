"""Flywheel orchestration: sync with the evaluation results, advance runs, start automatic steps.

Each sync, triggered while the user has the project open, takes in new evaluation results, settles
the assistant turn of an optimizing run, checks its result, and starts the next automatic step with
that user's login. Actions of one user and project run one at a time.
"""
import os
import threading
import time

from .. import audit as audit_log, eval_client, hub_packages, package_evaluations
from ..delegated import Delegated
from ..workspace import WorkspaceError
from . import evaluation, grants, intake, optimize, publish, snapshots, store

_guards = {}
_guards_guard = threading.Lock()
ACTION_WAIT_SECONDS = 30
# 一次同步最多回写几条（各要两次评测服务请求），其余留给下次同步
WRITEBACKS_PER_SYNC = 3
# 连续几次对账失败才提示评测服务不可用：偶发一次（服务或账号校验一时没响应）不提示
RECONCILE_FAILURES = 2
CHANGE_ACTIONS = {'optimize': '自动化处理优化建议', 'publish': '自动化确认发布与发起评测'}


def _guard(caller):
    with _guards_guard:
        return _guards.setdefault((caller.pid, caller.subject), threading.Lock())


def _blocked_action():
    return WorkspaceError('飞轮正在处理上一个操作，请稍后重试', 409, 'flywheel_busy')


# ---- 视图 ----


def unavailable(message):
    return {'available': False, 'message': message}


def _context(caller):
    linked = hub_packages.association(caller.pid) or {}
    meta = caller.manager.get_project(caller.pid)
    return {'linked': linked, 'name': meta.get('name') or caller.pid,
            'acted_for': grants.acts_for(caller.manager, meta, caller.username)}


def view(caller, state=None, overview=None, context=None):
    context = context or _context(caller)
    state = state or store.load(caller.pid, caller.subject)
    linked, overview = context['linked'], overview or {}
    message = ''
    if not eval_client.configured():
        message = '未配置评测服务地址，无法接收评测反馈'
    elif not linked:
        message = '项目尚未发布到 Hub：发布首个正式版本并完成评测后，飞轮才会开始流转'
    evaluating = overview.get('active') or {}
    runs = state['runs']
    current, ready = store.active(state), store.candidate(state)
    return {
        'available': True, 'message': message, 'can_write': caller.can_write,
        'project': {'id': caller.pid, 'name': context['name']},
        'package': {'ontology_id': linked.get('ontology_id'), 'release_id': linked.get('release_id'),
                    'version': linked.get('version')},
        'evaluation': {'configured': eval_client.configured(), 'reachable': _reachable(state),
                       'notifications': bool(os.environ.get('ONTO_EVALUATION_SERVICE_TOKEN')),
                       'evaluating': evaluating.get('release_id') == linked.get('release_id') and bool(evaluating)},
        'settings': state['settings'], 'auto_rounds': state['auto_rounds'],
        'delegation': grants.summary(state, bool(linked), context['acted_for']),
        'assistant_busy': caller.assistant.busy(caller.pid),
        'active_id': current['id'] if current else None, 'candidate_id': ready['id'] if ready else None,
        'pending': sum(1 for run in runs if run['status'] in store.PENDING),
        'runs': [_summary(run, linked) for run in runs[:store.VIEW_RUNS]], 'total': len(runs),
        'audit': state['audit'][:store.VIEW_AUDIT],
    }


def _reachable(state):
    """Whether the evaluation service answered the latest reconciliations; None before the first one.

    Kept in the state, so a sync that does not reconcile shows the last known answer instead of hiding it."""
    reconciled = state['reconcile']
    if reconciled['failures'] >= RECONCILE_FAILURES:
        return False
    return True if reconciled['at'] else None


def runs_page(caller, offset, limit, group):
    """Run summaries newest first, optionally of one status group; at most store.PAGE_LIMIT per batch."""
    statuses = store.RUN_FILTERS[group]
    runs = [run for run in store.load(caller.pid, caller.subject)['runs'] if statuses is None or run['status'] in statuses]
    batch = store.page(runs, offset, limit)
    return {**batch, 'items': [store.summary(run) for run in batch['items']]}


def audit_page(caller, offset, limit):
    """Setting changes newest first; at most store.PAGE_LIMIT per batch."""
    return store.page(store.load(caller.pid, caller.subject)['audit'], offset, limit)


def _summary(run, linked):
    value = store.summary(run)
    if publish.publishable(run):
        value['earlier_changes'] = publish.earlier_changes(run, linked)
    return value


def detail(caller, run_id):
    run = store.find(store.load(caller.pid, caller.subject), run_id)
    return {**store.summary(run), 'history': run['history'], 'checks': run.get('checks'),
            'candidate': run.get('candidate'), 'report': run.get('report'), 'hub_error': run.get('hub_error'),
            'conversation_id': run.get('conversation_id')}


def changes(caller, run_id):
    run = store.find(store.load(caller.pid, caller.subject), run_id)
    snapshot = run.get('snapshot')
    if not snapshot:
        return {'base': None, 'head': None, 'files': [], 'total': 0, 'truncated': False}
    head = (run.get('candidate') or {}).get('head')
    # 优化进行中或尚未形成候选内容时对比工作区，之后对比候选内容的迭代
    if run['status'] == 'optimizing' or not head or head == snapshot['commit']:
        head = None
    return snapshots.changes(caller.manager, caller.pid, snapshot['commit'], head)


# ---- 同步与自动执行 ----


def sync(caller, reconcile=False):
    """One read by the signed-in user: advance the flywheel, keep the delegations current, report."""
    overview = advance(caller, reconcile)
    return view(caller, overview=overview, context=_context(caller))


def advance(caller, reconcile=False):
    """Advance the flywheel with the caller's credentials; shared by user requests and background rounds."""
    context = _context(caller)
    if not context['linked'] or not eval_client.configured():
        return None
    guard = _guard(caller)
    if not guard.acquire(blocking=False):
        return None  # 另一请求正在推进本飞轮
    try:
        grants.align(caller)
        return _advance(caller, context, reconcile)
    finally:
        guard.release()


def _advance(caller, context, reconcile):
    pid, subject, release = caller.pid, caller.subject, context['linked'].get('release_id')
    # 还不知道自己在评测服务的所有者标识时，只有对账能读到评测结果
    refresh = bool(caller.token) and (reconcile or package_evaluations._owner_of(subject) is None)
    overview = package_evaluations.overview(pid, subject, caller.token, refresh)
    # 后台没有评测服务登记时读不到建议摘要：新结果留给有凭据的同步
    entries = intake.unseen(store.load(pid, subject), overview) if caller.token else []
    summaries = intake.summaries(pid, subject, caller.token, entries, release) if entries else {}
    with store.update(pid, subject) as state:
        if overview.get('reachable') is not None:
            failures = 0 if overview['reachable'] else state['reconcile']['failures'] + 1
            state['reconcile'] = {'failures': failures, 'at': time.time()}
        intake.take_in(state, entries, release, summaries)
        intake.expire(state, release, context['linked'].get('committed_at', ''))
        passed = intake.close_passed(state, release)
        publish.reap(state, pid, subject)
        running = next((run for run in state['runs'] if run['status'] == 'optimizing'), None)
        due = running and optimize.settle(
            running, caller.assistant.outcome(pid, running.get('conversation_id'), running.get('history_start', 0)))
        hold = not state['settings']['publish']
    for run in passed:
        optimize.forget(caller, run)
    if running and running['status'] == 'blocked':
        optimize.end_revision(caller, running)  # 助手没有正常结束本轮，修订已停
    _watch(caller)
    if due:
        try:
            optimize.check(caller, running, hold)
        except WorkspaceError as exc:
            # 检查本身没跑完：停在可“重新检查”的阻断态，不让每次同步都重复失败
            with store.update(pid, subject) as state:
                store.block(store.find(state, running['id']), 'checks_failed', '发布检查未能完成', str(exc),
                            error=str(exc)[:store.TEXT_LIMIT])
            optimize.end_revision(caller, running)  # 修订已结束，只是检查没跑完
    _write_back(caller)
    if caller.can_write:
        _automate(caller, release)
    return overview


def _write_back(caller):
    """Tell the evaluation service about feedback the latest version already resolves.

    Writing back takes the user's own login: background rounds leave it to the user's next visit."""
    if not caller.present:
        return
    due = [run for run in store.load(caller.pid, caller.subject)['runs'] if intake.resolved_unreported(run)]
    results = {}
    for run in due[:WRITEBACKS_PER_SYNC]:
        try:
            package_evaluations.report_resolved(caller.pid, caller.subject, caller.token, run['eval_run_id'],
                                                intake.base_release(run) or '', run.get('base_version') or '')
            results[run['id']] = ('done', '')
        except WorkspaceError as exc:
            if exc.status in (404, 409, 422):
                results[run['id']] = ('refused', str(exc)[:store.TEXT_LIMIT])  # 评测服务不接受：不再重试
    if not results:
        return
    with store.update(caller.pid, caller.subject) as state:
        for run_id, (status, message) in results.items():
            run = store.find(state, run_id)
            run['writeback'] = {'status': status, 'message': message, 'at': time.time()}
            if status == 'done':
                store.note(run, '已告知评测服务', '评测服务「用户反馈」中这一轮记为已处理：最新版本已解决。')
            else:
                store.note(run, '评测服务未接受处理结果', message, 'fail')


def _watch(caller):
    """An evaluation that stopped without a result would wait forever: check its status now and then."""
    if not caller.token:
        return
    own_login = not isinstance(caller.token, Delegated)
    watched = [run for run in store.load(caller.pid, caller.subject)['runs'] if evaluation.due(run, own_login)]
    found = {run['id']: evaluation.status(caller.token, run) for run in watched}
    if not found:
        return
    with store.update(caller.pid, caller.subject) as state:
        for run_id, value in found.items():
            run = store.find(state, run_id)
            if run['status'] == 'evaluating':
                evaluation.settle(run, value, own_login)


def _automate(caller, release):
    pid, subject = caller.pid, caller.subject
    state = store.load(pid, subject)
    settings = state['settings']
    if store.active(state) or not caller.fresh():
        return
    pending = next((run for run in state['runs'] if run['status'] == 'waiting_evaluation' and not run.get('human_hold')), None)
    if pending:
        if settings['publish'] and grants.evaluation_login(state):
            _attempt(caller, evaluation.start, pending, 'evaluate', 'human_hold', 'evaluation')
        else:
            with store.update(pid, subject) as state:
                current = store.find(state, pending['id'])
                current['human_hold'] = True
                store.note(current, '等待确认评测', '后台评测授权未就绪：确认后用你的登录发起评测。', 'wait')
        return
    ready = store.candidate(state)
    if ready:
        due = (caller.can_publish and settings['publish'] and not ready.get('publish_hold')
               and intake.base_release(ready) == release)
        # 别人的飞轮正在修订或发布工作区：等它结束再发布，不转为人工确认
        if due and not store.others_working(pid, subject):
            _attempt(caller, publish.start, ready, 'publish', 'publish_hold', 'hub')
        return
    queued = next((run for run in reversed(state['runs'])
                   if run['status'] == 'waiting_optimize' and not run.get('human_hold')), None)
    if not caller.token or not queued:
        return
    if caller.assistant.busy(pid) or store.others_working(pid, subject):
        return  # 别人的飞轮还在用工作区：等它结束，不转为人工确认
    if not intake.is_feedback(queued) and store.rounds_exhausted(state):
        with store.update(pid, subject) as state:
            store.find(state, queued['id']).update(human_hold=True, reason='round_limit')
        return
    _attempt(caller, optimize.start, queued, 'optimize', 'human_hold', 'evaluation')


def _attempt(caller, step, run, name, hold, delegate):
    """Automatic steps leave a failed run waiting for a confirmation instead of retrying forever."""
    audit_log.log(caller.username, 'FLYWHEEL', f'/api/projects/{caller.pid}/flywheel/runs/{run["id"]}/{name}')
    try:
        step(caller, run['id'], True)
    except WorkspaceError as exc:
        if exc.code in (store.OTHERS_WORKING_CODE, store.ASSISTANT_BUSY_CODE):
            return  # 检查之后、拿到锁之前，别人的飞轮或对话开始占用工作区与助手：下一轮再试
        with store.update(caller.pid, caller.subject) as state:
            current = store.find(state, run['id'])
            current.update({hold: True, 'error': str(exc)[:store.TEXT_LIMIT]})
            store.note(current, '自动执行未开始', f'{exc}；已转为人工确认。', 'wait')
            if exc.code in eval_client.LOGIN_CODES:
                grants.lose_login(state, caller.username)
            elif not caller.present and exc.status in (401, 403):
                grants.reject(state, delegate, caller.username, grants.key_of(caller, delegate))


# ---- 用户操作 ----


def _locked(caller, action):
    guard = _guard(caller)
    if not guard.acquire(timeout=ACTION_WAIT_SECONDS):
        raise _blocked_action()
    try:
        return action()
    finally:
        guard.release()


def update_settings(caller, changes):
    def apply():
        with store.update(caller.pid, caller.subject) as state:
            settings = state['settings']
            for key, label in CHANGE_ACTIONS.items():
                value = changes.get(key)
                if value is None or value == settings[key]:
                    continue
                settings[key] = value
                store.audit(state, caller.username, label,
                            f"{'关闭 → 开启' if value else '开启 → 关闭'}；已有待确认事项保持不变")
                _released(state, key, value)
            limit = changes.get('max_rounds')
            if limit is not None and limit != settings['max_rounds']:
                if limit not in store.ROUND_LIMITS:
                    raise WorkspaceError('连续自动处理上限只能是 1、3、5 轮或无限次', 422)
                store.audit(state, caller.username, '自动优化规则',
                            f"连续自动处理上限：{store.rounds_label(settings['max_rounds'])} → {store.rounds_label(limit)}")
                settings['max_rounds'] = limit
            days = changes.get('delegation_days')
            if days is not None and days != settings['delegation_days']:
                if days not in store.DELEGATION_DAYS:
                    raise WorkspaceError('后台授权有效期只能是 1、3、7、30 天或无限制', 422)
                store.audit(state, caller.username, '后台授权',
                            f"有效期：{grants.label(settings['delegation_days'])} → {grants.label(days)}")
                settings['delegation_days'] = days
        grants.align(caller)
    _locked(caller, apply)
    return view(caller)


def _released(state, key, enabled):
    """Turning a switch off stops later automatic steps; items already waiting stay as they are."""
    if enabled:
        return
    for run in state['runs']:
        if key == 'optimize' and run['status'] == 'waiting_optimize' and not run.get('human_hold'):
            run.update(human_hold=True, reason='automation_off')
        if key == 'publish' and run['status'] == 'waiting_publish':
            run['publish_hold'] = True
        if key == 'publish' and run['status'] == 'waiting_evaluation':
            run['human_hold'] = True


def confirm_optimize(caller, run_id):
    _locked(caller, lambda: optimize.start(caller, run_id, False))
    return view(caller)


def confirm_publish(caller, run_id):
    _locked(caller, lambda: publish.start(caller, run_id, False))
    return view(caller)


def confirm_evaluate(caller, run_id, confirmation=''):
    _locked(caller, lambda: evaluation.start(caller, run_id, False, confirmation))
    return view(caller)


def skip_evaluation(caller, run_id):
    def apply():
        with store.update(caller.pid, caller.subject) as state:
            run = store.find(state, run_id)
            # 评测中的也能不再等待：评测任务被删除或迟迟没有结果时，这是离开“评测中”的出口
            if run['status'] not in ('waiting_evaluation', 'evaluating') and not (
                    run['status'] == 'blocked' and run['reason'] in evaluation.RETRY):
                raise WorkspaceError('该运行没有待评测的版本', 409, 'flywheel_blocked')
            evaluation.skip(run)
    _locked(caller, apply)
    return view(caller)


def stop(caller, run_id):
    def apply():
        optimize.stop(caller, run_id)
        grants.align(caller)  # 中止会关闭自动优化：撤销评测服务登记
    _locked(caller, apply)
    return view(caller)


IGNORABLE = ('version_mismatch', 'suggestions_failed', 'no_changes', 'optimization_failed',
             'optimization_interrupted', 'assistant_unavailable')


def ignore(caller, run_id):
    def apply():
        with store.update(caller.pid, caller.subject) as state:
            run = store.find(state, run_id)
            if not (run['status'] in ('waiting_optimize', 'interrupted')
                    or (run['status'] == 'blocked' and run['reason'] in IGNORABLE)):
                raise WorkspaceError('该运行不能忽略', 409, 'flywheel_blocked')
            run.update(status='ignored', reason='user_ignored', human_hold=True)
            store.note(run, '用户忽略本次建议', '未修改知识包，未触发发布。', 'wait')
        optimize.forget(caller, run)
    _locked(caller, apply)
    return view(caller)


def recheck(caller, run_id):
    def apply():
        run = store.find(store.load(caller.pid, caller.subject), run_id)
        if not (run['status'] == 'blocked' and run['reason'] in ('checks_failed', 'content_changed')):
            raise WorkspaceError('该运行不需要重新检查', 409, 'flywheel_blocked')
        current = store.candidate(store.load(caller.pid, caller.subject))
        if current and current['id'] != run_id:
            raise WorkspaceError('请先处理当前候选内容', 409, 'flywheel_blocked')
        if store.others_working(caller.pid, caller.subject):
            # 检查会把工作区提交为候选内容：别人改到一半的内容不能算进来
            raise store.others_working_error('重新检查')
        optimize.check(caller, run, True)
    _locked(caller, apply)
    return view(caller)


ROLLBACK_FROM = ('published', 'waiting_publish', 'waiting_evaluation', 'blocked', 'passed')


def rollback(caller, run_id, scope):
    if scope not in ('workspace', 'hub'):
        raise WorkspaceError('回退范围无效', 422)

    def apply():
        pid, subject = caller.pid, caller.subject
        # 从检查别人的飞轮到恢复内容，别人不能开始优化或发布：恢复不能抹掉别人正在进行的修订
        with optimize.handoff(pid):
            with store.update(pid, subject) as state:
                run = store.find(state, run_id)
                if not run.get('snapshot') or run['status'] not in ROLLBACK_FROM:
                    raise WorkspaceError('该运行没有可回退的执行前快照', 409, 'flywheel_blocked')
                if store.active(state):
                    raise WorkspaceError('请等待当前运行结束后再回退', 409, 'flywheel_blocked')
                if store.others_working(pid, subject):
                    raise store.others_working_error('回退')
                # 助手这一轮改的文件还没提交：恢复快照会把它们一并抹掉，且无法找回
                if caller.assistant.busy(pid):
                    raise WorkspaceError('建模助手正在对话中修改内容，请等本轮对话结束后再回退', 409, 'flywheel_blocked')
                snapshot = run['snapshot']
            restored = snapshots.restore(caller.manager, pid, snapshot['commit'], f'flywheel: 回退 {run_id} 到执行前内容')
            # 快照之后的修改都已撤回：之后的发布（包括恢复版本）不是这期间评测优化的处理结果
            package_evaluations.forget_after_rollback(pid)
        linked = hub_packages.association(pid) or {}
        with store.update(pid, subject) as state:
            _pause(state, caller.username)
            run = store.find(state, run_id)
            for other in state['runs']:
                if other['status'] == 'waiting_publish' and other['id'] != run_id:
                    other.update(status='ignored', reason='replaced_by_rollback')
                    store.note(other, '候选内容已失效', '执行了人工内容回退，本次候选内容保留在历史中。', 'wait')
            run.update(previous_status=run['status'], status='rolledback', rollback_scope=scope)
            store.note(run, '已执行人工回退', f"恢复 {optimize.iteration_label(snapshot)}；"
                       + ('通过检查后将创建新的恢复版本。' if scope == 'hub' else '只恢复建模内容，Hub 正式版本保留。'))
            back = store.new_run(state, kind='rollback', source_id=run_id, status='restored', human_hold=True,
                                 release_id=linked.get('release_id'), version=linked.get('version'),
                                 content_version=run.get('version'), restored_iteration=restored['iteration'])
            store.note(back, '恢复建模内容', f"内容恢复到 {optimize.iteration_label(snapshot)}，"
                       f"生成新迭代 iteration-{restored['iteration']:03d}。")
            store.note(back, '两个自动化开关已暂停', '避免恢复的内容立即被再次优化或发布。')
            store.audit(state, caller.username, '内容回退', f"{run_id} → {optimize.iteration_label(snapshot)}；"
                        + ('检查后发布新的恢复版本' if scope == 'hub' else '仅恢复建模内容'))
        grants.align(caller)  # 回退会关闭两个开关：撤销对应登记
        if scope == 'hub':
            checked = optimize.check(caller, back, True)
            if checked['status'] == 'waiting_publish':
                publish.start(caller, back['id'], False)
    _locked(caller, apply)
    return view(caller)


def _pause(state, actor):
    settings = state['settings']
    for key, label in CHANGE_ACTIONS.items():
        if settings[key]:
            settings[key] = False
            store.audit(state, actor, label, '回退操作：开启 → 关闭')
            _released(state, key, False)
    state['auto_rounds'] = 0
