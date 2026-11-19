"""Package-level views of runs and the artifacts onto-modeling reads back with the user's login."""
import threading

from fastapi import Body, Query, Request, Response

from .artifacts import ACTIVE, LIMITS, has_results
from .core import Failure
from . import carryover, delivery, modeling_result
from .feedback.store import run_owner

RELEASES = 100
SCANNED_RUNS = 1000
PAGE = 20
# Feedback runs modeling may see: confirmed by a package owner, whatever modeling did with them since.
CONFIRMED = ('notified', 'modeling_failed', 'modeling_done')
ORIGIN = r'^(evaluation|consumer_feedback)$'
MEDIA = {'.md': 'text/markdown; charset=utf-8', '.json': 'application/json'}


def release_of(row):
    ref = row.get('base_ref') or row.get('package_ref') or {}
    return ref.get('release_id'), ref.get('version')


def scoped_run(store, owner, rid, scope):
    """The run, within a delegation's package when the read is delegated."""
    row = store.get(owner, rid, 'runs')
    if scope and (row.get('base_ref') or row.get('package_ref') or {}).get('ontology_id') != scope:
        raise Failure('not_found', '记录不存在', 404)
    return row


def register(app, prefix, actor, load_run, store, engine, jobs, notifier, run_view, login, feedback, reader, read_run):
    """``reader`` and ``read_run`` also accept onto-modeling's delegation, for the reads the data flywheel needs."""
    access, pipeline = feedback['access'], feedback['pipeline']

    drafting = threading.Lock()

    def draft_waiting(package, runs, request, token):
        """Draft the suggestions of feedback runs waiting for an owner, with the login of a caller who may read the
        package: sealing runs in the background without any login, so their package cannot be read then.

        The judge is the deployment's default configuration, as for the rest of feedback processing."""
        owner = run_owner(package)
        with drafting:
            for run in runs:
                meta = jobs.meta(owner, run['id']) or {}
                due = meta.get('suggestions') == 'pending' or (meta.get('suggestions') == 'ready' and meta.get('suggestions_stale'))
                if run['status'] != 'completed' or meta.get('status') == 'generating' or not due:
                    continue
                try:
                    feedback['resolve'](owner, run, token)
                except Failure:
                    continue  # Version not published to Hub yet; tried again on the next visit.
                jobs.regenerate(owner, run['id'], login(request, token))

    def suggestion_state(owner, runs):
        metas = [jobs.meta(owner, run['id']) or {} for run in runs]
        if any(meta.get('suggestions') == 'failed' for meta in metas):
            return 'failed'
        return 'ready' if all(jobs.drafted(meta) for meta in metas) else 'generating'

    def feedback_tasks(request, owner, token, offset):
        """Packages with feedback runs that the caller may read in Hub, newest first, with what waits for an owner."""
        visible = [(package, count) for package, count in feedback['store'].packages()
                   if package and access.visible(owner, token, package)]
        items = []
        for package, count in visible[offset:offset + PAGE]:
            runs = feedback['store'].feedback_runs(package)
            states = {run['id']: modeling_result.state(store, jobs, run_owner(package), run) or {} for run in runs}
            status = {rid: state.get('status') for rid, state in states.items()}
            # What the owner confirms for the package as a whole: every run waiting for review, whatever its age.
            awaiting = [run for run in runs if status[run['id']] == 'awaiting_review']
            draft_waiting(package, awaiting, request, token)
            collecting = next((run for run in runs if run['status'] == 'collecting'), None)
            handled = []
            for state in states.values():
                if state.get('status') not in ('modeling_done', 'modeling_failed'):
                    continue
                if not (state.get('result') or {}).get('via'):
                    handled.append(state)
            handled.sort(key=lambda state: (state.get('result') or {}).get('reported_at') or 0)
            # Deliveries modeling has not handled: runs handed over that no later delivery carries.
            handed = [states[run['id']] for run in runs if status[run['id']] == 'notified' and not states[run['id']].get('carried_into')]
            transport = {'delivered': 0, 'pending': 0, 'failed': 0, 'none': 0}
            for run in runs:
                state = states[run['id']]
                if state.get('status') != 'notified' or state.get('carried_into'):
                    continue
                if delivery.summary(store, jobs, run_owner(package), run)['state'] == 'none':
                    transport['none'] += 1
                    continue
                notice = notifier.status(run['id']).get('status')
                transport['delivered' if notice == 'delivered' else 'failed' if notice in ('failed', 'rejected') else 'pending'] += 1
            latest_result = handled[-1] if handled else None
            if awaiting:
                overall = 'delivering' if any(states[run['id']].get('handover') for run in awaiting) else 'pending'
            elif latest_result and latest_result['status'] == 'modeling_failed':
                overall = 'failed'
            elif handed:
                overall = 'notified'
            elif collecting:
                overall = 'collecting'
            elif latest_result:
                overall = 'done'
            else:
                overall = 'idle'
            done = [run for run in runs if run['status'] == 'completed']
            received = [((run.get('feedback') or {}).get('window') or {}).get('to') or (run.get('feedback') or {}).get('last_included')
                        or run.get('created_at') or 0 for run in runs]
            items.append(dict(ontology_id=package, name=access.names.get(package), run_count=count, executing=False,
                              status=overall, transport=transport, collecting=collecting is not None, awaiting_review=len(awaiting),
                              awaiting_cases=sum(len(run['cases']) for run in awaiting),
                              awaiting_suggestions=sum(len((run.get('feedback') or {}).get('suggestions') or [])
                                                       for run in awaiting),
                              awaiting_since=min((run.get('created_at') or 0 for run in awaiting), default=None),
                              suggestions=suggestion_state(run_owner(package), awaiting) if awaiting else None,
                              collecting_clusters=len(((collecting or {}).get('feedback') or {}).get('clusters') or []),
                              handed=len(handed), handed_since=min((state.get('notified_at') or state.get('updated_at') or 0
                                                                    for state in handed), default=None),
                              latest_result=modeling_result.public(latest_result) if latest_result else None,
                              total_cases=sum(len(run['cases']) for run in done),
                              total_suggestions=sum(len((run.get('feedback') or {}).get('suggestions') or []) for run in done),
                              last_received=max(received, default=None),
                              latest=run_view(run_owner(package), runs[0])))
        return {'items': items, 'total': len(visible), 'offset': offset,
                'next_offset': offset + PAGE if offset + PAGE < len(visible) else None}

    @app.get(f'{prefix}/package-tasks')
    def package_tasks(request: Request, offset: int = Query(0, ge=0), origin: str = Query('evaluation', pattern=ORIGIN)):
        """One task per evaluated package: its newest run, how many runs it has and whether one is executing."""
        owner, token = actor(request)
        if origin == 'consumer_feedback':
            return feedback_tasks(request, owner, token, offset)
        page = store.package_groups(owner, offset)
        for item in page['items']:
            rows = store.package_run_rows(owner, item['ontology_id'], SCANNED_RUNS)
            pending, failed = 0, 0
            for row in rows:
                meta = jobs.meta(owner, row['id']) or {}
                counts = meta.get('summary') or {}
                has_failures = any((counts.get('fail'), counts.get('partial'), counts.get('error')))
                if row['id'] != item['latest']['id'] and has_failures:
                    failed += 1
                state = modeling_result.state(store, jobs, owner, row) or {}
                if state.get('status') in ('notified', 'modeling_failed') and not state.get('carried_into'):
                    if delivery.summary(store, jobs, owner, row)['state'] in ('actionable', 'diagnose', 'unknown'):
                        pending += 1
            item.update(executing=store.package_run_in_status(owner, item['ontology_id'], ACTIVE) is not None,
                        pending_deliveries=pending, historical_failures=failed,
                        history_limited=item['run_count'] > len(rows), latest=run_view(owner, item['latest']))
        return page

    @app.get(f'{prefix}/package-evaluations')
    def package_evaluations(request: Request, ontology_id: str = Query(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$'),
                            origin: str = Query('evaluation', pattern=r'^(evaluation|all)$')):
        """Per release and run type, the newest run with a ready report; plus the package's active run.

        ``releases`` holds data evaluations and ``solver_releases`` solver validations, at most 100 releases each.
        ``owner`` identifies the caller as notifications do, so onto-modeling files both under the same user.
        ``origin=all`` adds, per release, the newest confirmed feedback run of the package when the caller may read it;
        a delegated read has no login to check that with and lists the user's own evaluation runs only."""
        owner, token, scope = reader(request)
        if scope and scope != ontology_id:
            raise Failure('not_found', '记录不存在', 404)
        active, found = None, {'data': {}, 'solver': {}}
        for row in store.package_run_rows(owner, ontology_id, SCANNED_RUNS):
            release_id, version = release_of(row)
            kind = row['run_type'] if row['run_type'] in found else 'data'
            if row['status'] in ACTIVE:
                if active is None:
                    active = dict(run_id=row['id'], release_id=release_id, version=version, status=row['status'], run_type=kind)
                continue
            releases = found[kind]
            if not release_id or release_id in releases or len(releases) >= RELEASES:
                continue
            meta = jobs.meta(owner, row['id'])
            if meta and meta.get('report') == 'ready':
                state = modeling_result.state(store, jobs, owner, {'id': row['id'], 'run_type': kind})
                releases[release_id] = dict(release_id=release_id, version=version, run_id=row['id'], status=row['status'],
                                            run_type=kind, created_at=row['created_at'], finished_at=row['finished_at'],
                                            artifacts=jobs.view(owner, row['id']), summary=meta.get('summary'),
                                            solver_summary=meta.get('solver_summary'),
                                            origin='evaluation', optimization=modeling_result.public(state))
        listed = list(found['data'].values())
        if origin == 'all' and not scope and access.visible(owner, token, ontology_id):
            seen = set()
            for run in feedback['store'].feedback_runs(ontology_id, ('completed',)):
                state = modeling_result.state(store, jobs, run_owner(ontology_id), run)
                release_id, version = release_of(run)
                meta = jobs.meta(run_owner(ontology_id), run['id'])
                if not (state and state.get('status') in CONFIRMED and meta):
                    continue
                if not release_id or release_id in seen:
                    continue
                seen.add(release_id)
                listed.append(dict(release_id=release_id, version=version, run_id=run['id'], status=run['status'],
                                   created_at=run.get('created_at'), finished_at=run.get('finished_at'),
                                   artifacts=jobs.view(run_owner(ontology_id), run['id']), summary=meta.get('summary'),
                                   origin='consumer_feedback', optimization=modeling_result.public(state)))
        return {'ontology_id': ontology_id, 'owner': owner, 'active': active, 'releases': listed,
                'solver_releases': list(found['solver'].values())}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts')
    def artifacts_status(rid: str, request: Request):
        owner = load_run(request, rid)[0]
        return {'artifacts': jobs.view(owner, rid), 'notification': notifier.status(rid)}

    @app.get(f'{prefix}/runs/{{rid}}/delivery')
    def delivery_view(rid: str, request: Request, scope: str = Query('all', pattern=r'^(all|own|history)$'),
                      offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=20)):
        owner, _, _, run = load_run(request, rid)
        return delivery.content(store, jobs, owner, run, scope, offset, limit)

    @app.get(f'{prefix}/runs/{{rid}}/carryover')
    def carryover_view(rid: str, request: Request):
        """Earlier runs of the same source still waiting for modeling: those this run's files carry once handed over,
        or, before a package owner confirmed a feedback run, those confirming it now would carry."""
        owner, _, _, run = load_run(request, rid)
        meta = jobs.meta(owner, rid) or {}
        if 'carryover' in meta:
            return {'carryover': meta['carryover'], 'preview': False}
        window, outside = carryover.history(store, jobs, owner, run)
        return {'carryover': carryover.public(carryover.plan(run, window, outside)), 'preview': True}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts/{{name}}')
    def artifact(rid: str, name: str, request: Request):
        if name not in LIMITS:
            raise Failure('not_found', '产物不存在', 404)
        owner = read_run(request, rid)[0]
        meta = jobs.meta(owner, rid)
        revision, body = store.artifact(owner, rid, name)
        stale = not meta or body is None or revision != meta['revision']
        if stale or (name == 'suggestions.md' and meta['suggestions'] != 'ready'):
            raise Failure('artifact_not_ready', '评测报告或优化建议尚未生成', 404)
        return Response(body, media_type=MEDIA[name[name.rfind('.'):]],
                        headers={'X-Artifact-Revision': str(revision), 'X-Artifact-Stem': meta['stem']})

    @app.post(f'{prefix}/runs/{{rid}}/artifacts/regenerate')
    def regenerate(rid: str, request: Request):
        owner, caller, token, run = load_run(request, rid)
        if run.get('origin') == 'consumer_feedback':
            # Generating now ends collecting; suggestions read the package with this owner's login.
            if run['status'] == 'collecting':
                pipeline.seal(owner, run, 'owner')
            run = feedback['resolve'](owner, store.get(owner, rid, 'runs'), token)
        with engine.guard:
            run = store.get(owner, rid, 'runs')
            if (owner, rid) in engine.active or run['status'] in ACTIVE:
                raise Failure('run_active', '评测仍在执行，结束后才能生成报告', 409)
        if not has_results(run):
            raise Failure('artifacts_unavailable', '此任务没有可生成报告的用例结果', 409)
        # Evaluation suggestions use the judge configuration of the run's owner; feedback uses the default configuration,
        # as all feedback processing does, whoever asks.
        return jobs.regenerate(owner, rid, login(request, token), None if run.get('origin') == 'consumer_feedback' else caller)

    @app.post(f'{prefix}/runs/{{rid}}/notifications/retry')
    def retry_notification(rid: str, request: Request, body: dict | None = Body(default=None)):
        """Send the latest notification again; for a feedback run awaiting review, a package owner's decision instead."""
        owner, caller, token, run = load_run(request, rid)
        state = modeling_result.state(store, jobs, owner, run) or {}
        if run.get('origin') == 'consumer_feedback' and state.get('status') == 'awaiting_review':
            decided = feedback['decide'](owner, caller, token, run, body, login(request, token))
            return {'optimization': modeling_result.public(decided), 'notification': notifier.status(rid)}
        if (body or {}).get('decision'):
            raise Failure('invalid_state', '这一轮不需要确认或已经处理过', 409)
        return notifier.retry(owner, rid)
