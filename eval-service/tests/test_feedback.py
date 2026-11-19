"""Consumer feedback in the data flywheel: intake, processing into feedback runs, report, review and delivery.

All users, packages, questions, answers and dependency responses are synthetic.
"""
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from evaluation.api import create_app
from evaluation.core import Config, Failure, digest
from evaluation.feedback.store import run_owner
from test_evaluation import Dependencies
from test_run_artifacts import Modeling, until

SERVICE = 'synthetic-flywheel-service-token-0123456789'
PACKAGE = 'knowledge_package_demo'
PUSH = '/evaluation/api/internal/feedback-signals'


@pytest.fixture
def flywheel(tmp_path):
    deps, modeling, asked = Dependencies(), Modeling(), []
    original = deps.json
    answers = {'submit_correction': {'is_correction': True, 'correct_statement': 'Synthetic correct statement'},
               'submit_question': {'question': 'Synthetic standalone question'},
               'submit_match': {'relation': 'unrelated', 'cluster': ''}}

    def transport(method, base, path, **kwargs):
        token = str(kwargs.get('token') or '')
        if path == '/userinfo' and token.startswith('synthetic-user-'):
            return {'sub': token, 'role': 'user'}
        if path == '/api/internal/evaluation/notifications':
            return modeling(kwargs)
        if path == '/chat/completions':
            tool = kwargs['json']['tool_choice']['function']['name']
            messages = kwargs['json']['messages']
            asked.append((tool, messages[0]['content'], json.loads(messages[1]['content'])))
            if tool == 'submit_suggestions':
                arguments = {'summary': 'Synthetic summary', 'items': [
                    {'cases': [1], 'suggestions': [1], 'category': 'B', 'evidence': 'Synthetic evidence',
                     'suggestion': 'Synthetic generic revision', 'targets': ['objects/item.md']}]}
            elif tool in answers:
                arguments = answers[tool]
            else:
                arguments = {'verdict': 'pass', 'reason': 'Synthetic judgement'}
            return {'choices': [{'message': {'tool_calls': [{'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}}]}
        return original(method, base, path, **kwargs)

    deps.json = transport
    cfg = Config(tmp_path, auth_url='https://auth.example.com', issuer='synthetic', hub_url='https://hub.example.com',
                 hub_token='synthetic-service', consumer_url='https://consumer.example.com',
                 consumer_token='synthetic-evaluation-service', encryption_key=Fernet.generate_key().decode(),
                 judge_url='https://judge.example.com', judge_key='synthetic-judge', judge_model='synthetic', judge_revision='1',
                 modeling_url='https://modeling.example.com', modeling_service_token='synthetic-notification-credential',
                 feedback_enabled='true', flywheel_token=SERVICE, notify_feedback_runs='true', feedback_seal_clusters='3')
    app = create_app(cfg, deps)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer synthetic-user-a'
        yield SimpleNamespace(client=client, app=app, deps=deps, modeling=modeling, asked=asked, answers=answers, cfg=cfg)


def signal(kind='turn_feedback', run='run_1', seq=1, vote='down', question='Synthetic question one', user='synthetic-user-a',
           version='0.1.1', detected_by=None, **parts):
    key = f'workspace_demo|{run}|{kind}|{seq}|{detected_by or ""}'
    value = {'schema': 'consumer-feedback-signal.v1', 'signal_id': 'sig_' + hashlib.sha256(key.encode()).hexdigest()[:32],
             'signal_type': kind, 'signal_seq': seq, 'occurred_at': '2026-11-04T08:00:00Z',
             'origin': {'service': 'consumer', 'workspace_id': 'workspace_demo', 'session_id': 'session_demo', 'run_id': run},
             'actor': {'user_id': user}, 'package': {'ontology_id': PACKAGE, 'version': version}, 'future_field': {'kept': True},
             'qa': {'question': question, 'answer': 'Synthetic answer', 'turn_status': 'success'},
             'evidence': {'evidence_level': 'full', 'tool_event_total': 2,
                          'tool_failures': [{'tool': 'execute_script', 'error_code': 'SCRIPT_ARGUMENT_VALIDATION_FAILED',
                                             'message': 'Synthetic invalid argument'}],
                          'tool_events': [{'index': 1, 'tool': 'ontology_context', 'arguments': '{}', 'result': 'Synthetic context'},
                                          {'index': 2, 'tool': 'execute_script', 'arguments': '{"value": 1}',
                                           'result': 'Synthetic error', 'failed': True}]}}
    if kind == 'turn_feedback':
        value['feedback'] = {'vote': vote, **parts.pop('feedback', {})}
    if kind == 'error_qa':
        value['error_detection'] = {'detected_by': detected_by, 'rules': parts.pop('rules', [])}
    if kind == 'user_suggestion':
        value.pop('qa')
        value['suggestion'] = {'content': parts.pop('content', 'Synthetic suggestion: include the overseas regions'),
                               'scope_hint': {'operation_key': 'region_summary'}}
    value.update(parts)
    return value


def push(env, signals, token='synthetic-user-a', service=SERVICE):
    return env.client.post(PUSH, json={'schema': 'consumer-feedback-signal-batch.v1', 'signals': signals},
                           headers={'Authorization': f'Bearer {token}', 'X-Eval-Service-Token': service})


def feedback_runs(env):
    response = env.client.get('/evaluation/api/runs', params={'origin': 'consumer_feedback', 'ontology_id': PACKAGE})
    assert response.status_code == 200, response.text
    return response.json()['items']


def sealed(env):
    """Process the received feedback and return the feedback run once its report exists."""
    env.app.state.feedback.process()
    def ready():
        runs = feedback_runs(env)
        return runs[0] if runs and runs[0]['status'] == 'completed' and runs[0]['artifacts'] else None
    return until(ready, 'feedback run was not sealed')


def test_intake_checks_the_caller_and_stores_each_signal_once(flywheel):
    env = flywheel
    first = signal()
    assert push(env, [first], service='wrong').json()['code'] == 'service_forbidden'
    assert push(env, [first], token='synthetic-expired').status_code == 401
    assert push(env, [first], token='synthetic-expired').json()['code'] == 'invalid_identity'
    assert env.client.post(PUSH, json={'schema': 'consumer-feedback-signal-batch.v1', 'signals': [first]},
                           headers={'Authorization': '', 'X-Eval-Service-Token': SERVICE}).status_code == 401
    assert env.client.post(PUSH, json={'schema': 'other', 'signals': [first]},
                           headers={'X-Eval-Service-Token': SERVICE}).json()['code'] == 'invalid_input'
    assert push(env, []).status_code == 422
    changed = dict(first, qa={'question': 'Synthetic other question', 'answer': 'Synthetic answer'})
    other_user = signal(run='run_2', user='synthetic-user-b')
    broken = dict(signal(run='run_3'), signal_type='unknown')
    large = signal(run='run_4', qa={'question': 'Q', 'answer': 'x' * 300000})
    response = push(env, [first, first, changed, other_user, broken, large])
    assert response.status_code == 202
    results = response.json()['results']
    assert [r['status'] for r in results] == ['accepted', 'duplicate', 'conflict', 'rejected', 'rejected', 'rejected']
    assert [r.get('code') for r in results[2:]] == ['payload_mismatch', 'actor_mismatch', 'schema_invalid', 'signal_too_large']
    rows = env.app.state.store.db.execute('SELECT * FROM feedback_signals').fetchall()
    assert len(rows) == 1
    # Neither the user name nor any content is stored in plain text; unknown fields are kept for later versions.
    assert all('synthetic-user-a' not in str(tuple(row)) and 'Synthetic question one' not in str(tuple(row)) for row in rows)
    kept = env.app.state.store.decrypt(rows[0]['body'])
    assert 'actor' not in kept and kept['future_field'] == {'kept': True}


def test_intake_is_off_unless_enabled(flywheel):
    env = flywheel
    env.cfg.feedback_enabled = ''
    response = push(env, [signal()])
    assert response.status_code == 503 and response.json()['code'] == 'feedback_disabled'
    assert response.json()['retryable'] is True


def test_feedback_becomes_a_report_that_says_what_users_reported(flywheel):
    env = flywheel
    signals = [signal(feedback={'reason_category': 'wrong_answer', 'comment': 'Synthetic wrong period'}),
               signal(run='run_2', question='Synthetic question two', feedback={'expected_answer': 'Synthetic expected'}),
               signal('user_suggestion', run='run_3', seq=0, qa=None)]
    signals[2].pop('qa', None)
    assert all(r['status'] == 'accepted' for r in push(env, signals).json()['results'])
    run = sealed(env)
    assert run['origin'] == 'consumer_feedback' and run['optimization']['status'] == 'awaiting_review'
    assert run['summary']['planned'] == 2 and run['artifacts']['suggestions'] == 'pending'
    report = env.client.get(f"/evaluation/api/runs/{run['id']}/artifacts/report.md").text
    assert '# 消费反馈评测报告' in report and '本次没有执行评测用例' in report and '本报告不计算得分' in report
    assert '### 1. ❌ fail' in report and '### 2. ⚠️ partial' in report
    assert '- **问题**：Synthetic question two' in report and 'Synthetic expected' in report
    assert '用户反馈：答案错误（Synthetic wrong period）' in report and '`execute_script`' in report
    assert '## 用户建议' in report and '操作：region_summary' in report
    assert 'synthetic-user-a' not in report
    # Waiting for an owner: modeling is not told yet.
    assert not [body for _, body in env.modeling.received if body['run_id'] == run['id']]


def test_an_owner_reviews_generates_and_confirms_for_modeling(flywheel):
    env = flywheel
    push(env, [signal(feedback={'reason_category': 'missing_data'}),
               signal(run='run_2', question='Synthetic question two', feedback={'comment': 'Synthetic wrong'}),
               signal(run='run_3', question='Synthetic question three', feedback={'comment': 'Synthetic wrong'})])
    run = sealed(env)
    rid = run['id']
    # Another user cannot see the package in Hub, so neither the run nor that it exists.
    other = {'Authorization': 'Bearer synthetic-user-b'}
    assert env.client.get(f'/evaluation/api/runs/{rid}', headers=other).status_code == 404
    assert env.client.get('/evaluation/api/runs', params={'origin': 'consumer_feedback', 'ontology_id': PACKAGE},
                          headers=other).status_code == 404
    assert env.client.post(f'/evaluation/api/runs/{rid}/resume').json()['code'] == 'not_applicable'
    # The owner supplies a reference answer and marks one answer as the user's own misunderstanding.
    current = env.client.get(f'/evaluation/api/runs/{rid}').json()
    response = env.client.post(f'/evaluation/api/runs/{rid}/results/0/override', json={
        'verdict': 'fail', 'reason': 'Synthetic owner review', 'reference': 'Synthetic owner reference', 'revision': current['revision']})
    assert response.status_code == 200, response.text
    current = env.client.get(f'/evaluation/api/runs/{rid}').json()
    assert env.client.post(f'/evaluation/api/runs/{rid}/results/1/override', json={
        'verdict': 'pass', 'reason': 'Synthetic user misunderstanding', 'revision': current['revision']}).status_code == 200
    assert env.client.post(f'/evaluation/api/runs/{rid}/artifacts/regenerate').status_code == 200
    ready = until(lambda: (lambda r: r if r['artifacts']['suggestions'] == 'ready' else None)(
        env.client.get(f'/evaluation/api/runs/{rid}').json()), 'suggestions')
    # Each correction drafts the suggestions again; the newest draft leaves out the misjudged answer.
    tool, system, payload = [item for item in env.asked if item[0] == 'submit_suggestions'][-1]
    assert '终端用户' in system and [case['number'] for case in payload['cases']] == [1, 3]
    suggestions = env.client.get(f'/evaluation/api/runs/{rid}/artifacts/suggestions.md').text
    assert '覆盖：2 条错误问答' in suggestions
    assert ready['artifacts']['suggestions_stale'] is False
    # The package list counts runs still waiting for an owner.
    tasks = env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'}).json()['items']
    assert [(item['ontology_id'], item['awaiting_review']) for item in tasks] == [(PACKAGE, 1)]
    # The owner sees what waits for the package as a whole: questions, user suggestions and since when.
    assert (tasks[0]['awaiting_cases'], tasks[0]['awaiting_suggestions'], tasks[0]['handed']) == (env.client.get(f'/evaluation/api/runs/{rid}/results').json()['total'], 0, 0)
    assert tasks[0]['awaiting_since'] == ready['created_at']
    assert ready['summary']['score'] is None
    # Confirming finds the release of the version in Hub and tells modeling, filed under the confirming owner.
    confirmed = env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'confirm'})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['optimization']['status'] == 'notified'
    subject = env.client.get('/evaluation/api/session').json()['subject']
    body = until(lambda: next((b for _, b in env.modeling.received if b['run_id'] == rid), None), 'notification')
    assert body['origin'] == 'consumer_feedback' and body['owner'] == subject and body['release_id'] == 'release_demo'
    assert body['summary']['score'] is None
    tasks = env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'}).json()['items']
    assert (tasks[0]['awaiting_review'], tasks[0]['awaiting_cases'], tasks[0]['handed']) == (0, 0, 1)
    assert tasks[0]['transport'] == {'delivered': 1, 'pending': 0, 'failed': 0, 'none': 0}
    # Modeling sees it per release when it asks for all sources, and only then.
    listed = env.client.get('/evaluation/api/package-evaluations', params={'ontology_id': PACKAGE, 'origin': 'all'}).json()
    assert [entry['origin'] for entry in listed['releases']] == ['consumer_feedback']
    assert env.client.get('/evaluation/api/package-evaluations', params={'ontology_id': PACKAGE}).json()['releases'] == []
    # A solver validation of the same version is listed beside the feedback run, in its own section.
    from evaluation.core import Identity, canonical
    owner = Identity(env.cfg, env.deps).verify('synthetic-user-a')
    reference = {'ontology_id': PACKAGE, 'release_id': 'release_demo', 'version': '0.1.1'}
    solver_run = env.app.state.store.create(owner, 'runs', dict(
        status='completed', run_type='solver', base_ref=reference, package_ref=reference, created_at=1.0, finished_at=2.0))
    env.app.state.store.put_artifacts(owner, solver_run['id'], 1, {'meta': canonical(dict(
        revision=1, status='ready', report='ready', suggestions='ready', summary={'total': 1},
        solver_summary={'total': 1, 'passed': 1, 'failed': 0}))})
    both = env.client.get('/evaluation/api/package-evaluations', params={'ontology_id': PACKAGE, 'origin': 'all'}).json()
    assert [entry['origin'] for entry in both['releases']] == ['consumer_feedback']
    assert [entry['run_id'] for entry in both['solver_releases']] == [solver_run['id']]
    assert both['solver_releases'][0]['solver_summary']['failed'] == 0 and both['solver_releases'][0]['optimization'] is None
    assert env.client.post(f'/evaluation/api/runs/{rid}/results/0/override', json={
        'verdict': 'fail', 'reason': 'Synthetic', 'revision': ready['revision']}).json()['code'] == 'invalid_state'
    # Modeling writes back the release it published from these suggestions.
    receipt = dict(env.deps.receipt, release_id='release_demo_next', version='0.1.2', package_digest=digest(env.deps.raw))
    env.deps.releases['release_demo_next'] = (env.deps.raw, receipt)
    written = env.client.post(f'/evaluation/api/runs/{rid}/modeling-result',
                              json={'request_id': 'result_feedback', 'outcome': 'done', 'release_id': 'release_demo_next'})
    assert written.status_code == 200 and written.json()['optimization']['status'] == 'modeling_done'


def test_an_owner_may_dismiss_a_feedback_run(flywheel):
    env = flywheel
    push(env, [signal(feedback={'comment': 'Synthetic'}), signal(run='run_2', question='Synthetic two', feedback={'comment': 'x'}),
               signal(run='run_3', question='Synthetic three', feedback={'comment': 'x'})])
    rid = sealed(env)['id']
    assert env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'dismiss'}).status_code == 422
    response = env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'dismiss', 'reason': 'Synthetic noise'})
    assert response.json()['optimization']['status'] == 'dismissed'
    assert env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'confirm'}).status_code == 409


