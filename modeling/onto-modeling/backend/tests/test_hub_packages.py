"""Hub modeling integration, using only synthetic inputs in isolated directories."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import config
from app.routers import projects, hub_packages as routes
from app.services import auth, hub_packages, knowledge_hub
from app.services.workspace import WorkspaceError, WorkspaceManager, _git

# Standalone modeling test bundles do not include the sibling Hub source tree.
pytest.importorskip('hub', reason='Hub source is not included in standalone modeling tests')

from hub.package_contract import PackageError
from hub.package_store import PackageStore, public
from hub.storage import Store
from hub.user_auth import Identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', tmp_path / 'assets')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(projects, 'workspaces', manager)
    meta = manager.create_project('project_alpha', owner='operator_a')
    root = manager.project_root(meta['id'])
    (root / 'research').mkdir(exist_ok=True)
    (root / 'research/report.md').write_text('# Synthetic research\n')
    (root / 'ontology/object/object_demo.md').write_text('# Synthetic object\n')
    (root / 'ontology/operation').mkdir(exist_ok=True)
    (root / 'ontology/operation/operation_demo.md').write_text('# Synthetic operation\n')
    (root / 'reports').mkdir(exist_ok=True)
    (root / 'reports/validation.md').write_text('# Synthetic validation\n')
    store = PackageStore(Store(tmp_path / 'hub'))
    captured = []

    def call(method, path, token, body=None, upload=None, headers=None, download=None, upload_name=None):
        actor = Identity(token, 'modeling')
        captured.append((method, path, body))
        with store.io.lock():
            if path == '/api/package-publish-requests':
                return store.reserve(body, actor)[0]
            if method == 'GET' and path.startswith('/api/package-publish-requests/'):
                try:
                    return public(store.get_request(path.rsplit('/', 1)[-1], actor))
                except PackageError as exc:
                    raise WorkspaceError(str(exc), exc.status, exc.code) from exc
            if path.endswith('/content'):
                with Path(upload).open('rb') as handle:
                    return store.upload(path.split('/')[-2], actor, handle, headers['X-Package-Digest'])
            if path.endswith('/commit'):
                return store.commit(path.split('/')[-2], actor)
            parts = path.split('?')[0].strip('/').split('/')
            if len(parts) == 4 and parts[3] == 'authorized-users':
                return store.authorized_users(parts[2], actor.subject)
            if len(parts) == 2:
                return store.packages(actor.subject)
            if len(parts) == 4:
                return store.listing(parts[2], actor.subject)
            receipt, archive = store.release(parts[2], parts[4], actor.subject)
            if download:
                shutil.copyfile(archive, download)
                return Path(download)
            return receipt
    monkeypatch.setattr(hub_packages, 'call', call)
    return manager, meta['id'], store, captured, call


def test_actual_modeling_export_uses_independent_identity_and_retains_parent(setup):
    manager, pid, store, captured, _ = setup
    first = hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    assert first['ontology_id'] != pid and first['ontology_id'] != pid
    assert first == hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    second = hub_packages.publish(manager, pid, 'request_next', 'synthetic-actor', 'synthetic-scope')
    assert second['base_release_id'] == first['release_id']
    assert second['ontology_id'] == first['ontology_id']
    assert second['version'] == '0.1.2'
    assert all('project_id' not in (body or {}) and 'workspace_id' not in (body or {}) for _, _, body in captured)
    assert not (store.root / 'projects').exists()
    assert hub_packages.releases(pid, 'synthetic-scope', 'synthetic-actor')['total'] == 2
    hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    assert hub_packages.association(pid, 'synthetic-scope')['release_id'] == second['release_id']


def test_members_share_identity_but_require_explicit_hub_authorization(setup):
    from hub.package_contract import PackageError
    from hub.user_auth import subject_id
    manager, pid, store, _, _ = setup
    first = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    with pytest.raises(PackageError):
        hub_packages.publish(manager, pid, 'request_demo', 'actor_b', 'scope_b')
    target = dict(issuer='https://example.com', sub='operator_b')
    actor_b = subject_id(**target)
    store.grant(first['ontology_id'], 'actor_a', [target])
    second = hub_packages.publish(manager, pid, 'request_allowed', actor_b, 'scope_b')
    assert first['ontology_id'] == second['ontology_id'] == hub_packages.ontology_id(pid)
    assert second['base_release_id'] == first['release_id']
    assert store.packages('actor_a')['total'] == store.packages(actor_b)['total'] == 1


def test_lost_upload_reuses_frozen_source_after_edits(setup, monkeypatch):
    manager, pid, _, _, actual = setup
    sent = []
    def flaky(method, path, token, **kwargs):
        if path.endswith('/content'):
            sent.append(Path(kwargs['upload']).read_bytes())
            if len(sent) == 1:
                raise WorkspaceError('Synthetic transport outage', 503)
        return actual(method, path, token, **kwargs)
    monkeypatch.setattr(hub_packages, 'call', flaky)
    with pytest.raises(WorkspaceError):
        hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Changed synthetic object\n')
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert result['status'] == 'committed' and sent[0] == sent[1]
    assert not list(hub_packages.directory(pid, 'scope_a').rglob('package.zip'))


def test_hub_committed_local_receipt_failure_is_recoverable(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    with monkeypatch.context() as m:
        m.setattr(hub_packages, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic failure')))
        result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert result['local_record_pending']
    restored = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    assert restored['release_id'] == result['release_id']


def test_delegated_retry_returns_saved_receipt_after_local_association_advanced(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    restored = hub_packages.publish(
        manager, pid, 'request_demo', '', 'scope_a', delegated={
            'token_id': '1' * 32, 'issuer': 'https://example.com', 'sub': 'operator_a'},
        expected_input_digest=hub_packages.transport.load(
            hub_packages.directory(pid, 'scope_a') / 'requests/request_demo/task.json')['payload']['input_digest'],
        expected_base=(None, None))
    assert restored == result
    assert store.packages('actor_a')['total'] == 1
    assert hub_packages.association(pid, 'scope_a')['release_id'] == result['release_id']


def test_saved_receipt_rejects_reused_request_id_for_changed_publish_intent(setup):
    manager, pid, store, _, _ = setup
    result = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Changed synthetic object\n')
    _, changed_digest = hub_packages.transport.source_state(manager, pid, False)
    with pytest.raises(WorkspaceError) as exc:
        hub_packages.publish(
            manager, pid, 'request_demo', '', 'scope_a', delegated={
                'token_id': '2' * 32, 'issuer': 'https://example.com', 'sub': 'operator_a'},
            expected_input_digest=changed_digest,
            expected_base=(result['release_id'], result['package_digest']))
    assert exc.value.status == 409 and exc.value.code == 'publish_preview_stale'
    assert store.packages('actor_a')['total'] == 1


def test_lost_commit_response_recovers_after_a_newer_version_was_published(setup, monkeypatch):
    manager, pid, store, _, actual = setup
    committed = []
    def lose_first_commit(method, path, token, **kwargs):
        result = actual(method, path, token, **kwargs)
        if path.endswith('/commit') and not committed:
            committed.append(result)
            raise WorkspaceError('Synthetic lost commit response', 503)
        return result
    monkeypatch.setattr(hub_packages, 'call', lose_first_commit)
    with pytest.raises(WorkspaceError):
        hub_packages.publish(manager, pid, 'request_a', 'actor_a', 'scope_a')
    task = hub_packages.transport.load(
        hub_packages.directory(pid, 'scope_a') / 'requests/request_a/task.json')
    hub_packages.record(manager, pid, 'scope_a', committed[0])
    (manager.project_root(pid) / 'ontology/object/object_demo.md').write_text('# Newer synthetic object\n')
    newer = hub_packages.publish(manager, pid, 'request_b', 'actor_a', 'scope_a')
    assert newer['version'] == '0.1.2'

    recovered = hub_packages.publish(
        manager, pid, 'request_a', 'actor_a', 'scope_a',
        expected_input_digest=task['payload']['input_digest'], expected_base=(None, None))
    assert recovered['release_id'] == committed[0]['release_id']
    assert store.packages('actor_a')['total'] == 1
    assert store.listing(recovered['ontology_id'], 'actor_a')['total'] == 2
    assert hub_packages.association(pid, 'scope_a')['release_id'] == newer['release_id']


def test_import_external_then_publish_preserves_manifest_and_package_identity(setup, tmp_path):
    manager, pid, store, _, _ = setup
    original = tmp_path / 'synthetic.zip'
    manager.export_package_zip(pid, original, version='0.1.8', ontology_id='ontology_demo')
    data = original.read_bytes()
    actor = Identity('actor_a', 'consumer')
    payload = dict(request_id='external_demo', ontology_id='ontology_demo', change_type='external_import',
                   input_digest=hashlib.sha256(data).hexdigest(), version='0.1.8')
    store.reserve(payload, actor)
    store.upload('external_demo', actor, io.BytesIO(data), payload['input_digest'])
    receipt = store.commit('external_demo', actor)
    body = dict(request_id='import_demo', ontology_id=receipt['ontology_id'], release_id=receipt['release_id'],
                package_digest=receipt['package_digest'], name='project_beta')
    imported = hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')
    assert hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')['id'] == imported['id']
    imported_root = manager.project_root(imported['id'])
    for rel in ('research/report.md', 'reports/validation.md'):
        path = imported_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Synthetic stage artifact\n')
    result = hub_packages.publish(manager, imported['id'], 'next_demo', 'actor_a', 'scope_a', True)
    assert result['ontology_id'] == hub_packages.ontology_id(imported['id'])
    assert result['ontology_id'] != receipt['ontology_id'] and result['version'] == '0.1.1'
    assert 'base_digest' not in result


def test_modeling_routes_require_login_and_project_permissions(setup, monkeypatch):
    manager, pid, _, _, _ = setup
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_a', 'iss': 'https://example.com'} if token else None)
    app = FastAPI()
    app.include_router(projects.router)
    app.include_router(routes.router)
    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)
    with TestClient(app) as client:
        url = f'/api/projects/{pid}/package/releases'
        assert client.post(url, json={'request_id': 'request_demo'}).status_code == 401
        response = client.post(url, json={'request_id': 'request_demo'},
                               headers={'Authorization': 'Bearer synthetic-access'})
        assert response.status_code == 200, response.text
        monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_b', 'iss': 'https://example.com'})
        assert client.post(url, json={'request_id': 'request_other'},
                           headers={'Authorization': 'Bearer synthetic-access'}).status_code == 403


def test_solver_release_requires_acknowledgement_and_records_its_revision(setup, monkeypatch):
    from tests import test_solver_contract_check as solver
    manager, plain, _, calls, _ = setup
    pid, root = solver._with_contract(manager)
    complete_publish_stages(root)
    manager._update_meta(pid, owner='operator_a')
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_a', 'iss': 'https://example.com'})
    app = FastAPI()
    app.include_router(projects.router)
    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc), 'code': exc.code}, status_code=exc.status)
    headers = {'Authorization': 'Bearer synthetic-access'}
    with TestClient(app) as client:
        url = f'/api/projects/{pid}/package/releases'
        refused = client.post(url, headers=headers, json={'request_id': 'solver_demo'})
        assert refused.status_code == 409 and refused.json()['code'] == 'solver_validation_acknowledgement_required'
        assert calls == []
        accepted = client.post(url, headers=headers, json={'request_id': 'solver_demo', 'solver_unverified_acknowledged': True})
        assert accepted.status_code == 200, accepted.text
        receipt = accepted.json()
        # 普通知识包不需要确认，也不记录求解契约修订
        assert client.post(f'/api/projects/{plain}/package/releases', headers=headers,
                           json={'request_id': 'plain_demo'}).status_code == 200
    assert hub_packages.solver_revisions(pid) == {receipt['release_id']: manager.solver_check(pid).revision}
    stored = knowledge_hub.load(hub_packages._solver_releases_path(pid))[receipt['release_id']]
    assert stored['acknowledged_by'] == 'operator_a' and stored['version'] == receipt['version']
    assert hub_packages.solver_revisions(plain) == {}


def record_solver_validation(pid, release_id, version, failed=0, earlier=None):
    """Stores synthetic solver-validation results as the evaluation notifications would.

    ``earlier`` adds a result for another release, finished before this one: (release_id, version, failed).
    """
    from app.services import package_evaluations
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    owner = 'e' * 64
    knowledge_hub.save(package_evaluations._owner_path(subject), {'owner': owner})
    def entry(release, label, failures, finished):
        return {'release_id': release, 'version': label, 'run_id': 'f' * 32, 'status': 'completed', 'run_type': 'solver',
                'finished_at': finished, 'artifacts': {'generated_at': finished},
                'solver_summary': {'total': 1, 'passed': 1 - failures, 'failed': failures}}
    releases = {release_id: entry(release_id, version, failed, 2000.0)}
    if earlier:
        releases[earlier[0]] = entry(*earlier, 1000.0)
    knowledge_hub.save(package_evaluations._index_path(hub_packages.ontology_id(pid), owner),
                       {'active': None, 'releases': {}, 'solver_releases': releases})


def complete_publish_stages(root):
    """Adds the synthetic stage files the publish gate requires, keeping the solver project's own objects."""
    (root / 'research').mkdir(exist_ok=True)
    (root / 'research/report.md').write_text('# Synthetic research\n')
    (root / 'reports').mkdir(exist_ok=True)
    (root / 'reports/validation.md').write_text('# Synthetic validation\n')
    objects = root / 'ontology/object'
    objects.mkdir(parents=True, exist_ok=True)
    if not any(path.name != 'index.md' for path in objects.glob('*.md')):
        (objects / 'object_demo.md').write_text('# Synthetic object\n')


