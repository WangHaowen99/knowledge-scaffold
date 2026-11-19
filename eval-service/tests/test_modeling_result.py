"""Modeling writes back how it handled a run's suggestions. All identities, packages and releases are synthetic."""
from evaluation.core import digest
from test_run_artifacts import service, settled, start, until
from test_evaluation import preparation


def published(deps, release_id='release_demo_next', version='0.1.2'):
    """A newer release of the synthetic package in the synthetic Hub."""
    receipt = dict(deps.receipt, release_id=release_id, version=version, package_digest=digest(deps.raw))
    deps.releases[release_id] = (deps.raw, receipt)
    return receipt


def write_back(client, rid, **body):
    return client.post(f'/evaluation/api/runs/{rid}/modeling-result', json={'request_id': 'result_demo', 'outcome': 'done', **body})


def test_a_new_release_is_checked_with_hub_and_shown_on_the_run(service):
    client, app, deps, modeling, verdicts = service
    verdicts['Synthetic failing question'] = 'fail'
    record = start(client, preparation(client), [('Synthetic failing question', '2')], 'run_written_back')
    run = settled(client, record['id'])
    assert run['origin'] == 'evaluation' and run['optimization']['status'] == 'notified'
    until(lambda: modeling.received and modeling.received[-1][1]['optimization']['status'] == 'notified')
    published(deps)
    response = write_back(client, record['id'], release_id='release_demo_next', note='Synthetic revision note')
    assert response.status_code == 200, response.text
    result = response.json()['optimization']
    assert result['status'] == 'modeling_done' and result['result']['version'] == '0.1.2'
    assert 'reported_by' not in result['result'] and 'request_id' not in result['result']
    # The run page, its package history and modeling's per-release view all show the result.
    assert client.get(f"/evaluation/api/runs/{record['id']}").json()['optimization']['status'] == 'modeling_done'
    history = client.get('/evaluation/api/runs', params={'ontology_id': 'knowledge_package_demo'}).json()['items']
    assert history[0]['optimization']['result']['version'] == '0.1.2'
    releases = client.get('/evaluation/api/package-evaluations', params={'ontology_id': 'knowledge_package_demo'}).json()['releases']
    assert releases[0]['optimization']['status'] == 'modeling_done' and releases[0]['origin'] == 'evaluation'
    # A retry of the same request answers the same; reusing its ID for another result is refused.
    assert write_back(client, record['id'], release_id='release_demo_next', note='Synthetic revision note').status_code == 200
    assert write_back(client, record['id'], release_id='release_demo_next', note='Other').status_code == 409


def test_a_later_result_replaces_the_earlier_one(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_history')
    settled(client, record['id'])
    failed = client.post(f"/evaluation/api/runs/{record['id']}/modeling-result",
                         json={'request_id': 'result_first', 'outcome': 'failed', 'note': 'Synthetic session busy'})
    assert failed.status_code == 200 and failed.json()['optimization']['status'] == 'modeling_failed'
    published(deps)
    done = client.post(f"/evaluation/api/runs/{record['id']}/modeling-result",
                       json={'request_id': 'result_second', 'outcome': 'done', 'release_id': 'release_demo_next'}).json()
    state = done['optimization']
    assert state['status'] == 'modeling_done'
    assert [entry['outcome'] for entry in state['history']] == ['failed']


def test_modeling_may_report_the_latest_version_already_resolves_the_problems(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_resolved')
    rid = record['id']
    settled(client, rid)
    # 没有发布新版本：核对的版本可以就是被评测的版本，也要在 Hub 上存在
    assert write_back(client, rid, outcome='resolved', release_id='release_missing').json()['code'] == 'release_not_found'
    response = write_back(client, rid, outcome='resolved', release_id='release_demo', note='Synthetic check note')
    assert response.status_code == 200, response.text
    state = response.json()['optimization']
    assert state['status'] == 'modeling_done' and state['result']['outcome'] == 'resolved'
    assert state['result']['release_id'] == 'release_demo' and state['result']['version'] == deps.receipt['version']
    # 同一请求重试返回原结果
    assert write_back(client, rid, outcome='resolved', release_id='release_demo', note='Synthetic check note').status_code == 200
    # 不带核对版本也可以记录
    other = start(client, preparation(client), [('Synthetic question', '2')], 'run_resolved_plain')
    settled(client, other['id'])
    plain = write_back(client, other['id'], outcome='resolved', request_id='result_plain').json()['optimization']
    assert plain['status'] == 'modeling_done' and 'release_id' not in plain['result']


def test_invalid_results_are_refused(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_refused')
    rid = record['id']
    settled(client, rid)
    assert write_back(client, rid, release_id='release_missing').json()['code'] == 'release_not_found'
    assert write_back(client, rid, release_id='release_demo').json()['code'] == 'same_release'
    assert write_back(client, rid).json()['code'] == 'release_required'
    assert write_back(client, rid, outcome='unknown').status_code == 422
    assert write_back(client, rid, release_id='release_demo_next', extra='x').status_code == 422
    # Another user cannot see the run, nor tell that it exists.
    client.headers['Authorization'] = 'Bearer synthetic-user-b'
    assert write_back(client, rid, release_id='release_demo_next').status_code == 404


def test_a_run_without_a_report_cannot_be_written_back(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_no_report')
    settled(client, record['id'])
    owner = app.state.store.db.execute('SELECT owner FROM records WHERE id=?', (record['id'],)).fetchone()[0]
    app.state.store.db.execute('DELETE FROM optimizations')
    app.state.store.db.execute('DELETE FROM artifacts')
    app.state.store.db.commit()
    assert app.state.store.optimization(record['id']) is None and owner
    published(deps)
    response = write_back(client, record['id'], release_id='release_demo_next')
    assert response.status_code == 409 and response.json()['code'] == 'invalid_state'


def test_runs_from_before_the_state_existed_read_as_notified(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_legacy')
    settled(client, record['id'])
    app.state.store.db.execute('DELETE FROM optimizations')
    app.state.store.db.commit()
    assert client.get(f"/evaluation/api/runs/{record['id']}").json()['optimization']['status'] == 'notified'
    published(deps)
    assert write_back(client, record['id'], release_id='release_demo_next').status_code == 200


def test_deleting_a_run_removes_its_result(service):
    client, app, deps, modeling, verdicts = service
    record = start(client, preparation(client), [('Synthetic question', '2')], 'run_deleted')
    settled(client, record['id'])
    assert client.delete(f"/evaluation/api/runs/{record['id']}").status_code == 200
    assert app.state.store.optimization(record['id']) is None