def test_the_flywheel_delegation_reads_feedback_runs_its_user_confirmed(flywheel):
    env = flywheel
    push(env, [signal(feedback={'comment': 'Synthetic wrong'}), signal(run='run_2', question='Synthetic two', feedback={'comment': 'x'}),
               signal(run='run_3', question='Synthetic three', feedback={'comment': 'x'})])
    rid = sealed(env)['id']

    def delegation(user='synthetic-user-a', package=PACKAGE):
        body = {'ontology_id': package, 'expires_in': 86400, 'delegation': secrets.token_urlsafe(32)}
        response = env.client.post('/evaluation/api/delegations', json=body,
                                   headers={'Authorization': f'Bearer {user}',
                                            'X-Modeling-Service-Token': 'synthetic-notification-credential'})
        assert response.status_code == 201, response.text
        return {'Authorization': '', 'X-Modeling-Service-Token': 'synthetic-notification-credential',
                'X-Evaluation-Delegation': response.json()['delegation']}
    mine, other_user, other_package = delegation(), delegation('synthetic-user-b'), delegation(package='other_package_demo')
    # 负责人确认交给建模之前，数据飞轮读不到这一轮
    assert env.client.get(f'/evaluation/api/runs/{rid}', headers=mine).status_code == 404
    assert env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'confirm'}).status_code == 200
    # 确认后等优化建议写好、随同一知识包的待交付轮次一起交给建模
    until(lambda: (env.client.get(f'/evaluation/api/runs/{rid}').json().get('optimization') or {}).get('status') == 'notified',
          'feedback run was not handed over')
    # 确认人登记的委托能读到这一轮和它的报告：用户不在时数据飞轮也能优化
    assert env.client.get(f'/evaluation/api/runs/{rid}', headers=mine).json()['origin'] == 'consumer_feedback'
    assert env.client.get(f'/evaluation/api/runs/{rid}/artifacts/report.md', headers=mine).status_code == 200
    # 别人的委托、别的知识包的委托都读不到
    for auth in (other_user, other_package):
        assert env.client.get(f'/evaluation/api/runs/{rid}', headers=auth).status_code == 404
        assert env.client.get(f'/evaluation/api/runs/{rid}/artifacts/report.md', headers=auth).status_code == 404


