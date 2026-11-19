"""Evaluation of a version the flywheel published, with the evaluation set of the run it came from.

After each publication the new version is evaluated exactly as the run being optimized was: same
evaluation set, data source, model and concurrency (the evaluation service's rerun). With the switch on
this starts by itself on the login the evaluation service keeps for the delegation, so it also happens
while the user is away; with the switch off the user confirms it. The result comes back as a new run,
linked to the run that published the version: the loop goes on until every case passes.
"""
import hashlib
import re
import time

from .. import eval_client, hub_packages
from ..delegated import Delegated
from ..workspace import WorkspaceError
from . import grants, intake, store

# 评测任务本身中断时（不会有评测结果到达），隔多久查一次它的状态
STATUS_POLL_SECONDS = 120
STOPPED = ('failed', 'cancelled', 'interrupted', 'waiting_auth')
# 用户本人的登录也找不到的评测任务（如已在评测页面删除）：不会再有结果
MISSING = 'missing'
RETRY = ('evaluation_failed',)
# 评测服务不能自己替换图范围里的旧数据时的错误码：只有用户本人确认后才能删除
GRAPH_CONFIRMATION = 'graph_confirmation_required'
CONFIRMATION_CODE = re.compile(r'[0-9a-f]{1,64}')
STOPPED_TEXT = {'failed': '失败', 'cancelled': '已取消', 'interrupted': '已中断', 'waiting_auth': '等待重新登录'}


def request_id(pid, run, attempt):
    """Stable for retries of one attempt, and distinct between projects: run numbers restart at FW-001 in
    every project while the evaluation service keys request ids per user, so they carry the project too."""
    project = hashlib.sha256(pid.encode()).hexdigest()[:12]
    return f"flywheel-{project}-{run['id'].lower()}-eval-{attempt}"


def source_run(state, run):
    """The evaluation run to repeat: the run's own, or the one of the run it restored."""
    if run.get('eval_run_id'):
        return run['eval_run_id']
    origin = next((item for item in state['runs'] if item['id'] == run.get('source_id')), None)
    # 用户反馈没有评测集，恢复它之前的内容后也没有可沿用的评测配置
    return origin.get('eval_run_id') if origin and not intake.is_feedback(origin) else None


def after_publish(state, run):
    """The version is in Hub: evaluate it next, by itself with the switch on, otherwise once confirmed."""
    if intake.is_feedback(run):
        # 用户反馈没有评测集可沿用：发布即本次处理完成，不让用户再确认评测
        run['status'] = 'published'
        store.note(run, '用户反馈处理完成', '用户反馈的优化不发起评测；新版本可在评测页面另行评测。')
        return
    if not source_run(state, run):
        run['status'] = 'published'
        store.note(run, '无法自动评测', '找不到可沿用的评测配置，请在评测页面评测新版本。', 'wait')
        return
    hold = not state['settings']['publish']
    run.update(status='waiting_evaluation', human_hold=hold, evaluation={'status': 'waiting'})
    if hold:
        store.note(run, '等待确认评测', '自动化确认发布与发起评测已关闭：确认后才会用同一评测集评测新版本。', 'wait')


def blocker(state, run):
    if run['status'] != 'waiting_evaluation' and not (run['status'] == 'blocked' and run['reason'] in RETRY):
        return '该运行没有待评测的版本'
    current = store.active(state)
    if current and current['id'] != run['id']:
        return '项目正在执行任务，请等待本次任务完成'
    if not source_run(state, run):
        return '找不到可沿用的评测配置'
    return ''


