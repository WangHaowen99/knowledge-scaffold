"""Hub contracts exercised with synthetic users, packages and ephemeral credentials."""
import hashlib
import io
import json
import secrets
import zipfile
from datetime import timedelta
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from hub.package_contract import PackageError
from hub.package_store import PackageStore, now
from hub.storage import HubError, Store, write_json
from hub.user_auth import Identity


def archive(version='0.1.1', mapping=None, changes=None, manifest_changes=None):
    """Entirely synthetic package, including its object/table/field identifiers."""
    manifest = dict(kind='ontology-package', schema_version='0.1', id='ontology_demo',
                    version=version, name='knowledge_package_demo', description='Synthetic package')
    manifest.update(manifest_changes or {})
    files = {'manifest.yaml': yaml.safe_dump(manifest), 'objects/index.md': '# Synthetic index',
             'operations/index.md': '# Synthetic index', 'objects/object_demo.md': '# Synthetic object',
             'scripts/demo.py': '# Synthetic script\n'}
    if mapping is not None:
        files['mappings/object-data-mappings.json'] = json.dumps(mapping)
    files.update(changes or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, value in files.items():
            z.writestr('ontology_demo/' + name, value)
    return out.getvalue()


def mapping():
    return {'schema_version': '0.1', 'objects': {'object_demo': {
        'resource': 'table_demo', 'fields': {'field_demo': {'source': 'field_demo'}},
        'primary_key': ['field_demo']}}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store(tmp_path):
    return PackageStore(Store(tmp_path / 'hub'))


def publish(store, data=None, request_id='request_demo', identity=None, **extra):
    identity = identity or Identity('synthetic-owner', 'consumer')
    data = archive() if data is None else data
    payload = dict(request_id=request_id, ontology_id='ontology_demo', change_type='external_import',
                   input_digest=sha(data), version='0.1.1', **extra)
    result, _ = store.reserve(payload, identity)
    store.upload(request_id, identity, io.BytesIO(data), sha(data))
    return store.commit(request_id, identity)


def derived(store, base, request_id='derived_demo', kind='release'):
    actor = Identity('synthetic-owner', 'evaluation')
    payload = dict(request_id=request_id, ontology_id='ontology_demo', hub_package_id=base['hub_package_id'],
                   change_type='mapping_update', input_digest='a' * 64, kind=kind,
                   base_release_id=base['release_id'], base_digest=base['package_digest'])
    result, _ = store.reserve(payload, actor)
    return actor, result, payload


def test_external_without_project_and_cross_service_owner_access(store):
    original = archive()
    result = publish(store, original)
    assert store.release(result['hub_package_id'], result['release_id'], 'synthetic-owner')[1].read_bytes() == original
    assert result['ontology_id'] == 'ontology_demo'
    assert store.packages('synthetic-owner')['items'][0]['ontology_id'] == 'ontology_demo'
    assert not (store.root / 'projects').exists()
    assert 'project_id' not in json.dumps(result) and 'owner_subject' not in result
    assert store.packages('synthetic-owner')['total'] == 1
    assert store.packages('synthetic-other')['total'] == 0
    with pytest.raises(PackageError, match='not found'):
        store.release(result['hub_package_id'], result['release_id'], 'synthetic-other')


def test_mapping_updates_are_new_versions_and_snapshots_are_separate(store):
    base = publish(store)
    actor, result, _ = derived(store, base, kind='evaluation_snapshot')
    data = archive(result['version'], mapping())
    store.upload(result['request_id'], actor, io.BytesIO(data), sha(data))
    receipt = store.commit(result['request_id'], actor)
    assert receipt['version'] == '0.1.1-eval.1' and receipt['base_digest'] == base['package_digest']
    assert store.listing(base['hub_package_id'], actor.subject)['total'] == 1
    assert store.listing(base['hub_package_id'], actor.subject, 'evaluation_snapshot')['total'] == 1
    assert store.release(base['hub_package_id'], base['release_id'], actor.subject)[0] == base


def test_snapshots_are_named_after_their_base_and_leave_formal_versions_alone(store):
    base = publish(store)
    assert derived(store, base, 'snapshot_two', 'evaluation_snapshot')[1]['version'] == '0.1.1-eval.1'
    assert derived(store, base, 'snapshot_three', 'evaluation_snapshot')[1]['version'] == '0.1.1-eval.2'
    # Snapshots no longer take numbers from the formal 0.1.N sequence.
    assert derived(store, base, 'formal_next')[1]['version'] == '0.1.2'


def test_snapshot_version_rules():
    from hub.package_store import snapshot_version
    assert snapshot_version('0.1.10', {'0.1.10', '0.1.11'}) == '0.1.10-eval.1'
    assert snapshot_version('0.1.10', {'0.1.10-eval.1', '0.1.10-eval.2', '0.1.7-eval.5'}) == '0.1.10-eval.3'
    assert snapshot_version('0.1.10-eval.2', {'0.1.10-eval.2'}) == '0.1.10-eval.3'
    assert snapshot_version('', set()) == '0.1.0-eval.1'


@pytest.mark.parametrize('changes,manifest_changes', [
    ({'scripts/demo.py': '# Changed content'}, None),
    ({'objects/object_demo.md': '# Changed object'}, None),
    ({'operations/new.md': '# Added operation'}, None),
    ({'objects/index.md': '# Changed unrelated index'}, None),
    (None, {'description': 'Changed metadata'}),
    (None, {'unknown': 'extra'}),
])
def test_mapping_cannot_modify_body_or_unrelated_metadata(store, changes, manifest_changes):
    base = publish(store)
    actor, result, _ = derived(store, base)
    data = archive(result['version'], mapping(), changes, manifest_changes)
    with pytest.raises(PackageError, match='knowledge content'):
        store.upload(result['request_id'], actor, io.BytesIO(data), sha(data))
    assert store.listing(base['hub_package_id'], actor.subject)['total'] == 1


def test_duplicate_keys_unknown_objects_and_runtime_bindings_are_rejected(store):
    base = publish(store)
    actor, result, _ = derived(store, base)
    invalid = mapping()
    invalid['objects']['object_demo']['password'] = 'synthetic-placeholder'
    for data in (archive(result['version'], invalid),
                 archive(result['version'], mapping(), {'manifest.yaml': 'id: a\nid: b\n'}),
                 archive(result['version'], {'schema_version': '0.1', 'objects': {'missing': {}}})):
        with pytest.raises(PackageError):
            store.upload(result['request_id'], actor, io.BytesIO(data), sha(data))


def test_idempotency_and_version_collision(store):
    first = publish(store)
    assert publish(store) == first
    with pytest.raises(PackageError, match='inputs changed'):
        publish(store, archive(changes={'objects/object_demo.md': 'Other synthetic input'}))
    with pytest.raises(PackageError, match='already reserved'):
        publish(store, request_id='different_request', hub_package_id=first['hub_package_id'])
    assert store.packages('synthetic-owner')['total'] == 1


def test_durable_reservation_recovers_metadata_without_new_package(store, monkeypatch):
    actor = Identity('synthetic-owner', 'consumer')
    body = dict(request_id='request_demo', change_type='external_import', ontology_id='ontology_demo',
                version='0.1.8', input_digest='a' * 64)
    with monkeypatch.context() as m:
        m.setattr(store, 'ensure_metadata', lambda row: (_ for _ in ()).throw(OSError('synthetic outage')))
        with pytest.raises(OSError):
            store.reserve(body, actor)
    store.recover_all()
    result, created = store.reserve(body, actor)
    assert not created and len(store.requests()) == 1
    assert store.owned(result['hub_package_id'], actor.subject)


def test_git_failure_never_exposes_version_and_recovery_completes_only_commit(store, monkeypatch):
    actor = Identity('synthetic-owner', 'consumer')
    data = archive()
    body = dict(request_id='request_demo', change_type='external_import', ontology_id='ontology_demo',
                version='0.1.1', input_digest=sha(data))
    result, _ = store.reserve(body, actor)
    store.upload(body['request_id'], actor, io.BytesIO(data), sha(data))
    store.recover_all()
    assert store.packages(actor.subject)['total'] == 0
    actual_git = store.io.git
    def fail_commit(root, *args):
        if args[0] == 'commit':
            raise HubError('Synthetic Git failure', 503)
        return actual_git(root, *args)
    with monkeypatch.context() as m:
        m.setattr(store.io, 'git', fail_commit)
        with pytest.raises(HubError):
            store.commit(body['request_id'], actor)
        assert store.packages(actor.subject)['total'] == 0
    store.io.recover_all()
    assert store.commit(body['request_id'], actor)['status'] == 'committed'
    assert store.packages(actor.subject)['total'] == 1


def test_expired_upload_not_published_and_version_not_reused(store):
    base = publish(store)
    actor, reserved, _ = derived(store, base)
    data = archive(reserved['version'], mapping())
    store.upload(reserved['request_id'], actor, io.BytesIO(data), sha(data))
    row = store.get_request(reserved['request_id'], actor)
    row['expires_at'] = (now() - timedelta(seconds=1)).isoformat()
    store.save(row)
    store.recover_all()
    assert store.get_request(reserved['request_id'], actor)['status'] == 'expired'
    assert not (store.pending(row) / 'package.zip').exists()
    with pytest.raises(PackageError) as exc:
        store.commit(reserved['request_id'], actor)
    assert exc.value.status == 410
    assert derived(store, base, 'another_request')[1]['version'] == '0.1.3'


def test_backup_restore_and_project_deletion_do_not_erase_independent_package(store, tmp_path):
    from hub.backup import backup, restore
    result = publish(store)
    snapshot = backup(store.root, tmp_path / 'backup')
    deletion = tmp_path / 'deletions'
    deletion.mkdir()
    write_json(deletion / (result['hub_package_id'] + '.json'), {'deleted': True})
    restored = restore(snapshot, tmp_path / 'restored', deletion)
    other = PackageStore(Store(restored))
    assert other.release(result['hub_package_id'], result['release_id'], 'synthetic-owner')[0] == result


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('HUB_DATA_DIR', str(tmp_path))
    from hub import main
    monkeypatch.setattr(main, 'store', Store(tmp_path))
    headers = {}
    for service in ('modeling', 'evaluation', 'consumer'):
        secret = secrets.token_urlsafe(32)
        variable = 'HUB_SERVICE_TOKEN' if service == 'modeling' else 'HUB_' + service.upper() + '_SERVICE_TOKEN'
        monkeypatch.setenv(variable, secret)
        headers[service] = {'Authorization': 'Bearer synthetic-access', 'X-Hub-Service-Token': secret}
    monkeypatch.setenv('HUB_AUTH_URL', 'https://service.example.net')
    with TestClient(main.app) as client:
        yield client, headers


def userinfo(subject='operator_a'):
    return io.BytesIO(json.dumps({'sub': subject}).encode())


def test_service_identity_separation_with_shared_modeling_credential(api, monkeypatch):
    client, headers = api
    from hub import main
    with patch.object(main, 'authorize', return_value='operator_a'):
        # Evaluation and consumer identities cannot use modeling resource cleanup.
        for service in ('evaluation', 'consumer'):
            assert client.delete('/api/internal/projects/project_alpha/artifacts', headers=headers[service]).status_code == 401
    monkeypatch.setenv('HUB_EVALUATION_SERVICE_TOKEN', headers['modeling']['X-Hub-Service-Token'])
    assert client.get('/api/packages', headers=headers['modeling']).status_code == 503


def test_http_contract_with_real_authorization_boundary(api):
    client, headers = api
    data = archive()
    payload = dict(request_id='request_demo', ontology_id='ontology_demo', change_type='external_import',
                   version='0.1.1', input_digest=sha(data))
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo()):
        result = client.post('/api/package-publish-requests', headers=headers['consumer'], json=payload)
        assert result.status_code == 201, result.text
        receipt = result.json()
        assert client.post('/api/package-publish-requests', headers=headers['consumer'], json=payload).status_code == 200
        assert client.put('/api/package-publish-requests/request_demo/content', headers={
            **headers['consumer'], 'X-Package-Digest': sha(data)}, files={'file': ('demo.zip', data)}).status_code == 200
        assert client.post('/api/package-publish-requests/request_demo/commit', headers=headers['consumer']).status_code == 200
        url = f'/api/packages/{receipt["hub_package_id"]}/releases/{receipt["release_id"]}/package'
        for service in headers:
            response = client.get(url, headers=headers[service])
            assert response.status_code == 200 and response.content == data
            assert response.headers['x-package-digest'] == sha(data)
            assert int(response.headers['content-length']) == len(data)
        assert client.get('/api/package-publish-requests/request_demo', headers=headers['modeling']).status_code == 404
        assert client.post('/api/package-publish-requests', headers=headers['evaluation'], json=payload).status_code == 403
        assert client.post('/api/package-publish-requests', headers=headers['consumer'], json={**payload, 'project_id': 'project_alpha'}).status_code == 422
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo('operator_b')):
        assert client.get(url, headers=headers['consumer']).status_code == 404
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=TimeoutError):
        assert client.get('/api/packages', headers=headers['consumer']).status_code == 503
    assert client.get('/api/packages', headers={'X-Hub-Service-Token': headers['consumer']['X-Hub-Service-Token']}).status_code == 401