def test_only_the_newest_vote_of_a_consumer_run_counts(flywheel):
    env = flywheel
    store = env.app.state.store
    push(env, [signal(seq=2, feedback={'comment': 'Synthetic newer'})])
    env.app.state.feedback.process()
    push(env, [signal(seq=1, feedback={'comment': 'Synthetic older'})])
    env.app.state.feedback.process()
    states = dict(store.db.execute('SELECT signal_seq,processing_status FROM feedback_signals').fetchall())
    assert states == {2: 'clustered', 1: 'superseded'}
    push(env, [signal(seq=3, vote='none')])
    env.app.state.feedback.process()
    states = dict(store.db.execute('SELECT signal_seq,processing_status FROM feedback_signals').fetchall())
    assert states == {1: 'superseded', 2: 'superseded', 3: 'withdrawn'}
    assert store.db.execute('SELECT downvotes FROM feedback_clusters').fetchone()[0] == 0


def test_thumbs_down_alone_waits_for_enough_different_users(flywheel):
    env = flywheel
    store = env.app.state.store
    for index in range(4):
        # One user voting on four runs is still one user.
        push(env, [signal(run=f'run_a{index}')])
    env.app.state.feedback.process()
    status, users, reason = store.db.execute('SELECT status,distinct_users,status_reason FROM feedback_clusters').fetchone()
    assert (status, users, reason) == ('open', 1, 'insufficient_info')
    for user in ('synthetic-user-b', 'synthetic-user-c'):
        push(env, [signal(run=f'run_{user}', user=user)], token=user)
    env.app.state.feedback.process()
    assert tuple(store.db.execute('SELECT status,distinct_users FROM feedback_clusters').fetchone()) in (('included', 3), ('ready', 3))


