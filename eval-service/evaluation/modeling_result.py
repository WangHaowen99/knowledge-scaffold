"""How modeling handled a run's optimization suggestions, written back after it published a new version.

A run's ``optimization`` starts when its report is first generated: runs of the evaluation wait for modeling
(``notified``), runs built from consumer feedback first wait for a package owner to confirm them. Modeling
reports a new release or a failure for the run whose suggestions it applied; the release is checked with Hub
before it is recorded. Recording never starts an evaluation: verifying the new version is a new run.
"""
import time
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from .core import Failure

# States in which modeling may report a result; a later report replaces the earlier one, which goes to history.
REPORTABLE = ('notified', 'modeling_failed', 'modeling_done')
HISTORY = 10


class ModelingResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')
    outcome: str = Field(pattern=r'^(done|failed)$')
    release_id: str = Field(default='', max_length=128, pattern=r'^(|[A-Za-z0-9_-]+)$')
    note: str = Field(default='', max_length=2000)


def applies(run):
    """Solver validations end in deterministic guidance: there are no suggestions for modeling to handle."""
    return run.get('run_type') != 'solver'


def initial(run):
    return 'awaiting_review' if run.get('origin') == 'consumer_feedback' else 'notified'


def state(store, jobs, owner, run):
    """The stored state, or for runs from before it was kept, ``notified`` once their report exists."""
    if not applies(run):
        return None
    value = store.optimization(run['id'])
    if value is None and run.get('origin') != 'consumer_feedback':
        meta = jobs.meta(owner, run['id'])
        if meta and meta.get('report') == 'ready':
            value = {'status': 'notified', 'updated_at': meta.get('generated_at')}
    return value


def started(store, owner, run, now=None):
    """Create the state when a run's report is first generated; an existing state is kept."""
    if not applies(run):
        return None
    return store.put_optimization(owner, run['id'], {'status': initial(run), 'updated_at': now or time.time()}, only_new=True)


def record(store, jobs, packages, owner, run, body, token, actor):
    """Validate a result modeling reported for the run and store it; returns the new state."""
    if not applies(run):
        raise Failure('not_applicable', '求解验证没有优化建议，不需要回写处理结果', 409)
    current = state(store, jobs, owner, run) or {}
    note = body.note.strip()
    release_id = body.release_id if body.outcome == 'done' else ''
    # A retried request is answered with what it recorded; a different result under the same ID is refused.
    for earlier in [current.get('result') or {}, *(current.get('history') or [])]:
        if earlier.get('request_id') == body.request_id:
            if (earlier.get('outcome'), earlier.get('release_id', ''), earlier.get('note', '')) != (body.outcome, release_id, note):
                raise Failure('request_conflict', '同一请求标识对应不同输入', 409)
            return current
    if current.get('status') not in REPORTABLE:
        raise Failure('invalid_state', '这一轮的优化建议还未交给建模，不能回写处理结果', 409)
    entry = dict(outcome=body.outcome, note=note, request_id=body.request_id)
    if body.outcome == 'done':
        if not release_id:
            raise Failure('release_required', '处理完成时需要提供新版本 release_id', 422)
        evaluated = {(run.get(key) or {}).get('release_id') for key in ('base_ref', 'package_ref')}
        if release_id in evaluated:
            raise Failure('same_release', '回写的版本就是被评测的版本，请提供修改后发布的新版本', 422)
        package = (run.get('base_ref') or run.get('package_ref') or {}).get('ontology_id', '')
        try:
            receipt = packages.call('GET', f"/api/packages/{quote(package, safe='')}/releases/{quote(release_id, safe='')}", token)
        except Failure as exc:
            if exc.status in (404, 410):
                raise Failure('release_not_found', 'Hub 上没有这个知识包版本', 422) from exc
            raise
        if receipt.get('ontology_id') != package or receipt.get('release_id') != release_id:
            raise Failure('release_not_found', 'Hub 上没有这个知识包版本', 422)
        entry.update(release_id=release_id, version=str(receipt.get('version') or '')[:64])
    now = time.time()
    entry.update(reported_by=actor, reported_at=now)
    history = ([current['result']] if current.get('result') else []) + list(current.get('history') or [])
    value = {**current, 'status': 'modeling_done' if body.outcome == 'done' else 'modeling_failed', 'updated_at': now,
             'result': entry, 'history': history[:HISTORY]}
    store.put_optimization(owner, run['id'], value)
    return value


def public(value):
    """The state shown to users and modeling: who reported is a subject digest and stays internal."""
    if not value:
        return None
    hidden = ('reported_by', 'request_id')
    shown = {key: item for key, item in value.items() if key not in ('confirmed_by',)}
    if shown.get('result'):
        shown['result'] = {key: item for key, item in shown['result'].items() if key not in hidden}
    shown['history'] = [{key: item for key, item in entry.items() if key not in hidden} for entry in value.get('history') or []]
    return shown