def test_revocation_and_redirects_fail_closed_without_project_callback(api, monkeypatch):
    import urllib.error
    client, headers = api
    monkeypatch.setenv('HUB_DEV_ALLOW_UNAUTH', '1')
    monkeypatch.setenv('ONTO_AUTHZ_URL', '')
    for upstream, expected in ((401, 401), (403, 401), (302, 503), (500, 503)):
        with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=urllib.error.HTTPError(
                'https://service.example.net/userinfo', upstream, 'synthetic failure', {}, None)):
            assert client.get('/api/packages', headers=headers['modeling']).status_code == expected
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', return_value=io.BytesIO(
            json.dumps({'sub': 'operator_a', 'password_change_required': True}).encode())):
        assert client.get('/api/packages', headers=headers['modeling']).status_code == 403


def test_concurrent_reservations_have_distinct_versions(store):
    from concurrent.futures import ThreadPoolExecutor
    base = publish(store)
    def reserve(number):
        with store.io.lock():
            return derived(store, base, f'request_{number}')[1]['version']
    with ThreadPoolExecutor(max_workers=4) as workers:
        versions = list(workers.map(reserve, range(8)))
    assert len(set(versions)) == 8
    assert set(versions) == {f'0.1.{n}' for n in range(2, 10)}


def test_wrong_base_and_overwrite_of_committed_zip_are_rejected(store):
    base = publish(store)
    actor, reservation, payload = derived(store, base)
    with pytest.raises(PackageError, match='digest mismatch'):
        store.reserve({**payload, 'request_id': 'wrong_base', 'base_digest': '0' * 64}, actor)
    other = publish(store, request_id='other_package')
    with pytest.raises(PackageError, match='not found'):
        store.reserve({**payload, 'request_id': 'cross_package', 'hub_package_id': other['hub_package_id']}, actor)
    changed = archive(changes={'scripts/demo.py': '# Altered synthetic script'})
    with pytest.raises(PackageError, match='content changed'):
        store.upload('request_demo', Identity('synthetic-owner', 'consumer'), io.BytesIO(changed), sha(changed))