def solver_publish_client(setup, monkeypatch):
    from tests import test_solver_contract_check as solver
    manager = setup[0]
    pid, root = solver._with_contract(manager)
    complete_publish_stages(root)
    manager._update_meta(pid, owner='operator_a')
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_a', 'iss': 'https://example.com'})
    app = FastAPI()
    app.include_router(projects.router)
    @app.middleware('http')
    async def signed_in(request, call_next):
        # The production auth middleware stores the verified claims the same way.
        request.state.user = {'sub': 'operator_a', 'iss': 'https://example.com'}
        return await call_next(request)
    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc), 'code': exc.code}, status_code=exc.status)
    return manager, pid, root, app


@pytest.mark.parametrize('failed', [0, 1])
def test_verified_solver_revision_publishes_without_acknowledgement(setup, monkeypatch, failed):
    manager, pid, _root, app = solver_publish_client(setup, monkeypatch)
    headers = {'Authorization': 'Bearer synthetic-access'}
    with TestClient(app) as client:
        url = f'/api/projects/{pid}/package/releases'
        first = client.post(url, headers=headers, json={'request_id': 'solver_first', 'solver_unverified_acknowledged': True})
        assert first.status_code == 200, first.text
        record_solver_validation(pid, first.json()['release_id'], first.json()['version'], failed)
        check = client.get(f'/api/projects/{pid}/package/check', headers=headers).json()['solver']
        again = client.post(url, headers=headers, json={'request_id': 'solver_second'})
    if failed:
        assert check['requires_validation_after_publish'] is True and 'verified_release' not in check
        assert again.status_code == 409 and again.json()['code'] == 'solver_validation_acknowledgement_required'
        return
    assert check['requires_validation_after_publish'] is False
    assert check['verified_release'] == {'release_id': first.json()['release_id'], 'version': first.json()['version']}
    assert again.status_code == 200, again.text
    stored = knowledge_hub.load(hub_packages._solver_releases_path(pid))[again.json()['release_id']]
    assert stored['acknowledged_by'] == '' and stored['revision'] == manager.solver_check(pid).revision