def test_environment_errors_are_not_given_to_modeling(flywheel):
    env = flywheel
    push(env, [signal('error_qa', seq=0, detected_by='system_rule', rules=['error_code:MODEL_TIMEOUT'])])
    env.app.state.feedback.process()
    status, reason = env.app.state.store.db.execute('SELECT status,status_reason FROM feedback_clusters').fetchone()
    assert (status, reason) == ('dismissed', 'runtime_error')


def test_corrections_are_confirmed_and_follow_ups_rewritten(flywheel):
    env = flywheel
    store = env.app.state.store
    correction = {'text': 'Synthetic: no, count by calendar month', 'matched': 'no', 'next_answer': 'Synthetic next answer'}
    follow_up = {'question': 'Synthetic and last month?', 'answer': 'Synthetic answer', 'conversation_context': ['Synthetic first question']}
    push(env, [signal('error_qa', seq=0, detected_by='user_correction', correction=correction, qa=follow_up)])
    env.app.state.feedback.process()
    row = store.db.execute("SELECT level,derived,processing_status FROM feedback_signals").fetchone()
    derived = store.decrypt(row['derived'])
    assert row['processing_status'] == 'clustered' and row['level'] == 'L3'
    assert derived['question'] == 'Synthetic standalone question' and derived['reference'] == 'Synthetic correct statement'
    env.answers['submit_correction'] = {'is_correction': False, 'correct_statement': ''}
    push(env, [signal('error_qa', run='run_2', seq=0, detected_by='user_correction', correction=correction)])
    env.app.state.feedback.process()
    assert store.db.execute("SELECT count(*) FROM feedback_signals WHERE processing_status='not_correction'").fetchone()[0] == 1