def test_pending_archive_validation_rejects_duplicate_paths_and_mismatched_index(store):
    data = archive(changes={'objects/index.md': '[missing](missing.md)'})
    with pytest.raises(PackageError, match='structure'):
        publish(store, data)
    with pytest.raises(PackageError, match='inputs changed'):
        publish(store, archive())
    assert store.packages('synthetic-owner')['total'] == 0


def test_solver_package_mapping_preserves_solver_files_and_manifest(store):
    manifest = {'schema_version': '0.2', 'solver_layout': 'operation-centric.v1',
                'capabilities': {'decision_models': []}}
    files = {'operations/solver_demo.model.yaml': 'id: solver_demo\n'}
    base = publish(store, archive(manifest_changes=manifest, changes=files))
    actor, reserved, _ = derived(store, base)
    data = archive(reserved['version'], mapping(), changes=files, manifest_changes=manifest)
    store.upload(reserved['request_id'], actor, io.BytesIO(data), sha(data))
    assert store.commit(reserved['request_id'], actor)['base_digest'] == base['package_digest']
    for number, changed in enumerate(({'operations/solver_demo.model.yaml': 'id: changed_demo\n'},
                                       {'operations/solver_demo.input.schema.json': '{}'})):
        actor, reserved, _ = derived(store, base, f'changed_solver_{number}')
        data = archive(reserved['version'], mapping(), changes={**files, **changed}, manifest_changes=manifest)
        with pytest.raises(PackageError, match='knowledge content'):
            store.upload(reserved['request_id'], actor, io.BytesIO(data), sha(data))
    actor, reserved, _ = derived(store, base, 'changed_capability')
    data = archive(reserved['version'], mapping(), changes=files,
                   manifest_changes={**manifest, 'capabilities': {'decision_models': [{'id': 'changed_demo'}]}})
    with pytest.raises(PackageError, match='knowledge content'):
        store.upload(reserved['request_id'], actor, io.BytesIO(data), sha(data))