@pytest.mark.parametrize(('older_failed', 'newer_failed', 'exempt'), [(0, 1, False), (1, 0, True)])
def test_latest_validation_of_the_revision_decides_the_exemption(setup, monkeypatch, older_failed, newer_failed, exempt):
    manager, pid, _root, app = solver_publish_client(setup, monkeypatch)
    headers = {'Authorization': 'Bearer synthetic-access'}
    with TestClient(app) as client:
        url = f'/api/projects/{pid}/package/releases'
        older = client.post(url, headers=headers, json={'request_id': 'solver_older', 'solver_unverified_acknowledged': True}).json()
        newer = client.post(url, headers=headers, json={'request_id': 'solver_newer', 'solver_unverified_acknowledged': True}).json()
        record_solver_validation(pid, newer['release_id'], newer['version'], newer_failed,
                                 earlier=(older['release_id'], older['version'], older_failed))
        solver = client.get(f'/api/projects/{pid}/package/check', headers=headers).json()['solver']
        again = client.post(url, headers=headers, json={'request_id': 'solver_third'})
    assert solver['requires_validation_after_publish'] is (not exempt)
    assert again.status_code == (200 if exempt else 409), again.text
    if exempt:
        assert solver['verified_release']['release_id'] == newer['release_id']


def test_every_publish_entry_needs_the_solver_acknowledgement(setup, monkeypatch):
    manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    calls = setup[3]
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    with pytest.raises(WorkspaceError) as raised:
        hub_packages.publish(manager, pid, 'solver_direct', 'synthetic-actor', subject)
    assert raised.value.code == 'solver_validation_acknowledgement_required' and calls == []
    receipt = hub_packages.publish(manager, pid, 'solver_direct', 'synthetic-actor', subject, acknowledged_by='operator_a')
    assert receipt['status'] == 'committed'
    verified = hub_packages.publish(manager, pid, 'solver_verified', 'synthetic-actor', subject,
                                    verified_revision=manager.solver_check(pid).revision)
    assert verified['status'] == 'committed'


