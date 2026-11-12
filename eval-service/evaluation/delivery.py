"""Read-only delivery views. Original verdicts and modeling write-back states remain unchanged."""
import json

from . import artifacts, carryover, modeling_result
from .core import Failure


def summary(store, jobs, owner, run):
    meta = jobs.meta(owner, run['id']) or {}
    counts = meta.get('summary') or {}
    carried = meta.get('carryover') or {}
    rounds = carried.get('rounds') or []
    rows = carryover.items(store, owner, run['id'])
    own = {'cases': counts.get('total', len(run.get('cases') or [])),
           'failed': counts.get('fail', 0) + counts.get('partial', 0),
           'errors': counts.get('error', 0), 'manual': counts.get('manual', 0),
           'items': sum(row.get('category') != 'E' for row in rows) if rows is not None else None}
    history = {'cases': sum(r.get('cases', 0) for r in rounds), 'items': sum(r.get('items', 0) for r in rounds),
               'rounds': len(rounds), 'omitted': len(carried.get('slid_out') or [])}
    ready = jobs.drafted(meta)
    has_suggestions = any((history['cases'], history['items'], own['items']))
    legacy_failure = own['failed'] and rows is None
    has_unresolved_cases = any((own['errors'], own['manual'], own['failed']))
    if not modeling_result.applies(run):
        state = 'solver'
    elif not ready:
        state = 'pending'
    elif has_suggestions or legacy_failure:
        state = 'actionable'
    elif has_unresolved_cases or run.get('status') != 'completed':
        state = 'diagnose'
    elif rows is None:
        # Legacy reports without stored rows cannot prove there is nothing to optimize.
        state = 'unknown'
    else:
        state = 'none'
    return {'state': state, 'own': own, 'history': history, 'revision': meta.get('revision')}


def content(store, jobs, owner, run, scope, offset, limit):
    """Read exactly the published attachment; preview uses the same splice and size limits as delivery.

    Each page has at most 20 answers, with 2,000 characters per answer and 1,000 per question/reason.
    A preview is provisional: pending deliveries and later corrections can change the eventual result.
    """
    meta = jobs.meta(owner, run['id']) or {}
    if meta.get('report') != 'ready':
        raise Failure('artifact_not_ready', '交付报告尚未生成', 409)
    state = modeling_result.state(store, jobs, owner, run) or {}
    preview = run.get('origin') == 'consumer_feedback' and state.get('status') == 'awaiting_review'
    if preview:
        from .feedback.report import render
        own = {'report.md': render(run, meta['stem'], meta['generated_at']),
               'answers.json': artifacts.render_answers(run)}
        text, _ = jobs.own(owner, run['id'])
        if text is not None and jobs.drafted(meta):
            own['suggestions.md'] = text
        files, plan = carryover.files(store, jobs, owner, run, meta['stem'], meta['generated_at'], own)
        raw = files['answers.json']
        carried = carryover.public(plan)
    else:
        revision, raw = store.artifact(owner, run['id'], 'answers.json')
        if raw is None or revision != meta['revision']:
            raise Failure('artifact_not_ready', '交付内容正在更新，请刷新后重试', 409)
        carried = meta.get('carryover')
    answers = json.loads(raw)['answers']
    own_count = len(run.get('cases') or [])
    sources = [run['id']] * own_count
    for row in (carried or {}).get('rounds') or []:
        sources.extend([row['run_id']] * row.get('cases', 0))
    selected = [(i, a) for i, a in enumerate(answers)
                if scope == 'all' or (i < own_count) == (scope == 'own')]
    items = []
    for i, answer in selected[offset:offset + limit]:
        text = str(answer.get('finalAnswer') or '')
        items.append({**answer, 'question': str(answer.get('question') or '')[:1000],
                      'reason': str(answer.get('reason') or '')[:1000], 'finalAnswer': text[:2000],
                      'truncated': answer.get('truncated', False) or len(text) > 2000,
                      'source_run_id': sources[i] if i < len(sources) else None,
                      'scope': 'own' if i < own_count else 'history'})
    # Never combine metadata and attachments from different report revisions.
    if (jobs.meta(owner, run['id']) or {}).get('revision') != meta['revision']:
        raise Failure('artifacts_changed', '交付内容正在更新，请刷新后重试', 409)
    return {'items': items, 'total': len(selected), 'offset': offset, 'revision': meta['revision'],
            'own_cases': own_count, 'history_cases': max(0, len(answers) - own_count),
            'carryover': carried, 'preview': preview,
            'provisional': preview, 'suggestions_ready': jobs.drafted(meta)}
