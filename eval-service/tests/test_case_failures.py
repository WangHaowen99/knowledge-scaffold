"""Cases without an answer explain consumer's reason; all identities, runs and errors are synthetic."""
import time

import pytest

from evaluation import execution
from test_evaluation import environment, preparation, run, wait


def remote_result(deps, payload):
    original = deps.json

    def dependency(method, base, path, **kwargs):
        if path.startswith('/agentic/runs/') and method == 'GET':
            return payload
        return original(method, base, path, **kwargs)

    deps.json = dependency


@pytest.mark.parametrize('payload,code,expected', [
    ({'status': 'failed', 'error': {'error_code': 'TOOL_LOOP_STOPPED', 'message': 'Synthetic repeated tool failure'}},
     'consumer_run_failed', 'consumer 执行失败（TOOL_LOOP_STOPPED）：Synthetic repeated tool failure'),
    ({'status': 'failed', 'error': {'error_code': 'SERVICE_RESTARTED', 'message': 'Execution failed'}},
     'consumer_run_failed', 'consumer 执行失败（SERVICE_RESTARTED）'),
    ({'status': 'failed'}, 'consumer_run_failed', 'consumer 执行失败（UNKNOWN）'),
    ({'status': 'completed', 'result': {'content': ''}}, 'consumer_empty_answer', 'consumer 运行已完成，但未返回可判分的回答'),
    ({'status': 'awaiting_input'}, 'consumer_awaiting_input', 'consumer 运行在等待用户补充信息，评测无法交互作答'),
])
def test_execution_errors_carry_consumer_reason(environment, payload, code, expected):
    client, app, deps, cfg = environment
    remote_result(deps, payload)
    record = run(client, preparation(client))
    wait(client, record['id'])
    listed = client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items'][0]
    assert listed['status'] == 'execution_error'
    assert listed['error']['code'] == code and listed['error']['message'] == expected
    answer = client.get(f"/evaluation/api/runs/{record['id']}/results/0/answer").json()
    assert answer['error']['message'] == expected


def test_long_consumer_messages_are_bounded(environment):
    client, app, deps, cfg = environment
    remote_result(deps, {'status': 'failed', 'error': {'error_code': 'SYNTHETIC', 'message': 'x' * 5000}})
    record = run(client, preparation(client))
    wait(client, record['id'])
    answer = client.get(f"/evaluation/api/runs/{record['id']}/results/0/answer").json()
    assert len(answer['error']['message']) <= 1100
    listed = client.get(f"/evaluation/api/runs/{record['id']}/results").json()['items'][0]
    assert len(listed['error']['message']) == 256


PREPARE_FAILED = {'status': 'failed', 'error': {'error_code': 'SESSION_PREPARE_FAILED', 'message': '会话运行状态准备失败'}}


def prepare_failures(deps, monkeypatch, failing, delay=0):
    """Remote runs listed in ``failing`` end before they start; records each submission and requested retry delay."""
    original, submitted, delays = deps.json, [], []

    def dependency(method, base, path, **kwargs):
        if path == '/agentic/runs' and method == 'POST':
            submitted.append((kwargs['headers']['Idempotency-Key'], kwargs['json']['session_id']))
        if path.startswith('/agentic/runs/') and method == 'GET' and path.rsplit('/', 1)[1] in failing:
            return failing[path.rsplit('/', 1)[1]]
        return original(method, base, path, **kwargs)

    deps.json = dependency
    monkeypatch.setattr(execution, 'retry_delay', lambda attempt: delays.append(attempt) or delay)
    return submitted, delays


def test_failed_session_preparation_is_retried_in_a_new_session(environment, monkeypatch):
    client, app, deps, cfg = environment
    submitted, delays = prepare_failures(deps, monkeypatch, {'remote_1': PREPARE_FAILED, 'remote_2': PREPARE_FAILED})
    record = run(client, preparation(client))
    result = wait(client, record['id'])
    assert result['status'] == 'completed', result
    rid = record['id']
    assert submitted == [(f'{rid}-0', f'eval-{rid}-0'), (f'{rid}-0-1', f'eval-{rid}-0-1'), (f'{rid}-0-2', f'eval-{rid}-0-2')]
    assert delays == [1, 2]
    case = client.get(f'/evaluation/api/runs/{rid}/results/0/answer').json()
    assert case['status'] == 'settled' and case['attempt'] == 2 and case['remote_id'] == 'remote_3'
    assert case['evidence']['remote_run_id'] == 'remote_3'


def test_failed_session_preparation_gives_up_after_six_submissions(environment, monkeypatch):
    client, app, deps, cfg = environment
    # Older consumers carry no error code; the message alone identifies the failure.
    legacy = {'status': 'failed', 'error': {'message': '会话运行状态准备失败'}}
    submitted, delays = prepare_failures(deps, monkeypatch, {f'remote_{n}': legacy for n in range(1, 8)})
    record = run(client, preparation(client))
    wait(client, record['id'])
    assert len(submitted) == execution.SESSION_ATTEMPTS == 6
    assert len({session for _, session in submitted}) == 6 and delays == [1, 2, 3, 4, 5]
    case = client.get(f"/evaluation/api/runs/{record['id']}/results/0/answer").json()
    assert case['status'] == 'execution_error'
    assert case['error']['message'] == 'consumer 执行失败（UNKNOWN）：会话运行状态准备失败（已换新会话重试 5 次）'


def test_other_consumer_failures_are_not_retried(environment, monkeypatch):
    client, app, deps, cfg = environment
    other = {'status': 'failed', 'error': {'error_code': 'TOOL_LOOP_STOPPED', 'message': 'Synthetic repeated tool failure'}}
    submitted, delays = prepare_failures(deps, monkeypatch, {'remote_1': other})
    record = run(client, preparation(client))
    wait(client, record['id'])
    assert len(submitted) == 1 and delays == []
    case = client.get(f"/evaluation/api/runs/{record['id']}/results/0/answer").json()
    assert case['status'] == 'execution_error' and 'attempt' not in case


def test_cancelling_during_retry_wait_submits_nothing_more(environment, monkeypatch):
    client, app, deps, cfg = environment
    submitted, delays = prepare_failures(deps, monkeypatch, {'remote_1': PREPARE_FAILED}, delay=30)
    record = run(client, preparation(client))
    rid = record['id']
    for _ in range(200):
        if delays: break
        time.sleep(.02)
    assert client.post(f'/evaluation/api/runs/{rid}/cancel', json={}).status_code == 200
    result = wait(client, rid, statuses=('cancelled',))
    assert result['status'] == 'cancelled'
    assert len(submitted) == 1
    assert client.get(f'/evaluation/api/runs/{rid}/results/0/answer').json()['status'] == 'cancelled'


def test_retry_delay_doubles_up_to_thirty_seconds_with_jitter():
    assert all(1.6 <= execution.retry_delay(1) <= 2.4 for _ in range(50))
    assert all(6.4 <= execution.retry_delay(3) <= 9.6 for _ in range(50))
    assert all(24 <= execution.retry_delay(n) <= 36 for n in (5, 9) for _ in range(50))
