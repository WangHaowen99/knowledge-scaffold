"""Hub lifecycle regressions using synthetic identities and archives."""
import io
from unittest.mock import patch
import pytest
from test_packages import archive, sha, publish, store, api, userinfo
from hub.package_contract import PackageError
from hub.user_auth import Identity, subject_id


def test_grant_revoke_pending_commit_last_user_and_deleted_tombstone(store):
    first = publish(store)
    target = dict(issuer='https://example.com', sub='operator_b')
    other = Identity(subject_id(**target), 'consumer', **target)
    store.grant('ontology_demo', 'synthetic-owner', [target])
    assert store.packages(other.subject)['total'] == 1
    data = archive('0.1.2')
    body = dict(request_id='pending_demo', ontology_id='ontology_demo', change_type='external_import',
                version='0.1.2', input_digest=sha(data))
    store.reserve(body, other)
    store.upload(body['request_id'], other, io.BytesIO(data), sha(data))
    store.revoke('ontology_demo', 'synthetic-owner', other.subject)
    with pytest.raises(PackageError):
        store.commit(body['request_id'], other)
    with pytest.raises(PackageError, match='last authorized'):
        store.revoke('ontology_demo', 'synthetic-owner', 'synthetic-owner')
    store.grant('ontology_demo', 'synthetic-owner', [target])
    store.revoke('ontology_demo', other.subject, 'synthetic-owner')
    store.recover_all()  # Archive integrity must not depend on the creator retaining access.
    assert store.release('ontology_demo', first['release_id'], other.subject)[0] == first
    assert store.delete('ontology_demo', other.subject)['state'] == 'deleted'
    assert store.delete('ontology_demo', other.subject)['state'] == 'deleted'
    store.recover_all()
    assert store.packages(other.subject)['total'] == 0
    with pytest.raises(PackageError) as error:
        store.reserve(dict(body, request_id='after_delete'), other)
    assert error.value.status == 410


def test_two_first_creators_do_not_implicitly_share(store):
    data = archive()
    actors = [Identity('operator_a', 'consumer'), Identity('operator_b', 'consumer')]
    for index, actor in enumerate(actors):
        body = dict(request_id=f'create_{index}', ontology_id='ontology_demo', change_type='external_import',
                    version='0.1.1', input_digest=sha(data))
        store.reserve(body, actor)
        store.upload(body['request_id'], actor, io.BytesIO(data), sha(data))
    store.commit('create_0', actors[0])
    with pytest.raises(PackageError):
        store.commit('create_1', actors[1])
    assert store.packages(actors[1].subject)['total'] == 0
    store.recover_all()


def test_resource_authorization_and_atomic_target_validation(api, monkeypatch):
    client, headers = api
    data = archive()
    body = dict(request_id='request_demo', ontology_id='ontology_demo', change_type='external_import',
                version='0.1.1', input_digest=sha(data))
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo()):
        assert client.post('/api/package-publish-requests', headers=headers['consumer'], json=body).status_code == 201
        client.put('/api/package-publish-requests/request_demo/content',
                   headers={**headers['consumer'], 'X-Package-Digest': sha(data)}, files={'file': ('demo.zip', data)})
        client.post('/api/package-publish-requests/request_demo/commit', headers=headers['consumer'])
        url = '/api/packages/ontology_demo'
        assert client.get(url, headers=headers['evaluation']).json()['name'] == 'knowledge_package_demo'
        assert client.get(url + '/resources/current', headers=headers['evaluation']).json()['files'] == []
        assert client.delete(url + '/resources/current', headers={**headers['consumer'], 'X-Resource-Generation':'1'}).status_code == 403
        assert client.delete(url + '/resources/current', headers={**headers['modeling'], 'X-Resource-Generation':'1'}).json()['empty']
        with patch('hub.package_routes.validate_targets', side_effect=PackageError('invalid_target','Invalid account',422)):
            response = client.put(url + '/authorized-users', headers=headers['consumer'],
                                  json={'users':[{'issuer':'https://example.com','sub':'operator_missing'}]})
            assert response.status_code == 422
        assert client.get(url + '/authorized-users', headers=headers['consumer']).json()['total'] == 1
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo('operator_other')):
        assert client.get(url + '/resources/current', headers=headers['consumer']).status_code == 404
        assert client.post('/api/package-publish-requests', headers=headers['consumer'],
                           json=dict(body, request_id='duplicate_other')).status_code == 404


def test_target_directory_checks_all_accounts_and_issuer(monkeypatch):
    import json
    import secrets
    from hub.user_auth import validate_targets
    monkeypatch.setenv('HUB_AUTH_URL', 'https://service.example.net')
    monkeypatch.setenv('HUB_AUTH_ISSUER', 'https://example.com')
    monkeypatch.setenv('HUB_AUTH_DIRECTORY_TOKEN', secrets.token_urlsafe(32))
    target = dict(issuer='https://example.com', sub='operator_a')
    directory = json.dumps({'users': [{'username': 'operator_a'}]}).encode()
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: io.BytesIO(directory)):
        assert validate_targets([target]) == [target]
        with pytest.raises(PackageError) as error:
            validate_targets([target, dict(target, sub='operator_missing')])
        assert error.value.code == 'invalid_target'
        with pytest.raises(PackageError):
            validate_targets([dict(target, issuer='https://other.example.com')])
    monkeypatch.delenv('HUB_AUTH_DIRECTORY_TOKEN')
    with pytest.raises(PackageError) as error:
        validate_targets([target])
    assert error.value.status == 503


def test_restore_replays_current_acl_and_tombstone(store, tmp_path):
    from hub.backup import backup, restore
    from hub.package_store import PackageStore
    from hub.storage import Store
    receipt = publish(store)
    snapshot = backup(store.root, tmp_path / 'backup')
    target = dict(issuer='https://example.com', sub='operator_b')
    other = subject_id(**target)
    store.grant('ontology_demo', 'synthetic-owner', [target])
    store.revoke('ontology_demo', other, 'synthetic-owner')
    restored = PackageStore(Store(restore(snapshot, tmp_path / 'restored', store.root / 'packages')))
    assert restored.release('ontology_demo', receipt['release_id'], other)[0] == receipt
    assert restored.packages('synthetic-owner')['total'] == 0
    store.delete('ontology_demo', other)
    deleted = PackageStore(Store(restore(snapshot, tmp_path / 'deleted', store.root / 'packages')))
    assert deleted.packages(other)['total'] == 0
    assert deleted.delete('ontology_demo', other)['state'] == 'deleted'


def test_safe_filename_is_bounded_without_changing_archive():
    from hub.package_store import package_filename
    name = package_filename('ontology_demo', '../' + '合成' * 100, 'v' * 64)
    assert '/' not in name and len(name.encode('utf-8')) < 255