def start(caller, run_id, automatic, confirmation=''):
    """``confirmation``: the user confirmed deleting old graph data the evaluation service would not replace unasked,
    with the code of the refusal that showed that data (kept on the run as ``graph_confirmation_code``)."""
    pid, subject = caller.pid, caller.subject
    linked = hub_packages.association(pid) or {}
    state = store.load(pid, subject)
    run = store.find(state, run_id)
    reason = blocker(state, run)
    if reason:
        raise WorkspaceError(reason, 409, 'flywheel_blocked')
    if automatic:
        # 自动评测总是用评测服务为委托保存的登录：评测要跑很久，页面上的登录撑不到结束
        credential, refresh = grants.evaluation_login(state), ''
        if not credential:
            raise WorkspaceError('后台评测授权未就绪', 409, 'evaluation_login_missing')
    else:
        credential, refresh = caller.token, caller.refresh
    attempt = (run.get('evaluation') or {}).get('attempt', 0) + 1
    target = {'ontology_id': linked.get('ontology_id'), 'release_id': run['published_release_id'],
              'package_digest': run.get('published_digest', '')}
    try:
        value = eval_client.rerun(credential, source_run(state, run), request_id(pid, run, attempt), target, refresh,
                                  '' if automatic else confirmation)
    except WorkspaceError as exc:
        if exc.code == GRAPH_CONFIRMATION:
            # 图范围里的旧数据不是上一轮评测原样导入的：记下说明与确认码，确认评测时请用户一并确认删除
            with store.update(pid, subject) as state:
                store.find(state, run_id).update(graph_confirmation=str(exc)[:store.TEXT_LIMIT],
                                                 graph_confirmation_code=_confirmation_code(exc))
        raise
    with store.update(pid, subject) as state:
        run = store.find(state, run_id)
        now = time.time()
        _forget_confirmation(run)
        run.update(status='evaluating', reason='', error='', human_hold=False, step=1,
                   evaluation={'status': 'running', 'run_id': value['id'], 'attempt': attempt, 'requested_at': now,
                               'checked_at': now, 'mode': 'auto' if automatic else 'manual'})
        store.note(run, '自动发起评测' if automatic else '人工确认评测',
                   f"使用同一评测集评测 {intake.version_label(run.get('target_version'))}，评测任务 {value['id'][:8]}。", 'active')
        return run


def _confirmation_code(exc):
    code = (getattr(exc, 'details', None) or {}).get('confirmation_code')
    return code if isinstance(code, str) and CONFIRMATION_CODE.fullmatch(code) else ''


def _forget_confirmation(run):
    run.pop('graph_confirmation', None)
    run.pop('graph_confirmation_code', None)


def due(run, own_login=True):
    """Whether to check the run's evaluation now. A background check that read nothing does not put off the user's
    own: only the user's login can tell that the evaluation is gone (status)."""
    if run['status'] != 'evaluating':
        return False
    evaluation = run['evaluation']
    last = evaluation.get('checked_at', 0)
    if not own_login:
        last = max(last, evaluation.get('background_checked_at', 0))
    return time.time() - last >= STATUS_POLL_SECONDS


def status(credential, run):
    """The evaluation service's status of the run's evaluation; None when it cannot be read now.

    MISSING when the user's own login cannot find it. A delegation that cannot find it proves nothing (it
    reads one package only), so that waits for the user's next visit."""
    try:
        value = eval_client.call('GET', f"/runs/{run['evaluation']['run_id']}", credential)
    except WorkspaceError as exc:
        if exc.code == 'eval_not_found' and not isinstance(credential, Delegated):
            return MISSING
        return None
    return value.get('status') if isinstance(value, dict) else None


def settle(run, state_of_run, own_login=True):
    """Record what the status check found; a finished evaluation is completed by its result arriving."""
    if state_of_run is None and not own_login:
        run['evaluation']['background_checked_at'] = time.time()
        return
    run['evaluation']['checked_at'] = time.time()
    if state_of_run == MISSING:
        run['evaluation']['status'] = MISSING
        store.block(run, 'evaluation_failed', '评测任务已不存在',
                    '评测服务中已找不到这次评测（可能已在评测页面删除），不会再有结果。可确认后重新评测，或跳过评测。')
    elif state_of_run in STOPPED:
        run['evaluation']['status'] = state_of_run
        store.block(run, 'evaluation_failed', '评测未完成',
                    f"评测任务状态：{STOPPED_TEXT[state_of_run]}。可在评测页面查看原因，确认后重新评测。")


def finish(run, entry, passed):
    """The result of the version's evaluation arrived; the run is done, the result goes on as a new run."""
    cases = entry['summary']
    _forget_confirmation(run)
    run.update(status='published', step=4, reason='')
    run['evaluation'] = {**(run.get('evaluation') or {}), 'status': 'done', 'run_id': entry['run_id'],
                         'score': intake.strict_rate(entry), 'passed': passed}
    remaining = cases['total'] - cases['pass']
    store.note(run, '评测完成', f"严格通过率 {intake.rate_label(intake.strict_rate(entry))}，"
               + ('全部用例通过。' if passed else f"{remaining} 个用例未通过，结果转入下一轮。"))


def skip(run):
    """Also ends waiting for an evaluation that is running: the evaluation service keeps it, and its result
    arrives later as a new run like that of any evaluation of the version."""
    waiting = run['status'] == 'evaluating'
    _forget_confirmation(run)
    run.update(status='published', reason='', human_hold=True)
    run['evaluation'] = {**(run.get('evaluation') or {}), 'status': 'skipped'}
    if waiting:
        store.note(run, '不再等待评测', '评测服务中的这次评测不受影响：之后有结果时照常进入数据飞轮。', 'wait')
    else:
        store.note(run, '跳过评测', '用户选择不评测该版本。', 'wait')
