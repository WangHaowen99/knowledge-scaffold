"""Consumer outages, rate limits, slow questions, mapping conflicts, notification limits and judge gateways.

All identities, packages, data and dependency responses are synthetic.
"""
import io
import socket
import time
import zipfile

import pytest
import yaml

from evaluation import execution, judge_settings
from evaluation.core import Config, Failure, digest
from evaluation.notifications import Notifier
from evaluation.store import Store
from test_evaluation import environment, preparation, run, wait
from test_run_artifacts import service, settled, start, until


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(execution, 'remote_delay', lambda attempt, hint=None: 0)


def eval_set(client, count, name='Synthetic resilience set'):
    response = client.post('/evaluation/api/eval-sets', json={'name': name, 'cases': [
        {'question': f'Synthetic question {i}', 'reference': '2'} for i in range(count)]})
    assert response.status_code == 200, response.text
    return response.json()


def start_run(client, prep, cases, request_id='run_resilience', concurrency=1):
    response = client.post('/evaluation/api/runs', json={'request_id': request_id, 'preparation_id': prep['id'],
                                                          'eval_set_id': cases['id'], 'concurrency': concurrency})
    assert response.status_code == 202, response.text
    return response.json()


def scripted_submissions(deps, failures):
    """POST /agentic/runs answers with the scripted failures in order, then as usual; records every attempt."""
    original, attempts = deps.json, []

    def dependency(method, base, path, **kwargs):
        if path == '/agentic/runs':
            attempts.append(kwargs['headers'])
            if failures:
                raise failures.pop(0)
        return original(method, base, path, **kwargs)

    deps.json = dependency
    return attempts


def limited(status=429, retry_after=None):
    return Failure('dependency_rejected', '依赖服务拒绝请求', status if status in (429,) else 503, 'dependency',
                   upstream_status=status, retry_after=retry_after)


def test_rate_limited_questions_are_resent_and_marked_as_evaluation_traffic(environment, monkeypatch):
    client, app, deps, cfg = environment
    delays = []
    monkeypatch.setattr(execution, 'remote_delay', lambda attempt, hint=None: delays.append((attempt, hint)) or 0)
    attempts = scripted_submissions(deps, [limited(429, 2.0), limited(503)])
    record = run(client, preparation(client))
    result = wait(client, record['id'])
    assert result['status'] == 'completed', result
    assert len(attempts) == 3 and delays == [(1, 2.0), (2, None)]
    # Every attempt carries the same idempotency key and the evaluation traffic marker.
    assert len({headers['Idempotency-Key'] for headers in attempts}) == 1
    assert all(headers['X-Evaluation-Service-Token'] == cfg.consumer_token for headers in attempts)


def test_the_same_outage_in_three_cases_stops_the_run_and_resume_finishes_it(environment):
    client, app, deps, cfg = environment
    attempts = scripted_submissions(deps, [limited(503) for _ in range(3 * execution.REMOTE_ATTEMPTS)])
    record = start_run(client, preparation(client), eval_set(client, 5))
    result = wait(client, record['id'])
    assert result['status'] == 'interrupted' and result['error']['code'] == 'dependency_circuit_open', result
    # Three cases used up their retries; the remaining two were never submitted.
    assert len(attempts) == 3 * execution.REMOTE_ATTEMPTS
    cases = client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items']
    assert [case['status'] for case in cases] == ['unknown', 'unknown', 'unknown', 'pending', 'pending']
    assert cases[0]['error']['retryable'] is True
    client.post(f"/evaluation/api/runs/{record['id']}/resume", json={})
    time.sleep(.05)
    result = wait(client, record['id'])
    assert result['status'] == 'completed' and result['summary']['counts']['pass'] == 5, result
    assert len(deps.submissions) == 5


def test_a_question_consumer_refuses_does_not_stop_the_others(environment):
    client, app, deps, cfg = environment
    scripted_submissions(deps, [Failure('dependency_rejected', '依赖服务拒绝请求', 422, 'dependency', upstream_status=422)])
    record = start_run(client, preparation(client), eval_set(client, 3))
    result = wait(client, record['id'])
    assert result['status'] == 'completed', result
    counts = result['summary']['counts']
    assert counts['execution_error'] == 1 and counts['pass'] == 2


