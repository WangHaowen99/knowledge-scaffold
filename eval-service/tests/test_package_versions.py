"""Chosen release and evaluation snapshot versions; packages, identities and data are synthetic."""
from test_evaluation import environment, preparation, run, wait


def dataset(client, content, name):
    uploaded = client.post('/evaluation/api/inputs', files={'file': ('items.csv', content, 'text/csv')}).json()
    return client.post('/evaluation/api/data-sources', json={'name': name, 'mode': 'dataset', 'input_ids': [uploaded['id']]}).json()


def prepare(client, source, request_id):
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': request_id, 'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'},
        'data_source_id': source['id']})
    assert response.status_code == 200, response.text
    return response.json()


def test_same_mapping_reuses_one_snapshot_instead_of_taking_new_versions(environment):
    client, app, deps, cfg = environment
    source = dataset(client, b'id,value\nitem_0001,2\n', 'Synthetic dataset')
    first, second = prepare(client, source, 'prepare_first'), prepare(client, source, 'prepare_second')
    assert first['package_ref']['kind'] == 'evaluation_snapshot'
    assert second['package_ref'] == first['package_ref']
    assert len(deps.requests) == 1
    other = prepare(client, dataset(client, b'id\nitem_0001\n', 'Synthetic narrower dataset'), 'prepare_other')
    assert other['package_ref']['release_id'] != first['package_ref']['release_id']
    assert len(deps.requests) == 2


def test_runs_record_the_chosen_release_next_to_the_snapshot_that_ran(environment, monkeypatch):
    client, app, deps, cfg = environment
    # Synthetic deployment settings pass configuration preflight; the unavailable database stops execution.
    cfg.sql_config = '{"host":"127.0.0.1","port":1,"connect_timeout":1}'
    cfg.nl2sql_url = 'https://service.example.net/evaluation/api/nl2sql'
    import pymysql
    def unavailable(**kwargs):
        raise OSError('Synthetic unavailable database')
    monkeypatch.setattr(pymysql, 'connect', unavailable)
    prep = prepare(client, dataset(client, b'id,value\nitem_0001,2\n', 'Synthetic dataset'), 'prepare_snapshot')
    record = run(client, prep)
    assert record['base_ref']['version'] == '0.1.1' and record['base_ref']['release_id'] == 'release_demo'
    assert record['package_ref']['kind'] == 'evaluation_snapshot'
    assert record['package_ref']['version'] != record['base_ref']['version']
    # The stub has no evaluation database, so the dataset import stops the run; versions are recorded regardless.
    result = wait(client, record['id'])
    assert result['base_ref']['version'] == '0.1.1'
    assert result['package_ref']['kind'] == 'evaluation_snapshot'
    listed = client.get('/evaluation/api/runs').json()['items'][0]
    assert listed['base_ref']['version'] == '0.1.1' and listed['package_ref']['kind'] == 'evaluation_snapshot'


def test_runs_without_mapping_change_run_the_chosen_release(environment):
    client, app, deps, cfg = environment
    record = run(client, preparation(client))
    assert record['base_ref']['release_id'] == record['package_ref']['release_id'] == 'release_demo'
    assert record['package_ref']['kind'] == 'release'
    assert deps.requests == {}


def test_runs_recorded_before_base_ref_show_the_release_kept_by_their_preparation(environment, monkeypatch):
    client, app, deps, cfg = environment
    cfg.sql_config = '{"host":"mysql.example.net"}'
    cfg.nl2sql_url = 'https://service.example.net/evaluation/api/nl2sql'
    import pymysql
    def unavailable(**kwargs):
        raise OSError('Synthetic unavailable database')
    monkeypatch.setattr(pymysql, 'connect', unavailable)
    prep = prepare(client, dataset(client, b'id,value\nitem_0001,2\n', 'Synthetic dataset'), 'prepare_legacy')
    record = run(client, prep)
    wait(client, record['id'])
    owner = client.get('/evaluation/api/session').json()['subject']
    store = app.state.store
    legacy = {k: v for k, v in store.get(owner, record['id'], 'runs').items() if k not in ('id', 'revision', 'base_ref')}
    store.save(owner, record['id'], legacy)
    assert client.get('/evaluation/api/runs').json()['items'][0]['base_ref']['version'] == '0.1.1'
    assert client.get(f"/evaluation/api/runs/{record['id']}").json()['base_ref']['version'] == '0.1.1'
    # Display only: the stored run keeps its original shape.
    assert 'base_ref' not in store.get(owner, record['id'], 'runs')
