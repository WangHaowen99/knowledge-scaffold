"""Synthetic storage configurations and fake MySQL only; no external service access."""
import json
import secrets
from urllib.parse import urlsplit

import pytest

from evaluation.core import Failure
from evaluation.dataset_settings import DatasetSettings
from evaluation.data_sources import graph_keys
from evaluation.nl2sql import QueryService, scene_token
from test_evaluation import environment, run, wait


def settings(host='mysql.example.net', password=None):
    if password is None:
        password = secrets.token_urlsafe(24)
    return dict(host=host, port=3306, user='synthetic_user', password=password,
                query_url='https://evaluation.example.net/evaluation/api/nl2sql')


def test_invalid_form_reports_fields_without_echoing_credentials(environment):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    private_input = secrets.token_urlsafe(24)
    for method, endpoint in (('PUT', '/evaluation/api/dataset-settings'),
                             ('POST', '/evaluation/api/dataset-settings/test')):
        response = client.request(method, endpoint,
                                  json={**settings(password=private_input), 'user': '', 'port': 0})
        assert response.status_code == 422
        assert '请填写MySQL 用户名' in response.json()['message']
        assert '端口必须是' in response.json()['message']
        assert {v['field'] for v in response.json()['details']['fields']} == {'port', 'user'}
        assert private_input not in response.text


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('host', '', '请填写MySQL 主机'),
    ('query_url', 'x' * 2049, '自定义查询入口过长，请缩短后重试'),
    ('password', None, '密码格式不正确，请检查'),
])
def test_validation_uses_field_titles(environment, field, value, message):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    body = settings()
    body[field] = value
    response = client.put('/evaluation/api/dataset-settings', json=body)
    assert response.status_code == 422
    assert response.json()['details']['fields'] == [{'field': field, 'message': message}]


def test_query_endpoint_defaults_to_container_port_and_explicit_override_wins(environment, monkeypatch):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    monkeypatch.setenv('EVAL_PORT', '8090')
    body = settings()
    del body['query_url']
    response = client.put('/evaluation/api/dataset-settings', json=body)
    assert response.status_code == 200
    endpoint = urlsplit(response.json()['query_url'])
    assert (endpoint.scheme, endpoint.hostname, endpoint.port, endpoint.path) == (
        'http', 'evaluation', 8090, '/evaluation/api/nl2sql')
    assert response.json()['query_auto'] is True
    cfg.nl2sql_url = 'https://evaluation.example.net/query-adapter'
    response = client.put('/evaluation/api/dataset-settings', json=body)
    assert response.json()['query_url'] == cfg.nl2sql_url
    response = client.put('/evaluation/api/dataset-settings', json={**body, 'query_url': 'https://service.example.net/custom-gateway'})
    assert response.json()['query_auto'] is False
    assert response.json()['query_url'].startswith('https://service.example.net/')


@pytest.mark.parametrize('endpoint', ['sftp://example.net', 'https://example.net:0',
                                     'https://example.net/path?mode=demo', 'https://example.net/path#demo'])
def test_invalid_query_endpoint_still_rejected(endpoint):
    with pytest.raises(Failure) as error:
        DatasetSettings.ready({'url': endpoint})
    assert error.value.code == 'dataset_query_unconfigured'


@pytest.mark.parametrize('identity_change', [{}, {'host': 'other.example.net'}, {'port': 3307}, {'user': 'other_user'}])
def test_dataset_run_refreshes_password_only_for_same_connection(environment, identity_change):
    _, app, _, cfg = environment
    storage = DatasetSettings(cfg, app.state.store)
    original, updated = settings(), settings()
    storage.save(original)
    snapshot = storage.effective()
    started, pending = 'a' * 32, 'b' * 32
    storage.bind(started, snapshot)
    storage.save({**updated, **identity_change, 'query_url': 'https://example.net/new-gateway'})
    expected_password = original['password'] if identity_change else updated['password']
    for bound in (storage.bind(started, snapshot), storage.bind(pending, snapshot), storage.for_run(started)):
        assert bound['sql'] == {**snapshot['sql'], 'password': expected_password}
        assert bound['url'] == snapshot['url']


