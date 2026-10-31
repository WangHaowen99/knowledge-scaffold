"""Deleting evaluation records; all identities, runs and failures are synthetic."""
from evaluation.core import Failure
from test_evaluation import environment, preparation, run, wait


def owner_of(client):
    return client.get('/evaluation/api/session').json()['subject']


def test_interrupted_run_without_consumer_submissions_can_be_deleted(environment):
    client, app, deps, cfg = environment
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path.startswith('/agentic/internal/evaluation/workspaces'):
            raise Failure('dependency_rejected', 'Synthetic not found', 404, 'dependency', upstream_status=404)
        return original(method, base, path, **kwargs)

    deps.json = dependency
    record = run(client, preparation(client))
    assert wait(client, record['id'])['status'] == 'interrupted'
    assert client.delete('/evaluation/api/runs/' + record['id']).json() == {'deleted': True}
    assert client.get('/evaluation/api/runs/' + record['id']).status_code == 404


def test_unreconciled_submissions_must_be_stopped_before_deletion(environment):
    client, app, deps, cfg = environment
    record = run(client, preparation(client))
    wait(client, record['id'])
    store, owner = app.state.store, owner_of(client)
    current = store.get(owner, record['id'], 'runs')
    current['status'] = 'interrupted'
    current['cases'][0]['status'] = 'executing'
    store.save(owner, record['id'], {k: v for k, v in current.items() if k not in ('id', 'revision')})
    rejected = client.delete('/evaluation/api/runs/' + record['id'])
    assert rejected.status_code == 409 and '先停止评测' in rejected.json()['message']
    current = store.get(owner, record['id'], 'runs')
    current['cases'][0]['status'] = 'answer_ready'
    store.save(owner, record['id'], {k: v for k, v in current.items() if k not in ('id', 'revision')})
    assert client.delete('/evaluation/api/runs/' + record['id']).json() == {'deleted': True}


def test_running_evaluation_cannot_be_deleted(environment):
    client, app, deps, cfg = environment
    deps.running = True
    record = run(client, preparation(client))
    wait(client, record['id'], statuses=('running',))
    rejected = client.delete('/evaluation/api/runs/' + record['id'])
    assert rejected.status_code == 409 and '先停止评测' in rejected.json()['message']
    deps.running = False
    assert wait(client, record['id'])['status'] == 'completed'