def test_corrections_wait_while_the_default_judge_is_disabled(flywheel):
    env = flywheel
    env.app.state.engine.judge.settings.set_default_enabled(False, 'synthetic-admin')
    correction = {'text': 'Synthetic: wrong', 'next_answer': 'Synthetic'}
    push(env, [signal('error_qa', seq=0, detected_by='user_correction', correction=correction)])
    env.app.state.feedback.process()
    assert env.app.state.store.db.execute('SELECT processing_status FROM feedback_signals').fetchone()[0] == 'received'
    env.app.state.engine.judge.settings.set_default_enabled(True, 'synthetic-admin')
    env.app.state.feedback.process()
    assert env.app.state.store.db.execute('SELECT processing_status FROM feedback_signals').fetchone()[0] == 'clustered'


def test_deleting_a_feedback_run_returns_its_clusters(flywheel):
    env = flywheel
    push(env, [signal(feedback={'comment': 'Synthetic'}), signal(run='run_2', question='Synthetic two', feedback={'comment': 'x'}),
               signal(run='run_3', question='Synthetic three', feedback={'comment': 'x'})])
    rid = sealed(env)['id']
    assert env.client.delete(f'/evaluation/api/runs/{rid}').status_code == 200
    statuses = {row[0] for row in env.app.state.store.db.execute('SELECT status FROM feedback_clusters').fetchall()}
    assert statuses == {'ready'}
    env.app.state.feedback.process()
    runs = feedback_runs(env)
    assert len(runs) == 1 and runs[0]['id'] != rid