def test_query_and_cleanup_can_use_rotated_password_without_reimport(environment, monkeypatch):
    import pymysql
    _, app, deps, cfg = environment
    storage = DatasetSettings(cfg, app.state.store)
    original, updated = settings(), settings()
    storage.save(original)
    rid = 'a' * 32
    storage.bind(rid)
    storage.save(updated)

    def connect(**params):
        assert params['host'] == original['host']
        assert params['password'] == updated['password']
        return Database([])

    monkeypatch.setattr(pymysql, 'connect', connect)
    scene = 'eval_' + rid
    service = QueryService(cfg, deps, app.state.engine.judge.settings, storage)
    assert service.query({'scene': scene, 'sql': 'SELECT value FROM items'}, scene_token(cfg, scene), direct=True)['success']
    app.state.engine.data.cleanup(rid, {'mode': 'dataset'})


def test_admin_only_encrypted_settings_and_secret_reuse(environment):
    client, app, deps, cfg = environment
    path = '/evaluation/api/dataset-settings'
    assert client.put(path, json=settings()).status_code == 403
    assert client.post('/evaluation/api/dataset-settings/test', json=settings()).status_code == 403
    assert client.get(path).json()['configured'] is False
    deps.roles['synthetic-user-a'] = 'admin'
    body = settings()
    secret = body.get('password')
    response = client.put(path, json=body)
    assert response.status_code == 200 and response.json()['password_set']
    assert secret not in response.text
    raw = app.state.store.db.execute('SELECT body FROM settings WHERE key=?', ('dataset-settings',)).fetchone()[0]
    assert secret not in raw and 'mysql.example.net' not in raw
    assert client.put(path, json=settings(password='')).status_code == 200
    assert client.put(path, json=settings(host='other.example.net', password='')).status_code == 409
    deps.roles['synthetic-user-a'] = 'user'
    shown = client.get(path).json()
    assert shown['configured'] and not shown['editable']
    assert not {'host', 'user', 'password', 'query_url'} & shown.keys()


def test_upload_can_be_saved_but_unconfigured_run_is_not_created(environment):
    client, app, deps, cfg = environment
    uploaded = client.post('/evaluation/api/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n')}).json()
    source = client.post('/evaluation/api/data-sources', json={'name': 'Synthetic dataset', 'mode': 'dataset',
                                                              'input_ids': [uploaded['id']]}).json()
    prep = client.post('/evaluation/api/package-preparations', json={'request_id': 'prepare_dataset',
                       'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'},
                       'data_source_id': source['id']}).json()
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic cases', 'cases': [
        {'question': 'Synthetic question', 'reference': '2'}]}).json()
    response = client.post('/evaluation/api/runs', json={'request_id': 'run_dataset', 'preparation_id': prep['id'],
                                                        'eval_set_id': cases['id']})
    assert response.status_code == 409
    assert response.json()['code'] == 'dataset_unconfigured'
    assert '上传数据存储设置' in response.json()['message']
    assert client.get('/evaluation/api/runs').json()['total'] == 0
    assert not deps.submissions


class Database:
    def __init__(self, calls, fail_insert=False):
        self.calls, self.fail_insert = calls, fail_insert
        self.description = [('value',)]
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def cursor(self): return self
    def commit(self): pass
    def execute(self, query, *args):
        self.calls.append(query)
        if self.fail_insert and query.startswith('INSERT'):
            raise RuntimeError('synthetic-secret-should-not-escape')
    def executemany(self, query, values): self.calls.append(query)
    def fetchone(self): return (1,)
    def fetchall(self): return [('value',)]
    def fetchmany(self, limit): return [(1,)]


def test_explicit_connection_test_cleans_probe_even_when_write_fails(environment, monkeypatch):
    import pymysql
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    calls = []
    monkeypatch.setattr(pymysql, 'connect', lambda **kw: Database(calls, fail_insert=True))
    response = client.post('/evaluation/api/dataset-settings/test', json=settings())
    assert response.status_code == 409
    assert response.json()['code'] == 'dataset_connection_failed'
    assert 'synthetic-secret' not in response.text
    assert calls[-1].startswith('DROP DATABASE `eval_')
    assert app.state.store.setting('dataset-settings') is None
    monkeypatch.setattr(pymysql, 'connect', lambda **kw: Database(calls))
    assert client.post('/evaluation/api/dataset-settings/test', json=settings()).json()['success']


