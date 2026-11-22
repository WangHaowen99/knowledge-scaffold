"""Hub publication of a flywheel candidate, in a worker thread holding the confirming user's login.

Same orchestration as the web publication (hub_packages.publish), pinned to the candidate's ontology
revision: content changed after the checks is not published. The request ID is fixed per candidate
attempt, so a retry continues the same Hub request and never creates a second version. A user's login
stays in memory for this one publication only; background rounds publish on the Hub registration, each
request signed anew (hub_packages.delegated_call).
"""
import logging
import threading
import time

from .. import hub_packages, package_evaluations
from ..solver import service as solver_service
from ..workspace import WorkspaceError
from . import checks, evaluation, grants, intake, optimize, store

_log = logging.getLogger(__name__)
RETRYABLE = ('hub_failed', 'publish_interrupted', 'hub_expired', 'request_conflict', 'authorization',
             'solver_unverified')
# 发布时因内容本身被拒（阶段产出、数据绑定、脚本契约、脚本输出未验证）：重试同一请求没用，改好内容并重新检查后再发布
PUBLISH_CHECKS = ('modeling_stages_incomplete', 'binding_problems', 'script_binding_conflict', 'script_argument_shape',
                  'script_output_unverified', 'package_structure_invalid', 'release_tree_unsafe', 'invalid_commit')
# 这些失败需要新的发布请求：原请求已过期，或同一请求 ID 已用于不同内容
RENEW = ('hub_expired', 'request_conflict')
# 飞轮不代用户确认求解能力尚未验证（网页与 MCP 发布由用户确认）：只发布已通过求解验证的求解契约修订
SOLVER_UNVERIFIED = ('solver_unverified', '求解能力尚未验证',
                     '当前求解契约修订尚未通过求解验证，飞轮不代为确认，未创建新版本。修订未改动求解契约时，可先在'
                     '「已发布版本（知识包评测）」对当前版本完成求解验证后重试发布；否则请在发布检查中确认后手动发布。')
STEPS = {'frozen': (2, '发布条件复核通过', '内容未漂移，全部必需检查通过。'),
         'uploading': (2, '上传知识包', '上传本次冻结的知识包内容。'),
         'committing': (3, '等待 Hub 回执', '按同一发布请求确认结果，防止重复创建版本。')}
_workers = {}
_workers_guard = threading.Lock()


def request_id(run):
    return f"flywheel-{run['id'].lower()}-{run['candidate']['attempt']}"


def earlier_changes(run, linked):
    """Whether publishing this candidate also releases edits made before the optimization and never published.

    None when it cannot be told: the last release or the run predates the recorded content fingerprints."""
    if run.get('kind') == 'rollback' or not run.get('candidate'):
        return None
    published, before = (linked or {}).get('inputs_digest'), run.get('base_inputs')
    return None if not published or not before else published != before


def publishable(run):
    return bool(run.get('candidate')) and (
        run['status'] == 'waiting_publish' or (run['status'] == 'blocked' and run['reason'] in RETRYABLE))


def blocker(state, run, linked):
    if not publishable(run):
        return '该运行没有待发布的候选内容'
    if intake.base_release(run) != linked.get('release_id'):
        return (f"候选内容基于 {intake.version_label(run.get('base_version') or run.get('version'))}，当前项目版本为 "
                f"{intake.version_label(linked.get('version'))}，请评测当前版本后再优化")
    current = store.active(state)
    if current and current['id'] != run['id']:
        return '项目正在执行任务，请等待本次任务完成'
    return ''


def alive(pid, subject, run_id):
    with _workers_guard:
        worker = _workers.get((pid, subject, run_id))
    return bool(worker and worker.is_alive())


def wait(pid, subject, run_id, timeout=None):
    with _workers_guard:
        worker = _workers.get((pid, subject, run_id))
    if worker:
        worker.join(timeout)


def reap(state, pid, subject):
    """A publication without a live worker was cut off (restart); it can be retried as the same request."""
    for run in state['runs']:
        if run['status'] == 'publishing' and not alive(pid, subject, run['id']):
            store.block(run, 'publish_interrupted', '上次发布未完成',
                        '发布过程中断，尚未确认 Hub 回执。确认后重试同一发布请求。')


def _frozen(pid, subject, rid):
    return (hub_packages.directory(pid, subject) / 'requests' / rid / 'task.json').is_file()


def _recheck(manager, pid, run):
    """校验发布条件: the candidate's content is unchanged and the quick gates still pass."""
    try:
        revision = hub_packages.content_revision(manager.project_root(pid))
    except WorkspaceError:
        revision = ''
    if revision != run['candidate']['revision']:
        return 'content_changed', '候选内容已变化', '候选内容生成后工作区又有修改，未创建新版本。请重新检查后再发布。'
    failed = [item for item in checks.quick(manager, pid) if not item['passed']]
    if failed:
        return 'checks_failed', '发布条件复核未通过', '未通过：' + '、'.join(item['label'] for item in failed) + '。'
    return None


def _verified_solver(manager, pid, subject):
    """The solver revision to publish on: '' without enabled solver contracts, None when it is not verified."""
    check = solver_service.solver_check(manager, pid)
    if not check.needs_validation:
        return ''
    return check.revision if package_evaluations.verified_solver_release(pid, subject, check.revision) else None