def test_feedback_tasks_list_packages_the_caller_may_read(flywheel):
    env = flywheel
    push(env, [signal(feedback={'comment': 'Synthetic'})])
    env.app.state.feedback.process()
    tasks = env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'}).json()
    assert tasks['total'] == 1 and tasks['items'][0]['ontology_id'] == PACKAGE and tasks['items'][0]['collecting'] is True
    # Feedback names the package by ID only; pages show the name Hub gave the caller.
    assert tasks['items'][0]['name'] == 'Synthetic package'
    assert tasks['items'][0]['latest']['package_ref']['name'] == 'Synthetic package'
    listed = env.client.get('/evaluation/api/runs', params={'origin': 'consumer_feedback', 'ontology_id': PACKAGE}).json()['items']
    assert listed[0]['package_ref']['name'] == 'Synthetic package'
    other = env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'},
                           headers={'Authorization': 'Bearer synthetic-user-b'}).json()
    assert other['total'] == 0
    assert env.app.state.store.db.execute('SELECT owner FROM records WHERE kind=?', ('runs',)).fetchone()[0] == run_owner(PACKAGE)


def test_signal_bodies_are_cleared_when_retention_ends(flywheel):
    env = flywheel
    push(env, [signal()])
    from evaluation.feedback.store import FeedbackStore
    assert FeedbackStore(env.app.state.store).prune(now=10 ** 12) == 1
    assert env.app.state.store.db.execute('SELECT body FROM feedback_signals').fetchone()[0] is None


CONTRACT = Path(__file__).resolve().parents[2] / 'contracts' / 'flywheel' / 'feedback-signal.v1.schema.json'


def test_the_shared_contract_is_generated_from_what_the_service_accepts(tmp_path):
    """contracts/flywheel holds FeedbackSignal v1 for consumer and evaluation; regenerate it when the model changes."""
    from evaluation.feedback.models import write_contract
    if not CONTRACT.is_file():
        pytest.skip('the shared contracts are not beside eval-service, as in a standalone evaluation package')
    write_contract(tmp_path / 'contract.json')
    assert (tmp_path / 'contract.json').read_bytes() == CONTRACT.read_bytes()


def test_a_json_schema_validator_and_the_service_agree_on_signals():
    """Consumer validates outgoing signals with the contract; it must accept and refuse what the service does."""
    jsonschema = pytest.importorskip('jsonschema')
    from pydantic import ValidationError
    from evaluation.feedback.models import FeedbackSignalV1, contract
    validator = jsonschema.Draft202012Validator(contract())
    correction = {'text': 'Synthetic: no, by calendar month', 'next_answer': 'Synthetic'}
    valid = [signal(), signal(vote='up'), signal('error_qa', seq=0, detected_by='system_rule', rules=['tool_failure:execute_sql']),
             signal('error_qa', seq=0, detected_by='user_correction', correction=correction),
             {key: value for key, value in signal('user_suggestion', seq=0).items() if key != 'qa'}]
    invalid = [{key: value for key, value in signal().items() if key != 'feedback'},
               signal('error_qa', seq=0, detected_by='user_correction'),
               {key: value for key, value in signal('user_suggestion', seq=0).items() if key not in ('qa', 'package')},
               dict(signal(), signal_type='unknown'), dict(signal(), signal_id='not-an-id'),
               dict(signal(), qa={'question': 'x' * 4001, 'answer': ''})]
    for item in valid:
        assert not list(validator.iter_errors(item)), item['signal_type']
        FeedbackSignalV1.model_validate(item)
    for item in invalid:
        assert list(validator.iter_errors(item)), item.get('signal_type')
        with pytest.raises(ValidationError):
            FeedbackSignalV1.model_validate(item)


