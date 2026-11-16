"""Synthetic query receipts: equal answers cannot replace required execution evidence."""
import json

import pytest

from evaluation import execution_evidence as evidence
from evaluation import artifacts, carryover, suggestions
from test_evaluation import environment, preparation
from test_run_artifacts import settled, start


def replay(name='business_data_search', status='completed', count=1):
    events = []
    def emit(kind, payload):
        events.append({'seq': len(events) + 1, 'event_type': kind, 'payload': payload})
    emit('run.started', {})
    for number in range(count):
        key = f'synthetic-call-{number}'
        emit('tool.started', {'name': name, 'tool_call_id': key,
                             'input_preview': {'entity': 'synthetic_item', 'action': 'query'}})
        emit('tool.finished', {'name': name, 'tool_call_id': key, 'status': status,
                              'output_preview': json.dumps({'records': [{'count': 2}],
                                  'evidence': [{'query_digest': 'synthetic-query-digest'}]})})
    emit('turn.finished', {'status': 'completed'})
    return events


def stream(events):
    return ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)


def gate(events):
    collected = evidence.collect(events)
    return evidence.check(evidence.requirement('请查询配套业务数据，统计合成记录数量。'), collected)


@pytest.mark.parametrize('question,mode', [
    ('请查询配套业务数据，统计合成记录数量。', 'business_query'),
    ('请查询\n业务数据，统计合成记录数量。', 'business_query'),
    ('Query the database for synthetic records.', 'business_query'),
    ('独立假设场景，无需查询业务数据，计算 1+1。', 'reasoning_only'),
    ('仅依据题干，计算 1+1。', 'reasoning_only'),
    ('独立假设场景。但请查询业务数据。', 'ambiguous'),
])
def test_requirements(question, mode):
    assert evidence.requirement(question)['mode'] == mode
    assert evidence.requirement(question, 'business_query')['source'] == 'explicit'


@pytest.mark.parametrize('damage', ['absent', 'gap', 'no_terminal', 'unclosed', 'truncated', 'no_anchor', 'overflow',
                                    'input_redacted', 'output_redacted'])
def test_incomplete_receipts_cannot_pass(damage):
    events = replay(count=21 if damage == 'overflow' else 1)
    if damage == 'absent': events = []
    if damage == 'gap': events[1]['seq'] = 99
    if damage == 'no_terminal': events.pop()
    if damage == 'unclosed': events.pop(2)
    if damage == 'truncated': events[2]['payload']['output_truncated'] = True
    if damage == 'input_redacted': events[1]['payload']['omitted'] = {'sensitive_fields_redacted': 1}
    if damage == 'output_redacted': events[2]['payload']['omitted'] = {'sensitive_fields_redacted': 1}
    if damage == 'no_anchor': events[2]['payload']['output_preview'] = '{"evidence":"I queried"}'
    assert gate(events)['status'] == 'unverifiable'


def test_no_query_ontology_only_failure_and_success():
    assert gate(replay(count=0))['status'] == 'missing_query'
    assert gate(replay(name='ontology_context'))['status'] == 'missing_query'
    assert gate(replay(status='failed'))['status'] == 'query_failed'
    assert gate(replay())['status'] == 'review'
    assert gate(replay(count=13))['status'] == 'review'  # Beyond old suggestion preview limit.
    events = replay(name='execute_script')
    assert gate(events)['status'] == 'missing_query'
    events[2]['payload']['business_query_audit'] = {'status': 'completed', 'plan_digest': 'synthetic-plan', 'action': 'query'}
    assert gate(events)['status'] == 'review'


@pytest.mark.parametrize('assessment,expected', [
    ({'status': 'supported', 'reason': 'Synthetic record count matches', 'receipt_ids': ['synthetic-call-0']}, 'verified'),
    ({'status': 'supported', 'reason': 'Synthetic claim', 'receipt_ids': ['forged']}, 'unverifiable'),
    ({'status': 'supported', 'reason': 'Synthetic claim', 'receipt_ids': []}, 'unverifiable'),
    ({'status': 'unsupported', 'reason': 'Synthetic unrelated query', 'receipt_ids': []}, 'unsupported'),
    ({}, 'unverifiable'),
])
def test_judge_must_cite_actual_receipts(assessment, expected):
    collected = evidence.collect(replay())
    answer = {'verdict': 'pass', 'reason': 'Synthetic matching answer'}
    result, verification, status = evidence.finalize(answer, gate(replay()), collected, assessment)
    assert verification['status'] == expected
    assert (result or {}).get('verdict') == ('pass' if expected == 'verified' else 'fail' if expected == 'unsupported' else None)