def test_a_question_still_running_remotely_is_left_for_reconciliation(environment, monkeypatch):
    client, app, deps, cfg = environment
    monkeypatch.setattr(execution, 'REMOTE_SECONDS', 0.05)
    deps.running = True
    record = run(client, preparation(client))
    result = wait(client, record['id'])
    assert result['status'] == 'interrupted' and result['error']['code'] == 'cases_unsettled', result
    case = client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items'][0]
    assert case['status'] == 'executing' and case['error']['code'] == 'remote_timeout'
    deps.running = False
    client.post(f"/evaluation/api/runs/{record['id']}/resume", json={})
    time.sleep(.05)
    assert wait(client, record['id'])['status'] == 'completed'
    assert len(deps.submissions) == 1


def conflicting_package():
    files = {'manifest.yaml': yaml.safe_dump(dict(id='knowledge_package_demo', name='Synthetic package', kind='ontology-package',
                                                  schema_version='0.1', version='0.1.1')),
             'objects/item.md': '# item\n\n# 数据来源\nmissing_items\n\n# 字段\n\n| 英文名称 | 中文名称 | 类型 | 主键 |\n'
                                '| --- | --- | --- | --- |\n| id | 标识 | string | 是 |\n',
             'index.md': '# Synthetic package'}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for key, value in files.items():
            archive.writestr('knowledge_package_demo/' + key, value)
    return output.getvalue()


def test_a_mapping_conflict_names_each_object_and_table(environment):
    client, app, deps, cfg = environment
    deps.raw = conflicting_package()
    deps.receipt['package_digest'] = digest(deps.raw)
    deps.releases['release_demo'] = (deps.raw, deps.receipt)
    upload = client.post('/evaluation/api/inputs', files={'file': ('items.csv', b'id,value\nitem_0001,2\n', 'text/csv')}).json()
    source = client.post('/evaluation/api/data-sources', json={'name': 'Synthetic dataset', 'mode': 'dataset',
                                                                 'input_ids': [upload['id']]}).json()
    response = client.post('/evaluation/api/package-preparations', json={
        'request_id': 'prepare_conflict', 'data_source_id': source['id'],
        'package_ref': {'ontology_id': 'knowledge_package_demo', 'release_id': 'release_demo'}})
    assert response.status_code == 422
    body = response.json()
    assert body['code'] == 'mapping_conflict' and '1 个对象' in body['message']
    issue, = body['details']['issues']
    assert issue['object'] == 'item' and issue['table'] == 'missing_items' and issue['kind'] == 'declared_missing'


class Rejecting:
    def __init__(self, status):
        self.status, self.calls = status, 0

    def json(self, method, base, path, **kwargs):
        self.calls += 1
        raise Failure('dependency_rejected', 'Synthetic rejection', 503, 'dependency', upstream_status=self.status)


@pytest.mark.parametrize('status,limit,outcome', [(404, 3, 'failed'), (503, 4, 'failed'), (422, 1, 'rejected')])
def test_notifications_give_up_after_their_limit_and_retry_starts_over(tmp_path, status, limit, outcome):
    cfg = Config(tmp_path, modeling_url='https://modeling.example.com', modeling_service_token='synthetic-notification-credential',
                 notification_max_attempts='4')
    store, http = Store(tmp_path), Rejecting(status)
    notifier = Notifier(cfg, store, http)
    notifier.enqueue('owner_demo', 'a' * 32, {'status': 'completed'})
    for _ in range(10):
        notifier.deliver_due(now=time.time() + 10 ** 6)
    state = notifier.status('a' * 32)
    assert http.calls == limit and state['status'] == outcome and state['attempts'] == limit
    if outcome == 'failed' and status == 404:
        assert '通知凭据' in state['last_error']
    notifier.retry('owner_demo', 'a' * 32)
    assert notifier.status('a' * 32)['status'] == 'pending' and notifier.status('a' * 32)['attempts'] == 0


def test_a_dataset_run_is_deletable_without_an_evaluation_database(environment):
    client, app, deps, cfg = environment
    from evaluation import data_sources
    store = app.state.store
    sources = data_sources.DataSources(cfg, store)
    # No EVAL_SQL_CONFIG: nothing to drop and no connection attempted.
    sources.cleanup('b' * 32, {'mode': 'dataset', 'provider': 'nl2sql_http'})


INTERNAL_HOST = 'judge.internal.example'