def test_feedback_run_content_is_encrypted_at_rest(flywheel):
    env = flywheel
    push(env, [signal(feedback={'comment': 'Synthetic private comment'}), signal(run='run_2', question='Synthetic two', feedback={'comment': 'x'}),
               signal(run='run_3', question='Synthetic three', feedback={'comment': 'x'})])
    run = sealed(env)
    raw = env.app.state.store.db.execute('SELECT body FROM records WHERE id=?', (run['id'],)).fetchone()[0]
    assert 'Synthetic question one' not in raw and 'Synthetic answer' not in raw and 'Synthetic private comment' not in raw
    assert json.loads(raw)['status'] == 'completed'
    assert env.client.get(f"/evaluation/api/runs/{run['id']}/results/0/answer").json()['answer'] == 'Synthetic answer'


HEALTH = '/evaluation/api/internal/feedback-signals/health'


def checking_identity(env, answer=None):
    """Count account service lookups; ``answer`` replaces the account service's reply, or 'unavailable' makes it fail."""
    calls, original = [], env.deps.json

    def transport(method, base, path, **kwargs):
        if path == '/userinfo':
            calls.append(kwargs.get('token'))
            if answer == 'unavailable':
                raise Failure('dependency_unavailable', '依赖服务不可用，请重试或恢复任务', 503, 'dependency')
            if answer is not None:
                return answer(kwargs.get('token'))
        return original(method, base, path, **kwargs)

    env.deps.json = transport
    return calls


def health(env, token='synthetic-user-a', service=SERVICE):
    return env.client.get(HEALTH, headers={'Authorization': f'Bearer {token}' if token else '', 'X-Eval-Service-Token': service})


def test_the_connectivity_check_reports_what_consumer_needs(flywheel):
    env = flywheel
    response = health(env)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'ok' and body['feedback_enabled'] is True
    assert body['accepted_schemas'] == ['consumer-feedback-signal.v1', 'consumer-feedback-signal-batch.v1']
    assert re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', body['server_time'])
    # Nothing about the caller, tokens or addresses; not for browsers; never cached.
    assert 'synthetic-user-a' not in response.text and SERVICE not in response.text and 'example.com' not in response.text
    assert response.headers['cache-control'] == 'no-store' and 'access-control-allow-origin' not in response.headers
    # Read only: nothing stored.
    assert env.app.state.store.db.execute('SELECT count(*) FROM feedback_signals').fetchone()[0] == 0


def test_the_connectivity_check_answers_while_receiving_is_off(flywheel):
    env = flywheel
    env.cfg.feedback_enabled = 'false'
    response = health(env)
    assert response.status_code == 200 and response.json()['feedback_enabled'] is False
    assert push(env, [signal()]).status_code == 503


@pytest.mark.parametrize('configured', ['', 'short-token', 'synthetic-service', None])
def test_a_wrong_or_unusable_service_token_is_refused_before_the_account_service(flywheel, configured):
    env = flywheel
    calls = checking_identity(env)
    supplied = SERVICE
    if configured is None:
        supplied = 'synthetic-wrong-service-token-0123456789abcdef'
    else:
        # Unset, too short, or equal to another service's token (here the Hub token).
        env.cfg.flywheel_token = configured
        supplied = configured or SERVICE
    response = health(env, service=supplied)
    assert response.status_code == 403
    assert response.json() == {'code': 'service_forbidden', 'message': '服务凭据无效', 'phase': 'authorization', 'retryable': False}
    assert calls == []


@pytest.mark.parametrize('token,answer,status,code,retryable', [
    ('', None, 401, 'invalid_identity', False),
    ('synthetic-expired', None, 401, 'invalid_identity', False),
    ('synthetic-user-a', 'unavailable', 503, 'dependency_unavailable', True),
    ('synthetic-user-a', lambda token: {'sub': token, 'role': 'user', 'password_change_required': True}, 403,
     'account_action_required', False),
])
def test_user_login_problems_are_told_apart_from_the_service_token(flywheel, token, answer, status, code, retryable):
    env = flywheel
    checking_identity(env, answer)
    response = health(env, token=token)
    assert response.status_code == status
    body = response.json()
    assert body['code'] == code and body['retryable'] is retryable and code != 'service_forbidden'
    assert 'synthetic-user' not in response.text and SERVICE not in response.text