@pytest.mark.parametrize('scenario,expected', [('missing', 'missing_query'), ('success', 'verified'),
    ('unrelated', 'unsupported'), ('failure', 'query_failed'), ('absent', 'unverifiable'), ('rule', 'not_required'),
    ('output_redacted', 'unverifiable'), ('multiline_missing', 'missing_query')])
def test_real_engine_query_gate_and_diagnostic_artifacts(environment, scenario, expected):
    client, app, deps, _ = environment
    events = replay(count=0 if scenario in ('missing', 'multiline_missing') else 1,
                    status='failed' if scenario == 'failure' else 'completed')
    if scenario == 'output_redacted':
        events[2]['payload']['omitted'] = {'sensitive_fields_redacted': 1}
    deps.events = '' if scenario == 'absent' else stream(events)
    original = deps.json
    received = []
    def score(method, base, path, **kwargs):
        if path == '/chat/completions':
            payload = json.loads(kwargs['json']['messages'][1]['content'])
            if 'execution_evidence' in payload:
                received.append(payload)
                value = {'verdict': 'pass', 'reason': 'Synthetic matching answer', 'execution': {
                    'status': 'unsupported' if scenario == 'unrelated' else 'supported',
                    'reason': 'Synthetic relevance check', 'receipt_ids': ['synthetic-call-0']}}
                return {'choices': [{'message': {'tool_calls': [{'function': {'name': 'submit_verdict', 'arguments': json.dumps(value)}}]}}]}
        return original(method, base, path, **kwargs)
    deps.json = score
    question = '纯规则：1+1？' if scenario == 'rule' else '请查询业务数据，统计合成记录数量。'
    if scenario == 'multiline_missing':
        question = '请查询\n业务数据，统计合成记录数量。'
    run = start(client, preparation(client), [(question, '2')], 'synthetic_evidence_run')
    rid = run['id']
    done = settled(client, rid)
    case = client.get(f'/evaluation/api/runs/{rid}/results/0/answer').json()
    assert case['execution_verification']['status'] == expected
    assert case['answer_judgement']['verdict'] == 'pass'
    assert case['question'] == question and case['reference'] == '2'
    if expected in ('verified', 'not_required'):
        assert case['judgement']['verdict'] == 'pass'
    else:
        assert (case.get('judgement') or {}).get('verdict') != 'pass'
        assert done['delivery']['state'] == 'diagnose'
        text = client.get(f'/evaluation/api/runs/{rid}/artifacts/suggestions.md').text
        assert '不据此生成知识包修订建议' in text
        assert suggestions.failed_cases({'cases': [case]}) == []
    stored = client.get(f'/evaluation/api/runs/{rid}/results/0/evidence').json()
    assert stored['execution']['trace_complete'] == (scenario != 'absent')
    assert bool(received) == (scenario in ('success', 'unrelated'))
    if expected == 'unverifiable':
        assert done['summary']['execution_unverified'] == 1
        report = client.get(f'/evaluation/api/runs/{rid}/artifacts/report.md').text
        assert '无法核实' in report


def test_explicit_mode_validation_and_frozen_snapshot(environment):
    client, _, _, _ = environment
    payload = {'name': 'Synthetic explicit mode', 'cases': [{'question': 'Synthetic records?', 'reference': '2', 'execution_mode': 'business_query'}]}
    created = client.post('/evaluation/api/eval-sets', json=payload).json()
    prep = preparation(client)
    response = client.post('/evaluation/api/runs', json={'request_id': 'synthetic_frozen', 'preparation_id': prep['id'], 'eval_set_id': created['id']})
    rid = response.json()['id']
    current = client.get(f"/evaluation/api/eval-sets/{created['id']}").json()
    payload['cases'][0]['execution_mode'] = 'reasoning_only'
    assert client.put(f"/evaluation/api/eval-sets/{created['id']}", json={**payload, 'revision': current['revision']}).status_code == 200
    settled(client, rid)
    case = client.get(f'/evaluation/api/runs/{rid}/results/0/answer').json()
    assert case['execution_requirement']['mode'] == 'business_query'
    payload['cases'][0]['execution_mode'] = 'invalid'
    assert client.post('/evaluation/api/eval-sets', json=payload).status_code == 422