def test_saving_same_connection_preserves_deployment_tls_options(environment, monkeypatch):
    _, app, _, cfg = environment
    old = settings()
    options = {'ssl_verify_cert': True, 'ssl_verify_identity': True, 'connect_timeout': 7}
    cfg.sql_config = json.dumps({**{key: old[key] for key in ('host', 'port', 'user')},
                                 'passwd': old['password'], **options})
    storage = app.state.engine.data.settings
    assert storage.view(True)['password_set']
    calls = []
    monkeypatch.setattr('pymysql.connect', lambda **sql: calls.append(sql) or Database([]))
    updated = {**old, 'password': '', 'query_url': 'https://service.example.net/gateway'}
    assert storage.test(updated)['success']
    storage.save(updated)
    saved = storage.effective()['sql']
    assert all(saved[key] == calls[0][key] == value for key, value in options.items())
    assert saved['password'] == old['password'] and 'passwd' not in saved
    storage.save(settings(host='other.example.net'))
    assert 'ssl_verify_cert' not in storage.effective()['sql']


def test_legacy_socket_configuration_remains_usable(environment, monkeypatch):
    _, app, deps, cfg = environment
    cfg.sql_config = json.dumps({'unix_socket': '/tmp/example.sock', 'user': 'synthetic_user'})
    sources = app.state.engine.data
    captured = []
    monkeypatch.setattr('pymysql.connect', lambda **sql: captured.append(sql) or Database([]))
    rid = 'a' * 32
    scene = 'eval_' + rid
    query = QueryService(cfg, deps, app.state.engine.judge.settings, sources.settings)
    assert query.query({'scene': scene, 'sql': 'SELECT 1'}, scene_token(cfg, scene), direct=True)['success']
    sources.cleanup(rid, {'mode': 'dataset'})
    assert sources.settings.view()['configured']
    assert [sql['unix_socket'] for sql in captured] == ['/tmp/example.sock'] * 2


def test_unimported_task_never_cleans_up_in_deployment_database(environment, monkeypatch):
    _, app, _, cfg = environment
    sources = app.state.engine.data
    cfg.sql_config = json.dumps({'host': 'deployment.example.net'})
    sources.settings.save(settings())
    source = {'mode': 'dataset', 'dataset_storage': app.state.store.encrypt(sources.settings.effective())}
    def unexpected(**sql):
        pytest.fail('Unimported new run must not contact MySQL')
    monkeypatch.setattr('pymysql.connect', unexpected)
    sources.cleanup('a' * 32, source)


def test_resume_repairs_query_url_without_switching_database(environment, monkeypatch):
    client, app, deps, _ = environment
    deps.roles['synthetic-user-a'] = 'admin'
    config = settings()
    wrong_url = config['query_url']
    fixed_url = 'https://service.example.net/gateway'
    current_urls, hosts = [], []
    monkeypatch.setattr('pymysql.connect', lambda **sql: hosts.append(sql['host']) or Database([]))
    original = deps.json
    def call(method, base, path, **kwargs):
        if method == 'PUT' and path.endswith('/data-source'):
            current_urls.append(kwargs['json']['config']['url'])
        if path.endswith('/data-source/test'):
            return {'success': current_urls[-1] == fixed_url}
        return original(method, base, path, **kwargs)
    monkeypatch.setattr(deps, 'json', call)
    assert client.put('/evaluation/api/dataset-settings', json=config).status_code == 200
    upload = client.post('/evaluation/api/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n')}).json()
    source = client.post('/evaluation/api/data-sources', json={'name': 'Synthetic dataset', 'mode': 'dataset',
                                                              'input_ids': [upload['id']]}).json()
    prep = client.post('/evaluation/api/package-preparations', json={'request_id': 'prepare_dataset',
                       'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'},
                       'data_source_id': source['id']}).json()
    rid = run(client, prep)['id']
    assert wait(client, rid)['error']['code'] == 'dataset_query_unavailable'
    assert not deps.submissions
    assert client.put('/evaluation/api/dataset-settings', json={**config, 'password': '', 'query_url': fixed_url}).status_code == 200
    assert client.post(f'/evaluation/api/runs/{rid}/resume').status_code == 200
    assert wait(client, rid, ('completed',))['status'] == 'completed'
    assert current_urls == [wrong_url, fixed_url]
    assert hosts == [config['host']] * 2
    storage = app.state.engine.data.settings
    storage.save({**config, 'query_url': wrong_url})
    assert storage.for_run(rid)['url'] == fixed_url