def test_the_public_health_check_is_unchanged(flywheel):
    response = flywheel.client.get('/evaluation/api/health', headers={'Authorization': ''})
    assert response.status_code == 200 and response.json() == {'status': 'ok', 'service': 'evaluation'}
    assert response.headers['access-control-allow-origin'] == '*'


def test_suggestions_are_drafted_when_an_owner_visits_and_delivery_waits_for_them(flywheel):
    env = flywheel
    def tasks():
        return env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'}).json()['items'][0]

    push(env, [signal(run=f'run_{n}', question=f'Synthetic question {n}', feedback={'comment': 'Synthetic wrong'}) for n in (1, 2, 3)])
    rid = sealed(env)['id']
    # Sealed in the background without a login: nothing could read the package yet.
    assert env.client.get(f'/evaluation/api/runs/{rid}').json()['artifacts']['suggestions'] == 'pending'
    gate, original = threading.Event(), env.deps.json

    def slow(method, base, path, **kwargs):
        if path == '/chat/completions' and kwargs['json']['tool_choice']['function']['name'] == 'submit_suggestions':
            gate.wait(10)
        return original(method, base, path, **kwargs)
    env.deps.json = slow
    # A visit by someone who may read the package drafts them with that login and the default judge.
    first = tasks()
    assert (first['status'], first['suggestions'], first['awaiting_cases'], first['handed']) == ('pending', 'generating', 3, 0)
    # Handing over now is accepted and waits for the suggestions; corrections are refused meanwhile.
    asked = env.client.post(f'/evaluation/api/runs/{rid}/notifications/retry', json={'decision': 'confirm'}).json()
    assert asked['optimization']['status'] == 'awaiting_review' and asked['optimization']['handover']
    assert tasks()['status'] == 'delivering'
    current = env.client.get(f'/evaluation/api/runs/{rid}').json()
    assert env.client.post(f'/evaluation/api/runs/{rid}/results/0/override', json={
        'verdict': 'pass', 'reason': 'Synthetic', 'revision': current['revision']}).json()['code'] == 'invalid_state'
    assert not any(body['run_id'] == rid for _, body in env.modeling.received)
    gate.set()
    delivered = until(lambda: (lambda r: r if r['optimization']['status'] == 'notified' else None)(
        env.client.get(f'/evaluation/api/runs/{rid}').json()), 'delivery')
    assert delivered['artifacts']['suggestions'] == 'ready' and 'handover' not in delivered['optimization']
    until(lambda: any(body['run_id'] == rid and body['artifacts']['suggestions'] == 'ready' for _, body in env.modeling.received),
          'notification')
    after = tasks()
    assert (after['status'], after['awaiting_review'], after['handed'], after['total_cases']) == ('notified', 0, 1, 3)


def test_an_owner_s_correction_drafts_the_suggestions_again(flywheel):
    env = flywheel
    push(env, [signal(run=f'run_{n}', question=f'Synthetic question {n}', feedback={'comment': 'Synthetic wrong'}) for n in (1, 2, 3)])
    rid = sealed(env)['id']
    env.client.get('/evaluation/api/package-tasks', params={'origin': 'consumer_feedback'})
    ready = until(lambda: (lambda r: r if r['artifacts']['suggestions'] == 'ready' else None)(
        env.client.get(f'/evaluation/api/runs/{rid}').json()), 'suggestions')
    response = env.client.post(f'/evaluation/api/runs/{rid}/results/1/override', json={
        'verdict': 'pass', 'reason': 'Synthetic user misunderstanding', 'revision': ready['revision']})
    assert response.status_code == 200
    again = until(lambda: (lambda r: r if r['artifacts']['suggestions'] == 'ready' and r['artifacts']['revision'] > ready['artifacts']['revision']
                           else None)(env.client.get(f'/evaluation/api/runs/{rid}').json()), 'suggestions again')
    assert again['artifacts']['suggestions_stale'] is False
    payload = [item for item in env.asked if item[0] == 'submit_suggestions'][-1][2]
    assert [case['number'] for case in payload['cases']] == [1, 3]