def test_changed_solver_revision_needs_acknowledgement_again(setup, monkeypatch):
    manager, pid, root, app = solver_publish_client(setup, monkeypatch)
    headers = {'Authorization': 'Bearer synthetic-access'}
    with TestClient(app) as client:
        url = f'/api/projects/{pid}/package/releases'
        first = client.post(url, headers=headers, json={'request_id': 'solver_first', 'solver_unverified_acknowledged': True}).json()
        record_solver_validation(pid, first['release_id'], first['version'])
        model = next((root / 'ontology' / 'solver').glob('*/presentation.yaml'))
        model.write_text(model.read_text(encoding='utf-8') + '# synthetic edit\n', encoding='utf-8')
        _git(root, 'add', '-A')
        _git(root, 'commit', '-qm', 'synthetic solver edit')
        assert client.get(f'/api/projects/{pid}/package/check', headers=headers).json()['solver']['requires_validation_after_publish'] is True
        refused = client.post(url, headers=headers, json={'request_id': 'solver_second'})
    assert refused.status_code == 409 and refused.json()['code'] == 'solver_validation_acknowledgement_required'


def test_publish_rechecks_the_verified_revision_when_freezing(setup, monkeypatch):
    manager, pid, _root, _app = solver_publish_client(setup, monkeypatch)
    subject = json.dumps(['https://example.com', 'operator_a'], ensure_ascii=False)
    with pytest.raises(WorkspaceError) as raised:
        hub_packages.publish(manager, pid, 'solver_race', 'synthetic-actor', subject, verified_revision='sha256:' + '0' * 64)
    assert raised.value.code == 'solver_validation_acknowledgement_required'


