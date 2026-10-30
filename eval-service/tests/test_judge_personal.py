"""Personal Judge configuration overrides the shared default per user; all identities and keys are synthetic."""
from evaluation.core import Failure
from test_evaluation import environment, preparation, run, wait

ME = '/evaluation/api/judge-settings/me'
PERSONAL = {'base_url': 'https://judge-mine.example.com/v1', 'model': 'synthetic-mine', 'api_key': 'synthetic-mine-key'}
OTHER = {'Authorization': 'Bearer synthetic-user-b'}


def test_personal_configuration_overrides_only_the_owner_and_can_be_removed(environment):
    client, app, deps, cfg = environment
    default = client.get(ME).json()
    assert default == {'personal': None, 'active': {'source': 'deployment', 'model': 'synthetic', 'revision': '1', 'configured': True}}
    saved = client.put(ME, json=PERSONAL)
    assert saved.status_code == 200, saved.text
    mine = saved.json()
    assert mine['personal'] == {'base_url': 'https://judge-mine.example.com/v1', 'model': 'synthetic-mine',
                                'revision': mine['personal']['revision'], 'api_key_set': True}
    assert mine['personal']['revision'].startswith('me-') and mine['active']['source'] == 'personal'
    assert 'synthetic-mine-key' not in saved.text
    assert all('synthetic-mine-key' not in row[0] for row in app.state.store.db.execute('SELECT body FROM settings'))
    session = client.get('/evaluation/api/session').json()
    assert session['judge'] == {'model': 'synthetic-mine', 'revision': mine['personal']['revision'], 'source': 'personal'}
    # Another user and the shared default are unaffected.
    assert client.get(ME, headers=OTHER).json()['active']['source'] == 'deployment'
    assert client.get('/evaluation/api/judge-settings').json()['source'] == 'deployment'

    deps.judge_calls.clear()
    result = wait(client, run(client, preparation(client))['id'])
    assert result['status'] == 'completed', result
    assert set(deps.judge_calls) == {('https://judge-mine.example.com/v1', 'synthetic-mine-key', 'synthetic-mine')}
    assert result['judge']['revision'] == mine['personal']['revision']

    reset = client.delete(ME).json()
    assert reset == default
    deps.judge_calls.clear()
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic default', 'cases': [{'question': 'Synthetic', 'reference': '2'}]}).json()
    again = client.post('/evaluation/api/runs', json={'request_id': 'run_default_judge', 'preparation_id': preparation(client)['id'], 'eval_set_id': cases['id']}).json()
    assert wait(client, again['id'])['status'] == 'completed'
    assert set(deps.judge_calls) == {('https://judge.example.com', 'synthetic-judge', 'synthetic')}


def test_personal_key_is_never_borrowed_from_the_shared_default(environment):
    client, app, deps, cfg = environment
    rejected = client.put(ME, json={'base_url': 'https://judge.example.com', 'model': 'synthetic-other', 'api_key': ''})
    assert rejected.status_code == 422 and rejected.json()['code'] == 'judge_key_required'
    assert client.post(ME + '/test', json={**PERSONAL, 'api_key': ''}).status_code == 422
    client.put(ME, json=PERSONAL)
    # Re-saving with an empty key keeps the user's own key and revision for the same gateway.
    first = client.get(ME).json()['personal']['revision']
    kept = client.put(ME, json={**PERSONAL, 'api_key': ''}).json()
    assert kept['personal']['revision'] == first
    assert client.put(ME, json={**PERSONAL, 'base_url': 'https://judge-other.example.com', 'api_key': ''}).status_code == 422


def test_any_user_can_test_a_personal_configuration_without_saving(environment):
    client, app, deps, cfg = environment
    deps.judge_calls.clear()
    assert client.post(ME + '/test', json=PERSONAL, headers=OTHER).json() == {'ok': True}
    assert deps.judge_calls == [('https://judge-mine.example.com/v1', 'synthetic-mine-key', 'synthetic-mine')]
    assert client.get(ME, headers=OTHER).json()['personal'] is None


def test_admin_default_changes_do_not_replace_a_personal_override(environment):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-b'] = 'admin'
    client.put(ME, json=PERSONAL)
    shared = {'base_url': 'https://judge-shared.example.com', 'model': 'synthetic-shared', 'api_key': 'synthetic-shared-key'}
    assert client.put('/evaluation/api/judge-settings', json=shared, headers=OTHER).status_code == 200
    assert client.get(ME).json()['active']['model'] == 'synthetic-mine'
    assert client.get(ME, headers=OTHER).json()['active'] == {'source': 'settings', 'model': 'synthetic-shared',
                                                               'revision': client.get('/evaluation/api/judge-settings').json()['revision'],
                                                               'configured': True}


def test_changing_the_personal_configuration_stops_judging_of_existing_runs(environment):
    client, app, deps, cfg = environment
    client.put(ME, json=PERSONAL)
    owner = client.get('/evaluation/api/session').json()['subject']
    expected = app.state.engine.judge.profile(owner=owner)
    client.put(ME, json={**PERSONAL, 'model': 'synthetic-mine-2'})
    try:
        app.state.engine.judge.score('Synthetic', '2', '2', expected=expected, owner=owner)
        raise AssertionError('a changed personal configuration must not judge an existing run')
    except Failure as exc:
        assert exc.code == 'configuration_changed'


def test_admin_can_disable_the_default_for_users_without_a_personal_configuration(environment):
    client, app, deps, cfg = environment
    deps.roles['synthetic-user-b'] = 'admin'
    url = '/evaluation/api/judge-settings'
    assert client.get(url).json()['enabled'] is True
    assert client.put(url + '/enabled', json={'enabled': False}).status_code == 403
    owner = client.get('/evaluation/api/session').json()['subject']
    expected = app.state.engine.judge.profile(owner=owner)

    off = client.put(url + '/enabled', json={'enabled': False}, headers=OTHER)
    assert off.status_code == 200 and off.json()['enabled'] is False
    assert client.get(url).json()['enabled'] is False
    assert client.get(ME).json()['active'] == {'source': 'disabled', 'model': '', 'revision': '', 'configured': False}
    session = client.get('/evaluation/api/session').json()
    assert session['judge_configured'] is False and session['judge']['source'] == 'disabled'
    cases = client.post('/evaluation/api/eval-sets', json={'name': 'Synthetic set', 'cases': [{'question': 'Synthetic', 'reference': '2'}]}).json()
    rejected = client.post('/evaluation/api/runs', json={'request_id': 'run_disabled', 'preparation_id': preparation(client)['id'], 'eval_set_id': cases['id']})
    assert rejected.status_code == 409 and rejected.json()['code'] == 'judge_default_disabled'
    # Runs already using the default stop judging, while the built-in NL2SQL keeps the default.
    try:
        app.state.engine.judge.score('Synthetic', '2', '2', expected=expected, owner=owner)
        raise AssertionError('a disabled default must not judge existing runs')
    except Failure as exc:
        assert exc.code == 'judge_default_disabled'
    assert app.state.engine.judge.settings.effective()['source'] == 'deployment'

    # A personal configuration is unaffected.
    client.put(ME, json=PERSONAL, headers=OTHER)
    assert client.get(ME, headers=OTHER).json()['active']['source'] == 'personal'

    # Re-enabling keeps the revision, so stopped runs can resume.
    on = client.put(url + '/enabled', json={'enabled': True}, headers=OTHER).json()
    assert on['enabled'] is True and on['revision'] == expected['revision']
    assert app.state.engine.judge.score('Synthetic', '2', '2', expected=expected, owner=owner)['verdict'] == 'pass'
