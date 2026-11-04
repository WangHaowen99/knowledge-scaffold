"""Package-level views of runs and the artifacts onto-modeling reads back with the user's login."""
from fastapi import Query, Request, Response

from .artifacts import ACTIVE, LIMITS, has_results
from .core import Failure

RELEASES = 100
SCANNED_RUNS = 1000
MEDIA = {'.md': 'text/markdown; charset=utf-8', '.json': 'application/json'}


def release_of(row):
    ref = row.get('base_ref') or row.get('package_ref') or {}
    return ref.get('release_id'), ref.get('version')


def register(app, prefix, actor, store, engine, jobs, notifier, run_view):
    @app.get(f'{prefix}/package-tasks')
    def package_tasks(request: Request, offset: int = Query(0, ge=0)):
        """One task per evaluated package: its newest run, how many runs it has and whether one is executing."""
        owner, _ = actor(request)
        page = store.package_groups(owner, offset)
        page['items'] = [dict(ontology_id=item['ontology_id'], run_count=item['run_count'],
                              executing=store.package_run_in_status(owner, item['ontology_id'], ACTIVE) is not None,
                              latest=run_view(owner, item['latest'])) for item in page['items']]
        return page

    @app.get(f'{prefix}/package-evaluations')
    def package_evaluations(request: Request, ontology_id: str = Query(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')):
        """Per release, the newest run with a ready report; plus the package's active run. At most 100 releases.

        ``owner`` identifies the caller as notifications do, so onto-modeling files both under the same user."""
        owner, _ = actor(request)
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
                releases[release_id] = dict(release_id=release_id, version=version, run_id=row['id'], status=row['status'],
                                            created_at=row['created_at'], finished_at=row['finished_at'],
                                            artifacts=jobs.view(owner, row['id']), summary=meta.get('summary'))
        return {'ontology_id': ontology_id, 'owner': owner, 'active': active, 'releases': list(releases.values())}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts')
    def artifacts_status(rid: str, request: Request):
        owner, _ = actor(request)
        store.get(owner, rid, 'runs')
        return {'artifacts': jobs.view(owner, rid), 'notification': notifier.status(rid)}

    @app.get(f'{prefix}/runs/{{rid}}/artifacts/{{name}}')
    def artifact(rid: str, name: str, request: Request):
        owner, _ = actor(request)
        if name not in LIMITS:
            raise Failure('not_found', '产物不存在', 404)
        store.get(owner, rid, 'runs')
        meta = jobs.meta(owner, rid)
        revision, body = store.artifact(owner, rid, name)
        stale = not meta or body is None or revision != meta['revision']
        if stale or (name == 'suggestions.md' and meta['suggestions'] != 'ready'):
            raise Failure('artifact_not_ready', '评测报告或优化建议尚未生成', 404)
        return Response(body, media_type=MEDIA[name[name.rfind('.'):]],
                        headers={'X-Artifact-Revision': str(revision), 'X-Artifact-Stem': meta['stem']})

    @app.post(f'{prefix}/runs/{{rid}}/artifacts/regenerate')
    def regenerate(rid: str, request: Request):
        owner, token = actor(request)
        with engine.guard:
            run = store.get(owner, rid, 'runs')
            if (owner, rid) in engine.active or run['status'] in ACTIVE:
                raise Failure('run_active', '评测仍在执行，结束后才能生成报告', 409)
        if not has_results(run):
            raise Failure('artifacts_unavailable', '此任务没有可生成报告的用例结果', 409)
        return jobs.regenerate(owner, rid, token)

    @app.post(f'{prefix}/runs/{{rid}}/notifications/retry')
    def retry_notification(rid: str, request: Request):
        owner, _ = actor(request)
        store.get(owner, rid, 'runs')
        return notifier.retry(owner, rid)
