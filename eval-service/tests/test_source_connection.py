"""Connections, package mappings and dependency responses are fully synthetic."""
import pytest

from evaluation.core import Failure, digest
from evaluation.packages import derive
from test_evaluation import consumer_data_source, environment, run, wait


CONNECTION = {
    'url': 'https://service.example.net/api/v1/', 'scene': 'workspace_demo',
    'evaluation_workspace_id': 'workspace_demo', 'token': 'synthetic-invalid-token',
}


@pytest.mark.parametrize('provider', ['nl2sql_http', 'nl2sql_template'])
def test_connection_uses_a_fixed_probe_without_saving_or_reading_metadata(environment, provider):
    client, app, deps, _ = environment
    calls, original = [], deps.json

    def dependency(method, base, path, **kwargs):
        if path == '/api/v1/sql':
            calls.append((method, base, kwargs))
            return {'success': True, 'data': [{'ok': 1}]}
        return original(method, base, path, **kwargs)

    deps.json = dependency
    before = app.state.store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
    result = client.post('/evaluation/api/data-source-tests', json={
        'provider': provider, 'connection': CONNECTION})
    assert result.status_code == 200, result.text
    assert result.json()['success'] is True and result.json()['tested_at'] > 0
    assert len(calls) == 1
    method, base, kwargs = calls[0]
    assert method == 'POST' and base == 'https://service.example.net'
    assert kwargs['json'] == {'scene': 'workspace_demo', 'sql': 'SELECT 1'}
    assert kwargs['token'] == CONNECTION['token']
    assert kwargs['timeout'] == 10 and kwargs['limit'] == 64 * 1024
    assert app.state.store.db.execute('SELECT COUNT(*) FROM records').fetchone()[0] == before
    assert CONNECTION['token'] not in result.text and CONNECTION['url'] not in result.text


@pytest.mark.parametrize('provider', ['nl2sql_http', 'nl2sql_template'])
def test_connection_reuses_only_the_owners_saved_secret(environment, provider):
    client, app, deps, _ = environment
    source = client.post('/evaluation/api/data-sources', json={
        'name': 'data_source_demo', 'mode': 'connection', 'provider': provider,
        'connection': CONNECTION}).json()
    before = dict(app.state.store.db.execute(
        'SELECT * FROM records WHERE id=?', (source['id'],)).fetchone())
    calls, original = [], deps.json

    def dependency(method, base, path, **kwargs):
        if path == '/api/v1/sql':
            calls.append(kwargs['token'])
            return {'success': True}
        return original(method, base, path, **kwargs)

    deps.json = dependency
    body = {'provider': provider, 'data_source_id': source['id'],
            'connection': {**CONNECTION, 'token': ''}}
    assert client.post('/evaluation/api/data-source-tests', json=body).status_code == 200
    assert calls == [CONNECTION['token']]
    assert dict(app.state.store.db.execute(
        'SELECT * FROM records WHERE id=?', (source['id'],)).fetchone()) == before
    changed = {**body, 'connection': {**body['connection'], 'url': 'https://other.example.net'}}
    assert client.post('/evaluation/api/data-source-tests', json=changed).status_code == 422
    client.headers['Authorization'] = 'Bearer synthetic-user-b'
    assert client.post('/evaluation/api/data-source-tests', json=body).status_code == 404
    assert calls == [CONNECTION['token']]


@pytest.mark.parametrize('response', [None, [], {}, {'success': 'true'}, {'success': False,
    'error': 'synthetic private diagnostic'}])
def test_failed_connection_rejects_invalid_responses_without_exposing_them(environment, response):
    client, _, deps, _ = environment
    original = deps.json

    def dependency(method, base, path, **kwargs):
        return response if path == '/api/v1/sql' else original(method, base, path, **kwargs)

    deps.json = dependency
    result = client.post('/evaluation/api/data-source-tests', json={
        'provider': 'nl2sql_http', 'connection': CONNECTION})
    assert result.status_code == 422
    assert result.json()['code'] == 'connection_test_failed'
    assert 'synthetic private diagnostic' not in result.text


