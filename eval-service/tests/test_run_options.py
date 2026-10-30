"""Run concurrency, answering model choice and graph workspace binding; all data is synthetic."""
import threading
import time

from evaluation.core import Failure
from test_evaluation import environment, preparation, wait


def eval_set(client, count, name):
    cases = [{'question': f'Synthetic question {i}', 'reference': '2'} for i in range(count)]
    return client.post('/evaluation/api/eval-sets', json={'name': name, 'cases': cases}).json()


def start(client, prep, cases, request_id, **options):
    return client.post('/evaluation/api/runs', json={'request_id': request_id, 'preparation_id': prep['id'],
                                                     'eval_set_id': cases['id'], **options})


def wrap(deps, handler):
    original = deps.json

    def dependency(method, base, path, **kwargs):
        result = handler(method, path, kwargs)
        return original(method, base, path, **kwargs) if result is None else result

    deps.json = dependency
    return original


def test_cases_run_with_the_requested_concurrency(environment):
    client, app, deps, cfg = environment
    prep = preparation(client)
    lock, state = threading.Lock(), {'active': 0, 'peak': 0}
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path != '/agentic/runs':
            return original(method, base, path, **kwargs)
        with lock:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        try:
            time.sleep(0.2)
            return original(method, base, path, **kwargs)
        finally:
            with lock:
                state['active'] -= 1

    deps.json = dependency
    for width, request_id in ((3, 'run_parallel'), (1, 'run_serial')):
        state['peak'] = 0
        response = start(client, prep, eval_set(client, 6, request_id), request_id, concurrency=width)
        assert response.status_code == 202, response.text
        assert response.json()['concurrency'] == width
        result = wait(client, response.json()['id'])
        assert result['status'] == 'completed', result
        assert result['summary']['counts']['pass'] == 6
        assert (2 <= state['peak'] <= 3) if width == 3 else state['peak'] == 1


def test_defaults_and_invalid_run_options(environment):
    client, app, deps, cfg = environment
    prep = preparation(client)
    cases = eval_set(client, 1, 'Synthetic defaults')
    for options in ({'concurrency': 0}, {'concurrency': 9}, {'model_profile_id': 'workspace:private_demo'}):
        assert start(client, prep, cases, 'run_invalid', **options).status_code == 422
    response = start(client, prep, cases, 'run_defaults')
    assert response.json()['concurrency'] == 4 and response.json()['model_profile_id'] == ''
    assert wait(client, response.json()['id'])['status'] == 'completed'
    assert set(deps.submitted_models) == {'global:synthetic'}


def test_selected_global_model_answers_every_case_and_must_stay_enabled(environment):
    client, app, deps, cfg = environment
    prep = preparation(client)
    response = start(client, prep, eval_set(client, 3, 'Synthetic model'), 'run_model',
                     model_profile_id='global:synthetic_alt')
    result = wait(client, response.json()['id'])
    assert result['status'] == 'completed', result
    assert result['summary']['model']['id'] == 'global:synthetic_alt'
    assert set(deps.submitted_models) == {'global:synthetic_alt'}
    deps.profiles = [item for item in deps.profiles if item['id'] != 'global:synthetic_alt']
    response = start(client, prep, eval_set(client, 1, 'Synthetic removed'), 'run_removed_model',
                     model_profile_id='global:synthetic_alt')
    result = wait(client, response.json()['id'])
    assert result['status'] == 'interrupted' and result['error']['code'] == 'model_unavailable'


def test_model_choices_list_only_the_enabled_global_pool(environment):
    client, app, deps, cfg = environment
    listed = client.get('/evaluation/api/consumer-models').json()
    assert listed['available'] is True
    assert [item['id'] for item in listed['items']] == ['global:synthetic', 'global:synthetic_alt']
    assert listed['items'][0] == {'id': 'global:synthetic', 'name': 'Synthetic', 'model': 'synthetic',
                                  'provider': 'openai', 'is_default': True}

    def unavailable(method, path, kwargs):
        if path == '/agentic/workspaces':
            raise Failure('dependency_unavailable', 'Synthetic outage', 503, 'dependency')

    wrap(deps, unavailable)
    assert client.get('/evaluation/api/consumer-models').json() == {'available': False, 'items': []}


def graph_run(client, deps, request_id, workspace_value='workspace_demo'):
    connection = {'uri': 'bolt://service.example.net:7687', 'database': 'neo4j', 'username': 'operator_a',
                  'password': 'synthetic-invalid-password', 'workspace_property': 'd2o_workspace_id',
                  'workspace_value': workspace_value, 'ontology_property': 'd2o_ontology_id'}
    source = client.post('/evaluation/api/data-sources', json={'name': 'graph_' + request_id, 'mode': 'connection',
                                                              'provider': 'neo4j', 'connection': connection,
                                                              'import_data': False}).json()
    prep = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'prepare_' + request_id, 'package_ref': {'hub_package_id': 'package_demo', 'release_id': 'release_demo'},
        'data_source_id': source['id']}).json()
    return wait(client, start(client, prep, eval_set(client, 2, 'Synthetic ' + request_id), request_id).json()['id'])


def test_graph_runs_use_the_workspace_value_as_the_consumer_workspace(environment):
    client, app, deps, cfg = environment
    calls = []

    def consumer(method, path, kwargs):
        calls.append((method, path, kwargs.get('workspace', '')))
        if path.endswith('/data-source') or path.endswith('/data-source/test'):
            return {'success': True}

    wrap(deps, consumer)
    created = graph_run(client, deps, 'run_graph_created')
    assert created['status'] == 'completed', created
    assert ('POST', '/agentic/workspaces', '') in calls
    assert not any(path.startswith('/agentic/internal/evaluation/workspaces') for _, path, _ in calls)
    assert {ws for _, path, ws in calls if path.endswith('/import-zip') or path == '/agentic/runs'} == {'workspace_demo'}
    calls.clear()
    reused = graph_run(client, deps, 'run_graph_reused')
    assert reused['status'] == 'completed', reused
    assert ('POST', '/agentic/workspaces', '') not in calls
    # The user's graph workspace is shared by later runs and is never deleted with a run.
    assert client.delete('/evaluation/api/runs/' + created['id']).json() == {'deleted': True}
    assert not any(method == 'DELETE' for method, _, _ in calls)


def test_graph_runs_refuse_foreign_or_shared_workspaces(environment):
    client, app, deps, cfg = environment
    calls = []

    def consumer(method, path, kwargs):
        calls.append(path)
        if path.endswith('/data-source') or path.endswith('/data-source/test'):
            return {'success': True}
        if path == '/agentic/workspaces/workspace_foreign':
            raise Failure('dependency_rejected', 'Forbidden', 403, 'dependency', upstream_status=403)

    wrap(deps, consumer)
    deps.workspaces['workspace_shared'] = {'id': 'workspace_shared', 'role': 'admin', 'members': ['operator_b']}
    for value in ('workspace_shared', 'workspace_foreign'):
        result = graph_run(client, deps, 'run_' + value, value)
        assert result['status'] == 'interrupted' and result['error']['code'] == 'workspace_not_owned', result
    assert not any(path.endswith('/import-zip') for path in calls)
