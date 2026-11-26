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
        assert client.get(f'{url}/resources/current', headers=headers['evaluation']).json()['files'] == []
        assert client.delete(f'{url}/resources/current', headers={**headers['consumer'], 'X-Resource-Generation':'1'}).status_code == 403
        assert client.delete(f'{url}/resources/current', headers={**headers['modeling'], 'X-Resource-Generation':'1'}).json()['empty']
        target = {'issuer': 'https://service.example.net', 'sub': 'operator_not_registered'}
        response = client.put(f'{url}/authorized-users', headers=headers['consumer'],
                              json={'users': [target]})
        assert response.status_code == 200
        response = client.put(f'{url}/authorized-users', headers=headers['consumer'],
                              json={'users': [dict(target, sub='operator_new'),
                                              dict(target, issuer='https://other.example.com')]})
        assert response.status_code == 422
        assert client.get(f'{url}/authorized-users', headers=headers['consumer']).json()['total'] == 2
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo('operator_other')):
        assert client.get(f'{url}/resources/current', headers=headers['consumer']).status_code == 404
        assert client.post('/api/package-publish-requests', headers=headers['consumer'],
                           json=dict(body, request_id='duplicate_other')).status_code == 404


def test_targets_need_no_directory_and_reject_untrusted_issuer(monkeypatch):
    from hub.user_auth import validate_targets
    from pydantic import ValidationError
    monkeypatch.setenv('HUB_AUTH_URL', 'https://service.example.net')
    monkeypatch.setenv('HUB_AUTH_ISSUER', 'https://example.com')
    target = dict(issuer='https://example.com', sub='operator_not_registered')
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=AssertionError('No directory call')):
        assert validate_targets([target]) == [target]
        with pytest.raises(PackageError) as error:
            validate_targets([target, dict(target, issuer='https://other.example.com')])
        assert error.value.code == 'invalid_target'
        with pytest.raises(ValidationError):
            validate_targets([dict(target, sub='')])
    monkeypatch.delenv('HUB_AUTH_ISSUER')
    assert validate_targets([dict(target, issuer='https://service.example.net')])


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
    assert {p.name for p in deleted.root_of('ontology_demo').iterdir()} == {'metadata.json'}
    assert deleted.packages(other)['total'] == 0
    assert deleted.delete('ontology_demo', other)['state'] == 'deleted'


def test_safe_filename_is_bounded_without_changing_archive():
    from hub.package_store import package_filename
    name = package_filename('ontology_demo', '../' + '合成' * 100, 'v' * 64)
    assert '/' not in name and len(name.encode('utf-8')) < 255


@pytest.mark.parametrize('legacy', [False, True])
def test_release_creator_is_immutable_across_users_and_duplicate_uploads(store, legacy):
    from hub.storage import read_json, write_json
    first_user = dict(issuer='https://example.com', sub='operator_a')
    second_user = dict(issuer='https://example.com', sub='operator_b')
    first_actor = Identity(subject_id(**first_user), 'consumer', **first_user)
    second_actor = Identity(subject_id(**second_user), 'consumer', **second_user)
    data = archive()
    first = publish(store, data, identity=first_actor)
    assert first['creator'] == first_user
    row = store.get_request('request_demo', first_actor)
    receipt_path = store.archived(row) / 'receipt.json'
    assert read_json(receipt_path)['creator'] == first_user
    if legacy:
        # Simulate an archive written before creator was introduced, without backfilling it.
        row.pop('creator')
        store.save(row)
        first.pop('creator')
        write_json(receipt_path, first)
    store.grant('ontology_demo', first_actor.subject, [second_user])
    duplicate = publish(store, data, request_id='duplicate_demo', identity=second_actor)
    assert duplicate['existing'] and duplicate['release_id'] == first['release_id']
    assert duplicate.get('creator') == first.get('creator')
    assert store.get_request('duplicate_demo', second_actor).get('creator') == first.get('creator')
    assert store.commit('duplicate_demo', second_actor) == duplicate
    next_data = archive('0.1.2')
    body = dict(request_id='next_demo', ontology_id='ontology_demo', change_type='external_import',
                version='0.1.2', input_digest=sha(next_data))
    store.reserve(body, second_actor)
    store.upload('next_demo', second_actor, io.BytesIO(next_data), sha(next_data))
    second = store.commit('next_demo', second_actor)
    assert second['creator'] == second_user
    store.recover_all()
    assert store.listing('ontology_demo', first_actor.subject)['items'] == [second, first]
    assert store.packages(first_actor.subject)['items'] == [
        dict(second, latest_release_version=second['version'])]
    assert store.release('ontology_demo', first['release_id'], second_actor.subject)[0] == first
    store.revoke('ontology_demo', second_actor.subject, first_actor.subject)
    store.recover_all()
    assert store.release('ontology_demo', first['release_id'], second_actor.subject)[0] == first


def test_physical_delete_keeps_only_tombstone_and_retries_failure(store, monkeypatch):
    from hub.storage import read_json, write_json
    from hub import package_store
    publish(store)
    root = store.root_of('ontology_demo')
    (root / 'resources-current').mkdir()
    (root / 'resources-current' / 'synthetic.txt').write_text('Synthetic resource')
    write_json(store.root / 'delegations' / 'synthetic.json', {'ontology_id': 'ontology_demo'})
    write_json(store.root / 'delegations' / 'unrelated.json', {'ontology_id': 'ontology_other'})
    original = package_store.shutil.rmtree

    def fail(path):
        raise OSError('Synthetic disk failure')

    monkeypatch.setattr(package_store.shutil, 'rmtree', fail)
    with pytest.raises(OSError):
        store.delete('ontology_demo', 'synthetic-owner')
    assert read_json(root / 'metadata.json')['state'] == 'deleted'
    store.recover_all()
    assert store.packages('synthetic-owner')['items'] == []
    monkeypatch.setattr(package_store.shutil, 'rmtree', original)
    result = store.delete('ontology_demo', 'synthetic-owner')
    assert result == {'ontology_id': 'ontology_demo', 'state': 'deleted'}
    assert {p.name for p in root.iterdir()} == {'metadata.json'}
    assert not list((store.root / 'publish-requests').glob('*.json'))
    assert not (store.root / 'delegations' / 'synthetic.json').exists()
    assert (store.root / 'delegations' / 'unrelated.json').exists()
    store.recover_all()
    assert store.delete('ontology_demo', 'synthetic-owner') == result
    with pytest.raises(PackageError):
        store.delete('ontology_demo', 'unauthorized-synthetic')


def test_delete_http_contract_physically_cleans(api):
    client, headers = api
    from test_packages import sha, archive
    payload = archive()
    with patch('hub.user_auth.urllib.request.OpenerDirector.open', side_effect=lambda *a, **k: userinfo()):
        request = dict(request_id='physical_demo', ontology_id='ontology_demo',
                       change_type='modeling_content', kind='release', input_digest=sha(payload))
        assert client.post('/api/package-publish-requests', json=request, headers=headers['modeling']).status_code == 201
        assert client.put('/api/package-publish-requests/physical_demo/content',
                          files={'file': ('package.zip', payload)}, headers={**headers['modeling'], 'X-Package-Digest': sha(payload)}).status_code == 200
        assert client.post('/api/package-publish-requests/physical_demo/commit', headers=headers['modeling']).status_code == 200
        response = client.delete('/api/packages/ontology_demo', headers=headers['modeling'])
        assert response.status_code == 200
        assert response.json() == {'ontology_id': 'ontology_demo', 'state': 'deleted'}
        assert client.get('/api/packages/ontology_demo', headers=headers['modeling']).status_code == 410
