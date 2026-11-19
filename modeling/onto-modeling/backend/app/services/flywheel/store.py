"""Stored flywheel state of one project and user: switches, runs and the settings change log.

Bounded on disk and in responses: at most MAX_RUNS runs with MAX_HISTORY steps each, MAX_AUDIT
setting changes, and VIEW_RUNS / VIEW_AUDIT of them per response (about 60 KiB at most).
"""
from contextlib import contextmanager
import hashlib
import time

from .. import hub_packages, knowledge_hub as transport
from ..workspace import WorkspaceError

# 连续自动处理上限（轮）；0 表示无限次：一直处理到评测集全部通过
ROUND_LIMITS = (1, 3, 5, 0)
# 后台授权时长（天）；0 表示无限制
DELEGATION_DAYS = (1, 3, 7, 30, 0)
MAX_RUNS = 300
MAX_HISTORY = 40
MAX_AUDIT = 200
MAX_SEEN = 1000
VIEW_RUNS = 50
VIEW_AUDIT = 50
# 运行记录与设置变更分批读取时每批的上限（每条运行摘要约 1 KiB）
PAGE_LIMIT = 50
TEXT_LIMIT = 300

ACTIVE = ('optimizing', 'publishing', 'evaluating')
PENDING = ('waiting_optimize', 'waiting_publish', 'waiting_evaluation', 'blocked')
# 运行记录的状态筛选，与弹窗里「全部状态」下拉框的分组一致；None 表示不筛选
RUN_FILTERS = {
    'all': None,
    'waiting': ('waiting_optimize', 'waiting_publish', 'waiting_evaluation'),
    'running': ACTIVE,
    'success': ('published', 'restored', 'passed'),
    'blocked': ('blocked', 'interrupted'),
    'rolledback': ('rolledback',),
}
# 列表与卡片需要的字段；执行过程、检查明细只在详情里返回
SUMMARY_FIELDS = (
    'id', 'kind', 'source_id', 'status', 'reason', 'note', 'error', 'mode', 'publish_mode', 'human_hold',
    'publish_hold', 'step', 'eval_run_id', 'release_id', 'version', 'revision', 'score', 'after_score',
    'suggestions', 'suggestions_error', 'received_at', 'started_at', 'published_at', 'target_version',
    'published_release_id', 'snapshot', 'restored_iteration', 'local_record_pending', 'changed', 'check_groups',
    'content_version', 'evaluation', 'trigger', 'cases', 'graph_confirmation', 'graph_confirmation_code', 'origin',
    'base_release_id', 'base_version',
)


def _path(pid, subject):
    return transport.state_dir() / 'flywheel' / transport.identifier(pid) / f'{hub_packages.scope(subject)}.json'


def _lock_key(pid, subject):
    return 'flywheel-' + hashlib.sha256(f'{pid}/{subject}'.encode()).hexdigest()[:32]


def empty():
    # baseline：首次同步只把当前版本的评测建成运行，更早的评测视为历史
    return {'settings': {'optimize': False, 'publish': False, 'max_rounds': 3, 'delegation_days': 30}, 'auto_rounds': 0,
            'baseline': False, 'seen': {}, 'next': 1, 'runs': [], 'audit': [],
            'identity': None, 'delegation': {}, 'delegation_issues': {},
            # 撤销未获对方确认的委托：句柄只用于重试撤销（grants.retry_revocations），不再用于任何调用
            'revoking': [],
            # 最近与评测服务对账的结果：连续失败次数与最近一次对账时间
            'reconcile': {'failures': 0, 'at': None}}


def _complete(value):
    defaults = empty()
    return {**defaults, **value, 'settings': {**defaults['settings'], **value.get('settings', {})}}


def load(pid, subject):
    value = transport.load(_path(pid, subject))
    return _complete(value) if isinstance(value, dict) else empty()


def all_states():
    """Every stored flywheel with its project, for the background rounds."""
    for path in (transport.state_dir() / 'flywheel').glob('*/*.json'):
        try:
            value = transport.load(path)
        except (OSError, ValueError):
            continue  # 一份读不了的状态不能挡住其他用户的飞轮
        if isinstance(value, dict):
            yield path.parent.name, _complete(value)