@pytest.fixture
def internal_gateway(monkeypatch):
    """A synthetic gateway host name that resolves to this machine, as a private network host would."""
    resolve = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if host == INTERNAL_HOST:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 0))]
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(judge_settings.socket, 'getaddrinfo', lookup)
    return INTERNAL_HOST


@pytest.mark.parametrize('base_url,allowed,refused', [
    ('http://127.0.0.1:8000/v1', '', True),
    (f'https://{INTERNAL_HOST}/v1', '', True),
    (f'https://{INTERNAL_HOST}/v1', INTERNAL_HOST, False),
    ('https://judge.example.com/v1', '', False),
])
def test_personal_gateways_on_internal_addresses_are_refused(environment, internal_gateway, base_url, allowed, refused):
    client, app, deps, cfg = environment
    cfg.judge_allowed_hosts = allowed
    response = client.put('/evaluation/api/judge-settings/me', json={'base_url': base_url, 'model': 'synthetic',
                                                                       'api_key': 'synthetic-judge-key'})
    if refused:
        assert response.status_code == 422 and response.json()['code'] == 'judge_gateway_internal'
    else:
        assert response.status_code == 200, response.text


def test_a_saved_gateway_is_checked_again_before_each_call(environment, internal_gateway):
    client, app, deps, cfg = environment
    cfg.judge_allowed_hosts = internal_gateway
    saved = client.put('/evaluation/api/judge-settings/me', json={
        'base_url': f'https://{internal_gateway}/v1', 'model': 'synthetic', 'api_key': 'synthetic-judge-key'})
    assert saved.status_code == 200, saved.text
    cfg.judge_allowed_hosts = ''
    owner = client.get('/evaluation/api/session').json()['subject']
    with pytest.raises(Failure) as raised:
        app.state.engine.judge.settings.effective(owner)
    assert raised.value.code == 'judge_gateway_internal'


def test_the_deployment_judge_host_is_always_allowed(tmp_path):
    cfg = Config(tmp_path, judge_url='http://127.0.0.1:9000')
    settings = judge_settings.JudgeSettings(cfg, Store(tmp_path))
    assert settings.reachable('http://127.0.0.1:9000/v1') == 'http://127.0.0.1:9000/v1'


def test_reports_do_not_wait_behind_suggestions_and_show_the_queue(service, monkeypatch):
    client, app, deps, modeling, verdicts = service
    import threading
    release = threading.Event()
    deps.on_suggestions = release.wait
    verdicts['Synthetic failing question'] = 'fail'
    jobs = app.state.jobs
    records = []
    for name in ('first', 'second', 'third'):
        # One package runs one evaluation at a time; each report is ready while earlier suggestions still wait.
        record = start(client, preparation(client, 'prepare_' + name), [('Synthetic failing question', '2')], 'run_' + name)
        artifacts = until(lambda: client.get(f"/evaluation/api/runs/{record['id']}").json()['artifacts'], 'report')
        assert artifacts['report'] == 'ready'
        records.append(record)
    third = records[-1]
    # Two suggestion workers are busy waiting on the model; the third run's suggestions wait in line.
    queued = until(lambda: client.get(f"/evaluation/api/runs/{third['id']}").json()['artifacts'].get('queue') is not None
                   and client.get(f"/evaluation/api/runs/{third['id']}").json()['artifacts'])
    assert queued['queue'] == 0 and queued['suggestions'] == 'generating'
    release.set()
    settled(client, third['id'])
    assert jobs.queue == type(jobs.queue)()


def test_suggestions_with_an_expired_login_ask_to_sign_in_again(service):
    client, app, deps, modeling, verdicts = service
    verdicts['Synthetic failing question'] = 'fail'
    original = deps.json

    def expired(method, base, path, **kwargs):
        if path.startswith('/api/packages/') and deps.expire:
            raise Failure('dependency_rejected', '依赖服务拒绝请求', 401, 'dependency', upstream_status=401)
        return original(method, base, path, **kwargs)

    deps.expire = False
    deps.json = expired
    record = start(client, preparation(client), [('Synthetic failing question', '2')], 'run_expired')
    settled(client, record['id'])
    deps.expire = True
    client.post(f"/evaluation/api/runs/{record['id']}/artifacts/regenerate")
    failed = settled(client, record['id'], suggestions=('failed',))
    assert failed['artifacts']['suggestions_error'] == '登录已过期，请重新登录后点击“重新生成”'
