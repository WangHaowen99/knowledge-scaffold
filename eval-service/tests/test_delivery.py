"""Synthetic delivery views: exact published content, provisional previews and access isolation."""
import json

from evaluation import delivery
from test_run_artifacts import service, settled, start  # noqa: F401
from test_evaluation import preparation
from test_carryover import owner_of


def test_delivery_preserves_score_and_exposes_carried_sources(service):
    client, app, deps, modeling, verdicts = service
    verdicts['Synthetic earlier'] = 'fail'
    prep = preparation(client)
    first = start(client, prep, [('Synthetic earlier', '2')], 'delivery_first')
    settled(client, first['id'])
    second = start(client, prep, [('Synthetic current', '2')], 'delivery_second')
    result = settled(client, second['id'])
    assert result['summary']['score'] == 100
    assert result['delivery']['state'] == 'actionable'
    assert result['delivery']['history']['cases'] == 1
    full = client.get(f"/evaluation/api/runs/{second['id']}/delivery").json()
    assert full['preview'] is False and full['own_cases'] == 1 and full['history_cases'] == 1
    assert [r['source_run_id'] for r in full['items']] == [second['id'], first['id']]
    history = client.get(f"/evaluation/api/runs/{second['id']}/delivery?scope=history&limit=1").json()
    assert history['total'] == 1 and history['items'][0]['question'] == 'Synthetic earlier'
    assert client.get(f"/evaluation/api/runs/{second['id']}/delivery?scope=history&offset=1").json()['items'] == []
    assert client.get(f"/evaluation/api/runs/{second['id']}/delivery?scope=bad").status_code == 422
    tasks = client.get('/evaluation/api/package-tasks').json()['items'][0]
    assert tasks['pending_deliveries'] == 1 and tasks['historical_failures'] == 1
    client.headers['Authorization'] = 'Bearer synthetic-user-b'
    assert client.get(f"/evaluation/api/runs/{second['id']}/delivery").status_code == 404


def test_passing_delivery_has_no_optimization_and_legacy_is_unknown(service):
    client, app, _, _, _ = service
    current = start(client, preparation(client), [('Synthetic correct', '2')], 'delivery_pass')
    result = settled(client, current['id'])
    assert result['delivery']['state'] == 'none'
    assert client.get('/evaluation/api/package-tasks').json()['items'][0]['pending_deliveries'] == 0
    store, jobs = app.state.store, app.state.jobs
    owner = owner_of(app, current['id'])
    with store.transaction():
        store.db.execute("DELETE FROM artifacts WHERE run_id=? AND name='suggestions.items'", (current['id'],))
    assert delivery.summary(store, jobs, owner, store.get(owner, current['id']))['state'] == 'unknown'


def test_feedback_preview_is_provisional_deduplicated_and_read_only(service):
    client, app, _, _, verdicts = service
    verdicts['Synthetic earlier'] = 'fail'
    prep = preparation(client)
    first = start(client, prep, [('Synthetic earlier', '2')], 'preview_first')
    settled(client, first['id'])
    second = start(client, prep, [('Synthetic current', '2')], 'preview_second')
    settled(client, second['id'])
    owner = owner_of(app, second['id'])
    store, jobs = app.state.store, app.state.jobs
    run = store.get(owner, second['id'])
    # Exercise preview rendering with a synthetic feedback run, without notifying anyone.
    run['origin'] = 'consumer_feedback'
    store.put_optimization(owner, second['id'], {'status': 'awaiting_review'})
    before = store.artifact(owner, second['id'], 'report.md')
    preview = delivery.content(store, jobs, owner, run, 'all', 0, 20)
    assert preview['preview'] and preview['provisional']
    assert preview['history_cases'] == 1
    assert store.artifact(owner, second['id'], 'report.md') == before
    assert store.optimization(second['id'])['status'] == 'awaiting_review'
    # Newest conclusions exclude the same historical question.
    run['cases'][0]['question'] = 'Synthetic earlier'
    preview = delivery.content(store, jobs, owner, run, 'all', 0, 20)
    assert preview['history_cases'] == 0


def test_type_filter_counts_before_pagination_and_preview_is_bounded(service):
    client, app, _, _, _ = service
    current = start(client, preparation(client), [('Synthetic correct', '2')], 'delivery_types')
    settled(client, current['id'])
    store, owner = app.state.store, owner_of(app, current['id'])
    original = store.get(owner, current['id'])
    solver = store.create(owner, 'runs', {**{k: v for k, v in original.items() if k not in ('id', 'revision')},
                                        'run_type': 'solver', 'created_at': original['created_at'] + 1})
    package = original['package_ref']['ontology_id']
    page = client.get('/evaluation/api/runs', params={'ontology_id': package, 'run_type': 'data', 'limit': 1}).json()
    assert page['total'] == 1 and page['items'][0]['id'] == current['id']
    page = client.get('/evaluation/api/runs', params={'ontology_id': package, 'run_type': 'solver', 'limit': 1}).json()
    assert page['total'] == 1 and page['items'][0]['id'] == solver['id']
    revision, raw = store.artifact(owner, current['id'], 'answers.json')
    value = json.loads(raw)
    value['answers'][0].update(question='q' * 4000, finalAnswer='a' * 8000, reason='r' * 4000)
    store.put_artifacts(owner, current['id'], revision, {'answers.json': json.dumps(value)})
    content = client.get(f"/evaluation/api/runs/{current['id']}/delivery").json()['items'][0]
    assert len(content['question']) == 1000 and len(content['reason']) == 1000
    assert len(content['finalAnswer']) == 2000 and content['truncated']