# 这些运行正在改动工作区，或冻结其内容发布：期间别人的飞轮不能开始优化、发布、重新检查或回退
WORKSPACE_BUSY = ('optimizing', 'publishing')
# 自动步骤遇到这个错误码只等下一轮，不转为人工确认（service._attempt）
OTHERS_WORKING_CODE = 'flywheel_others_working'
# 助手正忙时开始优化：自动步骤同样只等下一轮
ASSISTANT_BUSY_CODE = 'flywheel_assistant_busy'


def others_working_error(action):
    return WorkspaceError(f'项目中另一位成员的数据飞轮正在优化或发布，请等其完成后再{action}', 409, OTHERS_WORKING_CODE)


def holds_workspace(state):
    return any(run.get('status') in WORKSPACE_BUSY for run in state.get('runs') or [])


def others_working(pid, subject):
    """Whether another user's flywheel in this project is revising the workspace or publishing it.

    Each user's flywheel is stored apart, so one user's flywheel cannot see another's by its own state alone. Both
    end without their user: background rounds finish them even for users who left (background._caller). A candidate
    waiting for its user's confirmation does not count, as that user may never return."""
    own = _path(pid, subject)
    for path in (transport.state_dir() / 'flywheel' / transport.identifier(pid)).glob('*.json'):
        if path == own:
            continue
        try:
            value = transport.load(path)
        except (OSError, ValueError):
            continue
        # 没有身份的状态后台无法代为收尾，计入会一直挡住别人
        if isinstance(value, dict) and isinstance(value.get('identity'), dict) and holds_workspace(value):
            return True
    return False


@contextmanager
def update(pid, subject):
    """Read, change and write back the state; no network or subprocess work while holding it."""
    with transport.lock(_lock_key(pid, subject)):
        state = load(pid, subject)
        yield state
        _trim(state)
        transport.save(_path(pid, subject), state)


def _trim(state):
    state['runs'] = state['runs'][:MAX_RUNS]
    state['audit'] = state['audit'][:MAX_AUDIT]
    seen = state['seen']
    if len(seen) > MAX_SEEN:
        state['seen'] = dict(list(seen.items())[-MAX_SEEN:])


def rounds_exhausted(state):
    """Whether automatic handling has used up its consecutive rounds; never with no limit."""
    limit = state['settings']['max_rounds']
    return bool(limit) and state['auto_rounds'] >= limit


def rounds_label(limit):
    return f'{limit} 轮' if limit else '无限次'


def new_run(state, **fields):
    run = {'id': f"FW-{state['next']:03d}", 'kind': 'evaluation', 'status': 'waiting_optimize', 'reason': '',
           'note': '', 'mode': 'manual', 'human_hold': True, 'received_at': time.time(), 'history': [], **fields}
    state['next'] += 1
    state['runs'].insert(0, run)
    return run


def find(state, run_id):
    run = next((item for item in state['runs'] if item['id'] == run_id), None)
    if run is None:
        raise WorkspaceError('运行记录不存在', 404, 'flywheel_run_not_found')
    return run


def active(state):
    return next((run for run in state['runs'] if run['status'] in ACTIVE), None)


def candidate(state):
    return next((run for run in state['runs'] if run['status'] == 'waiting_publish'), None)


def note(run, title, text='', result='done'):
    run['history'].append({'title': title, 'text': str(text)[:TEXT_LIMIT], 'at': time.time(), 'result': result})
    del run['history'][:-MAX_HISTORY]


def block(run, reason, title, text='', **fields):
    run.update(status='blocked', reason=reason, **fields)
    note(run, title, text, 'fail')


def audit(state, actor, action, description):
    state['audit'].insert(0, {'at': time.time(), 'actor': actor, 'action': action, 'description': description})


def summary(run):
    return {key: run[key] for key in SUMMARY_FIELDS if key in run}


def page(rows, offset, limit):
    """One batch of a newest-first list, as the records list loads it while the user scrolls down.

    Newer entries are added at the front, so a batch read later may repeat entries of the previous one but
    never skips any; the browser drops the repeats."""
    items = rows[offset:offset + limit]
    end = offset + len(items)
    return {'items': items, 'total': len(rows), 'next_offset': end if end < len(rows) else None}
