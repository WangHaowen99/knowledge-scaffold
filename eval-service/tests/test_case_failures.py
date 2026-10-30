"""Cases without an answer explain consumer's reason; all identities, runs and errors are synthetic."""
import pytest

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
