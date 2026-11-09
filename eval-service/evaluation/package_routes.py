"""Package-level views of runs and the artifacts onto-modeling reads back with the user's login."""
from fastapi import Body, Query, Request, Response

from .artifacts import ACTIVE, LIMITS, has_results
from .core import Failure
from . import modeling_result
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


def register(app, prefix, actor, load_run, store, engine, jobs, notifier, run_view, login, feedback):
    access, pipeline = feedback['access'], feedback['pipeline']

    def feedback_tasks(owner, token, offset):
        """Packages with feedback runs that the caller may read in Hub, newest first."""
        visible = [(package, count) for package, count in feedback['store'].packages()
                   if package and access.visible(owner, token, package)]
        items = []
        for package, count in visible[offset:offset + PAGE]:
            latest = feedback['store'].feedback_runs(package)[0]
            items.append(dict(ontology_id=package, name=access.names.get(package), run_count=count, executing=False,
                              collecting=latest['status'] == 'collecting', latest=run_view(run_owner(package), latest)))
        return {'items': items, 'total': len(visible), 'offset': offset,
                'next_offset': offset + PAGE if offset + PAGE < len(visible) else None}

    @app.get(f'{prefix}/package-tasks')
    def package_tasks(request: Request, offset: int = Query(0, ge=0), origin: str = Query('evaluation', pattern=ORIGIN)):
        """One task per evaluated package: its newest run, how many runs it has and whether one is executing."""
        owner, token = actor(request)
        if origin == 'consumer_feedback':
            return feedback_tasks(owner, token, offset)
        page = store.package_groups(owner, offset)
        page['items'] = [dict(ontology_id=item['ontology_id'], run_count=item['run_count'],
                              executing=store.package_run_in_status(owner, item['ontology_id'], ACTIVE) is not None,
                              latest=run_view(owner, item['latest'])) for item in page['items']]
        return page

    @app.get(f'{prefix}/package-evaluations')
    def package_evaluations(request: Request, ontology_id: str = Query(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$'),
                            origin: str = Query('evaluation', pattern=r'^(evaluation|all)$')):
        """Per release, the newest run with a ready report; plus the package's active run. At most 100 releases.

        ``owner`` identifies the caller as notifications do, so onto-modeling files both under the same user.
        ``origin=all`` adds, per release, the newest confirmed feedback run of the package when the caller may read it."""
        owner, token = actor(request)
        active, releases = None, {}
        for row in store.package_run_rows(owner, ontology_id, SCANNED_RUNS):
            release_id, version = release_of(row)
            if row['status'] in ACTIVE:
                if active is None:
                    active = dict(run_id=row['id'], release_id=release_id, version=version, status=row['status'])
                continue
            if not release_id or release_id in releases or len(releases) >= RELEASES:
                continue
            meta = jobs.meta(owner, row['id'])
            if meta and meta.get('report') == 'ready':
                state = modeling_result.state(store, jobs, owner, {'id': row['id']})
                releases[release_id] = dict(release_id=release_id, version=version, run_id=row['id'], status=row['status'],
                                            created_at=row['created_at'], finished_at=row['finished_at'],
                                            artifacts=jobs.view(owner, row['id']), summary=meta.get('summary'),
                                            origin='evaluation', optimization=modeling_result.public(state))
        listed = list(releases.values())
        if origin == 'all' and access.visible(owner, token, ontology_id):
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
        return {'ontology_id': ontology_id, 'owner': owner, 'active': active, 'releases': listed}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts')
    def artifacts_status(rid: str, request: Request):
        owner = load_run(request, rid)[0]
        return {'artifacts': jobs.view(owner, rid), 'notification': notifier.status(rid)}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts/{{name}}')
    def artifact(rid: str, name: str, request: Request):
        if name not in LIMITS:
            raise Failure('not_found', '产物不存在', 404)
        owner = load_run(request, rid)[0]
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
        # Suggestions use the judge configuration of whoever asks: the run's owner, or a package owner for feedback.
        return jobs.regenerate(owner, rid, login(request, token), caller)

    @app.post(f'{prefix}/runs/{{rid}}/notifications/retry')
    def retry_notification(rid: str, request: Request, body: dict | None = Body(default=None)):
        """Send the latest notification again; for a feedback run awaiting review, a package owner's decision instead."""
        owner, caller, token, run = load_run(request, rid)
        state = modeling_result.state(store, jobs, owner, run) or {}
        if run.get('origin') == 'consumer_feedback' and state.get('status') == 'awaiting_review':
            decided = feedback['decide'](owner, caller, token, run, body)
            return {'optimization': modeling_result.public(decided), 'notification': notifier.status(rid)}
        if (body or {}).get('decision'):
            raise Failure('invalid_state', '这一轮不需要确认或已经处理过', 409)
        return notifier.retry(owner, rid)