def test_package_project_lookup_lists_only_the_callers_visible_linked_projects(setup, monkeypatch):
    manager, pid, _, _, _ = setup
    claims = {'sub': 'operator_a', 'iss': 'https://example.com'}
    # Same issuer/subject key the Hub routes derive from the login claims.
    subject = json.dumps([claims['iss'], claims['sub']], ensure_ascii=False)
    first = hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', subject)
    copy = manager.create_project('project_alpha_copy', owner='operator_a')
    hidden = manager.create_project('project_hidden', owner='operator_b')
    foreign = manager.create_project('project_foreign_scope', owner='operator_a')
    for project, scope in ((copy, subject), (hidden, subject), (foreign, 'scope_other')):
        hub_packages.record(manager, project['id'], scope, first)
    package_id = first['ontology_id']
    result = hub_packages.linked_projects(manager, subject, 'operator_a', False, [package_id, 'package_unknown'])
    assert result == {'items': [
        {'ontology_id': package_id, 'projects': ['project_alpha', 'project_alpha_copy', 'project_foreign_scope'], 'total': 3},
        {'ontology_id': 'package_unknown', 'projects': [], 'total': 0}]}
    assert hub_packages.linked_projects(manager, subject, 'operator_a', True, [package_id])['items'][0]['total'] == 4
    with pytest.raises(WorkspaceError):
        hub_packages.linked_projects(manager, subject, 'operator_a', False, [f'package_{i}' for i in range(21)])

    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: claims if token else None)
    app = FastAPI()
    app.include_router(routes.router)
    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)
    with TestClient(app) as client:
        url = f'/api/hub/package-projects?ontology_id={package_id}'
        assert client.get(url).status_code == 401
        response = client.get(url, headers={'Authorization': 'Bearer synthetic-access'})
        assert response.json()['items'][0]['projects'] == ['project_alpha', 'project_alpha_copy', 'project_foreign_scope']


def test_local_recovery_and_project_deletion_do_not_delete_hub_package(setup, monkeypatch):
    manager, pid, store, _, _ = setup
    with monkeypatch.context() as m:
        m.setattr(hub_packages, 'record', lambda *a, **k: (_ for _ in ()).throw(OSError('synthetic failure')))
        receipt = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    hub_packages.maintain(manager)
    assert hub_packages.association(pid, 'scope_a')['release_id'] == receipt['release_id']
    manager.delete_project(pid)
    hub_packages.maintain(manager)
    assert not hub_packages.directory(pid, 'scope_a').exists()
    assert store.release(receipt['ontology_id'], receipt['release_id'], 'actor_a')[0]['status'] == 'committed'