def test_import_query_and_cleanup_use_original_connection_after_settings_change(environment, monkeypatch):
    import pymysql
    client, app, deps, cfg = environment
    storage = DatasetSettings(cfg, app.state.store)
    storage.save(settings())
    calls, hosts = [], []
    def connect(**kw):
        hosts.append(kw['host'])
        return Database(calls)
    monkeypatch.setattr(pymysql, 'connect', connect)
    source = {'mode': 'dataset', 'tables_snapshot': app.state.store.encrypt({
        'items': {'columns': ['id'], 'rows': [['item_0001']]}})}
    rid = app.state.store.create('owner_demo', 'runs', {'source': source})['id']
    app.state.engine.data.prepare(rid, source, None)
    storage.save(settings(host='other.example.net'))
    # Recreate the settings service to mimic process restart: binding is durable.
    storage = DatasetSettings(cfg, app.state.store)
    service = QueryService(cfg, deps, app.state.engine.judge.settings, storage)
    scene = 'eval_' + rid
    service.query({'scene': scene, 'sql': 'SELECT value FROM items'}, scene_token(cfg, scene), direct=True)
    cfg.sql_config = json.dumps({'host': 'deployment.example.net'})
    app.state.engine.data.cleanup(rid, source)
    assert app.state.store.setting('dataset-run:' + rid) is not None
    app.state.engine.data.cleanup(rid, source)
    assert hosts == ['mysql.example.net'] * 4
    assert storage.effective()['sql']['host'] == 'other.example.net'
    app.state.store.delete('owner_demo', rid)
    assert app.state.store.setting('dataset-run:' + rid) is None


def test_legacy_runs_use_deployment_and_new_run_snapshot_is_not_redirected(environment):
    client, app, deps, cfg = environment
    cfg.sql_config = json.dumps({'host': 'deployment.example.net'})
    cfg.nl2sql_url = 'https://evaluation.example.net/evaluation/api/nl2sql'
    storage = DatasetSettings(cfg, app.state.store)
    old = storage.ready(storage.effective())
    storage.save(settings())
    assert storage.for_run('a' * 32)['sql']['host'] == 'deployment.example.net'
    storage.bind('b' * 32, old)
    assert storage.for_run('b' * 32)['sql']['host'] == 'deployment.example.net'
    storage.bind('b' * 32, storage.effective())
    assert storage.for_run('b' * 32)['sql']['host'] == 'deployment.example.net'


def test_legacy_import_keeps_valid_deployment_but_unconfigured_task_can_recover(environment, monkeypatch):
    import pymysql
    client, app, deps, cfg = environment
    storage = DatasetSettings(cfg, app.state.store)
    storage.save(settings())
    hosts = []
    def connect(**kw):
        hosts.append(kw['host'])
        return Database([])
    monkeypatch.setattr(pymysql, 'connect', connect)
    source = {'mode': 'dataset', 'tables_snapshot': app.state.store.encrypt({
        'items': {'columns': ['id'], 'rows': [['item_0001']]}})}
    app.state.engine.data.prepare('c' * 32, source, None)
    cfg.sql_config = '{"host":"deployment.example.net"}'
    cfg.nl2sql_url = 'https://evaluation.example.net/evaluation/api/nl2sql'
    app.state.engine.data.prepare('d' * 32, source, None)
    assert hosts == ['mysql.example.net', 'deployment.example.net']


def test_graph_diagnostic_names_empty_key_without_exposing_cell_values():
    item = {'primary_key': ['id'], 'sources': [{'resource': 'items', 'fields': {
        'id': {'source_column': 'id'}, 'parent_id': {'source_column': 'parent_id'}}}]}
    table = {'columns': ['id', 'parent_id'], 'rows': [['item_0001', None], [None, 'synthetic-private-cell']]}
    with pytest.raises(Failure) as caught:
        graph_keys('item', item, table)
    assert caught.value.details['rows'] == [{'data_row': 2, 'fields': ['id']}]
    assert 'synthetic-private-cell' not in str(caught.value)
    assert graph_keys('item', item, {**table, 'rows': table['rows'][:1]}) == [0]