def test_network_failure_is_controlled_and_does_not_prevent_saving(environment):
    client, _, deps, _ = environment
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path == '/api/v1/sql':
            raise Failure('dependency_unavailable', CONNECTION['url'] + CONNECTION['token'], 503)
        return original(method, base, path, **kwargs)

    deps.json = dependency
    result = client.post('/evaluation/api/data-source-tests', json={
        'provider': 'nl2sql_template', 'connection': CONNECTION})
    assert result.status_code == 503 and result.json()['code'] == 'connection_unavailable'
    assert CONNECTION['url'] not in result.text and CONNECTION['token'] not in result.text
    saved = client.post('/evaluation/api/data-sources', json={
        'name': 'data_source_demo', 'mode': 'connection', 'provider': 'nl2sql_template',
        'connection': CONNECTION})
    assert saved.status_code == 200, saved.text


def test_connection_request_accepts_no_queries_and_checks_inputs_before_network(environment):
    client, _, _, _ = environment
    body = {'provider': 'nl2sql_http', 'connection': CONNECTION}
    for extra in ({'sql': 'SELECT 2'}, {'question': 'Synthetic query'}):
        assert client.post('/evaluation/api/data-source-tests', json={**body, **extra}).status_code == 422
    assert client.post('/evaluation/api/data-source-tests', json={**body, 'provider': 'neo4j'}).status_code == 422
    assert client.post('/evaluation/api/data-source-tests', json={**body,
        'connection': {**CONNECTION, 'scene': 'invalid scene'}}).status_code == 422


@pytest.mark.parametrize('provider', ['nl2sql_http', 'nl2sql_template'])
@pytest.mark.parametrize('old_metadata', [False, True])
def test_nl2sql_preparation_preserves_the_package_mapping_and_original_release(environment, provider, old_metadata):
    client, app, deps, _ = environment
    mapping = {'schema_version': '0.1', 'default_provider_alias': 'data', 'objects': {
        'item': {'resource': 'items', 'primary_key': ['id'], 'fields': {
            'id': {'source': 'id', 'source_column': 'id', 'type': 'string'}}}}}
    deps.raw = derive(deps.raw, mapping, '0.1.1')
    deps.receipt = {**deps.receipt, 'package_digest': digest(deps.raw)}
    deps.releases['release_demo'] = (deps.raw, deps.receipt)
    configured, imported, original = [], [], deps.json

    def dependency(method, base, path, **kwargs):
        assert path not in ('/api/v1/sql', '/api/v1/query')
        if path.endswith('/data-source'):
            configured.append(kwargs['json'])
            return consumer_data_source(kwargs['json'])
        if path.endswith('/import-zip'):
            imported.append(kwargs['files']['file'][1])
        return original(method, base, path, **kwargs)

    deps.json = dependency
    saved = client.post('/evaluation/api/data-sources', json={
        'name': 'data_source_demo', 'mode': 'connection', 'provider': provider,
        'connection': CONNECTION, 'tables': {'unrelated_table': ['different_column']} if old_metadata else {}})
    assert saved.status_code == 200, saved.text
    source = saved.json()
    if old_metadata:
        owner = app.state.store.db.execute('SELECT owner FROM records WHERE id=?', (source['id'],)).fetchone()[0]
        stored = app.state.store.get(owner, source['id'], 'data-sources')
        app.state.store.save(owner, source['id'], {**stored,
            'tables_snapshot': app.state.store.encrypt({'unrelated_table': {'columns': ['different_column'], 'rows': []}}),
            'input_ids': ['removed_input_demo']})
    package_ref = {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}
    preview = client.post('/evaluation/api/data-source-previews', json={
        'data_source_id': source['id'], 'package_ref': package_ref})
    assert preview.status_code == 200, preview.text
    assert preview.json()['objects'] == []
    prepared = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'prepare_sql_demo', 'package_ref': package_ref, 'data_source_id': source['id']})
    assert prepared.status_code == 200, prepared.text
    prep = prepared.json()
    assert prep['status'] == 'ready' and prep['mapping'] is False and prep['changed'] is False
    assert prep['package_ref']['release_id'] == 'release_demo' and not deps.requests
    result = wait(client, run(client, prep)['id'])
    assert result['status'] == 'completed', result
    assert imported == [deps.raw]
    assert configured == [{'mode': 'direct_provider', 'provider': provider, 'config': {
        'url': 'https://service.example.net', 'scene': CONNECTION['scene'], 'token': CONNECTION['token']}}]
    assert not deps.requests