def test_solver_publish_gate_precedes_hub_reservation(setup, monkeypatch):
    manager, pid, _, calls, _ = setup
    def blocked(project_id):
        raise WorkspaceError('Synthetic solver gate failure', code='solver_contract_invalid')
    monkeypatch.setattr(manager, '_ensure_solver_publishable', blocked)
    with pytest.raises(WorkspaceError, match='solver gate'):
        hub_packages.publish(manager, pid, 'solver_gate_demo', 'actor_a', 'scope_a', True)
    assert calls == []


def test_modeling_stage_gate_matches_page_required_stages(setup):
    manager, pid, _, calls, _ = setup
    root = manager.project_root(pid)
    (root / 'ontology/operation/operation_demo.md').unlink()
    readiness = manager.package_publish_readiness(pid)
    assert not readiness['ready']
    assert readiness['missing_stages'] == [{'key': 'operation', 'title': '操作提取'}]
    with pytest.raises(WorkspaceError) as exc:
        hub_packages.publish(manager, pid, 'stage_gate_demo', 'actor_a', 'scope_a', True)
    assert exc.value.status == 409 and exc.value.code == 'modeling_stages_incomplete'
    assert calls == []


def test_legacy_logic_directory_satisfies_operation_stage(setup):
    manager, pid, _, _, _ = setup
    root = manager.project_root(pid)
    (root / 'ontology/operation/operation_demo.md').unlink()
    (root / 'ontology/logic').mkdir(exist_ok=True)
    (root / 'ontology/logic/logic_demo.md').write_text('# Synthetic legacy operation\n')
    assert manager.package_publish_readiness(pid)['ready']


@pytest.mark.parametrize('allow_unbound', [False, True])
def test_consumer_import_rules_precede_hub_reservation(setup, allow_unbound):
    manager, pid, _, calls, _ = setup
    root = manager.project_root(pid)
    # 只差大小写的对象文件：consumer 解压时判为重复路径而拒绝导入；降级发布也不能绕过
    (root / 'ontology/object/OBJECT_DEMO.md').write_text('# Synthetic object\n')
    with pytest.raises(WorkspaceError) as caught:
        hub_packages.publish(manager, pid, 'structure_demo', 'actor_a', 'scope_a', allow_unbound)
    assert caught.value.code == 'package_structure_invalid' and 'duplicate paths' in str(caught.value)
    assert calls == []
    assert not (hub_packages.directory(pid, 'scope_a') / 'requests' / 'structure_demo' / 'task.json').exists()
    (root / 'ontology/object/OBJECT_DEMO.md').unlink()
    assert hub_packages.publish(manager, pid, 'structure_demo', 'actor_a', 'scope_a', allow_unbound)['status'] == 'committed'


def test_evaluation_rejects_changed_validated_objects_before_hub_reservation(setup):
    manager, pid, _, calls, _ = setup
    with pytest.raises(WorkspaceError, match='评测对象'):
        hub_packages.publish(manager, pid, 'snapshot_changed', 'actor_a', 'scope_a',
                             kind='evaluation_snapshot', object_texts={'object_demo': '# Earlier synthetic object'})
    assert calls == []


def test_fixed_namespace_and_project_association(setup):
    import uuid
    manager, pid, _, _, _ = setup
    assert hub_packages.ontology_id('project_alpha-00000001') == 'dd15af86-473f-562d-a47e-5f0677437380'
    assert hub_packages.ontology_id(pid) != hub_packages.ontology_id(pid + '-copy')


def test_viewer_cannot_publish_or_resume_evaluation(setup, monkeypatch):
    manager, pid, _, calls, _ = setup
    manager._update_meta(pid, members={'operator_b': 'viewer'})
    monkeypatch.setattr(auth, 'ENABLED', True)
    monkeypatch.setattr(auth, 'verify_access_token', lambda token: {'sub': 'operator_b', 'iss': 'https://example.com'})
    app = FastAPI()
    @app.middleware('http')
    async def identity_middleware(request, call_next):
        request.state.user = {'sub': 'operator_b', 'iss': 'https://example.com'}
        return await call_next(request)
    app.include_router(projects.router)
    @app.exception_handler(WorkspaceError)
    async def error(request, exc):
        return JSONResponse({'error': str(exc)}, status_code=exc.status)
    with TestClient(app) as client:
        headers = {'Authorization': 'Bearer synthetic-access'}
        assert client.post(f'/api/projects/{pid}/package/releases', headers=headers,
                           json={'request_id': 'viewer_demo'}).status_code == 403
        assert client.post(f'/api/projects/{pid}/eval/resume', headers=headers).status_code == 403
    assert not calls
