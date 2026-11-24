"""Synthetic storage configurations and fake MySQL only; no external service access."""
import json

import pytest

from evaluation.core import Failure
from evaluation.dataset_settings import DatasetSettings
from evaluation.data_sources import graph_keys
from evaluation.nl2sql import QueryService, scene_token
from test_evaluation import environment


def settings(host='mysql.example.net', password='synthetic-password'):
    return dict(host=host, port=3306, user='synthetic_user', password=password,
                query_url='https://evaluation.example.net/evaluation/api/nl2sql')


def test_invalid_form_reports_fields_without_echoing_credentials(environment):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    for suffix in ('', '/test'):
        response = client.request('POST' if suffix else 'PUT', '/evaluation/api/dataset-settings' + suffix,
                                  json={**settings(), 'user': '', 'port': 0, 'password': 'synthetic-private-input'})
        assert response.status_code == 422
        assert '请填写MySQL 用户名' in response.json()['message']
        assert '端口必须是' in response.json()['message']
        assert {v['field'] for v in response.json()['details']['fields']} == {'port', 'user'}
        assert 'synthetic-private-input' not in response.text


def test_query_endpoint_defaults_to_container_port_and_explicit_override_wins(environment, monkeypatch):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-a'] = 'admin'
    monkeypatch.setenv('EVAL_PORT', '8090')
    body = settings()
    del body['query_url']
    response = client.put('/evaluation/api/dataset-settings', json=body)
    assert response.status_code == 200
    assert response.json()['query_url'] == 'http://evaluation:8090/evaluation/api/nl2sql'
    assert response.json()['query_auto'] is True
    cfg.nl2sql_url = 'https://evaluation.example.net/evaluation/api/nl2sql'
    response = client.put('/evaluation/api/dataset-settings', json=body)
    assert response.json()['query_url'] == cfg.nl2sql_url
    response = client.put('/evaluation/api/dataset-settings', json={**body, 'query_url': 'https://service.example.net/evaluation/api/nl2sql'})
    assert response.json()['query_auto'] is False
    assert response.json()['query_url'].startswith('https://service.example.net/')


def test_admin_only_encrypted_settings_and_secret_reuse(environment):
    client, app, deps, cfg = environment
    path = '/evaluation/api/dataset-settings'
    assert client.put(path, json=settings()).status_code == 403
    assert client.post(path + '/test', json=settings()).status_code == 403
    assert client.get(path).json()['configured'] is False
    deps.roles['synthetic-user-a'] = 'admin'
    response = client.put(path, json=settings())
    assert response.status_code == 200 and response.json()['password_set']
    assert 'synthetic-password' not in response.text
    raw = app.state.store.db.execute('SELECT body FROM settings WHERE key=?', ('dataset-settings',)).fetchone()[0]
    assert 'synthetic-password' not in raw and 'mysql.example.net' not in raw
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
    rid = 'a' * 32
    app.state.engine.data.prepare(rid, source, None)
    storage.save(settings(host='other.example.net'))
    # Recreate the settings service to mimic process restart: binding is durable.
    storage = DatasetSettings(cfg, app.state.store)
    service = QueryService(cfg, deps, app.state.engine.judge.settings, storage)
    scene = 'eval_' + rid
    service.query({'scene': scene, 'sql': 'SELECT value FROM items'}, scene_token(cfg, scene), direct=True)
    app.state.engine.data.cleanup(rid, source)
    assert hosts == ['mysql.example.net'] * 3
    assert storage.effective()['sql']['host'] == 'other.example.net'
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
