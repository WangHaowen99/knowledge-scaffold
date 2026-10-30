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
from app.services.workspace import WorkspaceError, WorkspaceManager
from hub.package_store import PackageStore
from hub.storage import Store
from hub.user_auth import Identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    manager = WorkspaceManager(tmp_path / 'modeling', tmp_path / 'assets')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path / 'modeling')
    monkeypatch.setattr(projects, 'workspaces', manager)
    meta = manager.create_project('project_alpha', owner='operator_a')
    (manager.project_root(meta['id']) / 'ontology/object/object_demo.md').write_text('# Synthetic object\n')
    store = PackageStore(Store(tmp_path / 'hub'))
    captured = []

    def call(method, path, token, body=None, upload=None, headers=None, download=None):
        actor = Identity(token, 'modeling')
        captured.append((method, path, body))
        with store.io.lock():
            if path == '/api/package-publish-requests':
                return store.reserve(body, actor)[0]
            if path.endswith('/content'):
                with Path(upload).open('rb') as handle:
                    return store.upload(path.split('/')[-2], actor, handle, headers['X-Package-Digest'])
            if path.endswith('/commit'):
                return store.commit(path.split('/')[-2], actor)
            parts = path.split('?')[0].strip('/').split('/')
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
    assert first['manifest_id'] != pid and first['hub_package_id'] != pid
    assert first == hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    second = hub_packages.publish(manager, pid, 'request_next', 'synthetic-actor', 'synthetic-scope')
    assert second['base_release_id'] == first['release_id']
    assert second['hub_package_id'] == first['hub_package_id']
    assert second['version'] == '0.1.2'
    assert all('project_id' not in (body or {}) and 'workspace_id' not in (body or {}) for _, _, body in captured)
    assert not (store.root / 'projects').exists()
    assert hub_packages.releases(pid, 'synthetic-scope', 'synthetic-actor')['total'] == 2
    hub_packages.publish(manager, pid, 'request_demo', 'synthetic-actor', 'synthetic-scope')
    assert hub_packages.association(pid, 'synthetic-scope')['release_id'] == second['release_id']


def test_members_cannot_inherit_another_users_package(setup):
    manager, pid, store, _, _ = setup
    first = hub_packages.publish(manager, pid, 'request_demo', 'actor_a', 'scope_a')
    second = hub_packages.publish(manager, pid, 'request_demo', 'actor_b', 'scope_b')
    assert first['hub_package_id'] != second['hub_package_id']
    assert store.packages('actor_a')['total'] == store.packages('actor_b')['total'] == 1


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
    assert store.packages('actor_a')['total'] == 1
    assert hub_packages.association(pid, 'scope_a')['release_id'] == result['release_id']


def test_import_external_then_publish_preserves_manifest_and_package_identity(setup, tmp_path):
    manager, pid, store, _, _ = setup
    original = tmp_path / 'synthetic.zip'
    manager.export_package_zip(pid, original, version='0.1.8', manifest_id='ontology_demo')
    data = original.read_bytes()
    actor = Identity('actor_a', 'consumer')
    payload = dict(request_id='external_demo', manifest_id='ontology_demo', change_type='external_import',
                   input_digest=hashlib.sha256(data).hexdigest(), version='0.1.8')
    store.reserve(payload, actor)
    store.upload('external_demo', actor, io.BytesIO(data), payload['input_digest'])
    receipt = store.commit('external_demo', actor)
    body = dict(request_id='import_demo', hub_package_id=receipt['hub_package_id'], release_id=receipt['release_id'],
                package_digest=receipt['package_digest'], name='project_beta')
    imported = hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')
    assert hub_packages.import_package(manager, body, 'actor_a', 'scope_a', 'operator_a')['id'] == imported['id']
    result = hub_packages.publish(manager, imported['id'], 'next_demo', 'actor_a', 'scope_a', True)
    assert result['hub_package_id'] == receipt['hub_package_id']
    assert result['manifest_id'] == 'ontology_demo' and result['version'] == '0.1.9'
    assert result['base_digest'] == receipt['package_digest']


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
    package_id = first['hub_package_id']
    result = hub_packages.linked_projects(manager, subject, 'operator_a', False, [package_id, 'package_unknown'])
    assert result == {'items': [
        {'hub_package_id': package_id, 'projects': ['project_alpha', 'project_alpha_copy'], 'total': 2},
        {'hub_package_id': 'package_unknown', 'projects': [], 'total': 0}]}
    assert hub_packages.linked_projects(manager, subject, 'operator_a', True, [package_id])['items'][0]['total'] == 3
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
        url = f'/api/hub/package-projects?hub_package_id={package_id}'
        assert client.get(url).status_code == 401
        response = client.get(url, headers={'Authorization': 'Bearer synthetic-access'})
        assert response.json()['items'][0]['projects'] == ['project_alpha', 'project_alpha_copy']


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
    assert store.release(receipt['hub_package_id'], receipt['release_id'], 'actor_a')[0]['status'] == 'committed'


def test_solver_publish_gate_precedes_hub_reservation(setup, monkeypatch):
    manager, pid, _, calls, _ = setup
    def blocked(project_id):
        raise WorkspaceError('Synthetic solver gate failure', code='solver_validation_required')
    monkeypatch.setattr(manager, '_ensure_solver_publishable', blocked)
    with pytest.raises(WorkspaceError, match='solver gate'):
        hub_packages.publish(manager, pid, 'solver_gate_demo', 'actor_a', 'scope_a', True)
    assert calls == []


def test_evaluation_rejects_changed_validated_objects_before_hub_reservation(setup):
    manager, pid, _, calls, _ = setup
    with pytest.raises(WorkspaceError, match='评测对象'):
        hub_packages.publish(manager, pid, 'snapshot_changed', 'actor_a', 'scope_a',
                             kind='evaluation_snapshot', object_texts={'object_demo': '# Earlier synthetic object'})
    assert calls == []