def start(caller, run_id, automatic):
    # 与优化的开始共用一把锁：从检查别人的飞轮到记为发布中，别人不能开始优化
    with optimize.handoff(caller.pid):
        return _start(caller, run_id, automatic)


def _start(caller, run_id, automatic):
    manager, pid, subject = caller.manager, caller.pid, caller.subject
    linked = hub_packages.association(pid) or {}
    others = store.others_working(pid, subject)
    with store.update(pid, subject) as state:
        run = store.find(state, run_id)
        reason = blocker(state, run, linked)
        if reason:
            raise WorkspaceError(reason, 409, 'flywheel_blocked')
        if others:
            # 在别人优化或发布中途发布：要么发出它改到一半的内容，要么让它的修订建在过时的版本上
            raise store.others_working_error('发布')
        if run['reason'] in RENEW:
            run['candidate']['attempt'] += 1
        rid = request_id(run)
        candidate = dict(run['candidate'])
    # 已冻结的请求沿用冻结内容，复核与求解验证在冻结前已经做过
    frozen = _frozen(pid, subject, rid)
    problem = None if frozen else _recheck(manager, pid, run)
    verified = '' if frozen or problem else _verified_solver(manager, pid, subject)
    if verified is None:
        problem = SOLVER_UNVERIFIED
    with store.update(pid, subject) as state:
        run = store.find(state, run_id)
        if problem:
            store.block(run, *problem)
            return run
        run.update(status='publishing', step=0, reason='', error='', publish_mode='auto' if automatic else 'manual',
                   publish_request_id=rid)
        run.pop('hub_error', None)
        store.note(run, '自动发布：冻结待发布内容' if automatic else '人工确认发布：冻结待发布内容',
                   '发布目标为新的正式版本，使用本次冻结内容和固定发布请求。', 'active')
        result = dict(run)
    worker = threading.Thread(target=_work, name=f'flywheel-publish-{run_id}', daemon=True,
                              args=(manager, pid, subject, caller.hub_token, caller.hub_delegation, caller.username,
                                    run_id, rid, candidate['revision'], verified))
    with _workers_guard:
        _workers[(pid, subject, run_id)] = worker
    worker.start()
    return result


def _failure(run, exc):
    code = getattr(exc, 'code', '') or ''
    status = getattr(exc, 'status', 500)
    message = str(exc)[:store.TEXT_LIMIT] if isinstance(exc, WorkspaceError) else '发布过程出现未知错误'
    run.update(error=message, hub_error=getattr(exc, 'hub_error', None) or {})
    if code in ('revision_conflict', 'publish_input_changed', 'revision_unavailable'):
        store.block(run, 'content_changed', '候选内容已变化', '冻结时发现内容与检查时不一致，未创建新版本。请重新检查后再发布。')
    elif code == 'solver_validation_acknowledgement_required':
        # 复核之后、冻结之前求解契约又改了：新修订没有验证依据
        store.block(run, *SOLVER_UNVERIFIED)
    elif code in PUBLISH_CHECKS or code.startswith('solver_'):
        store.block(run, 'checks_failed', '发布条件复核未通过', message)
    elif code == 'publish_request_conflict':
        store.block(run, 'request_conflict', '发布请求冲突', '同一发布请求已用于其他内容，重试将创建新的发布请求。')
    elif status == 410:
        store.block(run, 'hub_expired', 'Hub 发布请求已过期', '原发布请求已过期，重试将按候选内容创建新的发布请求。')
    elif status in (401, 403):
        store.block(run, 'authorization', '需要重新授权', 'Hub 未接受当前登录，请重新登录后重试同一发布请求。')
    else:
        store.block(run, 'hub_failed', '发布失败，等待人工处理',
                    '未收到 Hub 成功回执。本次冻结内容和发布请求已保留，重试将继续同一发布请求。')


def _work(manager, pid, subject, token, delegated, username, run_id, rid, revision, verified):
    def progress(step):
        index, title, text = STEPS[step]
        with store.update(pid, subject) as state:
            run = store.find(state, run_id)
            run['step'] = index
            store.note(run, title, text)

    try:
        receipt, failure = None, None
        try:
            receipt = hub_packages.publish(manager, pid, rid, token, subject, False, delegated=delegated,
                                           expected_revision=revision, verified_revision=verified,
                                           author=username, progress=progress)
        except Exception as exc:  # 任何失败都让运行停在可重试的阻断态
            if not isinstance(exc, WorkspaceError):
                _log.warning('flywheel publication failed', exc_info=True)
            failure = exc
        with store.update(pid, subject) as state:
            run = store.find(state, run_id)
            if failure is not None:
                _failure(run, failure)
                if delegated and run['reason'] == 'authorization':
                    grants.reject(state, 'hub', username, delegated['token_id'])
                return
            run.update(status='published', step=4, reason='', target_version=receipt['version'],
                       published_release_id=receipt['release_id'], published_digest=receipt.get('package_digest', ''),
                       published_committed_at=receipt.get('committed_at', ''), published_at=time.time(),
                       local_record_pending=bool(receipt.get('local_record_pending')))
            store.note(run, 'Hub 已确认发布', f"正式版本 {intake.version_label(receipt['version'])} 已创建，旧版本保留。")
            intake.expire(state, receipt['release_id'], receipt.get('committed_at', ''))
            evaluation.after_publish(state, run)
    finally:
        with _workers_guard:
            if _workers.get((pid, subject, run_id)) is threading.current_thread():
                del _workers[(pid, subject, run_id)]
